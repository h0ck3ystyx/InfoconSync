"""FakeTorrentEngine — test double for TorrentAdapter.

Configure poll sequences and manifests before starting jobs.
Call advance(engine_id) from test code to drive state transitions.
"""
from __future__ import annotations

from dataclasses import dataclass

from infocon_librarian.domain.errors import InvalidTorrent
from infocon_librarian.domain.models import (
    EngineJobId,
    TorrentManifest,
    TorrentStartParams,
    TransferProgress,
    TransferState,
)


@dataclass
class _FakeJob:
    params: TorrentStartParams
    poll_sequence: list[TransferProgress]
    poll_index: int = 0
    paused: bool = False
    recheck_result: bool = True    # True = recheck passes
    resume_data: bytes = b"fake-resume"
    recheck_called: bool = False

    def current(self) -> TransferProgress:
        idx = min(self.poll_index, len(self.poll_sequence) - 1)
        return self.poll_sequence[idx]

    def advance(self) -> None:
        if self.poll_index < len(self.poll_sequence) - 1:
            self.poll_index += 1


class FakeTorrentEngine:
    """Scriptable TorrentAdapter for unit and integration tests."""

    def __init__(self) -> None:
        self._jobs: dict[str, _FakeJob] = {}
        self._manifest: TorrentManifest | None = None
        self._inspect_raises: Exception | None = None
        self.started_params: list[TorrentStartParams] = []

    # ------------------------------------------------------------------
    # Test configuration helpers
    # ------------------------------------------------------------------

    def configure_manifest(self, manifest: TorrentManifest) -> None:
        self._manifest = manifest

    def configure_inspect_error(self, exc: Exception) -> None:
        self._inspect_raises = exc

    def configure_job_sequence(
        self,
        engine_id: EngineJobId,
        sequence: list[TransferProgress],
        *,
        recheck_result: bool = True,
    ) -> None:
        self._jobs[engine_id.value].poll_sequence = list(sequence)
        self._jobs[engine_id.value].poll_index = 0
        self._jobs[engine_id.value].recheck_result = recheck_result

    def advance(self, engine_id: EngineJobId) -> None:
        self._jobs[engine_id.value].advance()

    def current_state(self, engine_id: EngineJobId) -> TransferState:
        return self._jobs[engine_id.value].current().state

    def was_recheck_called(self, engine_id: EngineJobId) -> bool:
        return self._jobs[engine_id.value].recheck_called

    # ------------------------------------------------------------------
    # TorrentAdapter protocol implementation
    # ------------------------------------------------------------------

    def inspect(self, torrent_bytes: bytes, *, url: str = "") -> TorrentManifest:
        if self._inspect_raises is not None:
            raise self._inspect_raises
        if self._manifest is not None:
            return self._manifest
        raise InvalidTorrent("FakeTorrentEngine: no manifest configured")

    def start(self, params: TorrentStartParams) -> EngineJobId:
        self.started_params.append(params)
        job_id = EngineJobId()
        initial = TransferProgress(
            job_id=job_id,
            state=TransferState.CHECKING,
            total_bytes=1024,
            downloaded_bytes=0,
            uploaded_bytes=0,
            download_rate=0,
            upload_rate=0,
            num_peers=0,
            last_error=None,
        )
        self._jobs[job_id.value] = _FakeJob(params=params, poll_sequence=[initial])
        return job_id

    def pause(self, job_id: EngineJobId) -> None:
        self._jobs[job_id.value].paused = True

    def resume(self, job_id: EngineJobId) -> None:
        self._jobs[job_id.value].paused = False

    def remove_keep_data(self, job_id: EngineJobId) -> None:
        self._jobs.pop(job_id.value, None)

    def recheck(self, job_id: EngineJobId) -> None:
        job = self._jobs[job_id.value]
        job.recheck_called = True
        last = job.current()
        checking = TransferProgress(
            job_id=job_id,
            state=TransferState.CHECKING,
            total_bytes=last.total_bytes,
            downloaded_bytes=last.downloaded_bytes,
            uploaded_bytes=0,
            download_rate=0,
            upload_rate=0,
            num_peers=last.num_peers,
            last_error=None,
        )
        if job.recheck_result:
            after = TransferProgress(
                job_id=job_id,
                state=TransferState.COMPLETE,
                total_bytes=last.total_bytes,
                downloaded_bytes=last.total_bytes,
                uploaded_bytes=0,
                download_rate=0,
                upload_rate=0,
                num_peers=last.num_peers,
                last_error=None,
            )
        else:
            after = TransferProgress(
                job_id=job_id,
                state=TransferState.FAILED,
                total_bytes=last.total_bytes,
                downloaded_bytes=0,
                uploaded_bytes=0,
                download_rate=0,
                upload_rate=0,
                num_peers=0,
                last_error="recheck failed: corrupt pieces",
            )
        job.poll_sequence.extend([checking, after])

    def poll(self, job_id: EngineJobId) -> TransferProgress:
        return self._jobs[job_id.value].current()

    def save_resume_data(self, job_id: EngineJobId) -> bytes:
        return self._jobs[job_id.value].resume_data
