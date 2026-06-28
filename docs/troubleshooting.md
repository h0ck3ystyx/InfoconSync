# Troubleshooting

## "Legacy version" status after Check

**Symptom:** A collection shows "Legacy version" (teal badge) after running Check upstream.

**Explanation:** All files are present locally, but they are larger than what the current torrent manifest expects. This is the expected result when you have v1 originals (higher-bitrate, uncompressed) and the InfoCon archive has since published v2 re-encoded versions that are smaller. The content is likely still valid — no re-download is needed.

**What to do:** Nothing, if you want to keep the original quality files. If you want the v2 re-encoded version to match the current torrent exactly, select the collection and create a plan — it will re-download the v2 files.

## Status stays "Present, unverified" after Check

**Symptom:** A collection still shows "Present, unverified" even after "Check upstream" completes.

**Causes:**
1. **No torrent published upstream** — The Details column will show "— No torrent". Nothing to verify against; HTTPS fallback is the only option.
2. **Torrent URL returned 404** — The upstream listing references a torrent that is no longer available. Report to InfoCon administrators.
3. **Collection not found in the current section listing** — The check may have scanned a different section. Try running Check with `--section NAME`.

## No peers / torrent stalled

**Symptom:** Transfer shows "Awaiting peers" or progress is zero.

**Causes and actions:**
1. **Tracker is unreachable** — The plan shows the tracker host. Verify you can reach it from a browser.
2. **No seeders online** — The InfoCon archive relies on volunteer seeders. Try again later.
3. **Firewall blocking incoming connections** — Outgoing connections to trackers and peers are needed. Check your firewall rules.
4. **Use HTTPS fallback** — If the torrent has been stuck for a long time and the file is available directly, use the **Use HTTPS for this item** button in the plan screen. You will be asked to confirm because HTTPS transfers produce an unverified result (no piece-level verification).

## HTTP fallback — "no usable torrent" items

Some InfoCon collections do not have a published `.torrent` file. These are automatically
planned as HTTPS fallback items. The plan screen shows the reason, which is one of:

| Reason | Meaning |
|---|---|
| `NO_TORRENT` | No `.torrent` link found in the upstream listing |
| `TORRENT_MALFORMED` | A `.torrent` was found but could not be parsed |
| `TORRENT_UNSAFE_PATH` | The torrent contains path components that fail safety validation |
| `TORRENT_UNSUPPORTED` | The torrent uses features the engine does not support |

HTTPS downloads produce a **Downloaded, unverified** result. To promote to **Verified current**
you must either find a valid torrent and recheck, or accept the unverified state.

## Drive disconnected mid-transfer

If you eject or disconnect the drive containing your archive root while a transfer is running:

1. Active transfers pause immediately
2. In-progress `.part` files are left intact
3. When you reconnect the same drive, the application detects the matching volume fingerprint and offers to resume
4. If you connect a **different** drive to the same path, the application will not resume until you confirm the new volume

## Corrupted database

If the application crashes and the database is corrupt on next launch:

1. The original database file is preserved — never deleted automatically
2. Look for `librarian.db.backup-*` files in the data directory (created before risky migrations)
3. To reset: rename or remove `librarian.db`, then relaunch — a fresh database is created
4. Archive content is never affected by database issues

## Session expired / token invalid

If you see a 403 error when loading the UI:

- The launch token is single-use. Restarting the application generates a fresh token and opens a new browser tab.
- If the browser tab is closed without completing the bootstrap, relaunch the app.

## "Unsafe path" error in torrent metainfo

This means the `.torrent` file contains a path component that InfoCon Librarian
has rejected for safety reasons (for example, `..`, an absolute path, or a
Windows reserved device name). The torrent is not used and the item is flagged
for HTTPS fallback with a `TORRENT_UNSAFE_PATH` reason.

Report this to the InfoCon administrators so the torrent can be corrected.

## Exporting a support bundle

```bash
infocon-librarian receipts export <receipt-id>
```

This writes a JSON file to the current directory containing:
- Transfer plan details and per-item evidence
- Redacted diagnostic log excerpts
- No archive media, peer IPs, or credentials

Include this file when reporting issues.
