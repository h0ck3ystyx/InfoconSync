"""P-013 — ReceiptService unit tests."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from infocon_librarian.services.receipt_service import generate_receipt, write_receipt
from infocon_librarian.transfer.planner import (
    FallbackReason,
    PlanItem,
    PlanItemStatus,
    TransferPlan,
)

_ROOT = "/archive"


def _plan(items: list[PlanItem] | None = None) -> TransferPlan:
    pid = str(uuid.uuid4())
    return TransferPlan(
        id=pid,
        archive_root=_ROOT,
        items=tuple(items or []),
        created_at="2026-01-01T00:00:00+00:00",
    )


def _item(
    relative_path: str = "defcon/dc32/slides.pdf",
    method: str = "torrent",
    size_bytes: int = 1_000,
    status: PlanItemStatus = PlanItemStatus.PENDING,
    fallback_reason: FallbackReason | None = None,
) -> PlanItem:
    pid = str(uuid.uuid4())
    return PlanItem(
        id=str(uuid.uuid4()),
        plan_id=pid,
        collection_key_str="defcon/dc32",
        relative_path=relative_path,
        method=method,
        url="https://example.com/dc32.torrent",
        size_bytes=size_bytes,
        status=status,
        fallback_reason=fallback_reason,
        evidence_ids=("ev-001",),
        torrent_url="https://example.com/dc32.torrent" if method == "torrent" else None,
    )


# ---------------------------------------------------------------------------
# P-013: Receipt redaction — root-relative paths, evidence, no peer IPs
# ---------------------------------------------------------------------------


def test_p013_receipt_contains_plan_id() -> None:
    plan = _plan([_item()])
    receipt = generate_receipt(plan)
    assert receipt.plan_id == plan.id


def test_p013_receipt_item_uses_relative_path() -> None:
    plan = _plan([_item(relative_path="defcon/dc32/slides.pdf")])
    receipt = generate_receipt(plan)
    assert receipt.items[0].relative_path == "defcon/dc32/slides.pdf"


def test_p013_absolute_path_stripped_in_receipt() -> None:
    """If a path starts with /, it's stripped to root-relative in the receipt."""
    plan = _plan([_item(relative_path="/defcon/dc32/slides.pdf")])
    receipt = generate_receipt(plan)
    assert not receipt.items[0].relative_path.startswith("/")
    assert "slides.pdf" in receipt.items[0].relative_path


def test_p013_no_peer_ip_in_receipt_json() -> None:
    plan = _plan([_item()])
    receipt = generate_receipt(plan)
    body = receipt.to_json()
    # No IP addresses should appear in the receipt
    import re
    ip_pattern = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
    assert not ip_pattern.search(body)


def test_p013_receipt_has_method_and_status() -> None:
    plan = _plan([
        _item(method="torrent", status=PlanItemStatus.PENDING),
        _item(
            relative_path="dc32/audio.mp3",
            method="https",
            status=PlanItemStatus.PENDING,
            fallback_reason=FallbackReason.NO_TORRENT,
        ),
    ])
    receipt = generate_receipt(plan)
    assert len(receipt.items) == 2
    methods = {i.method for i in receipt.items}
    assert methods == {"torrent", "https"}


def test_p013_outcome_override_applied() -> None:
    item = _item()
    plan = _plan([item])
    receipt = generate_receipt(
        plan,
        item_outcomes={
            item.id: {
                "status": "complete",
                "verification_level": "piece_verified",
            }
        },
    )
    ri = receipt.items[0]
    assert ri.status == "complete"
    assert ri.verification_level == "piece_verified"


def test_p013_fallback_reason_in_receipt() -> None:
    item = _item(method="https", fallback_reason=FallbackReason.NO_TORRENT)
    plan = _plan([item])
    receipt = generate_receipt(plan)
    assert receipt.items[0].fallback_reason == "no_torrent"


def test_p013_receipt_is_valid_json() -> None:
    plan = _plan([_item(), _item(relative_path="dc32/audio.mp3")])
    receipt = generate_receipt(plan)
    parsed = json.loads(receipt.to_json())
    assert "receipt_id" in parsed
    assert "plan_id" in parsed
    assert "items" in parsed
    assert len(parsed["items"]) == 2


def test_p013_partial_plan_receipt_has_all_items() -> None:
    """A receipt for a partial plan includes ALL items, even unfinished ones."""
    items = [
        _item(relative_path="dc32/slides.pdf", status=PlanItemStatus.PENDING),
        _item(relative_path="dc32/audio.mp3", status=PlanItemStatus.SKIPPED),
        _item(relative_path="dc32/video.mp4", status=PlanItemStatus.BLOCKED),
    ]
    plan = _plan(items)
    receipt = generate_receipt(plan)
    assert len(receipt.items) == 3
    statuses = {i.status for i in receipt.items}
    assert "pending" in statuses
    assert "skipped" in statuses
    assert "blocked" in statuses


def test_p013_write_receipt_creates_file(tmp_path: Path) -> None:
    plan = _plan([_item()])
    receipt = generate_receipt(plan, receipt_id="test-receipt-id")

    out = tmp_path / "receipts" / "test.json"
    write_receipt(receipt, out)

    assert out.exists()
    parsed = json.loads(out.read_text())
    assert parsed["receipt_id"] == "test-receipt-id"


def test_p013_receipt_item_size_preserved() -> None:
    item = _item(size_bytes=99_999)
    plan = _plan([item])
    receipt = generate_receipt(plan)
    assert receipt.items[0].size_bytes == 99_999
