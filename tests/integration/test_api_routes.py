"""U-009, U-010 — API route validation and abuse rejection."""
from __future__ import annotations

import json

import pytest

from infocon_librarian.web.app import create_app


@pytest.fixture()
def app(tmp_path):
    db_path = tmp_path / "test.db"
    return create_app(secret_key="test-secret", db_path=db_path)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def token(app):
    from infocon_librarian.web.auth import LaunchToken
    tok = LaunchToken.generate()
    app.config["_LAUNCH_TOKEN"] = tok
    return tok.value


@pytest.fixture()
def authed_client(client, token):
    client.get(f"/bootstrap/{token}", follow_redirects=True)
    return client


@pytest.fixture()
def csrf_token(authed_client):
    with authed_client.session_transaction() as sess:
        return sess.get("csrf_token", "")


def _csrf_post(authed_client, csrf_token, path, body=None):
    return authed_client.post(
        path,
        data=json.dumps(body or {}),
        content_type="application/json",
        headers={"X-Csrf-Token": csrf_token, "Origin": "http://localhost"},
    )


# ---------------------------------------------------------------------------
# U-009: Invalid plan/item ID → 404, no state mutation
# ---------------------------------------------------------------------------


def test_u009_unknown_plan_id_returns_404(authed_client):
    r = authed_client.get("/api/plans/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
    assert r.get_json()["error"] == "not_found"


def test_u009_non_uuid_plan_id_returns_404(authed_client):
    r = authed_client.get("/api/plans/not-a-uuid")
    assert r.status_code == 404


def test_u009_unknown_item_pause_returns_404(authed_client, csrf_token):
    path = "/api/items/00000000-0000-0000-0000-000000000000/pause"
    r = _csrf_post(authed_client, csrf_token, path)
    assert r.status_code == 404


def test_u009_non_uuid_item_id_returns_404(authed_client, csrf_token):
    r = _csrf_post(authed_client, csrf_token, "/api/items/bad-id/pause")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Pause/resume state guards — a "pending"/"downloading" item can be paused,
# a "paused" item can be resumed, and nothing else. Regression coverage for
# the bug where Pause was offered on items that weren't running and pausing
# didn't actually stop anything (see torrent/http transfer runners).
# ---------------------------------------------------------------------------


def _make_plan_item(app, status: str) -> str:
    """Insert a plan + single item with the given status directly via the repos."""
    from infocon_librarian.storage.plan_repository import PlanRepository
    from infocon_librarian.storage.repositories import ArchiveRootRepository

    db_path = app.config["_DB_PATH"]
    from infocon_librarian.storage.database import open_db
    from infocon_librarian.storage.migrations import migrate

    conn = open_db(db_path)
    migrate(conn)
    root = ArchiveRootRepository(conn).upsert("/tmp/fake-archive-root", "fp")
    plan = PlanRepository(conn).create_plan(root.id)
    item = PlanRepository(conn).add_item(
        plan.id,
        method="https",
        status=status,
        destination_relpath="defcon/DEF CON 32",
        fallback_reason=None,
        collection_key="defcon/DEF CON 32",
        url="https://infocon.org/defcon/DEF%20CON%2032/",
    )
    conn.close()
    return item.id


def test_pause_rejected_for_complete_item(app, authed_client, csrf_token):
    item_id = _make_plan_item(app, "complete")
    r = _csrf_post(authed_client, csrf_token, f"/api/items/{item_id}/pause")
    assert r.status_code == 409
    assert r.get_json()["error"] == "invalid_state"


def test_pause_allowed_for_downloading_item(app, authed_client, csrf_token):
    item_id = _make_plan_item(app, "downloading")
    r = _csrf_post(authed_client, csrf_token, f"/api/items/{item_id}/pause")
    assert r.status_code == 200
    assert r.get_json()["status"] == "paused"


def test_resume_rejected_for_non_paused_item(app, authed_client, csrf_token):
    item_id = _make_plan_item(app, "pending")
    r = _csrf_post(authed_client, csrf_token, f"/api/items/{item_id}/resume")
    assert r.status_code == 409
    assert r.get_json()["error"] == "invalid_state"


def test_resume_allowed_for_paused_item(app, authed_client, csrf_token):
    item_id = _make_plan_item(app, "paused")
    r = _csrf_post(authed_client, csrf_token, f"/api/items/{item_id}/resume")
    assert r.status_code == 200
    assert r.get_json()["status"] == "pending"


def test_u009_unknown_receipt_returns_404(authed_client):
    r = authed_client.get("/api/receipts/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_u009_start_unknown_plan_returns_404(authed_client, csrf_token):
    path = "/api/plans/00000000-0000-0000-0000-000000000000/start"
    r = _csrf_post(authed_client, csrf_token, path)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# U-010: API abuse — payload with URL/path/unknown fields rejected
# ---------------------------------------------------------------------------


def test_u010_plan_with_url_field_rejected(authed_client, csrf_token):
    r = _csrf_post(authed_client, csrf_token, "/api/plans", {"url": "https://evil.com"})
    assert r.status_code in (403, 422)


def test_u010_plan_with_path_field_rejected(authed_client, csrf_token):
    r = _csrf_post(authed_client, csrf_token, "/api/plans", {"path": "/etc/passwd"})
    assert r.status_code in (403, 422)


def test_u010_plan_with_unknown_field_rejected(authed_client, csrf_token):
    r = _csrf_post(authed_client, csrf_token, "/api/plans", {"xss": "<script>alert(1)</script>"})
    assert r.status_code == 422
    data = r.get_json()
    assert "fields" in data or "error" in data


def test_u010_check_with_unknown_field_rejected(authed_client, csrf_token):
    r = _csrf_post(authed_client, csrf_token, "/api/checks", {"unknown_key": "value"})
    assert r.status_code == 422


def test_plan_with_real_world_collection_name_not_rejected(authed_client, csrf_token):
    # Regression: legitimate directory names with ordinary punctuation (e.g.
    # "T&A Talks", per tests/fixtures/listings/encoded_names.html) must not be
    # rejected as invalid_collection_ids by an overly narrow character allowlist.
    r = _csrf_post(
        authed_client, csrf_token, "/api/plans", {"collection_ids": ["defcon/T&A Talks"]}
    )
    assert r.status_code != 422 or r.get_json().get("error") != "invalid_collection_ids"


def test_plan_with_traversal_collection_id_rejected(authed_client, csrf_token):
    r = _csrf_post(
        authed_client, csrf_token, "/api/plans", {"collection_ids": ["defcon/../../etc"]}
    )
    assert r.status_code == 422
    assert r.get_json()["error"] == "invalid_collection_ids"


def test_u010_check_with_invalid_section_type_rejected(authed_client, csrf_token):
    r = _csrf_post(authed_client, csrf_token, "/api/checks", {"section": 42})
    assert r.status_code == 422


def test_u010_csrf_required_for_mutation(authed_client):
    """POST without CSRF header must be rejected."""
    r = authed_client.post(
        "/api/checks",
        data=json.dumps({}),
        content_type="application/json",
        headers={"Origin": "http://localhost"},
        # No X-Csrf-Token header
    )
    assert r.status_code == 403


def test_u010_origin_required_for_mutation(authed_client, csrf_token):
    """POST without matching Origin must be rejected."""
    r = authed_client.post(
        "/api/checks",
        data=json.dumps({}),
        content_type="application/json",
        headers={"X-Csrf-Token": csrf_token},
        # No Origin header
    )
    assert r.status_code == 403


def test_u010_health_requires_session(client):
    """GET /api/health without session must return 403."""
    r = client.get("/api/health")
    assert r.status_code == 403


def test_u010_csp_header_present(authed_client):
    r = authed_client.get("/api/health")
    csp = r.headers.get("Content-Security-Policy", "")
    assert "default-src" in csp


def test_u010_no_cors_header(authed_client):
    r = authed_client.get("/api/health")
    assert "Access-Control-Allow-Origin" not in r.headers
