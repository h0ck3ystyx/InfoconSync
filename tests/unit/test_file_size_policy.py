"""Strict size matching is the default; legacy larger encodings are opt-in."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from infocon_librarian.domain.models import TorrentFile
from infocon_librarian.services.check_runner import _stored_result
from infocon_librarian.services.verify_runner import manifest_check
from infocon_librarian.storage.database import open_db
from infocon_librarian.storage.migrations import migrate
from infocon_librarian.storage.settings_repository import SettingsRepository
from infocon_librarian.web.api import _apply_file_size_policy


@pytest.mark.parametrize("accept_larger", [False, True])
@pytest.mark.parametrize("local_size", [None, 0, 5, 10, 15])
def test_manifest_size_policy(tmp_path, local_size, accept_larger):
    file = tmp_path / "defcon/test/talk.mp4"
    if local_size is not None:
        file.parent.mkdir(parents=True)
        file.write_bytes(b"x" * local_size)
    adapter = SimpleNamespace(
        inspect=lambda _: SimpleNamespace(
            files=(TorrentFile(0, "test/talk.mp4", 10),),
        )
    )
    level, _, details = manifest_check(
        b"torrent",
        section="defcon",
        archive_root=tmp_path,
        adapter=adapter,
        accept_larger_local_files=accept_larger,
    )
    expected = (
        "manifest_verified"
        if local_size == 10
        else "has_older_version"
        if local_size == 15 and accept_larger
        else "unverified"
    )
    assert level == expected
    assert details["size_larger_total"] == int(local_size == 15)


def test_missing_file_is_not_accepted_even_with_larger_file(tmp_path):
    file = tmp_path / "defcon/test/large.mp4"
    file.parent.mkdir(parents=True)
    file.write_bytes(b"x" * 20)
    adapter = SimpleNamespace(
        inspect=lambda _: SimpleNamespace(
            files=(
                TorrentFile(0, "test/large.mp4", 10),
                TorrentFile(1, "test/missing.mp4", 10),
            )
        )
    )
    assert (
        manifest_check(
            b"torrent",
            section="defcon",
            archive_root=tmp_path,
            adapter=adapter,
            accept_larger_local_files=True,
        )[0]
        == "unverified"
    )


@pytest.mark.parametrize("accept_larger", [False, True])
def test_old_cached_legacy_result_respects_current_policy(tmp_path, accept_larger):
    db = open_db(tmp_path / "state.db")
    migrate(db)
    settings = SettingsRepository(db)
    assert settings.get("accept_larger_local_files") == "0"
    if accept_larger:
        settings.set("accept_larger_local_files", "1")
    record = SimpleNamespace(level="has_older_version", error="larger")
    expected = "has_older_version" if accept_larger else "unverified"
    assert _stored_result(record, accept_larger)["level"] == expected
    results = [{"status": "has_older_version", "verify_result": {"level": record.level}}]
    _apply_file_size_policy(results, db)
    assert results[0]["status"] == ("has_older_version" if accept_larger else "changed_manifest")
    assert results[0]["verify_result"]["level"] == expected
    db.close()
