import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from travelrag.ingestion.domain import Video, VideoSourceKind
from travelrag.jobs.domain import Job, JobKind


@dataclass(frozen=True)
class IncomingVideo:
    filename: str
    content_type: str | None
    chunks: AsyncIterator[bytes]


class VideoSource(Protocol):
    """Where a video comes from: a local upload now, a TikTok link later."""

    kind: VideoSourceKind

    async def open(self) -> IncomingVideo: ...


@dataclass(frozen=True)
class StoredFile:
    size_bytes: int
    sha256: str


class VideoStorage(Protocol):
    async def save(self, key: str, chunks: AsyncIterator[bytes], max_bytes: int) -> StoredFile:
        """Write the stream under `key`. Raises FileTooLargeError (nothing is kept)."""
        ...

    async def delete(self, key: str) -> None:
        """Remove the file; a missing file is not an error."""
        ...

    def local_path(self, key: str) -> Path:
        """Filesystem path for readers that need one (decoders, file responses)."""
        ...


class VideoRepository(Protocol):
    async def add(self, video: Video) -> None:
        """Stage a new video. Raises DuplicateVideoError on a (user, sha256) clash."""
        ...

    async def get(self, video_id: uuid.UUID) -> Video | None: ...

    async def find_by_hash(self, user_id: uuid.UUID, sha256: str) -> Video | None: ...

    async def list_for_user(self, user_id: uuid.UUID, limit: int = 100) -> list[Video]: ...

    async def update(self, video: Video) -> None: ...

    async def delete(self, video_id: uuid.UUID) -> None:
        """Delete the video; its jobs are removed with it."""
        ...


class JobScheduler(Protocol):
    """The slice of the jobs module that ingestion needs (implemented by JobService)."""

    async def create(
        self,
        user_id: uuid.UUID,
        kind: JobKind,
        payload: dict[str, Any] | None = None,
        video_id: uuid.UUID | None = None,
    ) -> Job: ...

    async def latest_for_videos(self, video_ids: list[uuid.UUID]) -> dict[uuid.UUID, Job]: ...


class PlaceCleanup(Protocol):
    """The slice of the places module that video deletion needs (implemented by PlacesService)."""

    async def places_for_video(self, video_id: uuid.UUID) -> set[uuid.UUID]: ...

    async def refresh_after_removal(
        self, user_id: uuid.UUID, place_ids: set[uuid.UUID]
    ) -> set[uuid.UUID]: ...
