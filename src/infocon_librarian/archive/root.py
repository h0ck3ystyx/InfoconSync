"""Archive root validation."""
from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from infocon_librarian.domain.errors import ArchiveRootError

# Advisory InfoCon section names — presence is a hint, not a requirement
_KNOWN_SECTIONS = frozenset(
    {
        "Defcon",
        "defcon",
        "DEFCON",
        "Shmoocon",
        "shmoocon",
        "Blackhat",
        "blackhat",
        "phreaknic",
        "Notacon",
        "LayerOne",
        "HOPE",
        "Summercon",
    }
)


@dataclass(frozen=True)
class ArchiveRootInfo:
    """Validated archive root descriptor."""

    canonical_path: str          # absolute, resolved path
    volume_fingerprint: str      # st_dev + platform extras, stable per volume
    free_bytes: int              # available space on the volume at validation time
    known_sections: list[str]    # advisory: InfoCon section names found at top level


@dataclass(frozen=True)
class ArchiveRootFailure:
    """Describes why an archive root is unusable."""

    path: str
    reason: str                  # human-readable explanation
    kind: str                    # "not_dir", "not_writable", "nested_in_appdata", "disconnected"


def validate_root(
    path: Path,
    *,
    app_data_dir: Path | None = None,
) -> ArchiveRootInfo:
    """Validate that *path* is a suitable archive root.

    Args:
        path: Candidate archive root. May not yet be canonical.
        app_data_dir: Application data directory to reject nested roots.

    Returns:
        ArchiveRootInfo on success.

    Raises:
        ArchiveRootError: With a human-readable reason on any failure.
    """
    try:
        resolved = path.resolve(strict=True)
    except (OSError, FileNotFoundError) as exc:
        raise ArchiveRootError(
            f"Archive root {str(path)!r} does not exist or is disconnected: {exc}"
        ) from exc

    if not resolved.is_dir():
        raise ArchiveRootError(f"Archive root {str(resolved)!r} is not a directory")

    # Reject roots nested inside the application data directory
    if app_data_dir is not None:
        try:
            resolved.relative_to(app_data_dir.resolve())
            raise ArchiveRootError(
                f"Archive root {str(resolved)!r} is nested inside the application "
                f"data directory {str(app_data_dir)!r}"
            )
        except ValueError:
            pass  # not nested — good

    # Check writability with a probe file
    probe = resolved / f".librarian-probe-{uuid.uuid4().hex}"
    try:
        probe.touch()
        probe.unlink()
    except OSError as exc:
        raise ArchiveRootError(
            f"Archive root {str(resolved)!r} is not writable: {exc}"
        ) from exc

    # Capture volume fingerprint
    fingerprint = _volume_fingerprint(resolved)

    # Free space
    try:
        usage = shutil.disk_usage(resolved)
        free_bytes = usage.free
    except OSError as exc:
        raise ArchiveRootError(
            f"Cannot determine free space for {str(resolved)!r}: {exc}"
        ) from exc

    # Advisory section detection
    known: list[str] = []
    try:
        for entry in resolved.iterdir():
            if entry.is_dir() and entry.name in _KNOWN_SECTIONS:
                known.append(entry.name)
    except OSError:
        pass

    return ArchiveRootInfo(
        canonical_path=str(resolved),
        volume_fingerprint=fingerprint,
        free_bytes=free_bytes,
        known_sections=sorted(known),
    )


def _volume_fingerprint(path: Path) -> str:
    """Return a stable volume identifier for *path*.

    Uses st_dev on all platforms plus the volume name on macOS/Windows
    where available.
    """
    try:
        stat = os.stat(path)
        dev = stat.st_dev
    except OSError as exc:
        raise ArchiveRootError(f"Cannot stat archive root: {exc}") from exc

    extras = ""
    # macOS: get volume UUID via statfs
    try:
        import subprocess  # noqa: PLC0415

        result = subprocess.run(
            ["diskutil", "info", "-plist", str(path)],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            import plistlib  # noqa: PLC0415

            info = plistlib.loads(result.stdout)
            vol_uuid = info.get("VolumeUUID", "")
            if vol_uuid:
                extras = f":{vol_uuid}"
    except Exception:  # noqa: BLE001
        pass

    return f"{dev}{extras}"
