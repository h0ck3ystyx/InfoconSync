"""Background verification runner — manifest-checks a collection against its torrent.

Runs in a daemon thread (same pattern as check_runner). The adapter must have been
created at app startup; if None, the endpoint returns 503 before this is called.

Manifest verification: parse the torrent, stat each declared file on disk, compare
sizes. Takes seconds instead of minutes (no disk reads, no piece hashing).
"""
from __future__ import annotations

import contextlib
import logging
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from infocon_librarian.torrent.adapter import TorrentAdapter

from urllib.parse import quote, unquote, urlsplit, urlunsplit

from infocon_librarian.remote.client import RemoteClient, RemoteFetchError
from infocon_librarian.storage.database import open_db
from infocon_librarian.storage.migrations import migrate
from infocon_librarian.storage.repositories import VerificationRepository

log = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)


_DETAIL_CAP = 50  # max file paths sent to UI


def manifest_check(
    torrent_bytes: bytes,
    *,
    section: str,
    archive_root: Path,
    adapter: "TorrentAdapter",
) -> tuple[str, str | None, dict]:
    """Stat each file in the torrent manifest. Returns (level, error_summary, details).

    Distinguishes three size-mismatch cases:
    - local > expected: file is larger than the torrent declares — characteristic of
      pre-re-encoding originals (v1 content when torrent is v2).  Not corruption.
    - local < expected: file is smaller than declared — truncated or wrong file.
    - missing: file absent entirely.

    Returns 'has_older_version' when files are all present and only the
    larger-than-expected mismatch is found.  Returns 'unverified' for any missing
    or truncated files.
    """
    manifest = adapter.inspect(torrent_bytes)
    save_root = archive_root / section

    missing: list[str] = []
    size_larger: list[dict] = []   # local > expected — likely pre-encoding original
    size_smaller: list[dict] = []  # local < expected — truncated or wrong file

    for tf in manifest.files:
        local = save_root / tf.relative_path
        if not local.exists():
            missing.append(tf.relative_path)
        else:
            actual = local.stat().st_size
            if actual > tf.size:
                size_larger.append({"path": tf.relative_path, "expected": tf.size, "actual": actual})
            elif actual < tf.size:
                size_smaller.append({"path": tf.relative_path, "expected": tf.size, "actual": actual})

    # Split missing into wholly-absent subdirectories vs individual missing files.
    # libtorrent file_path() format: CollectionName/SubDir/file.ext
    # parts[0] = torrent root (collection name), parts[1] = first-level subdir.
    # A subdir is "entirely missing" when every torrent file under it is absent.
    from collections import defaultdict
    _subdir_total: dict[str, int] = defaultdict(int)
    _subdir_missing: dict[str, int] = defaultdict(int)
    _missing_set = set(missing)
    for tf in manifest.files:
        _parts = tf.relative_path.split("/")
        if len(_parts) >= 3:  # CollectionName/SubDir/file
            _subdir_total[_parts[1]] += 1
            if tf.relative_path in _missing_set:
                _subdir_missing[_parts[1]] += 1
    _fully_missing = {d for d, total in _subdir_total.items() if _subdir_missing.get(d, 0) == total}
    missing_dirs = sorted(_fully_missing)
    missing_files = [
        m for m in missing
        if not (len(m.split("/")) >= 3 and m.split("/")[1] in _fully_missing)
    ]

    details: dict = {
        "total_files": len(manifest.files),
        "missing_dirs": missing_dirs[:_DETAIL_CAP],
        "missing_dirs_total": len(missing_dirs),
        "missing": missing_files[:_DETAIL_CAP],
        "missing_total": len(missing_files),
        "size_larger": size_larger[:_DETAIL_CAP],
        "size_larger_total": len(size_larger),
        "size_smaller": size_smaller[:_DETAIL_CAP],
        "size_smaller_total": len(size_smaller),
    }

    if not missing and not size_larger and not size_smaller:
        return "manifest_verified", None, details

    # All files present, only larger-than-expected mismatches → older version
    if not missing and not size_smaller and size_larger:
        summary = f"{len(size_larger)} file(s) are larger than the current torrent expects"
        return "has_older_version", summary, details

    # Missing dirs/files or truncated files → genuine problem
    parts = []
    if missing_dirs and missing_files:
        parts.append(f"{len(missing_dirs)} missing folder(s), {len(missing_files)} missing file(s)")
    elif missing_dirs:
        parts.append(f"{len(missing_dirs)} missing folder(s)")
    elif missing_files:
        parts.append(f"{len(missing_files)} missing")
    if size_smaller:
        parts.append(f"{len(size_smaller)} truncated/wrong-size")
    if size_larger:
        parts.append(f"{len(size_larger)} larger than expected")
    return "unverified", "; ".join(parts), details


