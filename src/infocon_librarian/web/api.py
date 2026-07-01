"""JSON API routes — all IDs are server-issued; no browser input becomes a URL or path."""
from __future__ import annotations

import json
import queue
import re
import threading
import uuid
from pathlib import Path
from typing import Any

# Allowed format for collection_ids submitted by the browser (section/key).
# Prevents path traversal — no dots-dots, no slashes beyond the one separator.
_COLLECTION_ID_RE = re.compile(r'^[A-Za-z0-9_.() -]+/[A-Za-z0-9_.() -]+$')

from flask import Blueprint, Response, current_app, g, jsonify, request, stream_with_context

from infocon_librarian.web.auth import require_csrf, require_session

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

    if root_info is not None:
        usage = shutil.disk_usage(root_info.canonical_path)
        payload["archive_root"] = {
            "path": root_info.canonical_path,
            "volume_fingerprint": root_info.volume_fingerprint,
            "free_bytes": usage.free,
            "known_sections": root_info.known_sections,
        }

    return jsonify(payload)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


@api_bp.route("/checks", methods=["POST"])
@require_session
@require_csrf
def create_check() -> Response:
    from pathlib import Path  # noqa: PLC0415

    from flask import current_app  # noqa: PLC0415

    from infocon_librarian.services.check_runner import start_check_thread  # noqa: PLC0415
    from infocon_librarian.storage.repositories import (  # noqa: PLC0415
        ArchiveRootRepository,
        CheckRepository,
    )

    body = request.get_json(silent=True) or {}
    allowed = {"section", "fresh"}
    unknown = set(body.keys()) - allowed
    if unknown:
        return jsonify({"error": "unknown_fields", "fields": sorted(unknown)}), 422

    section = body.get("section")
    if section is not None and not isinstance(section, str):
        return jsonify({"error": "invalid_field", "field": "section"}), 422

    db = getattr(g, "db", None)
    root_info = getattr(g, "archive_root_info", None)
    db_path: Path | None = current_app.config.get("_DB_PATH")

    if db is None or root_info is None or db_path is None:
        return jsonify({"error": "not_configured"}), 503

    # Upsert archive root, create check record
    root_repo = ArchiveRootRepository(db)
    root_record = root_repo.get_by_path(str(root_info.canonical_path))
    if root_record is None:
        root_record = root_repo.upsert(
            str(root_info.canonical_path), root_info.volume_fingerprint
        )

    check_id = str(uuid.uuid4())
    check_repo = CheckRepository(db)
    check_repo.create(check_id, archive_root_id=root_record.id, section=section)

    # Start background worker — uses its own DB connection
    start_check_thread(
        check_id=check_id,
        db_path=db_path,
        archive_root=Path(root_info.canonical_path),
        section=section,
        broadcast=_broadcast,
        adapter=current_app.config.get("_ADAPTER"),
    )

    return jsonify({"check_id": check_id, "status": "running", "section": section}), 202


@api_bp.route("/checks/latest")
@require_session
def get_latest_check() -> Response:
    db = getattr(g, "db", None)
    root_info = getattr(g, "archive_root_info", None)
    if db is None or root_info is None:
        return jsonify({"error": "not_configured"}), 503

    import json as _json  # noqa: PLC0415

    from infocon_librarian.storage.repositories import (  # noqa: PLC0415
        ArchiveRootRepository,
        CheckRepository,
    )

    root_repo = ArchiveRootRepository(db)
    root_record = root_repo.get_by_path(str(root_info.canonical_path))
    if root_record is None:
        return jsonify({"error": "not_found"}), 404

    check_repo = CheckRepository(db)
    record = check_repo.get_latest_completed(root_record.id)
    if record is None:
        return jsonify({"error": "not_found"}), 404

    results = _json.loads(record.result_json) if record.result_json else []
    return jsonify({
        "check_id": record.id,
        "state": record.state,
        "section": record.section,
        "started_at": record.started_at,
        "completed_at": record.completed_at,
        "error": record.error,
        "results": results,
        "count": len(results),
    })


