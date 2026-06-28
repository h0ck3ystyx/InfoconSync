# InfoCon Librarian — Product Specification

**Status:** proposed replacement for `infocon-sync-requirements.md`  
**Product:** a local-first manager for an existing InfoCon archive  
**Primary transfer:** BitTorrent  
**Fallback transfer:** direct HTTPS only when a usable torrent is unavailable or the user explicitly approves fallback after a documented torrent failure

## 1. Product thesis

InfoCon Librarian helps a person who already has an InfoCon archive on a local or external drive answer four questions safely:

1. What has changed upstream since the archive was last checked?
2. How certain is that conclusion?
3. What will a chosen update cost in disk space, time, and network exposure?
4. Can the resulting local archive be shown to match the published release?

The product is an **archive steward**, not a generic download manager. Its job is to maintain a useful, inspectable, resumable local copy of a community archive.

The software is local-only. It has no account, telemetry, hosted service, cloud sync, or remote-control capability.

## 2. Who it serves

### Primary users

- An attendee with an archive drive received at DEF CON or from a trusted peer.
- A researcher, teacher, or local DEF CON group that maintains an offline copy.
- A community archivist who wants to distribute or seed verified material without exposing an always-on public service.

### Constraints that shape the design

- Archives can be multi-terabyte and live on removable drives.
- The upstream directory listing is not a formal API and is not a complete integrity mechanism.
- Some collections have versioned torrents; some have unversioned torrents; some have none.
- Users may be on poor or untrusted networks. Torrenting exposes the user's IP address to trackers and peers; it is not anonymous.
- Users need trustworthy results more than a fast but ambiguous green badge.

## 3. Product principles

1. **Evidence over implication.** `Verified current` requires evidence; a folder's presence never proves completeness.
2. **Torrent-first, not torrent-only.** Prefer a valid published torrent for transfer and verification. Use HTTPS when no usable torrent exists, and label its verification level honestly.
3. **Never silently broaden network exposure.** The application shows trackers, peer-discovery settings, and upload behaviour before a torrent starts.
4. **Never delete archive content automatically.** Report local-only content; leave it intact.
5. **Plan before transferring.** Every run has a reviewable plan with destination, free-space impact, transfer method, and verification outcome.
6. **Local control.** No third-party frontend, analytics, or CDN assets. The UI binds to loopback only and requires an unguessable per-launch capability token.
7. **Accessible operation is a core requirement.** The complete workflow works with keyboard and screen readers and does not depend on colour.

## 4. What a torrent means here

A `.torrent` file is small metadata, not the archive payload. It names the files, their exact sizes, trackers, and cryptographic piece hashes. A BitTorrent client obtains pieces from peers, checks each piece against that metadata, and reconstructs the selected files. This is why torrents provide much better completion verification than a plain HTTP stream.

Important implications:

- Downloaded data is verified against the torrent's piece hashes, but the user still needs to trust the source from which the `.torrent` file was acquired. Librarian records the HTTPS source URL, fetch time, and infohash in every receipt.
- A tracker and the peers contacted for a torrent can learn the user's IP address and the torrent identifier. A torrent is not anonymous.
- A client commonly uploads while it downloads. Librarian makes upload limits and post-completion seeding explicit, and does not continue seeding by default after completion.
- A torrent can contain many files. The client must support per-file priorities so the user can obtain a selected subset without downloading the rest.

The implementation must support BitTorrent v1 torrents and should support v2/hybrid torrents. It must use a mature maintained BitTorrent engine rather than implementing the wire protocol itself. BitTorrent's specifications define torrent file lists, piece hashes, tracker disclosure, and v2 path-sanitisation concerns.

## 5. Scope

### In scope

- Configure and validate a local archive root.
- Discover upstream directory entries and `.torrent` files.
- Inspect torrent metadata without joining a swarm.
- Compare a local snapshot, remote listing, and torrent manifest to produce evidence-backed change states.
- Build and execute a transfer plan, selecting torrent or HTTPS per item.
- Resume interrupted transfers across restarts.
- Verify torrent transfers through the engine's completed piece checks.
- Record local receipts, logs, and manifests.
- Search the local archive catalogue by path, collection, conference, and year-like tokens.

### Out of scope

- Hosting an InfoCon tracker, index, or public mirror.
- Automatically publishing the user as a seed.
- Anonymous networking or anonymity guarantees.
- Deleting, moving, renaming, or deduplicating existing archive files automatically.
- General-purpose torrent management unrelated to selected InfoCon content.

