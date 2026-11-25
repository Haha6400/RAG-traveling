import uuid
from contextlib import suppress
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from travelrag.api.deps import CurrentUserDep, SessionDep, get_job_queue
from travelrag.indexing.service import IndexingService
from travelrag.ingestion.adapters.sqlalchemy_repo import SqlAlchemyVideoRepository
from travelrag.jobs.ports import JobQueue
from travelrag.media.adapters.chunk_repo import SqlAlchemyChunkRepository
from travelrag.ml.factory import get_ml_client
from travelrag.ml.ports import MLUnavailableError
from travelrag.places.adapters.sqlalchemy_repo import SqlAlchemyPlaceRepository
from travelrag.places.domain import Place, format_price
from travelrag.places.factory import build_places_service
from travelrag.places.service import CandidateNotFoundError, PlaceNotFoundError, PlacesService

router = APIRouter(tags=["places"])


async def get_places_service(
    session: SessionDep, queue: Annotated[JobQueue, Depends(get_job_queue)]
) -> PlacesService:
    return build_places_service(session, queue=queue)


PlacesDep = Annotated[PlacesService, Depends(get_places_service)]


class PlaceBrief(BaseModel):
    id: uuid.UUID
    name: str
    category: str
    area: str | None
    address: str | None
    lat: float | None
    lng: float | None
    aliases: list[str]
    price: str | None = None
    rating: float | None = None
    videos: int = 0
    has_conflicts: bool = False

    @classmethod
    def build(
        cls, place: Place, aggregated: dict[str, Any] | None, conflicts: list[Any] | None
    ) -> "PlaceBrief":
        agg = aggregated or {}
        return cls(
            id=place.id,
            name=place.name,
            category=place.category.value,
            area=place.area,
            address=place.address,
            lat=place.lat,
            lng=place.lng,
            aliases=place.aliases,
            price=format_price(agg.get("price")),
            rating=(agg.get("recommendation") or {}).get("mean"),
            videos=agg.get("videos") or 0,
            has_conflicts=bool(conflicts),
        )


class MentionOut(BaseModel):
    id: uuid.UUID
    video_id: uuid.UUID
    video_name: str
    video_date: str
    start_ms: int
    end_ms: int
    name: str
    details: dict[str, Any]


class PlaceDetail(BaseModel):
    place: PlaceBrief
    osm: dict[str, Any] | None
    card: dict[str, Any] | None
    summary: str | None
    summary_stale: bool
    conflicts: list[Any]
    mentions: list[MentionOut]


class LocationIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class CandidateOut(BaseModel):
    id: uuid.UUID
    score: float
    place: PlaceBrief | None
    osm: dict[str, Any] | None


class ReviewOut(BaseModel):
    mention_id: uuid.UUID
    video_id: uuid.UUID
    name: str
    area: str | None
    start_ms: int
    details: dict[str, Any]
    candidates: list[CandidateOut]


def _not_found(what: str = "Place") -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found")


@router.get("/places")
async def list_places(user: CurrentUserDep, session: SessionDep) -> list[PlaceBrief]:
    entries = await SqlAlchemyPlaceRepository(session).catalog(user.id)
    return [
        PlaceBrief.build(
            e.place, e.card.aggregated if e.card else None, e.card.conflicts if e.card else None
        )
        for e in entries
    ]


@router.get("/places/{place_id}")
async def get_place(
    place_id: uuid.UUID, user: CurrentUserDep, service: PlacesDep, session: SessionDep
) -> PlaceDetail:
    """The place card. A stale LLM summary is regenerated lazily here (best effort)."""
    try:
        await service.get_view(place_id, user.id)
    except PlaceNotFoundError as exc:
        raise _not_found() from exc
    card = await service.ensure_summary(place_id)
    if card is not None and card.indexed_version != card.version:
        # If ml-service is down, the card is re-indexed by the next pipeline run.
        with suppress(MLUnavailableError):
            await IndexingService(session, get_ml_client()).index_cards(user.id, {place_id})
    view = await service.get_view(place_id, user.id)
    return _detail(view)


@router.post("/places/{place_id}/summary")
async def refresh_summary(
    place_id: uuid.UUID, user: CurrentUserDep, service: PlacesDep
) -> PlaceDetail:
    try:
        await service.get_view(place_id, user.id)
    except PlaceNotFoundError as exc:
        raise _not_found() from exc
    await service.ensure_summary(place_id, force=True)
    return _detail(await service.get_view(place_id, user.id))


