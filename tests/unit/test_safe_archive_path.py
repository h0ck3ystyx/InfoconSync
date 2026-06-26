"""F-005, F-006, F-007 — SafeArchivePath validation."""
from __future__ import annotations

from pathlib import Path

import pytest

from infocon_librarian.domain.errors import PathEscapesRoot
from infocon_librarian.domain.paths import CaseCollision, safe_archive_path

# ---------------------------------------------------------------------------
# F-005: .., absolute, empty, and NUL path components are all rejected
# ---------------------------------------------------------------------------


def test_f005_dotdot_rejected(tmp_path: Path) -> None:
    with pytest.raises(PathEscapesRoot, match=r"\.\.|traversal|escapes"):
        safe_archive_path(tmp_path, ["..", "evil.txt"])


def test_f005_dotdot_embedded_rejected(tmp_path: Path) -> None:
    with pytest.raises(PathEscapesRoot):
        safe_archive_path(tmp_path, ["subdir", "..", "..", "evil.txt"])


def test_f005_absolute_path_rejected(tmp_path: Path) -> None:
    with pytest.raises(PathEscapesRoot):
        safe_archive_path(tmp_path, ["/etc/passwd"])


def test_f005_absolute_windows_path_rejected(tmp_path: Path) -> None:
    with pytest.raises((PathEscapesRoot, ValueError)):
        safe_archive_path(tmp_path, ["C:\\Windows\\System32\\evil.exe"])


def test_f005_nul_byte_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="NUL"):
        safe_archive_path(tmp_path, ["file\x00name.txt"])


def test_f005_empty_component_skipped(tmp_path: Path) -> None:
    # Empty components between separators should be ignored, not raise
    result = safe_archive_path(tmp_path, ["a", "", "b.txt"])
    assert result.relative == "a/b.txt"


def test_f005_reserved_name_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reserved"):
        safe_archive_path(tmp_path, ["NUL"])


def test_f005_reserved_name_with_ext_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reserved"):
        safe_archive_path(tmp_path, ["CON.txt"])


def test_f005_valid_path_accepted(tmp_path: Path) -> None:
    result = safe_archive_path(tmp_path, ["talks", "2024", "keynote.mp4"])
    assert result.relative == "talks/2024/keynote.mp4"
    assert result.absolute == tmp_path / "talks" / "2024" / "keynote.mp4"
    assert result.root == tmp_path


def test_f005_slash_in_component_split(tmp_path: Path) -> None:
    # A component containing a slash is split into sub-parts
    result = safe_archive_path(tmp_path, ["talks/2024/keynote.mp4"])
    assert result.relative == "talks/2024/keynote.mp4"


# ---------------------------------------------------------------------------
# F-006: Symlink escaping root is rejected
# ---------------------------------------------------------------------------


def test_f006_symlink_escape_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)

    root = tmp_path / "archive"
    root.mkdir()
    evil_link = root / "evil"
    evil_link.symlink_to(outside)

    with pytest.raises(PathEscapesRoot, match="[Ss]ymlink|escapes"):
        safe_archive_path(root, ["evil", "payload.txt"])


def test_f006_symlink_within_root_allowed(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    root.mkdir()
    inner = root / "inner"
    inner.mkdir()
    link = root / "link_to_inner"
    link.symlink_to(inner)

    # Symlink points inside root — should be allowed
    result = safe_archive_path(root, ["link_to_inner", "file.txt"])
    assert "link_to_inner" in result.relative


# ---------------------------------------------------------------------------
# F-007: Case collision detection
# ---------------------------------------------------------------------------


def test_f007_case_collision_detected(tmp_path: Path) -> None:
    existing = ["defcon/talks/README.TXT"]
    with pytest.raises(CaseCollision):
        safe_archive_path(
            tmp_path,
            ["defcon", "talks", "readme.txt"],
            existing_paths=existing,
        )


def test_f007_exact_match_not_collision(tmp_path: Path) -> None:
    existing = ["defcon/talks/readme.txt"]
    result = safe_archive_path(
        tmp_path,
        ["defcon", "talks", "readme.txt"],
        existing_paths=existing,
    )
    assert result.relative == "defcon/talks/readme.txt"


def test_f007_no_collision_different_names(tmp_path: Path) -> None:
    existing = ["defcon/talks/other.txt"]
    result = safe_archive_path(
        tmp_path,
        ["defcon", "talks", "readme.txt"],
        existing_paths=existing,
    )
    assert result.relative == "defcon/talks/readme.txt"


def test_f007_no_existing_paths_no_check(tmp_path: Path) -> None:
    # When existing_paths is None, collision check is skipped entirely
    result = safe_archive_path(tmp_path, ["README.TXT"], existing_paths=None)
    assert result.relative == "README.TXT"
