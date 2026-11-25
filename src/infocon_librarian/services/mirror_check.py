"""Discover and compare a public directory mirror without downloading its media."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from threading import Thread
from typing import Any

import httpx

from infocon_librarian.remote.mirror import (
    MirrorSite,
    directory_entries,
    mirror_client,
    mirror_path,
    name_key,
    read_limited,
)
from infocon_librarian.service_runtime import ServiceInterrupted, checkpoint, start_worker
from infocon_librarian.storage.database import open_db
from infocon_librarian.storage.repositories import CheckRepository
from infocon_librarian.storage.settings_repository import SettingsRepository
from infocon_librarian.torrent.adapter import TorrentAdapter

log = logging.getLogger(__name__)
_MAX_DEPTH = 32
_MAX_ENTRIES = 100_000


def exact_size(client: httpx.Client, url: str) -> int | None:
    try:
        response = client.head(url, headers={"Accept-Encoding": "identity"})
        length = response.headers.get("content-length", "")
        if (
            response.status_code == 200 and length.isdecimal()
            and response.headers.get("content-encoding", "identity") == "identity"
        ):
            return int(length)
    except httpx.HTTPError:
        pass
    return None


def _inventory(
    client: httpx.Client, name: str, url: str, is_dir: bool, budget: list[int],
) -> dict[str, Any]:
    files: list[tuple[str, str]] = []
    dirs: list[str] = []
    pending = [(name, url, is_dir, 0)]
    visited: set[str] = set()
    while pending:
        checkpoint()
        path, href, directory, depth = pending.pop()
        budget[0] -= 1
        if budget[0] < 0 or depth > _MAX_DEPTH:
            raise ValueError(f"Directory scan limit reached at {path}")
        if directory:
            if href in visited:
                raise ValueError(f"Directory cycle at {path}")
            visited.add(href)
            dirs.append(path)
            for child, dest, child_is_dir in directory_entries(client, href):
                pending.append((f"{path}/{child}", dest, child_is_dir, depth + 1))
        else:
            files.append((path, href))
    return {"files": files, "dirs": dirs}


def _status(
    root: Path, name: str, manifest: dict[str, Any], client: httpx.Client,
    accept_larger: bool,
) -> str:
    target = mirror_path(root, name)
    if not target.exists():
        return "new"
    for path in manifest["dirs"]:
        if not mirror_path(root, path).is_dir():
            return "changed_manifest"
    unknown = False
    legacy = False
    for path, url in manifest["files"]:
        checkpoint()
        local = mirror_path(root, path)
        if not local.is_file():
            return "changed_manifest"
        size = exact_size(client, url)
        if size is None:
            unknown = True
        elif local.stat().st_size != size:
            if local.stat().st_size > size and accept_larger:
                legacy = True
            else:
                return "changed_manifest"
    if unknown:
        return "unknown"
    if legacy:
        return "has_older_version"
    return "present_unverified"


def _conference_number(name: str) -> str | None:
    match = re.search(r"\bdef[\s_-]*con[\s_-]*(\d+)\b", name, re.IGNORECASE)
    return match[1] if match else None


def _match_torrents(
    results: list[dict[str, Any]], client: httpx.Client, adapter: TorrentAdapter,
) -> None:
    torrent_index = next(
        (row for row in results if row["key"] == "DEF CON Torrents" and row.get("_mirror")),
        None,
    )
    if torrent_index is None:
        return
    torrents = torrent_index["_mirror"]["files"]
    for row in results:
        manifest = row.get("_mirror")
        conference = _conference_number(row["key"])
        if not manifest or not conference or not manifest["dirs"] or not manifest["files"]:
            continue
        paths = dict(manifest["files"])
        # Torrents cannot reproduce empty directories.
        file_parents = {str(parent) for path in paths for parent in PurePosixPath(path).parents}
        if not set(manifest["dirs"]).issubset(file_parents):
            continue
        candidates = [
            url for path, url in torrents if path.lower().endswith(".torrent")
            and _conference_number(path.rsplit("/", 1)[-1]) == conference
        ]
        for url in sorted(candidates, reverse=True)[:5]:
            checkpoint()
            try:
                raw = read_limited(client, url)
                files = adapter.inspect(raw).files
                sizes = {file.relative_path: file.size for file in files}
                if len(sizes) != len(files) or sizes.keys() != paths.keys():
                    continue
                if any(exact_size(client, paths[path]) != size for path, size in sizes.items()):
                    continue
                manifest["size_bytes"] = sum(sizes.values())
                manifest["torrent_digest"] = hashlib.sha256(raw).hexdigest()
                manifest["torrent_url"] = url
                row["evidence"].append({"kind": "remote_listing", "payload": {"torrent_url": url}})
                break
            except ServiceInterrupted:
                raise
            except Exception as exc:
                log.info("Could not match DEF CON torrent %s: %s", url, exc)


def run_mirror_check(
    *, check_id: str, db_path: Path, archive_root: Path, site: MirrorSite,
    broadcast: Callable[[dict[str, Any]], None], adapter: TorrentAdapter | None = None,
) -> None:
    conn = open_db(db_path)
    repo = CheckRepository(conn)
    try:
        root = archive_root
        if site.relative_folder:
            root = mirror_path(archive_root, site.relative_folder)
        accept_larger = bool(SettingsRepository(conn).get_int("accept_larger_local_files"))
        results = []
        with mirror_client(site.base_url) as client:
            entries = directory_entries(client, site.base_url)
            budget = [_MAX_ENTRIES]
            for index, (name, url, is_dir) in enumerate(entries, 1):
                checkpoint()
                broadcast({
                    "type": "check_progress", "check_id": check_id, "site_id": site.id,
                    "phase": "fetch", "section": name, "current": index, "total": len(entries),
                })
                row: dict[str, Any] = {
                    "section": site.name, "key": name, "display_name": name,
                    "id": hashlib.sha256(url.encode()).hexdigest(), "evidence": [],
                }
                try:
                    manifest = _inventory(client, name, url, is_dir, budget)
                    row["status"] = _status(root, name, manifest, client, accept_larger)
                    row["_mirror"] = {**manifest, "url": url}
                    row["selectable"] = True
                except (httpx.HTTPError, ValueError, OSError) as exc:
                    row.update(status="unknown", error=str(exc), selectable=False)
                results.append(row)

            if site.id == "defcon" and adapter is not None:
                _match_torrents(results, client, adapter)
        upstream = {name_key(entry[0]) for entry in entries}
        if root.is_dir():
            for path in sorted(root.iterdir()):
                if path.name.startswith(".") or name_key(path.name) in upstream:
                    continue
                results.append({
                    "section": site.name, "key": path.name, "display_name": path.name,
                    "status": "local_only", "selectable": False,
                })
        checkpoint()
        repo.complete(check_id, json.dumps(results))
        broadcast({
            "type": "check_complete", "check_id": check_id, "site_id": site.id,
            "count": len(results),
        })
    except Exception as exc:
        log.warning("Mirror check %s failed: %s", check_id, exc)
        repo.fail(check_id, str(exc))
        broadcast({
            "type": "check_failed", "check_id": check_id, "site_id": site.id, "error": str(exc),
        })
    finally:
        conn.close()


def start_mirror_check(**kwargs: Any) -> None:
    start_worker(Thread(target=run_mirror_check, kwargs=kwargs, daemon=True))
