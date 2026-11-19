import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from travelrag.api.deps import CurrentUserDep, JobServiceDep, resolve_user
from travelrag.api.sse import job_event_stream
from travelrag.core.config import get_settings
from travelrag.core.db import get_sessionmaker
from travelrag.jobs.adapters.pg_events import PgListenJobEventSubscriber
from travelrag.jobs.domain import InvalidTransitionError, Job, JobKind, JobStage, JobStatus
from travelrag.jobs.dummy import DummyJobOptions
from travelrag.jobs.factory import build_job_service
from travelrag.jobs.service import JobNotFoundError, JobService

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobOut(BaseModel):
    id: uuid.UUID
    kind: JobKind
    status: JobStatus
    stage: JobStage | None
    progress: int
    error: str | None
    payload: dict[str, Any]
    video_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_job(cls, job: Job) -> "JobOut":
        return cls(
            id=job.id,
            kind=job.kind,
            status=job.status,
            stage=job.stage,
            progress=job.progress,
            error=job.error,
            payload=job.payload,
            video_id=job.video_id,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")


async def _get_owned(service: JobService, job_id: uuid.UUID, user_id: uuid.UUID) -> Job:
    try:
        return await service.get_for_user(job_id, user_id)
    except JobNotFoundError as exc:
        raise _not_found() from exc


@router.get("")
async def list_jobs(user: CurrentUserDep, service: JobServiceDep) -> list[JobOut]:
    return [JobOut.from_job(job) for job in await service.list_for_user(user.id)]


@router.post("/dummy", status_code=status.HTTP_202_ACCEPTED)
async def create_dummy_job(
    body: DummyJobOptions, user: CurrentUserDep, service: JobServiceDep
) -> JobOut:
    job = await service.create(user.id, JobKind.DUMMY, body.model_dump(mode="json"))
    return JobOut.from_job(job)


@router.get("/{job_id}")
async def get_job(job_id: uuid.UUID, user: CurrentUserDep, service: JobServiceDep) -> JobOut:
    return JobOut.from_job(await _get_owned(service, job_id, user.id))


@router.post("/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_job(job_id: uuid.UUID, user: CurrentUserDep, service: JobServiceDep) -> JobOut:
    try:
        return JobOut.from_job(await service.retry(job_id, user.id))
    except JobNotFoundError as exc:
        raise _not_found() from exc
    except InvalidTransitionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/{job_id}/review/approve")
async def approve_review(job_id: uuid.UUID, user: CurrentUserDep, service: JobServiceDep) -> JobOut:
    try:
        return JobOut.from_job(await service.approve_review(job_id, user.id))
    except JobNotFoundError as exc:
        raise _not_found() from exc
    except InvalidTransitionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/{job_id}/events", response_class=StreamingResponse)
async def job_events(
    job_id: uuid.UUID,
    token: str = Query(description="Access token (EventSource cannot send headers)"),
) -> StreamingResponse:
    # Uses short-lived sessions instead of the request-scoped dependency so that no pooled
    # connection stays checked out for the lifetime of the stream.
    async with get_sessionmaker()() as session:
        user = await resolve_user(session, token)
        await _get_owned(build_job_service(session), job_id, user.id)
    user_id = user.id

    async def load_snapshot() -> Job:
        async with get_sessionmaker()() as session:
            return await build_job_service(session).get_for_user(job_id, user_id)

    settings = get_settings()
    subscriber = PgListenJobEventSubscriber(
        settings.psycopg_conninfo, settings.sse_heartbeat_seconds
    )
    return StreamingResponse(
        job_event_stream(job_id, load_snapshot, subscriber),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
