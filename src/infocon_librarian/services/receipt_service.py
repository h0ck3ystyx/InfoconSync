"""ReceiptService — generate auditable JSON receipts for terminal plan states.

Receipts contain:
- Root-relative file paths only (no absolute paths, no peer IPs)
- Required evidence IDs and methods
- Per-item terminal state and verification level
- Plan metadata (ID, created_at, completed_at)

A partial plan (some items failed/incomplete) still produces a receipt with
accurate state per item.
"""
from __future__ import annotations

import datetime
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from infocon_librarian.transfer.planner import TransferPlan


@dataclass(frozen=True)
class ReceiptItem:
    plan_item_id: str
    relative_path: str
    method: str
    status: str                    # PlanItemStatus value
    verification_level: str | None # VerificationLevel value or None
    error: str | None
    size_bytes: int | None
    fallback_reason: str | None


@dataclass(frozen=True)
class Receipt:
    id: str
    plan_id: str
    completed_at: str   # ISO-8601
    items: tuple[ReceiptItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.id,
            "plan_id": self.plan_id,
            "completed_at": self.completed_at,
            "items": [
                {
                    "plan_item_id": i.plan_item_id,
                    "relative_path": i.relative_path,
                    "method": i.method,
                    "status": i.status,
                    "verification_level": i.verification_level,
                    "error": i.error,
                    "size_bytes": i.size_bytes,
                    "fallback_reason": i.fallback_reason,
                }
                for i in self.items
            ],
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


def generate_receipt(
    plan: TransferPlan,
    *,
    item_outcomes: dict[str, dict[str, Any]] | None = None,
    receipt_id: str | None = None,
    completed_at: str | None = None,
) -> Receipt:
    """Build a Receipt from a plan and per-item outcome overrides.

    Args:
        plan: The transfer plan (may be partially complete).
        item_outcomes: Map of plan_item_id → dict with optional keys:
            ``status``, ``verification_level``, ``error``.
            Unspecified items use their plan-level status.
        receipt_id: Override receipt UUID (for testing).
        completed_at: Override timestamp (for testing).

    Returns:
        Receipt with redacted, root-relative paths only.
    """
    outcomes = item_outcomes or {}
    ts = completed_at or datetime.datetime.now(datetime.UTC).isoformat()
    rid = receipt_id or str(uuid.uuid4())

    receipt_items: list[ReceiptItem] = []
    for item in plan.items:
        override = outcomes.get(item.id, {})
        status = override.get("status", item.status.value)
        verification = override.get("verification_level", None)
        error = override.get("error", None)
        receipt_items.append(ReceiptItem(
            plan_item_id=item.id,
            relative_path=_redact_path(item.relative_path),
            method=item.method,
            status=status,
            verification_level=verification,
            error=error,
            size_bytes=item.size_bytes,
            fallback_reason=item.fallback_reason.value if item.fallback_reason else None,
        ))

    return Receipt(
        id=rid,
        plan_id=plan.id,
        completed_at=ts,
        items=tuple(receipt_items),
    )


def write_receipt(receipt: Receipt, output_path: Path) -> None:
    """Write *receipt* as JSON to *output_path*."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(receipt.to_json())


def _redact_path(path: str) -> str:
    """Ensure path is root-relative and contains no absolute components."""
    clean = path.replace("\\", "/").lstrip("/")
    # Strip any leading drive letters (Windows safety)
    if len(clean) >= 2 and clean[1] == ":":
        clean = clean[2:].lstrip("/")
    return clean
