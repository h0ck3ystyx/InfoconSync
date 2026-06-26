# Release Checklist

## Pre-release gates (all must be green)

- [ ] `python -m pytest tests/unit -q` — no failures, no skips without documented issue
- [ ] `python -m pytest tests/integration -q` — no failures
- [ ] `python -m pytest tests/engine -q` — no failures (real libtorrent, loopback only)
- [ ] `python -m pytest tests/e2e -q` — no failures
- [ ] `python -m ruff check src tests` — no warnings
- [ ] `python -m mypy src` — no errors
- [ ] Coverage ≥ 90% for `domain/`, `planner.py`, path validation code
- [ ] No `xfail` or `skip` on security, path-traversal, or transfer tests without a linked issue

## Security checks

- [ ] Bootstrap token is single-use (test F-009)
- [ ] CSRF + Origin required on all mutation routes (test F-010)
- [ ] No `unsafe-inline` in CSP (test R-008)
- [ ] No `Access-Control-Allow-Origin` on any response (test R-008)
- [ ] Path traversal rejected (test R-008)
- [ ] Peer IPs redacted in logs (test R-log)

## Transfer policy checks (manual)

- [ ] Confirm no automatic torrent → HTTPS downgrade: start a torrent with no peers; verify it stays "Awaiting user fallback" without touching HTTPS
- [ ] Confirm no auto-seeding: complete a local fixture transfer; verify seeding does not start without approval
- [ ] Confirm HTTPS result is "Downloaded, unverified" (not "Verified current")

## Packaging smoke test (macOS)

- [ ] `./scripts/package-macos.sh` completes without error
- [ ] `dist/InfoConLibrarian.app` launches on a clean machine (no development Python)
- [ ] Bootstrap URL opens in browser; session cookie is set
- [ ] Check with fixture HTML produces expected status summary
- [ ] Local fixture torrent transfer completes and produces a receipt

## Documentation

- [ ] `docs/installation.md` reviewed for accuracy
- [ ] `docs/privacy-and-network.md` reviewed for accuracy
- [ ] `docs/troubleshooting.md` reviewed for accuracy
- [ ] Third-party license notices present in `docs/licenses/` or `pyproject.toml`

## Dependency audit

- [ ] `pip-audit` or `safety check` clean
- [ ] libtorrent version pinned in `pyproject.toml` or build spec
- [ ] No unexpected runtime network calls (test R-007)

## Manual exploratory test script

Run against a local fixture archive (not the live InfoCon site):

1. Launch app with `--root /path/to/fixture-archive`
2. Verify health screen shows correct root path and free space
3. Run Check; verify status summary includes New, Present (unverified), and Local Only items
4. Click evidence link on a Changed item; confirm evidence drawer shows torrent URL and manifest
5. Create a plan including at least one torrent item and one HTTP fallback item
6. Review privacy disclosure; confirm tracker hosts, upload cap, and seeding choice are shown
7. Start the plan; monitor SSE progress in the transfer panel
8. Pause a running torrent item; verify it pauses; resume it
9. After completion, verify receipt is created with correct verification level
10. Export the receipt; verify it contains no absolute paths or peer IPs
11. Disconnect the fixture drive while a transfer is in progress; verify transfer pauses
12. Reconnect the drive; verify resume is offered
13. Run `infocon-librarian receipts list --format json` and verify output is valid JSON
14. Verify browser UI has no external network requests (check browser DevTools Network tab)
