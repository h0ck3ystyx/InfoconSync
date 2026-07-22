"""P-009 through P-012 — HttpDownloader unit tests (no real network)."""
from __future__ import annotations

from pathlib import Path

import httpx

from infocon_librarian.transfer.http_downloader import DownloadState, download

# ---------------------------------------------------------------------------
# Helpers: fake HTTPX transport
# ---------------------------------------------------------------------------


class _FakeTransport(httpx.BaseTransport):
    """Programmable transport for testing HttpDownloader without real HTTP."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self._index = 0
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._index >= len(self._responses):
            raise httpx.ConnectError("No more fake responses")
        resp = self._responses[self._index]
        self._index += 1
        return resp


def _fake_client(responses: list[httpx.Response]) -> tuple[httpx.Client, _FakeTransport]:
    transport = _FakeTransport(responses)
    client = httpx.Client(transport=transport)
    return client, transport


def _resp200(body: bytes, content_type: str = "application/octet-stream") -> httpx.Response:
    return httpx.Response(
        200,
        content=body,
        headers={"content-length": str(len(body)), "content-type": content_type},
    )


def _resp206(body: bytes, start: int, total: int) -> httpx.Response:
    end = start + len(body) - 1
    return httpx.Response(
        206,
        content=body,
        headers={
            "content-length": str(len(body)),
            "content-range": f"bytes {start}-{end}/{total}",
        },
    )


# ---------------------------------------------------------------------------
# P-009: HTTP starts fresh — writes .part, emits progress, atomically renames
# ---------------------------------------------------------------------------


def test_p009_fresh_download_complete(tmp_path: Path) -> None:
    content = b"hello world content"
    client, _ = _fake_client([_resp200(content)])
    dest = tmp_path / "dc32" / "slides.pdf"

    result = download("https://example.com/slides.pdf", dest, http_client=client)

    assert result.state == DownloadState.COMPLETE
    assert result.destination == dest
    assert dest.exists()
    assert dest.read_bytes() == content
    # .part file should be cleaned up
    assert not dest.with_suffix(".pdf.part").exists()


def test_p009_progress_callback_called(tmp_path: Path) -> None:
    content = b"x" * (128 * 1024)
    client, _ = _fake_client([_resp200(content)])
    dest = tmp_path / "file.bin"
    calls: list[tuple[int, int | None]] = []

    def _record(b: int, t: int | None) -> None:
        calls.append((b, t))

    download("https://example.com/file.bin", dest, http_client=client, progress=_record)

    assert len(calls) > 0
    # Last call should report total bytes
    assert calls[-1][0] == len(content)


def test_p009_part_file_not_left_on_success(tmp_path: Path) -> None:
    client, _ = _fake_client([_resp200(b"data")])
    dest = tmp_path / "file.bin"
    download("https://example.com/file.bin", dest, http_client=client)

    part = Path(str(dest) + ".part")
    assert not part.exists()


def test_p009_creates_parent_directories(tmp_path: Path) -> None:
    client, _ = _fake_client([_resp200(b"data")])
    dest = tmp_path / "deep" / "nested" / "file.pdf"

    download("https://example.com/file.pdf", dest, http_client=client)

    assert dest.exists()


def test_p009_result_is_downloaded_unverified(tmp_path: Path) -> None:
    """The downloader produces COMPLETE state; verification is a separate step."""
    client, _ = _fake_client([_resp200(b"data")])
    dest = tmp_path / "file.bin"
    result = download("https://example.com/file.bin", dest, http_client=client)

    # COMPLETE means transfer done — caller must verify separately
    assert result.state == DownloadState.COMPLETE
    assert result.error is None


# ---------------------------------------------------------------------------
# P-010: Valid HTTP range resume — sends Range, receives 206, appends correctly
# ---------------------------------------------------------------------------


def test_p010_valid_range_resume(tmp_path: Path) -> None:
    dest = tmp_path / "file.bin"
    part = Path(str(dest) + ".part")
    meta = Path(str(part) + ".meta")

    # Simulate a partial download
    first_chunk = b"FIRST_CHUNK"
    part.write_bytes(first_chunk)
    meta.write_text(
        f"etag=\"abc123\"\nlast_modified=\nbytes_written={len(first_chunk)}\n"
    )

    second_chunk = b"_SECOND_CHUNK"
    total = len(first_chunk) + len(second_chunk)
    client, transport = _fake_client([
        _resp206(second_chunk, start=len(first_chunk), total=total)
    ])

    result = download("https://example.com/file.bin", dest, http_client=client)

    assert result.state == DownloadState.COMPLETE
    assert dest.read_bytes() == first_chunk + second_chunk


def test_p010_range_header_sent(tmp_path: Path) -> None:
    dest = tmp_path / "file.bin"
    part = Path(str(dest) + ".part")
    meta = Path(str(part) + ".meta")

    existing = b"existing_bytes"
    part.write_bytes(existing)
    meta.write_text(f"etag=\nlast_modified=\nbytes_written={len(existing)}\n")

    second = b"_more_data"
    total = len(existing) + len(second)
    client, transport = _fake_client([_resp206(second, len(existing), total)])

    download("https://example.com/file.bin", dest, http_client=client)

    assert transport.requests[0].headers.get("range") == f"bytes={len(existing)}-"


def test_p010_etag_sent_in_if_range(tmp_path: Path) -> None:
    dest = tmp_path / "file.bin"
    part = Path(str(dest) + ".part")
    meta = Path(str(part) + ".meta")

    part.write_bytes(b"partial")
    meta.write_text("etag=\"myetag\"\nlast_modified=\nbytes_written=7\n")

    client, transport = _fake_client([_resp206(b"_rest", 7, 12)])
    download("https://example.com/file.bin", dest, http_client=client)

    assert transport.requests[0].headers.get("if-range") == '"myetag"'


# ---------------------------------------------------------------------------
# P-011: Server returns 200 to a range request (no Range support) → the 200 is
# the authoritative full file, so discard the partial and re-download fresh.
# ---------------------------------------------------------------------------


def test_p011_200_to_range_request_redownloads_fresh(tmp_path: Path) -> None:
    dest = tmp_path / "file.bin"
    part = Path(str(dest) + ".part")
    meta = Path(str(part) + ".meta")

    part.write_bytes(b"partial_data")
    meta.write_text("etag=\nlast_modified=\nbytes_written=12\n")

    # Server ignores Range, returns the full file with 200.
    full_content = b"FULL_CONTENT_FROM_SERVER"
    client, _ = _fake_client([_resp200(full_content)])

    result = download("https://example.com/file.bin", dest, http_client=client)

    # Completes with the full body — the stale partial is overwritten, not appended.
    assert result.state == DownloadState.COMPLETE
    assert dest.read_bytes() == full_content
    assert not part.exists()


def test_p011_no_quarantine_on_ignored_range(tmp_path: Path) -> None:
    dest = tmp_path / "file.bin"
    part = Path(str(dest) + ".part")
    meta = Path(str(part) + ".meta")

    part.write_bytes(b"partial")
    meta.write_text("etag=\nlast_modified=\nbytes_written=7\n")

    client, _ = _fake_client([_resp200(b"the full clean body")])
    download("https://example.com/file.bin", dest, http_client=client)

    # No quarantine sidecar, and the resume metadata is cleaned up.
    assert not Path(str(part) + ".quarantine").exists()
    assert not meta.exists()


# ---------------------------------------------------------------------------
# P-012: Remote metadata changes before resume → do not resume stale partial
# ---------------------------------------------------------------------------


def test_p012_content_range_mismatch_quarantined(tmp_path: Path) -> None:
    """If server 206 content-range doesn't match what we asked for, quarantine."""
    dest = tmp_path / "file.bin"
    part = Path(str(dest) + ".part")
    meta = Path(str(part) + ".meta")

    existing = 100
    part.write_bytes(b"x" * existing)
    meta.write_text(f"etag=\nlast_modified=\nbytes_written={existing}\n")

    # Server returns 206 but with wrong start (0 instead of 100)
    wrong_range_resp = httpx.Response(
        206,
        content=b"data",
        headers={
            "content-length": "4",
            "content-range": "bytes 0-3/200",   # starts at 0, not 100
        },
    )
    client, _ = _fake_client([wrong_range_resp])

    result = download("https://example.com/file.bin", dest, http_client=client)

    assert result.state == DownloadState.QUARANTINED
    assert "content_range_mismatch" in result.error


