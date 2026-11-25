"""Configuration and bounded, same-site access for public directory mirrors."""
from __future__ import annotations

import ipaddress
import re
import socket
import sqlite3
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from infocon_librarian.domain.paths import is_valid_path_component
from infocon_librarian.remote.fancyindex import parse_listing

STAGING = ".mirror-partials"
_LISTING_LIMIT = 8 * 1024 * 1024


@dataclass(frozen=True)
class MirrorSite:
    id: str
    name: str
    base_url: str
    folder: str
    local_path: str | None = None
    version: int = 1

    @property
    def relative_folder(self) -> str:
        return "" if self.local_path else self.folder

    def destination(self, default_root: Path) -> Path:
        return Path(self.local_path) if self.local_path else default_root / self.folder


def managed_sections(
    db: sqlite3.Connection, infocon_root: Path, default_root: Path | None = None,
) -> set[str]:
    """Top-level directories owned by another site under the InfoCon root."""
    return {
        name_key(destination.name)
        for site in list_sites(db)
        if site.id != "infocon"
        and (destination := site.destination(default_root or infocon_root)).parent == infocon_root
    }


def list_sites(db: sqlite3.Connection) -> list[MirrorSite]:
    rows = db.execute("SELECT * FROM mirror_sites ORDER BY rowid").fetchall()
    return [MirrorSite(**dict(row)) for row in rows]


def get_site(db: sqlite3.Connection, site_id: object) -> MirrorSite | None:
    if not isinstance(site_id, str):
        return None
    row = db.execute("SELECT * FROM mirror_sites WHERE id = ?", (site_id,)).fetchone()
    return MirrorSite(**dict(row)) if row else None


def name_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def valid_component(name: str) -> bool:
    return (
        is_valid_path_component(name)
        and "/" not in name and "\\" not in name
        and not name.endswith((".", " "))
        and is_valid_path_component(name.partition(".")[0] or name)
        and name_key(name) != name_key(STAGING)
    )


def public_base_url(value: str) -> str:
    try:
        parts = urlsplit(value.strip())
        if (
            parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.port not in (None, 443)
            or parts.query or parts.fragment
            or any(ord(char) < 32 or char == "\\" for char in value)
        ):
            raise ValueError
        url = httpx.URL(value.strip())
        path = url.path.rstrip("/")
        if path and any(not valid_component(part) for part in path[1:].split("/")):
            raise ValueError
        return str(url.copy_with(raw_path=url.raw_path.rstrip(b"/") + b"/"))
    except (ValueError, httpx.InvalidURL):
        raise ValueError("Enter a public HTTPS directory URL on port 443.") from None


def _public_addresses(host: str) -> list[str]:
    addresses = []
    try:
        for _, _, _, _, sockaddr in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
            address = ipaddress.ip_address(sockaddr[0])
            if (
                address.is_global and not address.is_multicast
                and not address.is_reserved and "%" not in str(address)
            ):
                addresses.append(str(address))
    except OSError as exc:
        raise httpx.ConnectError(f"Could not resolve {host}") from exc
    if not addresses:
        raise httpx.ConnectError(f"{host} has no public address")
    return addresses


class _PublicTransport(httpx.BaseTransport):
    """Pin each connection to a checked address while keeping hostname TLS validation."""

    def __init__(self, base_url: str) -> None:
        self.base = httpx.URL(base_url)
        self.transport = httpx.HTTPTransport(http2=True, trust_env=False)
        self.addresses = _public_addresses(self.base.host)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if (
            url.scheme != "https" or url.netloc != self.base.netloc
            or url.userinfo or url.query or url.fragment
            or not url.raw_path.startswith(self.base.raw_path)
        ):
            raise httpx.RequestError("URL is outside the mirror site", request=request)
        for address in self.addresses:
            pinned = httpx.Request(
                request.method,
                url.copy_with(host=address),
                headers=request.headers,
                stream=request.stream,
                extensions={**request.extensions, "sni_hostname": self.base.host},
            )
            try:
                return self.transport.handle_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if address == self.addresses[-1]:
                    raise
        raise AssertionError("No address checked")

    def close(self) -> None:
        self.transport.close()


def mirror_client(base_url: str) -> httpx.Client:
    return httpx.Client(
        transport=_PublicTransport(base_url), timeout=30, trust_env=False,
        headers={"User-Agent": "infocon-librarian (directory mirror)"},
    )


def read_limited(client: httpx.Client, url: str) -> bytes:
    with client.stream("GET", url) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError(f"Expected a full remote response, got HTTP {response.status_code}")
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > _LISTING_LIMIT:
                raise ValueError("Remote listing or metadata is too large")
        return bytes(body)


class _IndexMarkup(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fancy = False
        self.pre = False
        self._table_depth = 0
        self._in_pre = False
        self.table_links: list[str] = []
        self.pre_links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "table" and (self._table_depth or values.get("id") == "list"):
            self._table_depth += 1
        elif tag == "pre":
            self._in_pre = True
        elif tag == "a" and (href := values.get("href")):
            if self._table_depth:
                self.table_links.append(href)
            elif self._in_pre:
                self.pre_links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._table_depth:
            self._table_depth -= 1
            if not self._table_depth:
                self.fancy = True
        elif tag == "pre" and self._in_pre:
            self._in_pre = False
            self.pre = True


def directory_entries(client: httpx.Client, url: str) -> list[tuple[str, str, bool]]:
    """Read immediate children from a recognized directory index."""
    html = read_limited(client, url).decode("utf-8", errors="replace")
    markup = _IndexMarkup()
    markup.feed(html)
    apache = re.search(r"<(?:title|h1)>\s*Index of\s", html, re.IGNORECASE)
    if markup.fancy:
        links = markup.table_links
    elif apache and markup.pre:
        links = markup.pre_links
    elif apache and "</table>" in html.lower():
        links = [entry.url for entry in parse_listing(html, url)]
    else:
        raise ValueError("The server did not return a supported directory listing")

    root = httpx.URL(url)
    entries: dict[str, tuple[str, str, bool]] = {}
    for href in links:
        if href.startswith(("?", "#")):
            continue
        try:
            child = root.join(href)
        except (ValueError, httpx.InvalidURL):
            continue
        if (
            child.scheme != root.scheme or child.netloc != root.netloc
            or child.query or child.fragment or child.userinfo
            or not child.raw_path.startswith(root.raw_path)
        ):
            continue
        tail = child.raw_path[len(root.raw_path):]
        is_dir = tail.endswith(b"/")
        segment = tail.removesuffix(b"/")
        if not segment or b"/" in segment:
            continue
        name = unquote(segment.decode("ascii"), errors="strict")
        if not valid_component(name):
            raise ValueError(f"Unsupported filename {name!r}")
        entry = (name, str(child), is_dir)
        previous = entries.setdefault(name_key(name), entry)
        if previous != entry:
            raise ValueError(f"Conflicting remote filenames {previous[0]!r} and {name!r}")
    return list(entries.values())


def mirror_path(root: Path, relative: str) -> Path:
    """Refuse symbolic links and names that alias another entry on some volumes."""
    path = root
    for name in relative.split("/"):
        if not valid_component(name):
            raise ValueError(f"Invalid mirror path {relative!r}")
        child = path / name
        if child.is_symlink():
            raise ValueError(f"Symbolic link in mirror path {relative!r}")
        # If the filesystem resolves an alias, require the exact stored name.
        if child.exists() and not any(entry.name == name for entry in path.iterdir()):
            raise ValueError(f"Filename collision in mirror path {relative!r}")
        path = child
    return path
