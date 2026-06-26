"""VerifyService — recheck existing collection data against a torrent manifest."""
from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from pathlib import Path

from infocon_librarian.domain.models import (
    EngineJobId,
    TorrentStartParams,
    TransferProgress,
    TransferState,
    VerificationLevel,
)
from infocon_librarian.torrent.adapter import TorrentAdapter
from infocon_librarian.torrent.metainfo import MetainfoResult, process_metainfo


@dataclass(frozen=True)
class VerifyResult:
    engine_id: EngineJobId
    verification_level: VerificationLevel
    progress: TransferProgress
    error: str | None = None


def verify(
    torrent_bytes: bytes,
    *,
    archive_root: Path,
    adapter: TorrentAdapter,
    torrent_url: str = "",
    timeout: float = 30.0,
    poll_interval: float = 0.25,
) -> VerifyResult:
    """Recheck existing data in *archive_root* against *torrent_bytes*.

    Does not contact any trackers or peers — only piece-checks local data.
    Returns VerifyResult with PIECE_VERIFIED on success, UNVERIFIED on failure.
    """
    meta: MetainfoResult = process_metainfo(
        torrent_bytes,
        archive_root=archive_root,
        adapter=adapter,
        torrent_url=torrent_url,
    )

    params = TorrentStartParams(
        torrent_bytes=torrent_bytes,
        save_path=str(archive_root),
        selected_indices=tuple(f.index for f in meta.manifest.files),
        enable_dht=False,
        enable_pex=False,
        enable_lsd=False,
        enable_upnp=False,
        enable_natpmp=False,
    )

    engine_id = adapter.start(params)

    deadline = time.monotonic() + timeout
    last_progress: TransferProgress | None = None
    recheck_triggered = False

    while time.monotonic() < deadline:
        progress = adapter.poll(engine_id)
        last_progress = progress

        if progress.state == TransferState.COMPLETE and not recheck_triggered:
            # Initial check (existing-data scan) done — request piece recheck
            adapter.recheck(engine_id)
            recheck_triggered = True

        elif progress.state == TransferState.COMPLETE and recheck_triggered:
            # Recheck passed
            adapter.remove_keep_data(engine_id)
            return VerifyResult(
                engine_id=engine_id,
                verification_level=VerificationLevel.PIECE_VERIFIED,
                progress=progress,
            )

        elif progress.state == TransferState.FAILED:
            adapter.remove_keep_data(engine_id)
            return VerifyResult(
                engine_id=engine_id,
                verification_level=VerificationLevel.UNVERIFIED,
                progress=progress,
                error=progress.last_error,
            )

        time.sleep(poll_interval)

    # Timed out
    with contextlib.suppress(Exception):
        adapter.remove_keep_data(engine_id)
    assert last_progress is not None
    return VerifyResult(
        engine_id=engine_id,
        verification_level=VerificationLevel.UNVERIFIED,
        progress=last_progress,
        error="verification timed out",
    )
