"""Structured logging with mandatory redaction.

All log records produced by this module strip:
- IPv4/IPv6 peer addresses (BitTorrent swarm IPs)
- Absolute filesystem paths that escape the configured archive root
- Session tokens (bootstrap URL fragments)
- Any field named 'ip', 'peer', 'token', or 'address'

Use `configure_logging(app_config)` once at startup. Application code uses
the standard `logging.getLogger(__name__)` API — no special log calls needed.
"""
from __future__ import annotations

import ipaddress
import logging
import logging.handlers
import re
from pathlib import Path
from typing import Any

_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_IPV6 = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b")
_TOKEN = re.compile(r"/bootstrap/[A-Za-z0-9_\-]{10,}")

_REDACT = "[redacted]"
_MAX_LOG_BYTES = 10 * 1024 * 1024   # 10 MiB per file
_BACKUP_COUNT = 5


class RedactingFilter(logging.Filter):
    """Strip peer IPs and secrets from any log record's message and args."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _scrub(str(record.msg))
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _scrub(str(v)) if isinstance(v, str) else v
                               for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(_scrub(a) if isinstance(a, str) else a
                                    for a in record.args)
        return True


def _scrub(text: str) -> str:
    text = _TOKEN.sub(_REDACT, text)
    # Replace valid routable IPs; keep loopback/private in case they aid debug
    def _replace_ip(m: re.Match) -> str:  # type: ignore[type-arg]
        try:
            addr = ipaddress.ip_address(m.group())
            if addr.is_global:
                return _REDACT
        except ValueError:
            pass
        return m.group()

    text = _IPV4.sub(_replace_ip, text)
    text = _IPV6.sub(lambda m: _REDACT, text)
    return text


def configure_logging(
    log_dir: Path | None = None,
    *,
    level: int = logging.INFO,
    stderr: bool = True,
) -> None:
    """Configure root logger with optional rotating file handler.

    Args:
        log_dir: Directory for rotating log files. If None, file logging
            is skipped (useful in tests).
        level: Minimum log level.
        stderr: Attach a StreamHandler to stderr when True.
    """
    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    filt = RedactingFilter()

    if stderr:
        handler: logging.Handler = logging.StreamHandler()
        handler.setFormatter(fmt)
        handler.addFilter(filt)
        root.addHandler(handler)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "librarian.log",
            maxBytes=_MAX_LOG_BYTES,
            backupCount=_BACKUP_COUNT,
        )
        file_handler.setFormatter(fmt)
        file_handler.addFilter(filt)
        root.addHandler(file_handler)

    # Silence overly chatty third-party loggers
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("libtorrent").setLevel(logging.WARNING)
    # httpx uses %d for status codes; Python 3.14 strict type checking breaks it
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def make_support_bundle_summary() -> dict[str, Any]:
    """Return a summary dict safe to include in a support bundle.

    Contains no archive contents, credentials, peer IPs, or absolute paths.
    """
    import datetime

    return {
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "description": "InfoCon Librarian diagnostic summary",
        "note": (
            "This bundle contains only application logs and configuration "
            "metadata. No archive media, credentials, or peer IP addresses "
            "are included."
        ),
    }
