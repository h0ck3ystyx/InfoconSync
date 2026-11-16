"""Flask application factory."""
from __future__ import annotations

import secrets
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

from flask import Flask, g, jsonify, request, send_from_directory, session, url_for
from werkzeug.wrappers import Response

from infocon_librarian.web.api import api_bp
from infocon_librarian.web.auth import add_security_headers, auth_bp


def create_app(
    *,
    db_path: Path | None = None,
    secret_key: str | None = None,
    archive_root_info: Any = None,
    adapter: Any = None,
) -> Flask:
    """Create and configure the Flask application.

    Args:
        db_path: Path to the SQLite database. If None, an in-memory DB is used
            (useful for tests).
        secret_key: Flask session secret key. If None, a random key is generated
            per launch (sessions do not survive restarts, which is correct).
        archive_root_info: Optional ArchiveRootInfo to expose via g.archive_root_info.
    """
    app = Flask(__name__, static_folder=None)

    mutation_lock = threading.Lock()
    app.permanent_session_lifetime = timedelta(hours=12)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

    app.config["SECRET_KEY"] = secret_key or secrets.token_hex(32)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
    # Secure only when served over HTTPS — loopback is http
    app.config["SESSION_COOKIE_SECURE"] = False

    # Store config for use in request context
    app.config["_DB_PATH"] = db_path
    app.config["_ARCHIVE_ROOT_INFO"] = archive_root_info
    app.config["_ADAPTER"] = adapter  # TorrentAdapter or None

    # Register blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp)

    # Minimal web routes
    web_bp_routes(app)

    from infocon_librarian.service_runtime import ServiceInterrupted

    @app.errorhandler(ServiceInterrupted)
    def _interrupted(exc: ServiceInterrupted) -> tuple[Response, int]:
        return jsonify({"error": "service_interrupted", "detail": str(exc)}), 503

    # Request lifecycle hooks
    @app.before_request
    def _service_guard() -> Response | tuple[Response, int] | None:
        runtime = app.config.get("SERVICE_RUNTIME")
        if (runtime and session.get("authenticated")
                and request.path.startswith("/api/") and request.method != "GET"):
            from infocon_librarian.service_runtime import ServiceInterrupted

            mutation_lock.acquire()
            g.service_mutation_lock = True
            try:
                runtime.checkpoint(force=True)
            except ServiceInterrupted:
                return jsonify({"error": "archive_unavailable_or_service_stopping",
                                "detail": runtime.reason}), 503
        return None

    @app.before_request
    def _open_db() -> None:
        path = app.config.get("_DB_PATH")
        if path is not None:
            try:
                from infocon_librarian.storage.database import open_db  # noqa: PLC0415
                from infocon_librarian.storage.migrations import migrate  # noqa: PLC0415

                conn = open_db(path)
                migrate(conn)
                g.db = conn
                g.db_ok = True
            except Exception:
                g.db_ok = False
        else:
            g.db_ok = False
        g.archive_root_info = app.config.get("_ARCHIVE_ROOT_INFO")

    @app.teardown_request
    def _close_db(exc: BaseException | None) -> None:
        if g.pop("service_mutation_lock", False):
            mutation_lock.release()
        db: sqlite3.Connection | None = g.pop("db", None)
        if db is not None:
            db.close()

    @app.after_request
    def _security_headers(response: Response) -> Response:
        if app.config.get("SERVICE_AUTH"):
            response.headers["Cache-Control"] = "no-store"
            # Refresh both cookies together for the sliding session lifetime.
            if session.get("authenticated") and session.get("csrf_token"):
                response.set_cookie(
                    "csrftoken", session["csrf_token"], httponly=False,
                    samesite="Strict", max_age=12 * 3600,
                    secure=app.config["SESSION_COOKIE_SECURE"],
                )
        return add_security_headers(response)

    return app


def web_bp_routes(app: Flask) -> None:
    """Register minimal page routes on the app."""
    static_dir = Path(__file__).parent / "static"

    def index() -> Response:
        from flask import abort, session  # noqa: PLC0415

        if not session.get("authenticated"):
            if app.config.get("SERVICE_AUTH"):
                return app.redirect(url_for("auth.login"))
            abort(403)
        return send_from_directory(str(static_dir), "index.html")

    def static_file(filename: str) -> Response:
        return send_from_directory(str(static_dir), filename)

    app.add_url_rule("/", "web.index", index)
    app.add_url_rule("/static/<path:filename>", "web.static", static_file)
