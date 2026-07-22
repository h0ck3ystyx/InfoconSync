"""Shared test-support helpers."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _detect_symlink_support() -> bool:
    """True if this process can create symlinks.

    On Windows, creating a symlink requires the SeCreateSymbolicLinkPrivilege,
    granted only to administrators or when Developer Mode is enabled. Rather
    than fail, symlink-dependent tests skip when the privilege is absent.
    """
    if not hasattr(os, "symlink"):
        return False
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "target"
        target.mkdir()
        link = Path(d) / "link"
        try:
            link.symlink_to(target)
            return True
        except (OSError, NotImplementedError):
            return False


SYMLINKS_SUPPORTED = _detect_symlink_support()
