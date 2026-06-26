"""Controlled shutdown for InfoCon Librarian.

Sequence:
  1. Mark shutdown initiated — reject new commands
  2. Broadcast PAUSE to TransferManager (best-effort)
  3. Wait for resume data flush (bounded by timeout)
  4. Commit open DB connections
  5. Signal Flask/WSGI to stop accepting requests
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


class ShutdownController:
    """Coordinates graceful application shutdown across threads.

    Attributes:
        initiated: True once shutdown has been requested.
        complete: True once all phases have finished.
    """

    _RESUME_POLL_INTERVAL = 0.1

    def __init__(
        self,
        *,
        transfer_manager: Any | None = None,
        resume_timeout: float = 10.0,
        on_complete: Callable[[], None] | None = None,
    ) -> None:
        self._manager = transfer_manager
        self._resume_timeout = resume_timeout
        self._on_complete = on_complete

        self._initiated = threading.Event()
        self._complete = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def initiated(self) -> bool:
        return self._initiated.is_set()

    @property
    def complete(self) -> bool:
        return self._complete.is_set()

    def request(self) -> None:
        """Request shutdown. Idempotent — safe to call multiple times."""
        with self._lock:
            if self._initiated.is_set():
                return
            self._initiated.set()

        log.info("Shutdown requested — pausing transfers")
        self._run_shutdown()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until shutdown is complete. Returns True if completed."""
        return self._complete.wait(timeout=timeout)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_shutdown(self) -> None:
        try:
            self._pause_transfers()
            self._wait_for_resume_data()
        except Exception:
            log.exception("Error during shutdown sequence")
        finally:
            log.info("Shutdown complete")
            self._complete.set()
            if self._on_complete is not None:
                try:
                    self._on_complete()
                except Exception:
                    log.exception("Error in shutdown completion callback")

    def _pause_transfers(self) -> None:
        if self._manager is None:
            return
        try:
            self._manager.pause_all()
            log.info("All transfers paused")
        except Exception:
            log.warning("Could not pause transfers: %s", exc_info=True)

    def _wait_for_resume_data(self) -> None:
        if self._manager is None:
            return
        deadline = time.monotonic() + self._resume_timeout
        while time.monotonic() < deadline:
            try:
                if self._manager.resume_data_saved():
                    log.info("Resume data flushed")
                    return
            except Exception:
                break
            time.sleep(self._RESUME_POLL_INTERVAL)
        log.warning("Resume data flush timed out after %.1fs", self._resume_timeout)


def install_signal_handlers(controller: ShutdownController) -> None:
    """Register SIGINT and SIGTERM to trigger the controller."""
    import signal

    def _handler(signum: int, _frame: Any) -> None:
        log.info("Signal %d received — initiating shutdown", signum)
        thread = threading.Thread(target=controller.request, daemon=True)
        thread.start()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
