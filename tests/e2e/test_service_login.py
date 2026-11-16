"""Real-browser password login and logout in LAN service mode."""

from __future__ import annotations

import threading

from playwright.sync_api import Page, expect
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server

from infocon_librarian.web.app import create_app


def test_service_password_login_and_sign_out(page: Page):
    password = "mirror browser test password"
    app = create_app(secret_key="browser-test-secret")
    app.config.update(
        SERVICE_AUTH={"password_hash": generate_password_hash(password)},
        TRUSTED_HOSTS=["127.0.0.1"],
    )
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        page.goto(base)
        expect(page).to_have_url(base + "/login")
        page.get_by_label("Password").fill("incorrect")
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("alert")).to_have_text("Incorrect password")
        page.get_by_label("Password").fill(password)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page).to_have_url(base + "/")
        expect(page.get_by_role("tab", name="Collections", exact=True)).to_be_visible()
        page.get_by_role("button", name="Sign out", exact=True).click()
        expect(page).to_have_url(base + "/login")
        assert page.request.get(base + "/api/health").status == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
