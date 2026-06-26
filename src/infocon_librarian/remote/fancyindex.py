"""Pure HTML parser for nginx fancyindex / Apache autoindex directory listings."""
from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from infocon_librarian.domain.models import RemoteEntry

_SIZE_RE = re.compile(r"^([\d.]+)\s*([KMGTP]?)$", re.IGNORECASE)
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})")


def parse_listing(html: str, base_url: str) -> list[RemoteEntry]:
    """Parse an HTML directory listing and return filtered RemoteEntry objects.

    Filters out:
    - Sort/query links (href starts with ?)
    - Fragment-only links (href starts with #)
    - Parent directory links (href is ../ or ..)
    - Off-host absolute URLs
    """
    parser = _ListingParser()
    parser.feed(html)

    base = urlparse(base_url)
    entries: list[RemoteEntry] = []

    for row in parser.rows:
        href = row.get("href")
        display = row.get("display", "")

        if not href:
            continue

        # Sort and fragment links
        if href.startswith("?") or href.startswith("#"):
            continue

        # Parent directory
        stripped = href.rstrip("/")
        if stripped == ".." or stripped == "":
            continue

        # Resolve to absolute URL
        canonical = urljoin(base_url, href)
        parsed = urlparse(canonical)

        # Off-host links
        if parsed.netloc and parsed.netloc != base.netloc:
            continue

        kind = "directory" if href.endswith("/") else "file"
        entry_id = hashlib.sha256(canonical.encode()).hexdigest()

        entries.append(
            RemoteEntry(
                id=entry_id,
                url=canonical,
                parent_url=base_url,
                kind=kind,
                display_name=(display or href).rstrip("/"),
                size_hint=_parse_size(row.get("cells", [])),
                modified_hint=_parse_modified(row.get("cells", [])),
            )
        )

    return entries


# ---------------------------------------------------------------------------
# Internal HTML parser
# ---------------------------------------------------------------------------


class _ListingParser(HTMLParser):
    """Extracts table rows from a directory listing page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []

        self._in_row = False
        self._row_href: str | None = None
        self._row_display: str | None = None
        self._row_cells: list[str] = []

        self._in_cell = False
        self._cell_parts: list[str] = []

        self._in_a = False
        self._a_parts: list[str] = []
        self._a_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)

        if tag == "tr":
            self._in_row = True
            self._row_href = None
            self._row_display = None
            self._row_cells = []

        elif tag in ("td", "th") and self._in_row:
            self._in_cell = True
            self._cell_parts = []

        elif tag == "a" and self._in_row:
            self._in_a = True
            self._a_parts = []
            self._a_href = attrs_dict.get("href") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr":
            if self._in_row and self._row_href is not None:
                self.rows.append(
                    {
                        "href": self._row_href,
                        "display": self._row_display,
                        "cells": list(self._row_cells),
                    }
                )
            self._in_row = False
            self._in_cell = False
            self._in_a = False

        elif tag in ("td", "th"):
            if self._in_cell:
                self._row_cells.append("".join(self._cell_parts).strip())
            self._in_cell = False

        elif tag == "a":
            if self._in_a and self._row_href is None:
                # Capture the first link per row only
                self._row_href = self._a_href
                self._row_display = "".join(self._a_parts).strip()
            self._in_a = False
            self._a_href = None

    def handle_data(self, data: str) -> None:
        if self._in_a:
            self._a_parts.append(data)
        if self._in_cell:
            self._cell_parts.append(data)


# ---------------------------------------------------------------------------
# Hint parsers
# ---------------------------------------------------------------------------


def _parse_size(cells: list[str]) -> int | None:
    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
    for cell in cells:
        text = cell.strip().lstrip("\xa0").strip()
        if not text or text == "-":
            continue
        m = _SIZE_RE.match(text)
        if m:
            num = float(m.group(1))
            suffix = m.group(2).upper()
            return int(num * multipliers.get(suffix, 1))
    return None


def _parse_modified(cells: list[str]) -> str | None:
    for cell in cells:
        m = _DATE_RE.search(cell)
        if m:
            return m.group(1)
    return None
