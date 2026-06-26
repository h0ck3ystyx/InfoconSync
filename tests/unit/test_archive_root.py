"""F-003, F-004 — ArchiveRootValidator."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from infocon_librarian.archive.root import validate_root
from infocon_librarian.domain.errors import ArchiveRootError

# ---------------------------------------------------------------------------
# F-003: Writable root
# ---------------------------------------------------------------------------


def test_f003_writable_root(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    root.mkdir()

    info = validate_root(root)

    assert info.canonical_path == str(root.resolve())
    assert info.volume_fingerprint != ""
    assert info.free_bytes > 0


def test_f003_volume_fingerprint_stable(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    root.mkdir()

    info1 = validate_root(root)
    info2 = validate_root(root)

    assert info1.volume_fingerprint == info2.volume_fingerprint


def test_f003_known_sections_detected(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    root.mkdir()
    (root / "Defcon").mkdir()
    (root / "Shmoocon").mkdir()
    (root / "random_stuff").mkdir()

    info = validate_root(root)

    assert "Defcon" in info.known_sections
    assert "Shmoocon" in info.known_sections
    assert "random_stuff" not in info.known_sections


# ---------------------------------------------------------------------------
# F-004: Read-only or disconnected root
# ---------------------------------------------------------------------------


def test_f004_nonexistent_path_rejected() -> None:
    nonexistent = Path("/tmp/does_not_exist_infocon_librarian_test")
    with pytest.raises(ArchiveRootError, match="not exist|disconnected|No such"):
        validate_root(nonexistent)


def test_f004_file_not_directory_rejected(tmp_path: Path) -> None:
    f = tmp_path / "not_a_dir.txt"
    f.write_text("hello")
    with pytest.raises(ArchiveRootError, match="not a directory"):
        validate_root(f)


def test_f004_nested_in_app_data_rejected(tmp_path: Path) -> None:
    app_data = tmp_path / "app_data"
    app_data.mkdir()
    archive = app_data / "my_archive"
    archive.mkdir()

    with pytest.raises(ArchiveRootError, match="nested"):
        validate_root(archive, app_data_dir=app_data)


def test_f004_non_nested_root_accepted(tmp_path: Path) -> None:
    app_data = tmp_path / "app_data"
    app_data.mkdir()
    archive = tmp_path / "archive"
    archive.mkdir()

    info = validate_root(archive, app_data_dir=app_data)
    assert info.canonical_path == str(archive.resolve())


@pytest.mark.skipif(os.getuid() == 0, reason="root can write to read-only dirs")
def test_f004_read_only_root_rejected(tmp_path: Path) -> None:
    root = tmp_path / "readonly_archive"
    root.mkdir()
    root.chmod(0o555)

    try:
        with pytest.raises(ArchiveRootError, match="not writable"):
            validate_root(root)
    finally:
        root.chmod(0o755)
