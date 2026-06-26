from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ArchiveStatus(StrEnum):
    NEW = "new"
    CHANGED_MARKER = "changed_marker"
    CHANGED_MANIFEST = "changed_manifest"
    VERIFIED_CURRENT = "verified_current"
    PRESENT_UNVERIFIED = "present_unverified"
    UNKNOWN = "unknown"
    LOCAL_ONLY = "local_only"
    TRANSFER_INCOMPLETE = "transfer_incomplete"
    DOWNLOADED_UNVERIFIED = "downloaded_unverified"


class TransferMethod(StrEnum):
    TORRENT = "torrent"
    HTTPS = "https"


class VerificationLevel(StrEnum):
    PIECE_VERIFIED = "piece_verified"
    MANIFEST_VERIFIED = "manifest_verified"
    UNVERIFIED = "unverified"


class TransferState(StrEnum):
    DRAFT = "draft"
    PREFLIGHTED = "preflighted"
    QUEUED = "queued"
    CHECKING = "checking"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    AWAITING_USER_FALLBACK = "awaiting_user_fallback"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELED = "canceled"


class EvidenceKind(StrEnum):
    REMOTE_LISTING = "remote_listing"
    TORRENT_MANIFEST = "torrent_manifest"
    TORRENT_RECHECK = "torrent_recheck"
    LOCAL_SNAPSHOT = "local_snapshot"
    HTTP_RESULT = "http_result"


class TorrentProtocol(StrEnum):
    V1 = "v1"
    V2 = "v2"
    HYBRID = "hybrid"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TorrentFile:
    """One file entry from a torrent manifest."""

    index: int
    relative_path: str  # normalized, forward-slash separated
    size: int           # exact bytes from the manifest


@dataclass(frozen=True)
class TorrentManifest:
    """Parsed, validated metainfo from a .torrent file."""

    url: str
    protocol: TorrentProtocol
    v1_infohash: str | None   # hex string, None for v2-only
    v2_infohash: str | None   # hex string, None for v1-only
    files: tuple[TorrentFile, ...]
    trackers: tuple[str, ...]    # tracker URLs
    total_size: int              # sum of all file sizes in bytes
    name: str                    # advisory root name from the torrent

    @property
    def infohash(self) -> str:
        """Primary identifier: prefer v1 for display/logging."""
        return self.v1_infohash or self.v2_infohash or ""


@dataclass(frozen=True)
class EngineJobId:
    value: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True)
class TransferProgress:
    job_id: EngineJobId
    state: TransferState
    total_bytes: int
    downloaded_bytes: int
    uploaded_bytes: int
    download_rate: int   # bytes/sec
    upload_rate: int     # bytes/sec
    num_peers: int
    last_error: str | None


@dataclass(frozen=True)
class TorrentStartParams:
    """Parameters for starting a torrent job."""

    torrent_bytes: bytes
    save_path: str               # absolute path to archive root
    selected_indices: tuple[int, ...]  # file indices to download; empty = all
    enable_dht: bool = False
    enable_pex: bool = False
    enable_lsd: bool = False
    enable_upnp: bool = False
    enable_natpmp: bool = False
    download_limit: int = 0      # bytes/sec, 0 = unlimited
    upload_limit: int = 0        # bytes/sec, 0 = unlimited
    resume_data: bytes | None = None
