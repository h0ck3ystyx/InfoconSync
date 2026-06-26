from __future__ import annotations


class LibrarianError(Exception):
    """Base for all application errors."""


class InvalidTorrent(LibrarianError):
    """Torrent bytes could not be parsed or failed safety validation."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PathEscapesRoot(LibrarianError):
    """A resolved path would land outside the archive root."""

    def __init__(self, path: str) -> None:
        super().__init__(f"Path escapes archive root: {path!r}")
        self.path = path


class ArchiveRootError(LibrarianError):
    """Archive root is unusable (missing, read-only, or mismatched volume)."""


class TransferError(LibrarianError):
    """A transfer operation failed."""
