"""D-001 through D-004 — fancyindex HTML parser."""
from __future__ import annotations

from pathlib import Path

from infocon_librarian.remote.fancyindex import parse_listing

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "listings"
_BASE = "https://infocon.example.com/defcon/"


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# D-001: Sort links and parent row are not returned as remote entries
# ---------------------------------------------------------------------------


def test_d001_sort_links_excluded() -> None:
    html = _load("defcon_section.html")
    entries = parse_listing(html, _BASE)
    # Sort links like ?C=N&O=D must not appear
    assert not any("?" in e.url for e in entries)


def test_d001_parent_row_excluded() -> None:
    html = _load("defcon_section.html")
    entries = parse_listing(html, _BASE)

    display_names = [e.display_name for e in entries]
    assert "Parent Directory" not in display_names
    assert ".." not in display_names


def test_d001_valid_entries_present() -> None:
    html = _load("defcon_section.html")
    entries = parse_listing(html, _BASE)

    kinds = {e.kind for e in entries}
    assert "directory" in kinds
    assert "file" in kinds


# ---------------------------------------------------------------------------
# D-002: Encoded names and HTML entities
# ---------------------------------------------------------------------------


def test_d002_raw_href_preserved() -> None:
    html = _load("encoded_names.html")
    base = "https://infocon.example.com/archive/"
    entries = parse_listing(html, base)

    urls = [e.url for e in entries]
    # URL-encoded form must survive into the canonical URL
    assert any("DEF%20CON%2032" in u for u in urls)


def test_d002_html_entities_decoded_in_display() -> None:
    html = _load("encoded_names.html")
    base = "https://infocon.example.com/archive/"
    entries = parse_listing(html, base)

    display_names = [e.display_name for e in entries]
    # &amp; in display text must be decoded to &
    assert any("T&A Talks" in n for n in display_names)


def test_d002_off_host_link_excluded() -> None:
    html = _load("encoded_names.html")
    base = "https://infocon.example.com/archive/"
    entries = parse_listing(html, base)

    assert not any("other.example.com" in e.url for e in entries)


# ---------------------------------------------------------------------------
# D-003: Off-host absolute links are rejected
# ---------------------------------------------------------------------------


def test_d003_offhost_absolute_rejected() -> None:
    html = """
    <table>
      <tr><td><a href="local/">local dir</a></td><td>2024-01-01</td><td>-</td></tr>
      <tr><td><a href="http://evil.com/payload">evil link</a></td><td></td><td></td></tr>
    </table>
    """
    base = "https://good.example.com/archive/"
    entries = parse_listing(html, base)

    assert all("evil.com" not in e.url for e in entries)
    assert len(entries) == 1
    assert entries[0].display_name == "local dir"


def test_d003_same_host_absolute_accepted() -> None:
    html = """
    <table>
      <tr>
        <td><a href="https://good.example.com/archive/sub/">sub dir</a></td>
        <td></td><td></td>
      </tr>
    </table>
    """
    base = "https://good.example.com/archive/"
    entries = parse_listing(html, base)

    assert len(entries) == 1


# ---------------------------------------------------------------------------
# D-004: Missing size/date cells → None hints
# ---------------------------------------------------------------------------


def test_d004_missing_size_is_none() -> None:
    html = _load("missing_hints.html")
    base = "https://infocon.example.com/minimal/"
    entries = parse_listing(html, base)

    assert len(entries) >= 1
    for entry in entries:
        assert entry.size_hint is None


def test_d004_missing_modified_is_none() -> None:
    html = _load("missing_hints.html")
    base = "https://infocon.example.com/minimal/"
    entries = parse_listing(html, base)

    for entry in entries:
        assert entry.modified_hint is None


def test_d004_size_parsed_when_present() -> None:
    html = _load("defcon_section.html")
    entries = parse_listing(html, _BASE)

    file_entries = [e for e in entries if e.kind == "file"]
    sizes = [e.size_hint for e in file_entries]
    assert any(s is not None for s in sizes)


def test_d004_date_parsed_when_present() -> None:
    html = _load("defcon_section.html")
    entries = parse_listing(html, _BASE)

    modified_hints = [e.modified_hint for e in entries]
    assert any(m is not None for m in modified_hints)
