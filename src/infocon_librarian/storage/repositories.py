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


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckRecord:
    id: str
    archive_root_id: str | None
    section: str | None
    state: str           # running | complete | failed
    started_at: str
    completed_at: str | None
    error: str | None
    result_json: str | None  # JSON-serialised list[dict]


class CheckRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create(
        self,
        check_id: str,
        *,
        archive_root_id: str | None = None,
        section: str | None = None,
    ) -> CheckRecord:
        now = _now()
        self._conn.execute(
            "INSERT INTO checks (id, archive_root_id, section, state, started_at) "
            "VALUES (?, ?, ?, 'running', ?)",
            (check_id, archive_root_id, section, now),
        )
        self._conn.commit()
        return CheckRecord(
            id=check_id,
            archive_root_id=archive_root_id,
            section=section,
            state="running",
            started_at=now,
            completed_at=None,
            error=None,
            result_json=None,
        )

    def complete(self, check_id: str, result_json: str) -> None:
        now = _now()
        self._conn.execute(
            "UPDATE checks SET state='complete', completed_at=?, result_json=? WHERE id=?",
            (now, result_json, check_id),
        )
        self._conn.commit()

    def fail(self, check_id: str, error: str) -> None:
        now = _now()
        self._conn.execute(
            "UPDATE checks SET state='failed', completed_at=?, error=? WHERE id=?",
            (now, error, check_id),
        )
        self._conn.commit()

    def get(self, check_id: str) -> CheckRecord | None:
        row = self._conn.execute(
            "SELECT id, archive_root_id, section, state, started_at, "
            "completed_at, error, result_json FROM checks WHERE id=?",
            (check_id,),
        ).fetchone()
        if row is None:
            return None
        return CheckRecord(**dict(row))
