import uuid
from dataclasses import dataclass

from travelrag.ingestion.domain import (
    DuplicateVideoError,
    Video,
    VideoNotFoundError,
    storage_key,
    video_extension,
)
from travelrag.ingestion.ports import (
    JobScheduler,
    PlaceCleanup,
    VideoRepository,
    VideoSource,
    VideoStorage,
)
from travelrag.jobs.domain import Job, JobKind
from travelrag.jobs.ports import UnitOfWork


@dataclass(frozen=True)
class VideoWithJob:
    video: Video
    job: Job | None


class IngestionService:
    def __init__(
        self,
        videos: VideoRepository,
        storage: VideoStorage,
        jobs: JobScheduler,
        uow: UnitOfWork,
        max_upload_bytes: int,
        places: PlaceCleanup | None = None,
    ) -> None:
        self._videos = videos
        self._storage = storage
        self._jobs = jobs
        self._uow = uow
        self._max_upload_bytes = max_upload_bytes
        self._places = places

    async def ingest(self, user_id: uuid.UUID, source: VideoSource) -> VideoWithJob:
        """Store the file, record the video and schedule its processing job."""
        incoming = await source.open()
        extension = video_extension(incoming.filename)
        video_id = uuid.uuid4()
        key = storage_key(user_id, video_id, extension)

        stored = await self._storage.save(key, incoming.chunks, self._max_upload_bytes)
        # Until the commit inside jobs.create, a failure must not leave the file behind.
        # After it, a failure (e.g. enqueueing) keeps the file: a row without its file would
        # be worse than an orphaned file.
        try:
            existing = await self._videos.find_by_hash(user_id, stored.sha256)
            if existing is not None:
                raise DuplicateVideoError(existing.id)
            video = Video(
                id=video_id,
                user_id=user_id,
                source=source.kind,
                storage_key=key,
                original_name=incoming.filename,
                content_type=incoming.content_type,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
            )
            await self._videos.add(video)
        except BaseException:
            await self._storage.delete(key)
            raise

        job = await self._jobs.create(user_id, JobKind.PROCESS_VIDEO, video_id=video.id)
        return VideoWithJob(video, job)

    async def get_for_user(self, video_id: uuid.UUID, user_id: uuid.UUID) -> Video:
        video = await self._videos.get(video_id)
        if video is None or video.user_id != user_id:
            raise VideoNotFoundError(video_id)
        return video

    async def list_for_user(self, user_id: uuid.UUID) -> list[VideoWithJob]:
        videos = await self._videos.list_for_user(user_id)
        latest = await self._jobs.latest_for_videos([v.id for v in videos])
        return [VideoWithJob(v, latest.get(v.id)) for v in videos]

    async def latest_job(self, video_id: uuid.UUID) -> Job | None:
        return (await self._jobs.latest_for_videos([video_id])).get(video_id)

    async def delete(self, video_id: uuid.UUID, user_id: uuid.UUID) -> set[uuid.UUID]:
        """Delete the video with its file, jobs, chunks and mentions, then bring the places it
        mentioned up to date. Returns the ids of places whose card changed (to re-index)."""
        video = await self.get_for_user(video_id, user_id)
        affected = await self._places.places_for_video(video.id) if self._places else set()
        await self._videos.delete(video.id)
        await self._uow.commit()
        changed: set[uuid.UUID] = set()
        if self._places is not None:
            changed = await self._places.refresh_after_removal(user_id, affected)
        await self._storage.delete(video.storage_key)
        return changed