## 6. Information model and status vocabulary

### Archive states

| State | Meaning | Evidence shown to the user |
|---|---|---|
| `New` | No matching local item exists. | Remote directory or torrent manifest. |
| `Changed — release marker` | A newer or different upstream torrent marker exists. | Old/new filename, versions, infohashes. |
| `Changed — manifest` | A file path, size, or torrent manifest differs. | File-level differences. |
| `Verified current` | Local files pass the current torrent's piece check or manifest verification (all paths and sizes match exactly). | Verification time and infohash/manifest ID. |
| `Legacy version` | All files are present but larger than the current torrent expects — characteristic of pre-re-encoding originals (e.g. v1 content when the archive now publishes v2 re-encoded files). Content is likely valid; no re-download is needed. | Torrent manifest sizes vs local sizes; count of affected files. |
| `Present, unverified` | Matching paths exist but have not been verified against a current manifest. | Local scan only. |
| `Unknown` | Upstream lacks enough information to determine a state cheaply. | Explain what evidence is missing and offer verification. |
| `Local only` | Local content has no corresponding current upstream item. | Local path and last-seen upstream time. |
| `Transfer incomplete` | A resumable job exists but has not completed. | Transfer method, bytes/pieces completed, last error. |
| `Downloaded, unverified` | HTTPS transfer completed without a cryptographic verification source. | Remote listing size/date and reason a torrent was unavailable. |

`Unchanged` is not a user-facing state. It conceals uncertainty. The UI uses `Verified current`, `Present, unverified`, or `Unknown` instead.

### Verification levels

The application tracks two verification methods with different cost/confidence trade-offs:

| Level | Method | Cost | Confidence |
|---|---|---|---|
| `manifest_verified` | Stat each file; compare size to torrent manifest | Fast — no I/O reads, seconds per collection | Path and size match; no cryptographic guarantee |
| `piece_verified` | Torrent engine reads and hashes every declared piece | Slow — full disk read | Cryptographically matches published torrent |
| `has_older_version` | Manifest check: all present, only larger-than-expected sizes | Fast | Content likely valid; not the current torrent version |
| `unverified` | Manifest check found missing or truncated files | Fast | Known to be incomplete or corrupted |
| `no_torrent` | No torrent file found upstream | Instant | Cannot verify |

"Check upstream" automatically runs manifest verification for all `present_unverified` collections: it first uses stored database results (no network), then fetches torrents for any collection not yet in the database and runs a fresh manifest check. Piece verification remains on-demand only.

### Collection evidence

Every displayed state links to an evidence panel containing:

- discovery URL and fetch time;
- torrent filename, URL, version marker, and v1/v2 infohash where available;
- torrent file count and exact total bytes;
- selected-file count and total bytes;
- local verification time and result;
- all assumptions used to classify the state.

## 7. Transfer policy

### Selection algorithm

For each collection or file group, Librarian selects a transfer method in this order:

1. **Torrent** if a published `.torrent` is retrievable, parses successfully, maps safely into the selected archive location, covers the selected content, and is supported by the configured torrent engine.
2. **HTTPS** when no usable torrent covers the item. The plan labels the item `HTTP fallback — no usable torrent` and explains why: no torrent published, malformed torrent, unsupported format, or torrent does not contain the selected file.
3. **No automatic transfer** when a torrent exists but has temporarily failed to find peers or a tracker. The UI offers `Retry torrent` and, separately, an explicit `Use HTTPS for this item` action. It must never silently downgrade a transfer from torrent to HTTPS.

This preserves a torrent-first policy while preventing a broken swarm from trapping the user without an informed choice.

### Torrent start checklist

Before starting a torrent, the plan displays:

- tracker hostnames and protocol (`https`, `http`, or `udp`);
- whether DHT, peer exchange (PEX), and local peer discovery are enabled;
- selected files and skipped files;
- download and upload rate caps;
- post-completion seeding setting;
- destination mapping and free-space requirement;
- torrent infohash(es).

Defaults:

- do not seed after a completed transfer;
- cap uploads to a conservative configurable value while downloading;
- disable DHT, PEX, and local peer discovery by default unless the user enables them for that transfer;
- use only trackers embedded in the fetched torrent; do not append public trackers;
- retain the `.torrent` file and transfer receipt locally for reproducibility;
- verify existing local data before downloading so already-present valid pieces are reused.

### Optional community-seeding mode

