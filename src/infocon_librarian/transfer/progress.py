"""CoalescingWriter — rate-limits DB writes for rapid progress updates."""
from __future__ import annotations

import time
from collections.abc import Callable


class CoalescingWriter:
    """Suppress redundant writes within a time window.

    Call update(key, value, writer_fn) on every tick.  writer_fn is invoked
    only when value changes OR the coalesce window has elapsed.
    """

    def __init__(self, window_seconds: float = 2.0) -> None:
        self._window = window_seconds
        self._last_value: dict[str, object] = {}
        self._last_write: dict[str, float] = {}
        self.write_count: int = 0

    def update(self, key: str, value: object, writer_fn: Callable[[], None]) -> bool:
        """Maybe call writer_fn.  Returns True if a write occurred."""
        now = time.monotonic()
        last = self._last_value.get(key)
        age = now - self._last_write.get(key, 0.0)

        if value != last or age >= self._window:
            writer_fn()
            self._last_value[key] = value
            self._last_write[key] = now
            self.write_count += 1
            return True
        return False
