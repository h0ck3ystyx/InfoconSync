"""One-process service lifecycle and NAS identity guard.

Desktop launches have no active runtime; their behavior is unchanged.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class ServiceInterrupted(RuntimeError):
    """Work must stop; partial data is retained for an explicit retry."""


def _unescape_mount(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)


def require_local_state(path: Path) -> None:
    """SQLite WAL metadata must not live on a remote filesystem."""
    resolved = path.resolve()
    matches = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        mount = Path(_unescape_mount(before.split()[4]))
        if resolved.is_relative_to(mount):
            matches.append((len(mount.parts), after.split()[0]))
    if not matches:
        raise ValueError("Cannot determine the state directory filesystem")
    filesystem = max(matches)[1]
    if filesystem in {"nfs", "nfs4", "cifs", "smb3", "9p", "fuse.sshfs"}:
        raise ValueError("state_dir must use a local filesystem; SQLite WAL cannot use NAS storage")


class ArchiveGuard:
    def __init__(self, root: Path, mount_path: Path, mount_source: str, archive_id: str):
        self.root = root.resolve()
        self.mount_path = mount_path.resolve()
        self.mount_source = mount_source
        self.archive_id = archive_id
        if not self.root.is_relative_to(self.mount_path):
            raise ValueError("archive_root must be inside mount_path")

    def check_mount(self) -> None:
        # Reading mountinfo does not touch a possibly hung network filesystem.
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            before, after = line.split(" - ", 1)
            fields, filesystem = before.split(), after.split()
            if (
                _unescape_mount(fields[4]) == str(self.mount_path)
                and _unescape_mount(filesystem[1]) == self.mount_source
            ):
                return
        raise ServiceInterrupted("Expected NAS mount/source is unavailable")

    def check(self) -> None:
        self.check_mount()
        marker = self.root / ".infocon-archive-id"
        if marker.read_text().strip() != self.archive_id:
            raise ServiceInterrupted("NAS archive identity does not match configuration")


class ServiceRuntime:
    def __init__(self, guard: ArchiveGuard, adapter: Any = None):
        self.guard = guard
        self.adapter = adapter
        self.stopped = threading.Event()
        self.reason = ""
        self._lock = threading.RLock()
        self._workers: list[threading.Thread] = []
        self._last_probe = 0.0

    def interrupt(self, reason: str) -> None:
        with self._lock:
            if self.stopped.is_set():
                return
            self.reason = reason
            self.stopped.set()
        log.warning("Service work stopped: %s", reason)
        if self.adapter is not None:
            # Pause the entire engine promptly even if a runner is fetching metadata.
            try:
                self.adapter.close()
            except Exception:
                log.exception("Could not pause the torrent session during interruption")

    def checkpoint(self, *, force: bool = False) -> None:
        if self.stopped.is_set():
            raise ServiceInterrupted(self.reason)
        # Do not hold a lifecycle lock across NAS I/O: an unresponsive share
        # must not prevent SIGTERM from requesting shutdown.
        now = time.monotonic()
        with self._lock:
            probe = force or now - self._last_probe >= 1.0
            if probe:
                self._last_probe = now
        if probe:
            try:
                self.guard.check()
            except (OSError, UnicodeError, ServiceInterrupted) as exc:
                self.interrupt(str(exc))
                raise ServiceInterrupted(str(exc)) from exc
        if self.stopped.is_set():
            raise ServiceInterrupted(self.reason)

    def start(self, thread: threading.Thread) -> None:
        with self._lock:
            if self.stopped.is_set():
                raise ServiceInterrupted(self.reason)
            self._workers = [t for t in self._workers if t.is_alive()]
            self._workers.append(thread)
            thread.start()

    def drain(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._lock:
            workers = list(self._workers)
        for thread in workers:
            thread.join(max(0, deadline - time.monotonic()))
        return not any(t.is_alive() for t in workers)


_active: ServiceRuntime | None = None


def activate(runtime: ServiceRuntime | None) -> None:
    global _active
    _active = runtime


def checkpoint() -> None:
    if _active is not None:
        _active.checkpoint()


def interruptible_sleep(seconds: float) -> None:
    if _active is None:
        time.sleep(seconds)
    else:
        _active.stopped.wait(seconds)
        checkpoint()


def start_worker(thread: threading.Thread) -> None:
    if _active is None:
        thread.start()
    else:
        _active.start(thread)
