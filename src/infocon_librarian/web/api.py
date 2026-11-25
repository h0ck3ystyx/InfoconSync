"""Authenticated JSON API routes."""
from __future__ import annotations

import json
import queue
import shutil
import sqlite3
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
from flask import Blueprint, Response, current_app, g, jsonify, request, stream_with_context
from flask.typing import ResponseReturnValue

from infocon_librarian.archive.root import ArchiveRootInfo, validate_root
from infocon_librarian.domain.errors import ArchiveRootError
from infocon_librarian.domain.paths import is_valid_path_component
from infocon_librarian.remote.mirror import (
    MirrorSite,
    directory_entries,
    get_site,
    list_sites,
    managed_sections,
    mirror_client,
    mirror_path,
    name_key,
    public_base_url,
    valid_component,
)
from infocon_librarian.storage.repositories import ArchiveRootRepository
from infocon_librarian.web.auth import require_csrf, require_session


def _is_safe_collection_id(cid: object) -> bool:
    """True if *cid* is a well-formed "section/key" collection id.

    Each of the two segments must be a safe single path component (see
    :func:`is_valid_path_component`) and must not start with "." (hidden
    directories/traversal-adjacent names are excluded defensively, even
    though "." itself is already rejected).
    """
    parts = str(cid).split("/")
    if len(parts) != 2:
        return False
    return all(is_valid_path_component(p) and not p.startswith(".") for p in parts)

api_bp = Blueprint("api", __name__, url_prefix="/api")

# Global SSE broker: maps session_key -> queue of event dicts
_sse_lock = threading.Lock()
_sse_queues: dict[str, queue.Queue] = {}  # type: ignore[type-arg]

# Latest progress snapshot per plan item (item_id -> progress dict)
# Lets get_plan return live bytes/peers/rate on initial load.
_item_progress_cache: dict[str, dict[str, Any]] = {}


def _broadcast(event: dict[str, Any]) -> None:
    if event.get("type") == "progress":
        _item_progress_cache[event["item_id"]] = event
    with _sse_lock:
        dead = []
        for key, q in _sse_queues.items():
            try:
                q.put_nowait(event)
            except queue.Full:
                dead.append(key)
        for key in dead:
            del _sse_queues[key]


def _space_estimate(items: list[Any], root_path: str | None) -> dict[str, Any]:
    """Estimate whether the archive volume has room for a plan's remaining transfers.

    ``bytes_needed`` sums every item not yet complete. It is an upper bound, not
    an exact figure: items that are partially downloaded, or collections with
    ``has_older_version`` content already on disk, need less than their full
    torrent/HTTP size — we don't have per-file remaining-bytes at plan level.
    """
    remaining = [item for item in items if item.status != "complete"]
    bytes_needed = sum(item.size_bytes or 0 for item in remaining)
    unknown_size_items = sum(item.size_bytes is None for item in remaining)
    free_bytes = None
    if root_path:
        try:
            free_bytes = shutil.disk_usage(root_path).free
        except OSError:
            free_bytes = None
    return {
        "bytes_needed": bytes_needed,
        "unknown_size_items": unknown_size_items,
        "free_bytes": free_bytes,
        "sufficient": free_bytes is None or free_bytes >= bytes_needed,
    }


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@api_bp.route("/health")
@require_session
def health() -> Response:
    import shutil

    root_info = getattr(g, "archive_root_info", None)
    db_ok = getattr(g, "db_ok", False)

    payload: dict[str, Any] = {
        "status": "ok",
        "database": "ok" if db_ok else "error",
        "archive_root": None,
    }

    runtime = current_app.config.get("SERVICE_RUNTIME")
    if runtime is not None:
        payload["service"] = "stopped" if runtime.stopped.is_set() else "running"
        payload["service_message"] = runtime.reason
        if runtime.stopped.is_set():
            payload["status"] = "degraded"
            return jsonify(payload)
    if root_info is not None:
        try:
            usage = shutil.disk_usage(root_info.canonical_path)
        except OSError:
            payload["status"] = "degraded"
            return jsonify(payload)
        payload["archive_root"] = {
            "path": root_info.canonical_path,
            "volume_fingerprint": root_info.volume_fingerprint,
            "free_bytes": usage.free,
            "known_sections": root_info.known_sections,
        }

    return jsonify(payload)


@api_bp.route("/mirror-sites", methods=["GET", "POST"])
@require_session
def mirror_sites() -> ResponseReturnValue:
    if request.method == "POST":
        return add_mirror_site()
    db = getattr(g, "db", None)
    if db is None:
        return jsonify({"error": "not_configured"}), 503
    root = getattr(g, "archive_root_info", None)
    return jsonify([_site_details(site, root) for site in list_sites(db)])


def _site_details(site: MirrorSite, root: ArchiveRootInfo | None) -> dict[str, Any]:
    return {
        "id": site.id, "name": site.name, "base_url": site.base_url, "folder": site.folder,
        "local_path": site.local_path,
        "destination": str(site.destination(Path(root.canonical_path)))
        if root else site.local_path,
    }


def _site_archive(site: MirrorSite, default: ArchiveRootInfo) -> ArchiveRootInfo:
    if site.local_path is None:
        return default
    path = Path(site.local_path)
    runtime = current_app.config.get("SERVICE_RUNTIME")
    if runtime is not None and not path.resolve().is_relative_to(runtime.guard.root):
        raise ArchiveRootError("Choose a destination inside the configured NAS archive root.")
    db_path: Path | None = current_app.config.get("_DB_PATH")
    archive = validate_root(path, app_data_dir=db_path.parent if db_path else None)
    if archive.canonical_path != site.local_path:
        raise ArchiveRootError("Destination path changed on disk. Edit the site to confirm it.")
    if runtime is not None and path.stat().st_dev != runtime.guard.root.stat().st_dev:
        raise ArchiveRootError("Choose a folder on the configured NAS volume.")
    return archive