`Support this swarm` is opt-in, per torrent, and time-bounded. It lets a user seed verified content after completion with a visible upload cap, expiry time or share ratio, and a one-click stop control. The default remains off. This makes community participation deliberate rather than an accidental side effect of installing the app.

### HTTPS behaviour

- HTTPS jobs use a private `.part` file and atomic same-directory final rename.
- Resume only after a valid `206 Partial Content` response and a matching `Content-Range`. A server response that ignores a range request must never be appended to an existing partial file.
- Persist source URL plus remote listing size/date with the partial job. If those change before resume, quarantine the partial and ask the user to restart or retain it.
- Verify against a checksum or torrent manifest if one becomes available. If not, report `Downloaded, unverified`; do not describe it as verified merely because the stream closed cleanly.

## 8. Core user experience

### First-run workflow

1. Choose an archive root or mount an archive drive.
2. Librarian checks that the path is writable, has a stable volume identity, contains expected section names when applicable, and has enough free space for metadata.
3. The user can import an existing release receipt, if one was supplied with the archive.
4. Librarian performs a local inventory, storing a portable archive snapshot outside the media tree by default.
5. It presents a short network privacy notice before the first torrent-enabled plan.

The first scan must not hash every byte of a multi-terabyte archive. Hashing and torrent rechecks are opt-in or occur only for selected collections.

### Home screen

The home screen is a change feed, not an all-sections tree:

- `Needs attention`: new, changed, incomplete, and unknown items.
- `Ready to verify`: present local collections with available torrent manifests.
- `Recently completed`: receipts, failures, and interrupted jobs.
- `Archive health`: root availability, free space, last upstream check, unverified data count, and local-only data count.

Users can search and filter by section, collection, year, media type, status, transfer method, and size. A tree remains available as a secondary navigation view.

### Review plan

Choosing items opens a plan before any network transfer:

| Field | Required behaviour |
|---|---|
| Items | Group by collection and method; let the user expand to files. |
| Method | Show Torrent or HTTPS fallback, with the reason. |
| Size | Use exact torrent-manifest bytes where available; otherwise clearly label directory-listing sizes as estimates. |
| Disk impact | Include new bytes, temporary space, and available space on the actual destination volume. |
| Integrity outcome | `Piece-verified`, `manifest-verified`, or `unverified`. |
| Privacy/network | Show tracker hosts, discovery settings, and upload/seeding state for each torrent. |
| Conflicts | Detect pre-existing nonmatching files, case collisions, invalid names, or paths escaping the archive root. |

The transfer button states its action precisely, for example: `Start 3 torrents and 2 HTTPS fallbacks`.

### Transfer screen

- Show torrents and HTTPS jobs separately, with current file/piece state, source count, speed, ETA when defensible, uploaded bytes, and retry reason.
- `Pause`, `resume`, `cancel`, `retry torrent`, and `use HTTPS instead` are item-level controls.
- Cancellation preserves valid torrent data and HTTPS partials for safe resumption.
- A completed torrent triggers a final engine recheck before being marked `Piece-verified`.

### Receipt and recovery

Each completed or failed plan produces a JSON receipt and readable summary containing:

- application and torrent-engine version;
- archive volume ID and root-relative paths, never an unnecessary absolute personal path;
- source URLs, infohashes, tracker hostnames, and discovery settings;
- selected files, expected bytes, verification result, and transfer method;
- start/end times, errors, and whether any HTTPS results remain unverified.

Receipts make a drive inspectable months later and can be exported without the broader application cache.

## 9. Torrent integration architecture

### Required capability boundary

The Python application owns discovery, policy, UI, job persistence, destination validation, and receipts. A **torrent adapter** owns protocol execution and exposes only these product-level operations:

- parse metainfo and return files, exact sizes, trackers, infohashes, and supported protocol version;
- add a torrent with a controlled save root and selected-file priorities;
- recheck existing data;
- pause, resume, remove transfer state while keeping data, and report progress;
- configure upload/download caps and peer-discovery features;
- report completion only after piece verification;
- report tracker and peer failures without exposing sensitive peer details in the normal UI.

The implementation must use a mature engine with a supported v1/v2/hybrid feature set. The chosen engine is a dependency to package and update deliberately, not an optional binary discovered at runtime. Do not write a BitTorrent client from scratch.

### Destination safety

Torrent metadata names are untrusted input even when fetched over HTTPS. Before a torrent is added:

