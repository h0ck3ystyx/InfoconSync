"""T-001 through T-004 — TorrentMetainfoService path validation and coverage."""
from __future__ import annotations

from pathlib import Path

import pytest

from infocon_librarian.domain.errors import InvalidTorrent
from infocon_librarian.domain.models import (
    TorrentFile,
    TorrentManifest,
    TorrentProtocol,
)
from infocon_librarian.torrent.metainfo import coverage_map, process_metainfo
from tests.support.fake_engine import FakeTorrentEngine


def _make_manifest(
    files: list[tuple[str, int]], protocol: TorrentProtocol = TorrentProtocol.V1
) -> TorrentManifest:
    return TorrentManifest(
        url="http://example.com/test.torrent",
        protocol=protocol,
        v1_infohash="aabbcc" if protocol in (TorrentProtocol.V1, TorrentProtocol.HYBRID) else None,
        v2_infohash="ddeeff" if protocol in (TorrentProtocol.V2, TorrentProtocol.HYBRID) else None,
        files=tuple(
            TorrentFile(index=i, relative_path=path, size=size)
            for i, (path, size) in enumerate(files)
        ),
        trackers=("http://tracker.example.com/announce",),
        total_size=sum(s for _, s in files),
        name="test-collection",
    )


# ---------------------------------------------------------------------------
# T-001: Unsafe v1 path — rejected before engine add
# ---------------------------------------------------------------------------


def test_t001_dotdot_path_rejected(tmp_path: Path) -> None:
    engine = FakeTorrentEngine()
    engine.configure_manifest(_make_manifest([("../evil.txt", 100)]))

    with pytest.raises(InvalidTorrent, match="[Uu]nsafe|escapes|traversal"):
        process_metainfo(b"torrent", archive_root=tmp_path, adapter=engine)


def test_t001_absolute_path_rejected(tmp_path: Path) -> None:
    engine = FakeTorrentEngine()
    engine.configure_manifest(_make_manifest([("/etc/passwd", 100)]))

    with pytest.raises(InvalidTorrent):
        process_metainfo(b"torrent", archive_root=tmp_path, adapter=engine)


def test_t001_reserved_name_rejected(tmp_path: Path) -> None:
    engine = FakeTorrentEngine()
    engine.configure_manifest(_make_manifest([("NUL", 100)]))

    with pytest.raises(InvalidTorrent, match="[Uu]nsafe|reserved"):
        process_metainfo(b"torrent", archive_root=tmp_path, adapter=engine)


def test_t001_engine_never_started_for_unsafe_torrent(tmp_path: Path) -> None:
    engine = FakeTorrentEngine()
    engine.configure_manifest(_make_manifest([("../evil.txt", 100)]))

    with pytest.raises(InvalidTorrent):
        process_metainfo(b"torrent", archive_root=tmp_path, adapter=engine)

    # start() must not have been called
    assert engine.started_params == []


# ---------------------------------------------------------------------------
# T-002: Unsafe v2 file-tree path — rejected before engine add
# ---------------------------------------------------------------------------


def test_t002_v2_dotdot_path_rejected(tmp_path: Path) -> None:
    engine = FakeTorrentEngine()
    engine.configure_manifest(
        _make_manifest(
            [("safe/file.mp4", 1024), ("../escape.txt", 512)],
            protocol=TorrentProtocol.HYBRID,
        )
    )

    with pytest.raises(InvalidTorrent):
        process_metainfo(b"torrent", archive_root=tmp_path, adapter=engine)

    assert engine.started_params == []


def test_t002_v2_nul_byte_in_path(tmp_path: Path) -> None:
    engine = FakeTorrentEngine()
    engine.configure_manifest(
        _make_manifest([("talks/file\x00name.mp4", 1024)], protocol=TorrentProtocol.V2)
    )

    with pytest.raises(InvalidTorrent):
        process_metainfo(b"torrent", archive_root=tmp_path, adapter=engine)


# ---------------------------------------------------------------------------
# T-003: Valid multi-file mapping — exact destinations and total bytes
# ---------------------------------------------------------------------------


