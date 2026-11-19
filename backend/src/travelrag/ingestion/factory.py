"""Composition root for the ingestion module."""

from sqlalchemy.ext.asyncio import AsyncSession

from travelrag.core.config import get_settings
from travelrag.core.db import SessionUnitOfWork
from travelrag.ingestion.adapters.local_storage import LocalVideoStorage
from travelrag.ingestion.adapters.sqlalchemy_repo import SqlAlchemyVideoRepository
from travelrag.ingestion.service import IngestionService
from travelrag.jobs.factory import build_job_service
from travelrag.jobs.ports import JobQueue


def build_video_storage() -> LocalVideoStorage:
    return LocalVideoStorage(get_settings().media_dir)


def build_ingestion_service(
    session: AsyncSession, queue: JobQueue | None = None
) -> IngestionService:
    return IngestionService(
        videos=SqlAlchemyVideoRepository(session),
        storage=build_video_storage(),
        jobs=build_job_service(session, queue),
        uow=SessionUnitOfWork(session),
        max_upload_bytes=get_settings().max_upload_bytes,
    )
