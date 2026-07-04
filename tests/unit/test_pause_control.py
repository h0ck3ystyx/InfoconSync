"""wait_while_paused — the mechanism transfer runners use to react to a
pause/resume flip made by the Flask handlers, without the handlers ever
touching the torrent adapter directly (architecture invariant #3)."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import infocon_librarian.services.pause_control as pause_control
from infocon_librarian.services.pause_control import wait_while_paused
from infocon_librarian.storage.database import open_db
from infocon_librarian.storage.migrations import migrate
from infocon_librarian.storage.plan_repository import PlanRepository
from infocon_librarian.storage.repositories import ArchiveRootRepository


def _repo(tmp_path: Path) -> PlanRepository:
    conn = open_db(tmp_path / "test.db")
    migrate(conn)
    return PlanRepository(conn)


def _make_item(repo: PlanRepository, root_repo: ArchiveRootRepository, status: str) -> str:
    root = root_repo.upsert("/tmp/fake-archive-root", "fp")
    plan = repo.create_plan(root.id)
    item = repo.add_item(
        plan.id,
        method="https",
        status=status,
        destination_relpath="defcon/DEF CON 32",
        fallback_reason=None,
        collection_key="defcon/DEF CON 32",
        url="https://infocon.org/defcon/DEF%20CON%2032/",
    )
    return item.id


def test_returns_immediately_when_not_paused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    root_repo = ArchiveRootRepository(repo._conn)  # noqa: SLF001
    item_id = _make_item(repo, root_repo, "downloading")

    assert wait_while_paused(repo, item_id) == "downloading"


def test_blocks_until_status_changes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pause_control, "_PAUSE_POLL_INTERVAL", 0.05)
    repo = _repo(tmp_path)
    root_repo = ArchiveRootRepository(repo._conn)  # noqa: SLF001
    item_id = _make_item(repo, root_repo, "paused")

    result: dict[str, str] = {}

    def _waiter() -> None:
        result["status"] = wait_while_paused(repo, item_id)

    t = threading.Thread(target=_waiter, daemon=True)
    t.start()
    time.sleep(0.1)
    assert "status" not in result  # still blocked

    repo.update_item_status(item_id, "pending")
    t.join(timeout=2)
    assert result["status"] == "pending"


def test_missing_item_reports_failed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert wait_while_paused(repo, "does-not-exist") == "failed"
