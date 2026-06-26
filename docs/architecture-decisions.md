# Architecture Decisions

## ADR-001: libtorrent adapter and packaging strategy

**Status:** Accepted  
**Date:** 2026-06-26  
**Phase:** 0 — torrent-engine feasibility spike

---

### Context

The product requires a BitTorrent engine capable of:
- Parsing v1 and v2/hybrid `.torrent` metainfo without network contact
- Per-file priority selection (download a subset of a multi-file torrent)
- Piece-level verification of existing local data before requesting peers
- Pause, resume, and resume-data persistence across process restarts
- Session-level and per-torrent control of DHT, PEX, LSD, UPnP, and NAT-PMP
- Rate limiting (upload and download)

The implementation plan explicitly prohibits implementing the BitTorrent wire protocol from scratch and requires a mature maintained engine.

### Decision

Use **libtorrent 2.0.x** (the `libtorrent-rasterbar` C++ library) via its Python bindings, installed through Homebrew on macOS.

**Version pinned:** `libtorrent-rasterbar 2.0.13` (Homebrew, macOS arm64/Tahoe)

### Rationale

libtorrent 2.0.x was selected over alternatives based on:

| Criterion | libtorrent 2.0.x | python-qbittorrent | Transmission RPC | aria2 RPC |
|---|---|---|---|---|
| v1 + v2/hybrid support | ✓ | via qBittorrent | v1 only | v1 only |
| In-process Python bindings | ✓ | ✗ (HTTP RPC) | ✗ (RPC) | ✗ (RPC) |
| Per-file priorities | ✓ | ✓ | limited | ✓ |
| Resume data API | ✓ (bencode blob) | ✗ | ✗ | ✗ |
| DHT/PEX/LSD per-session disable | ✓ | depends on daemon | ✗ | partial |
| Piece-recheck API | ✓ | via forced recheck | ✓ | ✗ |
| License | BSD-3-Clause | MIT | GPL | GPL |
| Maintenance | Active | Active | Active | Active |

RPC-based engines (qBittorrent, Transmission, aria2) require an external daemon that the user must install and run separately, which violates the "local-only, no external services" product principle and makes packaging unpredictable.

### Packaging approach on macOS

libtorrent is installed via **Homebrew** (`brew install libtorrent-rasterbar`). This formula:
- Provides pre-built arm64 and x86_64 bottles
- Depends on `python@3.14` and installs Python bindings at `/opt/homebrew/lib/python3.14/site-packages/`
- Includes all native dependencies (boost, boost-python3, openssl@3)

The project venv is created with `--system-site-packages` to inherit the Homebrew-installed bindings:

```bash
python3.14 -m venv .venv --system-site-packages
```

This avoids re-building the C++ extension from source (which requires a matching Boost build) while keeping all other Python dependencies isolated.

**Consequence:** the venv's Python version must match the version that Homebrew's `libtorrent-rasterbar` formula was compiled against (`python@3.14` at time of writing). If Homebrew upgrades the formula's Python dependency, the venv must be recreated.

### Privacy and security controls

All adapter sessions are created with the following defaults, which cannot be overridden by callers:

| Feature | Default | Mechanism |
|---|---|---|
| DHT | Disabled | `enable_dht=False` in session settings |
| LSD | Disabled | `enable_lsd=False` in session settings |
| NAT-PMP | Disabled | `enable_natpmp=False` in session settings |
| UPnP | Disabled | `enable_upnp=False` in session settings |
| PEX | Disabled | `disable_pex` flag on every torrent handle |
| Public trackers | Not injected | Only trackers embedded in the fetched `.torrent` file are used |

Per-torrent flags (`disable_dht | disable_pex | disable_lsd`) are set on every added torrent as defense-in-depth, in addition to the session-level settings.

The Flask server listener and the libtorrent session listener are on separate sockets. libtorrent listens on `127.0.0.1:0` (loopback only, OS-assigned port) by default. This port is never exposed to the UI.

### Adapter boundary

`LibtorrentAdapter` is the only code that imports `libtorrent`. All higher layers depend on the `TorrentAdapter` protocol (structural subtyping via `Protocol`). This boundary:
- Lets unit tests swap in a `FakeTorrentEngine` without the C extension
- Isolates resume-data format changes to a single module
- Makes engine replacement feasible if libtorrent packaging fails on a target platform

`LibtorrentAdapter` is **not thread-safe**. It must be owned and called exclusively by `TransferManager`'s worker thread.

### Phase 0 exit criteria met

- [x] `inspect()` returns exact files, sizes, trackers, and stable infohashes for v1 and hybrid fixtures (TE-001, TE-002)
- [x] Malformed bencode is rejected with a typed error before any session/network activity (TE-003)
- [x] Windows-reserved path components (e.g. `NUL`) that pass libtorrent's own sanitization are rejected by our validator (TE-003)
- [x] The selected subset downloads over loopback with file priorities and the download completes correctly (TE-004)
- [x] Corrupt local data is detected during the initial recheck; zero valid pieces are counted (TE-005)
- [x] Resume data survives serialization; a fresh adapter instance continues to completion (TE-006)
- [x] DHT, PEX, LSD, UPnP, NAT-PMP are confirmed disabled at both session and per-torrent level (TE-007)
- [x] A seeding torrent can be paused and removed while data remains intact on disk (TE-008)

### Known limitations and deferred items

- **Windows/Linux packaging:** Homebrew is macOS-only. Windows packaging via vcpkg or pre-built wheels, and Linux packaging via system packages or `manylinux` wheels, are deferred to Phase 6.
- **v2-only torrents:** The current adapter parses v2-only torrents but the loopback swarm tests cover only hybrid torrents. Full v2-only swarm compatibility should be tested before Phase 3 ships.
- **Upload cap default:** A conservative default (e.g. 50 KB/s) should be set after real-world testing with the selected engine. Currently `upload_limit=0` (unlimited) is the default, which is overridden by callers.
- **Pyproject.toml does not declare libtorrent as a dependency** because it is not available on PyPI for Python 3.14. The installation step (`brew install libtorrent-rasterbar`) is documented in the README and must be run before creating the venv.
