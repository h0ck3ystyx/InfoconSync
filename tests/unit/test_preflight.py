"""P-006, P-007, P-008 — PreflightService unit tests."""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from infocon_librarian.transfer.planner import (
    PlanItem,
    PlanItemStatus,
    TransferPlan,
)
from infocon_librarian.transfer.preflight import PreflightError, run_preflight

_FINGERPRINT = "vol-aabbcc1122"
_ROOT = "/archive"


def _plan(
    items: list[PlanItem] | None = None,
    archive_root: str = _ROOT,
) -> TransferPlan:
    plan_id = str(uuid.uuid4())
    return TransferPlan(
        id=plan_id,
        archive_root=archive_root,
        items=tuple(items or []),
        created_at="2026-01-01T00:00:00+00:00",
    )


def _item(
    relative_path: str = "defcon/dc32/slides.pdf",
    size_bytes: int = 1_000,
    status: PlanItemStatus = PlanItemStatus.PENDING,
) -> PlanItem:
    plan_id = str(uuid.uuid4())
    return PlanItem(
        id=str(uuid.uuid4()),
        plan_id=plan_id,
        collection_key_str="defcon/dc32",
        relative_path=relative_path,
        method="torrent",
        url="https://example.com/dc32.torrent",
        size_bytes=size_bytes,
        status=status,
        fallback_reason=None,
        evidence_ids=(),
        torrent_url="https://example.com/dc32.torrent",
    )


# ---------------------------------------------------------------------------
# P-006: Disk shortfall → preflight fails with DISK_SHORTFALL
# ---------------------------------------------------------------------------


def test_p006_disk_shortfall_raises() -> None:
    plan = _plan([_item(size_bytes=10_000)])

    with pytest.raises(PreflightError) as exc_info:
        run_preflight(
            plan,
            volume_fingerprint=_FINGERPRINT,
            current_fingerprint=_FINGERPRINT,
            free_bytes=5_000,  # less than 10_000 * 1.10 = 11_000
        )

    assert exc_info.value.reason == "DISK_SHORTFALL"
    assert "5000" in exc_info.value.detail
    assert "11000" in exc_info.value.detail


def test_p006_exact_overhead_enforced() -> None:
    # Need 1000 bytes + 10% = 1100; providing exactly 1100 should pass
    plan = _plan([_item(size_bytes=1_000)])
    result = run_preflight(
        plan,
        volume_fingerprint=_FINGERPRINT,
        current_fingerprint=_FINGERPRINT,
        free_bytes=1_100,
    )
    assert result.required_bytes == 1_100


def test_p006_skipped_items_not_counted(tmp_path: Path) -> None:
    skipped = _item(size_bytes=1_000_000, status=PlanItemStatus.SKIPPED)
    pending = _item(
        relative_path="defcon/dc32/audio.mp3", size_bytes=100, status=PlanItemStatus.PENDING
    )
    plan = _plan([skipped, pending], archive_root=str(tmp_path))

    result = run_preflight(
        plan,
        volume_fingerprint=_FINGERPRINT,
        current_fingerprint=_FINGERPRINT,
        free_bytes=200,  # would fail if skipped item counted (needs 1_000_000 * 1.1)
    )
    assert result.required_bytes == 110  # 100 * 1.10


# ---------------------------------------------------------------------------
# P-007: Drive swapped after planning → VOLUME_MISMATCH
# ---------------------------------------------------------------------------


def test_p007_volume_mismatch_raises() -> None:
    plan = _plan([_item()])

    with pytest.raises(PreflightError) as exc_info:
        run_preflight(
            plan,
            volume_fingerprint=_FINGERPRINT,
            current_fingerprint="different-fingerprint",
            free_bytes=1_000_000,
        )

    assert exc_info.value.reason == "VOLUME_MISMATCH"


def test_p007_matching_fingerprint_passes() -> None:
    plan = _plan([_item(size_bytes=100)])
    result = run_preflight(
        plan,
        volume_fingerprint=_FINGERPRINT,
        current_fingerprint=_FINGERPRINT,
        free_bytes=1_000_000,
    )
    assert result.plan_id == plan.id


# ---------------------------------------------------------------------------
# P-008: Existing verified file → skipped; listing-size equality alone is not enough
# ---------------------------------------------------------------------------


def test_p008_skipped_item_not_in_validated_paths() -> None:
    """A SKIPPED item (piece-verified) passes preflight without path validation."""
    skipped = _item(size_bytes=1_000, status=PlanItemStatus.SKIPPED)
    plan = _plan([skipped])

    result = run_preflight(
        plan,
        volume_fingerprint=_FINGERPRINT,
        current_fingerprint=_FINGERPRINT,
        free_bytes=1_000_000,
    )
    # Skipped items don't need destination validation — they're already verified
    assert skipped.relative_path not in result.validated_paths


def test_p008_pending_item_validated() -> None:
    pending = _item(relative_path="defcon/dc32/slides.pdf", status=PlanItemStatus.PENDING)
    plan = _plan([pending])

    result = run_preflight(
        plan,
        volume_fingerprint=_FINGERPRINT,
        current_fingerprint=_FINGERPRINT,
        free_bytes=1_000_000,
    )
    assert "defcon/dc32/slides.pdf" in result.validated_paths


def test_p008_unsafe_destination_rejected() -> None:
    """A path-traversal destination is rejected even if the plan is otherwise valid."""
    bad_item = _item(relative_path="../etc/passwd")
    plan = _plan([bad_item])

    with pytest.raises(PreflightError) as exc_info:
        run_preflight(
            plan,
            volume_fingerprint=_FINGERPRINT,
            current_fingerprint=_FINGERPRINT,
            free_bytes=1_000_000,
        )
    assert exc_info.value.reason == "UNSAFE_DESTINATION"