def _destination_conflict(
    site: MirrorSite, others: list[MirrorSite], default_root: Path,
) -> bool:
    path = tuple(map(name_key, site.destination(default_root).parts))
    for other in others:
        if other.id == site.id:
            continue
        target = tuple(map(name_key, other.destination(default_root).parts))
        if site.id == "infocon" and target[:-1] == path:
            continue
        if other.id == "infocon" and path[:-1] == target:
            continue
        if path[:len(target)] == target or target[:len(path)] == path:
            return True
    return False


@api_bp.route("/mirror-sites/<site_id>", methods=["PUT"])
@require_session
@require_csrf
def edit_mirror_site(site_id: str) -> ResponseReturnValue:
    db = getattr(g, "db", None)
    root = getattr(g, "archive_root_info", None)
    if db is None or root is None:
        return jsonify({"error": "not_configured"}), 503
    site = get_site(db, site_id)
    if site is None:
        return jsonify({"error": "unknown_site"}), 404
    body = request.get_json(silent=True)
    fields = {"name", "base_url", "local_path"}
    if not isinstance(body, dict) or not set(body) <= fields or not body:
        return jsonify({"error": "Enter site settings to save."}), 422
    if not all(isinstance(value, str) for value in body.values()):
        return jsonify({"error": "Site settings must be text."}), 422

    name = body.get("name", site.name).strip()
    if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
        return jsonify({"error": "Enter a site name of up to 80 characters."}), 422
    local_path = body.get("local_path", site.local_path) or None
    base_url = body.get("base_url", site.base_url)
    try:
        if local_path is not None:
            path = Path(local_path).expanduser()
            if not path.is_absolute():
                raise ValueError("Enter the full path to an existing folder.")
            local_path = str(path.resolve(strict=True))
        if base_url != site.base_url:
            if site.id in {"infocon", "defcon"}:
                raise ValueError("The remote URL is fixed for built-in sites.")
            base_url = public_base_url(base_url)
            if base_url != site.base_url:
                with mirror_client(base_url) as client:
                    directory_entries(client, base_url)
        updated = replace(site, name=name, base_url=base_url, local_path=local_path)
        if local_path != site.local_path:
            _site_archive(updated, root)
    except (ArchiveRootError, OSError, ValueError, httpx.HTTPError) as exc:
        return jsonify({"error": str(exc)}), 422

    changed = (updated.local_path, updated.base_url) != (site.local_path, site.base_url)
    try:
        db.execute("BEGIN IMMEDIATE")
        if get_site(db, site_id) != site:
            db.rollback()
            return jsonify({"error": "This site was edited elsewhere. Refresh Settings."}), 409
        if changed and _destination_conflict(
            updated, list_sites(db), Path(root.canonical_path),
        ):
            db.rollback()
            return jsonify({"error": "That destination overlaps another mirror site."}), 409
        if changed and db.execute(
            "SELECT 1 FROM plans WHERE state='running' AND (site_id=? OR site_id='infocon')",
            (site_id,),
        ).fetchone():
            db.rollback()
            return jsonify({"error": "Wait for active transfers before changing this site."}), 409
        db.execute(
            "UPDATE mirror_sites SET name=?, base_url=?, local_path=?,"
            " version=version+? WHERE id=?",
            (name, base_url, local_path, int(changed), site_id),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return jsonify({"error": "A site with that name or URL already exists."}), 409
    return jsonify({**_site_details(updated, root), "requires_check": changed})


@require_csrf
def add_mirror_site() -> ResponseReturnValue:
    db = getattr(g, "db", None)
    root_info = getattr(g, "archive_root_info", None)
    if db is None or root_info is None:
        return jsonify({"error": "not_configured"}), 503

    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) != {"name", "base_url", "folder"}:
        return jsonify({"error": "Name, directory URL and local folder are required."}), 422
    if not all(isinstance(value, str) for value in body.values()):
        return jsonify({"error": "Name, directory URL and local folder must be text."}), 422
    name = body["name"].strip()
    folder = body["folder"]
    if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
        return jsonify({"error": "Enter a site name of up to 80 characters."}), 422
    if not valid_component(folder) or folder.startswith(".") or len(folder.encode()) > 200:
        return jsonify({"error": "Enter a single folder name inside the archive root."}), 422
    try:
        base_url = public_base_url(body["base_url"])
        root = Path(root_info.canonical_path)
        existing_folders = [site.folder for site in list_sites(db)]
        if any(name_key(folder) == name_key(value) for value in existing_folders):
            raise ValueError("That folder is already assigned to a mirror site.")
        if any(name_key(folder) == name_key(child.name) for child in root.iterdir()):
            raise ValueError("Choose a new folder name to keep your existing archive separate.")
        with mirror_client(base_url) as client:
            directory_entries(client, base_url)
    except (ValueError, OSError, httpx.HTTPError) as exc:
        return jsonify({"error": str(exc)}), 422

    site_id = str(uuid.uuid4())
    try:
        db.execute("BEGIN IMMEDIATE")
        if _managed_folder(db, folder):
            db.rollback()
            return jsonify({"error": "That folder is already assigned to a mirror site."}), 409
        db.execute(
            "INSERT INTO mirror_sites (id, name, base_url, folder) VALUES (?, ?, ?, ?)",
            (site_id, name, base_url, folder),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return jsonify({"error": "A site with that name, URL, or folder already exists."}), 409
    return jsonify(_site_details(MirrorSite(site_id, name, base_url, folder), root_info)), 201


def _managed_folder(db: sqlite3.Connection, name: str) -> bool:
    return any(site.folder and name_key(site.folder) == name_key(name) for site in list_sites(db))


def _managed_infocon_section(db: sqlite3.Connection, root: str, name: str) -> bool:
    default = Path(g.archive_root_info.canonical_path)
    return name_key(name) in managed_sections(db, Path(root), default)


def _public_check_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in row.items() if key != "_mirror"} for row in results]


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


