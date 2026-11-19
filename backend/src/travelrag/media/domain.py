from dataclasses import dataclass


class InvalidVideoError(Exception):
    """The file could not be decoded as a media container."""


@dataclass(frozen=True)
class VideoMetadata:
    duration_ms: int | None
    width: int | None
    height: int | None
    has_video: bool
    has_audio: bool
