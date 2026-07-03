"""SettingsRepository — typed access to the settings key/value table."""
from __future__ import annotations

import sqlite3

_DEFAULTS: dict[str, str] = {
    "torrent_stall_timeout": "300",
    "torrent_download_limit": "0",
    "torrent_upload_limit": "0",
}


class SettingsRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, key: str) -> str:
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else _DEFAULTS.get(key, "")

    def get_int(self, key: str) -> int:
        try:
            return int(self.get(key))
        except ValueError:
            return int(_DEFAULTS.get(key, "0"))

    def set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )
        self._conn.commit()

    def get_all(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        result = dict(_DEFAULTS)
        result.update({row[0]: row[1] for row in rows})
        return result

    def update_many(self, updates: dict[str, str]) -> None:
        for key, value in updates.items():
            self._conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )
        self._conn.commit()
