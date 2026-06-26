"""HTTP client for remote InfoCon listings — timeouts, retries, conditional GET."""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from infocon_librarian.domain.errors import LibrarianError

_USER_AGENT = "infocon-librarian/0.1 (archive steward)"
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_RETRIES = 3
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class RemoteFetchError(LibrarianError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class FetchResult:
    status_code: int
    body: bytes
    etag: str | None
    last_modified: str | None
    from_cache: bool   # True when server returned 304 Not Modified


class RemoteClient:
    """Thin httpx wrapper with retry/backoff and conditional-GET support."""

    def __init__(
        self,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        transport: httpx.BaseTransport | None = None,
        retry_delay_base: float = 1.0,
    ) -> None:
        self._max_retries = max_retries
        self._retry_delay_base = retry_delay_base
        self._http = httpx.Client(
            headers={"User-Agent": _USER_AGENT},
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
        )

    def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        """Fetch *url* with conditional GET and bounded retry/backoff.

        Raises RemoteFetchError after exhausting retries.
        """
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        last_err: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = self._http.get(url, headers=headers)

                if resp.status_code == 304:
                    return FetchResult(
                        status_code=304,
                        body=b"",
                        etag=etag,
                        last_modified=last_modified,
                        from_cache=True,
                    )

                if resp.status_code not in _RETRY_STATUSES:
                    return FetchResult(
                        status_code=resp.status_code,
                        body=resp.content,
                        etag=resp.headers.get("ETag"),
                        last_modified=resp.headers.get("Last-Modified"),
                        from_cache=False,
                    )

                # Retryable status code
                last_err = Exception(f"HTTP {resp.status_code}")
                if attempt < self._max_retries - 1:
                    time.sleep(self._retry_delay_base * (2**attempt))

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_err = exc
                if attempt < self._max_retries - 1:
                    time.sleep(self._retry_delay_base * (2**attempt))

        raise RemoteFetchError(
            f"Failed to fetch {url!r} after {self._max_retries} attempts: {last_err}"
        ) from last_err

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> RemoteClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