def test_p012_no_sidecar_fresh_download(tmp_path: Path) -> None:
    """Without a sidecar, no Range is sent; download starts fresh."""
    dest = tmp_path / "file.bin"
    content = b"full fresh content"
    client, transport = _fake_client([_resp200(content)])

    result = download("https://example.com/file.bin", dest, http_client=client)

    assert result.state == DownloadState.COMPLETE
    assert "range" not in transport.requests[0].headers
    assert dest.read_bytes() == content


def test_p012_network_error_returns_failed(tmp_path: Path) -> None:
    dest = tmp_path / "file.bin"

    class _ErrorTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

    client = httpx.Client(transport=_ErrorTransport())
    result = download(
        "https://example.com/file.bin", dest, http_client=client, retry_delay_base=0.0
    )

    assert result.state == DownloadState.FAILED
    assert result.error is not None
    assert not dest.exists()


# ---------------------------------------------------------------------------
# Retry / backoff — transient failures are retried, permanent ones are not
# ---------------------------------------------------------------------------


class _ScriptedTransport(httpx.BaseTransport):
    """Raises/returns from a script, one entry per request."""

    def __init__(self, script: list[object]) -> None:
        self._script = list(script)
        self._i = 0
        self.calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        item = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, httpx.Response)
        return item


