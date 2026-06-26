"""T-005, T-006, T-008 — VerifyService with real libtorrent (loopback only)."""
from __future__ import annotations

import struct
import time
from pathlib import Path

import pytest

from infocon_librarian.domain.models import TransferState, VerificationLevel
from infocon_librarian.services.verify_service import verify
from infocon_librarian.torrent.libtorrent_adapter import LibtorrentAdapter
from tests.engine.conftest import HASHES, fixture_bytes, make_seeder, wait_for_state

pytestmark = pytest.mark.timeout(120)

_MULTI_V1 = "multi_file_v1"

_SEED_MAP = {
    "single_file_v1/talk.mp4": 1,
    "multi_file_v1/slides.pdf": 10,
    "multi_file_v1/audio/talk.mp3": 11,
    "multi_file_v1/video/talk.mp4": 12,
    "multi_file_hybrid/slides.pdf": 20,
    "multi_file_hybrid/talk.mp4": 21,
}


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


def _write_fixture_data(root: Path, files_spec: list[dict]) -> None:
    for spec in files_spec:
        target = root / spec["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        seed = _SEED_MAP.get(spec["path"], 42)
        target.write_bytes(_content(seed, spec["size"]))


# ---------------------------------------------------------------------------
# T-005: Existing valid data → piece-verified, no redownload
# ---------------------------------------------------------------------------


def test_t005_existing_valid_data_piece_verified(tmp_path: Path) -> None:
    manifest_info = HASHES[_MULTI_V1]
    torrent_bytes = fixture_bytes(_MULTI_V1)

    # Write the deterministic fixture data so verify finds all pieces valid
    _write_fixture_data(tmp_path, manifest_info["files"])

    adapter = LibtorrentAdapter(listen_interfaces="127.0.0.1:0")
    try:
        result = verify(
            torrent_bytes,
            archive_root=tmp_path,
            adapter=adapter,
            timeout=60.0,
            poll_interval=0.1,
        )

        assert result.verification_level == VerificationLevel.PIECE_VERIFIED
        assert result.error is None
    finally:
        adapter.close()


# ---------------------------------------------------------------------------
# T-006: Existing corrupt data → recheck detects corruption
# ---------------------------------------------------------------------------


def test_t006_corrupt_data_not_verified(tmp_path: Path) -> None:
    torrent_bytes = fixture_bytes(_MULTI_V1)
    manifest_info = HASHES[_MULTI_V1]

    # Write all files but corrupt the first one (wrong bytes, same size)
    _write_fixture_data(tmp_path, manifest_info["files"])
    first_file_path = tmp_path / manifest_info["files"][0]["path"]
    first_file_path.write_bytes(b"\x00" * manifest_info["files"][0]["size"])

    adapter = LibtorrentAdapter(listen_interfaces="127.0.0.1:0")
    try:
        # With some corrupt pieces and no peers, it will not verify or will time out
        result = verify(
            torrent_bytes,
            archive_root=tmp_path,
            adapter=adapter,
            timeout=10.0,
            poll_interval=0.1,
        )

        # Corrupt data must NOT yield piece-verified
        assert result.verification_level != VerificationLevel.PIECE_VERIFIED
    finally:
        adapter.close()


# ---------------------------------------------------------------------------
# T-008: Pause and app restart — resume blob saved, job continues
# ---------------------------------------------------------------------------


def test_t008_pause_save_resume_restart(tmp_path: Path) -> None:
    seed_dir = tmp_path / "seed"
    download_dir = tmp_path / "download"
    seed_dir.mkdir()
    download_dir.mkdir()

    manifest_info = HASHES[_MULTI_V1]
    torrent_bytes = fixture_bytes(_MULTI_V1)

    # Populate seed dir with real fixture data
    _write_fixture_data(seed_dir, manifest_info["files"])
    seeder_sess, seeder_handle = make_seeder(torrent_bytes, str(seed_dir))
    time.sleep(0.3)
    seeder_port = seeder_sess.listen_port()

    # First adapter: start download, pause mid-way, save resume
    adapter1 = LibtorrentAdapter(listen_interfaces="127.0.0.1:0")
    try:
        from infocon_librarian.domain.models import TorrentStartParams

        params = TorrentStartParams(
            torrent_bytes=torrent_bytes,
            save_path=str(download_dir),
            selected_indices=tuple(range(len(manifest_info["files"]))),
            enable_dht=False,
            enable_pex=False,
            enable_lsd=False,
            enable_upnp=False,
            enable_natpmp=False,
        )
        job_id1 = adapter1.start(params)

        # Connect to seeder and let some data transfer
        h = adapter1._handles[job_id1.value]
        h.connect_peer(("127.0.0.1", seeder_port))
        time.sleep(0.5)

        # Pause and save resume data
        adapter1.pause(job_id1)
        time.sleep(0.2)
        resume_data = adapter1.save_resume_data(job_id1)
        assert len(resume_data) > 0

        adapter1.remove_keep_data(job_id1)
    finally:
        adapter1.close()

    # Second adapter: restore from resume, continue to completion
    adapter2 = LibtorrentAdapter(listen_interfaces="127.0.0.1:0")
    try:
        from infocon_librarian.domain.models import TorrentStartParams

        params2 = TorrentStartParams(
            torrent_bytes=torrent_bytes,
            save_path=str(download_dir),
            selected_indices=tuple(range(len(manifest_info["files"]))),
            enable_dht=False,
            enable_pex=False,
            enable_lsd=False,
            enable_upnp=False,
            enable_natpmp=False,
            resume_data=resume_data,
        )
        job_id2 = adapter2.start(params2)

        h2 = adapter2._handles[job_id2.value]
        h2.connect_peer(("127.0.0.1", seeder_port))

        prog = wait_for_state(
            adapter2,
            job_id2,
            {TransferState.COMPLETE},
            timeout=60.0,
        )
        assert prog.state == TransferState.COMPLETE
    finally:
        adapter2.close()
        seeder_sess.remove_torrent(seeder_handle)
