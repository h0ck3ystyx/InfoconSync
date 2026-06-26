"""Torrent link discovery within a remote directory listing."""
from __future__ import annotations

import re

from infocon_librarian.domain.models import RemoteEntry

_TORRENT_RE = re.compile(r"\.torrent$", re.IGNORECASE)


def find_torrent_links(entries: list[RemoteEntry]) -> list[RemoteEntry]:
    """Return only file entries whose URL ends with .torrent."""
    return [e for e in entries if e.kind == "file" and _TORRENT_RE.search(e.url)]


def associate_torrents(
    directories: list[RemoteEntry],
    torrents: list[RemoteEntry],
) -> dict[str, str]:
    """Map each directory's display_name to its best-guess torrent URL.

    Heuristic: strip all non-alphanumeric characters and compare whether the
    directory's compact form is a prefix of the torrent stem's compact form.
    This handles "DEF CON 32" ↔ "defcon-32-v2.torrent" (defcon32 ≤ defcon32v2).
    """
    result: dict[str, str] = {}
    for torrent in torrents:
        stem = _compact(torrent.display_name.removesuffix(".torrent"))
        for directory in directories:
            compact_dir = _compact(directory.display_name)
            if compact_dir and stem and (
                compact_dir == stem
                or stem.startswith(compact_dir)
                or compact_dir.startswith(stem)
            ):
                result[directory.display_name] = torrent.url
                break
    return result


def _normalize(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    name = name.lower()
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def _compact(name: str) -> str:
    """Lowercase, strip ALL non-alphanumeric characters for fuzzy matching."""
    return re.sub(r"[^a-z0-9]", "", name.lower())
