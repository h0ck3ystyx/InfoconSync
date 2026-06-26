"""D-008 through D-012 — CheckService status logic."""
from __future__ import annotations

import hashlib

from infocon_librarian.domain.models import ArchiveStatus, EvidenceKind, RemoteEntry
from infocon_librarian.services.check_service import check_collections

_BASE = "https://infocon.example.com/defcon/"


def _dir_entry(name: str, url_suffix: str | None = None) -> RemoteEntry:
    suffix = url_suffix or (name.replace(" ", "%20") + "/")
    url = _BASE + suffix
    return RemoteEntry(
        id=hashlib.sha256(url.encode()).hexdigest(),
        url=url,
        parent_url=_BASE,
        kind="directory",
        display_name=name,
        size_hint=None,
        modified_hint=None,
    )


def _torrent_entry(filename: str) -> RemoteEntry:
    url = _BASE + filename
    return RemoteEntry(
        id=hashlib.sha256(url.encode()).hexdigest(),
        url=url,
        parent_url=_BASE,
        kind="file",
        display_name=filename,
        size_hint=1024 * 1024,
        modified_hint="2024-08-08 14:00",
    )


# ---------------------------------------------------------------------------
# D-008: Existing incomplete folder → Present, unverified
# ---------------------------------------------------------------------------


def test_d008_existing_folder_is_present_unverified() -> None:
    entries = [_dir_entry("DEF CON 32")]
    results = check_collections(
        section="defcon",
        remote_entries=entries,
        local_dirs={"DEF CON 32"},
    )

    assert len(results) == 1
    assert results[0].status == ArchiveStatus.PRESENT_UNVERIFIED


def test_d008_evidence_has_remote_listing_kind() -> None:
    entries = [_dir_entry("DEF CON 32")]
    results = check_collections(
        section="defcon",
        remote_entries=entries,
        local_dirs={"DEF CON 32"},
    )

    kinds = {e.kind for e in results[0].evidence}
    assert EvidenceKind.REMOTE_LISTING in kinds


# ---------------------------------------------------------------------------
# D-009: Remote collection absent locally → New with remote evidence
# ---------------------------------------------------------------------------


def test_d009_remote_only_collection_is_new() -> None:
    entries = [_dir_entry("DEF CON 32")]
    results = check_collections(
        section="defcon",
        remote_entries=entries,
        local_dirs=set(),  # nothing local
    )

    assert len(results) == 1
    assert results[0].status == ArchiveStatus.NEW


def test_d009_new_result_has_evidence() -> None:
    entries = [_dir_entry("DEF CON 32")]
    results = check_collections(
        section="defcon",
        remote_entries=entries,
        local_dirs=set(),
    )

    assert len(results[0].evidence) >= 1
    ev = results[0].evidence[0]
    assert ev.kind == EvidenceKind.REMOTE_LISTING
    assert ev.collection_key.section == "defcon"


# ---------------------------------------------------------------------------
# D-010: Local-only collection → Local only; no delete operation emitted
# ---------------------------------------------------------------------------


def test_d010_local_only_collection() -> None:
    # No remote entries; one local dir
    results = check_collections(
        section="defcon",
        remote_entries=[],
        local_dirs={"old-talks"},
    )

    assert len(results) == 1
    assert results[0].status == ArchiveStatus.LOCAL_ONLY


def test_d010_local_only_has_local_snapshot_evidence() -> None:
    results = check_collections(
        section="defcon",
        remote_entries=[],
        local_dirs={"old-talks"},
    )

    kinds = {e.kind for e in results[0].evidence}
    assert EvidenceKind.LOCAL_SNAPSHOT in kinds


def test_d010_local_only_does_not_produce_delete() -> None:
    results = check_collections(
        section="defcon",
        remote_entries=[],
        local_dirs={"old-talks"},
    )

    # Verify no result contains a delete indicator in its payload
    for result in results:
        for ev in result.evidence:
            assert "delete" not in str(ev.payload).lower()


# ---------------------------------------------------------------------------
# D-011: Versioned torrent marker changed → Changed_marker, no piece verify claim
# ---------------------------------------------------------------------------


def test_d011_changed_torrent_marker() -> None:
    entries = [
        _dir_entry("DEF CON 32"),
        _torrent_entry("defcon-32-v2.torrent"),
    ]
    previous = {"DEF CON 32": _BASE + "defcon-32-v1.torrent"}

    results = check_collections(
        section="defcon",
        remote_entries=entries,
        local_dirs={"DEF CON 32"},
        previous_markers=previous,
    )

    by_key = {r.collection_key.key: r for r in results}
    assert "DEF CON 32" in by_key
    assert by_key["DEF CON 32"].status == ArchiveStatus.CHANGED_MARKER


def test_d011_changed_marker_evidence_has_old_and_new_url() -> None:
    entries = [
        _dir_entry("DEF CON 32"),
        _torrent_entry("defcon-32-v2.torrent"),
    ]
    previous = {"DEF CON 32": _BASE + "defcon-32-v1.torrent"}

    results = check_collections(
        section="defcon",
        remote_entries=entries,
        local_dirs={"DEF CON 32"},
        previous_markers=previous,
    )

    ev = next(e for e in results[0].evidence if e.kind == EvidenceKind.REMOTE_LISTING)
    assert ev.payload.get("previous_torrent_url") is not None
    assert ev.payload.get("torrent_url") is not None
    assert ev.payload["previous_torrent_url"] != ev.payload["torrent_url"]


def test_d011_changed_marker_status_is_not_verified() -> None:
    entries = [
        _dir_entry("DEF CON 32"),
        _torrent_entry("defcon-32-v2.torrent"),
    ]
    previous = {"DEF CON 32": _BASE + "defcon-32-v1.torrent"}

    results = check_collections(
        section="defcon",
        remote_entries=entries,
        local_dirs={"DEF CON 32"},
        previous_markers=previous,
    )

    assert results[0].status != ArchiveStatus.VERIFIED_CURRENT


# ---------------------------------------------------------------------------
# D-012: Collection without torrent → Unknown or Present_unverified, not Unchanged
# ---------------------------------------------------------------------------


def test_d012_no_torrent_local_present_is_present_unverified() -> None:
    entries = [_dir_entry("Skills Village")]  # no torrent entry
    results = check_collections(
        section="defcon",
        remote_entries=entries,
        local_dirs={"Skills Village"},
    )

    assert results[0].status == ArchiveStatus.PRESENT_UNVERIFIED


def test_d012_no_torrent_not_local_is_new() -> None:
    entries = [_dir_entry("Skills Village")]
    results = check_collections(
        section="defcon",
        remote_entries=entries,
        local_dirs=set(),
    )

    # Must be New or Unknown — never Unchanged (not even a valid status)
    assert results[0].status in (ArchiveStatus.NEW, ArchiveStatus.UNKNOWN)


def test_d012_status_is_never_unchanged() -> None:
    """Unchanged is not a valid user-facing status."""
    entries = [_dir_entry("Skills Village"), _dir_entry("DEF CON 32")]
    results = check_collections(
        section="defcon",
        remote_entries=entries,
        local_dirs={"DEF CON 32"},
    )

    for r in results:
        assert r.status != "unchanged"
