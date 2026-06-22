# infocon-sync — Implementation Plan

*Updated with findings from live crawl of infocon.org on 2026-06-22.*

---

## What the live site actually looks like

### Root sections (https://infocon.org/)

```
cons/             documentaries/    mirrors/          podcasts/
rainbow tables/   skills/           word lists/
```

`mirrors/` and `rainbow tables/` are present but not mentioned in the original requirements.
URL-to-local mapping: section name is URL-percent-encoded (`word lists/` → `word%20lists/`).

### HTML structure — `<table id="list">`

```html
<table id="list">
  <thead>
    <tr>
      <th colspan="2">
        <a href="?C=N&amp;O=A">File Name</a>
        <a href="?C=N&amp;O=D">↓</a>
      </th>
      <th><a href="?C=S&amp;O=A">File Size</a> <a href="?C=S&amp;O=D">↓</a></th>
      <th><a href="?C=M&amp;O=A">Date</a> <a href="?C=M&amp;O=D">↓</a></th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td colspan="2" class="link">
        <a href="../">Parent directory/</a>
      </td>
      <td class="size">-</td>
      <td class="date">-</td>
    </tr>
    <tr>
      <td colspan="2" class="link">
        <a href="ATT%26CKcon/" title="ATT&amp;CKcon">ATT&amp;CKcon/</a>
      </td>
      <td class="size">-</td>
      <td class="date">2025 Dec 24 07:00</td>
    </tr>
    <tr>
      <td colspan="2" class="link">
        <a href="SecLists-master.rar" title="SecLists-master.rar">SecLists-master.rar</a>
      </td>
      <td class="size">247.4 MiB</td>
      <td class="date">2019 Oct 01 11:20</td>
    </tr>
  </tbody>
</table>
```

Key facts:
- **Sort is directories-first then files**, both groups alphabetically. The `<thead>` sort links start with `?C=` — filter on that, not `?`.
- **Parent link**: `<a href="../">` — filter on href `../` exactly (display text says "Parent directory/").
- **`title` attribute** contains the HTML-entity-escaped decoded name. Use `html.unescape(title)` to get the clean name; simpler and more reliable than `urllib.parse.unquote(href)`.
- **Size column**: Human-readable with binary units: `247.4 MiB`, `5.9 GiB`, `45.7 GiB`, `2.1 KiB`. Directories show `-`. Need to parse to bytes for comparison.
- **Date column**: `2026 Jun 07 18:33` (YYYY Mon DD HH:MM).
- **No Content-Length in HTTP responses.** The server uses HTTP/2 without declaring content-length. This means: (a) we can't verify completed downloads by comparing sizes, (b) progress bars must use the fancyindex-listed size as an estimate, (c) Range resume still works but we don't know the total size server-side.

### Torrent naming — varies by section

**`cons/`** — per-collection versioned torrents, listed in the same section directory alongside collection folders:
```
cons/0xcon/                                     ← collection folder
cons/0xcon archive v1 - infocon.org.torrent     ← version marker
cons/0xcon archive v2 - infocon.org.torrent     ← latest version
cons/ATT%26CKcon/
cons/ATT%26CKcon archive v1 - infocon.org.torrent
cons/ATT%26CKcon archive v2 - infocon.org.torrent
cons/DEF CON/
cons/DEF CON archive v1 - infocon.org.torrent   ← 13.1 MiB
cons/DEF CON archive v2 - infocon.org.torrent
```
Format: `<collection_name> archive v<N> - infocon.org.torrent`
The cons/ listing is 122KB of HTML (~228 collections × 2 torrents each). Directories come first, then the torrent files. One anomaly: `ICANN - infocon.org.torrent` (no "archive", no version number).

**`documentaries/`** — single section-wide torrent pair, different format:
```
documentaries/Hacker Movies/
documentaries/Hacking Documentaries/
documentaries/INFOCON Hosted Hacking Documentaries 2025-08-28 - v1.torrent
documentaries/INFOCON Hosted Hacking Documentaries 2025-08-28 - v2.torrent
```
Format: `INFOCON Hosted <section> YYYY-MM-DD - v<N>.torrent`

**`podcasts/`** — per-podcast torrents, but **no version numbers**:
```
podcasts/Darknet Diaries/
podcasts/Darknet Diaries archive.torrent        ← no v1/v2
podcasts/Security Now/
podcasts/Security Now archive.torrent
```
Format: `<podcast_name> archive.torrent`

**`skills/`** — no torrent files visible at the section level.