def test_retry_recovers_after_transient_network_error(tmp_path: Path) -> None:
    dest = tmp_path / "file.bin"
    content = b"recovered content"
    transport = _ScriptedTransport([
        httpx.ConnectError("reset"),
        httpx.ConnectError("reset"),
        _resp200(content),
    ])
    client = httpx.Client(transport=transport)

    result = download(
        "https://example.com/file.bin", dest, http_client=client,
        max_retries=3, retry_delay_base=0.0,
    )

    assert result.state == DownloadState.COMPLETE
    assert dest.read_bytes() == content
    assert transport.calls == 3  # two failures + one success


def test_retry_gives_up_after_max_retries(tmp_path: Path) -> None:
    dest = tmp_path / "file.bin"
    transport = _ScriptedTransport([httpx.ConnectError("reset")])
    client = httpx.Client(transport=transport)

    result = download(
        "https://example.com/file.bin", dest, http_client=client,
        max_retries=3, retry_delay_base=0.0,
    )

    assert result.state == DownloadState.FAILED
    assert transport.calls == 3  # exactly max_retries attempts


def test_permanent_http_error_is_not_retried(tmp_path: Path) -> None:
    dest = tmp_path / "file.bin"
    transport = _ScriptedTransport([httpx.Response(404, content=b"nope")])
    client = httpx.Client(transport=transport)

    result = download(
        "https://example.com/file.bin", dest, http_client=client,
        max_retries=3, retry_delay_base=0.0,
    )

    assert result.state == DownloadState.FAILED
    assert result.error == "http_404"
    assert transport.calls == 1  # 404 is permanent — no retry


def test_transient_5xx_is_retried(tmp_path: Path) -> None:
    dest = tmp_path / "file.bin"
    content = b"ok now"
    transport = _ScriptedTransport([
        httpx.Response(503, content=b"busy"),
        _resp200(content),
    ])
    client = httpx.Client(transport=transport)

    result = download(
        "https://example.com/file.bin", dest, http_client=client,
        max_retries=3, retry_delay_base=0.0,
    )

    assert result.state == DownloadState.COMPLETE
    assert transport.calls == 2


def test_resume_truncates_untracked_tail_before_appending(tmp_path: Path) -> None:
    """A .part longer than its sidecar records (crash between chunk write and
    meta write) must be truncated to the recorded length, not appended to."""
    dest = tmp_path / "file.bin"
    part = Path(str(dest) + ".part")
    meta = Path(str(part) + ".meta")

    # Sidecar records 5 bytes, but the .part has an extra un-recorded chunk.
    part.write_bytes(b"AAAAA" + b"GARBAGE")
    meta.write_text("etag=\nlast_modified=\nbytes_written=5\n")

    tail = b"BBBBB"
    client, transport = _fake_client([_resp206(tail, start=5, total=10)])

    result = download("https://example.com/file.bin", dest, http_client=client)

    assert result.state == DownloadState.COMPLETE
    assert dest.read_bytes() == b"AAAAA" + tail  # garbage tail dropped
