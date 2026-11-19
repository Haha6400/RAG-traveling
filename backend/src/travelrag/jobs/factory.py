"""Composition root for the jobs module: wires the service to its Postgres adapters."""

from sqlalchemy.ext.asyncio import AsyncSession

from travelrag.core.db import SessionUnitOfWork
from travelrag.jobs.adapters.pg_events import PgNotifyJobEventPublisher
from travelrag.jobs.adapters.sqlalchemy_repo import SqlAlchemyJobRepository
from travelrag.jobs.ports import JobQueue
from travelrag.jobs.service import JobService


def build_job_service(session: AsyncSession, queue: JobQueue | None = None) -> JobService:
    return JobService(
        repo=SqlAlchemyJobRepository(session),
        events=PgNotifyJobEventPublisher(session),
        uow=SessionUnitOfWork(session),
        queue=queue,
    )
