"""T-007, T-009 through T-012 — TransferManager state machine."""
from __future__ import annotations

from infocon_librarian.domain.models import (
    EngineJobId,
    TorrentStartParams,
    TransferProgress,
    TransferState,
)
from infocon_librarian.transfer.manager import TransferManager
from tests.support.fake_engine import FakeTorrentEngine


def _params() -> TorrentStartParams:
    return TorrentStartParams(
        torrent_bytes=b"fake",
        save_path="/tmp/archive",
        selected_indices=(0,),
        enable_dht=False,
        enable_pex=False,
        enable_lsd=False,
        enable_upnp=False,
        enable_natpmp=False,
    )


def _progress(engine_id: EngineJobId, state: TransferState, num_peers: int = 1) -> TransferProgress:
    return TransferProgress(
        job_id=engine_id,
        state=state,
        total_bytes=1024,
        downloaded_bytes=512 if state == TransferState.DOWNLOADING else 0,
        uploaded_bytes=0,
        download_rate=0,
        upload_rate=0,
        num_peers=num_peers,
        last_error=None,
    )


def _make_manager(engine: FakeTorrentEngine, no_peer_threshold: int = 3) -> TransferManager:
    return TransferManager(engine, coalesce_window=0.0, no_peer_threshold=no_peer_threshold)


# ---------------------------------------------------------------------------
# T-007: Privacy controls — adapter receives DHT/PEX/LSD disabled params
# ---------------------------------------------------------------------------


def test_t007_privacy_flags_disabled_by_default() -> None:
    engine = FakeTorrentEngine()
    manager = _make_manager(engine)
    manager.start_job("job-1", _params())

    assert len(engine.started_params) == 1
    p = engine.started_params[0]
    assert p.enable_dht is False
    assert p.enable_pex is False
    assert p.enable_lsd is False
    assert p.enable_upnp is False
    assert p.enable_natpmp is False


def test_t007_privacy_params_pass_through() -> None:
    engine = FakeTorrentEngine()
    manager = _make_manager(engine)
    custom = TorrentStartParams(
        torrent_bytes=b"t",
        save_path="/archive",
        selected_indices=(0,),
        enable_dht=False,
        enable_pex=False,
        enable_lsd=False,
        enable_upnp=False,
        enable_natpmp=False,
        download_limit=512 * 1024,
    )
    manager.start_job("job-1", custom)

    assert engine.started_params[0].download_limit == 512 * 1024


# ---------------------------------------------------------------------------
# T-009: Completion alert without final recheck → NOT piece-verified
# ---------------------------------------------------------------------------


def test_t009_download_complete_not_verified_until_recheck() -> None:
    engine = FakeTorrentEngine()
    manager = _make_manager(engine)
    engine_id = manager.start_job("job-1", _params())

    # Set up sequence: CHECKING → DOWNLOADING → COMPLETE (download done)
    engine.configure_job_sequence(
        engine_id,
        [
            _progress(engine_id, TransferState.CHECKING),
            _progress(engine_id, TransferState.DOWNLOADING),
            _progress(engine_id, TransferState.COMPLETE),
        ],
    )

    # Tick through CHECKING and DOWNLOADING
    engine.advance(engine_id)
    manager.process_tick("job-1")
    engine.advance(engine_id)
    manager.process_tick("job-1")

    # Download COMPLETE — manager should trigger recheck, NOT mark verified
    engine.advance(engine_id)
    manager.process_tick("job-1")

    assert not manager.is_piece_verified("job-1")
    assert engine.was_recheck_called(engine_id)


def test_t009_recheck_triggered_on_first_complete() -> None:
    engine = FakeTorrentEngine()
    manager = _make_manager(engine)
    engine_id = manager.start_job("job-1", _params())

    engine.configure_job_sequence(
        engine_id,
        [_progress(engine_id, TransferState.COMPLETE)],
    )
    manager.process_tick("job-1")

    assert engine.was_recheck_called(engine_id)
    assert not manager.is_piece_verified("job-1")


# ---------------------------------------------------------------------------
# T-010: Final recheck succeeds → marked piece-verified
# ---------------------------------------------------------------------------


def test_t010_piece_verified_after_recheck_success() -> None:
    engine = FakeTorrentEngine()
    manager = _make_manager(engine)
    engine_id = manager.start_job("job-1", _params())

    # Sequence: CHECKING → DOWNLOADING → COMPLETE (download done)
    engine.configure_job_sequence(
        engine_id,
        [
            _progress(engine_id, TransferState.CHECKING),
            _progress(engine_id, TransferState.DOWNLOADING),
            _progress(engine_id, TransferState.COMPLETE),
        ],
        recheck_result=True,
    )

    # Advance to COMPLETE (download)
    engine.advance(engine_id)
    manager.process_tick("job-1")
    engine.advance(engine_id)
    manager.process_tick("job-1")
    engine.advance(engine_id)
    manager.process_tick("job-1")  # triggers recheck; appends CHECKING + COMPLETE

    # FakeEngine.recheck() appended CHECKING and COMPLETE states
    engine.advance(engine_id)
    manager.process_tick("job-1")  # CHECKING (recheck)
    engine.advance(engine_id)
    manager.process_tick("job-1")  # COMPLETE (recheck success)

    assert manager.is_piece_verified("job-1")
    assert manager.get_state("job-1") == TransferState.COMPLETE


