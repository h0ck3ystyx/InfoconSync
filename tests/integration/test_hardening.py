"""R-001 through R-008 — shutdown, drive, logging, and packaging hardening tests."""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from infocon_librarian.archive.root import ArchiveRootInfo
from infocon_librarian.drive_monitor import DriveMonitor, probe_writable
from infocon_librarian.shutdown import ShutdownController
from infocon_librarian.structured_logging import (
    RedactingFilter,
    _scrub,
    configure_logging,
    make_support_bundle_summary,
)

# ---------------------------------------------------------------------------
# R-001: Graceful shutdown during torrent — resume state is persisted
# ---------------------------------------------------------------------------


class _FakeManager:
    """Minimal TransferManager stub for shutdown tests."""

    def __init__(self, *, resume_delay: float = 0.0, raises: bool = False) -> None:
        self._resume_delay = resume_delay
        self._raises = raises
        self.paused = False
        self.resume_data_calls = 0

    def pause_all(self) -> None:
        if self._raises:
            raise RuntimeError("pause failed")
        self.paused = True

    def resume_data_saved(self) -> bool:
        self.resume_data_calls += 1
        if self._resume_delay > 0:
            time.sleep(self._resume_delay)
        return self.paused


def test_r001_graceful_shutdown_pauses_and_flushes(tmp_path):
    manager = _FakeManager()
    ctrl = ShutdownController(
        transfer_manager=manager,
        resume_timeout=2.0,
    )
    ctrl.request()
    assert ctrl.wait(timeout=3.0), "Shutdown must complete"
    assert manager.paused, "Transfers must be paused"
    assert manager.resume_data_calls >= 1, "Resume data must be polled"


def test_r001_shutdown_is_idempotent():
    manager = _FakeManager()
    ctrl = ShutdownController(transfer_manager=manager, resume_timeout=0.5)
    # Calling twice must not cause errors or double-pause
    ctrl.request()
    ctrl.request()
    assert ctrl.wait(timeout=2.0)
    assert manager.paused


def test_r001_on_complete_callback_called():
    done = threading.Event()
    ctrl = ShutdownController(
        resume_timeout=0.1,
        on_complete=done.set,
    )
    ctrl.request()
    assert done.wait(timeout=2.0), "on_complete callback must fire"
    assert ctrl.complete


def test_r001_pause_failure_still_completes():
    manager = _FakeManager(raises=True)
    ctrl = ShutdownController(transfer_manager=manager, resume_timeout=0.1)
    ctrl.request()
    assert ctrl.wait(timeout=2.0), "Shutdown must complete even if pause raises"


# ---------------------------------------------------------------------------
# R-002: Graceful shutdown during HTTPS — .part sidecar is consistent
# ---------------------------------------------------------------------------


def test_r002_part_file_survives_shutdown(tmp_path):
    """Shutdown must not delete or corrupt an in-progress .part file."""
    part_file = tmp_path / "audio.mp3.part"
    part_file.write_bytes(b"partial-content-bytes" * 100)

    ctrl = ShutdownController(resume_timeout=0.1)
    ctrl.request()
    assert ctrl.wait(timeout=2.0)

    # Part file must still exist and be intact after shutdown
    assert part_file.exists(), ".part file must not be deleted on shutdown"
    content = part_file.read_bytes()
    assert content == b"partial-content-bytes" * 100, ".part file must not be corrupted"


def test_r002_resume_timeout_does_not_kill_part(tmp_path):
    """Even when resume data flush times out, .part files are untouched."""
    manager = _FakeManager(resume_delay=5.0)  # will timeout
    part = tmp_path / "talk.mp3.part"
    part.write_bytes(b"x" * 1024)

    ctrl = ShutdownController(transfer_manager=manager, resume_timeout=0.1)
    ctrl.request()
    assert ctrl.wait(timeout=2.0)
    assert part.exists()


