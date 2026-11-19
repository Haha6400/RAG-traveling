import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress

from sqlalchemy.ext.asyncio import AsyncSession

from travelrag.core.config import get_settings
from travelrag.core.db import SessionUnitOfWork, get_sessionmaker
from travelrag.core.logging import configure_logging
from travelrag.ingestion.adapters.sqlalchemy_repo import SqlAlchemyVideoRepository
from travelrag.ingestion.factory import build_video_storage
from travelrag.ingestion.pipeline import process_video
from travelrag.jobs.adapters.procrastinate_queue import TASK_NAMES
from travelrag.jobs.domain import InvalidTransitionError, JobKind
from travelrag.jobs.dummy import run_dummy_pipeline
from travelrag.jobs.factory import build_job_service
from travelrag.jobs.service import JobNotFoundError
from travelrag.media.adapters.pyav_prober import PyAVVideoProber
from travelrag.worker.app import app

configure_logging(get_settings().log_level)
logger = logging.getLogger(__name__)

Pipeline = Callable[[AsyncSession, uuid.UUID], Awaitable[None]]


async def _run_job(job_id: uuid.UUID, pipeline: Pipeline) -> None:
    """Run a pipeline in its own session; on an unexpected error, mark the job failed and
    re-raise so procrastinate records the failure too."""
    try:
        async with get_sessionmaker()() as session:
            await pipeline(session, job_id)
    except Exception as exc:
        logger.exception("job %s crashed", job_id)
        async with get_sessionmaker()() as session:
            # The job may already be terminal, or deleted together with its video.
            with suppress(InvalidTransitionError, JobNotFoundError):
                await build_job_service(session).fail(job_id, f"{type(exc).__name__}: {exc}")
        raise


async def _dummy(session: AsyncSession, job_id: uuid.UUID) -> None:
    await run_dummy_pipeline(build_job_service(session), job_id)


async def _process_video(session: AsyncSession, job_id: uuid.UUID) -> None:
    await process_video(
        job_id,
        jobs=build_job_service(session),
        videos=SqlAlchemyVideoRepository(session),
        storage=build_video_storage(),
        prober=PyAVVideoProber(),
        uow=SessionUnitOfWork(session),
        max_video_seconds=get_settings().max_video_seconds,
    )


@app.task(name=TASK_NAMES[JobKind.DUMMY])
async def run_dummy_job(job_id: str) -> None:
    await _run_job(uuid.UUID(job_id), _dummy)


@app.task(name=TASK_NAMES[JobKind.PROCESS_VIDEO])
async def run_process_video_job(job_id: str) -> None:
    await _run_job(uuid.UUID(job_id), _process_video)
