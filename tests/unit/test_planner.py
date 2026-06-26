"""P-001 through P-005 — TransferPlanner unit tests."""
from __future__ import annotations

from infocon_librarian.transfer.planner import (
    FallbackReason,
    PlanItemStatus,
    PlanRequest,
    TorrentSource,
    build_plan,
    build_plan_with_malformed_torrent,
)

_ROOT = "/archive"
_CKEY = "defcon/defcon-32"

_TORRENT_URL = "https://example.com/dc32.torrent"
_HTTPS_URL = "https://example.com/dc32/slides.pdf"


def _req(
    files: list[tuple[str, str, int | None]],
    torrent_source: TorrentSource | None = None,
    verified_paths: frozenset[str] | None = None,
    swarm_unreachable: bool = False,
    approve_http_fallback: bool = False,
) -> PlanRequest:
    return PlanRequest(
        collection_key_str=_CKEY,
        archive_root=_ROOT,
        selected_files=files,
        torrent_source=torrent_source,
        verified_paths=verified_paths or frozenset(),
        swarm_unreachable=swarm_unreachable,
        approve_http_fallback=approve_http_fallback,
    )


def _source(files: dict[str, tuple[int, int]]) -> TorrentSource:
    return TorrentSource(
        torrent_url=_TORRENT_URL,
        torrent_bytes=b"fake",
        file_map=files,
    )


# ---------------------------------------------------------------------------
# P-001: Valid torrent covers selection → torrent item with exact bytes
# ---------------------------------------------------------------------------


def test_p001_valid_torrent_produces_torrent_item() -> None:
    src = _source({"defcon-32/slides.pdf": (0, 5_000_000)})
    req = _req(
        files=[("defcon-32/slides.pdf", _HTTPS_URL, None)],
        torrent_source=src,
    )
    plan = build_plan([req], _ROOT)

    assert len(plan.items) == 1
    item = plan.items[0]
    assert item.method == "torrent"
    assert item.url == _TORRENT_URL
    assert item.size_bytes == 5_000_000
    assert item.status == PlanItemStatus.PENDING
    assert item.fallback_reason is None


def test_p001_torrent_bytes_aggregated() -> None:
    src = _source({
        "defcon-32/slides.pdf": (0, 1_000),
        "defcon-32/audio/talk.mp3": (1, 2_000),
    })
    req = _req(
        files=[
            ("defcon-32/slides.pdf", _HTTPS_URL, None),
            ("defcon-32/audio/talk.mp3", _HTTPS_URL + ".mp3", None),
        ],
        torrent_source=src,
    )
    plan = build_plan([req], _ROOT)

    assert plan.total_bytes == 3_000
    assert plan.torrent_bytes == 3_000
    assert plan.https_bytes == 0
    assert all(i.method == "torrent" for i in plan.items)


# ---------------------------------------------------------------------------
# P-002: No torrent exists → HTTPS fallback with NO_TORRENT reason
# ---------------------------------------------------------------------------


def test_p002_no_torrent_produces_https_fallback() -> None:
    req = _req(
        files=[("defcon-32/slides.pdf", _HTTPS_URL, 5_000)],
        torrent_source=None,
    )
    plan = build_plan([req], _ROOT)

    assert len(plan.items) == 1
    item = plan.items[0]
    assert item.method == "https"
    assert item.url == _HTTPS_URL
    assert item.fallback_reason == FallbackReason.NO_TORRENT
    assert item.status == PlanItemStatus.PENDING
    assert item.torrent_url is None


def test_p002_no_torrent_size_hint_preserved() -> None:
    req = _req(
        files=[("dc32/audio.mp3", "https://example.com/audio.mp3", 999_999)],
        torrent_source=None,
    )
    plan = build_plan([req], _ROOT)
    assert plan.items[0].size_bytes == 999_999


# ---------------------------------------------------------------------------
# P-003: Torrent malformed/unsupported → HTTPS fallback with precise reason
# ---------------------------------------------------------------------------


def test_p003_malformed_torrent_fallback_reason() -> None:
    req = _req(files=[("dc32/video.mp4", "https://example.com/video.mp4", 100)])
    plan = build_plan_with_malformed_torrent(
        [req], _ROOT, fallback_reason=FallbackReason.TORRENT_MALFORMED
    )

    assert len(plan.items) == 1
    item = plan.items[0]
    assert item.method == "https"
    assert item.fallback_reason == FallbackReason.TORRENT_MALFORMED
    assert item.status == PlanItemStatus.PENDING


def test_p003_unsupported_torrent_fallback_reason() -> None:
    req = _req(files=[("dc32/video.mp4", "https://example.com/video.mp4", 100)])
    plan = build_plan_with_malformed_torrent(
        [req], _ROOT, fallback_reason=FallbackReason.TORRENT_UNSUPPORTED
    )
    assert plan.items[0].fallback_reason == FallbackReason.TORRENT_UNSUPPORTED


