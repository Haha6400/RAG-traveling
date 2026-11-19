import uuid
from typing import Any

from travelrag.jobs.domain import Job, JobEvent, JobKind, JobStage
from travelrag.jobs.ports import JobEventPublisher, JobQueue, JobRepository, UnitOfWork


class JobNotFoundError(Exception):
    pass


class JobService:
    """Use cases for jobs. Every state change is persisted, published and committed together."""

    def __init__(
        self,
        repo: JobRepository,
        events: JobEventPublisher,
        uow: UnitOfWork,
        queue: JobQueue | None = None,
    ) -> None:
        self._repo = repo
        self._events = events
        self._uow = uow
        self._queue = queue

    # --- user-facing -------------------------------------------------------------------------

    async def create(
        self, user_id: uuid.UUID, kind: JobKind, payload: dict[str, Any] | None = None
    ) -> Job:
        job = Job.new(user_id, kind, payload)
        await self._repo.add(job)
        await self._events.publish(JobEvent.from_job(job))
        await self._uow.commit()
        # Enqueued after commit so the worker always finds the row. If enqueueing fails the
        # job stays 'pending'; a reaper for stale pending jobs can be added later.
        await self._require_queue().enqueue(job)
        return job

    async def get_for_user(self, job_id: uuid.UUID, user_id: uuid.UUID) -> Job:
        job = await self._repo.get(job_id)
        if job is None or job.user_id != user_id:
            raise JobNotFoundError(job_id)
        return job

    async def list_for_user(self, user_id: uuid.UUID, limit: int = 50) -> list[Job]:
        return await self._repo.list_for_user(user_id, limit)

    async def retry(self, job_id: uuid.UUID, user_id: uuid.UUID) -> Job:
        job = await self.get_for_user(job_id, user_id)
        job.retry()
        await self._save(job)
        await self._require_queue().enqueue(job)
        return job

    async def approve_review(self, job_id: uuid.UUID, user_id: uuid.UUID) -> Job:
        job = await self.get_for_user(job_id, user_id)
        job.complete()
        await self._save(job)
        return job

    # --- worker-facing -----------------------------------------------------------------------

    async def start(self, job_id: uuid.UUID) -> Job:
        job = await self._load(job_id)
        job.start()
        await self._save(job)
        return job

    async def advance(self, job_id: uuid.UUID, stage: JobStage, progress: int) -> Job:
        job = await self._load(job_id)
        job.advance(stage, progress)
        await self._save(job)
        return job

    async def request_review(self, job_id: uuid.UUID) -> Job:
        job = await self._load(job_id)
        job.request_review()
        await self._save(job)
        return job

    async def complete(self, job_id: uuid.UUID) -> Job:
        job = await self._load(job_id)
        job.complete()
        await self._save(job)
        return job

    async def fail(self, job_id: uuid.UUID, error: str) -> Job:
        job = await self._load(job_id)
        job.fail(error)
        await self._save(job)
        return job

    # --- internals ---------------------------------------------------------------------------

    async def _load(self, job_id: uuid.UUID) -> Job:
        job = await self._repo.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        return job

    async def _save(self, job: Job) -> None:
        await self._repo.update(job)
        await self._events.publish(JobEvent.from_job(job))
        await self._uow.commit()

    def _require_queue(self) -> JobQueue:
        if self._queue is None:
            raise RuntimeError("JobService was built without a queue")
        return self._queue
