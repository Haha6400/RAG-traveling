"""A fake pipeline that walks through every stage. Used to test queueing and progress events
end-to-end before the real video pipeline exists."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field

from travelrag.jobs.domain import JobStage
from travelrag.jobs.service import JobService


class DummyJobOptions(BaseModel):
    step_seconds: float = Field(default=1.0, ge=0, le=30)
    fail_at: JobStage | None = None
    needs_review: bool = False


async def run_dummy_pipeline(
    service: JobService,
    job_id: uuid.UUID,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    job = await service.start(job_id)
    options = DummyJobOptions.model_validate(job.payload)
    stages = list(JobStage)

    for index, stage in enumerate(stages):
        await service.advance(job_id, stage, progress=index * 100 // len(stages))
        await sleep(options.step_seconds)
        if stage == options.fail_at:
            await service.fail(job_id, f"Simulated failure at stage '{stage}'")
            return

    if options.needs_review:
        await service.request_review(job_id)
    else:
        await service.complete(job_id)