@router.patch("/places/{place_id}/location")
async def set_location(
    place_id: uuid.UUID, body: LocationIn, user: CurrentUserDep, service: PlacesDep
) -> PlaceBrief:
    """Pin a place that OSM did not know."""
    try:
        place = await service.set_location(place_id, user.id, body.lat, body.lng)
    except PlaceNotFoundError as exc:
        raise _not_found() from exc
    view = await service.get_view(place.id, user.id)
    return PlaceBrief.build(
        place,
        view.card.aggregated if view.card else None,
        view.card.conflicts if view.card else None,
    )


def _detail(view: Any) -> PlaceDetail:
    card = view.card
    place = view.place
    return PlaceDetail(
        place=PlaceBrief.build(
            place, card.aggregated if card else None, card.conflicts if card else None
        ),
        osm={"type": place.osm_type, "id": place.osm_id, "opening_hours": place.osm_opening_hours}
        if place.osm_id
        else None,
        card=card.aggregated if card else None,
        summary=card.summary_text if card else None,
        summary_stale=card.summary_stale if card else False,
        conflicts=card.conflicts if card else [],
        mentions=[
            MentionOut(
                id=s.mention.id,
                video_id=s.mention.video_id,
                video_name=s.video_name,
                video_date=s.video_date.date().isoformat(),
                start_ms=s.mention.source_start_ms,
                end_ms=s.mention.source_end_ms,
                name=s.mention.name,
                details=s.mention.payload,
            )
            for s in view.sources
        ],
    )


# --- review of uncertain matches ---------------------------------------------------------------


@router.get("/reviews")
async def list_reviews(user: CurrentUserDep, service: PlacesDep) -> list[ReviewOut]:
    """Mentions waiting for "Is this <place>?" confirmation."""
    items = await service.pending_reviews(user.id)
    return [
        ReviewOut(
            mention_id=i.mention.id,
            video_id=i.mention.video_id,
            name=i.mention.name,
            area=i.mention.area,
            start_ms=i.mention.source_start_ms,
            details=i.mention.payload,
            candidates=[
                CandidateOut(
                    id=c.id,
                    score=c.score,
                    place=PlaceBrief.build(p, None, None) if p else None,
                    osm={
                        "name": c.osm.name,
                        "address": c.osm.address,
                        "lat": c.osm.lat,
                        "lng": c.osm.lng,
                        "category": c.osm.category,
                    }
                    if c.osm
                    else None,
                )
                for c, p in i.candidates
            ],
        )
        for i in items
    ]


@router.post("/reviews/candidates/{candidate_id}/accept")
async def accept_candidate(
    candidate_id: uuid.UUID, user: CurrentUserDep, service: PlacesDep
) -> PlaceBrief:
    try:
        place = await service.accept_candidate(candidate_id, user.id)
    except CandidateNotFoundError as exc:
        raise _not_found("Candidate") from exc
    return PlaceBrief.build(place, None, None)


@router.post("/reviews/mentions/{mention_id}/reject")
async def reject_candidates(
    mention_id: uuid.UUID, user: CurrentUserDep, service: PlacesDep
) -> PlaceBrief:
    """None of the suggestions is right: create a new place for this mention."""
    try:
        place = await service.reject_candidates(mention_id, user.id)
    except CandidateNotFoundError as exc:
        raise _not_found("Mention") from exc
    return PlaceBrief.build(place, None, None)


# --- transcript --------------------------------------------------------------------------------


class ChunkOut(BaseModel):
    seq: int
    start_ms: int
    end_ms: int
    text: str
    ocr_text: str | None


@router.get("/videos/{video_id}/transcript")
async def transcript(
    video_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> list[ChunkOut]:
    video = await SqlAlchemyVideoRepository(session).get(video_id)
    if video is None or video.user_id != user.id:
        raise _not_found("Video")
    chunks = await SqlAlchemyChunkRepository(session).for_video(video_id)
    return [
        ChunkOut(seq=c.seq, start_ms=c.start_ms, end_ms=c.end_ms, text=c.text, ocr_text=c.ocr_text)
        for c in chunks
    ]