- map every selected path to a root-relative, normalized destination;
- reject absolute paths, `.`/`..`, empty path components, platform-reserved names, and any path whose resolved parent escapes the archive root;
- reject or require user resolution for case-insensitive collisions;
- never follow a symlink out of the archive root;
- place each torrent's temporary state in an application-controlled directory on the same volume when possible;
- ensure the torrent's advisory root name cannot override the intended collection path.

### Existing-data adoption

When a torrent covers an already-present collection, Librarian asks the engine to recheck the target data before requesting peers. Valid pieces are reused; invalid pieces are classified, never overwritten silently. This turns a distributed archive drive into a useful seed candidate only when the user later opts in.

## 10. Remote discovery and comparison

### Inputs

- Upstream directory listings, parsed defensively from links and row metadata.
- Published `.torrent` files, fetched independently and cached by URL plus infohash.
- Local archive snapshot and receipts.
- On-demand torrent manifest inspection.

### Discovery rules

- Use raw relative `href` values as the canonical remote path. A decoded display title is optional presentation metadata, not identity.
- Ignore parent, sort, anchor, and off-host links.
- Treat listing size and modification time as hints, not integrity assertions.
- Cache results with source timestamp and HTTP response validators when available.
- Do not assume a naming convention is universal. Torrent filename versions are useful change hints but never the only correctness test.

### Efficient comparison stages

1. **Index check:** fetch only section-level listings and identify candidate changes through collection presence, torrent name, URL, version marker, and previously seen infohash.
2. **Auto manifest check (during "Check upstream"):** for every `present_unverified` collection, the check runner first consults stored verification results in the database; for any not yet verified, it fetches the torrent and stat-checks each declared file against the local copy. This is fast (no disk I/O reads, no peer contact) and updates status to `Verified current`, `Legacy version`, or flags genuine missing/truncated files.
3. **On-demand manifest verify:** user-triggered verification of a single collection; same stat-based approach, full file-list details returned to the UI.
4. **Data verification (piece check):** ask the torrent engine to piece-check selected local files. This is on-demand because it reads all selected data from disk and takes minutes for large collections.
5. **Deep HTTPS diff:** only for content without usable torrents or for explicit investigation.

This supports fast first answers without pretending that a shallow marker scan proves archive completeness.

## 11. Security and privacy requirements

### Local service

- Bind only to `127.0.0.1` and `::1`, never an external interface.
- Generate a cryptographically random per-launch token; require it for state-changing routes and the UI bootstrap.
- Verify `Host` and same-origin `Origin` for state-changing requests. Do not enable permissive CORS.
- The browser never submits arbitrary URLs or destination paths. It submits opaque server-issued item IDs from the current plan.
- Canonicalize archive paths and enforce root containment at the last write operation, not only when a plan is created.
- Store logs and receipts with user-only permissions where supported. Do not log peer IP addresses by default.

### Privacy disclosure

Before the first torrent transfer and in every plan, plainly state:

> Torrent peers and trackers can observe your IP address and the torrent you request. This software does not provide anonymity. Use a network and privacy configuration appropriate to your threat model.

The app must not recommend using a torrent on a conference network as a default. It may provide a link to the user’s own network guidance, but makes no security claims beyond its implemented controls.

## 12. Accessibility and interaction requirements

- All actions are keyboard operable; the current focus is always visible.
- Statuses include icon, text, and accessible name; colour is supplementary only.
- Progress uses semantic progress controls and live-region announcements that do not flood screen readers.
- Rows have large targets and do not rely on hover-only controls.
- Respect reduced-motion and system contrast preferences.
- No requirement for a mouse, high-bandwidth assets, or JavaScript from a third party.
- The UI stays useful at narrow widths and at 200% browser zoom.

## 13. CLI surface

The GUI is primary, but the CLI exposes the same safe model:

```text
infocon-librarian --root PATH
infocon-librarian check [--section NAME] [--fresh]
infocon-librarian plan --new --changed [--format json]
infocon-librarian verify COLLECTION
infocon-librarian sync PLAN_ID
infocon-librarian receipts list|show|export
```

`sync` executes a previously created server-side or exported plan; it does not accept arbitrary remote URLs or arbitrary destination paths.

Useful options:

```text
--torrent-mode auto|only|off        default: auto
--allow-http-fallback               default: false for a failed torrent; automatic only when no usable torrent exists
--download-limit RATE
--upload-limit RATE
--seed-for DURATION                 opt-in, default: 0
--enable-dht                        opt-in, default: false
--enable-pex                        opt-in, default: false
--verify-on-complete                default: true for torrent jobs
--dry-run
```

