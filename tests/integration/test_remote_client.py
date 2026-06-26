"""D-005, D-006 — RemoteClient: retry/backoff and conditional cache."""
from __future__ import annotations

import httpx
import pytest

from infocon_librarian.remote.client import RemoteClient, RemoteFetchError

# ---------------------------------------------------------------------------
# Fake HTTPX transport helpers
# ---------------------------------------------------------------------------


class _SequentialTransport(httpx.BaseTransport):
    """Returns a fixed sequence of responses, then raises on extras."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._queue = list(responses)
        self.call_count = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        if not self._queue:
            raise AssertionError("No more responses queued")
        resp = self._queue.pop(0)
        resp.request = request
        # httpx requires stream to be read
        return resp


def _response(status: int, body: bytes = b"ok", **headers: str) -> httpx.Response:
    return httpx.Response(status_code=status, content=body, headers=headers)


# ---------------------------------------------------------------------------
# D-005: Bounded retries with backoff; typed error after exhaustion
# ---------------------------------------------------------------------------


def test_d005_transient_5xx_is_retried() -> None:
    transport = _SequentialTransport(
        [
            _response(500),
            _response(500),
            _response(200, b"content"),
        ]
    )
    client = RemoteClient(
        transport=transport,
        max_retries=3,
        retry_delay_base=0.0,  # no actual sleep in tests
    )
    result = client.fetch("http://example.com/listing.html")

    assert result.status_code == 200
    assert result.body == b"content"
    assert transport.call_count == 3


def test_d005_exhausted_retries_raises_fetch_error() -> None:
    transport = _SequentialTransport(
        [_response(503), _response(503), _response(503)]
    )
    client = RemoteClient(
        transport=transport,
        max_retries=3,
        retry_delay_base=0.0,
    )
    with pytest.raises(RemoteFetchError):
        client.fetch("http://example.com/listing.html")

    assert transport.call_count == 3


def test_d005_connection_error_is_retried() -> None:
    class _FailOnce(httpx.BaseTransport):
        def __init__(self) -> None:
            self.calls = 0

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            self.calls += 1
            if self.calls == 1:
                raise httpx.ConnectError("connection refused")
            return _response(200, b"recovered")

    transport = _FailOnce()
    client = RemoteClient(transport=transport, max_retries=2, retry_delay_base=0.0)
    result = client.fetch("http://example.com/")
    assert result.status_code == 200


def test_d005_connection_error_exhausted_raises() -> None:
    class _AlwaysFail(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

    client = RemoteClient(
        transport=_AlwaysFail(), max_retries=2, retry_delay_base=0.0
    )
    with pytest.raises(RemoteFetchError):
        client.fetch("http://example.com/")


def test_d005_404_not_retried() -> None:
    transport = _SequentialTransport([_response(404)])
    client = RemoteClient(transport=transport, max_retries=3, retry_delay_base=0.0)
    result = client.fetch("http://example.com/missing.html")

    assert result.status_code == 404
    assert transport.call_count == 1  # not retried


# ---------------------------------------------------------------------------
# D-006: Conditional cache hit — 304 response → from_cache=True
# ---------------------------------------------------------------------------


def test_d006_304_returns_from_cache_true() -> None:
    transport = _SequentialTransport([_response(304)])
    client = RemoteClient(transport=transport, retry_delay_base=0.0)

    result = client.fetch(
        "http://example.com/listing.html",
        etag='"abc123"',
        last_modified="Thu, 01 Jan 2024 00:00:00 GMT",
    )

    assert result.from_cache is True
    assert result.status_code == 304
    assert result.body == b""


def test_d006_304_preserves_cached_etag() -> None:
    transport = _SequentialTransport([_response(304)])
    client = RemoteClient(transport=transport, retry_delay_base=0.0)

    result = client.fetch(
        "http://example.com/listing.html",
        etag='"abc123"',
    )

    assert result.etag == '"abc123"'


def test_d006_200_response_is_not_from_cache() -> None:
    transport = _SequentialTransport(
        [_response(200, b"<html>listing</html>", ETag='"newetag"')]
    )
    client = RemoteClient(transport=transport, retry_delay_base=0.0)

    result = client.fetch("http://example.com/listing.html")

    assert result.from_cache is False
    assert result.status_code == 200
    assert result.etag == '"newetag"'


def test_d006_conditional_headers_sent() -> None:
    captured: list[httpx.Request] = []

    class _CapturingTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return _response(304)

    client = RemoteClient(transport=_CapturingTransport(), retry_delay_base=0.0)
    client.fetch(
        "http://example.com/listing.html",
        etag='"xyz"',
        last_modified="Mon, 01 Jan 2024 00:00:00 GMT",
    )

    assert len(captured) == 1
    req = captured[0]
    assert req.headers.get("if-none-match") == '"xyz"'
    assert "if-modified-since" in req.headers
