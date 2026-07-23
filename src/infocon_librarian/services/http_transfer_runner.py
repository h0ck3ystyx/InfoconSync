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
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx

from infocon_librarian.remote.client import new_http_client
from infocon_librarian.remote.fancyindex import parse_listing
from infocon_librarian.services.pause_control import wait_while_paused
from infocon_librarian.storage.database import open_db
from infocon_librarian.storage.migrations import migrate
from infocon_librarian.storage.plan_repository import PlanItemRecord, PlanRepository
from infocon_librarian.storage.settings_repository import SettingsRepository
from infocon_librarian.transfer.http_downloader import DownloadState, download

log = logging.getLogger(__name__)

_RECEIPTS_DIRNAME = "receipts"
_MAX_CRAWL_DEPTH = 8

# Emit an INFO progress line every this many files so a large collection is not
# a silent black box between "N files" and completion.
_PROGRESS_LOG_EVERY = 25

# If a single worker sees this many files fail back-to-back, its HTTP/2
# connection to infocon.org is probably wedged (the server resets long-lived
# connections). Rebuild that worker's client so the next file negotiates a fresh
# connection instead of retrying into a dead one.
_CONSECUTIVE_FAILURES_BEFORE_REFRESH = 5

# Upper bound on concurrent file downloads per collection. infocon.org is
# volunteer infrastructure; this caps how hard any one plan can hit it even if
# the stored setting is raised beyond it.
HTTP_CONCURRENCY_MAX = 8

# How often a worker re-checks a paused item before resuming its file.
_PAUSE_POLL_INTERVAL = 2.0

# nginx fancyindex consumes these files server-side for directory display;
# they don't appear in the listing HTML but are downloadable by direct URL.
_FANCYINDEX_HIDDEN = ("HEADER.md", "FOOTER.md", "HEADER.html", "FOOTER.html")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _resolve_concurrency(conn: Any) -> int:
    """Read the http_concurrency setting and clamp it to a safe range."""
    value = SettingsRepository(conn).get_int("http_concurrency")
    return max(1, min(value, HTTP_CONCURRENCY_MAX))


def _probe_hidden_files(
    dir_url: str,
    collection_url: str,
    client: httpx.Client,
) -> list[tuple[str, str]]:
    """HEAD-probe for nginx fancyindex files that aren't listed in the directory HTML."""
    base = dir_url.rstrip("/") + "/"
    found = []
    for name in _FANCYINDEX_HIDDEN:
        url = base + name
        if not url.startswith(collection_url):
            continue
        try:
            resp = client.head(url, timeout=10)
            if resp.status_code == 200:
                rel = unquote(url[len(collection_url):].lstrip("/"))
                found.append((rel, url))
                log.debug("Found hidden fancyindex file: %s", url)
        except Exception:
            pass
    return found


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
    # Probe for nginx fancyindex files not visible in the listing
    results.extend(_probe_hidden_files(current_url, collection_url, client))
    return results


class _ClientHolder:
    """Owns the shared HTTP client and can rebuild it if the connection wedges."""

    def __init__(self) -> None:
        self.client = new_http_client(timeout=60)

    def refresh(self) -> None:
        with contextlib.suppress(Exception):
            self.client.close()
        self.client = new_http_client(timeout=60)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.client.close()


class _WorkerClients:
    """One HTTP/2 client per worker thread, with independent wedge-recovery.

    An httpx client is safe to reuse across the many files a single worker
    thread downloads, but sharing one client across *all* workers would mean a
    reset connection (infocon.org drops long-lived ones) stalls every worker at
    once. Giving each thread its own client lets a wedged worker rebuild its own
    connection without disturbing the others. A per-thread consecutive-failure
    count drives that refresh.
    """

    def __init__(self, factory: Callable[[], httpx.Client] | None = None) -> None:
        self._factory = factory or (lambda: new_http_client(timeout=60))
        self._tl = threading.local()
        self._lock = threading.Lock()
        self._all: list[httpx.Client] = []

    def get(self) -> httpx.Client:
        client = getattr(self._tl, "client", None)
        if client is None:
            client = self._factory()
            self._tl.client = client
            with self._lock:
                self._all.append(client)
        return client

    def note_success(self) -> None:
        self._tl.failures = 0

    def note_failure(self) -> int:
        count = getattr(self._tl, "failures", 0) + 1
        self._tl.failures = count
        return count

    def refresh(self) -> None:
        """Rebuild the calling thread's client and reset its failure count."""
        old = getattr(self._tl, "client", None)
        if old is not None:
            with contextlib.suppress(Exception):
                old.close()
        client = self._factory()
        self._tl.client = client
        self._tl.failures = 0
        with self._lock:
            self._all.append(client)

    def close_all(self) -> None:
        with self._lock:
            clients = list(self._all)
            self._all.clear()
        for client in clients:
            with contextlib.suppress(Exception):
                client.close()


