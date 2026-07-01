"""HTTP transfer runner — downloads HTTPS plan items from InfoCon.

For each HTTPS plan item (which represents one collection directory):
1. Crawls the InfoCon fancyindex listing recursively to enumerate all files.
2. Downloads each file via http_downloader.download() with resume support.
3. Updates item status in the DB and broadcasts SSE progress events.
4. Marks the plan complete and writes a receipt JSON file.

Runs in a daemon thread — one thread per plan.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import logging
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx

from infocon_librarian.remote.fancyindex import parse_listing
from infocon_librarian.storage.database import open_db
from infocon_librarian.storage.migrations import migrate
from infocon_librarian.storage.plan_repository import PlanItemRecord, PlanRepository
from infocon_librarian.transfer.http_downloader import DownloadState, download

log = logging.getLogger(__name__)

_RECEIPTS_DIRNAME = "receipts"
_MAX_CRAWL_DEPTH = 8


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _crawl_files(
    collection_url: str,
    current_url: str,
    client: httpx.Client,
    *,
    _depth: int = 0,
) -> list[tuple[str, str]]:
    """Return (relative_path_within_collection, file_url) for every file under current_url.

    relative_path is relative to collection_url, preserving subdirectory structure.
    Only entries whose URL starts with collection_url are followed (safety invariant).
    """
    if _depth > _MAX_CRAWL_DEPTH:
        log.warning("Crawl depth limit reached at %s", current_url)
        return []
    try:
        resp = client.get(current_url, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        log.warning("Crawl failed for %s: %s", current_url, exc)
        return []

    entries = parse_listing(resp.text, current_url)
    results: list[tuple[str, str]] = []
    for entry in entries:
        if not entry.url.startswith(collection_url):
            continue  # never follow links outside the collection
        rel = unquote(entry.url[len(collection_url):].lstrip("/"))
        if not rel:
            continue
        if entry.kind == "file":
            results.append((rel, entry.url))
        elif entry.kind == "directory":
            results.extend(
                _crawl_files(collection_url, entry.url, client, _depth=_depth + 1)
            )
    return results


def _download_item(
    item: PlanItemRecord,
    archive_root: Path,
    client: httpx.Client,
    broadcast: Callable[[dict[str, Any]], None],
    plan_id: str,
) -> dict[str, Any]:
    """Download all files for one plan item. Returns outcome dict."""
    collection_url = item.url
    if not collection_url:
        return {"status": "failed", "error": "no_url"}

    dest_base = archive_root / item.destination_relpath
    archive_root_resolved = archive_root.resolve()

    broadcast({"type": "item_status", "plan_id": plan_id, "item_id": item.id, "status": "downloading"})

    file_list = _crawl_files(collection_url, collection_url, client)
    if not file_list:
        return {"status": "failed", "error": "no_files_found"}

    log.info("HTTP transfer: %d files for %s", len(file_list), item.destination_relpath)

    failed_files: list[str] = []
    total_downloaded = 0

    for rel_path, file_url in file_list:
        destination = (dest_base / rel_path).resolve()

        # Containment check — destination must stay inside archive root
        if not str(destination).startswith(str(archive_root_resolved)):
            log.warning("Path escape rejected: %s", destination)
            failed_files.append(rel_path)
            continue

        def _progress(bytes_so_far: int, total: int | None, _iid: str = item.id) -> None:
            broadcast({
                "type": "progress",
                "item_id": _iid,
                "plan_id": plan_id,
                "downloaded_bytes": bytes_so_far,
                "total_bytes": total,
            })

        result = download(file_url, destination, http_client=client, progress=_progress)
        if result.state == DownloadState.COMPLETE:
            total_downloaded += result.downloaded_bytes
            log.debug("Downloaded %s (%d B)", rel_path, result.downloaded_bytes)
        else:
            log.warning("File download failed %s: %s", file_url, result.error)
            failed_files.append(rel_path)

    if failed_files:
        log.warning("Item %s: %d file(s) failed", item.id, len(failed_files))
        return {
            "status": "failed",
            "error": f"{len(failed_files)}_files_failed",
            "downloaded_bytes": total_downloaded,
        }

    return {
        "status": "complete",
        "verification_level": "downloaded_unverified",
        "downloaded_bytes": total_downloaded,
    }


def run_http_transfer(
    plan_id: str,
    *,
    db_path: Path,
    archive_root: Path,
    broadcast: Callable[[dict[str, Any]], None],
) -> None:
    """Run HTTP transfers for all pending HTTPS items in a plan. Call from a daemon thread."""
    conn = open_db(db_path)
    migrate(conn)
    repo = PlanRepository(conn)

    try:
        items = repo.list_items(plan_id)
        https_items = [it for it in items if it.method == "https" and it.status == "pending"]

        if not https_items:
            log.info("Plan %s: no pending HTTPS items — marking complete", plan_id)
            repo.update_plan_state(plan_id, "complete")
            broadcast({"type": "plan_status", "plan_id": plan_id, "state": "complete"})
            return

        outcomes: dict[str, dict[str, Any]] = {}

        with httpx.Client(follow_redirects=True, timeout=60) as client:
            for item in https_items:
                log.info("HTTP transfer: item %s → %s", item.id[:8], item.destination_relpath)
                repo.update_item_status(item.id, "downloading")

                outcome = _download_item(item, archive_root, client, broadcast, plan_id)
                outcomes[item.id] = outcome

                repo.update_item_status(item.id, outcome["status"])
                broadcast({
                    "type": "item_status",
                    "plan_id": plan_id,
                    "item_id": item.id,
                    "status": outcome["status"],
                    "error": outcome.get("error"),
                })

        all_ok = all(o["status"] == "complete" for o in outcomes.values())
        plan_final = "complete" if all_ok else "failed"
        repo.update_plan_state(plan_id, plan_final)

        # Write receipt
        receipt_id = str(uuid.uuid4())
        receipt_dir = db_path.parent / _RECEIPTS_DIRNAME
        receipt_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = receipt_dir / f"{receipt_id}.json"

        receipt_items = [
            {
                "plan_item_id": it.id,
                "relative_path": it.destination_relpath,
                "method": it.method,
                "status": outcomes.get(it.id, {}).get("status", it.status),
                "verification_level": outcomes.get(it.id, {}).get("verification_level"),
                "error": outcomes.get(it.id, {}).get("error"),
                "size_bytes": it.size_bytes,
                "fallback_reason": it.fallback_reason,
            }
            for it in https_items
        ]
        receipt_path.write_text(json.dumps({
            "receipt_id": receipt_id,
            "plan_id": plan_id,
            "completed_at": _now_iso(),
            "items": receipt_items,
        }, indent=2))
        repo.add_receipt(plan_id, str(receipt_path))

        log.info("Plan %s %s — receipt %s", plan_id, plan_final, receipt_id)
        broadcast({
            "type": "plan_status",
            "plan_id": plan_id,
            "state": plan_final,
            "receipt_id": receipt_id,
        })

    except Exception as exc:
        log.exception("HTTP transfer plan %s failed", plan_id)
        with contextlib.suppress(Exception):
            repo.update_plan_state(plan_id, "failed")
        broadcast({"type": "plan_status", "plan_id": plan_id, "state": "failed", "error": str(exc)})
    finally:
        conn.close()


def start_http_transfer_thread(
    plan_id: str,
    *,
    db_path: Path,
    archive_root: Path,
    broadcast: Callable[[dict[str, Any]], None],
) -> threading.Thread:
    """Spawn a daemon thread to run HTTP transfers for the plan. Returns the thread."""
    t = threading.Thread(
        target=run_http_transfer,
        kwargs={
            "plan_id": plan_id,
            "db_path": db_path,
            "archive_root": archive_root,
            "broadcast": broadcast,
        },
        daemon=True,
        name=f"http-xfer-{plan_id[:8]}",
    )
    t.start()
    return t