## 14. Data and retention

Store application state under the platform-appropriate application-data directory, never in the archive root by default:

```text
config.json                 archive roots and user preferences
archive-snapshots/          compact local inventories
torrents/                   fetched .torrent files keyed by infohash
jobs/                       resumable plan and engine state references
receipts/                   immutable transfer receipts
logs/                       bounded, rotating diagnostic logs
```

The archive root may contain only media and an optional user-requested portable receipt bundle. Users can export a portable bundle containing manifests and receipts, but never credentials or tracker cookies.

## 15. Delivery phases

### Phase 1 — trustworthy torrent-first archive updates

- Archive-root validation and local snapshots.
- Upstream listing and torrent discovery.
- Torrent manifest inspection, plan review, and selected-file torrent transfers.
- A packaged mature torrent engine plus adapter.
- HTTPS fallback only under the policy in section 7.
- Piece-verified receipts, safe pause/resume, free-space checks, and local API protections.
- Change feed, tree view, accessibility baseline, and headless check/plan commands.

### Phase 2 — stronger catalogue and recovery

- Local full-text index and richer conference/year facets.
- Import/export portable receipt bundles.
- Background-scheduled **check only** operation; no scheduled downloads.
- Better conflict resolution and local-only reporting.
- Optional community-seeding mode.

### Phase 3 — archival quality improvements

- Optional manifest signatures if InfoCon publishes signed release metadata.
- Compare multiple trusted sources and flag disagreement.
- Archive-health reports and integrity sampling.
- Read-only sharing of exported plans/receipts for community maintainers.

## 16. Acceptance criteria

The product is ready for Phase 1 only when all of the following are true:

1. A collection with a valid published torrent uses the torrent engine by default; the plan shows its trackers, infohash, selected files, upload settings, and destination before transfer starts.
2. A torrent-completed collection is marked `Piece-verified` only after the engine reports a successful final recheck.
3. An item with no usable torrent can use HTTPS and is labelled `Downloaded, unverified` unless a checksum or later torrent verification is available.
4. A torrent with no peers or tracker failure does not silently switch to HTTPS. The user sees the cause and must explicitly choose fallback.
5. Restarting preserves torrent state and valid local pieces; HTTPS resumes only after validating range semantics.
6. An existing incomplete folder is never called verified merely because it exists. It is `Present, unverified` or `Unknown` until a manifest or torrent recheck supports a stronger claim.
7. The app rejects malicious torrent paths and all browser-originated paths/URLs that escape the chosen archive root.
8. The local UI is reachable only on loopback and protects state-changing requests with a per-launch token and origin checks.
9. The full check, review, transfer, pause/resume, and receipt workflows are keyboard operable and screen-reader legible.
10. A receipt records the source, method, verification state, and all unfinished/unverified results so an archive owner can audit the result later.

## 17. Decisions intentionally deferred

- **Torrent engine choice:** evaluate packaging, supported v1/v2/hybrid features, maintenance, license, sandboxing, and native-platform support before locking an engine. This is an engineering spike, not a product choice to make casually.
- **Signed upstream manifests:** adopt if InfoCon publishes a stable signed release manifest or agrees to host one.
- **Default upload cap:** test with the selected engine and document a conservative default; the cap is configurable by the user.
- **Direct HTTPS size verification:** retain remote listing size only as an estimate until the upstream provides cryptographic checksums or a reliable content-length/checksum endpoint.

## 18. Implementation notes for the team

The old `New / Updated / Unchanged` shallow-diff model should be retired. Keep torrent filename parsing only as an index optimization and display clue. Torrent metadata inspection provides exact path/size membership; the torrent engine's recheck provides data integrity. Those are the sources of truth.

Do not build a BitTorrent protocol client. Use the engine integration behind a narrow adapter and fixture-test the adapter contract with v1, v2/hybrid, multi-file, malformed, and path-traversal torrents. Test destination mapping on case-sensitive and case-insensitive filesystems, removable-drive disconnects, no-peer torrents, tracker outages, and interrupted jobs.

## References

- BitTorrent Protocol Specification (BEP 3): https://www.bittorrent.org/beps/bep_0003.html
- BitTorrent Protocol Specification v2 (BEP 52): https://www.bittorrent.org/beps/bep_0052.html
- DEF CON NOC network guidance: https://noc.defcon.org/about/
