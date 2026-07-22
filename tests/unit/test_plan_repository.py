"""PlanRepository behaviours not covered elsewhere — orphaned-plan recovery."""
from __future__ import annotations

from pathlib import Path

from infocon_librarian.storage.database import open_db
from infocon_librarian.storage.migrations import migrate
from infocon_librarian.storage.plan_repository import PlanRepository
from infocon_librarian.storage.repositories import ArchiveRootRepository


def _repo(tmp_path: Path) -> PlanRepository:
    conn = open_db(tmp_path / "plans.db")
    migrate(conn)
    ArchiveRootRepository(conn).upsert("/mnt/archive", "dev:1234")
    return PlanRepository(conn)


def _root_id(repo: PlanRepository) -> str:
    row = repo._conn.execute("SELECT id FROM archive_roots LIMIT 1").fetchone()
    return row[0]


def test_reset_orphaned_running_plan_becomes_restartable(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plan = repo.create_plan(_root_id(repo))
    item = repo.add_item(
        plan.id, method="https", status="downloading",
        destination_relpath="cons/CyberChess", url="https://infocon.org/cons/CyberChess/",
    )
    repo.update_plan_state(plan.id, "running")

    count = repo.reset_orphaned_running_plans()

    assert count == 1
    # Plan is restartable, item is queued again.
    assert repo.get_plan(plan.id).state == "preflighted"
    assert repo.get_item(item.id).status == "pending"


def test_reset_leaves_terminal_and_paused_states_untouched(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plan = repo.create_plan(_root_id(repo))
    done = repo.add_item(plan.id, method="https", status="complete",
                         destination_relpath="a")
    failed = repo.add_item(plan.id, method="https", status="failed",
                           destination_relpath="b")
    paused = repo.add_item(plan.id, method="https", status="paused",
                           destination_relpath="c")
    active = repo.add_item(plan.id, method="https", status="downloading",
                           destination_relpath="d")
    repo.update_plan_state(plan.id, "running")

    repo.reset_orphaned_running_plans()

    assert repo.get_item(done.id).status == "complete"
    assert repo.get_item(failed.id).status == "failed"
    assert repo.get_item(paused.id).status == "paused"
    assert repo.get_item(active.id).status == "pending"  # only this one flips


def test_requeue_incomplete_items_flips_failed_and_downloading(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plan = repo.create_plan(_root_id(repo))
    failed = repo.add_item(plan.id, method="https", status="failed", destination_relpath="a")
    active = repo.add_item(plan.id, method="https", status="downloading", destination_relpath="b")
    done = repo.add_item(plan.id, method="https", status="complete", destination_relpath="c")
    paused = repo.add_item(plan.id, method="https", status="paused", destination_relpath="d")

    n = repo.requeue_incomplete_items(plan.id)

    assert n == 2
    assert repo.get_item(failed.id).status == "pending"
    assert repo.get_item(active.id).status == "pending"
    assert repo.get_item(done.id).status == "complete"   # not retried
    assert repo.get_item(paused.id).status == "paused"   # left alone


def test_requeue_is_noop_when_all_pending(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plan = repo.create_plan(_root_id(repo))
    repo.add_item(plan.id, method="https", status="pending", destination_relpath="a")

    assert repo.requeue_incomplete_items(plan.id) == 0


def test_reset_ignores_non_running_plans(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plan = repo.create_plan(_root_id(repo))  # state "draft"
    repo.update_plan_state(plan.id, "complete")

    assert repo.reset_orphaned_running_plans() == 0
    assert repo.get_plan(plan.id).state == "complete"
