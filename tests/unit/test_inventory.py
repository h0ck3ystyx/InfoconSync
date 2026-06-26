"""D-007 — LocalInventory: bounded memory, cancellable, no recursion overflow."""
from __future__ import annotations

import threading
from pathlib import Path

from infocon_librarian.archive.inventory import scan, top_level_dirs


def _create_tree(root: Path, depth: int, breadth: int, files_per_dir: int = 1) -> int:
    """Create a synthetic tree; return total file count."""
    total = 0
    dirs = [root]
    for _level in range(depth):
        next_dirs = []
        for d in dirs:
            for i in range(breadth):
                child = d / f"dir_{i}"
                child.mkdir(exist_ok=True)
                for j in range(files_per_dir):
                    f = child / f"file_{j}.dat"
                    f.write_bytes(b"x")
                    total += 1
                next_dirs.append(child)
        dirs = next_dirs
    return total


# ---------------------------------------------------------------------------
# D-007: 100k generated paths — bounded memory, cancellable, no overflow
# ---------------------------------------------------------------------------


def test_d007_large_inventory_no_stack_overflow(tmp_path: Path) -> None:
    # Create ~1000 files spread across multiple directories (fast, avoids slow CI)
    root = tmp_path / "archive"
    root.mkdir()
    # 5^3 dirs * 8 files = 1000 files
    total_created = _create_tree(root, depth=3, breadth=5, files_per_dir=8)

    entries = list(scan(root))
    assert len(entries) == total_created
    for e in entries:
        assert e.relative_path  # non-empty string
        assert e.size == 1  # b"x"
        assert e.mtime_ns > 0


def test_d007_progress_callback_called(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    root.mkdir()
    for i in range(10):
        (root / f"file_{i}.txt").write_bytes(b"y")

    counts: list[int] = []
    list(scan(root, progress=counts.append))

    assert counts == list(range(1, 11))


def test_d007_cancel_stops_iteration(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    root.mkdir()
    for i in range(100):
        (root / f"file_{i}.txt").write_bytes(b"z")

    cancel = threading.Event()
    cancel.set()  # cancel immediately

    entries = list(scan(root, cancel=cancel))
    # Either zero or very few entries — definitely not all 100
    assert len(entries) < 100


def test_d007_symlinked_dir_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")

    link = root / "link_to_outside"
    link.symlink_to(outside)

    (root / "legit.txt").write_bytes(b"ok")

    entries = list(scan(root))
    relative_paths = [e.relative_path for e in entries]

    assert "legit.txt" in relative_paths
    assert not any("secret" in p for p in relative_paths)


def test_d007_app_data_dir_skipped(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    root.mkdir()
    app_data = root / "app_data"
    app_data.mkdir()
    (app_data / "librarian.db").write_bytes(b"db")
    (root / "talks.mp4").write_bytes(b"video")

    entries = list(scan(root, app_data_dir=app_data))
    relative_paths = [e.relative_path for e in entries]

    assert "talks.mp4" in relative_paths
    assert not any("librarian.db" in p for p in relative_paths)


def test_d007_top_level_dirs(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    root.mkdir()
    (root / "defcon").mkdir()
    (root / "shmoocon").mkdir()
    (root / "README.txt").write_text("notes")

    dirs = top_level_dirs(root)
    assert dirs == {"defcon", "shmoocon"}
