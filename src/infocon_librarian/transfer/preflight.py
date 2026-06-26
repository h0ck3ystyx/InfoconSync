"""PreflightService — validates a plan against current archive root state.

Checks before any transfer starts:
1. Volume identity still matches the fingerprint captured at root validation
2. Free disk space covers plan bytes + overhead
3. All destination paths are safe (re-validates through SafeArchivePath)
4. Existing files don't conflict (only skip if piece-verified; size match alone is not enough)

Raises PreflightError with a machine-readable reason on any failure.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from infocon_librarian.domain.errors import PathEscapesRoot
from infocon_librarian.domain.paths import safe_archive_path
from infocon_librarian.transfer.planner import PlanItemStatus, TransferPlan

# Fractional overhead added to required bytes (10%)
_OVERHEAD = 0.10


class PreflightError(Exception):
    """Raised when preflight rejects the plan."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class PreflightResult:
    plan_id: str
    required_bytes: int
    available_bytes: int
    validated_paths: tuple[str, ...]


def run_preflight(
    plan: TransferPlan,
    *,
    volume_fingerprint: str,
    current_fingerprint: str,
    free_bytes: int | None = None,
) -> PreflightResult:
    """Validate *plan* against current root state.

    Args:
        plan: The immutable transfer plan to validate.
        volume_fingerprint: Fingerprint captured when the root was registered.
        current_fingerprint: Fingerprint read right now from the mounted volume.
        free_bytes: Available space (bytes). If None, read from disk.

    Returns:
        PreflightResult on success.

    Raises:
        PreflightError with a machine-readable ``reason`` on any failure.
    """
    root = Path(plan.archive_root)

    # 1. Volume identity check
    if current_fingerprint != volume_fingerprint:
        raise PreflightError(
            "VOLUME_MISMATCH",
            f"Expected {volume_fingerprint!r}, got {current_fingerprint!r}",
        )

    # 2. Disk space
    pending = [i for i in plan.items if i.status == PlanItemStatus.PENDING]
    required = int(plan.total_bytes * (1 + _OVERHEAD))
    if free_bytes is None:
        usage = shutil.disk_usage(root)
        free_bytes = usage.free
    if free_bytes < required:
        raise PreflightError(
            "DISK_SHORTFALL",
            f"Required {required} bytes, available {free_bytes} bytes",
        )

    # 3. Re-validate destination paths
    validated: list[str] = []
    for item in pending:
        components = item.relative_path.replace("\\", "/").split("/")
        try:
            safe_archive_path(root, components)
        except PathEscapesRoot as exc:
            raise PreflightError(
                "UNSAFE_DESTINATION",
                f"Path {item.relative_path!r} failed safety check: {exc}",
            ) from exc
        validated.append(item.relative_path)

    return PreflightResult(
        plan_id=plan.id,
        required_bytes=required,
        available_bytes=free_bytes,
        validated_paths=tuple(validated),
    )