def _is_already_complete(destination: Path) -> bool:
    """True when a finished download already sits at *destination*.

    We only atomically rename to the final path on full completion, so a
    non-empty final file with no ``.part`` sidecar is a completed download and
    can be skipped when a plan is re-run or resumed.
    """
    part_path = destination.with_suffix(destination.suffix + ".part")
    return (
        destination.exists()
        and not part_path.exists()
        and destination.stat().st_size > 0
    )


def _download_item(
    item: PlanItemRecord,
    archive_root: Path,
    holder: _ClientHolder,
    broadcast: Callable[[dict[str, Any]], None],
    plan_id: str,
    repo: PlanRepository,
    *,
    concurrency: int,
    client_factory: Callable[[], httpx.Client] | None = None,
) -> dict[str, Any]:
    """Download all files for one plan item. Returns outcome dict.

    Files are fetched by a bounded pool of *concurrency* workers. The crawl and
    all DB access stay serialised; only the file transfers run in parallel.

    *client_factory* builds each worker's httpx client; tests inject a
    MockTransport-backed one, production uses the default infocon.org client.
    """
    collection_url = item.url
    if not collection_url:
        return {"status": "failed", "error": "no_url"}

    dest_base = archive_root / item.destination_relpath
    archive_root_resolved = archive_root.resolve()

    broadcast({"type": "item_status", "plan_id": plan_id, "item_id": item.id,
               "status": "downloading", "collection_key": item.collection_key})

    file_list = _crawl_files(collection_url, collection_url, holder.client)
    if not file_list:
        return {"status": "failed", "error": "no_files_found"}

    total = len(file_list)
    log.info(
        "HTTP transfer: %d files for %s (concurrency=%d)",
        total, item.destination_relpath, concurrency,
    )

    clients = _WorkerClients(client_factory)
    # Guards the shared counters below. The single sqlite connection is shared
    # across workers, so every repo call is serialised through db_lock too.
    state_lock = threading.Lock()
    db_lock = threading.Lock()
    abort = threading.Event()

    failed_files: list[str] = []
    total_downloaded = 0
    skipped = 0
    processed = 0
    # Cumulative live byte count for item-level progress. With several files in
    # flight at once a single file's offset is meaningless, so the UI shows this
    # monotonic running total (total size unknown → total_bytes stays None).
    progress_total = 0
    abort_status: str | None = None

    def _wait_while_paused_locked(item_id: str) -> str:
        """Like wait_while_paused, but serialises the shared connection."""
        while True:
            with db_lock:
                current = repo.get_item(item_id)
            status = current.status if current is not None else "failed"
            if status != "paused":
                return status
            time.sleep(_PAUSE_POLL_INTERVAL)

    def _log_progress_locked() -> None:
        log.info(
            "HTTP transfer %s: %d/%d files (%d skipped, %d failed, %.1f MB)",
            item.destination_relpath, processed, total, skipped,
            len(failed_files), total_downloaded / (1024 * 1024),
        )

    def _download_one(rel_path: str, file_url: str) -> None:
        nonlocal total_downloaded, skipped, processed, progress_total, abort_status
        if abort.is_set():
            return

        # The item may have been paused — block here rather than continuing to
        # download while the user thinks it's stopped.
        status = _wait_while_paused_locked(item.id)
        if status == "pending":
            with db_lock:
                repo.update_item_status(item.id, "downloading")
            broadcast({"type": "item_status", "plan_id": plan_id, "item_id": item.id,
                       "status": "downloading", "collection_key": item.collection_key})
        elif status != "downloading":
            with state_lock:
                if abort_status is None:
                    abort_status = status
            abort.set()
            return

        destination = (dest_base / rel_path).resolve()

        # Containment check — destination must stay inside archive root
        if not str(destination).startswith(str(archive_root_resolved)):
            log.warning("Path escape rejected: %s", destination)
            with state_lock:
                failed_files.append(rel_path)
                processed += 1
            return

        # Resume support: a completed file from a prior run is left in place.
        if _is_already_complete(destination):
            clients.note_success()
            with state_lock:
                skipped += 1
                processed += 1
                should_log = processed % _PROGRESS_LOG_EVERY == 0 or processed == total
                if should_log:
                    _log_progress_locked()
            return

        last = 0

        def _progress(bytes_so_far: int, _total_bytes: int | None, _iid: str = item.id) -> None:
            nonlocal last, progress_total
            with state_lock:
                progress_total += bytes_so_far - last
                snapshot = progress_total
            last = bytes_so_far
            broadcast({
                "type": "progress",
                "item_id": _iid,
                "plan_id": plan_id,
                "downloaded_bytes": snapshot,
                "total_bytes": None,
            })

        result = download(
            file_url, destination, http_client=clients.get(), progress=_progress
        )
        if result.state == DownloadState.COMPLETE:
            clients.note_success()
            with state_lock:
                total_downloaded += result.downloaded_bytes
                processed += 1
                should_log = processed % _PROGRESS_LOG_EVERY == 0 or processed == total
                if should_log:
                    _log_progress_locked()
            log.debug("Downloaded %s (%d B)", rel_path, result.downloaded_bytes)
        else:
            log.warning("File download failed %s: %s", file_url, result.error)
            failures = clients.note_failure()
            with state_lock:
                failed_files.append(rel_path)
                processed += 1
                should_log = processed % _PROGRESS_LOG_EVERY == 0 or processed == total
                if should_log:
                    _log_progress_locked()
            if failures >= _CONSECUTIVE_FAILURES_BEFORE_REFRESH:
                log.warning(
                    "Item %s: %d consecutive failures on one worker — "
                    "refreshing its HTTP connection",
                    item.id[:8], failures,
                )
                clients.refresh()

    try:
        with ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix=f"http-{item.id[:6]}"
        ) as pool:
            futures = [
                pool.submit(_download_one, rel_path, file_url)
                for rel_path, file_url in file_list
            ]
            for future in futures:
                # Surface an unexpected worker crash instead of swallowing it,
                # but keep draining the rest of the pool.
                exc = future.exception()
                if exc is not None:
                    log.exception("HTTP worker crashed for %s: %s", item.id[:8], exc)
    finally:
        clients.close_all()

    if abort_status is not None:
        return {"status": abort_status, "downloaded_bytes": total_downloaded}

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
    concurrency = _resolve_concurrency(conn)

    try:
        items = repo.list_items(plan_id)
        https_items = [it for it in items if it.method == "https" and it.status == "pending"]

        if not https_items:
            log.info("Plan %s: no pending HTTPS items — marking complete", plan_id)
            repo.update_plan_state(plan_id, "complete")
            broadcast({"type": "plan_status", "plan_id": plan_id, "state": "complete"})
            return

        outcomes: dict[str, dict[str, Any]] = {}

        holder = _ClientHolder()
        try:
            for item in https_items:
                # The item may have been paused while still queued behind an
                # earlier item — wait here rather than starting it anyway.
                queued_status = wait_while_paused(repo, item.id)
                if queued_status != "pending":
                    continue

                log.info("HTTP transfer: item %s -> %s", item.id[:8], item.destination_relpath)
                repo.update_item_status(item.id, "downloading")

                outcome = _download_item(
                    item, archive_root, holder, broadcast, plan_id, repo,
                    concurrency=concurrency,
                )
                outcomes[item.id] = outcome

                repo.update_item_status(item.id, outcome["status"])
                broadcast({
                    "type": "item_status",
                    "plan_id": plan_id,
                    "item_id": item.id,
                    "status": outcome["status"],
                    "error": outcome.get("error"),
                    "collection_key": item.collection_key,
                })
        finally:
            holder.close()

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
