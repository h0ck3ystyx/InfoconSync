"""TorrentMetainfoService — path-validate a torrent manifest against archive root."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from infocon_librarian.domain.errors import InvalidTorrent
from infocon_librarian.domain.models import TorrentManifest
from infocon_librarian.domain.paths import CaseCollision, SafePath, safe_archive_path
from infocon_librarian.torrent.adapter import TorrentAdapter


@dataclass(frozen=True)
class MetainfoResult:
    """Validated manifest plus the archive-root-relative path for each file."""

    manifest: TorrentManifest
    safe_paths: tuple[SafePath, ...]   # one per TorrentFile, same index order


def process_metainfo(
    torrent_bytes: bytes,
    *,
    archive_root: Path,
    adapter: TorrentAdapter,
    torrent_url: str = "",
    existing_paths: list[str] | None = None,
) -> MetainfoResult:
    """Inspect *torrent_bytes* and validate every file path against *archive_root*.

    Raises InvalidTorrent if:
    - The adapter cannot parse the torrent (malformed, unsupported).
    - Any file path contains ``..``, is absolute, uses reserved names, or
      a symlink in *archive_root* would escape the root.
    - A case collision exists against *existing_paths*.

    The engine never sees a torrent with unsafe paths.
    """
    manifest = adapter.inspect(torrent_bytes, url=torrent_url)

    safe_paths: list[SafePath] = []
    for f in manifest.files:
        raw = f.relative_path
        # Catch absolute paths before splitting (e.g. "/etc/passwd" → ["", "etc", "passwd"])
        if raw.startswith("/") or raw.startswith("\\"):
            raise InvalidTorrent(f"Absolute path in torrent: {raw!r}")
        if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
            raise InvalidTorrent(f"Absolute path in torrent (drive letter): {raw!r}")
        components = raw.replace("\\", "/").split("/")
        try:
            sp = safe_archive_path(archive_root, components, existing_paths=existing_paths)
        except CaseCollision as exc:
            raise InvalidTorrent(
                f"Case collision for torrent path {f.relative_path!r}: {exc}"
            ) from exc
        except Exception as exc:
            raise InvalidTorrent(
                f"Unsafe path in torrent: {f.relative_path!r} — {exc}"
            ) from exc
        safe_paths.append(sp)

    return MetainfoResult(manifest=manifest, safe_paths=tuple(safe_paths))


def coverage_map(
    manifest: TorrentManifest,
    selected_relative_paths: set[str],
) -> tuple[set[str], set[str]]:
    """Return *(covered, uncovered)* subsets of *selected_relative_paths*.

    A selected path is *covered* if the manifest contains an exact match or
    the manifest contains files whose path starts with that directory prefix.
    Uncovered paths are candidates for HTTPS fallback.
    """
    torrent_paths = {f.relative_path for f in manifest.files}
    covered: set[str] = set()
    uncovered: set[str] = set()

    for sel in selected_relative_paths:
        prefix = sel.rstrip("/") + "/"
        if sel in torrent_paths or any(
            tp == sel or tp.startswith(prefix) for tp in torrent_paths
        ):
            covered.add(sel)
        else:
            uncovered.add(sel)

    return covered, uncovered
