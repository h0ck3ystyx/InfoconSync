"""Concurrent HTTPS transfer runner — parallel file downloads + concurrency setting.

These exercise the real _download_item thread pool by driving it through an
httpx.MockTransport, so no socket ever leaves the process (architecture
invariant #7). The transport records how many file downloads overlap, which is
how we prove the pool is genuinely concurrent rather than serial.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from infocon_librarian.services.http_transfer_runner import (
    HTTP_CONCURRENCY_MAX,
    _download_item,
    _resolve_concurrency,
)
from infocon_librarian.storage.database import open_db
from infocon_librarian.storage.migrations import migrate
from infocon_librarian.storage.plan_repository import PlanRepository
from infocon_librarian.storage.repositories import ArchiveRootRepository
from infocon_librarian.storage.settings_repository import SettingsRepository

_COLLECTION_URL = "https://infocon.example.com/coll/testcol/"
_FILE_BODY = b"x" * 4096  # one 4 KiB body per file


class _RecordingTransport(httpx.BaseTransport):
    """Serves a synthetic listing + file bodies and tracks peak overlap."""

    def __init__(self, file_names: list[str], *, fail: set[str] | None = None) -> None:
        self._file_names = file_names
        self._fail = fail or set()
        self._lock = threading.Lock()
        self._in_flight = 0
        self.peak_in_flight = 0

    def _listing_html(self) -> str:
        rows = "\n".join(
            f'<tr><td><a href="{name}">{name}</a></td></tr>' for name in self._file_names
        )
        return f"<html><body><table>{rows}</table></body></html>"

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # Hidden-file HEAD probes (HEADER.md, ...) — report absent.
        if request.method == "HEAD":
            return httpx.Response(404)
        # The collection directory itself → the listing.
        if path.endswith("/"):
            return httpx.Response(200, text=self._listing_html())
        # A file.
        name = path.rsplit("/", 1)[-1]
        if name in self._fail:
            return httpx.Response(404)
        with self._lock:
            self._in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
        try:
            time.sleep(0.02)  # widen the overlap window so peak is observable
            return httpx.Response(200, content=_FILE_BODY)
        finally:
            with self._lock:
                self._in_flight -= 1


def _make_item(tmp_path: Path):
    conn = open_db(tmp_path / "lib.db")
    migrate(conn)
    root = ArchiveRootRepository(conn).upsert(str(tmp_path / "archive"), "fp")
    repo = PlanRepository(conn)
    plan = repo.create_plan(root.id)
    item = repo.add_item(
        plan.id,
        method="https",
        status="downloading",
        destination_relpath="defcon/testcol",
        collection_key="defcon/testcol",
        url=_COLLECTION_URL,
    )
    return conn, repo, plan, item


def test_concurrent_download_completes_and_overlaps(tmp_path: Path) -> None:
    names = [f"file{i}.bin" for i in range(8)]
    transport = _RecordingTransport(names)
    holder = SimpleNamespace(client=httpx.Client(transport=transport))
    factory = lambda: httpx.Client(transport=transport)  # noqa: E731

    archive_root = tmp_path / "archive"
    conn, repo, plan, item = _make_item(tmp_path)
    events: list[dict] = []
    events_lock = threading.Lock()

    def broadcast(ev: dict) -> None:
        with events_lock:
            events.append(ev)

    outcome = _download_item(
        item, archive_root, holder, broadcast, plan.id, repo,
        concurrency=4, client_factory=factory,
    )

    assert outcome["status"] == "complete"
    assert outcome["downloaded_bytes"] == len(names) * len(_FILE_BODY)

    # Every file landed under the archive root with the right bytes.
    for name in names:
        dest = archive_root / "defcon/testcol" / name
        assert dest.read_bytes() == _FILE_BODY

    # The pool genuinely overlapped — at least two files were in flight at once.
    assert transport.peak_in_flight >= 2

    # Item-level progress is cumulative and reaches the full byte count.
    progress = [e for e in events if e.get("type") == "progress"]
    assert progress, "expected progress events"
    assert max(e["downloaded_bytes"] for e in progress) == len(names) * len(_FILE_BODY)
    conn.close()


def test_never_exceeds_configured_concurrency(tmp_path: Path) -> None:
    names = [f"file{i}.bin" for i in range(12)]
    transport = _RecordingTransport(names)
    holder = SimpleNamespace(client=httpx.Client(transport=transport))
    factory = lambda: httpx.Client(transport=transport)  # noqa: E731

    conn, repo, plan, item = _make_item(tmp_path)
    outcome = _download_item(
        item, tmp_path / "archive", holder, lambda _ev: None, plan.id, repo,
        concurrency=2, client_factory=factory,
    )

    assert outcome["status"] == "complete"
    assert transport.peak_in_flight <= 2
    conn.close()


def test_failed_file_marks_item_failed(tmp_path: Path) -> None:
    names = [f"file{i}.bin" for i in range(4)]
    transport = _RecordingTransport(names, fail={"file2.bin"})
    holder = SimpleNamespace(client=httpx.Client(transport=transport))
    factory = lambda: httpx.Client(transport=transport)  # noqa: E731

    conn, repo, plan, item = _make_item(tmp_path)
    outcome = _download_item(
        item, tmp_path / "archive", holder, lambda _ev: None, plan.id, repo,
        concurrency=3, client_factory=factory,
    )

    assert outcome["status"] == "failed"
    assert outcome["error"] == "1_files_failed"
    conn.close()


@pytest.mark.parametrize(
    ("stored", "expected"),
    [(None, 4), ("1", 1), ("0", 1), ("-5", 1), ("6", 6), ("99", HTTP_CONCURRENCY_MAX)],
)
def test_resolve_concurrency_clamps(tmp_path: Path, stored: str | None, expected: int) -> None:
    conn = open_db(tmp_path / "lib.db")
    migrate(conn)
    if stored is not None:
        SettingsRepository(conn).set("http_concurrency", stored)
    assert _resolve_concurrency(conn) == expected
    conn.close()
