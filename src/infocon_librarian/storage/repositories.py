"""Database repositories — typed access over raw SQLite rows."""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Archive roots
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchiveRootRecord:
    id: str
    canonical_path: str
    volume_fingerprint: str
    last_seen_at: str


class ArchiveRootRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(self, canonical_path: str, volume_fingerprint: str) -> ArchiveRootRecord:
        """Insert or update an archive root, returning the persisted record."""
        existing = self._conn.execute(
            "SELECT id, canonical_path, volume_fingerprint, last_seen_at "
            "FROM archive_roots WHERE canonical_path = ?",
            (canonical_path,),
        ).fetchone()

        now = _now()
        if existing:
            self._conn.execute(
                "UPDATE archive_roots SET volume_fingerprint=?, last_seen_at=? WHERE id=?",
                (volume_fingerprint, now, existing["id"]),
            )
            self._conn.commit()
            return ArchiveRootRecord(
                id=existing["id"],
                canonical_path=canonical_path,
                volume_fingerprint=volume_fingerprint,
                last_seen_at=now,
            )

        new_id = str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO archive_roots (id, canonical_path, volume_fingerprint, last_seen_at) "
            "VALUES (?, ?, ?, ?)",
            (new_id, canonical_path, volume_fingerprint, now),
        )
        self._conn.commit()
        return ArchiveRootRecord(
            id=new_id,
            canonical_path=canonical_path,
            volume_fingerprint=volume_fingerprint,
            last_seen_at=now,
        )

    def get_by_path(self, canonical_path: str) -> ArchiveRootRecord | None:
        row = self._conn.execute(
            "SELECT id, canonical_path, volume_fingerprint, last_seen_at "
            "FROM archive_roots WHERE canonical_path = ?",
            (canonical_path,),
        ).fetchone()
        if row is None:
            return None
        return ArchiveRootRecord(**dict(row))

    def list_all(self) -> list[ArchiveRootRecord]:
        rows = self._conn.execute(
            "SELECT id, canonical_path, volume_fingerprint, last_seen_at FROM archive_roots"
        ).fetchall()
        return [ArchiveRootRecord(**dict(r)) for r in rows]