**`word lists/`** — flat section (no subdirectories), section-wide versioned torrents:
```
word lists/DCHTPassv1.0.rar
word lists/SecLists-master.rar
word lists/Word Lists archive v1 - infocon.org.torrent
word lists/Word Lists archive v2 - infocon.org.torrent
```
Format: `Word Lists archive v<N> - infocon.org.torrent`

**`rainbow tables/`** — no torrent files. Subdirectories for hash algorithm types.

**`mirrors/`** — different torrent format, varied:
```
mirrors/Internet Census 2012 - v1 archive.torrent
mirrors/cryptome.org-2025-04-22.rar - v1 archive.torrent
mirrors/textfiles-dot-com-2011.torrent          ← no version
```

### Special characters observed in real hrefs
`%26` (`&`), `%20` (space), `%40` (`@`), `%28`/`%29` (`(`/`)`), `%23` (`#` in filenames), `%27` (`'`).

---

## Project layout

```
infocon-sync/
├── pyproject.toml
├── requirements.txt
├── infocon_sync/
│   ├── __init__.py
│   ├── __main__.py        # python -m infocon_sync entry
│   ├── cli.py             # argparse, dispatches to gui or scan
│   ├── config.py          # Config dataclass, defaults
│   ├── crawler.py         # fancyindex parser + fetching layer
│   ├── scanner.py         # local walk + torrent filename parser
│   ├── diff.py            # phase-1 and phase-2 diff logic
│   ├── downloader.py      # HTTPS download, resume, progress
│   ├── cache.py           # JSON scan cache + download log
│   ├── server.py          # Flask app + all API routes
│   └── static/
│       ├── index.html
│       ├── app.js
│       └── style.css
└── tests/
    ├── fixtures/
    │   └── sample_fancyindex.html
    ├── test_crawler.py
    ├── test_scanner.py
    └── test_diff.py
```

**Dependencies:** Flask only. Everything else is stdlib. Pinned in `requirements.txt` and `pyproject.toml`. Installed via `pip install -e .`.

---

## Step 1 — Scaffold

`pyproject.toml`:
- `[project.scripts] infocon-sync = "infocon_sync.cli:main"`
- `python_requires = ">=3.10"`, `dependencies = ["flask"]`

`__main__.py` calls `cli.main()` so `python -m infocon_sync` also works.

**`config.py`:**

```python
@dataclass
class Config:
    root: Path
    base_url: str = "https://infocon.org"
    sections: list[str] = field(default_factory=lambda: [
        "cons", "documentaries", "podcasts", "skills", "word lists"
    ])
    no_cache: bool = False
    check_sizes: bool = False
    dry_run: bool = False
    port: int = 0                   # 0 = pick a free port
    open_browser: bool = True
    crawl_workers: int = 6
    download_workers: int = 3
    cache_dir: Path = field(default_factory=lambda: Path.home() / ".cache" / "infocon-sync")
```

Section name → URL path: `urllib.parse.quote(section_name, safe="") + "/"`. So `"word lists"` → `"word%20lists/"`.

**`cli.py`:** `argparse` with subcommand `scan`. Validates `--root` exists at startup. `--section` is repeatable (`action="append"`).

---

## Step 2 — Crawler (`crawler.py`)

### Parser (pure function, no network)

```python
@dataclass
class RemoteEntry:
    name: str           # HTML-unescaped display name (from title attr)
    href: str           # raw href (URL-encoded)
    is_dir: bool
    size_bytes: int | None     # parsed from "247.4 MiB" etc.; None for dirs or unparseable
    modified: str | None       # raw date string "2026 Jun 07 18:33"

def parse_fancyindex(html: str, base_url: str) -> list[RemoteEntry]:
    ...
```

**Filtering rules**, in order:
1. Skip href starting with `?` — sort-column links (`?C=N&O=A` etc.)
2. Skip href starting with `#` — anchors
3. Skip href `../` — parent directory link
4. Skip absolute URLs with a different host than `base_url`
5. Classify as dir if href ends with `/`
6. **Get name from `title` attribute** (HTML-unescape it), NOT from decoding the href. The title is always present on content links and gives the clean name directly.
7. Parse size from `<td class="size">`: split on space, multiply value by unit (KiB=1024, MiB=1024², GiB=1024³, TiB=1024⁴). Returns `None` if value is `-` or unparseable.
8. Parse modified from `<td class="date">`: capture raw string; return `None` if `-`.