@api_bp.route("/checks/<check_id>")
@require_session
def get_check(check_id: str) -> Response:
    if not _is_valid_uuid(check_id):
        return jsonify({"error": "not_found"}), 404

    db = getattr(g, "db", None)
    if db is None:
        return jsonify({"error": "not_configured"}), 503

    import json as _json  # noqa: PLC0415

    from infocon_librarian.storage.repositories import CheckRepository  # noqa: PLC0415

    repo = CheckRepository(db)
    record = repo.get(check_id)
    if record is None:
        return jsonify({"error": "not_found"}), 404

    results = _json.loads(record.result_json) if record.result_json else []
    return jsonify({
        "check_id": record.id,
        "state": record.state,
        "section": record.section,
        "started_at": record.started_at,
        "completed_at": record.completed_at,
        "error": record.error,
        "results": results,
        "count": len(results),
    })


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


@api_bp.route("/plans")
@require_session
def list_plans() -> Response:
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
            "state": p.state,
            "created_at": p.created_at,
            "item_count": len(items),
        })
    return jsonify(result)


@api_bp.route("/plans", methods=["POST"])
@require_session
@require_csrf
def create_plan() -> Response:
    body = request.get_json(silent=True) or {}
    allowed = {"collection_ids", "policy", "torrent_mode", "allow_http_fallback"}
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

    # Validate each id: must match "section/key" pattern and contain no dotdot components
    def _safe_cid(cid: object) -> bool:
        s = str(cid)
        if not _COLLECTION_ID_RE.match(s):
            return False
        # Reject any segment that is ".." or starts with "." (hidden/traversal)
        return all(part and part != ".." and not part.startswith(".") for part in s.split("/"))

    invalid = [cid for cid in collection_ids if not _safe_cid(cid)]
    if invalid:
        return jsonify({"error": "invalid_collection_ids", "ids": invalid}), 422

    db = getattr(g, "db", None)
    root_info = getattr(g, "archive_root_info", None)

    if db is None or root_info is None:
        return jsonify({"error": "not_configured"}), 503

    from infocon_librarian.storage.plan_repository import PlanRepository  # noqa: PLC0415
    from infocon_librarian.storage.repositories import ArchiveRootRepository  # noqa: PLC0415

    root_repo = ArchiveRootRepository(db)
    root_record = root_repo.get_by_path(str(root_info.canonical_path))
    if root_record is None:
        root_record = root_repo.upsert(
            str(root_info.canonical_path), root_info.volume_fingerprint
        )

    plan_repo = PlanRepository(db)
    record = plan_repo.create_plan(root_record.id)

    # Build a lookup of torrent URLs from the latest completed check
    from infocon_librarian.storage.repositories import CheckRepository  # noqa: PLC0415
    check_repo = CheckRepository(db)
    latest_check = check_repo.get_latest_completed(root_record.id)
    torrent_urls_by_cid: dict[str, str] = {}
    if latest_check and latest_check.result_json:
        for chk_item in json.loads(latest_check.result_json):
            ckey = f"{chk_item.get('section', '')}/{chk_item.get('key', '')}"
            for ev in chk_item.get("evidence", []):
                if ev.get("kind") == "remote_listing":
                    turl = ev.get("payload", {}).get("torrent_url")
                    if turl:
                        torrent_urls_by_cid[ckey] = turl

    infocon_base = "https://infocon.org/"
    for cid in collection_ids:
        # cid already validated: "section/key" — no path traversal possible
        torrent_url = torrent_urls_by_cid.get(cid)
        if torrent_url:
            plan_repo.add_item(
                record.id,
                method="torrent",
                status="pending",
                collection_key=cid,
                destination_relpath=cid,
                url=torrent_url,
                fallback_reason=None,
            )
        else:
            plan_repo.add_item(
                record.id,
                method="https",
                status="pending",
                collection_key=cid,
                destination_relpath=cid,
                url=infocon_base + cid + "/",
                fallback_reason="no_torrent",
            )

    items = plan_repo.list_items(record.id)
    _broadcast({"type": "plan_created", "plan_id": record.id})
    return jsonify({
        "plan_id": record.id,
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
    }), 201


