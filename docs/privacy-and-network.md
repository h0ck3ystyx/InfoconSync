# Privacy, Network Settings, and Torrent Behaviour

## What this application does NOT do

- It never contacts any remote server until you explicitly trigger a **Check** or **Transfer**
- It never seeds archive content without your explicit approval per transfer
- It never joins DHT, PEX (Peer Exchange), or LSD (Local Service Discovery) — these are disabled by default
- It never contacts public trackers beyond those published in the `.torrent` files you have reviewed
- It never automatically downgrade a torrent transfer to HTTPS

## Privacy disclosure before every torrent plan

Before any torrent transfer begins, the application displays:

- Which tracker hosts will be contacted
- Whether DHT/PEX/LSD are disabled (they always are, by default)
- The upload rate cap
- Whether post-completion seeding is enabled (it is off by default)

You must acknowledge this disclosure before the transfer starts.

## Tracker contacts

When a torrent transfer is running, your IP address is visible to the torrent trackers
listed in the `.torrent` file. InfoCon Librarian:

- Never injects extra trackers beyond those in the published `.torrent`
- Logs tracker host names but never peer IP addresses in diagnostic logs
- Redacts peer IPs from all support bundles

## Post-completion seeding

By default, seeding stops as soon as piece verification completes. You can enable
time-limited or ratio-limited seeding per transfer in the plan review screen. Seeding
always uses the same privacy settings as the download.

## HTTPS fallback

If no usable torrent exists for an item, the plan is labelled **HTTP fallback — no usable torrent**
with a machine-readable reason. HTTPS transfers go directly to InfoCon's server.
Your IP is visible to that server.

If a torrent transfer fails because no peers are reachable, the application **does not**
automatically switch to HTTPS. You must explicitly approve the fallback using the
**Use HTTPS for this item** action.

## Local network exposure

- The Flask UI binds only to `127.0.0.1` (loopback) — it is never accessible from other machines
- A cryptographically random capability token is required for the initial session
- All state-changing API calls require a CSRF token and same-origin header
- No cookies or credentials are transmitted outside the loopback interface

## Diagnostic logs and support bundles

Logs rotate at 10 MiB, keeping five generations. They never contain:
- Archive media content
- Peer IP addresses (redacted automatically)
- Session tokens or CSRF tokens (redacted automatically)
- Absolute filesystem paths outside the application data directory

Use `infocon-librarian receipts export <id>` to produce a support bundle. The bundle
contains transfer metadata, evidence, and redacted log excerpts only.