**Implementation note:** Walk the `<tbody>` row by row. For each `<tr>`, grab the `<a>` tag from the `class="link"` cell. Then read the next two `<td>` siblings for size and date. This avoids fragile sibling-counting and is robust to extra attributes or whitespace.

### Fetcher

```python
def fetch_directory(url: str, config: Config) -> list[RemoteEntry]:
```

- User-Agent: `infocon-sync/1.0`
- Timeout: 30s connect + 60s read (`urllib.request.urlopen(req, timeout=60)`)
- Retries: max 3, exponential backoff 1s/2s/4s
- Retry on: `urllib.error.URLError`, HTTP 5xx, socket timeout
- No retry on 4xx

### Recursive crawler (phase-2)

```python
def crawl_recursive(
    base_url: str,
    config: Config,
    executor: ThreadPoolExecutor,
    _rel: str = "",
) -> list[RemoteEntry]:
```

Recursively fans out directory fetches into `executor`. Each entry in the returned list gets a `rel_path` field (relative from `base_url`) prepended to `name` so the diff can match against local filesystem paths directly.

---

## Step 3 — Scanner (`scanner.py`)

### Torrent filename parser

Two patterns must be handled:

```python
# Canonical cons/ format: "2600 archive v2 - infocon.org.torrent"
VERSIONED_RE = re.compile(
    r'^(.+?)\s+archive\s+(v\d+(?:v\d+)*)\s*-\s*infocon\.org\.torrent$',
    re.IGNORECASE,
)

# Podcast format: "Darknet Diaries archive.torrent" (no version)
UNVERSIONED_RE = re.compile(
    r'^(.+?)\s+archive\.torrent$',
    re.IGNORECASE,
)

@dataclass
class TorrentMarker:
    collection: str    # e.g. "2600", "ATT&CKcon", "Darknet Diaries"
    version: int | None  # max numeric version extracted; None if no version
    filename: str

def parse_torrent_filename(filename: str) -> TorrentMarker | None:
    # Try VERSIONED_RE first, then UNVERSIONED_RE
    # For VERSIONED_RE: extract all \d+ groups from the version string, take max
    # "v1v2" → max(1,2) = 2
```

**Version comparison**: always compare `version` (int) values. `None` versions (podcasts) can only be checked for presence/absence.

### Local scanner

```python
@dataclass
class LocalSection:
    name: str
    path: Path
    collections: dict[str, Path]          # folder_name → path (shallow)
    torrent_markers: list[TorrentMarker]  # all .torrent files at section root

@dataclass
class LocalCollection:
    name: str
    path: Path
    files: dict[str, int]   # rel_path → size in bytes (populated on demand)

def scan_local_shallow(section_name: str, section_path: Path) -> LocalSection:
    # One scandir() call. Classifies each entry as dir or .torrent file.

def scan_local_deep(collection_path: Path) -> dict[str, int]:
    # Full recursive walk, returns {rel_path: size_bytes}
```

---

## Step 4 — Diff engine (`diff.py`)

### Data model

```python
class CollectionStatus(enum.Enum):
    NEW = "new"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    LOCAL_ONLY = "local_only"

@dataclass
class CollectionDiff:
    name: str
    status: CollectionStatus
    local_version: int | None
    remote_version: int | None
    remote_url: str

@dataclass
class FileDiff:
    rel_path: str
    status: Literal["missing", "changed", "present"]
    remote_size: int | None    # from fancyindex size column
    local_size: int | None
    remote_url: str
```

### Phase 1 — shallow diff

```python
def diff_section_shallow(
    section_name: str,
    remote_entries: list[RemoteEntry],   # full listing of the section directory
    local_section: LocalSection | None,
) -> list[CollectionDiff]:
```

Logic:
- Partition `remote_entries` into dirs (collections) and files (torrent markers + loose files)
- Build `remote_torrents: dict[str, int]` mapping collection_name → max remote version
- For each remote collection folder:
  - Find its torrent marker in `remote_torrents`
  - Compare against local marker for the same collection name
  - If local folder missing → `NEW`
  - If remote version > local version → `UPDATED` (record both)
  - If versions equal → `UNCHANGED`
- For each local collection not in remote → `LOCAL_ONLY`
- **Podcasts** (no versioned torrents): detection is presence/absence only. If remote has `<name> archive.torrent` but local doesn't → treat that collection as `UPDATED` (the torrent is the signal of change).

### Phase 2 — deep diff

```python
def diff_collection_deep(
    collection: CollectionDiff,
    remote_entries: list[RemoteEntry],   # recursive listing from crawl_recursive
    local_files: dict[str, int],         # from scan_local_deep
    config: Config,
) -> list[FileDiff]:
```

