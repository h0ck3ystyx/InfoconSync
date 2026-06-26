"""Background check runner — fetches remote listings and compares to local archive.

Runs in a daemon thread so it doesn't block Flask request handling. Results are
persisted to the checks table and broadcast via SSE when complete.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from infocon_librarian.remote.client import RemoteClient, RemoteFetchError
from infocon_librarian.remote.fancyindex import parse_listing
from infocon_librarian.services.check_service import check_collections
from infocon_librarian.storage.database import open_db
from infocon_librarian.storage.migrations import migrate
from infocon_librarian.storage.repositories import CheckRepository

log = logging.getLogger(__name__)

# Top-level dirs to skip — OS/filesystem artefacts
_SKIP_DIRS = frozenset({
    "$RECYCLE.BIN",
    "System Volume Information",
    ".Spotlight-V100",
    ".fseventsd",
    ".TemporaryItems",
    ".Trashes",
})

INFOCON_BASE_URL = "https://infocon.org/"


def _local_top_dirs(section_path: Path) -> set[str]:
    """Return names of immediate subdirectories under section_path."""
    dirs: set[str] = set()
    try:
        with os.scandir(section_path) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    dirs.add(entry.name)
    except OSError:
        pass
    return dirs


def _section_url(base_url: str, section: str) -> str:
    return base_url.rstrip("/") + "/" + section.rstrip("/") + "/"


def run_check(
    *,
    check_id: str,
    db_path: Path,
    archive_root: Path,
    section: str | None,
    broadcast: Callable[[dict[str, Any]], None],
    base_url: str = INFOCON_BASE_URL,
) -> None:
    """Execute a full upstream check and persist results.

    Intended to be called from a daemon thread.
    """
    conn = open_db(db_path)
    migrate(conn)
    check_repo = CheckRepository(conn)

    try:
        # Resolve which sections to scan
        root_path = archive_root.resolve()
        if section:
            sections_to_scan = [section]
        else:
            # Every non-hidden, non-skip top-level directory
            sections_to_scan = sorted(
                name
                for name in _local_top_dirs(root_path)
                if name not in _SKIP_DIRS and not name.startswith(".")
            )

        log.info("Check %s: scanning sections %s", check_id, sections_to_scan)

        all_results: list[dict[str, Any]] = []

        with RemoteClient() as client:
            for sec in sections_to_scan:
                sec_path = root_path / sec
                url = _section_url(base_url, sec)
                log.info("Check %s: fetching %s", check_id, url)

                try:
                    result = client.fetch(url)
                    if result.status_code not in (200, 304):
                        log.warning(
                            "Check %s: %s returned %d, skipping",
                            check_id, url, result.status_code,
                        )
                        continue

                    html = result.body.decode("utf-8", errors="replace")
                    entries = parse_listing(html, url)
                    local_dirs = _local_top_dirs(sec_path)

                    check_results = check_collections(
                        section=sec,
                        remote_entries=entries,
                        local_dirs=local_dirs,
                    )

                    for cr in check_results:
                        all_results.append({
                            "section": sec,
                            "key": cr.collection_key.key,
                            "status": cr.status.value,
                            "display_name": cr.collection_key.key,
                            "evidence": [
                                {
                                    "kind": e.kind.value,
                                    "payload": e.payload,
                                    "observed_at": e.observed_at,
                                }
                                for e in cr.evidence
                            ],
                        })

                except RemoteFetchError as exc:
                    log.warning("Check %s: fetch failed for %s: %s", check_id, url, exc)
                    all_results.append({
                        "section": sec,
                        "key": sec,
                        "status": "unknown",
                        "display_name": sec,
                        "error": str(exc),
                        "evidence": [],
                    })

        result_json = json.dumps(all_results)
        check_repo.complete(check_id, result_json)
        log.info("Check %s: complete, %d results", check_id, len(all_results))

        broadcast({
            "type": "check_complete",
            "check_id": check_id,
            "count": len(all_results),
        })

    except Exception as exc:
        log.exception("Check %s failed", check_id)
        with contextlib.suppress(Exception):
            check_repo.fail(check_id, str(exc))
        broadcast({"type": "check_failed", "check_id": check_id, "error": str(exc)})
    finally:
        conn.close()


def start_check_thread(
    *,
    check_id: str,
    db_path: Path,
    archive_root: Path,
    section: str | None,
    broadcast: Callable[[dict[str, Any]], None],
    base_url: str = INFOCON_BASE_URL,
) -> threading.Thread:
    """Spin up a daemon thread to run the check. Returns the thread."""
    t = threading.Thread(
        target=run_check,
        kwargs={
            "check_id": check_id,
            "db_path": db_path,
            "archive_root": archive_root,
            "section": section,
            "broadcast": broadcast,
            "base_url": base_url,
        },
        daemon=True,
        name=f"check-{check_id[:8]}",
    )
    t.start()
    return t
