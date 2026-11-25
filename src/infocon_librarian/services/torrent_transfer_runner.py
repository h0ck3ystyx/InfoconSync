"""Torrent transfer runner — downloads torrent plan items via libtorrent.

Runs in a daemon thread — one thread per plan. Polls the adapter until
each torrent completes or times out, then writes a receipt.

Architecture note: this calls the adapter directly from a daemon thread
as a stopgap until TransferManager is wired up.  The adapter's session
is thread-safe for add/poll/remove operations.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from infocon_librarian.service_runtime import (
    ServiceInterrupted,
    checkpoint,
    interruptible_sleep,
    start_worker,
)

if TYPE_CHECKING:
    from infocon_librarian.torrent.adapter import TorrentAdapter

from infocon_librarian.domain.models import TorrentStartParams, TransferState
from infocon_librarian.remote.client import new_http_client
from infocon_librarian.remote.mirror import get_site, mirror_client, mirror_path, read_limited
from infocon_librarian.services.pause_control import wait_while_paused
from infocon_librarian.storage.database import open_db
from infocon_librarian.storage.migrations import migrate
from infocon_librarian.storage.plan_repository import PlanRepository

log = logging.getLogger(__name__)

_RECEIPTS_DIRNAME = "receipts"
_POLL_INTERVAL = 3.0    # seconds between status polls
_ITEM_TIMEOUT = 4 * 3600  # 4-hour per-item ceiling
_STALL_TIMEOUT = 300.0  # 5 min with 0 peers + no progress → blocked


def _fetch_torrent_bytes(torrent_url: str) -> bytes:
    """Fetch .torrent file bytes, normalising any double-encoded percent sequences."""
    _parts = urlsplit(torrent_url)
    encoded_url = urlunsplit(_parts._replace(path=quote(unquote(_parts.path), safe="/")))
    with new_http_client(timeout=60) as client:
        resp = client.get(encoded_url)
        resp.raise_for_status()
        return resp.content


def run_torrent_transfer(
    plan_id: str,
    *,
    db_path: Path,
    archive_root: Path,
    adapter: TorrentAdapter,
    broadcast: Callable[[dict[str, Any]], None],
    stall_timeout: float = _STALL_TIMEOUT,
) -> None:
    """Run torrent downloads for all pending torrent items in *plan_id*.

    Called from a daemon thread; opens its own DB connection.
    """
    conn = open_db(db_path)
    migrate(conn)
    repo = PlanRepository(conn)

    active_job = None
    resume_path = None
    try:
        checkpoint()
        plan = repo.get_plan(plan_id)
        if plan is None:
            return
        site = get_site(conn, plan.site_id)
        if site is None or site.version != plan.site_version:
            raise ValueError("Plan mirror site is unavailable")
        items = repo.list_items(plan_id)
        torrent_items = [it for it in items if it.method == "torrent" and it.status == "pending"]

        if not torrent_items:
            log.info("Plan %s: no pending torrent items", plan_id)
            return

        outcomes: dict[str, dict[str, Any]] = {}

        for item in torrent_items:
            # The item may have been paused while still queued behind an
            # earlier item — wait here rather than starting it anyway.
            queued_status = wait_while_paused(repo, item.id)
            if queued_status != "pending":
                continue

            log.info("Torrent transfer: item %s → %s", item.id[:8], item.destination_relpath)
            repo.update_item_status(item.id, "downloading")
            broadcast({
                "type": "item_status",
                "plan_id": plan_id,
                "item_id": item.id,
                "status": "downloading",
                "collection_key": item.collection_key,
            })

            # InfoCon torrents are rooted at the section. DEF CON torrents are
            # accepted only when they reproduce all files in the mirror item.
            ckey = item.collection_key or item.destination_relpath
            section = ckey.split("/", 1)[0] if "/" in ckey else ""
            save_path = archive_root / section
            try:
                if item.mirror_json:
                    if site.id != "defcon" or not item.url:
                        raise ValueError("Torrent source is unavailable")
                    mirror = json.loads(item.mirror_json)
                    with mirror_client(site.base_url) as client:
                        torrent_bytes = read_limited(client, item.url)
                    if hashlib.sha256(torrent_bytes).hexdigest() != mirror["torrent_digest"]:
                        raise ValueError("Torrent metadata changed. Check the site again.")
                    expected = {path for path, _ in mirror["files"]}
                    torrent_files = adapter.inspect(torrent_bytes).files
                    if {file.relative_path for file in torrent_files} != expected:
                        raise ValueError("Torrent no longer matches the selected files")
                    save_path = (
                        mirror_path(archive_root, site.relative_folder)
                        if site.relative_folder else archive_root
                    )
                    for path in expected:
                        mirror_path(save_path, path)
                elif item.url:
                    torrent_bytes = _fetch_torrent_bytes(item.url)
                else:
                    raise ValueError("Missing torrent URL")
            except Exception as exc:
                log.warning("Torrent fetch failed for item %s: %s", item.id[:8], exc)
                repo.update_item_status(item.id, "failed")
                outcomes[item.id] = {"status": "failed", "error": f"torrent_fetch_failed:{exc}"}
                broadcast({
                    "type": "item_status",
                    "plan_id": plan_id,
                    "item_id": item.id,
                    "status": "failed",
                    "error": str(exc),
                    "collection_key": item.collection_key,
                })
                continue

            checkpoint()
            resume_path = db_path.parent / "jobs" / f"{item.id}.resume"
            resume_data = resume_path.read_bytes() if resume_path.exists() else None
            params = TorrentStartParams(
                torrent_bytes=torrent_bytes,
                save_path=str(save_path),
                selected_indices=(),  # download all files
                resume_data=resume_data,
            )

            try:
                job_id = adapter.start(params)
                active_job = job_id
            except Exception as exc:
                log.warning("Torrent start failed for item %s: %s", item.id[:8], exc)
                repo.update_item_status(item.id, "failed")
                outcomes[item.id] = {"status": "failed", "error": f"torrent_start_failed:{exc}"}
                broadcast({
                    "type": "item_status",
                    "plan_id": plan_id,
                    "item_id": item.id,
                    "status": "failed",
                    "error": str(exc),
                    "collection_key": item.collection_key,
                })
                continue

            # Poll until the torrent finishes, times out, stalls, or errors
            deadline = time.monotonic() + _ITEM_TIMEOUT
            final_state = "failed"
            last_error: str | None = None
            last_logged_state: str | None = None
            last_log_time = time.monotonic()
            _LOG_INTERVAL = 30.0  # log progress at most once per 30 s
            last_progress_bytes = 0
            stall_since: float | None = None  # when 0-peer/no-progress stall started

            while time.monotonic() < deadline:
                checkpoint()
                current_item = repo.get_item(item.id)
                if current_item is not None and current_item.status == "paused":
                    log.info("Torrent %s: pausing per user request", item.destination_relpath)
                    with contextlib.suppress(Exception):
                        adapter.pause(job_id)
                    broadcast({
                        "type": "item_status", "plan_id": plan_id, "item_id": item.id,
                        "status": "paused", "collection_key": item.collection_key,
                    })
                    resumed_status = wait_while_paused(repo, item.id)
                    if resumed_status != "pending":
                        final_state = "failed"
                        last_error = "cancelled_while_paused"
                        break
                    log.info("Torrent %s: resuming", item.destination_relpath)
                    with contextlib.suppress(Exception):
                        adapter.resume(job_id)
                    repo.update_item_status(item.id, "downloading")
                    broadcast({
                        "type": "item_status", "plan_id": plan_id, "item_id": item.id,
                        "status": "downloading", "collection_key": item.collection_key,
                    })
                    stall_since = None
                    last_progress_bytes = 0
                    continue

                try:
                    progress = adapter.poll(job_id)
                except Exception as exc:
                    log.warning("Poll failed for job %s: %s", job_id.value, exc)
                    last_error = str(exc)
                    break

                broadcast({
                    "type": "progress",
                    "item_id": item.id,
                    "plan_id": plan_id,
                    "downloaded_bytes": progress.downloaded_bytes,
                    "total_bytes": progress.total_bytes,
                    "num_peers": progress.num_peers,
                    "download_rate": progress.download_rate,
                    "torrent_state": progress.state.value,
                })

                state_str = progress.state.value
                now = time.monotonic()
                if state_str != last_logged_state or now - last_log_time >= _LOG_INTERVAL:
                    total_mb = (progress.total_bytes / 1_048_576) if progress.total_bytes else 0
                    done_mb = progress.downloaded_bytes / 1_048_576
                    peers = progress.num_peers
                    rate_kb = progress.download_rate // 1024
                    if progress.total_bytes:
                        log.info(
                            "Torrent %s: %s — %.1f / %.1f MB, %d peer(s), %d KB/s",
                            item.destination_relpath, state_str,
                            done_mb, total_mb, peers, rate_kb,
                        )
                    else:
                        log.info(
                            "Torrent %s: %s — %.1f MB done, %d peer(s), %d KB/s",
                            item.destination_relpath, state_str, done_mb, peers, rate_kb,
                        )
                    last_logged_state = state_str
                    last_log_time = now

                if progress.state == TransferState.COMPLETE:
                    final_state = "complete"
                    break
                elif progress.state == TransferState.FAILED:
                    last_error = progress.last_error
                    break

                # Stall detection: downloading with 0 peers and no byte progress
                if (
                    progress.state == TransferState.DOWNLOADING
                    and progress.num_peers == 0
                    and progress.downloaded_bytes <= last_progress_bytes
                ):
                    if stall_since is None:
                        stall_since = now
                    elif now - stall_since >= stall_timeout:
                        log.warning(
                            "Torrent %s: swarm unreachable after %.0f s — marking blocked",
                            item.destination_relpath, now - stall_since,
                        )
                        final_state = "blocked"
                        last_error = "swarm_unreachable"
                        break
                else:
                    stall_since = None
                    last_progress_bytes = progress.downloaded_bytes

                interruptible_sleep(_POLL_INTERVAL)
            else:
                last_error = "transfer_timeout"

            with contextlib.suppress(Exception):
                adapter.remove_keep_data(job_id)
            active_job = None
            if final_state == "complete":
                resume_path.unlink(missing_ok=True)

            if final_state == "complete":
                repo.update_item_status(item.id, "complete")
                outcomes[item.id] = {
                    "status": "complete",
                    "verification_level": "downloaded_unverified",
                }
            elif final_state == "blocked":
                repo.update_item_status(item.id, "blocked")
                outcomes[item.id] = {
                    "status": "blocked",
                    "error": last_error or "swarm_unreachable",
                }
            else:
                repo.update_item_status(item.id, "failed")
                outcomes[item.id] = {
                    "status": "failed",
                    "error": last_error or "torrent_failed",
                }

            broadcast({
                "type": "item_status",
                "plan_id": plan_id,
                "item_id": item.id,
                "status": final_state,
                "error": last_error,
                "collection_key": item.collection_key,
            })

        # If there are also HTTPS items they will update plan state themselves.
        # Only finalize here when all plan items are torrent-only.
        # If any item is blocked (swarm unreachable), leave the plan running —
        # the user may approve an HTTP fallback, which takes over finalization.
        all_items = repo.list_items(plan_id)
        http_pending = any(
            it.method == "https" and it.status in ("pending", "downloading")
            for it in all_items
        )
        has_blocked = any(o.get("status") == "blocked" for o in outcomes.values())
        checkpoint()
        unfinished = any(it.status in ("pending", "downloading", "paused", "blocked")
                         for it in all_items)
        if not http_pending and not has_blocked and not unfinished:
            all_ok = all(it.status == "complete" for it in all_items)
            plan_final = "complete" if all_ok else "failed"
            if not repo.finish_if_running(plan_id, plan_final):
                return

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
                    "verification_level": outcomes.get(it.id, {}).get(
                    "verification_level",
                    "downloaded_unverified" if it.status == "complete" else None,
                ),
                    "error": outcomes.get(it.id, {}).get("error"),
                    "size_bytes": it.size_bytes,
                    "fallback_reason": it.fallback_reason,
                }
                for it in all_items
            ]
            receipt_path.write_text(json.dumps({
                "receipt_id": receipt_id,
                "plan_id": plan_id,
                "site_id": site.id,
                "completed_at": datetime.datetime.now(datetime.UTC).isoformat(),
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

    except ServiceInterrupted:
        log.info("Torrent plan %s interrupted; retaining resume state", plan_id)
    except Exception as exc:
        log.exception("Torrent transfer plan %s failed", plan_id)
        with contextlib.suppress(Exception):
            repo.update_plan_state(plan_id, "failed")
        broadcast({"type": "plan_status", "plan_id": plan_id, "state": "failed", "error": str(exc)})
    finally:
        if active_job is not None:
            try:
                adapter.pause(active_job)
                data = adapter.save_resume_data(active_job)
                if resume_path is not None:
                    resume_path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = resume_path.with_suffix(".new")
                    temporary.write_bytes(data)
                    temporary.replace(resume_path)
            except Exception:
                log.exception("Could not save torrent resume data; retry will recheck files")
            finally:
                with contextlib.suppress(Exception):
                    adapter.remove_keep_data(active_job)
        conn.close()


def start_torrent_transfer_thread(
    plan_id: str,
    *,
    db_path: Path,
    archive_root: Path,
    adapter: TorrentAdapter,
    broadcast: Callable[[dict[str, Any]], None],
    stall_timeout: float = _STALL_TIMEOUT,
) -> threading.Thread:
    """Spawn a daemon thread to run torrent transfers for the plan."""
    t = threading.Thread(
        target=run_torrent_transfer,
        kwargs={
            "plan_id": plan_id,
            "db_path": db_path,
            "archive_root": archive_root,
            "adapter": adapter,
            "broadcast": broadcast,
            "stall_timeout": stall_timeout,
        },
        daemon=True,
        name=f"torrent-xfer-{plan_id[:8]}",
    )
    start_worker(t)
    return t
