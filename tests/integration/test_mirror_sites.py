"""Mirror site configuration, check isolation, and transfer tests using offline indexes."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path

import httpx
import pytest

from infocon_librarian.archive.root import validate_root
from infocon_librarian.remote import mirror
from infocon_librarian.services import http_transfer_runner, mirror_check
from infocon_librarian.storage.database import open_db
from infocon_librarian.web import api
from infocon_librarian.web.app import create_app
from infocon_librarian.web.auth import LaunchToken


def listing(*links: str) -> str:
    rows = "".join(f'<tr><td><a href="{link}">{link}</a></td></tr>' for link in links)
    return f'<table id="list">{rows}</table>'


@pytest.fixture
def app_session(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    root.mkdir()
    db_path = tmp_path / "state" / "state.db"
    app = create_app(db_path=db_path, archive_root_info=validate_root(root))
    token = LaunchToken.generate()
    app.config["_LAUNCH_TOKEN"] = token
    client = app.test_client()
    client.get(f"/bootstrap/{token.value}")
    with client.session_transaction() as session:
        headers = {"X-Csrf-Token": session["csrf_token"], "Origin": "http://localhost"}
    monkeypatch.setattr(mirror_check, "start_mirror_check", mirror_check.run_mirror_check)
    monkeypatch.setattr(
        http_transfer_runner, "start_http_transfer_thread", http_transfer_runner.run_http_transfer,
    )
    return client, headers, root, db_path


def test_defcon_check_plan_and_sync(app_session, monkeypatch):
    client, headers, root, db_path = app_session
    pages = {
        "/": listing("DEF%20CON%2032/", "Read%20me.txt", "unavailable/", "../", "?C=N"),
        "/DEF CON 32/": listing("Media/", "Empty/"),
        "/DEF CON 32/Media/": listing("Talk%20%26%20Demo.mp4"),
        "/DEF CON 32/Empty/": listing("../"),
    }
    files = {"/DEF CON 32/Media/Talk & Demo.mp4": b"video", "/Read me.txt": b"intro"}
    media_gets = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "media.defcon.org"
        path = request.url.path
        if path in pages:
            return httpx.Response(200, text=pages[path])
        if path in files:
            if request.method == "HEAD":
                return httpx.Response(200, headers={"Content-Length": str(len(files[path]))})
            media_gets.append(path)
            return httpx.Response(200, stream=httpx.ByteStream(files[path]))
        return httpx.Response(403)

    def factory(_base: str) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handle))

    monkeypatch.setattr(mirror_check, "mirror_client", factory)
    monkeypatch.setattr(http_transfer_runner, "mirror_client", factory)
    assert [s["id"] for s in client.get("/api/mirror-sites").json] == ["infocon", "defcon"]

    response = client.post("/api/checks", json={"site_id": "defcon"}, headers=headers)
    assert response.status_code == 202, response.json
    check = client.get("/api/checks/latest?site_id=defcon").json
    assert client.get("/api/checks/latest?site_id=infocon").status_code == 404
    assert media_gets == []
    assert not (root / "DefconMedia").exists()

    rows = {row["key"]: row for row in check["results"]}
    assert rows["unavailable"]["status"] == "unknown"
    assert rows["unavailable"]["selectable"] is False
    assert "403" in rows["unavailable"]["error"]
    assert rows["Read me.txt"]["status"] == "new"
    assert "_mirror" not in rows["Read me.txt"]

    options = {"site_id": "defcon", "check_id": check["check_id"]}
    response = client.post("/api/plans", headers=headers, json={
        **options, "collection_ids": [rows["unavailable"]["id"]],
    })
    assert response.status_code == 422
    response = client.post("/api/plans", headers=headers, json={
        **options, "site_id": "infocon", "collection_ids": ["defcon/DEF CON 32"],
    })
    assert response.status_code == 409

    response = client.post("/api/plans", headers=headers, json={
        **options, "collection_ids": [rows[key]["id"] for key in ("Read me.txt", "DEF CON 32")],
    })
    assert response.status_code == 201, response.json
    plan = response.json
    assert plan["site_id"] == "defcon"
    assert {item["destination_relpath"] for item in plan["items"]} == {
        "DefconMedia/Read me.txt", "DefconMedia/DEF CON 32",
    }
    dest = root / "DefconMedia"
    dest.mkdir()
    (dest / "Keep locally.txt").write_bytes(b"local")
    assert client.get("/api/checks/latest?site_id=infocon").status_code == 404
    assert client.post(f"/api/plans/{plan['plan_id']}/start", headers=headers).status_code == 200
    state = client.get(f"/api/plans/{plan['plan_id']}").json
    assert state["state"] == "complete"
    assert (dest / "Read me.txt").read_bytes() == b"intro"
    assert (dest / "DEF CON 32/Media/Talk & Demo.mp4").read_bytes() == b"video"
    assert (dest / "DEF CON 32/Empty").is_dir()
    assert (dest / "Keep locally.txt").read_bytes() == b"local"
    assert sorted(media_gets) == sorted(files)

    response = client.post("/api/checks", json={"site_id": "defcon"}, headers=headers)
    assert response.status_code == 202
    results = client.get("/api/checks/latest?site_id=defcon").json["results"]
    assert {row["key"]: row["status"] for row in results} == {
        "DEF CON 32": "present_unverified", "Read me.txt": "present_unverified",
        "unavailable": "unknown", "Keep locally.txt": "local_only",
    }
    with contextlib.closing(open_db(db_path)) as conn:
        file = conn.execute("SELECT json_path FROM receipts WHERE plan_id=?", (plan["plan_id"],))
        assert json.loads(Path(file.fetchone()[0]).read_text())["site_id"] == "defcon"


def test_add_site_validates_index_and_keeps_config_on_cache_reset(app_session, monkeypatch):
    client, headers, root, _ = app_session

    def factory(_base):
        def handler(request):
            if request.url.host == "plain.example":
                return httpx.Response(200, text="<html><a href='file'>Ordinary webpage</a></html>")
            return httpx.Response(
                200, text='<h1>Index of /art/</h1><pre><a href="hello.txt">Hello</a></pre>',
            )

        return httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(api, "mirror_client", factory)
    site = {"name": "Artwork", "base_url": "https://files.example/art", "folder": "Artwork"}
    endpoint = "/api/mirror-sites"
    assert client.post(endpoint, json=site).status_code == 403
    assert client.post(
        endpoint, json={**site, "folder": "../Artwork"}, headers=headers,
    ).status_code == 422
    (root / "InfoCon Section").mkdir()
    assert client.post(
        endpoint, json={**site, "folder": "infocon section"}, headers=headers,
    ).status_code == 422
    assert client.post(
        endpoint, json={**site, "base_url": "https://plain.example/"}, headers=headers,
    ).status_code == 422
    response = client.post(endpoint, json=site, headers=headers)
    assert response.status_code == 201, response.json
    assert response.json["base_url"] == "https://files.example/art/"
    assert client.post(endpoint, json=site, headers=headers).status_code == 422
    assert client.post("/api/admin/reset", headers=headers).status_code == 200
    assert client.get(endpoint).json[-1] == response.json


def test_mirror_transport_rejects_private_resolution_and_off_site_requests(monkeypatch):
    monkeypatch.setattr(
        mirror.socket, "getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(httpx.ConnectError, match="no public address"):
        mirror.mirror_client("https://files.example/")
    monkeypatch.setattr(mirror, "_public_addresses", lambda _: ["1.1.1.1"])
    with (
        mirror.mirror_client("https://files.example/pub/") as client,
        pytest.raises(httpx.RequestError, match="outside the mirror"),
    ):
        client.get("https://files.example/private/")


@pytest.mark.parametrize("torrent_size, method", [(4, "torrent"), (5, "https")])
def test_defcon_uses_torrent_only_if_it_matches_the_remote_files(
    app_session, monkeypatch, torrent_size, method,
):
    from infocon_librarian.domain.models import TorrentFile, TorrentManifest, TorrentProtocol
    from tests.support.fake_engine import FakeTorrentEngine

    client, headers, _, _ = app_session
    engine = FakeTorrentEngine()
    engine.configure_manifest(TorrentManifest(
        url="", protocol=TorrentProtocol.V1, v1_infohash="test", v2_infohash=None,
        files=(TorrentFile(0, "DEF CON 32/Talk.mp4", torrent_size),),
        trackers=(), total_size=torrent_size, name="DEF CON 32",
    ))
    client.application.config["_ADAPTER"] = engine

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/":
            return httpx.Response(200, text=listing("DEF%20CON%2032/", "DEF%20CON%20Torrents/"))
        if path == "/DEF CON 32/":
            return httpx.Response(200, text=listing("Talk.mp4"))
        if path == "/DEF CON Torrents/":
            return httpx.Response(200, text=listing("DEF%20CON%2032%20v1.torrent"))
        if path.endswith(".torrent"):
            return httpx.Response(200, content=b"synthetic metainfo")
        assert request.method == "HEAD"
        return httpx.Response(200, headers={"content-length": "4"})

    monkeypatch.setattr(
        mirror_check, "mirror_client",
        lambda _: httpx.Client(transport=httpx.MockTransport(handle)),
    )
    response = client.post("/api/checks", json={"site_id": "defcon"}, headers=headers)
    assert response.status_code == 202
    check = client.get("/api/checks/latest?site_id=defcon").json
    item = next(row for row in check["results"] if row["key"] == "DEF CON 32")
    plan = client.post("/api/plans", headers=headers, json={
        "site_id": "defcon", "check_id": check["check_id"], "collection_ids": [item["id"]],
    })
    assert plan.status_code == 201, plan.json
    assert plan.json["items"][0]["method"] == method


def test_edit_site_destination_requires_a_fresh_check_and_plan(app_session, monkeypatch):
    client, headers, root, _ = app_session
    old_mirror = root / "DefconMedia"
    old_mirror.mkdir()
    (old_mirror / "keep.txt").write_bytes(b"old")
    target = root.parent / "another drive"
    target.mkdir()
    endpoint = "/api/mirror-sites/defcon"

    def factory(_base):
        def handle(request):
            if request.url.path == "/":
                return httpx.Response(200, text=listing("notes.txt"))
            return httpx.Response(200, stream=httpx.ByteStream(b"mirror"))

        return httpx.Client(transport=httpx.MockTransport(handle))

    monkeypatch.setattr(mirror_check, "mirror_client", factory)
    monkeypatch.setattr(http_transfer_runner, "mirror_client", factory)

    def draft():
        checked = client.post("/api/checks", headers=headers, json={"site_id": "defcon"})
        assert checked.status_code == 202
        check = client.get("/api/checks/latest?site_id=defcon").json
        item = next(row for row in check["results"] if row["key"] == "notes.txt")
        return {
            "site_id": "defcon", "check_id": check["check_id"], "collection_ids": [item["id"]],
        }

    before = draft()
    old_plan = client.post("/api/plans", headers=headers, json=before).json
    assert old_plan["items"][0]["destination_relpath"] == "DefconMedia/notes.txt"

    assert client.put(endpoint, json={"local_path": str(target)}).status_code == 403
    assert client.put(endpoint, json={"local_path": str(root)}, headers=headers).status_code == 409
    changed = client.put(endpoint, json={"local_path": str(target)}, headers=headers)
    assert changed.status_code == 200, changed.json
    assert changed.json["destination"] == str(target.resolve())
    assert changed.json["requires_check"]
    assert client.get("/api/checks/latest?site_id=defcon").status_code == 404
    assert client.post("/api/plans", headers=headers, json=before).status_code == 409
    old_url = f"/api/plans/{old_plan['plan_id']}"
    assert client.get(old_url).json["destination_changed"]
    assert client.post(old_url + "/start", headers=headers).status_code == 409

    new = client.post("/api/plans", headers=headers, json=draft()).json
    assert new["archive_root"] == str(target.resolve())
    assert new["items"][0]["destination_relpath"] == "notes.txt"
    assert client.post(f"/api/plans/{new['plan_id']}/start", headers=headers).status_code == 200
    assert (target / "notes.txt").read_bytes() == b"mirror"
    assert (old_mirror / "keep.txt").read_bytes() == b"old"
    assert not (old_mirror / "notes.txt").exists()

    response = client.put(endpoint, json={"local_path": ""}, headers=headers)
    assert response.status_code == 200, response.json
    assert response.json["destination"] == str(old_mirror.resolve())
    assert client.get("/api/checks/latest?site_id=defcon").status_code == 404
    assert client.get(old_url).json["destination_changed"]


def test_file_preview_reports_pending_changes_without_transferring(app_session, monkeypatch):
    from infocon_librarian.services import plan_preview

    client, headers, root, db_path = app_session
    mirror_root = root / "DefconMedia"
    mirror_root.mkdir()
    (mirror_root / "keep.txt").write_bytes(b"same")
    (mirror_root / "replace.txt").write_bytes(b"old")
    (mirror_root / "conflict.txt").mkdir()
    (mirror_root / "local-only.txt").write_bytes(b"local")
    pages = {
        "/": listing("keep.txt", "replace.txt", "conflict.txt", "new.txt", "empty/", "tracks/"),
        "/tracks/": listing(*(f"track-{i:02d}.txt" for i in range(30))),
        "/empty/": listing(),
    }
    requests = []

    def remote(request):
        path = request.url.path
        if request.method == "GET" and path in pages:
            return httpx.Response(200, text=pages[path])
        requests.append((request.method, path))
        assert request.method == "HEAD"
        return httpx.Response(200, headers={"Content-Length": "4"})

    def factory(_base):
        return httpx.Client(transport=httpx.MockTransport(remote))

    monkeypatch.setattr(mirror_check, "mirror_client", factory)
    monkeypatch.setattr(plan_preview, "mirror_client", factory)
    check_response = client.post("/api/checks", headers=headers, json={"site_id": "defcon"})
    assert check_response.status_code == 202
    check = client.get("/api/checks/latest?site_id=defcon").json
    plan = client.post("/api/plans", headers=headers, json={
        "site_id": "defcon", "check_id": check["check_id"],
        "collection_ids": [row["id"] for row in check["results"] if row.get("selectable")],
    }).json
    requests.clear()

    actions = {}
    for item in plan["items"]:
        preview = f"/api/plans/{plan['plan_id']}/items/{item['item_id']}/preview"
        data = client.get(preview).json
        if item["destination_relpath"].endswith("/tracks"):
            assert data["total"] == 30
            assert len(data["rows"]) == 25
            assert len(client.get(preview + "?offset=25").json["rows"]) == 5
            matched = client.get(preview + "?q=track-29").json
            assert [row["path"] for row in matched["rows"]] == ["DefconMedia/tracks/track-29.txt"]
            continue
        assert data["total"] == 1
        row = data["rows"][0]
        actions[row["path"]] = row["action"]
        assert client.get(preview + "?q=no-such-file").json["total"] == 0
        assert client.get(preview + "?offset=25").json["rows"] == []

    for name, action in {
        "keep.txt": "keep", "replace.txt": "replace", "new.txt": "add",
        "conflict.txt": "conflict", "empty": "add",
    }.items():
        assert actions[f"DefconMedia/{name}"] == action
    assert "DefconMedia/local-only.txt" not in actions
    assert set(requests) == {("HEAD", "/keep.txt"), ("HEAD", "/replace.txt")}
    assert (mirror_root / "replace.txt").read_bytes() == b"old"
    assert not (mirror_root / "new.txt").exists()
    with contextlib.closing(open_db(db_path)) as db:
        assert db.execute("SELECT state FROM plans").fetchone()["state"] == "draft"
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0


def test_infocon_check_discovers_sections_on_empty_archive(app_session, monkeypatch):
    from types import SimpleNamespace

    from infocon_librarian.services import check_runner

    client, headers, root, _ = app_session
    requested = []

    def fetch(url):
        requested.append(url)
        if url == "https://infocon.org/":
            return SimpleNamespace(
                status_code=200,
                body=listing("cons/", "word%20lists/", "unavailable/", "DefconMedia/").encode(),
            )
        if url.endswith("/cons/"):
            return SimpleNamespace(
                status_code=200,
                body=b'<table><tr><td><a href="A%20Conference/">A Conference/</a>'
                     b'</td></tr></table>',
            )
        if url.endswith("/word%20lists/"):
            return SimpleNamespace(status_code=200, body=listing("Example/").encode())
        if url.endswith("/unavailable/"):
            return SimpleNamespace(status_code=403, body=b"")
        raise AssertionError(f"Unexpected URL {url}")

    monkeypatch.setattr(
        check_runner, "RemoteClient", lambda: contextlib.nullcontext(SimpleNamespace(fetch=fetch)),
    )
    monkeypatch.setattr(check_runner, "start_check_thread", check_runner.run_check)

    assert not list(root.iterdir())
    response = client.post("/api/checks", json={"site_id": "infocon"}, headers=headers)
    assert response.status_code == 202
    data = client.get("/api/checks/latest?site_id=infocon").json
    assert data["state"] == "complete"
    assert {(row["section"], row["key"], row["status"]) for row in data["results"]} == {
        ("cons", "A Conference", "new"),
        ("word lists", "Example", "new"),
        ("unavailable", "unavailable", "unknown"),
    }
    failure = next(row for row in data["results"] if row["section"] == "unavailable")
    assert failure["selectable"] is False
    assert "HTTP 403" in failure["error"]
    assert requested == [
        "https://infocon.org/", "https://infocon.org/cons/", "https://infocon.org/unavailable/",
        "https://infocon.org/word%20lists/",
    ]
    assert not list(root.iterdir())
