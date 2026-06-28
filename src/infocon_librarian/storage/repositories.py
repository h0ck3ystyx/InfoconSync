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

    def get_latest_completed(self, archive_root_id: str) -> CheckRecord | None:
        """Return the most recently completed check for this archive root."""
        row = self._conn.execute(
            "SELECT id, archive_root_id, section, state, started_at, "
            "completed_at, error, result_json FROM checks "
            "WHERE archive_root_id=? AND state='complete' "
            "ORDER BY completed_at DESC LIMIT 1",
            (archive_root_id,),
        ).fetchone()
        if row is None:
            return None
        return CheckRecord(**dict(row))


# ---------------------------------------------------------------------------
# Verifications
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationRecord:
    id: str
    collection_key: str
    archive_root_id: str | None
    level: str           # piece_verified | unverified
    torrent_url: str | None
    verified_at: str
    error: str | None


class VerificationRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create(
        self,
        verify_id: str,
        *,
        collection_key: str,
        archive_root_id: str | None,
        level: str,
        torrent_url: str | None = None,
        error: str | None = None,
    ) -> VerificationRecord:
        now = _now()
        self._conn.execute(
            "INSERT INTO verifications "
            "(id, collection_key, archive_root_id, level, torrent_url, verified_at, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (verify_id, collection_key, archive_root_id, level, torrent_url, now, error),
        )
        self._conn.commit()
        return VerificationRecord(
            id=verify_id,
            collection_key=collection_key,
            archive_root_id=archive_root_id,
            level=level,
            torrent_url=torrent_url,
            verified_at=now,
            error=error,
        )

    def get_latest(self, collection_key: str, archive_root_id: str) -> VerificationRecord | None:
        """Return the most recent verification for this collection."""
        row = self._conn.execute(
            "SELECT id, collection_key, archive_root_id, level, torrent_url, verified_at, error "
            "FROM verifications WHERE collection_key=? AND archive_root_id=? "
            "ORDER BY verified_at DESC LIMIT 1",
            (collection_key, archive_root_id),
        ).fetchone()
        if row is None:
            return None
        return VerificationRecord(**dict(row))

    def get_verified_keys(self, archive_root_id: str) -> set[str]:
        """Return collection keys whose most recent verification passed (any level)."""
        rows = self._conn.execute(
            "SELECT collection_key, level FROM verifications "
            "WHERE archive_root_id=? ORDER BY verified_at DESC",
            (archive_root_id,),
        ).fetchall()
        seen: set[str] = set()
        verified: set[str] = set()
        _VERIFIED_LEVELS = {"piece_verified", "manifest_verified"}
        for key, level in rows:
            if key not in seen:
                seen.add(key)
                if level in _VERIFIED_LEVELS:
                    verified.add(key)
        return verified

    def get_all_latest_levels(self, archive_root_id: str) -> dict[str, str]:
        """Return {collection_key: level} for the most recent verification of each key."""
        rows = self._conn.execute(
            "SELECT collection_key, level FROM verifications "
            "WHERE archive_root_id=? ORDER BY verified_at DESC",
            (archive_root_id,),
        ).fetchall()
        result: dict[str, str] = {}
        for key, level in rows:
            if key not in result:
                result[key] = level
        return result

    def get_all_latest(self, archive_root_id: str) -> dict[str, "VerificationRecord"]:
        """Return {collection_key: record} for the most recent verification of each key."""
        rows = self._conn.execute(
            "SELECT * FROM verifications WHERE archive_root_id=? ORDER BY verified_at DESC",
            (archive_root_id,),
        ).fetchall()
        result: dict[str, VerificationRecord] = {}
        for row in rows:
            rec = VerificationRecord(**dict(row))
            if rec.collection_key not in result:
                result[rec.collection_key] = rec
        return result
