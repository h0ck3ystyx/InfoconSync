"""Append-only SQLite migration runner.

Each migration is a (version, sql) pair. Migrations run in order from the
current schema version to the latest. Migrations are never modified after
they are committed — add new ones at the end.
"""
from __future__ import annotations

import sqlite3

_MIGRATIONS: list[tuple[int, str]] = [
    # ------------------------------------------------------------------
    # Version 1 — initial schema
    # ------------------------------------------------------------------
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS archive_roots (
            id                TEXT PRIMARY KEY,
            canonical_path    TEXT NOT NULL UNIQUE,
            volume_fingerprint TEXT NOT NULL,
            last_seen_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            id               TEXT PRIMARY KEY,
            archive_root_id  TEXT NOT NULL REFERENCES archive_roots(id),
            created_at       TEXT NOT NULL,
            kind             TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS snapshot_entries (
            snapshot_id      TEXT NOT NULL REFERENCES snapshots(id),
            relative_path    TEXT NOT NULL,
            size             INTEGER NOT NULL,
            mtime_ns         INTEGER NOT NULL,
            PRIMARY KEY (snapshot_id, relative_path)
        );

        CREATE TABLE IF NOT EXISTS remote_fetches (
            url              TEXT PRIMARY KEY,
            fetched_at       TEXT NOT NULL,
            etag             TEXT,
            last_modified    TEXT,
            body_hash        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS remote_entries (
            id               TEXT PRIMARY KEY,
            url              TEXT NOT NULL,
            parent_url       TEXT NOT NULL,
            kind             TEXT NOT NULL,
            size_hint        INTEGER,
            modified_hint    TEXT
        );

        CREATE TABLE IF NOT EXISTS torrent_manifests (
            id               TEXT PRIMARY KEY,
            url              TEXT NOT NULL,
            raw_path         TEXT NOT NULL,
            v1_infohash      TEXT,
            v2_infohash      TEXT,
            metadata_hash    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS torrent_files (
            manifest_id      TEXT NOT NULL REFERENCES torrent_manifests(id),
            idx              INTEGER NOT NULL,
            relative_path    TEXT NOT NULL,
            size             INTEGER NOT NULL,
            PRIMARY KEY (manifest_id, idx)
        );

        CREATE TABLE IF NOT EXISTS evidence (
            id               TEXT PRIMARY KEY,
            collection_key   TEXT NOT NULL,
            kind             TEXT NOT NULL,
            payload_json     TEXT NOT NULL,
            observed_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS plans (
            id               TEXT PRIMARY KEY,
            archive_root_id  TEXT NOT NULL REFERENCES archive_roots(id),
            state            TEXT NOT NULL,
            created_at       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS plan_items (
            id                  TEXT PRIMARY KEY,
            plan_id             TEXT NOT NULL REFERENCES plans(id),
            method              TEXT NOT NULL,
            status              TEXT NOT NULL,
            destination_relpath TEXT NOT NULL,
            fallback_reason     TEXT
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id               TEXT PRIMARY KEY,
            plan_item_id     TEXT NOT NULL REFERENCES plan_items(id),
            state            TEXT NOT NULL,
            resume_ref       TEXT,
            last_error       TEXT
        );

        CREATE TABLE IF NOT EXISTS receipts (
            id               TEXT PRIMARY KEY,
            plan_id          TEXT NOT NULL REFERENCES plans(id),
            json_path        TEXT NOT NULL,
            completed_at     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS schema_version (
            version          INTEGER PRIMARY KEY
        );
        """,
    ),
    # ------------------------------------------------------------------
    # Version 2 — extend plan_items with URL/size/collection metadata
    # ------------------------------------------------------------------
    (
        2,
        """
        ALTER TABLE plan_items ADD COLUMN collection_key TEXT;
        ALTER TABLE plan_items ADD COLUMN url TEXT;
        ALTER TABLE plan_items ADD COLUMN torrent_url TEXT;
        ALTER TABLE plan_items ADD COLUMN size_bytes INTEGER;
        """,
    ),
    # ------------------------------------------------------------------
    # Version 3 — upstream check runs and results
    # ------------------------------------------------------------------
    (
        3,
        """
        CREATE TABLE IF NOT EXISTS checks (
            id               TEXT PRIMARY KEY,
            archive_root_id  TEXT REFERENCES archive_roots(id),
            section          TEXT,
            state            TEXT NOT NULL,
            started_at       TEXT NOT NULL,
            completed_at     TEXT,
            error            TEXT,
            result_json      TEXT
        );
        """,
    ),
]

_LATEST_VERSION = max(v for v, _ in _MIGRATIONS)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply all pending migrations. Returns the new schema version."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
    )
    conn.commit()

    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row[0] is not None else 0

    for version, sql in _MIGRATIONS:
        if version > current:
            conn.executescript(sql)
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (version,)
            )
            conn.commit()
            current = version

    return current


def current_version(conn: sqlite3.Connection) -> int:
    """Return the current schema version (0 if never migrated)."""
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return row[0] if row[0] is not None else 0
    except sqlite3.OperationalError:
        return 0


def latest_version() -> int:
    return _LATEST_VERSION
