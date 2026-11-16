"""Persisted file-size policy and manual verification wiring."""

from __future__ import annotations

import uuid
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from infocon_librarian.domain.models import TorrentFile
from infocon_librarian.services import verify_runner
from infocon_librarian.storage.database import open_db
from infocon_librarian.storage.migrations import migrate
from infocon_librarian.storage.settings_repository import SettingsRepository
from infocon_librarian.web.app import create_app
from infocon_librarian.web.auth import LaunchToken


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=tmp_path / "state.db")
    token = LaunchToken()
    app.config["_LAUNCH_TOKEN"] = token
    client = app.test_client()
    client.get(f"/bootstrap/{token.value}")
    return client


def put(client, value):
    with client.session_transaction() as session:
        csrf = session["csrf_token"]
    return client.put(
        "/api/settings",
        json={"accept_larger_local_files": value},
        headers={"Origin": "http://localhost", "X-Csrf-Token": csrf},
    )


def test_strict_default_and_persisted_opt_in(client):
    assert client.get("/api/settings").json["accept_larger_local_files"] == "0"
    assert put(client, 1).status_code == 200
    assert client.get("/api/settings").json["accept_larger_local_files"] == "1"
    assert put(client, 0).status_code == 200
    assert client.get("/api/settings").json["accept_larger_local_files"] == "0"


@pytest.mark.parametrize("value", [2, -1, 0.5, True, None, "yes", "1.0"])
def test_invalid_policy_values_rejected(client, value):
    assert put(client, value).status_code == 400
    assert client.get("/api/settings").json["accept_larger_local_files"] == "0"


@pytest.mark.parametrize("accept_larger", [False, True])
def test_manual_verify_reads_persisted_policy(tmp_path, monkeypatch, accept_larger):
    db_path = tmp_path / "state.db"
    db = open_db(db_path)
    migrate(db)
    SettingsRepository(db).set("accept_larger_local_files", str(int(accept_larger)))
    db.close()
    file = tmp_path / "archive/defcon/test/talk.mp4"
    file.parent.mkdir(parents=True)
    file.write_bytes(b"x" * 20)
    adapter = SimpleNamespace(
        inspect=lambda _: SimpleNamespace(
            files=(TorrentFile(0, "test/talk.mp4", 10),),
        )
    )
    remote = SimpleNamespace(fetch=lambda _: SimpleNamespace(status_code=200, body=b"torrent"))
    monkeypatch.setattr(verify_runner, "RemoteClient", lambda: nullcontext(remote))
    events = []
    verify_runner.run_verify(
        verify_id=str(uuid.uuid4()),
        collection_key="defcon/test",
        archive_root_id=None,
        db_path=db_path,
        archive_root=tmp_path / "archive",
        torrent_url="https://example.test/a",
        adapter=adapter,
        broadcast=events.append,
    )
    assert events[-1]["level"] == ("has_older_version" if accept_larger else "unverified")


@pytest.mark.parametrize("accept_larger", [False, True])
@pytest.mark.parametrize("cached_legacy", [False, True])
def test_automatic_check_uses_policy_and_rechecks_old_legacy_results(
    tmp_path,
    monkeypatch,
    accept_larger,
    cached_legacy,
):
    import json

    from infocon_librarian.services import check_runner
    from infocon_librarian.storage.repositories import (
        ArchiveRootRepository,
        CheckRepository,
        VerificationRepository,
    )

    root = tmp_path / "archive"
    file = root / "defcon/test/talk.mp4"
    file.parent.mkdir(parents=True)
    file.write_bytes(b"x" * 20)
    db_path = tmp_path / "state.db"
    db = open_db(db_path)
    migrate(db)
    archive = ArchiveRootRepository(db).upsert(str(root.resolve()), "fixture")
    SettingsRepository(db).set("accept_larger_local_files", str(int(accept_larger)))
    if cached_legacy:
        VerificationRepository(db).create(
            str(uuid.uuid4()),
            collection_key="defcon/test",
            archive_root_id=archive.id,
            level="has_older_version",
            torrent_url="https://example.test/defcon/test.torrent",
        )
    check_id = str(uuid.uuid4())
    CheckRepository(db).create(check_id, archive_root_id=archive.id, section="defcon")
    db.close()
    listing = b"""<table>
      <tr><td><a href="test/">test/</a></td></tr>
      <tr><td><a href="test.torrent">test.torrent</a></td></tr>
    </table>"""
    remote = SimpleNamespace(
        fetch=lambda url: SimpleNamespace(
            status_code=200,
            body=b"torrent" if url.endswith(".torrent") else listing,
        )
    )
    monkeypatch.setattr(check_runner, "RemoteClient", lambda: nullcontext(remote))
    adapter = SimpleNamespace(
        inspect=lambda _: SimpleNamespace(
            files=(TorrentFile(0, "test/talk.mp4", 10),),
        )
    )
    check_runner.run_check(
        check_id=check_id,
        db_path=db_path,
        archive_root=root,
        section="defcon",
        broadcast=lambda _: None,
        adapter=adapter,
        base_url="https://example.test/",
    )
    db = open_db(db_path)
    row = db.execute("SELECT state, result_json FROM checks WHERE id=?", (check_id,)).fetchone()
    assert row["state"] == "complete"
    result = json.loads(row["result_json"])[0]
    assert result["status"] == ("has_older_version" if accept_larger else "changed_manifest")
    assert result["verify_result"]["level"] == (
        "has_older_version" if accept_larger else "unverified"
    )
    db.close()