def test_t003_valid_mapping_returns_safe_paths(tmp_path: Path) -> None:
    engine = FakeTorrentEngine()
    files = [
        ("talks/keynote.mp4", 1024 * 1024),
        ("slides/deck.pdf", 512 * 1024),
        ("audio/talk.mp3", 256 * 1024),
    ]
    engine.configure_manifest(_make_manifest(files))

    result = process_metainfo(b"torrent", archive_root=tmp_path, adapter=engine)

    assert len(result.safe_paths) == 3
    assert result.safe_paths[0].relative == "talks/keynote.mp4"
    assert result.safe_paths[1].relative == "slides/deck.pdf"
    assert result.safe_paths[2].relative == "audio/talk.mp3"


def test_t003_safe_paths_are_contained_in_root(tmp_path: Path) -> None:
    engine = FakeTorrentEngine()
    engine.configure_manifest(_make_manifest([("talks/keynote.mp4", 1024)]))

    result = process_metainfo(b"torrent", archive_root=tmp_path, adapter=engine)

    for sp in result.safe_paths:
        assert str(sp.absolute).startswith(str(tmp_path))


def test_t003_total_bytes_preserved(tmp_path: Path) -> None:
    engine = FakeTorrentEngine()
    files = [("a.mp4", 1000), ("b.pdf", 2000)]
    engine.configure_manifest(_make_manifest(files))

    result = process_metainfo(b"torrent", archive_root=tmp_path, adapter=engine)

    assert result.manifest.total_size == 3000


def test_t003_malformed_torrent_raises_invalid(tmp_path: Path) -> None:
    engine = FakeTorrentEngine()
    engine.configure_inspect_error(InvalidTorrent("bad bencode"))

    with pytest.raises(InvalidTorrent):
        process_metainfo(b"garbage", archive_root=tmp_path, adapter=engine)


# ---------------------------------------------------------------------------
# T-004: Partial torrent coverage → covered and uncovered sets
# ---------------------------------------------------------------------------


def test_t004_fully_covered(tmp_path: Path) -> None:
    engine = FakeTorrentEngine()
    files = [("talks/keynote.mp4", 1024), ("slides/deck.pdf", 512)]
    engine.configure_manifest(_make_manifest(files))
    result = process_metainfo(b"torrent", archive_root=tmp_path, adapter=engine)

    selected = {"talks/keynote.mp4", "slides/deck.pdf"}
    covered, uncovered = coverage_map(result.manifest, selected)

    assert covered == selected
    assert uncovered == set()


def test_t004_partially_covered(tmp_path: Path) -> None:
    engine = FakeTorrentEngine()
    files = [("talks/keynote.mp4", 1024), ("slides/deck.pdf", 512)]
    engine.configure_manifest(_make_manifest(files))
    result = process_metainfo(b"torrent", archive_root=tmp_path, adapter=engine)

    # audio/extra.mp3 is not in the torrent
    selected = {"talks/keynote.mp4", "slides/deck.pdf", "audio/extra.mp3"}
    covered, uncovered = coverage_map(result.manifest, selected)

    assert "talks/keynote.mp4" in covered
    assert "slides/deck.pdf" in covered
    assert "audio/extra.mp3" in uncovered


def test_t004_uncovered_not_in_covered(tmp_path: Path) -> None:
    """Covered and uncovered must be disjoint."""
    engine = FakeTorrentEngine()
    files = [("talks/keynote.mp4", 1024)]
    engine.configure_manifest(_make_manifest(files))
    result = process_metainfo(b"torrent", archive_root=tmp_path, adapter=engine)

    selected = {"talks/keynote.mp4", "missing/file.mp3"}
    covered, uncovered = coverage_map(result.manifest, selected)

    assert covered & uncovered == set()


def test_t004_directory_prefix_coverage(tmp_path: Path) -> None:
    """Selecting a directory prefix covers all files under it."""
    engine = FakeTorrentEngine()
    files = [("talks/day1.mp4", 100), ("talks/day2.mp4", 200)]
    engine.configure_manifest(_make_manifest(files))
    result = process_metainfo(b"torrent", archive_root=tmp_path, adapter=engine)

    selected = {"talks/"}
    covered, uncovered = coverage_map(result.manifest, selected)

    assert "talks/" in covered
    assert uncovered == set()