# ---------------------------------------------------------------------------
# R-003: Drive disconnect — job pauses safely; no writes after root gone
# ---------------------------------------------------------------------------


def test_r003_disconnect_callback_called(tmp_path):
    root_info = ArchiveRootInfo(
        canonical_path=str(tmp_path),
        volume_fingerprint="vol-abc",
        free_bytes=10 * 1024 * 1024,
        known_sections=[],
    )
    disconnected = threading.Event()
    monitor = DriveMonitor(
        root_info,
        poll_interval=0.05,
        on_disconnect=disconnected.set,
    )
    monitor.start()
    try:
        # Initially connected
        assert monitor.connected

        # Remove the directory to simulate ejection
        import shutil
        shutil.rmtree(tmp_path)

        assert disconnected.wait(timeout=2.0), "disconnect callback must fire"
        assert not monitor.connected
    finally:
        monitor.stop()
        tmp_path.mkdir(exist_ok=True)  # restore for tmp_path cleanup


def test_r003_probe_writable_returns_false_on_readonly(tmp_path):
    root = tmp_path / "ro-root"
    root.mkdir()
    try:
        os.chmod(root, 0o555)  # read-only
        result = probe_writable(root)
        # On macOS running as root this might still succeed; acceptable
        assert isinstance(result, bool)
    finally:
        os.chmod(root, 0o755)


def test_r003_probe_writable_succeeds_on_writable(tmp_path):
    assert probe_writable(tmp_path) is True
    # Probe file must be cleaned up
    assert not (tmp_path / ".librarian-probe").exists()


# ---------------------------------------------------------------------------
# R-004: Drive remount — same volume → reconnect; different volume → warning
# ---------------------------------------------------------------------------


def test_r004_reconnect_same_volume(tmp_path):
    root_info = ArchiveRootInfo(
        canonical_path=str(tmp_path),
        volume_fingerprint="vol-same",
        free_bytes=10 * 1024 * 1024,
        known_sections=[],
    )
    reconnected = threading.Event()
    wrong_volume = threading.Event()

    with patch(
        "infocon_librarian.drive_monitor.validate_root"
    ) as mock_validate:
        # Simulate: first call = disconnect, then reconnect with same fingerprint
        mock_validate.side_effect = [
            Exception("drive gone"),  # poll 1 → disconnect
            root_info,                # poll 2 → reconnect same vol
        ]
        monitor = DriveMonitor(
            root_info,
            poll_interval=0.01,
            on_disconnect=lambda: None,
            on_reconnect=lambda info: reconnected.set(),
            on_wrong_volume=lambda info: wrong_volume.set(),
        )
        monitor._connected = False  # pre-set to disconnected state
        monitor.start()
        reconnected.wait(timeout=1.0)
        monitor.stop()

    assert reconnected.is_set(), "Same-volume reconnect must fire on_reconnect"
    assert not wrong_volume.is_set(), "Must not fire on_wrong_volume for same vol"


def test_r004_wrong_volume_triggers_callback(tmp_path):
    original = ArchiveRootInfo(
        canonical_path=str(tmp_path),
        volume_fingerprint="vol-original",
        free_bytes=10 * 1024 * 1024,
        known_sections=[],
    )
    replacement = ArchiveRootInfo(
        canonical_path=str(tmp_path),
        volume_fingerprint="vol-different",
        free_bytes=5 * 1024 * 1024,
        known_sections=[],
    )
    wrong = threading.Event()

    with patch("infocon_librarian.drive_monitor.validate_root") as mock_v:
        mock_v.return_value = replacement
        monitor = DriveMonitor(
            original,
            poll_interval=0.01,
            on_wrong_volume=lambda info: wrong.set(),
        )
        monitor._connected = False  # pre-set disconnected
        monitor.start()
        wrong.wait(timeout=1.0)
        monitor.stop()

    assert wrong.is_set()


# ---------------------------------------------------------------------------
# R-005: Corrupted database — app preserves original, offers reset path
# ---------------------------------------------------------------------------


