"""CheckService — produces explainable ArchiveStatus for each collection."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from infocon_librarian.domain.models import (
    ArchiveStatus,
    CheckResult,
    CollectionKey,
    Evidence,
    EvidenceKind,
    RemoteEntry,
)
from infocon_librarian.remote.discovery import associate_torrents, find_torrent_links


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def check_collections(
    *,
    section: str,
    remote_entries: list[RemoteEntry],
    local_dirs: set[str],
    previous_markers: dict[str, str] | None = None,
    observed_at: str | None = None,
) -> list[CheckResult]:
    """Produce a CheckResult for each collection visible from *remote_entries*.

    Args:
        section: Name of the archive section being checked (e.g., "defcon").
        remote_entries: All entries returned by the fancyindex parser for this
            listing page (directories AND files mixed).
        local_dirs: Set of top-level subdirectory names present locally under
            the section root.
        previous_markers: Map from collection display_name → previously seen
            torrent URL.  Used to detect changed release markers.
        observed_at: ISO-8601 timestamp; defaults to now.
    """
    ts = observed_at or _now_iso()
    prev = previous_markers or {}

    remote_dirs = [e for e in remote_entries if e.kind == "directory"]
    torrents = find_torrent_links(remote_entries)
    torrent_map = associate_torrents(remote_dirs, torrents)

    remote_keys = {e.display_name for e in remote_dirs}
    # Case-insensitive sets for matching against local directory names (NTFS/HFS+ are
    # case-insensitive but case-preserving, so exact casing may differ from remote).
    local_dirs_lower = {d.lower(): d for d in local_dirs}
    remote_keys_lower = {k.lower() for k in remote_keys}

    results: list[CheckResult] = []

    # Status for each remote directory
    for entry in remote_dirs:
        name = entry.display_name
        ckey = CollectionKey(section=section, key=name)
        present_locally = name.lower() in local_dirs_lower
        current_torrent = torrent_map.get(name)
        previous_torrent = prev.get(name)

        evidence_list: list[Evidence] = [
            Evidence(
                id=str(uuid.uuid4()),
                collection_key=ckey,
                kind=EvidenceKind.REMOTE_LISTING,
                payload={
                    "url": entry.url,
                    "display_name": name,
                    "torrent_url": current_torrent,
                    "previous_torrent_url": previous_torrent,
                },
                observed_at=ts,
            )
        ]

        if not present_locally:
            status = ArchiveStatus.NEW
        elif current_torrent and previous_torrent and current_torrent != previous_torrent:
            status = ArchiveStatus.CHANGED_MARKER
        else:
            status = ArchiveStatus.PRESENT_UNVERIFIED

        results.append(
            CheckResult(
                collection_key=ckey,
                status=status,
                evidence=tuple(evidence_list),
            )
        )

    # Local-only collections (present locally but absent from remote listing)
    for name in local_dirs:
        if name.lower() not in remote_keys_lower:
            ckey = CollectionKey(section=section, key=name)
            results.append(
                CheckResult(
                    collection_key=ckey,
                    status=ArchiveStatus.LOCAL_ONLY,
                    evidence=(
                        Evidence(
                            id=str(uuid.uuid4()),
                            collection_key=ckey,
                            kind=EvidenceKind.LOCAL_SNAPSHOT,
                            payload={"name": name},
                            observed_at=ts,
                        ),
                    ),
                )
            )

    return results
