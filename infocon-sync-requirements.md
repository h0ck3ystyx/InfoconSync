# InfoCon Archive Sync — Requirements

A Python app with a local GUI that compares a local copy of the InfoCon archive against
the live infocon.org site and lets the user visually review the differences and
selectively download new or updated content to the local drive.

This document is the build spec. Hand it to Claude Code as-is. Everything an
implementer needs to start is here; open questions are listed at the end.

## Context

[infocon.org](https://infocon.org) is an archive of hacking and security conference
audio/video, documentaries, podcasts, skills, and word lists. It is served as a
browsable nginx **fancyindex** directory tree (plain HTML pages of `<a href>` links),
and content is also distributed as `.torrent` files. When content changes, InfoCon
removes the old torrent and replaces it with a fresh one, usually bumping a version
number in the filename (v1 to v2 to v3).

The user has a full copy of the archive on an external drive (taken from a DEF CON
distribution). They want to keep it current without re-downloading everything: see what
has changed upstream, pick what they want, and pull only that.

### Verified local layout

The drive root contains these top-level sections, each mirroring the matching directory
on infocon.org:

- `cons/` — conference talks. ~594 entries. The bulk of the archive (multiple TB).
- `documentaries/`
- `podcasts/`
- `skills/`
- `word lists/`

Inside a section, entries are a mix of:

- Content folders, e.g. `cons/2600/`, which contain per-year subfolders like
  `cons/0xcon/0xcon 2017/`, plus loose files (logos, `thank you.txt`).
- Torrent files that act as version markers, named with this pattern:
  `<name> archive v<N> - infocon.org.torrent`, e.g.
  `2600 archive v1 - infocon.org.torrent`,
  `ATT&CKcon archive v2 - infocon.org.torrent`.
  Some use combined labels like `v1v2`.

The torrent filenames are the cleanest signal for detecting updates, because they sit at
a shallow depth and encode versions explicitly. A remote `v2` where the drive has `v1`
means that collection was refreshed upstream.

### Default paths

- Local archive root: the mounted drive. On the user's machine this is
  `/Volumes/InfoCon 2024 June`. Make this a config value / CLI flag, not a hardcode.
- Remote base URL: `https://infocon.org`.

## Decisions already made

These are settled. Build to them; do not re-litigate.

1. **Download method: direct HTTP.** Download actual files straight from the
   infocon.org fancyindex over HTTPS. No torrent client, no torrent parsing for the
   download path. (Torrent *filenames* are still read as version markers for the diff.)
2. **Interface: local GUI.** A graphical app that shows the diff visually (a tree of
   sections and collections with New / Updated / Unchanged status), lets the user pick
   items with checkboxes, and shows live download progress. Implement as a **local web
   UI**: a small Python backend serving a single-page front-end on `localhost`, opened in
   the user's browser. It binds to localhost only and is single-user; no terminal
   interaction is required to operate it. A thin CLI may exist for launching and for
   scripted/dry runs, but the primary interface is the GUI.
3. **Scope: all four content areas.** Track `cons`, `documentaries`, `podcasts`, and
   `skills` + `word lists`. Sections should be configurable so the user can narrow a run.

## Goals

- Show the user, quickly, what exists upstream that is not on their drive (new
  collections, new years/folders, refreshed torrent versions).
- Let the user selectively choose what to download, at a sensible granularity (per
  collection and per folder, with the option to drill into individual files).
- Download chosen content over HTTP into the correct local paths, resumably, with
  verification.
- Be safe to re-run. Re-running after a partial or completed sync should pick up cleanly
  and not re-download what is already present and correct.

## Non-goals

- No torrent downloading or seeding.
- No deleting of local files. The app only adds or updates; it never removes content
  from the drive. (It may *report* items that exist locally but are gone upstream, but
  it does not act on them.)
- No public web service. The UI is a local server bound to `localhost`, for a single
  user on their own machine. No remote access, no multi-user, no deployment.
- No account, login, or auth. The site is public and anonymous, and the local UI is not
  exposed to the network.

## Functional requirements

### 1. Remote crawling (fancyindex parser)

- Fetch a directory URL and parse the returned HTML for entries. Use the standard
  library only for parsing (`html.parser`); do not assume a specific fancyindex theme.
- Extract `<a href>` values. Classify an entry as a directory if its href ends in `/`,
  otherwise a file.
- Ignore non-content links: parent directory (`../`), column-sort links (hrefs starting
  with `?`, e.g. `?C=N;O=D`), anchors (`#...`), and any absolute URL pointing to a
  different host.
- URL-decode entry names (handle `%20` and friends) so they map to real filesystem
  names.
- Where the listing exposes size and last-modified columns, capture them if they can be
  parsed reliably. Treat them as best-effort, not guaranteed.
- Be polite: set a descriptive User-Agent, cap concurrency (a small thread pool, e.g. 4
  to 8), add a short delay/backoff, and retry transient failures (timeouts, 5xx) a few
  times with exponential backoff.

### 2. Local scanning

- Walk the local archive root for the configured sections and build a tree of what
  exists (folders and files, with sizes).
- Parse local torrent filenames into `(collection, version)` so the diff can compare
  versions against remote.

### 3. Diff engine (two-phase)

The archive is large, so do not deep-crawl everything up front.

**Phase 1, shallow scan for the menu.** For each selected section, list the remote
section directory and compare collections (top-level folders) and torrent files against
local. From this alone, produce a menu that flags each collection as one of:

- **New** — remote folder/collection not present locally.
- **Updated** — remote has a higher torrent version than local, or a torrent filename
  the drive lacks. Show both versions (e.g. "drive v1, remote v2").
- **Unchanged** — same collections and torrent versions present.
- Optionally **Local-only** — present on the drive, absent upstream (report only).

**Phase 2, deep diff on selection.** When the user selects a collection to inspect or
sync, recursively list that collection's remote subtree and compare to local to find
missing files and changed files. A file is "missing" if the remote path has no local
counterpart. A file is "changed" if sizes differ (when remote size is known) or, behind
an explicit `--check-sizes` style flag, if an HTTP HEAD shows a different
`Content-Length`. Do not HEAD every file by default; it is too slow at this scale.

### 4. Local GUI

Implement as a local web UI: a Python backend (a lightweight server such as Flask or
FastAPI, or stdlib `http.server` if dependencies must stay zero) that serves a
single-page front-end and a small JSON API, plus a browser front-end. On launch the app
starts the server, binds to `127.0.0.1` on an open port, and opens the user's default
browser to it automatically.

Front-end behavior:

- On load, trigger the shallow scan and render results as a collapsible tree: section to
  collection, with a clear status badge on each collection (New, Updated, Unchanged, and
  optionally Local-only). Updated rows show both versions, e.g. "drive v1, remote v2".
- Provide checkboxes at every level. Checking a section or collection selects everything
  under it; the user can expand a collection to drill in and pick specific folders or
  files. Include quick actions: select all New, select all Updated, clear selection.
- Provide filtering and search: filter by status (show only New/Updated) and a text
  search across collection names.
- Expanding a collection triggers the phase-2 deep diff for that collection on demand
  (lazy load), so the initial view stays fast.
- A running selection summary shows count of files and estimated total bytes. A clear
  "Download selected" action starts the sync.

Progress and results:

- During download, show live progress in the UI: overall progress, current file(s),
  bytes and percent, and rough speed. Stream updates from the backend (server-sent
  events, websocket, or short polling of a status endpoint, implementer's choice).
- When finished, show a summary in the UI: succeeded, skipped (already present), and
  failed with reasons. Let the user retry failed items.

Backend API (shape, not exact routes): scan/status, deep-diff for a collection,
start-download for a selection, download-progress, cancel. All requests are local.

The UI does not need to be elaborate or styled like a product. It needs to make the diff
legible, make selection easy, and show progress honestly. Plain HTML/CSS/JS is fine; a
framework is optional and should not be required to run.

### 5. Downloader

- Download over HTTPS straight to the correct local path, creating directories as
  needed.
- **Resume support:** if a partial file exists, use an HTTP `Range` request to continue
  rather than restarting. Write to a `.part` temp file and rename on success.
- Show per-file and overall progress (filename, bytes, percent, rough speed).
- **Verify** after each file: confirm the final size matches the server's
  `Content-Length`. If the server exposes a checksum, verify it; otherwise size match is
  sufficient. On mismatch, mark the file failed and keep going.
- Continue on individual failures; collect them and print a summary at the end (succeeded,
  failed, skipped-already-present).
- Never overwrite a correct existing file. If a local file already matches the expected
  size, skip it.

### 6. State and caching

- Cache the remote scan results to a local JSON file (in the app's own config/cache dir,
  not on the archive drive) with a timestamp, so re-runs are fast and the app can show
  "changes since last check."
- Provide a way to force a fresh crawl (ignore cache).
- Keep a simple log of what was downloaded and when.

## Launch and CLI surface (suggested)

The primary entry point starts the GUI. A few flags exist for launching and for headless
runs; the GUI is where selection and downloading happen.

- `infocon-sync` — start the local server and open the browser to the GUI. This is the
  normal way to run it.
- `infocon-sync --no-browser --port N` — start the server without auto-opening a browser
  (the user opens the printed `http://127.0.0.1:N` URL themselves).
- `infocon-sync scan` — headless: run the shallow scan and print the diff report, no
  server, no downloading. Useful for a quick check or scripting.
- Common flags: `--root PATH` (local archive root), `--base-url URL`,
  `--section NAME` (limit to one or more sections), `--no-cache` (force fresh crawl),
  `--check-sizes` (enable HEAD-based change detection in phase 2), `--dry-run` (resolve
  selections and report what would download without downloading).

## Technical constraints

- Python 3.10 or newer.
- Prefer the standard library (`urllib`, `html.parser`, `concurrent.futures`,
  `argparse`, `json`, `pathlib`). For the GUI server, a small framework (Flask or
  FastAPI) is acceptable and likely simplest; stdlib `http.server` is an option if the
  goal is zero dependencies. Whatever is chosen, the front-end should be plain
  HTML/CSS/JS served by the app, with no build step required to run it.
- The GUI server binds to `127.0.0.1` only. Do not bind to `0.0.0.0` or expose it to the
  network. Pick an open port automatically if the default is taken.
- Keep third-party dependencies minimal. Pin whatever is required in `requirements.txt`
  or `pyproject.toml`, and document how to run it.
- Cross-platform paths via `pathlib`. The reference machine is macOS with the drive at
  `/Volumes/InfoCon 2024 June`, but do not hardcode that.
- Handle filenames with spaces, ampersands, and other characters that appear in real
  collection names (`ATT&CKcon`, `word lists`).
- Network code must tolerate flaky connections: timeouts, retries, resume.
- Be a good citizen toward infocon.org: limited concurrency, backoff, honest User-Agent.

## Acceptance criteria

The build is done when:

1. `scan` against a local root produces a correct New / Updated / Unchanged report,
   driven by folder presence and torrent version comparison, without deep-crawling the
   whole site.
2. Launching the app starts a local server bound to `127.0.0.1`, opens the browser, and
   renders the diff as a tree with New / Updated / Unchanged status badges and working
   checkboxes.
3. Selecting a "new" collection in the GUI and clicking download fetches its files into
   the matching local path, creating folders as needed, with live progress shown in the
   UI; a re-run reports it as unchanged.
4. Interrupting a download mid-file and re-running resumes that file via Range rather
   than restarting it.
5. A file whose local size already matches the server is skipped, not re-downloaded.
6. Individual file failures do not abort the run; the UI summary lists succeeded, failed,
   and skipped, and failed items can be retried.
7. The app runs with a clearly declared and pinned dependency set (or none), and the GUI
   server binds only to localhost.
8. The fancyindex parser correctly ignores parent, sort-column, and off-host links, and
   correctly URL-decodes names. Include a unit test that feeds it sample listing HTML.

## Testing

- Unit-test the fancyindex parser against captured sample HTML (directory with subdirs,
  files, sort links, parent link, encoded names).
- Unit-test the torrent-version parser (`v1`, `v2`, `v1v2`, odd spacing).
- Unit-test the diff engine with fixture trees (new collection, version bump, unchanged,
  local-only).
- Provide a `--dry-run` path so the user can exercise selection and reporting against the
  real site without writing anything.

## Open questions for the implementer to confirm with the user

These do not block starting, but resolve them before finalizing behavior.

1. **Change detection depth.** Is folder presence plus torrent-version comparison enough,
   or does the user also want file-level change detection inside already-present years
   (slower, needs HEAD requests)? Default assumption: version + presence is enough, with
   `--check-sizes` as an opt-in.
2. **Local-only items.** Should the app report folders that exist on the drive but are
   gone upstream? Default assumption: report only, never delete.
3. **Sidecar files.** The drive holds `.torrent` files and loose extras (logos, thank-you
   notes). When syncing a collection over HTTP, should those be pulled too, or only the
   media subfolders? Default assumption: mirror everything the remote folder contains.
4. **Concurrency ceiling.** Acceptable parallel-download count against infocon.org.
   Default assumption: conservative, 2 to 4 at a time.
