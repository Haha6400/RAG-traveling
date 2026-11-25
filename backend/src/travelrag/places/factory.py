"""Composition root for the places module."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from travelrag.core.config import get_settings
from travelrag.core.db import SessionUnitOfWork
from travelrag.geo.factory import get_geocoder
from travelrag.geo.ports import Geocoder
from travelrag.jobs.factory import build_job_service
from travelrag.jobs.ports import JobQueue
from travelrag.llm.factory import get_llm
from travelrag.llm.ports import StructuredLLM
from travelrag.places.adapters.sqlalchemy_repo import SqlAlchemyPlaceRepository
from travelrag.places.service import PlacesService, ResolveSettings


class JobServiceResumer:
    """JobResumer backed by the jobs module: re-enqueues the video's job in needs_review."""

    def __init__(self, session: AsyncSession, queue: JobQueue) -> None:
        self._jobs = build_job_service(session, queue)

    async def resume_after_review(self, video_id: uuid.UUID) -> None:
        job = (await self._jobs.latest_for_videos([video_id])).get(video_id)
        if job is not None and job.status.value == "needs_review":
            await self._jobs.resume(job.id)


def resolve_settings() -> ResolveSettings:
    s = get_settings()
    return ResolveSettings(s.resolve_auto_merge, s.resolve_ask_user, s.default_region)


def build_places_service(
    session: AsyncSession,
    *,
    queue: JobQueue | None = None,
    geocoder: Geocoder | None = None,
    llm: StructuredLLM | None = None,
) -> PlacesService:
    return PlacesService(
        repo=SqlAlchemyPlaceRepository(session),
        geocoder=geocoder or get_geocoder(),
        llm=llm or get_llm(),
        uow=SessionUnitOfWork(session),
        settings=resolve_settings(),
        resumer=JobServiceResumer(session, queue) if queue is not None else None,
    )
