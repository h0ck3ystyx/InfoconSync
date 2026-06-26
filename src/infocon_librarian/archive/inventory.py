"""Local filesystem inventory — iterative, cancellable, symlink-safe."""
from __future__ import annotations

import os
import threading
from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path

from infocon_librarian.domain.models import SnapshotEntry


def scan(
    root: Path,
    *,
    app_data_dir: Path | None = None,
    progress: Callable[[int], None] | None = None,
    cancel: threading.Event | None = None,
) -> Iterator[SnapshotEntry]:
    """Yield SnapshotEntry for every regular file under root.

    Uses iterative scandir (no recursion) so deep trees cannot overflow the
    stack. Symlinked *directories* are skipped; symlinked files are included.
    The app_data_dir subtree is silently skipped.
    """
    resolved_root = root.resolve()
    resolved_app_data = app_data_dir.resolve() if app_data_dir else None

    count = 0
    stack: deque[Path] = deque([resolved_root])

    while stack:
        if cancel is not None and cancel.is_set():
            return

        current = stack.popleft()

        try:
            with os.scandir(current) as it:
                for entry in it:
                    if cancel is not None and cancel.is_set():
                        return

                    entry_path = Path(entry.path)

                    if entry.is_symlink() and entry.is_dir(follow_symlinks=True):
                        continue

                    if entry.is_dir(follow_symlinks=False):
                        if resolved_app_data is not None:
                            try:
                                entry_path.resolve().relative_to(resolved_app_data)
                                continue
                            except ValueError:
                                pass
                        stack.append(entry_path)
                    elif entry.is_file(follow_symlinks=False):
                        stat = entry.stat(follow_symlinks=False)
                        relative = str(entry_path.relative_to(resolved_root))
                        relative = relative.replace(os.sep, "/")
                        yield SnapshotEntry(
                            relative_path=relative,
                            size=stat.st_size,
                            mtime_ns=stat.st_mtime_ns,
                        )
                        count += 1
                        if progress is not None:
                            progress(count)
        except PermissionError:
            continue


def top_level_dirs(root: Path) -> set[str]:
    """Return the set of top-level subdirectory names under root."""
    result: set[str] = set()
    try:
        with os.scandir(root) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    result.add(entry.name)
    except (PermissionError, OSError):
        pass
    return result