@api_bp.route("/checks", methods=["POST"])
@require_session
@require_csrf
def create_check() -> ResponseReturnValue:
    from pathlib import Path  # noqa: PLC0415

    from flask import current_app  # noqa: PLC0415

    from infocon_librarian.services.check_runner import start_check_thread  # noqa: PLC0415
    from infocon_librarian.storage.repositories import (  # noqa: PLC0415
        ArchiveRootRepository,
        CheckRepository,
    )

    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "invalid_request"}), 422
    allowed = {"section", "fresh", "site_id"}
    unknown = set(body.keys()) - allowed
    if unknown:
        return jsonify({"error": "unknown_fields", "fields": sorted(unknown)}), 422

    section = body.get("section")
    if section is not None and (not isinstance(section, str) or not valid_component(section)):
        return jsonify({"error": "invalid_field", "field": "section"}), 422

    db = getattr(g, "db", None)
    root_info = getattr(g, "archive_root_info", None)
    db_path: Path | None = current_app.config.get("_DB_PATH")

    if db is None or root_info is None or db_path is None:
        return jsonify({"error": "not_configured"}), 503

    site = get_site(db, body.get("site_id", "infocon"))
    if site is None:
        return jsonify({"error": "unknown_site"}), 422
    default_root = Path(root_info.canonical_path)
    try:
        root_info = _site_archive(site, root_info)
    except (ArchiveRootError, OSError) as exc:
        return jsonify({"error": str(exc)}), 422
    if section is not None and (
        site.id != "infocon" or _managed_infocon_section(db, root_info.canonical_path, section)
    ):
        return jsonify({"error": "invalid_field", "field": "section"}), 422

    # Upsert archive root, create check record
    root_repo = ArchiveRootRepository(db)
    root_record = root_repo.get_by_path(str(root_info.canonical_path))
    if root_record is None:
        root_record = root_repo.upsert(
            str(root_info.canonical_path), root_info.volume_fingerprint
        )

    check_id = str(uuid.uuid4())
    check_repo = CheckRepository(db)
    check_repo.create(
        check_id, archive_root_id=root_record.id, section=section, site_id=site.id,
        site_version=site.version,
    )
    worker_args: dict[str, Any] = {
        "check_id": check_id,
        "db_path": db_path,
        "archive_root": Path(root_info.canonical_path),
        "broadcast": _broadcast,
        "adapter": current_app.config.get("_ADAPTER"),
    }
    if site.id == "infocon":
        start_check_thread(section=section, default_root=default_root, **worker_args)
    else:
        from infocon_librarian.services.mirror_check import start_mirror_check

        start_mirror_check(site=site, **worker_args)

    return jsonify({
        "check_id": check_id, "status": "running", "section": section, "site_id": site.id,
    }), 202


@api_bp.route("/checks/latest")
@require_session
def get_latest_check() -> ResponseReturnValue:
    db = getattr(g, "db", None)
    root_info = getattr(g, "archive_root_info", None)
    if db is None or root_info is None:
        return jsonify({"error": "not_configured"}), 503

    import json as _json  # noqa: PLC0415

    from infocon_librarian.storage.repositories import (  # noqa: PLC0415
        ArchiveRootRepository,
        CheckRepository,
    )

    site = get_site(db, request.args.get("site_id", "infocon"))
    if site is None:
        return jsonify({"error": "not_found"}), 404

    root_repo = ArchiveRootRepository(db)
    root_record = root_repo.get_by_path(site.local_path or root_info.canonical_path)
    if root_record is None:
        return jsonify({"error": "not_found"}), 404

    check_repo = CheckRepository(db)
    record = check_repo.get_latest_completed(root_record.id, site.id, site.version)
    if record is None:
        return jsonify({"error": "not_found"}), 404

    results = _json.loads(record.result_json) if record.result_json else []
    _apply_file_size_policy(results, db)
    return jsonify({
        "check_id": record.id,
        "site_id": record.site_id,
        "state": record.state,
        "section": record.section,
        "started_at": record.started_at,
        "completed_at": record.completed_at,
        "error": record.error,
        "results": _public_check_rows(results),
        "count": len(results),
    })


@api_bp.route("/checks/<check_id>")
@require_session
def get_check(check_id: str) -> ResponseReturnValue:
    if not _is_valid_uuid(check_id):
        return jsonify({"error": "not_found"}), 404

    db = getattr(g, "db", None)
    if db is None:
        return jsonify({"error": "not_configured"}), 503

    import json as _json  # noqa: PLC0415

    from infocon_librarian.storage.repositories import CheckRepository  # noqa: PLC0415

    repo = CheckRepository(db)
    record = repo.get(check_id)
    site = get_site(db, record.site_id) if record else None
    if record is None or site is None or record.site_version != site.version:
        return jsonify({"error": "not_found"}), 404

    results = _json.loads(record.result_json) if record.result_json else []
    _apply_file_size_policy(results, db)
    return jsonify({
        "check_id": record.id,
        "site_id": record.site_id,
        "state": record.state,
        "section": record.section,
        "started_at": record.started_at,
        "completed_at": record.completed_at,
        "error": record.error,
        "results": _public_check_rows(results),
        "count": len(results),
    })


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def _plan_in_current_archive(db: sqlite3.Connection, plan: Any, path: str) -> bool:
    root = ArchiveRootRepository(db).get_by_path(path)
    return root is not None and root.id == plan.archive_root_id


@api_bp.route("/plans")
@require_session
def list_plans() -> ResponseReturnValue:
    db = getattr(g, "db", None)
    if db is None:
        return jsonify({"error": "not_configured"}), 503

    from infocon_librarian.storage.plan_repository import PlanRepository  # noqa: PLC0415

    repo = PlanRepository(db)
    plans = repo.list_plans()
    result = []
    for p in plans:
        items = repo.list_items(p.id)
        result.append({
            "plan_id": p.id,
            "site_id": p.site_id,
            "state": p.state,
            "created_at": p.created_at,
            "item_count": len(items),
        })
    return jsonify(result)


