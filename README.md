# infocon-sync

A local GUI tool that compares your copy of the [InfoCon](https://infocon.org) archive against the live site and lets you selectively download what's new or updated.

InfoCon is an archive of hacking and security conference talks, documentaries, podcasts, skills videos, and word lists — multiple terabytes in total. This tool is for people who already have a local copy and want to keep it current without re-downloading everything.

## How it works

1. **Shallow scan** — fetches the top-level listing for each section and compares torrent version markers against your local drive. Shows New / Updated / Unchanged status for each collection in seconds.
2. **Select** — check what you want. Quick-select buttons for all New or all Updated. Estimated download size shown live.
3. **Deep diff on demand** — expand a collection to see exactly which files are missing or changed before committing to a download.
4. **Download** — pulls files directly over HTTPS with live progress, resume support, and a retry UI for failures.

## Requirements

- Python 3.10+
- A local copy of the InfoCon archive mounted and accessible

## Installation

```bash
git clone git@github.com:h0ck3ystyx/InfoconSync.git
cd InfoconSync
pip install -e .
```

## Usage

**First launch** — pass your archive root:
```bash
infocon-sync --root "/Volumes/InfoCon 2024 June"
```

The root path is saved to `~/.config/infocon-sync/config.json`. Subsequent launches just need:
```bash
infocon-sync
```

This starts the local server and opens your browser to the GUI.

**Headless scan** (no GUI, prints diff to terminal):
```bash
infocon-sync scan
```

**Other flags:**
```
--root PATH          Local archive root (overrides saved config)
--section NAME       Limit to one or more sections (repeatable)
--no-cache           Force a fresh crawl (ignore cached scan results)
--no-browser         Start server without auto-opening a browser
--port N             Use a specific port (default: 7734 or auto)
--dry-run            Show what would be downloaded without downloading
--check-sizes        Enable HEAD-based file change detection in deep diff
```

**Sections available** (default: all five):
- `cons` — conference talks (~600 collections)
- `documentaries`
- `podcasts`
- `skills`
- `word lists`

Opt-in sections (not synced by default):
- `mirrors`
- `rainbow tables`

Example — sync only cons and podcasts, fresh crawl:
```bash
infocon-sync --section cons --section podcasts --no-cache
```

## Details

**Version detection** uses torrent files as markers. InfoCon publishes a `<name> archive v1 - infocon.org.torrent` alongside each collection; when content is refreshed it adds a `v2`, `v3`, etc. This tool compares the max version on the remote against what's on your drive to flag Updated collections — without crawling every file.

**Resume support** — interrupted downloads pick up where they left off via HTTP `Range` requests. Partial files are kept as `<filename>.part` until the transfer completes.

**Scan cache** — remote listings are cached for 1 hour at `~/.cache/infocon-sync/scan_cache.json`. Use `--no-cache` to force a fresh fetch.

**Download log** — every completed download is appended to `~/.cache/infocon-sync/download.log` (JSONL).

The GUI server binds to `127.0.0.1` only and is never exposed to the network.

## Development

```bash
pip install -e .
python -m pytest
```

Run a specific test module:
```bash
python -m pytest tests/test_crawler.py -v
```
