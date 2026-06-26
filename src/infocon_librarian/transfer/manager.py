"""TransferManager — owns all adapter calls; drives job state machine."""
from __future__ import annotations

import contextlib
from dataclasses import dataclass

from infocon_librarian.domain.models import (
    EngineJobId,
    TorrentStartParams,
    TransferProgress,
    TransferState,
)
from infocon_librarian.torrent.adapter import TorrentAdapter
from infocon_librarian.transfer.progress import CoalescingWriter

# Number of consecutive DOWNLOADING polls with 0 peers before giving up
_DEFAULT_NO_PEER_THRESHOLD = 3


@dataclass
class _JobRecord:
    engine_id: EngineJobId
    last_state: TransferState = TransferState.CHECKING
    recheck_triggered: bool = False
    piece_verified: bool = False
    no_peer_ticks: int = 0
    gave_up_on_peers: bool = False
    resume_data: bytes | None = None


class TransferManager:
    """Drives torrent jobs through their lifecycle using a TorrentAdapter.

    **Thread model**: in production, wrap in a worker thread and call
    run_forever().  In tests, call process_tick() directly to step the
    state machine without spawning threads.

    **Invariant**: a job is never marked piece-verified until after a
    successful final recheck following download completion.
    """

    def __init__(
        self,
        adapter: TorrentAdapter,
        *,
        coalesce_window: float = 2.0,
        no_peer_threshold: int = _DEFAULT_NO_PEER_THRESHOLD,
    ) -> None:
        self._adapter = adapter
        self._records: dict[str, _JobRecord] = {}
        self._coalescer = CoalescingWriter(window_seconds=coalesce_window)
        self._no_peer_threshold = no_peer_threshold
        # Injected writer — replaced in tests to capture state changes
        self._on_state_change: dict[str, list[TransferState]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_job(self, local_id: str, params: TorrentStartParams) -> EngineJobId:
        """Start a torrent job. Returns the engine-assigned job ID."""
        engine_id = self._adapter.start(params)
        self._records[local_id] = _JobRecord(engine_id=engine_id)
        self._on_state_change[local_id] = [TransferState.CHECKING]
        return engine_id

    def process_tick(self, local_id: str) -> TransferProgress:
        """Poll the adapter once and advance the state machine."""
        record = self._records[local_id]
        progress = self._adapter.poll(record.engine_id)
        self._handle(local_id, record, progress)
        return progress

    def pause_job(self, local_id: str) -> None:
        self._adapter.pause(self._records[local_id].engine_id)

    def resume_job(self, local_id: str) -> None:
        self._adapter.resume(self._records[local_id].engine_id)

    def get_state(self, local_id: str) -> TransferState:
        return self._records[local_id].last_state

    def is_piece_verified(self, local_id: str) -> bool:
        return self._records[local_id].piece_verified

    def get_resume_data(self, local_id: str) -> bytes | None:
        return self._records[local_id].resume_data

    def state_history(self, local_id: str) -> list[TransferState]:
        return list(self._on_state_change.get(local_id, []))

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def _handle(self, local_id: str, record: _JobRecord, progress: TransferProgress) -> None:
        new_state = progress.state

        # ---- Download complete: trigger final recheck ----
        if new_state == TransferState.COMPLETE and not record.recheck_triggered:
            record.recheck_triggered = True
            with contextlib.suppress(Exception):
                record.resume_data = self._adapter.save_resume_data(record.engine_id)
            self._adapter.recheck(record.engine_id)
            # Do NOT update state to COMPLETE yet — wait for recheck result
            return

        # ---- Recheck complete: mark piece-verified ----
        if new_state == TransferState.COMPLETE and record.recheck_triggered:
            record.piece_verified = True
            self._write_state(local_id, record, TransferState.COMPLETE)
            return

        # ---- Terminal failure ----
        if new_state == TransferState.FAILED:
            self._write_state(local_id, record, TransferState.FAILED)
            return

        # ---- No-peers detection ----
        if new_state == TransferState.DOWNLOADING and progress.num_peers == 0:
            record.no_peer_ticks += 1
            if record.no_peer_ticks >= self._no_peer_threshold and not record.gave_up_on_peers:
                record.gave_up_on_peers = True
                self._write_state(local_id, record, TransferState.AWAITING_USER_FALLBACK)
                return
        else:
            record.no_peer_ticks = 0

        # ---- Coalesced normal update ----
        self._coalescer.update(
            local_id,
            new_state,
            lambda: self._write_state(local_id, record, new_state),
        )

    def _write_state(
        self, local_id: str, record: _JobRecord, state: TransferState
    ) -> None:
        record.last_state = state
        self._on_state_change.setdefault(local_id, []).append(state)