- For each remote file entry: check if `rel_path` exists in `local_files`
  - Missing → `missing`
  - Present but `remote_size != local_size` (when both known) → `changed`
  - Otherwise → `present`
- With `--check-sizes`: issue HEAD request for "present" files. NOTE: server doesn't send `Content-Length`, so this flag may not be useful; document this limitation.
- Torrent files are included in the diff (they are content to be downloaded, not just metadata).

---

## Step 5 — Cache (`cache.py`)

```python
@dataclass
class ScanCache:
    timestamp: str       # ISO 8601
    base_url: str
    phase1: dict         # section_name → list[RemoteEntry as dict]
    # Phase-2 NOT cached — always fetch fresh per collection

def load_cache(config: Config) -> ScanCache | None:
    # Returns None if: file missing, >1h old, or config.no_cache

def save_cache(config: Config, data: ScanCache) -> None:
    # Atomic write: write to .tmp then rename

def append_download_log(config: Config, entries: list[dict]) -> None:
    # JSONL: one record per file with timestamp, path, bytes, status
```

Cache file: `~/.cache/infocon-sync/scan_cache.json`
Log file: `~/.cache/infocon-sync/download.log`

---

## Step 6 — Downloader (`downloader.py`)

```python
@dataclass
class DownloadJob:
    remote_url: str
    local_path: Path
    expected_size: int | None   # from fancyindex listing; used as estimate only

@dataclass
class DownloadResult:
    job: DownloadJob
    status: Literal["ok", "skipped", "failed"]
    bytes_written: int
    error: str | None

def download_file(
    job: DownloadJob,
    progress_cb: Callable[[DownloadProgress], None],
    config: Config,
) -> DownloadResult:
```

**Per-file logic:**
1. If `local_path` exists and `local_path.stat().st_size == expected_size` → skip (size must be known)
2. If `<local_path>.part` exists → get its size N, issue `Range: bytes=N-` to resume
3. Open `.part` file in append mode (`"ab"`)
4. Stream response in 64KB chunks, calling `progress_cb` each chunk
5. On clean EOF: rename `.part` → `local_path`
6. Verification: **server does not send Content-Length**, so size verification is not possible via HTTP headers. If `expected_size` is known (from fancyindex), verify `local_path.stat().st_size == expected_size` after rename. Otherwise, trust the transfer.
7. On any failure: leave `.part` intact for resume; record as failed

**Progress callback:**
```python
@dataclass
class DownloadProgress:
    job_url: str
    bytes_done: int
    total_bytes: int | None   # from expected_size; None when unknown
    speed_bps: float          # rolling 5s average
```

**Concurrency:** `ThreadPoolExecutor(max_workers=config.download_workers)`.

---

## Step 7 — Backend server (`server.py`)

Flask, threaded mode. All API routes under `/api/`. Static files served from `infocon_sync/static/`.

### Routes

| Method | Route | Notes |
|--------|-------|-------|
| GET | `/api/scan` | Triggers phase-1 (or returns cached). Returns `{sections:[...], cached:bool, timestamp:str}` |
| GET | `/api/scan/status` | `{state:"idle|scanning|done|error", progress:{...}}` |
| GET | `/api/diff/<section>/<collection>` | Triggers phase-2 for one collection. Returns `{files:[...]}` |
| POST | `/api/download` | `{items:[{url,local_path,size},...]}`. Starts queue. Returns `{job_id}` |
| GET | `/api/progress` | SSE stream — fires DownloadProgress events + final `done` event |
| POST | `/api/cancel` | Signals cancellation to active download threads |
| GET | `/` | Serves `index.html` |

**Concurrency model:** A module-level `AppState` (protected by `threading.Lock`) holds the current scan result, download job state, and a `queue.Queue` of SSE events. Scan and download threads push events into the queue; the SSE endpoint drains it.

**Port:** Try 7734; fall back to OS-assigned. Print `http://127.0.0.1:<port>` to stdout before opening browser.

---

## Step 8 — Frontend (`static/`)

Plain HTML/CSS/JS. No build step, no CDN, no framework.

### Layout

```
<header>  title, "Refresh scan" button, "Last checked: ..." timestamp
<div#filters>  All | New | Updated filter buttons; text search
<div#actions>  "Select all new", "Select all updated", "Clear all"; "Download selected" button
<div#summary>  "N files selected, ~X GiB"
<div#tree>  collapsible diff tree (built dynamically by app.js)
<div#progress>  download progress panel (hidden until download starts)
<div#results>  post-download summary with retry buttons (hidden until done)
```

