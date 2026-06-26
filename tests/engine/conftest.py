"""Shared fixtures for engine integration tests."""
from __future__ import annotations

import json
import time
from pathlib import Path

import libtorrent as lt  # type: ignore[import]
import pytest

from infocon_librarian.torrent.libtorrent_adapter import LibtorrentAdapter

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "torrents"
HASHES = json.loads((FIXTURES_DIR / "hashes.json").read_text())


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES_DIR / HASHES[name]["filename"]).read_bytes()


def wait_for_state(
    adapter: LibtorrentAdapter,
    job_id,
    target_states: set,
    *,
    timeout: float = 30.0,
    poll_interval: float = 0.1,
) -> TransferProgress:  # noqa: F821
    """Poll adapter.poll() until the job reaches one of target_states."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        prog = adapter.poll(job_id)
        if prog.state in target_states:
            return prog
        time.sleep(poll_interval)
    prog = adapter.poll(job_id)
    raise TimeoutError(
        f"Job did not reach {target_states!r} within {timeout}s; "
        f"last state={prog.state!r}, error={prog.last_error!r}"
    )


def make_seeder(torrent_bytes: bytes, data_dir: str) -> tuple[lt.session, lt.torrent_handle]:
    """Create a loopback-only seeder session for integration tests."""
    settings: dict = {
        "enable_dht": False,
        "enable_lsd": False,
        "enable_natpmp": False,
        "enable_upnp": False,
        "listen_interfaces": "127.0.0.1:0",
        "alert_mask": (
            lt.alert_category.status
            | lt.alert_category.storage
            | lt.alert_category.error
        ),
    }
    session = lt.session(settings)

    atp = lt.add_torrent_params()
    ti = lt.torrent_info(torrent_bytes)
    atp.ti = ti
    atp.save_path = data_dir
    atp.flags = (
        lt.torrent_flags.disable_dht
        | lt.torrent_flags.disable_pex
        | lt.torrent_flags.disable_lsd
        | lt.torrent_flags.seed_mode  # trust existing data
    )

    handle = session.add_torrent(atp)
    return session, handle


@pytest.fixture()
def tmp_download_dir(tmp_path: Path) -> Path:
    d = tmp_path / "download"
    d.mkdir()
    return d
