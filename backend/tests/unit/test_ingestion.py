import copy
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.fakes import JobHarness
from travelrag.ingestion.adapters.local_storage import LocalVideoStorage
from travelrag.ingestion.domain import (
    DuplicateVideoError,
    FileTooLargeError,
    UnsupportedVideoTypeError,
    Video,
    VideoNotFoundError,
    VideoSourceKind,
    metadata_problem,
    video_extension,
)
from travelrag.ingestion.pipeline import process_video
from travelrag.ingestion.ports import IncomingVideo
from travelrag.ingestion.service import IngestionService
from travelrag.jobs.domain import JobKind, JobStage, JobStatus
from travelrag.media.domain import InvalidVideoError, VideoMetadata

GOOD = VideoMetadata(duration_ms=30_000, width=720, height=1280, has_video=True, has_audio=True)


class FakeVideoRepository:
    def __init__(self) -> None:
        self.videos: dict[uuid.UUID, Video] = {}

    async def add(self, video: Video) -> None:
        if await self.find_by_hash(video.user_id, video.sha256):
            raise DuplicateVideoError(None)
        self.videos[video.id] = copy.deepcopy(video)

    async def get(self, video_id: uuid.UUID) -> Video | None:
        video = self.videos.get(video_id)
        return copy.deepcopy(video) if video else None

    async def find_by_hash(self, user_id: uuid.UUID, sha256: str) -> Video | None:
        return next(
            (v for v in self.videos.values() if v.user_id == user_id and v.sha256 == sha256), None
        )

    async def list_for_user(self, user_id: uuid.UUID, limit: int = 100) -> list[Video]:
        return [v for v in self.videos.values() if v.user_id == user_id][:limit]

    async def update(self, video: Video) -> None:
        self.videos[video.id] = copy.deepcopy(video)

    async def delete(self, video_id: uuid.UUID) -> None:
        self.videos.pop(video_id, None)


class FakeSource:
    kind = VideoSourceKind.LOCAL

    def __init__(self, filename: str, data: bytes) -> None:
        self._filename = filename
        self._data = data

    async def open(self) -> IncomingVideo:
        async def chunks() -> AsyncIterator[bytes]:
            yield self._data

        return IncomingVideo(self._filename, "video/mp4", chunks())


class FakeProber:
    def __init__(self, result: VideoMetadata | Exception) -> None:
        self._result = result

    async def probe(self, path: Path) -> VideoMetadata:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class Harness(JobHarness):
    def __init__(self, media_dir: Path, max_bytes: int = 1000) -> None:
        super().__init__()
        self.videos = FakeVideoRepository()
        self.storage = LocalVideoStorage(media_dir)
        self.ingestion = IngestionService(
            self.videos, self.storage, self.service, self.uow, max_upload_bytes=max_bytes
        )

    def files(self) -> list[Path]:
        return [p for p in self.storage.local_path("").rglob("*") if p.is_file()]

    async def run(self, job_id: uuid.UUID, prober: FakeProber, max_seconds: int = 600) -> None:
        await process_video(
            job_id,
            jobs=self.service,
            videos=self.videos,
            storage=self.storage,
            prober=prober,
            uow=self.uow,
            max_video_seconds=max_seconds,
        )


# --- domain ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["clip.MP4", "a.b.mov", "x.webm"])
def test_allowed_extensions(name: str) -> None:
    assert video_extension(name).startswith(".")


@pytest.mark.parametrize("name", ["photo.jpg", "noext", "movie.mp4.exe"])
def test_rejected_extensions(name: str) -> None:
    with pytest.raises(UnsupportedVideoTypeError):
        video_extension(name)


def test_metadata_problems() -> None:
    assert metadata_problem(GOOD, max_seconds=60) is None
    no_video = VideoMetadata(1000, None, None, has_video=False, has_audio=True)
    assert "no video" in (metadata_problem(no_video, 60) or "")
    too_long = VideoMetadata(61 * 60_000, 1, 1, has_video=True, has_audio=True)
    assert "limit" in (metadata_problem(too_long, 60 * 60) or "")


# --- upload ---------------------------------------------------------------------------------


