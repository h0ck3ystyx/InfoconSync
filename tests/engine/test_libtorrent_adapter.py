"""Phase 0 engine integration tests (TE-001 through TE-008).

All tests are loopback-only. No real tracker, DHT, PEX, or LSD is used.
Fixtures are pre-generated synthetic torrents checked into the repository.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from infocon_librarian.domain.errors import InvalidTorrent
from infocon_librarian.domain.models import (
    EngineJobId,
    TorrentProtocol,
    TorrentStartParams,
    TransferState,
)
from infocon_librarian.torrent.libtorrent_adapter import LibtorrentAdapter

from .conftest import HASHES, fixture_bytes, make_seeder, wait_for_state

pytestmark = pytest.mark.engine


# ---------------------------------------------------------------------------
# TE-001: Inspect valid v1 multi-file torrent
# ---------------------------------------------------------------------------

def test_te001_inspect_v1_multi_file() -> None:
    """inspect() returns exact file list, sizes, tracker URLs, and v1 infohash."""
    adapter = LibtorrentAdapter()
    data = fixture_bytes("multi_file_v1")
    manifest = adapter.inspect(data)

    expected = HASHES["multi_file_v1"]
    assert manifest.protocol == TorrentProtocol.V1
    assert manifest.v1_infohash == expected["v1_infohash"]
    assert manifest.v2_infohash is None

    # Exact file list and sizes
    assert len(manifest.files) == len(expected["files"])
    for actual_file, expected_file in zip(
        sorted(manifest.files, key=lambda f: f.relative_path),
        sorted(expected["files"], key=lambda f: f["path"]),
        strict=True,
    ):
        assert actual_file.relative_path == expected_file["path"]
        assert actual_file.size == expected_file["size"]

    assert manifest.total_size == sum(f["size"] for f in expected["files"])
    assert len(manifest.trackers) == 1
    assert "tracker.local" in manifest.trackers[0]


# ---------------------------------------------------------------------------
# TE-002: Inspect hybrid torrent
# ---------------------------------------------------------------------------

def test_te002_inspect_hybrid() -> None:
    """inspect() captures both v1 and v2 infohashes for a hybrid torrent."""
    adapter = LibtorrentAdapter()
    data = fixture_bytes("multi_file_hybrid")
    manifest = adapter.inspect(data)

    expected = HASHES["multi_file_hybrid"]
    assert manifest.protocol == TorrentProtocol.HYBRID
    assert manifest.v1_infohash == expected["v1_infohash"]
    assert manifest.v2_infohash == expected["v2_infohash"]
    assert manifest.v2_infohash is not None
    assert manifest.v1_infohash is not None

    assert len(manifest.files) == len(expected["files"])
    for f in manifest.files:
        match = next((e for e in expected["files"] if e["path"] == f.relative_path), None)
        assert match is not None, f"Unexpected file: {f.relative_path!r}"
        assert f.size == match["size"]


# ---------------------------------------------------------------------------
# TE-003: Inspect malformed bencode
# ---------------------------------------------------------------------------

def test_te003_malformed_truncated() -> None:
    """inspect() raises InvalidTorrent for truncated bencode without touching the network."""
    adapter = LibtorrentAdapter()
    with pytest.raises(InvalidTorrent):
        adapter.inspect(fixture_bytes("malformed_truncated"))


def test_te003_malformed_empty() -> None:
    """inspect() raises InvalidTorrent for an empty file."""
    adapter = LibtorrentAdapter()
    with pytest.raises(InvalidTorrent):
        adapter.inspect(fixture_bytes("malformed_empty"))


def test_te003_path_traversal_rejected() -> None:
    """inspect() raises InvalidTorrent for torrents containing .. path components."""
    adapter = LibtorrentAdapter()
    with pytest.raises(InvalidTorrent, match=r"traversal|path"):
        adapter.inspect(fixture_bytes("malformed_path_traversal"))


# ---------------------------------------------------------------------------
# TE-004: Start with file priorities (loopback swarm)
# ---------------------------------------------------------------------------

def test_te004_file_priorities(tmp_path: Path) -> None:
    """Only selected files exist in the destination after a completed download."""
    torrent_bytes = fixture_bytes("multi_file_v1")
    manifest_info = HASHES["multi_file_v1"]

    # Populate seed data with deterministic content matching the fixture piece hashes
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    _write_fixture_data(seed_dir, manifest_info["files"])

    # Start seeder
    seeder_session, seeder_handle = make_seeder(torrent_bytes, str(seed_dir))

    # Wait for seeder to be ready
    _wait_for_seeder(seeder_session, seeder_handle)

    # Leecher downloads only the first file (index 0)
    download_dir = tmp_path / "download"
    download_dir.mkdir()

    seeder_port = seeder_session.listen_port()
    adapter = LibtorrentAdapter(listen_interfaces="127.0.0.1:0")

    params = TorrentStartParams(
        torrent_bytes=torrent_bytes,
        save_path=str(download_dir),
        selected_indices=(0,),  # only first file
    )
    job_id = adapter.start(params)

    # Connect leecher to seeder manually
    handle = adapter._get_handle(job_id)
    handle.connect_peer(("127.0.0.1", seeder_port))

    prog = wait_for_state(adapter, job_id, {TransferState.COMPLETE}, timeout=30)
    assert prog.state == TransferState.COMPLETE

    # File at torrent index 0 (slides.pdf) should exist; others should not
    files_in_torrent_order = manifest_info["files"]  # list order = torrent file indices
    selected_file = files_in_torrent_order[0]
    downloaded = download_dir / selected_file["path"]
    assert downloaded.exists(), f"Expected selected file to exist: {downloaded}"
    assert downloaded.stat().st_size == selected_file["size"]

    # Skipped files (indices 1, 2) must not be present
    for f in files_in_torrent_order[1:]:
        skipped = download_dir / f["path"]
        assert not skipped.exists(), f"Unexpected file downloaded: {skipped}"

    adapter.remove_keep_data(job_id)
    seeder_session.pause()


# ---------------------------------------------------------------------------
# TE-005: Existing data recheck
# ---------------------------------------------------------------------------

def test_te005_existing_data_recheck(tmp_path: Path) -> None:
    """Valid pieces in existing data are recognized; corrupt bytes are detected."""
    torrent_bytes = fixture_bytes("single_file_v1")
    manifest_info = HASHES["single_file_v1"]

    # Create a directory with WRONG content (corrupt)
    corrupt_dir = tmp_path / "corrupt"
    corrupt_dir.mkdir()
    for f in manifest_info["files"]:
        target = corrupt_dir / f["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\xFF" * f["size"])  # all-FF = invalid content

    adapter = LibtorrentAdapter(listen_interfaces="127.0.0.1:0")
    params = TorrentStartParams(
        torrent_bytes=torrent_bytes,
        save_path=str(corrupt_dir),
        selected_indices=(),
    )
    job_id = adapter.start(params)

    # After the initial recheck the torrent should report 0 valid bytes
    # (all pieces are corrupt) — job will be checking then downloading (no peers)
    # Wait for the checking phase to complete
    wait_for_state(
        adapter, job_id,
        {TransferState.DOWNLOADING, TransferState.PAUSED},
        timeout=15,
    )
    # total_wanted_done should be 0 because all data is corrupt
    final = adapter.poll(job_id)
    assert final.downloaded_bytes == 0, (
        f"Expected 0 valid bytes from corrupt data, got {final.downloaded_bytes}"
    )
    adapter.remove_keep_data(job_id)


# ---------------------------------------------------------------------------
# TE-006: Pause / restart / resume
# ---------------------------------------------------------------------------

def test_te006_pause_restart_resume(tmp_path: Path) -> None:
    """Resume data survives serialization and the job continues to completion."""
    torrent_bytes = fixture_bytes("multi_file_v1")
    manifest_info = HASHES["multi_file_v1"]

    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    _write_fixture_data(seed_dir, manifest_info["files"])

    seeder_session, seeder_handle = make_seeder(torrent_bytes, str(seed_dir))
    _wait_for_seeder(seeder_session, seeder_handle)
    seeder_port = seeder_session.listen_port()

    download_dir = tmp_path / "download"
    download_dir.mkdir()

    adapter = LibtorrentAdapter(listen_interfaces="127.0.0.1:0")
    params = TorrentStartParams(
        torrent_bytes=torrent_bytes,
        save_path=str(download_dir),
        selected_indices=(),
    )
    job_id = adapter.start(params)
    handle = adapter._get_handle(job_id)
    handle.connect_peer(("127.0.0.1", seeder_port))

    # Wait until we have downloaded something, then pause
    _wait_for_nonzero_progress(adapter, job_id, timeout=15)
    adapter.pause(job_id)
    wait_for_state(adapter, job_id, {TransferState.PAUSED}, timeout=10)

    # Save resume data
    resume_bytes = adapter.save_resume_data(job_id)
    assert len(resume_bytes) > 0

    # Remove from engine (simulating app restart)
    adapter.remove_keep_data(job_id)

    # Create a fresh adapter and resume from saved data
    adapter2 = LibtorrentAdapter(listen_interfaces="127.0.0.1:0")
    params2 = TorrentStartParams(
        torrent_bytes=torrent_bytes,
        save_path=str(download_dir),
        selected_indices=(),
        resume_data=resume_bytes,
    )
    job_id2 = adapter2.start(params2)
    handle2 = adapter2._get_handle(job_id2)
    handle2.connect_peer(("127.0.0.1", seeder_port))

    prog = wait_for_state(adapter2, job_id2, {TransferState.COMPLETE}, timeout=30)
    assert prog.state == TransferState.COMPLETE

    adapter2.remove_keep_data(job_id2)
    seeder_session.pause()


# ---------------------------------------------------------------------------
# TE-007: Default privacy settings
# ---------------------------------------------------------------------------

def test_te007_privacy_settings() -> None:
    """DHT, PEX, LSD, UPnP, and NAT-PMP are disabled in default adapter settings."""
    import libtorrent as lt  # type: ignore[import]

    adapter = LibtorrentAdapter()
    settings = adapter.get_settings()

    assert settings["enable_dht"] is False, "DHT must be disabled"
    assert settings["enable_lsd"] is False, "LSD must be disabled"
    assert settings["enable_natpmp"] is False, "NAT-PMP must be disabled"
    assert settings["enable_upnp"] is False, "UPnP must be disabled"

    # Per-torrent flags: add a torrent and verify its flags
    torrent_bytes = fixture_bytes("single_file_v1")
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        params = TorrentStartParams(
            torrent_bytes=torrent_bytes,
            save_path=tmp,
            selected_indices=(),
        )
        job_id = adapter.start(params)
        handle = adapter._get_handle(job_id)
        flags = handle.flags()

        assert flags & lt.torrent_flags.disable_dht, "Per-torrent DHT must be disabled"
        assert flags & lt.torrent_flags.disable_pex, "Per-torrent PEX must be disabled"
        assert flags & lt.torrent_flags.disable_lsd, "Per-torrent LSD must be disabled"
        adapter.remove_keep_data(job_id)


# ---------------------------------------------------------------------------
# TE-008: Seed expiry
# ---------------------------------------------------------------------------

def test_te008_seed_expiry(tmp_path: Path) -> None:
    """A seeding torrent is paused when we remove it (expiry enforced by caller)."""
    torrent_bytes = fixture_bytes("single_file_v1")
    manifest_info = HASHES["single_file_v1"]

    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    _write_fixture_data(seed_dir, manifest_info["files"])

    # Seed the torrent
    adapter = LibtorrentAdapter(listen_interfaces="127.0.0.1:0")
    import libtorrent as lt  # type: ignore[import]

    atp = lt.add_torrent_params()
    ti = lt.torrent_info(torrent_bytes)
    atp.ti = ti
    atp.save_path = str(seed_dir)
    atp.flags = (
        lt.torrent_flags.disable_dht
        | lt.torrent_flags.disable_pex
        | lt.torrent_flags.disable_lsd
        | lt.torrent_flags.seed_mode
    )

    handle = adapter._session.add_torrent(atp)
    job_id = EngineJobId()
    adapter._handles[job_id.value] = handle

    # Verify it's seeding
    time.sleep(0.5)
    prog = adapter.poll(job_id)
    assert prog.state in {TransferState.COMPLETE, TransferState.CHECKING, TransferState.QUEUED}

    # Simulate expiry: pause and remove
    adapter.pause(job_id)
    wait_for_state(adapter, job_id, {TransferState.PAUSED}, timeout=5)
    adapter.remove_keep_data(job_id)

    # Data must still be present on disk
    for f in manifest_info["files"]:
        assert (seed_dir / f["path"]).exists(), f"File was deleted after seed expiry: {f['path']}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_fixture_data(root: Path, files_spec: list[dict]) -> None:
    """Write deterministic content matching what generate-test-torrents.py produces."""
    import struct

    def _content(seed: int, size: int) -> bytes:
        rng = seed
        chunks = []
        remaining = size
        while remaining > 0:
            rng = (rng * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
            chunk = struct.pack(">Q", rng)
            take = min(8, remaining)
            chunks.append(chunk[:take])
            remaining -= take
        return b"".join(chunks)

    # Map path -> seed from the fixture spec
    seed_map = {
        "single_file_v1/talk.mp4": 1,
        "multi_file_v1/slides.pdf": 10,
        "multi_file_v1/audio/talk.mp3": 11,
        "multi_file_v1/video/talk.mp4": 12,
        "multi_file_hybrid/slides.pdf": 20,
        "multi_file_hybrid/talk.mp4": 21,
    }

    for spec in files_spec:
        target = root / spec["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        seed = seed_map.get(spec["path"], 42)
        target.write_bytes(_content(seed, spec["size"]))


def _wait_for_seeder(session, handle, timeout: float = 10.0) -> None:
    """Wait for a seeder session to finish checking and start seeding."""
    import libtorrent as lt  # type: ignore[import]

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session.pop_alerts()
        ts = handle.status()
        if ts.state in (
            lt.torrent_status.seeding,
            lt.torrent_status.finished,
        ) or ts.is_seeding:
            return
        time.sleep(0.1)
    raise TimeoutError("Seeder did not become ready")


def _wait_for_nonzero_progress(adapter: LibtorrentAdapter, job_id, timeout: float = 15.0) -> None:
    """Wait until the job has downloaded at least one byte."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        prog = adapter.poll(job_id)
        if prog.downloaded_bytes > 0:
            return
        if prog.state == TransferState.COMPLETE:
            return
        time.sleep(0.1)
