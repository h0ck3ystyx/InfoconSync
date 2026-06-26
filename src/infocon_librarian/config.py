"""Platform-appropriate application data directories."""
from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import PlatformDirs

_APP_NAME = "infocon-librarian"
_APP_AUTHOR = "infocon-librarian"

_dirs = PlatformDirs(appname=_APP_NAME, appauthor=_APP_AUTHOR)


@dataclass(frozen=True)
class AppConfig:
    """Resolved paths for all application data directories."""

    config_dir: Path       # config.json lives here
    data_dir: Path         # database, archive snapshots
    cache_dir: Path        # remote fetch cache
    log_dir: Path          # rotating diagnostic logs
    torrents_dir: Path     # fetched .torrent files keyed by infohash
    jobs_dir: Path         # resumable plan and engine state
    receipts_dir: Path     # immutable transfer receipts
    snapshots_dir: Path    # compact local inventories

    @property
    def db_path(self) -> Path:
        return self.data_dir / "librarian.db"

    def ensure_dirs(self) -> None:
        """Create all application directories with user-only permissions."""
        for d in (
            self.config_dir,
            self.data_dir,
            self.cache_dir,
            self.log_dir,
            self.torrents_dir,
            self.jobs_dir,
            self.receipts_dir,
            self.snapshots_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
            # Restrict to owner-only on Unix; Windows ignores mode
            with contextlib.suppress(OSError):
                os.chmod(d, 0o700)


def default_config() -> AppConfig:
    """Return AppConfig using platform-standard directories."""
    data = Path(_dirs.user_data_dir)
    return AppConfig(
        config_dir=Path(_dirs.user_config_dir),
        data_dir=data,
        cache_dir=Path(_dirs.user_cache_dir),
        log_dir=Path(_dirs.user_log_dir),
        torrents_dir=data / "torrents",
        jobs_dir=data / "jobs",
        receipts_dir=data / "receipts",
        snapshots_dir=data / "archive-snapshots",
    )