@api_bp.route("/plans", methods=["POST"])
@require_session
@require_csrf
def create_plan() -> ResponseReturnValue:
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "invalid_request"}), 422
    allowed = {
        "collection_ids", "policy", "torrent_mode", "allow_http_fallback", "site_id", "check_id",
    }
    unknown = set(body.keys()) - allowed
    if unknown:
        return jsonify({"error": "unknown_fields", "fields": sorted(unknown)}), 422

    # Reject any field that looks like a URL or absolute path
    for key in ("url", "path", "destination", "href", "src"):
        if key in body:
            return jsonify({"error": "forbidden_field", "field": key}), 403

    collection_ids = body.get("collection_ids") or []
    if not isinstance(collection_ids, list):
        return jsonify({"error": "invalid_field", "field": "collection_ids"}), 422

    site_id = body.get("site_id", "infocon")
    invalid = [
        cid for cid in collection_ids
        if not (
            _is_safe_collection_id(cid) if site_id == "infocon"
            else isinstance(cid, str) and len(cid) == 64
        )
    ]
    if invalid:
        return jsonify({"error": "invalid_collection_ids", "ids": invalid}), 422

    db = getattr(g, "db", None)
    root_info = getattr(g, "archive_root_info", None)

    if db is None or root_info is None:
        return jsonify({"error": "not_configured"}), 503

    from infocon_librarian.storage.plan_repository import PlanRepository  # noqa: PLC0415
    from infocon_librarian.storage.repositories import ArchiveRootRepository  # noqa: PLC0415

    site = get_site(db, site_id)
    if site is None:
        return jsonify({"error": "unknown_site"}), 422
    try:
        root_info = _site_archive(site, root_info)
    except (ArchiveRootError, OSError) as exc:
        return jsonify({"error": str(exc)}), 422

    root_repo = ArchiveRootRepository(db)
    root_record = root_repo.get_by_path(str(root_info.canonical_path))
    if root_record is None:
        root_record = root_repo.upsert(
            str(root_info.canonical_path), root_info.volume_fingerprint
        )

    plan_repo = PlanRepository(db)

    # Build a lookup of torrent URLs from the latest completed check
    from infocon_librarian.storage.repositories import CheckRepository  # noqa: PLC0415
    check_repo = CheckRepository(db)
    check_id = body.get("check_id")
    if check_id is not None and not _is_valid_uuid(check_id):
        return jsonify({"error": "invalid_check"}), 422
    latest_check = (
        check_repo.get(check_id) if check_id else check_repo.get_latest_completed(
            root_record.id, site.id, site.version,
        )
    )
    if (check_id or site.id != "infocon" or site.version > 1) and (
        latest_check is None or latest_check.site_id != site.id
        or latest_check.site_version != site.version
        or latest_check.archive_root_id != root_record.id or latest_check.state != "complete"
    ):
        return jsonify({"error": "Check this site before creating a plan."}), 409
    check_rows = (
        json.loads(latest_check.result_json) if latest_check and latest_check.result_json else []
    )
    if site.id != "infocon":
        by_id = {row["id"]: row for row in check_rows if row.get("selectable")}
        if not collection_ids or any(cid not in by_id for cid in collection_ids):
            return jsonify({"error": "Select items from the completed check."}), 422
        selections = [by_id[cid] for cid in dict.fromkeys(collection_ids)]
        try:
            mirror_root = Path(root_info.canonical_path)
            if site.relative_folder:
                mirror_root = mirror_path(mirror_root, site.relative_folder)
            for row in selections:
                manifest = row["_mirror"]
                for relative in [*manifest["dirs"], *(file[0] for file in manifest["files"])]:
                    mirror_path(mirror_root, relative)
        except (ValueError, OSError) as exc:
            return jsonify({"error": str(exc)}), 422

        record = plan_repo.create_plan(root_record.id, site.id, site.version)
        for row in selections:
            manifest = row["_mirror"]
            torrent = manifest.get("torrent_url")
            plan_repo.add_item(
                record.id, method="torrent" if torrent else "https", status="pending",
                collection_key=row["id"],
                destination_relpath=(Path(site.relative_folder) / row["key"]).as_posix(),
                url=torrent or manifest["url"], fallback_reason=None if torrent else "no_torrent",
                mirror_json=json.dumps(manifest), size_bytes=manifest.get("size_bytes"),
            )
    else:
        if any(
            _managed_infocon_section(db, root_info.canonical_path, cid.split("/")[0])
            for cid in collection_ids
        ):
            return jsonify({"error": "Folder belongs to another mirror site."}), 422
        torrent_urls_by_cid = {
            f"{row['section']}/{row['key']}": ev["payload"]["torrent_url"]
            for row in check_rows for ev in row.get("evidence", [])
            if ev.get("kind") == "remote_listing" and ev.get("payload", {}).get("torrent_url")
        }
        record = plan_repo.create_plan(root_record.id, site.id, site.version)
        for cid in collection_ids:
            torrent = torrent_urls_by_cid.get(cid)
            plan_repo.add_item(
                record.id, method="torrent" if torrent else "https", status="pending",
                collection_key=cid, destination_relpath=cid,
                url=torrent or f"{site.base_url}{cid}/",
                fallback_reason=None if torrent else "no_torrent",
            )

    items = plan_repo.list_items(record.id)
    _broadcast({"type": "plan_created", "plan_id": record.id})
    return jsonify({
        "plan_id": record.id,
        "site_id": record.site_id,
        "archive_root": root_info.canonical_path,
        "state": record.state,
        "created_at": record.created_at,
        "items": [
            {
                "item_id": i.id,
                "method": i.method,
                "status": i.status,
                "destination_relpath": i.destination_relpath,
                "fallback_reason": i.fallback_reason,
                "size_bytes": i.size_bytes,
            }
            for i in items
        ],
        "space": _space_estimate(items, root_info.canonical_path if root_info else None),
    }), 201


