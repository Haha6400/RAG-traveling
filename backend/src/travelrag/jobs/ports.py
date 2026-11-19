import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from travelrag.jobs.domain import Job, JobEvent


class JobRepository(Protocol):
    async def add(self, job: Job) -> None: ...

    async def get(self, job_id: uuid.UUID) -> Job | None: ...

    async def list_for_user(self, user_id: uuid.UUID, limit: int = 50) -> list[Job]: ...

    async def latest_for_videos(self, video_ids: list[uuid.UUID]) -> dict[uuid.UUID, Job]:
        """Most recent job per video id (videos without jobs are absent)."""
        ...

    async def update(self, job: Job) -> None: ...


class JobEventPublisher(Protocol):
    async def publish(self, event: JobEvent) -> None:
        """Queue an event. It must be delivered only once the unit of work commits."""
        ...


class JobEventSubscriber(Protocol):
    def subscribe(self) -> AbstractAsyncContextManager[AsyncIterator[JobEvent | None]]:
        """Start listening on enter. The iterator yields events, or None as a heartbeat."""
        ...


class JobQueue(Protocol):
    async def enqueue(self, job: Job) -> None: ...


class UnitOfWork(Protocol):
    async def commit(self) -> None: ...
