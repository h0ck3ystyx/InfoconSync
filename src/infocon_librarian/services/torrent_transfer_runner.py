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
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

if TYPE_CHECKING:
    from infocon_librarian.torrent.adapter import TorrentAdapter

import httpx

from infocon_librarian.domain.models import TorrentStartParams, TransferState
from infocon_librarian.storage.database import open_db
from infocon_librarian.storage.migrations import migrate
from infocon_librarian.storage.plan_repository import PlanRepository

log = logging.getLogger(__name__)

_RECEIPTS_DIRNAME = "receipts"
_POLL_INTERVAL = 3.0    # seconds between status polls
_ITEM_TIMEOUT = 4 * 3600  # 4-hour per-item ceiling


def _fetch_torrent_bytes(torrent_url: str) -> bytes:
    """Fetch .torrent file bytes, normalising any double-encoded percent sequences."""
    _parts = urlsplit(torrent_url)
    encoded_url = urlunsplit(_parts._replace(path=quote(unquote(_parts.path), safe="/")))
    with httpx.Client(follow_redirects=True, timeout=60) as client:
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
) -> None:
    """Run torrent downloads for all pending torrent items in *plan_id*.

    Called from a daemon thread; opens its own DB connection.
    """
    conn = open_db(db_path)
    migrate(conn)
    repo = PlanRepository(conn)

    try:
        items = repo.list_items(plan_id)
        torrent_items = [it for it in items if it.method == "torrent" and it.status == "pending"]

        if not torrent_items:
            log.info("Plan %s: no pending torrent items", plan_id)
            return

        outcomes: dict[str, dict[str, Any]] = {}

        for item in torrent_items:
            log.info("Torrent transfer: item %s → %s", item.id[:8], item.destination_relpath)
            repo.update_item_status(item.id, "downloading")
            broadcast({
                "type": "item_status",
                "plan_id": plan_id,
                "item_id": item.id,
                "status": "downloading",
            })

            # Fetch torrent file
            try:
                torrent_bytes = _fetch_torrent_bytes(item.url)
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
                })
                continue

            # save_path = archive_root / section so the torrent root dir lands correctly
            ckey = item.collection_key or item.destination_relpath
            section = ckey.split("/", 1)[0] if "/" in ckey else ""
            save_path = str(archive_root / section) if section else str(archive_root)

            params = TorrentStartParams(
                torrent_bytes=torrent_bytes,
                save_path=save_path,
                selected_indices=(),  # download all files
            )

            try:
                job_id = adapter.start(params)
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
                })
                continue

            # Poll until the torrent finishes, times out, or errors
            deadline = time.monotonic() + _ITEM_TIMEOUT
            final_state = "failed"
            last_error: str | None = None
            last_logged_state: str | None = None
            last_log_time = time.monotonic()
            _LOG_INTERVAL = 30.0  # log progress at most once per 30 s

            while time.monotonic() < deadline:
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

                time.sleep(_POLL_INTERVAL)
            else:
                last_error = "transfer_timeout"

            with contextlib.suppress(Exception):
                adapter.remove_keep_data(job_id)

            if final_state == "complete":
                repo.update_item_status(item.id, "complete")
                outcomes[item.id] = {
                    "status": "complete",
                    "verification_level": "downloaded_unverified",
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
            })

        # If there are also HTTPS items they will update plan state themselves.
        # Only finalize here when all plan items are torrent-only.
        all_items = repo.list_items(plan_id)
        http_pending = any(
            it.method == "https" and it.status in ("pending", "downloading")
            for it in all_items
        )
        if not http_pending:
            all_ok = all(o["status"] == "complete" for o in outcomes.values())
            plan_final = "complete" if all_ok else "failed"
            repo.update_plan_state(plan_id, plan_final)

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
                for it in torrent_items
            ]
            receipt_path.write_text(json.dumps({
                "receipt_id": receipt_id,
                "plan_id": plan_id,
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

    except Exception as exc:
        log.exception("Torrent transfer plan %s failed", plan_id)
        with contextlib.suppress(Exception):
            repo.update_plan_state(plan_id, "failed")
        broadcast({"type": "plan_status", "plan_id": plan_id, "state": "failed", "error": str(exc)})
    finally:
        conn.close()


def start_torrent_transfer_thread(
    plan_id: str,
    *,
    db_path: Path,
    archive_root: Path,
    adapter: TorrentAdapter,
    broadcast: Callable[[dict[str, Any]], None],
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
        },
        daemon=True,
        name=f"torrent-xfer-{plan_id[:8]}",
    )
    t.start()
    return t