### Tree structure

```
▶ [checkbox] cons                           (section header, <details>)
    ☐ [badge: NEW]       BlackAlpsCon       (collection row)
    ☐ [badge: UPDATED]   BalCCon   v1→v2   (collection row, clickable to expand)
    ☐ [badge: UNCHANGED] 44CON             (collection row)
        ↳ (lazy-loaded on expand via GET /api/diff/cons/44CON)
            ☐ [badge: MISSING] 44CON 2019/talk.mp4
```

Checkboxes: section-level checks/unchecks all children. Child unchecks propagate "indeterminate" state upward.

### Progress panel

Opens `EventSource('/api/progress')`. Renders active file list (up to `download_workers` rows) with per-file bars, bytes, speed. On `done` event: show results with succeeded/skipped/failed counts. Failed rows get "Retry" button.

---

## Step 9 — Tests

### `tests/fixtures/sample_fancyindex.html`

Minimum realistic HTML reproducing:
- `<table id="list">` with `<thead>` containing sort links (`?C=N&O=A`, `?C=N&O=D`, etc.)
- Parent directory row: `<a href="../">Parent directory/</a>`
- Directory row with title: `<a href="ATT%26CKcon/" title="ATT&amp;CKcon">ATT&amp;CKcon/</a>` size=`-`
- File row with size: `<a href="2600%20archive%20v1%20-%20infocon.org.torrent" title="2600 archive v1 - infocon.org.torrent">` size=`687.7 KiB`
- URL-encoded name: `<a href="Black%20Hat/" title="Black Hat">`
- Off-host absolute URL: `<a href="https://example.com/foo">external</a>`

### `tests/test_crawler.py`

- `test_ignores_sort_links` — hrefs starting with `?` excluded
- `test_ignores_parent` — `../` excluded
- `test_ignores_off_host` — absolute URL to different host excluded
- `test_classifies_dirs` — hrefs ending `/` → `is_dir=True`
- `test_classifies_files` — other hrefs → `is_dir=False`
- `test_name_from_title` — title attr HTML-unescaped: `ATT&amp;CKcon` → `ATT&CKcon`
- `test_parses_size_kib` — `687.7 KiB` → `703,692` bytes (approximately)
- `test_parses_size_gib` — `5.9 GiB` → correct byte count
- `test_size_dash_is_none` — size `-` → `None`

### `tests/test_scanner.py`

- `test_versioned_v1` — `"2600 archive v1 - infocon.org.torrent"` → `version=1`
- `test_versioned_v2` — → `version=2`
- `test_versioned_combined_v1v2` — `"... v1v2 - infocon.org.torrent"` → `version=2`
- `test_versioned_ampersand` — `"ATT&CKcon archive v2 - infocon.org.torrent"` → `collection="ATT&CKcon"`, `version=2`
- `test_unversioned_podcast` — `"Darknet Diaries archive.torrent"` → `version=None`
- `test_non_match` — unrelated filename → `None`

### `tests/test_diff.py`

Fixture trees as plain dicts, no filesystem:
- `test_new_collection` — remote folder, no local → `NEW`
- `test_updated_version` — remote v2, local v1 → `UPDATED`
- `test_unchanged` — same folders and same max torrent version → `UNCHANGED`
- `test_local_only` — local folder not in remote → `LOCAL_ONLY`
- `test_podcast_unversioned_missing_torrent` — remote has `archive.torrent`, local doesn't → `UPDATED`
- `test_deep_missing_file` — remote path not in local set → `missing`
- `test_deep_present_file` — matching path and size → `present`
- `test_deep_changed_file` — size mismatch → `changed`

---

## Settled decisions (all questions resolved)

| # | Question | Decision |
|---|----------|----------|
| 1 | `mirrors/` and `rainbow tables/` scope | Opt-in: not in default section list, available via `--section mirrors` / `--section "rainbow tables"` |
| 2 | Podcast update detection | No deep diff. If both local and remote have a torrent → Unchanged. Missing torrent → Updated. No attempt to detect new episodes within an existing podcast. |
| 3 | Download verification without Content-Length | Trust clean transfer. No size verification. `.part` + resume is the recovery path. |
| 4 | Persistent `--root` config | Save to `~/.config/infocon-sync/config.json` after first use. First launch requires `--root`; subsequent launches use saved value. |
| 5 | `word lists/` display | Show individual files directly in the UI (flat section has no collection folders to expand). |
