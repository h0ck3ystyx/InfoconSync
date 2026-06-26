"""libtorrent 2.0.x implementation of TorrentAdapter."""
from __future__ import annotations

import threading
import time

import libtorrent as lt  # type: ignore[import]

from infocon_librarian.domain.errors import InvalidTorrent, TransferError
from infocon_librarian.domain.models import (
    EngineJobId,
    TorrentFile,
    TorrentManifest,
    TorrentProtocol,
    TorrentStartParams,
    TransferProgress,
    TransferState,
)

# File priority constants
_SKIP = 0
_NORMAL = 1


def _make_privacy_session(
    *,
    listen_interfaces: str = "127.0.0.1:0",
    download_limit: int = 0,
    upload_limit: int = 0,
) -> lt.session:
    """Create a session with all privacy-sensitive features disabled."""
    settings: dict = {
        "enable_dht": False,
        "enable_lsd": False,
        "enable_natpmp": False,
        "enable_upnp": False,
        "listen_interfaces": listen_interfaces,
        "download_rate_limit": download_limit,
        "upload_rate_limit": upload_limit,
        # Reduce noise in alerts
        "alert_mask": (
            lt.alert_category.status
            | lt.alert_category.storage
            | lt.alert_category.error
            | lt.alert_category.piece_progress
        ),
    }
    return lt.session(settings)


def _per_torrent_privacy_flags() -> lt.add_torrent_params_flags_t:
    """Return flags that disable per-torrent DHT, PEX, and LSD."""
    return (
        lt.torrent_flags.disable_dht
        | lt.torrent_flags.disable_pex
        | lt.torrent_flags.disable_lsd
    )


def _parse_manifest(ti: lt.torrent_info, url: str) -> TorrentManifest:
    """Convert a torrent_info object to a TorrentManifest."""
    ih = ti.info_hashes()
    if ih.has_v1() and ih.has_v2():
        protocol = TorrentProtocol.HYBRID
        v1 = str(ih.v1)
        v2 = str(ih.v2)
    elif ih.has_v2():
        protocol = TorrentProtocol.V2
        v1 = None
        v2 = str(ih.v2)
    else:
        protocol = TorrentProtocol.V1
        v1 = str(ih.v1)
        v2 = None

    fs = ti.files()
    files = tuple(
        TorrentFile(
            index=i,
            relative_path=fs.file_path(i).replace("\\", "/"),
            size=fs.file_size(i),
        )
        for i in range(fs.num_files())
        if not (fs.file_flags(i) & fs.flag_pad_file)  # skip padding files
    )

    trackers = tuple(t.url for t in ti.trackers())
    total = sum(f.size for f in files)

    return TorrentManifest(
        url=url,
        protocol=protocol,
        v1_infohash=v1,
        v2_infohash=v2,
        files=files,
        trackers=trackers,
        total_size=total,
        name=ti.name(),
    )


def _state_to_transfer_state(ts: lt.torrent_status) -> TransferState:
    s = ts.state
    if ts.paused:
        return TransferState.PAUSED
    state_map = {
        lt.torrent_status.checking_files: TransferState.CHECKING,
        lt.torrent_status.downloading_metadata: TransferState.DOWNLOADING,
        lt.torrent_status.downloading: TransferState.DOWNLOADING,
        lt.torrent_status.finished: TransferState.COMPLETE,
        lt.torrent_status.seeding: TransferState.COMPLETE,
        lt.torrent_status.allocating: TransferState.QUEUED,
        lt.torrent_status.checking_resume_data: TransferState.CHECKING,
    }
    return state_map.get(s, TransferState.QUEUED)


