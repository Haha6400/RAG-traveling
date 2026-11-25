import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, SmallInteger, String, Text, select
from sqlalchemy.dialects.postgresql import JSONB, distinct_on
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from travelrag.core.db import Base
from travelrag.core.users import UserRow
from travelrag.jobs.domain import Job, JobKind, JobStage, JobStatus


class JobRow(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    # Referencing the column object (not the "users.id" string) guarantees the users table is
    # registered in every process that loads this model, e.g. the worker.
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(UserRow.id, ondelete="CASCADE"))
    # The FK to videos.id (ON DELETE CASCADE) exists in the database (migration 0002) but is not
    # declared here, so the jobs module does not depend on the ingestion module's models.
    video_id: Mapped[uuid.UUID | None]
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    stage: Mapped[str | None] = mapped_column(String(16))
    progress: Mapped[int] = mapped_column(SmallInteger)
    error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def _to_domain(row: JobRow) -> Job:
    return Job(
        id=row.id,
        user_id=row.user_id,
        kind=JobKind(row.kind),
        status=JobStatus(row.status),
        stage=JobStage(row.stage) if row.stage else None,
        progress=row.progress,
        error=row.error,
        payload=dict(row.payload),
        video_id=row.video_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _copy_into(row: JobRow, job: Job) -> None:
    row.user_id = job.user_id
    row.video_id = job.video_id
    row.kind = job.kind.value
    row.status = job.status.value
    row.stage = job.stage.value if job.stage else None
    row.progress = job.progress
    row.error = job.error
    row.payload = job.payload
    row.created_at = job.created_at
    row.updated_at = job.updated_at


class SqlAlchemyJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, job: Job) -> None:
        row = JobRow(id=job.id)
        _copy_into(row, job)
        self._session.add(row)
        await self._session.flush()

    async def get(self, job_id: uuid.UUID) -> Job | None:
        row = await self._session.get(JobRow, job_id, populate_existing=True)
        return _to_domain(row) if row else None

    async def list_for_user(self, user_id: uuid.UUID, limit: int = 50) -> list[Job]:
        result = await self._session.execute(
            select(JobRow)
            .where(JobRow.user_id == user_id)
            .order_by(JobRow.created_at.desc())
            .limit(limit)
        )
        return [_to_domain(row) for row in result.scalars()]

    async def latest_for_videos(self, video_ids: list[uuid.UUID]) -> dict[uuid.UUID, Job]:
        result = await self._session.execute(
            select(JobRow)
            .where(JobRow.video_id.in_(video_ids))
            .order_by(JobRow.video_id, JobRow.created_at.desc())
            .ext(distinct_on(JobRow.video_id))
        )
        return {row.video_id: _to_domain(row) for row in result.scalars() if row.video_id}

    async def update(self, job: Job) -> None:
        row = await self._session.get(JobRow, job.id)
        if row is None:
            raise LookupError(f"job {job.id} does not exist")
        _copy_into(row, job)
        await self._session.flush()