@api_bp.route("/plans/<plan_id>")
@require_session
def get_plan(plan_id: str) -> ResponseReturnValue:
    if not _is_valid_uuid(plan_id):
        return jsonify({"error": "not_found"}), 404

    db = getattr(g, "db", None)
    if db is None:
        return jsonify({"error": "not_configured"}), 503

    from infocon_librarian.storage.plan_repository import PlanRepository

    repo = PlanRepository(db)
    record = repo.get_plan(plan_id)
    if record is None:
        return jsonify({"error": "not_found"}), 404

    items = repo.list_items(plan_id)
    archived_root = ArchiveRootRepository(db).get(record.archive_root_id)
    path = archived_root.canonical_path if archived_root else None
    site = get_site(db, record.site_id)
    default = getattr(g, "archive_root_info", None)
    expected_path = (site.local_path or default.canonical_path) if site and default else None
    stale = site is None or record.site_version != site.version or path != expected_path
    return jsonify({
        "plan_id": record.id,
        "site_id": record.site_id,
        "archive_root": path,
        "destination_changed": stale,
        "state": record.state,
        "created_at": record.created_at,
        "items": [
            {
                "item_id": i.id,
                "method": i.method,
                "status": i.status,
                "destination_relpath": i.destination_relpath,
                "fallback_reason": i.fallback_reason,
                "size_bytes": i.size_bytes,
                "progress": _item_progress_cache.get(i.id),
            }
            for i in items
        ],
        "space": _space_estimate(items, path if not stale else None),
    })


@api_bp.route("/plans/<plan_id>/items/<item_id>/preview")
@require_session
def get_plan_item_preview(plan_id: str, item_id: str) -> ResponseReturnValue:
    from infocon_librarian.domain.errors import InvalidTorrent
    from infocon_librarian.services.plan_preview import preview_item
    from infocon_librarian.storage.plan_repository import PlanRepository
    from infocon_librarian.storage.settings_repository import SettingsRepository

    if not _is_valid_uuid(plan_id) or not _is_valid_uuid(item_id):
        return jsonify({"error": "not_found"}), 404
    db = getattr(g, "db", None)
    default = getattr(g, "archive_root_info", None)
    if db is None or default is None:
        return jsonify({"error": "not_configured"}), 503
    repo = PlanRepository(db)
    plan, item = repo.get_plan(plan_id), repo.get_item(item_id)
    if plan is None or item is None or item.plan_id != plan.id:
        return jsonify({"error": "not_found"}), 404
    if plan.state not in {"draft", "preflighted", "failed"}:
        return jsonify({"error": "The file preview is available before starting a transfer."}), 409
    if item.status == "complete":
        return jsonify({"error": "This item has already completed."}), 409

    site = get_site(db, plan.site_id)
    if site is None or site.version != plan.site_version:
        return jsonify({"error": "Site settings changed. Check Remote and create a new plan."}), 409
    archive_path = site.local_path or default.canonical_path
    if not _plan_in_current_archive(db, plan, archive_path):
        return jsonify({"error": "Plan belongs to a different archive."}), 409
    if site.id == "infocon" and _managed_infocon_section(
        db, archive_path, item.destination_relpath.split("/")[0],
    ):
        return jsonify({"error": "Plan targets a folder managed by another site."}), 409
    query = request.args.get("q", "").strip()
    try:
        offset = int(request.args.get("offset", "0"))
        if not 0 <= offset <= 1_000_000 or len(query) > 240:
            raise ValueError
    except ValueError:
        return jsonify({"error": "Invalid preview page or search."}), 422
    settings = SettingsRepository(db)
    archive = Path(archive_path)
    try:
        if not archive.is_dir() or archive.resolve(strict=True) != archive:
            return jsonify({"error": "The plan's archive folder is unavailable."}), 409
        return jsonify(preview_item(
            item, archive, site, adapter=current_app.config.get("_ADAPTER"),
            offset=offset, query=query,
            accept_larger=bool(settings.get_int("accept_larger_local_files")),
            force_https=settings.get_int("torrent_stall_timeout") == 0,
        ))
    except (OSError, ValueError, InvalidTorrent, httpx.HTTPError) as exc:
        return jsonify({"error": f"Could not preview this item. {exc}"}), 422


@api_bp.route("/plans/<plan_id>", methods=["DELETE"])
@require_session
@require_csrf
def delete_plan(plan_id: str) -> Response:
    if not _is_valid_uuid(plan_id):
        return jsonify({"error": "not_found"}), 404

    db = getattr(g, "db", None)
    if db is None:
        return jsonify({"error": "not_configured"}), 503

    from infocon_librarian.storage.plan_repository import PlanRepository

    repo = PlanRepository(db)
    record = repo.get_plan(plan_id)
    if record is None:
        return jsonify({"error": "not_found"}), 404
    if record.state == "running":
        return jsonify({"error": "cannot_delete_running_plan"}), 409

    repo.delete_plan(plan_id)
    return jsonify({"deleted": plan_id}), 200


