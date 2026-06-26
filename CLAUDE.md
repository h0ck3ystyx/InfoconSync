# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

InfoCon Librarian is a local-first desktop application for managing an existing InfoCon archive. It maintains a local copy of the InfoCon archive using BitTorrent as the primary transfer mechanism, with HTTPS as a controlled fallback only when no usable torrent is available.

The product is an archive steward: its job is to detect what changed upstream, build a reviewable transfer plan, execute it safely, and produce auditable receipts. It is not a general-purpose download manager.

The design documents are in `docs/`:
- `infocon-librarian-product-spec.md` — requirements, status vocabulary, UX flows, security/privacy rules
- `infocon-librarian-implementation-plan.md` — locked technical decisions, repo layout, architecture diagram, phase-by-phase work and test IDs

## Locked technical decisions

| Area | Decision |
|---|---|
| Runtime | Python 3.11+; target CPython 3.11 and 3.12 in CI |
| UI | Flask local server, plain HTML/CSS/JS — no CDN, bundler, or third-party browser assets |
| Torrent engine | libtorrent 2.0.x Python bindings via `LibtorrentAdapter` — do not implement BitTorrent protocol |
| Persistence | SQLite (WAL mode, foreign keys enabled); JSON only for exported receipts/plans |
| HTTP client | `httpx` — used everywhere, not mixed with `urllib.request` |
| Test runner | `pytest`, `pytest-cov`, Playwright for browser E2E |
| Process model | One Python process; `TransferManager` worker thread owns all libtorrent calls |
| Listening | Flask binds only to `127.0.0.1`/`::1`; torrent networking on a separate interface binding |
| Release scope | macOS first; Linux/Windows only after their packaging smoke tests pass |

## Repository layout (planned)

```
src/infocon_librarian/
├── domain/          # models, status enums, path rules, errors — no Flask/libtorrent deps
├── storage/         # SQLite migrations and repositories
├── archive/         # root validation, inventory, snapshot, search
├── remote/          # HTTP client, fancyindex parser, discovery, cache
├── torrent/         # LibtorrentAdapter, metainfo inspection, policy, resume
├── transfer/        # pure planner, TransferManager, HTTP downloader, preflight
├── services/        # check, verify, plan, receipt services
└── web/             # Flask app, auth middleware, JSON API, static UI
tests/
├── unit/            # domain, paths, parser, planner — no sockets
├── integration/     # SQLite + Flask + HTTP test server + fake engine — loopback only
├── engine/          # real libtorrent loopback swarm tests
├── e2e/             # Playwright browser tests — loopback only
├── fixtures/        # synthetic listing HTML, torrent files, archive trees
└── support/         # FakeTorrentEngine, fake remote, local tracker, factories
```

## Commands (once project is initialized)

```bash
# Run tests
python -m pytest tests/unit -q
python -m pytest tests/integration -q
python -m pytest tests/engine -q
python -m pytest tests/e2e -q

# Lint and type-check
python -m ruff check src tests
python -m mypy src

# Install Playwright browsers
python -m playwright install --with-deps chromium

# CLI entry points
infocon-librarian --root PATH
infocon-librarian check [--section NAME] [--fresh] [--format json]
infocon-librarian plan --new --changed [--format json]
infocon-librarian verify COLLECTION
infocon-librarian sync PLAN_ID [--dry-run]
infocon-librarian receipts list|show|export
```

## Architecture invariants

These must never be violated:

1. Browser input never becomes a remote URL or filesystem path — it can only reference server-issued opaque IDs from the active plan.
2. The planner decides transfer method before a job starts — a job may not silently switch from torrent to HTTPS.
3. Only `TransferManager` calls the torrent adapter — Flask handlers never call libtorrent directly.
4. All archive destinations are validated at planning time AND immediately before writing.
5. A torrent is `Piece-verified` only after the engine reports a successful final recheck — not when the download alert fires.
6. HTTPS completion is `Downloaded, unverified` unless a trusted checksum or matching torrent recheck verifies it.
7. Tests never contact InfoCon, public trackers, DHT, PEX, or the public internet.

## Domain status vocabulary

The eight archive states (defined in `domain/status.py`):

| State | Key rule |
|---|---|
| `New` | No local item exists |
| `Changed — release marker` | A newer torrent marker exists upstream |
| `Changed — manifest` | File path/size differs from torrent manifest |
| `Verified current` | Piece-checked against current torrent, or persisted verified manifest matches |
| `Present, unverified` | Paths exist but not verified against a manifest |
| `Unknown` | Not enough upstream evidence to classify cheaply |
| `Local only` | No current upstream counterpart — never auto-deleted |
| `Transfer incomplete` | Resumable job exists but not complete |
| `Downloaded, unverified` | HTTPS transfer complete, no cryptographic verification |

`Unchanged` is not a valid user-facing state.

## Security requirements

- Flask binds loopback only; a per-launch cryptographically random capability token bootstraps a session cookie (`HttpOnly`, `SameSite=Strict`).
- State-changing routes require same-origin `Origin` header, session cookie, and CSRF header.
- Torrent metadata file paths are untrusted even when fetched over HTTPS — validate against `SafeArchivePath` before passing to the engine.
- `SafeArchivePath` rejects: absolute paths, `..`, empty components, reserved platform names, symlinks escaping root, case collisions (configurable).
- Path containment is enforced at the last write operation, not only at planning time.
- Privacy disclosure is shown before the first torrent plan and in every subsequent plan.

## Local service security (web/auth.py)

The token bootstrap flow:
1. App launches and opens `http://127.0.0.1:<port>/bootstrap/<random-token>`
2. Bootstrap validates one-time token → sets session cookie → redirects to `/`
3. All mutation routes check `Origin` + session cookie + CSRF header
4. Responses include restrictive CSP; no permissive CORS headers

## Transfer policy

Method selection order per item:
1. **Torrent** — if a published `.torrent` is retrievable, parses cleanly, maps safely, and covers the selected content
2. **HTTPS fallback** — if no usable torrent exists; plan labels item `HTTP fallback — no usable torrent` with a machine-readable reason
3. **No automatic transfer** — if a torrent exists but swarm is unreachable; UI offers `Retry torrent` and an explicit `Use HTTPS for this item` action

The app must never silently downgrade a torrent transfer to HTTPS.

## Implementation order

Follow this order from the implementation plan (§14):

1. Project tooling, domain enums/models, SQLite migration harness, pure path tests
2. Phase 0 torrent spike + ADR-001 — stop if libtorrent cannot be packaged
3. Archive-root validation, inventory, config, Flask security shell + tests
4. Fancyindex parser, remote cache/client, check service, CLI `check`
5. Metainfo inspection, verification workflow, real-engine loopback tests
6. Pure transfer planner and preflight; CLI `plan --dry-run`
7. Transfer manager, torrent job lifecycle, HTTP downloader, receipts
8. GUI screens and SSE; Playwright/accessibility tests
9. Shutdown, removable-drive, packaging hardening, release docs

Do not start frontend polish, search, scheduled checks, or community seeding before steps 1–7 have acceptance tests passing.

## Testing rules

- `domain/`, `planner.py`, path validation, and receipt generation must be pure unit tests — no Flask, no libtorrent.
- Use `FakeTorrentEngine` (from `tests/support/fake_engine.py`) in all unit and integration tests; reserve the real adapter for `tests/engine/`.
- Fixtures must be synthetic or freely redistributable — do not commit InfoCon media payloads.
- Coverage target: ≥90% for domain/planner/path code; do not use aggregate coverage to hide untested native-engine paths.