class LibtorrentAdapter:
    """Single-session libtorrent adapter.

    This object must be owned by a single thread (TransferManager).
    All public methods are not thread-safe and must be called from
    the owning thread only.
    """

    def __init__(
        self,
        *,
        listen_interfaces: str = "127.0.0.1:0",
        download_limit: int = 0,
        upload_limit: int = 0,
    ) -> None:
        self._session = _make_privacy_session(
            listen_interfaces=listen_interfaces,
            download_limit=download_limit,
            upload_limit=upload_limit,
        )
        self._handles: dict[str, lt.torrent_handle] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Stateless inspection — no session required, no network activity
    # ------------------------------------------------------------------

    def inspect(self, torrent_bytes: bytes, *, url: str = "") -> TorrentManifest:
        """Parse torrent metainfo without touching the network.

        Raises InvalidTorrent for malformed, v2-only-unsupported, or
        path-unsafe metainfo.
        """
        try:
            ti = lt.torrent_info(torrent_bytes)
        except Exception as exc:
            raise InvalidTorrent(f"Failed to parse torrent: {exc}") from exc

        if not ti.is_valid():
            raise InvalidTorrent("torrent_info reports invalid after parsing")

        manifest = _parse_manifest(ti, url)
        _validate_manifest_paths(manifest)
        return manifest

    # ------------------------------------------------------------------
    # Session operations
    # ------------------------------------------------------------------

    def start(self, params: TorrentStartParams) -> EngineJobId:
        """Add a torrent to the session and begin downloading."""
        try:
            ti = lt.torrent_info(params.torrent_bytes)
        except Exception as exc:
            raise InvalidTorrent(f"Failed to parse torrent on start: {exc}") from exc

        atp = lt.add_torrent_params()
        atp.ti = ti
        atp.save_path = params.save_path

        # Privacy flags: disable DHT, PEX, LSD per-torrent
        atp.flags = _per_torrent_privacy_flags()
        # Start paused so we can recheck before requesting peers
        atp.flags |= lt.torrent_flags.paused
        atp.flags &= ~lt.torrent_flags.auto_managed

        # Apply file selection priorities
        if params.selected_indices:
            priorities = [_SKIP] * ti.num_files()
            for idx in params.selected_indices:
                if 0 <= idx < ti.num_files():
                    priorities[idx] = _NORMAL
            atp.file_priorities = priorities

        # Restore resume data if available
        if params.resume_data:
            try:
                resume_params = lt.read_resume_data(params.resume_data)
                atp = resume_params
                atp.ti = ti
                atp.save_path = params.save_path
                atp.flags = _per_torrent_privacy_flags() | lt.torrent_flags.paused
                atp.flags &= ~lt.torrent_flags.auto_managed
                if params.selected_indices:
                    priorities = [_SKIP] * ti.num_files()
                    for idx in params.selected_indices:
                        if 0 <= idx < ti.num_files():
                            priorities[idx] = _NORMAL
                    atp.file_priorities = priorities
            except Exception:
                # If resume data is corrupt, start fresh
                atp = lt.add_torrent_params()
                atp.ti = ti
                atp.save_path = params.save_path
                atp.flags = _per_torrent_privacy_flags() | lt.torrent_flags.paused
                atp.flags &= ~lt.torrent_flags.auto_managed

        handle = self._session.add_torrent(atp)

        # Run existing-data recheck before we allow downloading
        handle.force_recheck()
        handle.resume()

        job_id = EngineJobId()
        with self._lock:
            self._handles[job_id.value] = handle
        return job_id

    def pause(self, job_id: EngineJobId) -> None:
        handle = self._get_handle(job_id)
        handle.pause()

    def resume(self, job_id: EngineJobId) -> None:
        handle = self._get_handle(job_id)
        handle.resume()

    def remove_keep_data(self, job_id: EngineJobId) -> None:
        handle = self._get_handle(job_id)
        self._session.remove_torrent(handle)
        with self._lock:
            del self._handles[job_id.value]

    def recheck(self, job_id: EngineJobId) -> None:
        handle = self._get_handle(job_id)
        handle.force_recheck()

    def poll(self, job_id: EngineJobId) -> TransferProgress:
        handle = self._get_handle(job_id)
        ts = handle.status()
        state = _state_to_transfer_state(ts)
        error = ts.errc.message() if ts.errc else None
        return TransferProgress(
            job_id=job_id,
            state=state,
            total_bytes=ts.total_wanted,
            downloaded_bytes=ts.total_wanted_done,
            uploaded_bytes=ts.total_upload,
            download_rate=ts.download_rate,
            upload_rate=ts.upload_rate,
            num_peers=ts.num_peers,
            last_error=error if error and error != "Success" else None,
        )

    def save_resume_data(self, job_id: EngineJobId) -> bytes:
        """Serialize resume state; blocks until the alert arrives."""
        handle = self._get_handle(job_id)
        handle.save_resume_data(lt.save_resume_flags_t.flush_disk_cache)

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            alerts = self._session.pop_alerts()
            for alert in alerts:
                if isinstance(alert, lt.save_resume_data_alert) and alert.handle == handle:
                    return lt.write_resume_data_buf(alert.params)
                if (
                    isinstance(alert, lt.save_resume_data_failed_alert)
                    and alert.handle == handle
                ):
                    raise TransferError(f"save_resume_data failed: {alert.message()}")
            time.sleep(0.05)
        raise TransferError("Timed out waiting for save_resume_data alert")

    def pop_alerts(self) -> list:
        return self._session.pop_alerts()

    def get_settings(self) -> dict:
        return self._session.get_settings()

    def close(self) -> None:
        """Gracefully stop the session."""
        self._session.pause()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_handle(self, job_id: EngineJobId) -> lt.torrent_handle:
        with self._lock:
            handle = self._handles.get(job_id.value)
        if handle is None or not handle.is_valid():
            raise TransferError(f"No valid handle for job {job_id.value!r}")
        return handle


def _validate_manifest_paths(manifest: TorrentManifest) -> None:
    """Raise InvalidTorrent if any file path is unsafe."""
    import os.path

    for tf in manifest.files:
        p = tf.relative_path
        if os.path.isabs(p):
            raise InvalidTorrent(f"Torrent contains absolute path: {p!r}")
        norm = os.path.normpath(p)
        parts = norm.replace("\\", "/").split("/")
        for part in parts:
            if part in (".", ".."):
                raise InvalidTorrent(f"Torrent path contains traversal component: {p!r}")
            if not part:
                raise InvalidTorrent(f"Torrent path contains empty component: {p!r}")
        # Reject platform-reserved names (Windows NUL, CON, etc.)
        for part in parts:
            upper = part.upper().split(".")[0]
            if upper in {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4",
                         "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2",
                         "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"}:
                raise InvalidTorrent(f"Torrent path contains reserved name: {part!r}")
        if "\x00" in p:
            raise InvalidTorrent(f"Torrent path contains NUL byte: {p!r}")