@api_bp.route("/plans/<plan_id>/start", methods=["POST"])
@require_session
@require_csrf
def start_plan(plan_id: str) -> ResponseReturnValue:
    if not _is_valid_uuid(plan_id):
        return jsonify({"error": "not_found"}), 404

    db = getattr(g, "db", None)
    if db is None:
        return jsonify({"error": "not_configured"}), 503

    from infocon_librarian.storage.plan_repository import PlanRepository  # noqa: PLC0415

    repo = PlanRepository(db)
    record = repo.get_plan(plan_id)
    if record is None:
        return jsonify({"error": "not_found"}), 404

    root_info = getattr(g, "archive_root_info", None)
    db_path: Path | None = current_app.config.get("_DB_PATH")
    if root_info is None or db_path is None:
        return jsonify({"error": "not_configured"}), 503

    from infocon_librarian.services.http_transfer_runner import (
        start_http_transfer_thread,  # noqa: PLC0415
    )
    from infocon_librarian.services.torrent_transfer_runner import (
        start_torrent_transfer_thread,  # noqa: PLC0415,E501
    )

    # "failed" is restartable: a retry re-crawls, skips completed files, and
    # re-attempts the ones that failed (e.g. after a server reset).
    if record.state not in ("draft", "preflighted", "failed"):
        return jsonify({"error": "invalid_state", "current_state": record.state}), 409

    from infocon_librarian.storage.settings_repository import SettingsRepository  # noqa: PLC0415

    adapter = current_app.config.get("_ADAPTER")
    settings = SettingsRepository(db)
    stall_timeout = float(settings.get_int("torrent_stall_timeout"))

    site = get_site(db, record.site_id)
    if site is None or site.version != record.site_version:
        return jsonify({"error": "Site settings changed. Check again and create a new plan."}), 409
    try:
        root_info = _site_archive(site, root_info)
    except (ArchiveRootError, OSError) as exc:
        return jsonify({"error": str(exc)}), 422
    db.execute("BEGIN IMMEDIATE")
    if not _plan_in_current_archive(db, record, root_info.canonical_path):
        return jsonify({"error": "Plan belongs to a different archive."}), 409
    if record.site_id == "infocon" and any(
        _managed_infocon_section(
            db, root_info.canonical_path, item.destination_relpath.split("/")[0],
        )
        for item in repo.list_items(plan_id)
    ):
        return jsonify({"error": "Plan targets a folder managed by another site."}), 409
    if not repo.start_if_available(record):
        return jsonify({"error": "A transfer is already running for this site."}), 409

    # Requeue anything left incomplete by a prior attempt so a retry picks it up.
    repo.requeue_incomplete_items(plan_id)
    _broadcast({"type": "plan_started", "plan_id": plan_id})

    items = repo.list_items(plan_id)
    archive_root_path = Path(root_info.canonical_path)
    infocon_base = "https://infocon.org/"

    # If stall_timeout == 0, convert all torrent items to HTTPS before starting
    if stall_timeout == 0:
        for it in items:
            if it.method == "torrent" and it.status == "pending":
                https_url = (
                    json.loads(it.mirror_json)["url"] if it.mirror_json
                    else infocon_base + it.destination_relpath + "/"
                )
                repo.update_item_to_https(it.id, https_url)
        items = repo.list_items(plan_id)  # reload after conversion

    # Spawn torrent worker for pending torrent items (requires adapter)
    has_torrent = any(it.method == "torrent" and it.status == "pending" for it in items)
    if has_torrent:
        if adapter is not None:
            start_torrent_transfer_thread(
                plan_id,
                db_path=db_path,
                archive_root=archive_root_path,
                adapter=adapter,
                broadcast=_broadcast,
                stall_timeout=stall_timeout,
            )
        else:
            # No adapter — torrent items cannot run; mark them blocked so the user knows
            for it in items:
                if it.method == "torrent" and it.status == "pending":
                    repo.update_item_status(it.id, "blocked")
            _broadcast({"type": "plan_warning", "plan_id": plan_id,
                        "warning": "torrent_adapter_unavailable"})

    # Spawn HTTPS worker for pending HTTPS items
    if any(it.method == "https" and it.status == "pending" for it in items):
        start_http_transfer_thread(
            plan_id,
            db_path=db_path,
            archive_root=archive_root_path,
            broadcast=_broadcast,
        )

    return jsonify({"plan_id": plan_id, "state": "running"}), 200


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


@api_bp.route("/items/<item_id>/pause", methods=["POST"])
@require_session
@require_csrf
def pause_item(item_id: str) -> Response:
    if not _is_valid_uuid(item_id):
        return jsonify({"error": "not_found"}), 404

    db = getattr(g, "db", None)
    if db is None:
        return jsonify({"error": "not_configured"}), 503

    from infocon_librarian.storage.plan_repository import PlanRepository

    repo = PlanRepository(db)
    item = repo.get_item(item_id)
    if item is None:
        return jsonify({"error": "not_found"}), 404

    if item.status not in ("pending", "downloading"):
        return jsonify({"error": "invalid_state", "current_status": item.status}), 409

    repo.update_item_status(item_id, "paused")
    _broadcast({
        "type": "item_status",
        "plan_id": item.plan_id,
        "item_id": item_id,
        "status": "paused",
        "collection_key": item.collection_key,
    })
    return jsonify({"item_id": item_id, "status": "paused"}), 200


@api_bp.route("/items/<item_id>/resume", methods=["POST"])
@require_session
@require_csrf
def resume_item(item_id: str) -> Response:
    if not _is_valid_uuid(item_id):
        return jsonify({"error": "not_found"}), 404

    db = getattr(g, "db", None)
    if db is None:
        return jsonify({"error": "not_configured"}), 503

    from infocon_librarian.storage.plan_repository import PlanRepository

    repo = PlanRepository(db)
    item = repo.get_item(item_id)
    if item is None:
        return jsonify({"error": "not_found"}), 404

    if item.status != "paused":
        return jsonify({"error": "invalid_state", "current_status": item.status}), 409

    repo.update_item_status(item_id, "pending")
    _broadcast({
        "type": "item_status",
        "plan_id": item.plan_id,
        "item_id": item_id,
        "status": "pending",
        "collection_key": item.collection_key,
    })
    return jsonify({"item_id": item_id, "status": "pending"}), 200


