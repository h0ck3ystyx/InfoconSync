"""Safe path mapping from torrent/URL path components to archive destinations."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from infocon_librarian.domain.errors import PathEscapesRoot

# Windows reserved device names (also dangerous on POSIX cross-platform tools)
_RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }
)

# Characters illegal in Windows filenames (also dangerous in URLs)
_ILLEGAL_CHARS_RE = re.compile(r'[<>:"|?*\x00-\x1f]')


@dataclass(frozen=True)
class SafePath:
    """A validated, root-relative destination path."""

    root: Path           # absolute, canonical archive root
    relative: str        # forward-slash-separated, no leading slash
    absolute: Path       # = root / relative (confirmed to be inside root)


class CaseCollision(Exception):
    """Two paths that differ only in case would map to the same destination."""

    def __init__(self, new_path: str, existing_path: str) -> None:
        super().__init__(
            f"Case collision: {new_path!r} conflicts with existing {existing_path!r}"
        )
        self.new_path = new_path
        self.existing_path = existing_path


def safe_archive_path(
    root: Path,
    components: list[str],
    *,
    existing_paths: list[str] | None = None,
) -> SafePath:
    """Map path components to a safe destination inside *root*.

    Args:
        root: Absolute, canonical archive root directory.
        components: Path components from a torrent file list or URL (may be
            untrusted). Forward or backward slashes within a component are
            treated as sub-component separators.
        existing_paths: Optional list of root-relative paths already present,
            used to detect case-insensitive collisions.

    Returns:
        SafePath confirming the destination is inside *root*.

    Raises:
        PathEscapesRoot: If any component would push the result outside *root*.
        ValueError: If a component is empty, contains NUL bytes, or is a
            platform-reserved name.
        CaseCollision: If the resolved path case-insensitively matches an
            existing path.
    """
    # Split each component on forward/back slashes to handle compound components.
    # Check for absolute paths BEFORE splitting so "/etc/passwd" is caught.
    parts: list[str] = []
    for comp in components:
        if os.path.isabs(comp):
            raise PathEscapesRoot(f"Absolute path component: {comp!r}")
        # Windows drive letter (e.g. "C:")
        if len(comp) >= 2 and comp[1] == ":" and comp[0].isalpha():
            raise PathEscapesRoot(f"Absolute path component: {comp!r}")
        for part in re.split(r"[/\\]", comp):
            parts.append(part)

    _validate_parts(parts)

    # Build the relative path incrementally, checking containment at each step
    relative_parts: list[str] = []
    for part in parts:
        if part in (".", ""):
            continue
        relative_parts.append(part)

    relative = "/".join(relative_parts)
    if not relative:
        raise ValueError("Path components resolve to an empty relative path")

    candidate = (root / relative).resolve()

    # Confirm the resolved path is inside root (follows symlinks)
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise PathEscapesRoot(str(candidate)) from exc

    # Symlink escape check: none of the intermediate directories may be a
    # symlink that points outside root
    _check_no_symlink_escape(root, relative_parts)

    # Case-collision check
    if existing_paths is not None:
        lower = relative.lower()
        for existing in existing_paths:
            if existing.lower() == lower and existing != relative:
                raise CaseCollision(relative, existing)

    return SafePath(root=root, relative=relative, absolute=candidate)


def _validate_parts(parts: list[str]) -> None:
    """Raise ValueError or PathEscapesRoot for any invalid component."""
    for part in parts:
        if part == "":
            continue  # will be skipped by the caller
        if "\x00" in part:
            raise ValueError(f"Path component contains NUL byte: {part!r}")
        if part == "..":
            raise PathEscapesRoot(f".. component: {part!r}")
        if os.path.isabs(part):
            raise PathEscapesRoot(f"Absolute path component: {part!r}")
        if _ILLEGAL_CHARS_RE.search(part):
            raise ValueError(f"Path component contains illegal characters: {part!r}")
        stem = part.upper().rsplit(".", 1)[0]
        if stem in _RESERVED_NAMES:
            raise ValueError(f"Path component is a reserved name: {part!r}")


def _check_no_symlink_escape(root: Path, parts: list[str]) -> None:
    """Walk the path tree; raise PathEscapesRoot if any symlink leaves root."""
    resolved_root = root.resolve()
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            target = current.resolve()
            try:
                target.relative_to(resolved_root)
            except ValueError:
                raise PathEscapesRoot(f"Symlink {current} → {target} escapes root") from None
