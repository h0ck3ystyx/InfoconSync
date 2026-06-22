# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`infocon-sync` — a Python app that compares a local copy of the [InfoCon](https://infocon.org) archive against the live site and lets the user selectively download new/updated content. The full build spec is in `infocon-sync-requirements.md`. The detailed implementation plan (with live-crawl findings) is in `implementation-plan.md`.

## Running

```bash
pip install -e .
infocon-sync --root "/Volumes/InfoCon 2024 June"   # first launch — saves --root to config
infocon-sync                                         # subsequent launches use saved root
infocon-sync scan                                    # headless diff report, no server
python -m pytest                                     # run tests
```

## Architecture

**Two-process model:** a Flask backend (threaded mode, `127.0.0.1` only) serves a JSON API and static HTML/CSS/JS. On launch it opens the user's default browser. No build step for the frontend — plain HTML/CSS/JS in `infocon_sync/static/`.

**Two-phase diff:**
- Phase 1 (shallow, fast): list each section directory remotely, compare torrent filenames to detect New / Updated / Unchanged collections. Drives the initial GUI tree.
- Phase 2 (deep, lazy): triggered per-collection when the user expands it in the GUI. Recursively walks the remote subtree and diffs against local.

**Key modules:**
- `config.py` — `Config` dataclass + persistent config (`~/.config/infocon-sync/config.json`)
- `crawler.py` — fancyindex HTML parser (`html.parser` only) + threaded fetcher with retry/backoff
- `scanner.py` — local filesystem walk + torrent filename parser
- `diff.py` — phase-1 and phase-2 diff logic
- `downloader.py` — HTTPS download with `.part` temp files, `Range` resume, progress callbacks
- `cache.py` — phase-1 JSON cache (`~/.cache/infocon-sync/`) + JSONL download log
- `server.py` — Flask routes + SSE progress stream
- `cli.py` — `argparse` entry point

## Settled decisions

### Site structure (from live crawl)

The site root at `https://infocon.org/` lists these sections:
```
cons/   documentaries/   mirrors/   podcasts/   rainbow tables/   skills/   word lists/
```

Section name → URL: `urllib.parse.quote(name, safe="") + "/"` — e.g., `"word lists"` → `"word%20lists/"`.

**Default sections** (in scope by default): `cons`, `documentaries`, `podcasts`, `skills`, `word lists`.  
**Opt-in sections**: `mirrors`, `rainbow tables` — available via `--section mirrors` etc., not included by default.

### Fancyindex HTML structure

```html
<table id="list">
  <tbody>
    <tr><td colspan="2" class="link"><a href="../">Parent directory/</a></td>...</tr>
    <tr><td colspan="2" class="link"><a href="ATT%26CKcon/" title="ATT&amp;CKcon">ATT&amp;CKcon/</a></td>
        <td class="size">-</td><td class="date">2025 Dec 24 07:00</td></tr>
    <tr><td colspan="2" class="link"><a href="file.rar" title="file.rar">file.rar</a></td>
        <td class="size">247.4 MiB</td><td class="date">2019 Oct 01 11:20</td></tr>
  </tbody>
</table>
```

- **Sort order**: directories first, then files (both groups alphabetically).
- **Entries to skip**: href starts with `?` (sort links), href starts with `#`, href is `../`, absolute URL to different host.
- **Display name**: use `title` attribute + `html.unescape()`. Do NOT decode the href — the title is cleaner and always present on content links.
- **Size**: parse `td.class="size"` — e.g., `247.4 MiB` → bytes. Units are KiB/MiB/GiB/TiB (binary). Directories show `-` → `None`.
- **Server does NOT send `Content-Length`** in HTTP responses. Sizes from the listing are the only source and are approximate (1 decimal place).

### Torrent naming per section

| Section | Torrent format | Version signal |
|---------|---------------|----------------|
| `cons/` | `<name> archive v<N> - infocon.org.torrent` | per-collection, v1/v2 |
| `documentaries/` | `INFOCON Hosted Hacking Documentaries YYYY-MM-DD - v<N>.torrent` | section-wide v1/v2 |
| `podcasts/` | `<name> archive.torrent` | no version number |
| `skills/` | none | presence/absence only |
| `word lists/` | `Word Lists archive v<N> - infocon.org.torrent` | section-wide v1/v2 |

Version parsing: extract all `\d+` groups from the version string, take the max. `v1v2` → max version 2.

### Per-section diff behavior

- **cons/**: Compare torrent max version per collection. Remote higher → Updated.
- **documentaries/**: Single section-wide torrent pair; compare versions to detect section updates.
- **podcasts/**: No version numbers. If both local and remote have `<name> archive.torrent` → Unchanged. If remote has it but local doesn't → Updated. **No deep diff for podcasts** — no attempt to detect new episodes.
- **skills/**: Presence/absence of collection folders only.
- **word lists/**: Section-wide torrent comparison. **Flat section** — no subdirectories. Show individual files directly in the UI.

### Download behavior

- Write to `<path>.part` temp file; rename to final path on clean transfer completion.
- Resume: if `.part` exists, send `Range: bytes=<size>-`.
- **No size verification** — server sends no `Content-Length`. Trust clean transfer. If something went wrong, the `.part` file stays and resumes on next run.
- Skip if `local_path` already exists and `local_path.stat().st_size == expected_size` (size from fancyindex listing, approximate).
- Concurrency: 3 parallel downloads by default.

### Config persistence

`~/.config/infocon-sync/config.json` stores `root` (archive drive path). First launch requires `--root`; subsequent launches use the saved value. `--root` on any launch overrides and updates the saved value.

## Technical constraints

- Python 3.10+. Prefer stdlib (`urllib`, `html.parser`, `concurrent.futures`, `pathlib`). Flask for the server only.
- Server binds to `127.0.0.1` only — never `0.0.0.0`.
- Port: try 7734, fall back to OS-assigned. Print URL to stdout before opening browser.
- `pathlib` everywhere for paths. Filenames contain spaces, `&`, `@`, `(`, `)`, `#`.
- Crawler: max 6 concurrent workers, exponential backoff (1s/2s/4s), max 3 retries, User-Agent `infocon-sync/1.0`.
- Retry on `urllib.error.URLError`, HTTP 5xx, socket timeout. Do NOT retry 4xx.

## Testing

```bash
python -m pytest              # all tests
python -m pytest tests/test_crawler.py  # single module
```

Required unit tests:
- **Parser** against `tests/fixtures/sample_fancyindex.html`: sort links filtered, parent filtered, dirs classified, files classified, title-attr name extraction, size parsing (KiB/MiB/GiB), off-host URLs filtered.
- **Torrent parser**: `v1`, `v2`, `v1v2` (→ max 2), names with `&`, no-version podcast format, non-matching filenames.
- **Diff engine** with fixture dicts (no filesystem): New, Updated, Unchanged, Local-only, podcast Unchanged (both have torrent), podcast Updated (missing local torrent), deep missing/changed/present.
