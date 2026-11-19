import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress

from travelrag.core.config import get_settings
from travelrag.core.db import get_sessionmaker
from travelrag.core.logging import configure_logging
from travelrag.jobs.adapters.procrastinate_queue import TASK_NAMES
from travelrag.jobs.domain import InvalidTransitionError, JobKind
from travelrag.jobs.dummy import run_dummy_pipeline
from travelrag.jobs.factory import build_job_service
from travelrag.jobs.service import JobService
from travelrag.worker.app import app

configure_logging(get_settings().log_level)
logger = logging.getLogger(__name__)

Pipeline = Callable[[JobService, uuid.UUID], Awaitable[None]]


async def _run_job(job_id: uuid.UUID, pipeline: Pipeline) -> None:
    """Run a pipeline; on an unexpected error, mark the job failed and re-raise so procrastinate
    records the failure too."""
    try:
        async with get_sessionmaker()() as session:
            await pipeline(build_job_service(session), job_id)
    except Exception as exc:
        logger.exception("job %s crashed", job_id)
        async with get_sessionmaker()() as session:
            with suppress(InvalidTransitionError):
                await build_job_service(session).fail(job_id, f"{type(exc).__name__}: {exc}")
        raise


@app.task(name=TASK_NAMES[JobKind.DUMMY])
async def run_dummy_job(job_id: str) -> None:
    await _run_job(uuid.UUID(job_id), run_dummy_pipeline)