async def test_ingest_stores_file_records_video_and_schedules_job(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    user = uuid.uuid4()

    result = await h.ingestion.ingest(user, FakeSource("Trip.MOV", b"video-bytes"))

    assert result.video.storage_key == f"{user}/{result.video.id}.mov"
    assert result.video.size_bytes == len(b"video-bytes")
    assert h.storage.local_path(result.video.storage_key).read_bytes() == b"video-bytes"
    assert result.job is not None
    assert result.job.kind is JobKind.PROCESS_VIDEO
    assert result.job.video_id == result.video.id
    assert h.queue.enqueued == [result.job.id]


async def test_duplicate_upload_is_rejected_and_cleaned_up(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    user = uuid.uuid4()
    first = await h.ingestion.ingest(user, FakeSource("a.mp4", b"same"))

    with pytest.raises(DuplicateVideoError) as info:
        await h.ingestion.ingest(user, FakeSource("b.mp4", b"same"))
    assert info.value.existing_id == first.video.id
    assert len(h.files()) == 1

    # Another user may upload the same file.
    await h.ingestion.ingest(uuid.uuid4(), FakeSource("a.mp4", b"same"))
    assert len(h.files()) == 2


async def test_unsupported_and_oversized_uploads_store_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_bytes=10)
    with pytest.raises(UnsupportedVideoTypeError):
        await h.ingestion.ingest(uuid.uuid4(), FakeSource("a.txt", b"x"))
    with pytest.raises(FileTooLargeError):
        await h.ingestion.ingest(uuid.uuid4(), FakeSource("a.mp4", b"x" * 11))
    assert h.files() == []
    assert h.queue.enqueued == []


async def test_list_includes_latest_job_and_delete_removes_file(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    user = uuid.uuid4()
    uploaded = await h.ingestion.ingest(user, FakeSource("a.mp4", b"x"))

    [item] = await h.ingestion.list_for_user(user)
    assert item.job is not None and item.job.id == uploaded.job.id  # type: ignore[union-attr]

    with pytest.raises(VideoNotFoundError):
        await h.ingestion.delete(uploaded.video.id, uuid.uuid4())
    await h.ingestion.delete(uploaded.video.id, user)
    assert h.files() == []
    assert await h.ingestion.list_for_user(user) == []


# --- pipeline -------------------------------------------------------------------------------


async def _uploaded(h: Harness) -> tuple[uuid.UUID, uuid.UUID]:
    user = uuid.uuid4()
    result = await h.ingestion.ingest(user, FakeSource("a.mp4", b"x"))
    assert result.job is not None
    return result.job.id, result.video.id


async def test_pipeline_probes_and_completes(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    job_id, video_id = await _uploaded(h)

    await h.run(job_id, FakeProber(GOOD))

    job = await h.repo.get(job_id)
    assert job is not None and job.status is JobStatus.DONE
    assert JobStage.PROBE in {e.stage for e in h.publisher.delivered}
    video = await h.videos.get(video_id)
    assert video is not None
    assert (video.duration_ms, video.width, video.height, video.has_audio) == (
        30_000,
        720,
        1280,
        True,
    )


@pytest.mark.parametrize(
    ("prober", "max_seconds", "expected"),
    [
        (FakeProber(InvalidVideoError("Invalid data")), 600, "could not be read"),
        (FakeProber(VideoMetadata(1000, None, None, False, True)), 600, "no video stream"),
        (FakeProber(GOOD), 10, "limit"),
    ],
)
async def test_pipeline_fails_on_unusable_videos(
    tmp_path: Path, prober: FakeProber, max_seconds: int, expected: str
) -> None:
    h = Harness(tmp_path)
    job_id, _ = await _uploaded(h)

    await h.run(job_id, prober, max_seconds)

    job = await h.repo.get(job_id)
    assert job is not None and job.status is JobStatus.FAILED
    assert expected in (job.error or "")


async def test_pipeline_fails_when_video_was_deleted(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    job_id, video_id = await _uploaded(h)
    await h.videos.delete(video_id)

    await h.run(job_id, FakeProber(GOOD))

    job = await h.repo.get(job_id)
    assert job is not None and job.status is JobStatus.FAILED
