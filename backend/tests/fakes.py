"""In-memory implementations of the jobs ports, for unit tests."""

import copy
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from travelrag.jobs.domain import Job, JobEvent
from travelrag.jobs.service import JobService


class FakeJobRepository:
    def __init__(self) -> None:
        self.jobs: dict[uuid.UUID, Job] = {}

    async def add(self, job: Job) -> None:
        self.jobs[job.id] = copy.deepcopy(job)

    async def get(self, job_id: uuid.UUID) -> Job | None:
        job = self.jobs.get(job_id)
        return copy.deepcopy(job) if job else None

    async def list_for_user(self, user_id: uuid.UUID, limit: int = 50) -> list[Job]:
        jobs = [j for j in self.jobs.values() if j.user_id == user_id]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)[:limit]

    async def update(self, job: Job) -> None:
        if job.id not in self.jobs:
            raise LookupError(job.id)
        self.jobs[job.id] = copy.deepcopy(job)


class FakeUnitOfWork:
    def __init__(self, publisher: "FakePublisher") -> None:
        self._publisher = publisher
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1
        self._publisher.flush()


class FakePublisher:
    """Mimics NOTIFY: events become visible only after commit."""

    def __init__(self) -> None:
        self._pending: list[JobEvent] = []
        self.delivered: list[JobEvent] = []

    async def publish(self, event: JobEvent) -> None:
        self._pending.append(event)

    def flush(self) -> None:
        self.delivered.extend(self._pending)
        self._pending.clear()


class FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[uuid.UUID] = []

    async def enqueue(self, job: Job) -> None:
        self.enqueued.append(job.id)


class FakeSubscriber:
    def __init__(self, events: list[JobEvent | None]) -> None:
        self._events = events
        self.listening = False

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[JobEvent | None]]:
        self.listening = True

        async def gen() -> AsyncIterator[JobEvent | None]:
            for event in self._events:
                yield event

        yield gen()


class JobHarness:
    def __init__(self) -> None:
        self.repo = FakeJobRepository()
        self.publisher = FakePublisher()
        self.uow = FakeUnitOfWork(self.publisher)
        self.queue = FakeQueue()
        self.service = JobService(self.repo, self.publisher, self.uow, self.queue)
