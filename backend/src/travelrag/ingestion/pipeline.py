"""The `process_video` job. Stages are added here as they are built (ASR, OCR, ...)."""

import uuid

from travelrag.ingestion.domain import metadata_problem
from travelrag.ingestion.ports import VideoRepository, VideoStorage
from travelrag.jobs.domain import JobStage
from travelrag.jobs.ports import UnitOfWork
from travelrag.jobs.service import JobService
from travelrag.media.domain import InvalidVideoError
from travelrag.media.ports import VideoProber


async def process_video(
    job_id: uuid.UUID,
    *,
    jobs: JobService,
    videos: VideoRepository,
    storage: VideoStorage,
    prober: VideoProber,
    uow: UnitOfWork,
    max_video_seconds: int,
) -> None:
    job = await jobs.start(job_id)
    video = await videos.get(job.video_id) if job.video_id else None
    if video is None:
        await jobs.fail(job_id, "The video no longer exists")
        return

    # --- probe: make sure the file is a usable video before spending ASR/LLM time on it.
    await jobs.advance(job_id, JobStage.PROBE, 0)
    try:
        metadata = await prober.probe(storage.local_path(video.storage_key))
    except InvalidVideoError as exc:
        await jobs.fail(job_id, f"The file could not be read as a video ({exc})")
        return
    if problem := metadata_problem(metadata, max_video_seconds):
        await jobs.fail(job_id, problem)
        return
    video.apply_metadata(metadata)
    await videos.update(video)
    await uow.commit()

    # Next: ASR (1.2), extraction (1.3), resolution (1.4), aggregation (1.5), OCR (1.6).
    await jobs.complete(job_id)
