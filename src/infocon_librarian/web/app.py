"""Flask application factory."""
from __future__ import annotations

import secrets
import sqlite3
from pathlib import Path
from typing import Any

from flask import Flask, Response, g, send_from_directory

from infocon_librarian.web.api import api_bp
from infocon_librarian.web.auth import add_security_headers, auth_bp


def create_app(
    *,
    db_path: Path | None = None,
    secret_key: str | None = None,
    archive_root_info: Any = None,
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

    app.config["SECRET_KEY"] = secret_key or secrets.token_hex(32)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
    # Secure only when served over HTTPS — loopback is http
    app.config["SESSION_COOKIE_SECURE"] = False

    # Store config for use in request context
    app.config["_DB_PATH"] = db_path
    app.config["_ARCHIVE_ROOT_INFO"] = archive_root_info

    # Register blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp)

    # Minimal web routes
    web_bp_routes(app)

    # Request lifecycle hooks
    @app.before_request
    def _open_db() -> None:  # type: ignore[return]
        path = app.config.get("_DB_PATH")
        if path is not None:
            try:
                from infocon_librarian.storage.database import open_db  # noqa: PLC0415

                g.db = open_db(path)
                g.db_ok = True
            except Exception:
                g.db_ok = False
        else:
            g.db_ok = False
        g.archive_root_info = app.config.get("_ARCHIVE_ROOT_INFO")

    @app.teardown_request
    def _close_db(exc: BaseException | None) -> None:
        db: sqlite3.Connection | None = g.pop("db", None)
        if db is not None:
            db.close()

    @app.after_request
    def _security_headers(response: Response) -> Response:
        return add_security_headers(response)

    return app


def web_bp_routes(app: Flask) -> None:
    """Register minimal page routes on the app."""
    static_dir = Path(__file__).parent / "static"

    @app.route("/")
    def index() -> Response:
        from flask import abort, session  # noqa: PLC0415

        if not session.get("authenticated"):
            abort(403)
        return send_from_directory(str(static_dir), "index.html")

    app.add_url_rule("/", "web.index", index)
