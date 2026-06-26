"""JSON API routes."""
from __future__ import annotations

import shutil

from flask import Blueprint, g, jsonify

from infocon_librarian.web.auth import require_session

api_bp = Blueprint("api", __name__, url_prefix="/api")


@api_bp.route("/health")
@require_session
def health():  # type: ignore[no-untyped-def]
    """Return engine, database, and archive root health."""
    root_info = getattr(g, "archive_root_info", None)
    db_ok = getattr(g, "db_ok", False)

    payload: dict = {
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
