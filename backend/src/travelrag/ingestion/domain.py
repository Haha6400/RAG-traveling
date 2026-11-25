"""Video entity and upload rules. Pure Python: no framework or database imports."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePath
from typing import Any

from travelrag.media.domain import VideoMetadata

ALLOWED_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".webm", ".mkv"})


class VideoSourceKind(StrEnum):
    LOCAL = "local"
    TIKTOK = "tiktok"  # Phase 4


class UnsupportedVideoTypeError(Exception):
    def __init__(self, filename: str) -> None:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        super().__init__(f"'{filename}' is not a supported video file ({allowed})")


class FileTooLargeError(Exception):
    def __init__(self, max_bytes: int) -> None:
        super().__init__(f"File is larger than the {max_bytes // (1024 * 1024)} MB limit")
        self.max_bytes = max_bytes


class DuplicateVideoError(Exception):
    def __init__(self, existing_id: uuid.UUID | None) -> None:
        super().__init__("This video was already uploaded")
        self.existing_id = existing_id


class VideoNotFoundError(Exception):
    pass


def video_extension(filename: str) -> str:
    """Return the lowercased extension, or raise UnsupportedVideoTypeError."""
    extension = PurePath(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise UnsupportedVideoTypeError(filename)
    return extension


def storage_key(user_id: uuid.UUID, video_id: uuid.UUID, extension: str) -> str:
    return f"{user_id}/{video_id}{extension}"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Video:
    id: uuid.UUID
    user_id: uuid.UUID
    source: VideoSourceKind
    storage_key: str
    original_name: str
    content_type: str | None
    size_bytes: int
    sha256: str
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    has_audio: bool | None = None
    created_at: datetime = field(default_factory=_now)
    # Intermediate pipeline results (see ingestion.pipeline): None = stage not run yet.
    language: str | None = None
    transcript: list[dict[str, Any]] | None = None  # [{start_ms, end_ms, text}]
    ocr: list[dict[str, Any]] | None = None  # [{ts_ms, lines: [str]}]
    extracted_at: datetime | None = None

    def apply_metadata(self, metadata: VideoMetadata) -> None:
        self.duration_ms = metadata.duration_ms
        self.width = metadata.width
        self.height = metadata.height
        self.has_audio = metadata.has_audio


def metadata_problem(metadata: VideoMetadata, max_seconds: int) -> str | None:
    """Why a probed file cannot be processed, or None if it is fine."""
    if not metadata.has_video:
        return "The file has no video stream"
    if metadata.duration_ms is not None and metadata.duration_ms > max_seconds * 1000:
        minutes = metadata.duration_ms / 60_000
        return f"The video is {minutes:.1f} min long; the limit is {max_seconds // 60} min"
    return None
