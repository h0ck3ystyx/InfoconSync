"""Shared pause/resume waiting logic for transfer runners.

Pause and resume are DB status flips issued by Flask handlers in
web/api.py. Per architecture invariant #3 ("only TransferManager calls the
torrent adapter"), the Flask handlers never touch the adapter directly —
they only set plan_items.status to "paused" or "pending". The runner
threads (torrent_transfer_runner, http_transfer_runner), which already own
the adapter/HTTP calls, poll for that flip here and react to it.
"""
from __future__ import annotations

import time

from infocon_librarian.storage.plan_repository import PlanRepository

_PAUSE_POLL_INTERVAL = 2.0


def wait_while_paused(repo: PlanRepository, item_id: str) -> str:
    """Block while the item's status is "paused". Return the status once it changes."""
    while True:
        current = repo.get_item(item_id)
        status = current.status if current is not None else "failed"
        if status != "paused":
            return status
        time.sleep(_PAUSE_POLL_INTERVAL)
