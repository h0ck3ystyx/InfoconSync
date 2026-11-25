"""Read-only preview of the file operations a transfer item may perform."""
from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import httpx

from infocon_librarian.domain.errors import InvalidTorrent, PathEscapesRoot
from infocon_librarian.domain.paths import safe_archive_path
from infocon_librarian.remote.client import new_http_client
from infocon_librarian.remote.mirror import MirrorSite, mirror_client, mirror_path, read_limited
from infocon_librarian.service_runtime import checkpoint
from infocon_librarian.services.http_transfer_runner import (
    _crawl_files,
    _is_already_complete,
    _remote_file_size,
    mirror_download_paths,
)
from infocon_librarian.services.torrent_transfer_runner import _fetch_torrent_bytes
from infocon_librarian.storage.plan_repository import PlanItemRecord
from infocon_librarian.torrent.adapter import TorrentAdapter

PAGE_SIZE = 25


def preview_item(
    item: PlanItemRecord,
    archive: Path,
    site: MirrorSite,
    *,
    adapter: TorrentAdapter | None,
    offset: int,
    query: str,
    accept_larger: bool,
    force_https: bool,
) -> dict[str, Any]:
    checkpoint()
    manifest = json.loads(item.mirror_json) if item.mirror_json else None
    method = "https" if force_https else item.method
    url = item.url
    prefix = site.relative_folder if manifest else item.destination_relpath
    def client_factory() -> httpx.Client:
        return mirror_client(site.base_url) if manifest else new_http_client(timeout=10)

    if method == "https" and item.method == "torrent":
        url = manifest["url"] if manifest else f"{site.base_url}{item.destination_relpath}/"
    if not url:
        raise ValueError("The plan item has no source URL.")

    files: list[tuple[str, str | None, int | None]] = []
    empty_dirs: list[str] = []
    if method == "torrent":
        if adapter is None:
            raise ValueError("The torrent engine is required to preview this item's file list.")
        if manifest:
            with client_factory() as client:
                raw = read_limited(client, url)
            if hashlib.sha256(raw).hexdigest() != manifest["torrent_digest"]:
                raise ValueError("Torrent metadata changed. Check Remote and create a new plan.")
        else:
            if urlsplit(url).netloc != urlsplit(site.base_url).netloc:
                raise ValueError("The torrent URL is outside the mirror site.")
            raw = _fetch_torrent_bytes(url)
            prefix = (item.collection_key or item.destination_relpath).partition("/")[0]
        files = [(entry.relative_path, None, entry.size) for entry in adapter.inspect(raw).files]
        if manifest and {path for path, _, _ in files} != {path for path, _ in manifest["files"]}:
            raise InvalidTorrent("The torrent no longer matches the planned files.")
    elif manifest:
        files = [(path, href, None) for path, href in manifest["files"]]
        # A directory with no children must be created explicitly.
        parents = {str(PurePosixPath(path).parent) for path, _, _ in files}
        parents.update(str(PurePosixPath(path).parent) for path in manifest["dirs"])
        empty_dirs = [path for path in manifest["dirs"] if path not in parents]
    else:
        with client_factory() as client:
            listing = _crawl_files(url, url, client, strict=True)
        if not listing:
            raise ValueError("No files found in the remote directory listing.")
        files = [(path, href, None) for path, href in listing]

    entries = [(path, href, size, False) for path, href, size in files]
    entries.extend((path, None, None, True) for path in empty_dirs)
    entries = [
        entry for entry in entries
        if query.casefold() in (PurePosixPath(prefix) / entry[0]).as_posix().casefold()
    ]
    entries.sort(key=lambda entry: entry[0].casefold())

    rows = []
    with ExitStack() as stack:
        size_client = None
        for relative, href, remote_size, is_dir in entries[offset:offset + PAGE_SIZE]:
            checkpoint()
            display = (PurePosixPath(prefix) / relative).as_posix()
            row: dict[str, Any] = {"path": display, "kind": "folder" if is_dir else "file"}
            try:
                partial = None
                if manifest:
                    safe_root = mirror_path(archive, prefix) if prefix else archive
                    if href is not None:
                        target, partial = mirror_download_paths(safe_root, relative, href)
                    else:
                        target = mirror_path(safe_root, relative)
                else:
                    target = safe_archive_path(archive, [prefix, relative]).absolute
                    if method == "https":
                        partial = target.with_suffix(target.suffix + ".part")
                wrong_parent = any(
                    parent.exists() and not parent.is_dir() for parent in target.parents
                    if parent.is_relative_to(archive)
                )
                wrong_type = target.exists() and (
                    not target.is_dir() if is_dir else not target.is_file()
                )
                if wrong_parent or wrong_type:
                    action, reason = "conflict", "An existing path has a different type."
                elif is_dir:
                    action = "keep" if target.is_dir() else "add"
                    reason = ""
                elif partial and partial.exists():
                    action, reason = "partial", "A partial download already exists."
                elif not target.exists():
                    action, reason = "add", ""
                elif method == "torrent":
                    if target.stat().st_size == remote_size:
                        action = "verify"
                        reason = "The torrent will check the existing file's pieces."
                    else:
                        action, reason = "replace", "The existing size differs from the torrent."
                else:
                    assert href is not None
                    if size_client is None:
                        size_client = stack.enter_context(client_factory())
                        size_client.timeout = httpx.Timeout(3)
                    remote_size = _remote_file_size(size_client, href)
                    if _is_already_complete(
                        target, remote_size, part_path=partial, accept_larger=accept_larger,
                    ):
                        action, reason = "keep", "The local size meets the transfer's size policy."
                    else:
                        action = "replace"
                        reason = (
                            "The source did not report an exact size." if remote_size is None
                            else "The existing size differs from the source."
                        )
                row.update(
                    action=action, reason=reason, remote_bytes=remote_size,
                    local_bytes=target.stat().st_size if target.is_file() else None,
                )
            except (ValueError, OSError, PathEscapesRoot) as exc:
                row.update(action="conflict", reason=str(exc))
            rows.append(row)
    return {
        "rows": rows, "offset": offset, "page_size": PAGE_SIZE, "total": len(entries),
        "file_count": len(files), "method": method,
    }
