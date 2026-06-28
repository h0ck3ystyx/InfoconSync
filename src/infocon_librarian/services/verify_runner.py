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

from infocon_librarian.remote.client import RemoteClient, RemoteFetchError
from infocon_librarian.storage.database import open_db
from infocon_librarian.storage.migrations import migrate
from infocon_librarian.storage.repositories import VerificationRepository

log = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)


_DETAIL_CAP = 50  # max file paths sent to UI


def _manifest_check(
    torrent_bytes: bytes,
    *,
    section: str,
    archive_root: Path,
    adapter: "TorrentAdapter",
) -> tuple[str, str | None, dict]:
    """Stat each file in the torrent manifest. Returns (level, error_summary, details)."""
    manifest = adapter.inspect(torrent_bytes)
    save_root = archive_root / section

    missing: list[str] = []
    wrong_size: list[dict] = []

    for tf in manifest.files:
        local = save_root / tf.relative_path
        if not local.exists():
            missing.append(tf.relative_path)
        elif local.stat().st_size != tf.size:
            wrong_size.append({
                "path": tf.relative_path,
                "expected": tf.size,
                "actual": local.stat().st_size,
            })

    details: dict = {
        "total_files": len(manifest.files),
        "missing": missing[:_DETAIL_CAP],
        "missing_total": len(missing),
        "wrong_size": wrong_size[:_DETAIL_CAP],
        "wrong_size_total": len(wrong_size),
    }

    if not missing and not wrong_size:
        return "manifest_verified", None, details

    parts = []
    if missing:
        parts.append(f"{len(missing)} missing file(s)")
    if wrong_size:
        parts.append(f"{len(wrong_size)} size mismatch(es)")
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
        log.info("Verify %s: fetching torrent %s", verify_id, torrent_url)
        with RemoteClient() as client:
            result = client.fetch(torrent_url)

        if result.status_code not in (200, 304):
            raise RemoteFetchError(
                f"torrent fetch returned HTTP {result.status_code} for {torrent_url}"
            )

        torrent_bytes = result.body
        section = collection_key.split("/", 1)[0]
        log.info("Verify %s: manifest-checking %s (%s bytes torrent)", verify_id, collection_key, len(torrent_bytes))

        level, error, details = _manifest_check(
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
