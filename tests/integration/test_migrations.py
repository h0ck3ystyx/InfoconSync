"""F-001, F-002 — SQLite migration harness."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from infocon_librarian.storage.database import open_db, verify_foreign_keys
from infocon_librarian.storage.migrations import (
    latest_version,
    migrate,
)
from infocon_librarian.storage.repositories import ArchiveRootRepository

# ---------------------------------------------------------------------------
# F-001: New database migration
# ---------------------------------------------------------------------------


def test_f001_new_database_migrates_to_latest(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "test.db")
    version = migrate(conn)

    assert version == latest_version()
    assert version > 0


def test_f001_foreign_keys_enabled(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "test.db")
    migrate(conn)

    assert verify_foreign_keys(conn) is True


def test_f001_all_tables_exist(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "test.db")
    migrate(conn)

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    required = {
        "archive_roots",
        "snapshots",
        "snapshot_entries",
        "remote_fetches",
        "remote_entries",
        "torrent_manifests",
        "torrent_files",
        "evidence",
        "plans",
        "plan_items",
        "jobs",
        "receipts",
        "schema_version",
    }
    assert required.issubset(tables), f"Missing tables: {required - tables}"


def test_f001_migration_idempotent(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "test.db")
    v1 = migrate(conn)
    v2 = migrate(conn)  # second call should be a no-op

    assert v1 == v2 == latest_version()


# ---------------------------------------------------------------------------
# F-002: Reopen upgraded database — data survives
# ---------------------------------------------------------------------------


def test_f002_data_survives_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "persist.db"

    # First open: migrate and insert data
    conn1 = open_db(db_path)
    migrate(conn1)
    repo1 = ArchiveRootRepository(conn1)
    record = repo1.upsert(
        canonical_path="/mnt/archive",
        volume_fingerprint="dev:1234",
    )
    conn1.close()

    # Second open: data must still be there
    conn2 = open_db(db_path)
    migrate(conn2)  # should be a no-op
    repo2 = ArchiveRootRepository(conn2)
    found = repo2.get_by_path("/mnt/archive")

    assert found is not None
    assert found.id == record.id
    assert found.volume_fingerprint == "dev:1234"
    conn2.close()


def test_f002_foreign_keys_enforced_after_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "persist.db"

    conn = open_db(db_path)
    migrate(conn)
    conn.close()

    conn2 = open_db(db_path)
    assert verify_foreign_keys(conn2) is True

    # Attempt to insert a snapshot with a non-existent archive_root_id
    with pytest.raises(sqlite3.IntegrityError):
        conn2.execute(
            "INSERT INTO snapshots (id, archive_root_id, created_at, kind) "
            "VALUES ('x', 'nonexistent', '2024-01-01T00:00:00Z', 'local')"
        )
        conn2.commit()
    conn2.close()


def test_f002_wal_mode_set(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "wal.db")
    migrate(conn)

    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    conn.close()
