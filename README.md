# InfoCon Librarian

A local-first desktop application for maintaining an existing [InfoCon](https://infocon.org) archive. It detects what changed upstream, builds a reviewable transfer plan, executes it using BitTorrent as the primary mechanism, and produces auditable receipts.

**macOS only** in the current release. Linux and Windows are supported after their packaging smoke tests pass.

---

## What it does

- Scans your local archive and compares it to the upstream InfoCon directory listing
- Classifies each collection with an explainable status: `New`, `Changed`, `Verified current`, `Present (unverified)`, `Local only`, etc.
- Downloads via **torrent-first**: fetches the published `.torrent` for each collection, inspects its manifest, and transfers only what you selected
- Falls back to **HTTPS only** when no usable torrent exists — never silently, always labelled
- Never downgrades a torrent transfer to HTTP without explicit user approval
- Produces a signed receipt for every completed or failed plan
- Binds only to `127.0.0.1` — the UI is never reachable from other machines

## Requirements

- macOS 13 (Ventura) or later
- [Homebrew](https://brew.sh)
- Python 3.11 or 3.12

## Install

```bash
brew install libtorrent-rasterbar python@3.12
pip3 install infocon-librarian
```

Or from source:

```bash
git clone https://github.com/h0ck3ystyx/InfoconSync
cd InfoconSync
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e .
```

## Usage

```bash
# Launch the browser UI (opens automatically)
infocon-librarian --root /Volumes/Archives/InfoCon

# CLI — check what changed upstream
infocon-librarian check --format json

# CLI — build a transfer plan (dry run)
infocon-librarian plan --new --changed --dry-run

# CLI — run a plan
infocon-librarian sync PLAN_ID

# CLI — list receipts
infocon-librarian receipts list
infocon-librarian receipts export RECEIPT_ID
```

## Privacy

Before every torrent transfer the UI shows:

- Which tracker hosts will be contacted
- That DHT, PEX, and LSD are disabled (always, by default)
- The upload rate cap and post-completion seeding choice (off by default)

Logs redact all routable peer IP addresses and session tokens automatically. See [`docs/privacy-and-network.md`](docs/privacy-and-network.md) for the full statement.

## Security

- Flask binds loopback only; a per-launch cryptographically random capability token bootstraps a session cookie (`HttpOnly`, `SameSite=Strict`)
- Every state-changing route requires a same-origin `Origin` header, session cookie, and CSRF token
- Torrent metadata paths are validated through `SafeArchivePath` before the engine sees them — `..`, absolute paths, empty components, and reserved platform names are all rejected
- No `unsafe-inline` in the Content Security Policy; all UI assets are served from `'self'`

## Development

```bash
python -m pytest tests/unit -q           # pure unit tests
python -m pytest tests/integration -q   # SQLite + Flask + fake engine
python -m pytest tests/engine -q        # real libtorrent, loopback only
python -m pytest tests/e2e -q           # Playwright browser tests
python -m ruff check src tests
python -m mypy src
```

**217 tests passing** across unit, integration, and E2E layers. Coverage ≥ 90% for domain/planner/path code.

## Architecture

```
Browser UI / CLI
    └── Flask API (loopback only, CSRF+Origin middleware)
            ├── Check / Plan / Verify / Receipt services
            │       ├── SQLite state (WAL + FK mode)
            │       ├── Remote listing fetcher + fancyindex parser
            │       └── Pure transfer planner
            └── TransferManager (single worker thread)
                    ├── LibtorrentAdapter (libtorrent 2.0.x bindings)
                    └── HttpDownloader (.part sidecar, Range resume)
```

Key invariants:
1. Browser input never becomes a remote URL or filesystem path
2. The planner decides transfer method before a job starts — no silent switching
3. Only `TransferManager` calls the torrent adapter
4. A torrent is `Piece-verified` only after a successful final recheck

## Packaging (macOS)

```bash
./scripts/package-macos.sh           # unsigned .app
./scripts/package-macos.sh --sign    # signed
./scripts/package-macos.sh --sign --notarize TEAM_ID   # signed + notarized DMG
```

Requires `pip install pyinstaller` and libtorrent available via Homebrew.

## License

MIT — see `pyproject.toml`.

Third-party notices: libtorrent (BSD), Flask (BSD), httpx (BSD), platformdirs (MIT).
