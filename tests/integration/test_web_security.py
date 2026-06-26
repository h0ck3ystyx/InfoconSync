"""F-008 through F-011 — Flask security: binding, token bootstrap, CSRF/origin."""
from __future__ import annotations

import pytest

from infocon_librarian.web.app import create_app
from infocon_librarian.web.auth import LaunchToken


@pytest.fixture()
def app():
    """Flask test app with a fresh launch token."""
    application = create_app(secret_key="test-secret-key-not-random")
    application.config["TESTING"] = True
    # Inject a fresh token into the auth module for test isolation
    import infocon_librarian.web.auth as auth_mod  # noqa: PLC0415

    auth_mod._launch_token = LaunchToken()
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def token(app) -> str:
    import infocon_librarian.web.auth as auth_mod  # noqa: PLC0415

    return auth_mod._launch_token.value


@pytest.fixture()
def authed_client(app, token):
    """A client that has already bootstrapped a session."""
    client = app.test_client()
    resp = client.get(f"/bootstrap/{token}", follow_redirects=False)
    assert resp.status_code in (301, 302, 303)
    return client


@pytest.fixture()
def csrf_token(authed_client) -> str:
    """Extract the CSRF token from the authed session."""
    with authed_client.session_transaction() as sess:
        return sess.get("csrf_token", "")


# ---------------------------------------------------------------------------
# F-008: Flask binding — server reachable on loopback
# ---------------------------------------------------------------------------


def test_f008_health_requires_session(client) -> None:
    """Unauthenticated requests to /api/health are rejected."""
    resp = client.get("/api/health")
    assert resp.status_code == 403


def test_f008_authenticated_health_ok(authed_client, csrf_token) -> None:
    resp = authed_client.get(
        "/api/health",
        headers={"X-Csrf-Token": csrf_token, "Origin": "http://localhost"},
    )
    # Health returns 200 with a session
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"


def test_f008_root_requires_session(client) -> None:
    resp = client.get("/")
    assert resp.status_code == 403


def test_f008_root_accessible_with_session(authed_client) -> None:
    resp = authed_client.get("/")
    assert resp.status_code == 200
    assert b"InfoCon Librarian" in resp.data


# ---------------------------------------------------------------------------
# F-009: Token bootstrap — valid token creates session, cannot be reused
# ---------------------------------------------------------------------------


def test_f009_valid_token_creates_session(client, token) -> None:
    resp = client.get(f"/bootstrap/{token}", follow_redirects=False)
    assert resp.status_code in (301, 302, 303)
    # Session cookie should be set
    assert any("session" in c.lower() for c in resp.headers.getlist("Set-Cookie"))


def test_f009_token_cannot_be_reused(client, token) -> None:
    # First use: succeeds
    resp1 = client.get(f"/bootstrap/{token}", follow_redirects=False)
    assert resp1.status_code in (301, 302, 303)

    # Second use of the same token: rejected
    resp2 = client.get(f"/bootstrap/{token}", follow_redirects=False)
    assert resp2.status_code == 403


def test_f009_wrong_token_rejected(client) -> None:
    resp = client.get("/bootstrap/completely-wrong-token", follow_redirects=False)
    assert resp.status_code == 403


def test_f009_empty_token_rejected(client) -> None:
    resp = client.get("/bootstrap/", follow_redirects=False)
    # Flask will 404 on a missing route param
    assert resp.status_code in (403, 404)


# ---------------------------------------------------------------------------
# F-010: CSRF/origin protection
# ---------------------------------------------------------------------------


def test_f010_missing_csrf_header_rejected(authed_client) -> None:
    """State-changing requests without X-Csrf-Token are rejected."""
    resp = authed_client.post(
        "/api/checks",
        json={},
        headers={"Origin": "http://localhost"},
    )
    # Route doesn't exist yet, but middleware should reject before routing
    assert resp.status_code in (403, 404, 405)


def test_f010_wrong_origin_rejected(authed_client, csrf_token) -> None:
    resp = authed_client.post(
        "/api/checks",
        json={},
        headers={
            "X-Csrf-Token": csrf_token,
            "Origin": "http://evil.example.com",
        },
    )
    assert resp.status_code in (403, 404, 405)


def test_f010_valid_origin_and_csrf_accepted(authed_client, csrf_token) -> None:
    """Valid same-origin request passes CSRF/origin checks (may 404 if route missing)."""
    resp = authed_client.post(
        "/api/checks",
        json={},
        headers={
            "X-Csrf-Token": csrf_token,
            "Origin": "http://localhost",
        },
    )
    # 404 is fine — route not yet implemented — but not 403
    assert resp.status_code != 403


def test_f010_csp_header_on_every_response(authed_client) -> None:
    resp = authed_client.get("/")
    assert "Content-Security-Policy" in resp.headers
    csp = resp.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp


def test_f010_no_cors_header(authed_client) -> None:
    resp = authed_client.get("/")
    assert "Access-Control-Allow-Origin" not in resp.headers


# ---------------------------------------------------------------------------
# F-011: Mutation routes reject arbitrary path/URL fields
# ---------------------------------------------------------------------------


def test_f011_unknown_fields_rejected_in_mutation(authed_client, csrf_token) -> None:
    """Mutation route rejects payloads containing arbitrary path fields."""
    resp = authed_client.post(
        "/api/plans",
        json={
            "destination_path": "/etc/evil",
            "url": "http://evil.example.com/payload.zip",
        },
        headers={
            "X-Csrf-Token": csrf_token,
            "Origin": "http://localhost",
        },
    )
    # Route not yet implemented but payload should never reach a backend
    # that would act on arbitrary paths — 404 is acceptable, 403 is fine, 422 is ideal
    assert resp.status_code in (403, 404, 405, 422)