def run_verify(
    *,
    verify_id: str,
    collection_key: str,
    archive_root_id: str | None,
    db_path: Path,
    archive_root: Path,
    torrent_url: str,
    broadcast: Callable[[dict[str, Any]], None],
    adapter: "TorrentAdapter",
) -> None:
    """Fetch torrent, run manifest check, persist result. Intended for a daemon thread."""
    conn = open_db(db_path)
    migrate(conn)
    verify_repo = VerificationRepository(conn)

    try:
        # Normalise URL encoding: unquote first to avoid double-encoding %20 → %2520
        _parts = urlsplit(torrent_url)
        encoded_url = urlunsplit(_parts._replace(path=quote(unquote(_parts.path), safe="/")))

        log.info("Verify %s: fetching torrent %s", verify_id, encoded_url)
        with RemoteClient() as client:
            result = client.fetch(encoded_url)

        if result.status_code == 404:
            log.warning("Verify %s: no torrent found for %s (404)", verify_id, collection_key)
            broadcast({
                "type": "verify_complete",
                "verify_id": verify_id,
                "collection_key": collection_key,
                "level": "no_torrent",
                "error": "No torrent file found for this collection",
                "details": {},
            })
            return

        if result.status_code not in (200, 304):
            raise RemoteFetchError(
                f"torrent fetch returned HTTP {result.status_code} for {encoded_url}"
            )

        torrent_bytes = result.body
        section = collection_key.split("/", 1)[0]
        log.info("Verify %s: manifest-checking %s (%s bytes torrent)", verify_id, collection_key, len(torrent_bytes))

        level, error, details = manifest_check(
            torrent_bytes,
            section=section,
            archive_root=archive_root,
            adapter=adapter,
        )

        verify_repo.create(
            verify_id,
            collection_key=collection_key,
            archive_root_id=archive_root_id,
            level=level,
            torrent_url=torrent_url,
            error=error,
        )

        log.info("Verify %s: %s → %s", verify_id, collection_key, level)
        broadcast({
            "type": "verify_complete",
            "verify_id": verify_id,
            "collection_key": collection_key,
            "level": level,
            "error": error,
            "details": details,
        })

    except Exception as exc:
        log.exception("Verify %s failed for %s", verify_id, collection_key)
        with contextlib.suppress(Exception):
            verify_repo.create(
                verify_id,
                collection_key=collection_key,
                archive_root_id=archive_root_id,
                level="unverified",
                torrent_url=torrent_url,
                error=str(exc),
            )
        broadcast({
            "type": "verify_failed",
            "verify_id": verify_id,
            "collection_key": collection_key,
            "error": str(exc),
        })
    finally:
        conn.close()


def start_verify_thread(
    *,
    collection_key: str,
    archive_root_id: str | None,
    db_path: Path,
    archive_root: Path,
    torrent_url: str,
    broadcast: Callable[[dict[str, Any]], None],
    adapter: "TorrentAdapter",
) -> tuple[str, threading.Thread]:
    """Spin up a daemon thread to run the verification. Returns (verify_id, thread)."""
    verify_id = str(uuid.uuid4())
    t = threading.Thread(
        target=run_verify,
        kwargs={
            "verify_id": verify_id,
            "collection_key": collection_key,
            "archive_root_id": archive_root_id,
            "db_path": db_path,
            "archive_root": archive_root,
            "torrent_url": torrent_url,
            "broadcast": broadcast,
            "adapter": adapter,
        },
        daemon=True,
        name=f"verify-{verify_id[:8]}",
    )
    t.start()
    return verify_id, t
