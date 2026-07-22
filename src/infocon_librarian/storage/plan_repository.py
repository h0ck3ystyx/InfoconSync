"""PlanRepository — typed access to plans, plan_items, and receipts tables."""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class PlanRecord:
    id: str
    archive_root_id: str
    state: str        # "draft" | "preflighted" | "running" | "complete" | "failed"
    created_at: str


@dataclass(frozen=True)
class PlanItemRecord:
    id: str
    plan_id: str
    method: str               # "torrent" | "https"
    status: str               # PlanItemStatus value
    destination_relpath: str
    fallback_reason: str | None
    collection_key: str | None
    url: str | None
    torrent_url: str | None
    size_bytes: int | None


@dataclass(frozen=True)
class ReceiptRecord:
    id: str
    plan_id: str
    json_path: str
    completed_at: str


class PlanRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create_plan(self, archive_root_id: str) -> PlanRecord:
        plan_id = str(uuid.uuid4())
        now = _now()
        self._conn.execute(
            "INSERT INTO plans (id, archive_root_id, state, created_at) VALUES (?, ?, ?, ?)",
            (plan_id, archive_root_id, "draft", now),
        )
        self._conn.commit()
        return PlanRecord(
            id=plan_id, archive_root_id=archive_root_id, state="draft", created_at=now
        )

    def get_plan(self, plan_id: str) -> PlanRecord | None:
        row = self._conn.execute(
            "SELECT id, archive_root_id, state, created_at FROM plans WHERE id = ?",
            (plan_id,),
        ).fetchone()
        if row is None:
            return None
        return PlanRecord(**dict(row))

    def list_plans(self) -> list[PlanRecord]:
        rows = self._conn.execute(
            "SELECT id, archive_root_id, state, created_at FROM plans ORDER BY created_at DESC"
        ).fetchall()
        return [PlanRecord(**dict(r)) for r in rows]

    def update_plan_state(self, plan_id: str, state: str) -> None:
        self._conn.execute(
            "UPDATE plans SET state = ? WHERE id = ?", (state, plan_id)
        )
        self._conn.commit()

    def requeue_incomplete_items(self, plan_id: str) -> int:
        """Flip a plan's failed/downloading items back to ``pending`` for a retry.

        Completed items keep their state (the downloader skips them on re-run),
        and explicit ``paused``/``blocked`` items are left alone. Returns the
        number of items requeued. Safe to call on an already-pending plan — it
        is then a no-op.
        """
        cur = self._conn.execute(
            """UPDATE plan_items SET status = 'pending'
               WHERE plan_id = ? AND status IN ('failed', 'downloading')""",
            (plan_id,),
        )
        self._conn.commit()
        return cur.rowcount

    def reset_orphaned_running_plans(self) -> int:
        """Recover plans left mid-transfer by a crash or kill.

        A plan is only ever in state ``running`` while a worker thread owns it.
        At process startup no such threads exist, so any ``running`` plan is
        orphaned: its worker died without finishing. Roll it back to
        ``preflighted`` and flip its in-flight (``downloading``) items back to
        ``pending`` so the plan can be restarted from the UI. Completed files are
        skipped on re-run, so this resumes rather than restarts. Terminal item
        states (complete/failed) and explicit ``paused`` items are left untouched.

        Returns the number of plans reset.
        """
        running = self._conn.execute(
            "SELECT id FROM plans WHERE state = 'running'"
        ).fetchall()
        if not running:
            return 0
        self._conn.execute(
            """UPDATE plan_items SET status = 'pending'
               WHERE status = 'downloading'
                 AND plan_id IN (SELECT id FROM plans WHERE state = 'running')"""
        )
        self._conn.execute(
            "UPDATE plans SET state = 'preflighted' WHERE state = 'running'"
        )
        self._conn.commit()
        return len(running)

    def delete_plan(self, plan_id: str) -> None:
        self._conn.execute("DELETE FROM plan_items WHERE plan_id = ?", (plan_id,))
        self._conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
        self._conn.commit()

    def add_item(
        self,
        plan_id: str,
        *,
        method: str,
        status: str,
        destination_relpath: str,
        fallback_reason: str | None = None,
        collection_key: str | None = None,
        url: str | None = None,
        torrent_url: str | None = None,
        size_bytes: int | None = None,
    ) -> PlanItemRecord:
        item_id = str(uuid.uuid4())
        self._conn.execute(
            """INSERT INTO plan_items
               (id, plan_id, method, status, destination_relpath, fallback_reason,
                collection_key, url, torrent_url, size_bytes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item_id, plan_id, method, status, destination_relpath,
                fallback_reason, collection_key, url, torrent_url, size_bytes,
            ),
        )
        self._conn.commit()
        return PlanItemRecord(
            id=item_id, plan_id=plan_id, method=method, status=status,
            destination_relpath=destination_relpath, fallback_reason=fallback_reason,
            collection_key=collection_key, url=url, torrent_url=torrent_url,
            size_bytes=size_bytes,
        )

    def get_item(self, item_id: str) -> PlanItemRecord | None:
        row = self._conn.execute(
            """SELECT id, plan_id, method, status, destination_relpath, fallback_reason,
                      collection_key, url, torrent_url, size_bytes
               FROM plan_items WHERE id = ?""",
            (item_id,),
        ).fetchone()
        if row is None:
            return None
        return PlanItemRecord(**dict(row))

    def list_items(self, plan_id: str) -> list[PlanItemRecord]:
        rows = self._conn.execute(
            """SELECT id, plan_id, method, status, destination_relpath, fallback_reason,
                      collection_key, url, torrent_url, size_bytes
               FROM plan_items WHERE plan_id = ?""",
            (plan_id,),
        ).fetchall()
        return [PlanItemRecord(**dict(r)) for r in rows]

    def update_item_status(self, item_id: str, status: str) -> None:
        self._conn.execute(
            "UPDATE plan_items SET status = ? WHERE id = ?", (status, item_id)
        )
        self._conn.commit()

    def update_item_to_https(self, item_id: str, https_url: str) -> None:
        """Switch a torrent item to HTTPS fallback, preserving the torrent URL."""
        self._conn.execute(
            """UPDATE plan_items
               SET method = 'https', url = ?, torrent_url = url,
                   fallback_reason = 'swarm_unreachable', status = 'pending'
               WHERE id = ?""",
            (https_url, item_id),
        )
        self._conn.commit()

    def add_receipt(self, plan_id: str, json_path: str) -> ReceiptRecord:
        rec_id = str(uuid.uuid4())
        now = _now()
        self._conn.execute(
            "INSERT INTO receipts (id, plan_id, json_path, completed_at) VALUES (?, ?, ?, ?)",
            (rec_id, plan_id, json_path, now),
        )
        self._conn.commit()
        return ReceiptRecord(id=rec_id, plan_id=plan_id, json_path=json_path, completed_at=now)

    def get_receipt(self, receipt_id: str) -> ReceiptRecord | None:
        row = self._conn.execute(
            "SELECT id, plan_id, json_path, completed_at FROM receipts WHERE id = ?",
            (receipt_id,),
        ).fetchone()
        if row is None:
            return None
        return ReceiptRecord(**dict(row))

    def list_receipts(self) -> list[ReceiptRecord]:
        rows = self._conn.execute(
            "SELECT id, plan_id, json_path, completed_at FROM receipts ORDER BY completed_at DESC"
        ).fetchall()
        return [ReceiptRecord(**dict(r)) for r in rows]
