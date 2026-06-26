"""JSON API routes — all IDs are server-issued; no browser input becomes a URL or path."""
from __future__ import annotations

import json
import queue
import threading
import uuid
from typing import Any

from flask import Blueprint, Response, g, jsonify, request, stream_with_context

from infocon_librarian.web.auth import require_csrf, require_session

api_bp = Blueprint("api", __name__, url_prefix="/api")

# Global SSE broker: maps session_key -> queue of event dicts
_sse_lock = threading.Lock()
_sse_queues: dict[str, queue.Queue] = {}  # type: ignore[type-arg]


def _broadcast(event: dict[str, Any]) -> None:
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
    )

    return jsonify({"check_id": check_id, "status": "running", "section": section}), 202


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

    db = getattr(g, "db", None)
    root_info = getattr(g, "archive_root_info", None)

    if db is None or root_info is None:
        return jsonify({"error": "not_configured"}), 503

    from infocon_librarian.storage.plan_repository import PlanRepository
    from infocon_librarian.storage.repositories import ArchiveRootRepository

    root_repo = ArchiveRootRepository(db)
    root_record = root_repo.get_by_path(str(root_info.canonical_path))
    if root_record is None:
        root_record = root_repo.upsert(
            str(root_info.canonical_path), root_info.volume_fingerprint
        )

    plan_repo = PlanRepository(db)
    record = plan_repo.create_plan(root_record.id)

    _broadcast({"type": "plan_created", "plan_id": record.id})
    return jsonify(
        {"plan_id": record.id, "state": record.state, "created_at": record.created_at}
    ), 201


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
            }
            for i in items
        ],
    })


@api_bp.route("/plans/<plan_id>/start", methods=["POST"])
@require_session
@require_csrf
def start_plan(plan_id: str) -> Response:
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

    if record.state not in ("draft", "preflighted"):
        return jsonify({"error": "invalid_state", "current_state": record.state}), 409

    repo.update_plan_state(plan_id, "running")
    _broadcast({"type": "plan_started", "plan_id": plan_id})
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
    if db is None:
        return jsonify({"error": "not_configured"}), 503

    from infocon_librarian.storage.plan_repository import PlanRepository

    repo = PlanRepository(db)
    item = repo.get_item(item_id)
    if item is None:
        return jsonify({"error": "not_found"}), 404

    if item.status != "blocked":
        return jsonify({"error": "not_blocked", "current_status": item.status}), 409

    repo.update_item_status(item_id, "pending")
    _broadcast({"type": "http_fallback_approved", "item_id": item_id})
    return jsonify({"item_id": item_id, "status": "pending"}), 200


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