# ---------------------------------------------------------------------------
# P-004: Torrent exists but swarm unreachable → BLOCKED; no auto HTTPS
# ---------------------------------------------------------------------------


def test_p004_swarm_unreachable_produces_blocked_item() -> None:
    src = _source({"dc32/slides.pdf": (0, 500)})
    req = _req(
        files=[("dc32/slides.pdf", _HTTPS_URL, 500)],
        torrent_source=src,
        swarm_unreachable=True,
        approve_http_fallback=False,
    )
    plan = build_plan([req], _ROOT)

    assert plan.items[0].status == PlanItemStatus.BLOCKED
    assert plan.items[0].method == "torrent"   # method stays torrent; item is blocked
    assert plan.items[0].fallback_reason is None


def test_p004_blocked_item_not_in_https_items() -> None:
    src = _source({"dc32/slides.pdf": (0, 500)})
    req = _req(
        files=[("dc32/slides.pdf", _HTTPS_URL, 500)],
        torrent_source=src,
        swarm_unreachable=True,
    )
    plan = build_plan([req], _ROOT)
    assert len(plan.https_items) == 0
    assert len(plan.blocked_items) == 1


def test_p004_approve_http_fallback_unlocks_torrent_item() -> None:
    """With explicit approve_http_fallback=True, blocked → pending (but still torrent method)."""
    src = _source({"dc32/slides.pdf": (0, 500)})
    req = _req(
        files=[("dc32/slides.pdf", _HTTPS_URL, 500)],
        torrent_source=src,
        swarm_unreachable=True,
        approve_http_fallback=True,
    )
    plan = build_plan([req], _ROOT)
    assert plan.items[0].status == PlanItemStatus.PENDING


# ---------------------------------------------------------------------------
# P-005: Mixed coverage → split plan with accurate per-method totals
# ---------------------------------------------------------------------------


def test_p005_mixed_coverage_split() -> None:
    # Torrent covers slides.pdf but NOT audio/talk.mp3
    src = _source({"dc32/slides.pdf": (0, 1_000)})
    req = _req(
        files=[
            ("dc32/slides.pdf", _HTTPS_URL, 1_000),
            ("dc32/audio/talk.mp3", "https://example.com/talk.mp3", 2_000),
        ],
        torrent_source=src,
    )
    plan = build_plan([req], _ROOT)

    assert len(plan.items) == 2
    assert plan.torrent_bytes == 1_000
    assert plan.https_bytes == 2_000
    assert plan.total_bytes == 3_000


def test_p005_mixed_items_have_correct_methods() -> None:
    src = _source({"dc32/slides.pdf": (0, 1_000)})
    req = _req(
        files=[
            ("dc32/slides.pdf", _HTTPS_URL, 1_000),
            ("dc32/audio/talk.mp3", "https://example.com/talk.mp3", 2_000),
        ],
        torrent_source=src,
    )
    plan = build_plan([req], _ROOT)

    by_path = {i.relative_path: i for i in plan.items}
    assert by_path["dc32/slides.pdf"].method == "torrent"
    assert by_path["dc32/audio/talk.mp3"].method == "https"
    assert by_path["dc32/audio/talk.mp3"].fallback_reason == FallbackReason.TORRENT_NO_COVERAGE


def test_p005_mixed_item_retains_torrent_url() -> None:
    src = _source({"dc32/slides.pdf": (0, 1_000)})
    req = _req(
        files=[
            ("dc32/slides.pdf", _HTTPS_URL, 1_000),
            ("dc32/audio/talk.mp3", "https://example.com/talk.mp3", 2_000),
        ],
        torrent_source=src,
    )
    plan = build_plan([req], _ROOT)
    https_item = next(i for i in plan.items if i.method == "https")
    # HTTPS fallback item retains the torrent URL for evidence
    assert https_item.torrent_url == _TORRENT_URL


def test_p005_verified_path_skipped() -> None:
    src = _source({"dc32/slides.pdf": (0, 1_000)})
    req = _req(
        files=[
            ("dc32/slides.pdf", _HTTPS_URL, 1_000),
            ("dc32/audio/talk.mp3", "https://example.com/talk.mp3", 2_000),
        ],
        torrent_source=src,
        verified_paths=frozenset(["dc32/slides.pdf"]),
    )
    plan = build_plan([req], _ROOT)

    by_path = {i.relative_path: i for i in plan.items}
    assert by_path["dc32/slides.pdf"].status == PlanItemStatus.SKIPPED
    # Skipped item doesn't count toward total_bytes
    assert plan.total_bytes == 2_000


def test_p005_plan_id_stable() -> None:
    req = _req(files=[("dc32/slides.pdf", _HTTPS_URL, 1_000)])
    plan = build_plan([req], _ROOT, plan_id="fixed-plan-id")
    assert plan.id == "fixed-plan-id"
    for item in plan.items:
        assert item.plan_id == "fixed-plan-id"
