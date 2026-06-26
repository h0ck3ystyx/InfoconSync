#!/usr/bin/env python3
"""Generate deterministic test torrent fixtures.

Run this script to regenerate test/fixtures/torrents/*.torrent and
tests/fixtures/torrents/hashes.json. The output is deterministic for
a given libtorrent version and seeded file contents.

Usage:
    python scripts/generate-test-torrents.py [--output-dir tests/fixtures/torrents]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import tempfile
from pathlib import Path

# Make sure we can import libtorrent from the venv or system
try:
    import libtorrent as lt  # type: ignore[import]
except ModuleNotFoundError:
    sys.exit("libtorrent not found. Activate the venv or install via brew.")


# ---------------------------------------------------------------------------
# Deterministic file content helpers
# ---------------------------------------------------------------------------

def _file_content(seed: int, size: int) -> bytes:
    """Generate deterministic pseudo-random content for a file."""
    rng = seed
    chunks = []
    remaining = size
    while remaining > 0:
        rng = (rng * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
        chunk = struct.pack(">Q", rng)
        take = min(8, remaining)
        chunks.append(chunk[:take])
        remaining -= take
    return b"".join(chunks)


def _populate_dir(root: Path, spec: list[tuple[str, int, int]]) -> None:
    """Create files under root according to spec (path, seed, size)."""
    for rel_path, seed, size in spec:
        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_file_content(seed, size))


def _make_file_storage(files: list[tuple[str, int]]) -> lt.file_storage:
    """Build a file_storage from [(relative_path, size)] pairs."""
    fs = lt.file_storage()
    for path, size in files:
        fs.add_file(path, size)
    return fs


# ---------------------------------------------------------------------------
# Torrent creation helpers
# ---------------------------------------------------------------------------

PIECE_LENGTH = 16 * 1024  # 16 KiB — small for fast tests

# In libtorrent 2.0, create_torrent_flags_t integer constants:
_FLAG_V1_ONLY = 64
_FLAG_V2_ONLY = 32


def _create_v1(
    tmp_dir: Path,
    name: str,
    files: list[tuple[str, int, int]],  # (path, seed, size)
    tracker_url: str = "http://tracker.local:6969/announce",
) -> bytes:
    """Create a v1-only torrent from real file content."""
    _populate_dir(tmp_dir, files)

    fs = lt.file_storage()
    for rel_path, _seed, size in files:
        fs.add_file(rel_path, size)
    fs.set_name(name)

    ct = lt.create_torrent(fs, PIECE_LENGTH, _FLAG_V1_ONLY)
    ct.add_tracker(tracker_url)
    ct.set_comment("InfoCon Librarian test fixture")
    ct.set_creator("generate-test-torrents.py")
    ct.modification_time = 0  # epoch — ensures deterministic output

    lt.set_piece_hashes(ct, str(tmp_dir))
    return _strip_creation_date(lt.bencode(ct.generate()))


def _create_hybrid(
    tmp_dir: Path,
    name: str,
    files: list[tuple[str, int, int]],
    tracker_url: str = "http://tracker.local:6969/announce",
) -> bytes:
    """Create a v1/v2 hybrid torrent."""
    _populate_dir(tmp_dir, files)

    fs = lt.file_storage()
    for rel_path, _seed, size in files:
        fs.add_file(rel_path, size)
    fs.set_name(name)

    # Default flags create hybrid
    ct = lt.create_torrent(fs, PIECE_LENGTH)
    ct.add_tracker(tracker_url)
    ct.set_comment("InfoCon Librarian hybrid test fixture")
    ct.set_creator("generate-test-torrents.py")
    ct.modification_time = 0  # epoch — ensures deterministic output

    lt.set_piece_hashes(ct, str(tmp_dir))
    return _strip_creation_date(lt.bencode(ct.generate()))


def _strip_creation_date(torrent_bytes: bytes) -> bytes:
    """Remove the creation date to produce a deterministic .torrent file."""
    decoded: dict = lt.bdecode(torrent_bytes)
    decoded.pop(b"creation date", None)
    return lt.bencode(decoded)


def _torrent_hashes(torrent_bytes: bytes) -> dict[str, str | None]:
    ti = lt.torrent_info(torrent_bytes)
    ih = ti.info_hashes()
    return {
        "v1": str(ih.v1) if ih.has_v1() else None,
        "v2": str(ih.v2) if ih.has_v2() else None,
    }


# ---------------------------------------------------------------------------
# Fixture definitions
# ---------------------------------------------------------------------------

FIXTURES: list[dict] = [
    {
        "name": "single_file_v1",
        "description": "v1-only torrent with a single file",
        "fn": "single_file_v1.torrent",
        "kind": "v1",
        "files": [("single_file_v1/talk.mp4", 1, 32 * 1024)],
    },
    {
        "name": "multi_file_v1",
        "description": "v1-only torrent with multiple files across subdirectories",
        "fn": "multi_file_v1.torrent",
        "kind": "v1",
        "files": [
            ("multi_file_v1/slides.pdf", 10, 20 * 1024),
            ("multi_file_v1/audio/talk.mp3", 11, 48 * 1024),
            ("multi_file_v1/video/talk.mp4", 12, 64 * 1024),
        ],
    },
    {
        "name": "multi_file_hybrid",
        "description": "v1/v2 hybrid torrent with multiple files",
        "fn": "multi_file_hybrid.torrent",
        "kind": "hybrid",
        "files": [
            ("multi_file_hybrid/slides.pdf", 20, 20 * 1024),
            ("multi_file_hybrid/talk.mp4", 21, 48 * 1024),
        ],
    },
]

# Malformed fixtures are static byte sequences — no libtorrent involvement
MALFORMED_FIXTURES: list[dict] = [
    {
        "name": "malformed_truncated",
        "description": "Truncated bencode — not a valid torrent",
        "fn": "malformed_truncated.torrent",
        "content": b"d8:announce",  # incomplete bencode
    },
    {
        "name": "malformed_empty",
        "description": "Empty file",
        "fn": "malformed_empty.torrent",
        "content": b"",
    },
    {
        "name": "malformed_path_traversal",
        "description": "Torrent with a Windows-reserved filename (NUL) that passes libtorrent sanitization but must be rejected by our validator",
        "fn": "malformed_path_traversal.torrent",
        "content": None,  # generated below
    },
]


def _build_path_traversal_torrent() -> bytes:
    """Build a v1 torrent with a Windows-reserved filename (NUL).

    Note: libtorrent 2.0 sanitizes `..` components automatically.
    To test our own validator we use a reserved name that libtorrent
    passes through unchanged (NUL is harmless on macOS/Linux but
    dangerous on Windows, so cross-platform safety requires rejection).
    """
    import hashlib

    piece = b"\x00" * PIECE_LENGTH
    piece_hash = hashlib.sha1(piece).digest()  # noqa: S324

    torrent: dict = {
        b"info": {
            b"name": b"evil",
            b"piece length": PIECE_LENGTH,
            b"pieces": piece_hash,
            b"files": [
                {b"path": [b"NUL"], b"length": PIECE_LENGTH},
            ],
        }
    }
    return lt.bencode(torrent)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate test torrent fixtures")
    parser.add_argument(
        "--output-dir",
        default="tests/fixtures/torrents",
        help="Directory to write .torrent files and hashes.json",
    )
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, dict] = {}

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        for spec in FIXTURES:
            print(f"Generating {spec['fn']} ...")
            if spec["kind"] == "v1":
                data = _create_v1(tmp_path, spec["files"][0][0].split("/")[0], spec["files"])
            else:
                data = _create_hybrid(tmp_path, spec["files"][0][0].split("/")[0], spec["files"])

            (out_dir / spec["fn"]).write_bytes(data)
            hashes = _torrent_hashes(data)
            manifest[spec["name"]] = {
                "filename": spec["fn"],
                "description": spec["description"],
                "kind": spec["kind"],
                "v1_infohash": hashes["v1"],
                "v2_infohash": hashes["v2"],
                "files": [
                    {"path": f[0], "size": f[2]} for f in spec["files"]
                ],
                "sha256": hashlib.sha256(data).hexdigest(),
            }

        # Malformed fixtures
        for mf in MALFORMED_FIXTURES:
            print(f"Writing {mf['fn']} ...")
            if mf["fn"] == "malformed_path_traversal.torrent":
                content = _build_path_traversal_torrent()
            else:
                content = mf["content"]
            (out_dir / mf["fn"]).write_bytes(content)
            manifest[mf["name"]] = {
                "filename": mf["fn"],
                "description": mf["description"],
                "kind": "malformed",
                "sha256": hashlib.sha256(content).hexdigest(),
            }

    hashes_path = out_dir / "hashes.json"
    hashes_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"\nWrote {len(manifest)} fixtures to {out_dir}/")
    print(f"Manifest: {hashes_path}")


if __name__ == "__main__":
    main()