def test_t010_final_recheck_failure_not_verified() -> None:
    engine = FakeTorrentEngine()
    manager = _make_manager(engine)
    engine_id = manager.start_job("job-1", _params())

    # Configure recheck to fail
    engine.configure_job_sequence(
        engine_id,
        [_progress(engine_id, TransferState.COMPLETE)],
        recheck_result=False,  # recheck will fail
    )

    manager.process_tick("job-1")  # download COMPLETE → trigger recheck

    # Advance through CHECKING → FAILED (recheck failure)
    engine.advance(engine_id)
    manager.process_tick("job-1")
    engine.advance(engine_id)
    manager.process_tick("job-1")

    assert not manager.is_piece_verified("job-1")
    assert manager.get_state("job-1") == TransferState.FAILED


# ---------------------------------------------------------------------------
# T-011: Tracker failure / no peers → AWAITING_USER_FALLBACK; no HTTPS job
# ---------------------------------------------------------------------------


def test_t011_no_peers_triggers_awaiting_fallback() -> None:
    engine = FakeTorrentEngine()
    manager = _make_manager(engine, no_peer_threshold=2)
    engine_id = manager.start_job("job-1", _params())

    # Sequence: all DOWNLOADING with 0 peers
    downloading_no_peers = _progress(engine_id, TransferState.DOWNLOADING, num_peers=0)
    engine.configure_job_sequence(
        engine_id,
        [downloading_no_peers, downloading_no_peers, downloading_no_peers],
    )

    manager.process_tick("job-1")  # 1st tick (no_peer_ticks = 1)
    engine.advance(engine_id)
    manager.process_tick("job-1")  # 2nd tick (no_peer_ticks = 2 → threshold met)

    assert manager.get_state("job-1") == TransferState.AWAITING_USER_FALLBACK


def test_t011_no_automatic_https_job_created() -> None:
    engine = FakeTorrentEngine()
    manager = _make_manager(engine, no_peer_threshold=1)
    engine_id = manager.start_job("job-1", _params())

    engine.configure_job_sequence(
        engine_id,
        [_progress(engine_id, TransferState.DOWNLOADING, num_peers=0)],
    )
    manager.process_tick("job-1")

    # Manager has only one job — no HTTP job was spun up
    assert len(engine.started_params) == 1


def test_t011_peers_arriving_resets_counter() -> None:
    engine = FakeTorrentEngine()
    manager = _make_manager(engine, no_peer_threshold=3)
    engine_id = manager.start_job("job-1", _params())

    no_peer = _progress(engine_id, TransferState.DOWNLOADING, num_peers=0)
    with_peer = _progress(engine_id, TransferState.DOWNLOADING, num_peers=1)
    engine.configure_job_sequence(engine_id, [no_peer, no_peer, with_peer, no_peer])

    manager.process_tick("job-1")  # no_peer_ticks=1
    engine.advance(engine_id)
    manager.process_tick("job-1")  # no_peer_ticks=2
    engine.advance(engine_id)
    manager.process_tick("job-1")  # peer arrives → counter reset
    engine.advance(engine_id)
    manager.process_tick("job-1")  # no_peer_ticks=1 (not yet threshold)

    # Should still be DOWNLOADING, not AWAITING_USER_FALLBACK
    assert manager.get_state("job-1") == TransferState.DOWNLOADING


# ---------------------------------------------------------------------------
# T-012: Engine alert flood → manager coalesces, DB not flooded
# ---------------------------------------------------------------------------


def test_t012_coalescing_suppresses_duplicate_writes() -> None:
    engine = FakeTorrentEngine()
    # Use a long coalesce window so all rapid polls are suppressed
    manager = TransferManager(engine, coalesce_window=60.0)
    engine_id = manager.start_job("job-1", _params())

    downloading = _progress(engine_id, TransferState.DOWNLOADING)
    engine.configure_job_sequence(engine_id, [downloading] * 20)

    # First tick writes the state change
    manager.process_tick("job-1")
    writes_after_first = manager._coalescer.write_count

    # 19 more ticks with the same DOWNLOADING state — all suppressed
    for _ in range(19):
        manager.process_tick("job-1")

    # write_count must not have increased significantly
    assert manager._coalescer.write_count == writes_after_first


def test_t012_state_change_breaks_coalesce() -> None:
    engine = FakeTorrentEngine()
    manager = TransferManager(engine, coalesce_window=60.0)
    engine_id = manager.start_job("job-1", _params())

    engine.configure_job_sequence(
        engine_id,
        [
            _progress(engine_id, TransferState.CHECKING),
            _progress(engine_id, TransferState.DOWNLOADING),
        ],
    )

    manager.process_tick("job-1")   # CHECKING → written
    engine.advance(engine_id)
    manager.process_tick("job-1")   # DOWNLOADING → state change → written despite window

    history = manager.state_history("job-1")
    assert TransferState.CHECKING in history
    assert TransferState.DOWNLOADING in history
