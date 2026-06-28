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
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

if TYPE_CHECKING:
    from infocon_librarian.torrent.adapter import TorrentAdapter

from infocon_librarian.remote.client import RemoteClient, RemoteFetchError
from infocon_librarian.remote.fancyindex import parse_listing
from infocon_librarian.services.check_service import check_collections
from infocon_librarian.services.verify_runner import manifest_check
from infocon_librarian.storage.database import open_db
from infocon_librarian.storage.migrations import migrate
from infocon_librarian.storage.repositories import ArchiveRootRepository, CheckRepository, VerificationRepository

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


def _find_torrent_url(evidence: list[dict]) -> str | None:
    """Extract torrent_url from remote_listing evidence, if present."""
    for ev in evidence:
        if ev.get("kind") == "remote_listing":
            url = (ev.get("payload") or {}).get("torrent_url")
            if url:
                return url
    return None


def run_check(
    *,
    check_id: str,
    db_path: Path,
    archive_root: Path,
    section: str | None,
    broadcast: Callable[[dict[str, Any]], None],
    base_url: str = INFOCON_BASE_URL,
    adapter: "TorrentAdapter | None" = None,
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
        total_sections = len(sections_to_scan)

        with RemoteClient() as client:
            for sec_idx, sec in enumerate(sections_to_scan, 1):
                broadcast({
                    "type": "check_progress",
                    "check_id": check_id,
                    "phase": "fetch",
                    "section": sec,
                    "current": sec_idx,
                    "total": total_sections,
                })
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

        # Auto-verify present_unverified collections using stored results or fresh
        # manifest checks (fetch torrent → stat local files).
        verify_repo = VerificationRepository(conn)
        root_repo = ArchiveRootRepository(conn)
        root_record = root_repo.get_by_path(str(archive_root.resolve()))
        root_id = root_record.id if root_record is not None else None

        if root_id is not None:
            existing = verify_repo.get_all_latest(root_id)

            # Pass 1: upgrade from stored verification results (no network needed)
            to_verify: list[tuple[dict, str, str]] = []
            for item in all_results:
                if item["status"] != "present_unverified":
                    continue
                ckey = f"{item['section']}/{item['key']}"
                rec = existing.get(ckey)
                if rec is not None:
                    if rec.level in ("manifest_verified", "piece_verified"):
                        item["status"] = "verified_current"
                        item["verify_result"] = {"level": rec.level, "error": rec.error}
                    elif rec.level == "has_older_version":
                        item["status"] = "has_older_version"
                        item["verify_result"] = {"level": rec.level, "error": rec.error}
                    else:
                        # unverified — set status but still re-verify to get full
                        # file details for the UI (stored record only has level+error)
                        item["status"] = "changed_manifest"
                        torrent_url = _find_torrent_url(item.get("evidence", []))
                        if torrent_url:
                            to_verify.append((item, ckey, torrent_url))
                        else:
                            item["verify_result"] = {"level": rec.level, "error": rec.error}
                else:
                    torrent_url = _find_torrent_url(item.get("evidence", []))
                    if torrent_url:
                        to_verify.append((item, ckey, torrent_url))

            # Pass 1b: attach stored verify results to non-present_unverified collections
            # so the UI can show the last-known verification state (e.g. CHANGED with Issues)
            for item in all_results:
                if "verify_result" in item:
                    continue
                ckey = f"{item['section']}/{item['key']}"
                rec = existing.get(ckey)
                if rec is not None:
                    item["verify_result"] = {"level": rec.level, "error": rec.error}

            # Pass 2: fresh manifest check for collections with no stored result
            if to_verify and adapter is not None:
                log.info(
                    "Check %s: auto-verifying %d unverified collections",
                    check_id, len(to_verify),
                )
                total_verify = len(to_verify)
                broadcast({
                    "type": "check_progress",
                    "check_id": check_id,
                    "phase": "verify",
                    "current": 0,
                    "total": total_verify,
                })
                with RemoteClient() as vclient:
                    for verify_idx, (item, ckey, torrent_url) in enumerate(to_verify, 1):
                        try:
                            _parts = urlsplit(torrent_url)
                            encoded_url = urlunsplit(_parts._replace(path=quote(unquote(_parts.path), safe="/")))
                            resp = vclient.fetch(encoded_url)
                            if resp.status_code == 404:
                                log.warning("Check %s: no torrent for %s (404)", check_id, ckey)
                                item["verify_result"] = {"level": "no_torrent", "error": None}
                                continue
                            if resp.status_code not in (200, 304):
                                continue
                            level, error, details = manifest_check(
                                resp.body,
                                section=item["section"],
                                archive_root=archive_root,
                                adapter=adapter,
                            )
                            verify_repo.create(
                                str(uuid.uuid4()),
                                collection_key=ckey,
                                archive_root_id=root_id,
                                level=level,
                                torrent_url=torrent_url,
                                error=error,
                            )
                            if level == "manifest_verified":
                                item["status"] = "verified_current"
                            elif level == "has_older_version":
                                item["status"] = "has_older_version"
                            elif level == "unverified":
                                item["status"] = "changed_manifest"
                            # Attach result counts so UI can render Details cell
                            item["verify_result"] = {
                                "level": level,
                                "error": error,
                                "total_files": details.get("total_files"),
                                "missing_dirs": details.get("missing_dirs", []),
                                "missing_dirs_total": details.get("missing_dirs_total", 0),
                                "missing": details.get("missing", []),
                                "missing_total": details.get("missing_total", 0),
                                "size_larger": details.get("size_larger", []),
                                "size_larger_total": details.get("size_larger_total", 0),
                                "size_smaller": details.get("size_smaller", []),
                                "size_smaller_total": details.get("size_smaller_total", 0),
                            }
                        except Exception as exc:
                            log.warning(
                                "Check %s: auto-verify failed for %s: %s",
                                check_id, ckey, exc,
                            )
                        broadcast({
                            "type": "check_progress",
                            "check_id": check_id,
                            "phase": "verify",
                            "current": verify_idx,
                            "total": total_verify,
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
    adapter: "TorrentAdapter | None" = None,
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
            "adapter": adapter,
        },
        daemon=True,
        name=f"check-{check_id[:8]}",
    )
    t.start()
    return t
