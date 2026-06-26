"""Drive (removable media) disconnect and remount detection.

The monitor polls the archive root's volume fingerprint on a short interval.
When the fingerprint changes or the root becomes unreachable it calls the
registered disconnect callback. When the root reappears with the SAME
fingerprint it calls the reconnect callback; with a DIFFERENT fingerprint
it calls the wrong_volume callback so the user can confirm before resuming.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from infocon_librarian.archive.root import ArchiveRootInfo, validate_root

log = logging.getLogger(__name__)

_PROBE_FILENAME = ".librarian-probe"


class DriveMonitor:
    """Polls archive root availability and volume identity.

    Callbacks are called from the monitor thread — keep them short.
    """

    def __init__(
        self,
        root_info: ArchiveRootInfo,
        *,
        poll_interval: float = 5.0,
        on_disconnect: Callable[[], None] | None = None,
        on_reconnect: Callable[[ArchiveRootInfo], None] | None = None,
        on_wrong_volume: Callable[[ArchiveRootInfo], None] | None = None,
    ) -> None:
        self._root_info = root_info
        self._poll_interval = poll_interval
        self._on_disconnect = on_disconnect
        self._on_reconnect = on_reconnect
        self._on_wrong_volume = on_wrong_volume

        self._connected = True
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="drive-monitor"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._poll_interval + 1.0)
            self._thread = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.wait(timeout=self._poll_interval):
            self._poll()

    def _poll(self) -> None:
        root_path = Path(self._root_info.canonical_path)
        try:
            new_info = validate_root(root_path)
        except Exception:
            if self._connected:
                self._connected = False
                log.warning("Archive root unavailable: %s", root_path)
                if self._on_disconnect:
                    self._on_disconnect()
            return

        if not self._connected:
            # Root came back — check volume identity
            if new_info.volume_fingerprint == self._root_info.volume_fingerprint:
                self._connected = True
                log.info("Archive root reconnected: %s", root_path)
                if self._on_reconnect:
                    self._on_reconnect(new_info)
            else:
                # Different volume mounted at same path
                log.warning(
                    "Wrong volume at %s (expected %s, got %s)",
                    root_path,
                    self._root_info.volume_fingerprint,
                    new_info.volume_fingerprint,
                )
                if self._on_wrong_volume:
                    self._on_wrong_volume(new_info)


def probe_writable(root: Path) -> bool:
    """Return True if the archive root is currently writable."""
    probe = root / _PROBE_FILENAME
    try:
        probe.write_bytes(b"probe")
        probe.unlink()
        return True
    except OSError:
        return False
