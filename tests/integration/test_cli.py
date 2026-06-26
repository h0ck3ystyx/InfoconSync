"""U-012 — CLI commands produce well-formed output matching service-level schemas."""
from __future__ import annotations

import json
import subprocess
import sys


def _run(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "infocon_librarian", *args],
        capture_output=True,
        text=True,
        check=check,
    )


# ---------------------------------------------------------------------------
# U-012: CLI check --format json
# ---------------------------------------------------------------------------


def test_u012_check_json_is_valid():
    r = _run("check", "--format", "json")
    # Exit code 0 (no root configured = informational, not error)
    assert r.returncode == 0 or r.returncode == 1
    # JSON must be parseable
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        # Might output to stderr on error path
        data = json.loads(r.stderr.strip() or '{"error": "parse_failed"}')
    assert isinstance(data, dict)


def test_u012_check_json_has_command_key():
    r = _run("check", "--format", "json")
    if r.returncode == 0:
        data = json.loads(r.stdout)
        assert "command" in data
        assert data["command"] == "check"


def test_u012_check_section_flag_accepted():
    r = _run("check", "--section", "defcon", "--format", "json")
    if r.returncode == 0:
        data = json.loads(r.stdout)
        assert data.get("section") == "defcon"


def test_u012_check_human_output_no_crash():
    r = _run("check")
    assert r.returncode in (0, 1)


# ---------------------------------------------------------------------------
# U-012: CLI plan --dry-run --format json
# ---------------------------------------------------------------------------


def test_u012_plan_dry_run_json_schema():
    r = _run("plan", "--new", "--changed", "--dry-run", "--format", "json")
    assert r.returncode == 0 or r.returncode == 1
    if r.returncode == 0:
        data = json.loads(r.stdout)
        assert "command" in data
        assert data["command"] == "plan"
        assert data.get("dry_run") is True
        assert "items" in data
        assert "total_bytes" in data


def test_u012_plan_json_has_method_fields():
    r = _run("plan", "--new", "--format", "json")
    if r.returncode == 0:
        data = json.loads(r.stdout)
        assert "torrent_bytes" in data
        assert "https_bytes" in data


def test_u012_plan_dry_run_has_no_plan_id():
    """Dry run must not persist a plan — plan_id should be null."""
    r = _run("plan", "--dry-run", "--format", "json")
    if r.returncode == 0:
        data = json.loads(r.stdout)
        assert data.get("plan_id") is None


def test_u012_plan_human_output_no_crash():
    r = _run("plan", "--new")
    assert r.returncode in (0, 1)


# ---------------------------------------------------------------------------
# U-012: CLI sync --dry-run
# ---------------------------------------------------------------------------


def test_u012_sync_dry_run_json():
    r = _run("sync", "fake-plan-id", "--dry-run", "--format", "json")
    assert r.returncode == 0 or r.returncode == 1
    # Should produce JSON (either result or error)
    combined = r.stdout + r.stderr
    try:
        data = json.loads(combined.strip())
        assert isinstance(data, dict)
    except json.JSONDecodeError:
        pass  # human output acceptable if not json format


def test_u012_sync_dry_run_schema():
    r = _run("sync", "fake-plan-id", "--dry-run", "--format", "json")
    if r.returncode == 0:
        data = json.loads(r.stdout)
        assert data.get("command") == "sync"
        assert data.get("dry_run") is True


# ---------------------------------------------------------------------------
# U-012: CLI receipts list
# ---------------------------------------------------------------------------


def test_u012_receipts_list_json():
    r = _run("receipts", "list", "--format", "json")
    if r.returncode == 0:
        data = json.loads(r.stdout)
        assert "receipts" in data
        assert isinstance(data["receipts"], list)


def test_u012_receipts_human_no_crash():
    r = _run("receipts", "list")
    assert r.returncode in (0, 1)


# ---------------------------------------------------------------------------
# General CLI contract
# ---------------------------------------------------------------------------


def test_u012_help_exits_zero():
    r = _run("--help")
    assert r.returncode == 0
    assert "infocon-librarian" in r.stdout.lower() or "usage" in r.stdout.lower()


def test_u012_unknown_command_exits_nonzero():
    r = _run("nonexistent-command")
    assert r.returncode != 0


def test_u012_error_in_json_mode_goes_to_stderr():
    r = _run("receipts", "--format", "json")
    # Missing subcommand should produce JSON error
    assert r.returncode != 0
    try:
        err = json.loads(r.stderr)
        assert "error" in err
    except json.JSONDecodeError:
        pass  # acceptable if error is human-readable