@api_bp.route("/plans/<plan_id>")
@require_session
def get_plan(plan_id: str) -> Response:
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
    return jsonify({
        "plan_id": record.id,
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
    })


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
def start_plan(plan_id: str) -> Response:
    if not _is_valid_uuid(plan_id):
        return jsonify({"error": "not_found"}), 404

    db = getattr(g, "db", None)
    root_info = getattr(g, "archive_root_info", None)
    db_path: Path | None = current_app.config.get("_DB_PATH")
    if db is None or root_info is None or db_path is None:
        return jsonify({"error": "not_configured"}), 503

    from infocon_librarian.services.http_transfer_runner import (
        start_http_transfer_thread,  # noqa: PLC0415
    )
    from infocon_librarian.services.torrent_transfer_runner import (
        start_torrent_transfer_thread,  # noqa: PLC0415,E501
    )
    from infocon_librarian.storage.plan_repository import PlanRepository  # noqa: PLC0415

    repo = PlanRepository(db)
    record = repo.get_plan(plan_id)
    if record is None:
        return jsonify({"error": "not_found"}), 404

    if record.state not in ("draft", "preflighted"):
        return jsonify({"error": "invalid_state", "current_state": record.state}), 409

    adapter = current_app.config.get("_ADAPTER")
    repo.update_plan_state(plan_id, "running")
    _broadcast({"type": "plan_started", "plan_id": plan_id})

    items = repo.list_items(plan_id)
    archive_root_path = Path(root_info.canonical_path)

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

    repo.update_item_status(item_id, "paused")
    _broadcast({"type": "item_paused", "item_id": item_id})
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

    repo.update_item_status(item_id, "pending")
    _broadcast({"type": "item_resumed", "item_id": item_id})
    return jsonify({"item_id": item_id, "status": "pending"}), 200


@api_bp.route("/items/<item_id>/approve-http-fallback", methods=["POST"])
@require_session
@require_csrf
def approve_http_fallback(item_id: str) -> Response:
    if not _is_valid_uuid(item_id):
        return jsonify({"error": "not_found"}), 404

    db = getattr(g, "db", None)
    root_info = getattr(g, "archive_root_info", None)
    db_path: Path | None = current_app.config.get("_DB_PATH")
    if db is None or root_info is None or db_path is None:
        return jsonify({"error": "not_configured"}), 503

    from infocon_librarian.services.http_transfer_runner import start_http_transfer_thread  # noqa: PLC0415
    from infocon_librarian.storage.plan_repository import PlanRepository  # noqa: PLC0415

    repo = PlanRepository(db)
    item = repo.get_item(item_id)
    if item is None:
        return jsonify({"error": "not_found"}), 404

    if item.status != "blocked":
        return jsonify({"error": "not_blocked", "current_status": item.status}), 409

    # Switch this item from torrent → HTTPS and start the HTTP worker for the plan
    https_url = f"https://infocon.org/{item.destination_relpath}/"
    repo.update_item_to_https(item_id, https_url)
    _broadcast({"type": "http_fallback_approved", "item_id": item_id})

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
def create_verify() -> Response:
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

    def _safe_cid(cid: str) -> bool:
        if not _COLLECTION_ID_RE.match(cid):
            return False
        return all(part and part != ".." and not part.startswith(".") for part in cid.split("/"))

    if not _safe_cid(collection_id):
        return jsonify({"error": "invalid_collection_id"}), 422

    adapter = current_app.config.get("_ADAPTER")
    if adapter is None:
        return jsonify({"error": "torrent_engine_unavailable",
                        "detail": "Start the app with libtorrent installed to enable verification"}), 503

    db = getattr(g, "db", None)
    root_info = getattr(g, "archive_root_info", None)
    db_path: Path | None = current_app.config.get("_DB_PATH")

    if db is None or root_info is None or db_path is None:
        return jsonify({"error": "not_configured"}), 503

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
        latest = check_repo.get_latest_completed(root_record.id)
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
# Helpers
# ---------------------------------------------------------------------------


def _is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False