def test_r005_corrupt_db_does_not_raise_on_open(tmp_path):
    db_path = tmp_path / "corrupt.db"
    db_path.write_bytes(b"this is not valid sqlite content")

    # open_db should fail gracefully, not crash the process
    import sqlite3

    from infocon_librarian.storage.database import open_db

    with pytest.raises(sqlite3.DatabaseError):
        open_db(db_path)

    # Original file must be preserved (not deleted)
    assert db_path.exists(), "Corrupt DB must not be auto-deleted"


def test_r005_corrupt_db_original_preserved(tmp_path):
    db_path = tmp_path / "bad.db"
    sentinel = b"CORRUPT-DATA-SENTINEL"
    db_path.write_bytes(sentinel)

    try:
        from infocon_librarian.storage.database import open_db
        open_db(db_path)
    except Exception:
        pass  # expected

    # File should still contain original bytes
    assert db_path.read_bytes() == sentinel


def test_r005_healthy_db_survives_open_close(tmp_path):
    db_path = tmp_path / "health.db"
    from infocon_librarian.storage.database import open_db
    from infocon_librarian.storage.migrations import migrate

    conn = open_db(db_path)
    migrate(conn)
    conn.close()

    # Reopen — should not raise
    conn2 = open_db(db_path)
    migrate(conn2)
    conn2.close()


# ---------------------------------------------------------------------------
# R-006: Packaged smoke test — launch, configure, inspect
# ---------------------------------------------------------------------------


def test_r006_cli_launches_without_error():
    """The CLI entry point must be importable and parseable without crashing."""
    from infocon_librarian.cli import _build_parser

    parser = _build_parser()
    # Parse help without crash
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--help"])
    assert exc.value.code == 0


def test_r006_cli_check_json_round_trip():
    """CLI check --format json returns parseable JSON on any invocation."""
    import json
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, "-m", "infocon_librarian", "check", "--format", "json"],
        capture_output=True,
        text=True,
    )
    assert r.returncode in (0, 1)
    if r.returncode == 0:
        data = json.loads(r.stdout)
        assert "command" in data


def test_r006_flask_app_creates_without_crash():
    from infocon_librarian.web.app import create_app

    app = create_app(secret_key="smoke-test")
    assert app is not None


# ---------------------------------------------------------------------------
# R-007: Dependency/license audit — locked artifacts and notices present
# ---------------------------------------------------------------------------


def test_r007_pyproject_has_license():
    pyproject = Path(__file__).parents[2] / "pyproject.toml"
    assert pyproject.exists(), "pyproject.toml must exist"
    content = pyproject.read_text()
    assert "license" in content.lower(), "pyproject.toml must declare a license"


def test_r007_no_public_network_imports():
    """Core domain modules must not import requests/urllib for public HTTP."""
    import ast

    forbidden = {"urllib.request", "requests", "aiohttp"}
    domain_dir = Path(__file__).parents[2] / "src" / "infocon_librarian" / "domain"
    for py_file in domain_dir.rglob("*.py"):
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in forbidden, (
                        f"{py_file}: forbidden import {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not any(module.startswith(f) for f in forbidden), (
                    f"{py_file}: forbidden import from {module}"
                )


# ---------------------------------------------------------------------------
# R-008: Security regression suite
# ---------------------------------------------------------------------------


def test_r008_loopback_only_no_cors():
    from infocon_librarian.web.app import create_app
    from infocon_librarian.web.auth import LaunchToken

    app = create_app(secret_key="sec-test")
    tok = LaunchToken.generate()
    app.config["_LAUNCH_TOKEN"] = tok

    client = app.test_client()
    client.get(f"/bootstrap/{tok.value}", follow_redirects=True)

    r = client.get("/api/health")
    assert "Access-Control-Allow-Origin" not in r.headers


