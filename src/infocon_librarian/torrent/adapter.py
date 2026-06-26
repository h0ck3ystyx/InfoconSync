"""Protocol that all torrent engine implementations must satisfy."""
from __future__ import annotations

from typing import Protocol

from infocon_librarian.domain.models import (
    EngineJobId,
    TorrentManifest,
    TorrentStartParams,
    TransferProgress,
)


class TorrentAdapter(Protocol):
    """Narrow product-level interface over a BitTorrent engine.

    Implementations must never be called from Flask request threads.
    All calls go through TransferManager.
    """

    def inspect(self, torrent_bytes: bytes) -> TorrentManifest:
        """Parse metainfo and return files, sizes, trackers, and infohashes.

        Must not join a swarm, open network sockets, or create a session.
        Raises InvalidTorrent for malformed, unsupported, or unsafe metainfo.
        """
        ...

    def start(self, params: TorrentStartParams) -> EngineJobId:
        """Add a torrent and start downloading selected files.

        Verifies existing data before requesting peers.
        Returns a stable job ID for subsequent operations.
        """
        ...

    def pause(self, job_id: EngineJobId) -> None:
        """Gracefully pause a running job, preserving valid data."""
        ...

    def resume(self, job_id: EngineJobId) -> None:
        """Resume a paused job."""
        ...

    def remove_keep_data(self, job_id: EngineJobId) -> None:
        """Remove job state from the engine while keeping downloaded files."""
        ...

    def recheck(self, job_id: EngineJobId) -> None:
        """Force a piece recheck of local data for this job."""
        ...

    def poll(self, job_id: EngineJobId) -> TransferProgress:
        """Return current progress snapshot for a job."""
        ...

    def save_resume_data(self, job_id: EngineJobId) -> bytes:
        """Serialize resume state to opaque bytes for durable storage."""
        ...
