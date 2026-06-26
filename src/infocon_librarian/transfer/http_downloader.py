"""HttpDownloader — safe, resumable HTTPS transfer for plan items.

Rules:
- URL and destination come exclusively from a plan item (no browser input).
- Writes to a `.part` sidecar; atomically renames on clean completion.
- Issues Range only for an existing sidecar whose ETag/Last-Modified still match.
- If the server ignores Range (returns 200), quarantines the partial and
  returns QUARANTINED — no silent appending.
- Output is always DOWNLOADED_UNVERIFIED unless a higher-level caller verifies.
- Progress is reported via a callback (bytes_so_far, total_bytes | None).
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx


class DownloadState(StrEnum):
    COMPLETE = "complete"
    QUARANTINED = "quarantined"
    FAILED = "failed"


@dataclass(frozen=True)
class DownloadResult:
    state: DownloadState
    destination: Path | None          # final path (only set on COMPLETE)
    downloaded_bytes: int
    total_bytes: int | None
    error: str | None = None


ProgressCallback = Callable[[int, int | None], None]

_SIDECAR_META_SUFFIX = ".part.meta"
_CHUNK = 64 * 1024  # 64 KiB


@dataclass
class _Sidecar:
    """Persistent state stored alongside the .part file."""
    etag: str | None
    last_modified: str | None
    bytes_written: int

    def to_lines(self) -> str:
        return (
            f"etag={self.etag or ''}\n"
            f"last_modified={self.last_modified or ''}\n"
            f"bytes_written={self.bytes_written}\n"
        )

    @classmethod
    def from_lines(cls, text: str) -> _Sidecar:
        data: dict[str, Any] = {}
        for line in text.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                data[k.strip()] = v.strip()
        return cls(
            etag=data.get("etag") or None,
            last_modified=data.get("last_modified") or None,
            bytes_written=int(data.get("bytes_written", 0)),
        )


def download(
    url: str,
    destination: Path,
    *,
    http_client: httpx.Client | None = None,
    progress: ProgressCallback | None = None,
    timeout: float = 60.0,
) -> DownloadResult:
    """Download *url* to *destination*, resuming if a .part sidecar exists.

    Args:
        url: Direct HTTPS URL from a plan item.
        destination: Final file path under the archive root.
        http_client: Optional shared httpx.Client; a transient one is created if None.
        progress: Called with (bytes_so_far, total_bytes_or_None) periodically.
        timeout: Per-request timeout in seconds.

    Returns:
        DownloadResult with state COMPLETE, QUARANTINED, or FAILED.
    """
    own_client = http_client is None
    client = http_client or httpx.Client(timeout=timeout, follow_redirects=True)

    try:
        return _download(url, destination, client=client, progress=progress)
    finally:
        if own_client:
            client.close()


def _download(
    url: str,
    destination: Path,
    *,
    client: httpx.Client,
    progress: ProgressCallback | None,
) -> DownloadResult:
    part_path = destination.with_suffix(destination.suffix + ".part")
    meta_path = Path(str(part_path) + ".meta")

    sidecar: _Sidecar | None = None
    if meta_path.exists() and part_path.exists():
        try:
            sidecar = _Sidecar.from_lines(meta_path.read_text())
        except Exception:
            sidecar = None

    # Build request — try Range if sidecar is valid
    headers: dict[str, str] = {}
    if sidecar is not None and sidecar.bytes_written > 0:
        headers["Range"] = f"bytes={sidecar.bytes_written}-"
        if sidecar.etag:
            headers["If-Range"] = sidecar.etag
        elif sidecar.last_modified:
            headers["If-Range"] = sidecar.last_modified

    try:
        with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 200 and sidecar is not None and sidecar.bytes_written > 0:
                # Server ignored Range → quarantine partial
                _quarantine(part_path, meta_path)
                return DownloadResult(
                    state=DownloadState.QUARANTINED,
                    destination=None,
                    downloaded_bytes=sidecar.bytes_written,
                    total_bytes=_parse_content_length(resp),
                    error="server_ignored_range",
                )

            if resp.status_code == 206:
                # Verify Content-Range matches what we asked for
                cr = resp.headers.get("content-range", "")
                if not _content_range_matches(cr, sidecar.bytes_written if sidecar else 0):
                    _quarantine(part_path, meta_path)
                    return DownloadResult(
                        state=DownloadState.QUARANTINED,
                        destination=None,
                        downloaded_bytes=sidecar.bytes_written if sidecar else 0,
                        total_bytes=None,
                        error=f"content_range_mismatch:{cr!r}",
                    )

            if resp.status_code not in (200, 206):
                return DownloadResult(
                    state=DownloadState.FAILED,
                    destination=None,
                    downloaded_bytes=0,
                    total_bytes=None,
                    error=f"http_{resp.status_code}",
                )

            # Determine total size
            total: int | None = _parse_content_length(resp)
            if sidecar and resp.status_code == 206:
                total = _parse_range_total(resp.headers.get("content-range", ""))

            # Record ETag / Last-Modified for future resume
            etag = resp.headers.get("etag") or (sidecar.etag if sidecar else None)
            last_modified = resp.headers.get("last-modified") or (
                sidecar.last_modified if sidecar else None
            )

            append = resp.status_code == 206
            bytes_so_far = sidecar.bytes_written if (sidecar and append) else 0

            destination.parent.mkdir(parents=True, exist_ok=True)
            mode = "ab" if append else "wb"
            with open(part_path, mode) as fh:
                for chunk in resp.iter_bytes(chunk_size=_CHUNK):
                    fh.write(chunk)
                    bytes_so_far += len(chunk)
                    current_meta = _Sidecar(
                        etag=etag,
                        last_modified=last_modified,
                        bytes_written=bytes_so_far,
                    )
                    meta_path.write_text(current_meta.to_lines())
                    if progress:
                        progress(bytes_so_far, total)

        # Atomic rename
        part_path.replace(destination)
        meta_path.unlink(missing_ok=True)
        return DownloadResult(
            state=DownloadState.COMPLETE,
            destination=destination,
            downloaded_bytes=bytes_so_far,
            total_bytes=total,
        )

    except httpx.HTTPError as exc:
        return DownloadResult(
            state=DownloadState.FAILED,
            destination=None,
            downloaded_bytes=sidecar.bytes_written if sidecar else 0,
            total_bytes=None,
            error=f"network_error:{exc}",
        )


def _quarantine(part_path: Path, meta_path: Path) -> None:
    q = part_path.with_suffix(".part.quarantine")
    if part_path.exists():
        part_path.rename(q)
    meta_path.unlink(missing_ok=True)


def _parse_content_length(resp: httpx.Response) -> int | None:
    raw = resp.headers.get("content-length")
    if raw and raw.isdigit():
        return int(raw)
    return None


def _parse_range_total(content_range: str) -> int | None:
    # "bytes 100-199/1000"
    m = re.search(r"/(\d+)$", content_range)
    if m:
        return int(m.group(1))
    return None


def _content_range_matches(content_range: str, expected_start: int) -> bool:
    # "bytes 100-199/1000" — check that start matches
    m = re.match(r"bytes (\d+)-", content_range)
    if not m:
        return False
    return int(m.group(1)) == expected_start