@api_bp.route("/items/<item_id>/approve-http-fallback", methods=["POST"])
@require_session
@require_csrf
def approve_http_fallback(item_id: str) -> ResponseReturnValue:
    if not _is_valid_uuid(item_id):
        return jsonify({"error": "not_found"}), 404

    db = getattr(g, "db", None)
    root_info = getattr(g, "archive_root_info", None)
    db_path: Path | None = current_app.config.get("_DB_PATH")
    if db is None or root_info is None or db_path is None:
        return jsonify({"error": "not_configured"}), 503

    from infocon_librarian.services.http_transfer_runner import (
        start_http_transfer_thread,  # noqa: PLC0415
    )
    from infocon_librarian.storage.plan_repository import PlanRepository  # noqa: PLC0415

    repo = PlanRepository(db)
    item = repo.get_item(item_id)
    if item is None:
        return jsonify({"error": "not_found"}), 404

    if item.status != "blocked":
        return jsonify({"error": "not_blocked", "current_status": item.status}), 409

    plan = repo.get_plan(item.plan_id)
    site = get_site(db, plan.site_id) if plan else None
    if plan is None or site is None or site.version != plan.site_version:
        return jsonify({"error": "Site settings changed. Check again and create a new plan."}), 409
    try:
        root_info = _site_archive(site, root_info)
    except (ArchiveRootError, OSError) as exc:
        return jsonify({"error": str(exc)}), 422
    db.execute("BEGIN IMMEDIATE")
    if not _plan_in_current_archive(db, plan, root_info.canonical_path):
        return jsonify({"error": "Plan belongs to a different archive."}), 409

    if plan.site_id == "infocon" and _managed_infocon_section(
        db, root_info.canonical_path, item.destination_relpath.split("/")[0],
    ):
        return jsonify({"error": "Plan targets a folder managed by another site."}), 409

    # Switch this item from torrent → HTTPS and reset the plan to running
    https_url = (
        json.loads(item.mirror_json)["url"] if item.mirror_json
        else f"https://infocon.org/{item.destination_relpath}/"
    )
    if not repo.approve_https(item, plan, https_url):
        return jsonify({"error": "Another transfer is running for this site."}), 409

    # Notify browser before starting the thread so Transfers tab reloads
    _broadcast({"type": "http_fallback_approved", "item_id": item_id,
                "plan_id": item.plan_id})
    _broadcast({"type": "plan_status", "plan_id": item.plan_id, "state": "running"})

    start_http_transfer_thread(
        item.plan_id,
        db_path=db_path,
        archive_root=Path(root_info.canonical_path),
        broadcast=_broadcast,
    )

    return jsonify({"item_id": item_id, "status": "pending", "method": "https"}), 200


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


@api_bp.route("/verify", methods=["POST"])
@require_session
@require_csrf
def create_verify() -> ResponseReturnValue:
    from pathlib import Path  # noqa: PLC0415

    from flask import current_app  # noqa: PLC0415

    from infocon_librarian.services.verify_runner import start_verify_thread  # noqa: PLC0415
    from infocon_librarian.storage.repositories import (  # noqa: PLC0415
        ArchiveRootRepository,
        CheckRepository,
    )

    body = request.get_json(silent=True) or {}
    collection_id = body.get("collection_id")
    if not collection_id or not isinstance(collection_id, str):
        return jsonify({"error": "missing_field", "field": "collection_id"}), 422

    if not _is_safe_collection_id(collection_id):
        return jsonify({"error": "invalid_collection_id"}), 422

    adapter = current_app.config.get("_ADAPTER")
    if adapter is None:
        return jsonify({"error": "torrent_engine_unavailable",
                        "detail": "Install libtorrent to enable verification"}), 503

    db = getattr(g, "db", None)
    root_info = getattr(g, "archive_root_info", None)
    db_path: Path | None = current_app.config.get("_DB_PATH")

    if db is None or root_info is None or db_path is None:
        return jsonify({"error": "not_configured"}), 503

    site = get_site(db, "infocon")
    if site is None:
        return jsonify({"error": "unknown_site"}), 422
    try:
        root_info = _site_archive(site, root_info)
    except (ArchiveRootError, OSError) as exc:
        return jsonify({"error": str(exc)}), 422
    if _managed_infocon_section(db, root_info.canonical_path, collection_id.split("/")[0]):
        return jsonify({"error": "Folder belongs to another mirror site."}), 422

    # Find archive root record
    root_repo = ArchiveRootRepository(db)
    root_record = root_repo.get_by_path(str(root_info.canonical_path))
    if root_record is None:
        root_record = root_repo.upsert(
            str(root_info.canonical_path), root_info.volume_fingerprint
        )

    # Find torrent URL from most recent check result for this collection
    torrent_url: str | None = body.get("torrent_url")  # allow explicit override
    if not torrent_url:
        check_repo = CheckRepository(db)
        latest = check_repo.get_latest_completed(root_record.id, site.id, site.version)
        if latest and latest.result_json:
            results = json.loads(latest.result_json)
            for item in results:
                if f"{item.get('section', '')}/{item.get('key', '')}" == collection_id:
                    for ev in item.get("evidence", []):
                        if ev.get("kind") == "remote_listing":
                            torrent_url = ev.get("payload", {}).get("torrent_url")
                    break

    if not torrent_url:
        # Fallback: construct conventional InfoCon torrent URL
        key = collection_id.split("/", 1)[1]
        torrent_url = f"https://infocon.org/{collection_id}/{key}.torrent"

    verify_id, _ = start_verify_thread(
        collection_key=collection_id,
        archive_root_id=root_record.id,
        db_path=db_path,
        archive_root=Path(root_info.canonical_path),
        torrent_url=torrent_url,
        broadcast=_broadcast,
        adapter=adapter,
    )

    return jsonify({
        "verify_id": verify_id,
        "collection_id": collection_id,
        "torrent_url": torrent_url,
        "state": "running",
    }), 202


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

