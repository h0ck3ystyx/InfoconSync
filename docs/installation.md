# Installation and Archive Root Selection

## Requirements

- macOS 13 (Ventura) or later (Apple Silicon or Intel)
- libtorrent-rasterbar 2.0.x (installed via Homebrew)
- Python 3.11 or 3.12
- At least 100 MB free disk space for the application data directory

## Install via Homebrew (recommended)

```bash
brew install libtorrent-rasterbar python@3.12
pip3 install infocon-librarian
```

## Install from source

```bash
git clone https://github.com/infocon/infocon-librarian
cd infocon-librarian
python3 -m venv --system-site-packages .venv   # inherit Homebrew libtorrent
.venv/bin/pip install -e .
```

## Launching

```bash
infocon-librarian --root /Volumes/Archives/InfoCon
```

The app opens a browser window at `http://127.0.0.1:<port>`. No external network
access occurs until you explicitly start a check or transfer.

## Selecting an archive root

The archive root is the top-level directory containing your InfoCon collection.
InfoCon Librarian expects a directory layout that mirrors the upstream InfoCon
archive (sections like `Defcon/`, `Blackhat/`, etc.).

Validation rules:
- Must be a writable directory
- Must not be inside the application's own data directory
- Must remain on the same volume between sessions (volume fingerprint is checked)

If the root is on a removable drive, ensure the drive is mounted before launching.
The application monitors for disconnection and will pause all active transfers if
the root becomes unavailable.

## Application data locations

On macOS, InfoCon Librarian stores its data in:

| Data | Location |
|---|---|
| Configuration | `~/Library/Application Support/infocon-librarian/` |
| Database | `~/Library/Application Support/infocon-librarian/librarian.db` |
| Logs | `~/Library/Logs/infocon-librarian/librarian.log` |
| Receipts | `~/Library/Application Support/infocon-librarian/receipts/` |
| Resume state | `~/Library/Application Support/infocon-librarian/jobs/` |

None of these directories contain archive media — only metadata and state.

## Uninstalling

1. Remove the application binary or package
2. Optionally remove application data: `rm -rf ~/Library/Application\ Support/infocon-librarian/`
3. Archive content in your configured root is never touched by uninstall
