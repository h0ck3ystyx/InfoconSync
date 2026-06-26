"""Per-launch token bootstrap and CSRF/Origin middleware."""
from __future__ import annotations

import secrets
from collections.abc import Callable
from functools import wraps

from flask import (
    Blueprint,
    Response,
    abort,
    redirect,
    request,
    session,
    url_for,
)

_SESSION_KEY = "authenticated"
_CSRF_SESSION_KEY = "csrf_token"
_CSRF_HEADER = "X-Csrf-Token"

auth_bp = Blueprint("auth", __name__)


class LaunchToken:
    """Holds the single-use per-launch capability token."""

    def __init__(self) -> None:
        self._token: str | None = secrets.token_urlsafe(32)

    @property
    def value(self) -> str | None:
        return self._token

    def consume(self, candidate: str) -> bool:
        """Validate and invalidate the token. Returns True on success."""
        if self._token is not None and secrets.compare_digest(candidate, self._token):
            self._token = None
            return True
        return False

    @property
    def url_path(self) -> str:
        return f"/bootstrap/{self._token}"


# Module-level singleton — reset on each process launch
_launch_token = LaunchToken()


def get_launch_token() -> LaunchToken:
    return _launch_token


@auth_bp.route("/bootstrap/<token>")
def bootstrap(token: str) -> Response:
    """One-time bootstrap endpoint that exchanges the launch token for a session."""
    if not _launch_token.consume(token):
        abort(403)

    csrf = secrets.token_urlsafe(32)
    session.clear()
    session[_SESSION_KEY] = True
    session[_CSRF_SESSION_KEY] = csrf
    session.permanent = False

    response = redirect(url_for("web.index"))
    return response


def require_session(f: Callable) -> Callable:
    """Decorator: require an authenticated session cookie."""

    @wraps(f)
    def decorated(*args, **kwargs):  # type: ignore[no-untyped-def]
        if not session.get(_SESSION_KEY):
            abort(403)
        return f(*args, **kwargs)

    return decorated


def require_csrf(f: Callable) -> Callable:
    """Decorator: require valid Origin and CSRF token for state-changing requests."""

    @wraps(f)
    def decorated(*args, **kwargs):  # type: ignore[no-untyped-def]
        if not session.get(_SESSION_KEY):
            abort(403)

        # Origin check — must match the loopback host we're bound to
        origin = request.headers.get("Origin", "")
        host = request.headers.get("Host", "")
        expected_origin = f"http://{host}"
        if origin and origin != expected_origin:
            abort(403)

        # CSRF token check
        expected_csrf = session.get(_CSRF_SESSION_KEY)
        provided_csrf = request.headers.get(_CSRF_HEADER, "")
        if not expected_csrf or not secrets.compare_digest(provided_csrf, expected_csrf):
            abort(403)

        return f(*args, **kwargs)

    return decorated


def add_security_headers(response: Response) -> Response:
    """Attach restrictive security headers to every response."""
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "frame-ancestors 'none';"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    # No permissive CORS
    response.headers.pop("Access-Control-Allow-Origin", None)
    return response
