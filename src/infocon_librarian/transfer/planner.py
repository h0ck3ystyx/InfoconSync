"""Pure transfer planner — no Flask, no libtorrent deps.

Inputs: selected collections, evidence, local snapshots, torrent manifests,
root capabilities, and user policy.
Output: an immutable TransferPlan with one item per selected collection file.

Invariant: the planner commits to a transfer method before execution starts.
A job may not silently switch from torrent to HTTPS.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum


class FallbackReason(StrEnum):
    NO_TORRENT = "no_torrent"
    TORRENT_MALFORMED = "torrent_malformed"
    TORRENT_UNSUPPORTED = "torrent_unsupported"
    TORRENT_NO_COVERAGE = "torrent_no_coverage"


class PlanItemStatus(StrEnum):
    PENDING = "pending"
    SKIPPED = "skipped"        # existing verified content, no action needed
    BLOCKED = "blocked"        # torrent existed but swarm unreachable; needs user action
    REQUIRES_APPROVAL = "requires_approval"   # HTTPS fallback waiting for user OK


@dataclass(frozen=True)
class PlanItem:
    id: str
    plan_id: str
    collection_key_str: str   # "section/key" — avoids circular import
    relative_path: str        # destination relative to archive root
    method: str               # TransferMethod value
    url: str                  # torrent URL or direct HTTPS URL
    size_bytes: int | None
    status: PlanItemStatus
    fallback_reason: FallbackReason | None
    evidence_ids: tuple[str, ...]
    torrent_url: str | None       # set for HTTPS items that have an associated torrent
    # For torrent items: indices to select within the manifest
    torrent_file_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class TransferPlan:
    id: str
    archive_root: str
    items: tuple[PlanItem, ...]
    created_at: str   # ISO-8601

    @property
    def torrent_items(self) -> tuple[PlanItem, ...]:
        return tuple(i for i in self.items if i.method == "torrent")

    @property
    def https_items(self) -> tuple[PlanItem, ...]:
        return tuple(i for i in self.items if i.method == "https")

    @property
    def skipped_items(self) -> tuple[PlanItem, ...]:
        return tuple(i for i in self.items if i.status == PlanItemStatus.SKIPPED)

    @property
    def blocked_items(self) -> tuple[PlanItem, ...]:
        return tuple(i for i in self.items if i.status == PlanItemStatus.BLOCKED)

    @property
    def total_bytes(self) -> int:
        return sum(i.size_bytes or 0 for i in self.items if i.status == PlanItemStatus.PENDING)

    @property
    def torrent_bytes(self) -> int:
        pending = PlanItemStatus.PENDING
        return sum(i.size_bytes or 0 for i in self.torrent_items if i.status == pending)

    @property
    def https_bytes(self) -> int:
        pending = PlanItemStatus.PENDING
        return sum(i.size_bytes or 0 for i in self.https_items if i.status == pending)


@dataclass(frozen=True)
class TorrentSource:
    """A torrent that covers (possibly partially) a collection."""
    torrent_url: str
    torrent_bytes: bytes
    # file relative_path -> (torrent_file_index, size_bytes)
    file_map: dict[str, tuple[int, int]]


@dataclass(frozen=True)
class PlanRequest:
    """Input to the planner for one collection."""
    collection_key_str: str
    archive_root: str
    # Files to transfer: list of (relative_path, https_url, size_hint)
    selected_files: list[tuple[str, str, int | None]]
    torrent_source: TorrentSource | None
    # Evidence IDs to carry into plan items
    evidence_ids: tuple[str, ...] = ()
    # Paths that are already piece-verified (skip if present and verified)
    verified_paths: frozenset[str] = field(default_factory=frozenset)
    # Whether the swarm was previously unreachable for this collection
    swarm_unreachable: bool = False
    # Whether the user has explicitly approved HTTPS fallback for swarm failures
    approve_http_fallback: bool = False


def build_plan(
    requests: list[PlanRequest],
    archive_root: str,
    *,
    plan_id: str | None = None,
    created_at: str | None = None,
) -> TransferPlan:
    """Produce a TransferPlan from a list of PlanRequests.

    Method selection per file:
    1. Torrent — if TorrentSource covers this file
    2. HTTPS fallback — if no usable torrent; reason is machine-readable
    3. BLOCKED — if torrent existed but swarm unreachable and user has not approved fallback
    4. SKIPPED — if path is in verified_paths (piece-verified evidence)
    """
    import datetime

    pid = plan_id or str(uuid.uuid4())
    ts = created_at or datetime.datetime.now(datetime.UTC).isoformat()

    items: list[PlanItem] = []

    for req in requests:
        for rel_path, https_url, size_hint in req.selected_files:
            item_id = str(uuid.uuid4())

            # Skip if already piece-verified
            if rel_path in req.verified_paths:
                items.append(PlanItem(
                    id=item_id,
                    plan_id=pid,
                    collection_key_str=req.collection_key_str,
                    relative_path=rel_path,
                    method="torrent",
                    url="",
                    size_bytes=size_hint,
                    status=PlanItemStatus.SKIPPED,
                    fallback_reason=None,
                    evidence_ids=req.evidence_ids,
                    torrent_url=None,
                ))
                continue

            ts_src = req.torrent_source
            if ts_src is not None and rel_path in ts_src.file_map:
                # Torrent covers this file
                _idx, torrent_size = ts_src.file_map[rel_path]
                indices = tuple(
                    idx for p, (idx, _) in ts_src.file_map.items()
                    if p in [f[0] for f in req.selected_files]
                )
                if req.swarm_unreachable and not req.approve_http_fallback:
                    # Torrent exists but swarm is unreachable — block; don't silently downgrade
                    items.append(PlanItem(
                        id=item_id,
                        plan_id=pid,
                        collection_key_str=req.collection_key_str,
                        relative_path=rel_path,
                        method="torrent",
                        url=ts_src.torrent_url,
                        size_bytes=torrent_size,
                        status=PlanItemStatus.BLOCKED,
                        fallback_reason=None,
                        evidence_ids=req.evidence_ids,
                        torrent_url=ts_src.torrent_url,
                        torrent_file_indices=indices,
                    ))
                else:
                    items.append(PlanItem(
                        id=item_id,
                        plan_id=pid,
                        collection_key_str=req.collection_key_str,
                        relative_path=rel_path,
                        method="torrent",
                        url=ts_src.torrent_url,
                        size_bytes=torrent_size,
                        status=PlanItemStatus.PENDING,
                        fallback_reason=None,
                        evidence_ids=req.evidence_ids,
                        torrent_url=ts_src.torrent_url,
                        torrent_file_indices=indices,
                    ))
            elif ts_src is not None and rel_path not in ts_src.file_map:
                # Torrent exists but doesn't cover this file — HTTPS fallback
                items.append(PlanItem(
                    id=item_id,
                    plan_id=pid,
                    collection_key_str=req.collection_key_str,
                    relative_path=rel_path,
                    method="https",
                    url=https_url,
                    size_bytes=size_hint,
                    status=PlanItemStatus.PENDING,
                    fallback_reason=FallbackReason.TORRENT_NO_COVERAGE,
                    evidence_ids=req.evidence_ids,
                    torrent_url=ts_src.torrent_url,
                ))
            else:
                # No torrent at all
                items.append(PlanItem(
                    id=item_id,
                    plan_id=pid,
                    collection_key_str=req.collection_key_str,
                    relative_path=rel_path,
                    method="https",
                    url=https_url,
                    size_bytes=size_hint,
                    status=PlanItemStatus.PENDING,
                    fallback_reason=FallbackReason.NO_TORRENT,
                    evidence_ids=req.evidence_ids,
                    torrent_url=None,
                ))

    return TransferPlan(
        id=pid,
        archive_root=archive_root,
        items=tuple(items),
        created_at=ts,
    )


def build_plan_with_malformed_torrent(
    requests: list[PlanRequest],
    archive_root: str,
    *,
    fallback_reason: FallbackReason = FallbackReason.TORRENT_MALFORMED,
    plan_id: str | None = None,
    created_at: str | None = None,
) -> TransferPlan:
    """Build a plan where the torrent was present but unusable.

    Each request's torrent_source is ignored; all items fall back to HTTPS
    with the given reason recorded.
    """
    import datetime

    pid = plan_id or str(uuid.uuid4())
    ts = created_at or datetime.datetime.now(datetime.UTC).isoformat()

    items: list[PlanItem] = []
    for req in requests:
        for rel_path, https_url, size_hint in req.selected_files:
            items.append(PlanItem(
                id=str(uuid.uuid4()),
                plan_id=pid,
                collection_key_str=req.collection_key_str,
                relative_path=rel_path,
                method="https",
                url=https_url,
                size_bytes=size_hint,
                status=PlanItemStatus.PENDING,
                fallback_reason=fallback_reason,
                evidence_ids=req.evidence_ids,
                torrent_url=None,
            ))

    return TransferPlan(
        id=pid,
        archive_root=archive_root,
        items=tuple(items),
        created_at=ts,
    )