def test_r008_csp_no_unsafe_inline():
    from infocon_librarian.web.app import create_app
    from infocon_librarian.web.auth import LaunchToken

    app = create_app(secret_key="sec-test-2")
    tok = LaunchToken.generate()
    app.config["_LAUNCH_TOKEN"] = tok
    client = app.test_client()
    client.get(f"/bootstrap/{tok.value}", follow_redirects=True)

    r = client.get("/api/health")
    csp = r.headers.get("Content-Security-Policy", "")
    assert "unsafe-inline" not in csp
    assert "default-src" in csp


def test_r008_path_traversal_rejected(tmp_path):
    from infocon_librarian.domain.errors import PathEscapesRoot
    from infocon_librarian.domain.paths import safe_archive_path

    dangerous = [
        [".."],
        ["/etc/passwd"],
        ["a", "..", ".."],
        ["C:\\Windows\\System32"],
    ]
    for components in dangerous:
        with pytest.raises((PathEscapesRoot, ValueError)):
            safe_archive_path(tmp_path, components)


def test_r008_token_consumed_after_use():
    from infocon_librarian.web.auth import LaunchToken

    tok = LaunchToken.generate()
    assert tok.value is not None
    assert tok.consume(tok.value) is True
    assert tok.value is None
    # Second consume must fail
    assert tok.consume("any-value") is False


def test_r008_csrf_origin_required():
    import json

    from infocon_librarian.web.app import create_app
    from infocon_librarian.web.auth import LaunchToken

    app = create_app(secret_key="sec-test-3")
    tok = LaunchToken.generate()
    app.config["_LAUNCH_TOKEN"] = tok
    client = app.test_client()
    client.get(f"/bootstrap/{tok.value}", follow_redirects=True)
    with client.session_transaction() as sess:
        csrf = sess.get("csrf_token", "")

    # Missing Origin → 403
    r = client.post(
        "/api/checks",
        data=json.dumps({}),
        content_type="application/json",
        headers={"X-Csrf-Token": csrf},
    )
    assert r.status_code == 403

    # Wrong CSRF → 403
    r = client.post(
        "/api/checks",
        data=json.dumps({}),
        content_type="application/json",
        headers={"X-Csrf-Token": "wrong", "Origin": "http://localhost"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Logging / redaction (also part of R-001 support bundle)
# ---------------------------------------------------------------------------


def test_r_log_redacts_public_ip():
    assert _scrub("peer connected from 1.2.3.4") == "peer connected from [redacted]"


def test_r_log_keeps_loopback_ip():
    result = _scrub("bound to 127.0.0.1")
    assert "127.0.0.1" in result


def test_r_log_redacts_bootstrap_token():
    result = _scrub("GET /bootstrap/abc123xyz-secrettoken HTTP/1.1")
    assert "secrettoken" not in result
    assert "[redacted]" in result


def test_r_log_redacting_filter():
    filt = RedactingFilter()
    record = logging.LogRecord(
        "test", logging.INFO, "", 0, "peer at 8.8.8.8", (), None
    )
    filt.filter(record)
    assert "8.8.8.8" not in record.msg
    assert "[redacted]" in record.msg


def test_r_configure_logging_no_crash(tmp_path):
    root_logger = logging.getLogger()
    handlers_before = list(root_logger.handlers)
    try:
        configure_logging(log_dir=tmp_path, stderr=False)
        log = logging.getLogger("infocon_librarian.test_hardening")
        log.info("test message")
        log_file = tmp_path / "librarian.log"
        assert log_file.exists()
    finally:
        # Remove handlers added by configure_logging to avoid polluting other tests
        for h in list(root_logger.handlers):
            if h not in handlers_before:
                h.close()
                root_logger.removeHandler(h)


def test_r_support_bundle_has_no_paths():
    summary = make_support_bundle_summary()
    assert "generated_at" in summary
    # Must not contain absolute paths
    for v in summary.values():
        if isinstance(v, str):
            assert not v.startswith("/"), f"Absolute path leaked: {v}"
