"""Per-launch token bootstrap and CSRF/Origin middleware."""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from flask import (
    Blueprint,
    abort,
    current_app,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.wrappers import Response

P = ParamSpec("P")
R = TypeVar("R")

_SESSION_KEY = "authenticated"
_CSRF_SESSION_KEY = "csrf_token"
_CSRF_HEADER = "X-Csrf-Token"

auth_bp = Blueprint("auth", __name__)


class LaunchToken:
    """Holds the single-use per-launch capability token."""

    def __init__(self) -> None:
        self._token: str | None = secrets.token_urlsafe(32)

    @classmethod
    def generate(cls) -> LaunchToken:
        return cls()

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
    from flask import current_app  # noqa: PLC0415

    if current_app.config.get("SERVICE_AUTH"):
        abort(404)
    app_token: LaunchToken | None = current_app.config.get("_LAUNCH_TOKEN")
    tok = app_token if app_token is not None else _launch_token
    if not tok.consume(token):
        abort(403)

    csrf = secrets.token_urlsafe(32)
    session.clear()
    session[_SESSION_KEY] = True
    session[_CSRF_SESSION_KEY] = csrf
    session.permanent = False

    response = current_app.redirect(url_for("web.index"))
    # Set a JS-readable cookie so the frontend can include the CSRF token in
    # request headers (double-submit cookie pattern). HttpOnly=False is
    # intentional — JS must be able to read it.
    response.set_cookie(
        "csrftoken",
        csrf,
        httponly=False,
        samesite="Strict",
        secure=False,  # loopback is plain HTTP
    )
    return response


def require_session(f: Callable[P, R]) -> Callable[P, R]:
    """Decorator: require an authenticated session cookie."""

    @wraps(f)
    def decorated(*args: P.args, **kwargs: P.kwargs) -> R:
        if not session.get(_SESSION_KEY):
            abort(401 if current_app.config.get("SERVICE_AUTH") else 403)
        return f(*args, **kwargs)

    return decorated


def require_csrf(f: Callable[P, R]) -> Callable[P, R]:
    """Decorator: require valid Origin and CSRF token for state-changing requests."""

    @wraps(f)
    def decorated(*args: P.args, **kwargs: P.kwargs) -> R:
        if not session.get(_SESSION_KEY):
            abort(401 if current_app.config.get("SERVICE_AUTH") else 403)

        # Origin check — must be present and match the loopback host
        origin = request.headers.get("Origin", "")
        host = request.headers.get("Host", "")
        expected_origin = f"{request.scheme}://{host}"
        if not origin or origin != expected_origin:
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
    # Form POSTs need their same-origin Origin header for login CSRF checks.
    response.headers["Referrer-Policy"] = (
        "same-origin" if current_app.config.get("SERVICE_AUTH") else "no-referrer"
    )
    # No permissive CORS
    response.headers.pop("Access-Control-Allow-Origin", None)
    return response


class LoginLimiter:
    """Bounded, process-wide attempt budget; forwarded client headers are ignored."""

    def __init__(self) -> None:
        self._attempts: deque[tuple[float, str]] = deque(maxlen=100)
        self._lock = threading.Lock()

    def allow(self, address: str) -> bool:
        now = time.monotonic()
        with self._lock:
            while self._attempts and self._attempts[0][0] < now - 60:
                self._attempts.popleft()
            if len(self._attempts) >= 100:
                return False
            count = sum(ip == address for _, ip in self._attempts)
            if count >= 5:
                return False
            self._attempts.append((now, address))
            return True


@auth_bp.route("/login", methods=["GET", "POST"])
def login() -> Response | str:
    auth = current_app.config.get("SERVICE_AUTH")
    if not auth:
        abort(404)
    error = None
    if request.method == "POST":
        origin = request.headers.get("Origin", "")
        if origin != request.host_url.rstrip("/"):
            abort(403)
        expected = session.get("login_csrf", "")
        provided = request.form.get("csrf_token", "")
        if not expected or not secrets.compare_digest(expected, provided):
            abort(403)
        limiter = current_app.extensions.setdefault("login_limiter", LoginLimiter())
        if not limiter.allow(request.remote_addr or "unknown"):
            return Response(
                "Too many login attempts. Try again in a minute.",
                status=429,
                headers={"Retry-After": "60"},
            )
        from werkzeug.security import check_password_hash

        if check_password_hash(auth["password_hash"], request.form.get("password", "")):
            session.clear()
            session[_SESSION_KEY] = True
            session[_CSRF_SESSION_KEY] = secrets.token_urlsafe(32)
            session.permanent = True
            response = current_app.redirect(url_for("web.index"))
            response.set_cookie(
                "csrftoken",
                session[_CSRF_SESSION_KEY],
                httponly=False,
                samesite="Strict",
                max_age=12 * 3600,
                secure=current_app.config["SESSION_COOKIE_SECURE"],
            )
            return response
        error = "Incorrect password"
    session.setdefault("login_csrf", secrets.token_urlsafe(32))
    html = render_template("login.html", csrf_token=session["login_csrf"], error=error)
    return Response(html, status=401 if error else 200, mimetype="text/html")


@auth_bp.route("/logout", methods=["POST"])
@require_csrf
def logout() -> Response:
    session.clear()
    response = current_app.redirect(url_for("auth.login"))
    response.delete_cookie("csrftoken")
    return response