_RESET_ORDER = [
    "jobs",
    "receipts",
    "plan_items",
    "plans",
    "snapshot_entries",
    "snapshots",
    "torrent_files",
    "torrent_manifests",
    "remote_entries",
    "remote_fetches",
    "evidence",
    "checks",
    "verifications",
    "archive_roots",
]


@api_bp.route("/admin/reset", methods=["POST"])
@require_session
@require_csrf
def admin_reset() -> Response:
    """Clear all data tables, preserving schema_version."""
    db = getattr(g, "db", None)
    if db is None:
        return jsonify({"error": "not_configured"}), 503
    for table in _RESET_ORDER:
        db.execute(f"DELETE FROM {table}")  # noqa: S608
    db.commit()
    return jsonify({"reset": True}), 200


# ---------------------------------------------------------------------------
# SSE event stream
# ---------------------------------------------------------------------------


@api_bp.route("/events")
@require_session
def events() -> Response:
    client_key = str(uuid.uuid4())
    q: queue.Queue = queue.Queue(maxsize=64)  # type: ignore[type-arg]
    with _sse_lock:
        _sse_queues[client_key] = q

    @stream_with_context
    def _generate():
        # type: ignore[no-untyped-def]
        try:
            yield "data: {\"type\": \"connected\"}\n\n"
            while True:
                try:
                    event = q.get(timeout=15.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _sse_lock:
                _sse_queues.pop(client_key, None)

    return Response(
        _generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


@api_bp.route("/receipts")
@require_session
def list_receipts() -> Response:
    db = getattr(g, "db", None)
    if db is None:
        return jsonify({"error": "not_configured"}), 503

    from infocon_librarian.storage.plan_repository import PlanRepository  # noqa: PLC0415

    repo = PlanRepository(db)
    receipts = repo.list_receipts()
    return jsonify([
        {"receipt_id": r.id, "plan_id": r.plan_id, "completed_at": r.completed_at}
        for r in receipts
    ])


@api_bp.route("/receipts/<receipt_id>")
@require_session
def get_receipt(receipt_id: str) -> Response:
    if not _is_valid_uuid(receipt_id):
        return jsonify({"error": "not_found"}), 404

    db = getattr(g, "db", None)
    if db is None:
        return jsonify({"error": "not_configured"}), 503

    from infocon_librarian.storage.plan_repository import PlanRepository

    repo = PlanRepository(db)
    record = repo.get_receipt(receipt_id)
    if record is None:
        return jsonify({"error": "not_found"}), 404

    import pathlib

    json_path = pathlib.Path(record.json_path)
    if not json_path.exists():
        return jsonify({"error": "receipt_file_missing"}), 404

    try:
        body = json.loads(json_path.read_text())
    except Exception:
        return jsonify({"error": "receipt_unreadable"}), 500

    return jsonify(body)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_ALLOWED_SETTINGS = {
    "torrent_stall_timeout",
    "torrent_download_limit",
    "torrent_upload_limit",
    "http_concurrency",
    "accept_larger_local_files",
}


@api_bp.route("/settings", methods=["GET"])
@require_session
def get_settings() -> Response:
    db = getattr(g, "db", None)
    root_info = getattr(g, "archive_root_info", None)
    if db is None:
        return jsonify({"error": "not_configured"}), 503

    from infocon_librarian.storage.settings_repository import SettingsRepository  # noqa: PLC0415

    data = SettingsRepository(db).get_all()
    data["archive_root"] = root_info.canonical_path if root_info else None
    return jsonify(data)


@api_bp.route("/settings", methods=["PUT"])
@require_session
@require_csrf
def put_settings() -> Response | tuple[Response, int]:
    db = getattr(g, "db", None)
    if db is None:
        return jsonify({"error": "not_configured"}), 503

    from infocon_librarian.services.http_transfer_runner import (  # noqa: PLC0415
        HTTP_CONCURRENCY_MAX,
    )

    body = request.get_json(silent=True) or {}
    updates: dict[str, str] = {}
    for key in _ALLOWED_SETTINGS:
        if key not in body:
            continue
        if key == "accept_larger_local_files":
            value = body[key]
            if not ((type(value) is int and value in (0, 1))
                    or (type(value) is str and value in ("0", "1"))):
                return jsonify({"error": "invalid_value", "key": key}), 400
        try:
            val = int(body[key])
        except (TypeError, ValueError):
            return jsonify({"error": "invalid_value", "key": key}), 400
        # http_concurrency needs at least one worker and is capped at the same
        # ceiling the runner enforces; every other setting is just non-negative.
        valid = 1 <= val <= HTTP_CONCURRENCY_MAX if key == "http_concurrency" else val >= 0
        if not valid:
            return jsonify({"error": "invalid_value", "key": key}), 400
        updates[key] = str(val)

    from infocon_librarian.storage.settings_repository import SettingsRepository  # noqa: PLC0415

    SettingsRepository(db).update_many(updates)

    # Apply rate limits to the live adapter if one is running
    adapter = current_app.config.get("_ADAPTER")
    if adapter is not None and (
        "torrent_download_limit" in updates or "torrent_upload_limit" in updates
    ):
        repo = SettingsRepository(db)
        adapter.set_rate_limits(
            download_kbps=repo.get_int("torrent_download_limit"),
            upload_kbps=repo.get_int("torrent_upload_limit"),
        )

    return jsonify({"saved": list(updates.keys())}), 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_valid_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _apply_file_size_policy(results: list[dict[str, Any]], db: Any) -> None:
    """Old legacy classifications are not valid evidence under the strict default."""
    from infocon_librarian.storage.settings_repository import SettingsRepository

    if SettingsRepository(db).get_int("accept_larger_local_files"):
        return
    for item in results:
        if item.get("status") == "has_older_version":
            item["status"] = "changed_manifest"
        verification = item.get("verify_result")
        if verification and verification.get("level") == "has_older_version":
            verification["level"] = "unverified"
