import uuid
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from travelrag.api.deps import CurrentUserDep, SessionDep
from travelrag.core.text import format_hhmm
from travelrag.places.domain import format_price
from travelrag.places.ports import CatalogEntry
from travelrag.planning.domain import Trip, TripPreferences
from travelrag.planning.factory import build_planner
from travelrag.planning.service import (
    InvalidTripError,
    NoReplacementError,
    PlannerService,
    TripItemLockedError,
    TripItemNotFoundError,
    TripNotFoundError,
)

router = APIRouter(prefix="/trips", tags=["trips"])


async def get_planner(session: SessionDep) -> PlannerService:
    return build_planner(session)


PlannerDep = Annotated[PlannerService, Depends(get_planner)]


class TripIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    area: str | None = Field(default=None, max_length=200, description="Only places in this area")
    start_date: date
    end_date: date
    preferences: TripPreferences = Field(default_factory=TripPreferences)


class ItemOut(BaseModel):
    id: uuid.UUID
    day: int
    date: date
    position: int
    start: str
    end: str
    place_id: uuid.UUID
    place_name: str
    category: str
    address: str | None
    lat: float | None
    lng: float | None
    price: str | None
    travel_from_prev: dict[str, Any] | None
    cost_estimate: int | None
    note: str | None
    locked: bool


class TripOut(BaseModel):
    id: uuid.UUID
    title: str
    area: str | None
    start_date: date
    end_date: date
    preferences: TripPreferences
    routing_source: str
    total_cost_estimate: int | None
    unscheduled: list[dict[str, Any]]
    items: list[ItemOut]
    created_at: datetime
    updated_at: datetime


class AlternativeOut(BaseModel):
    place_id: uuid.UUID
    name: str
    category: str
    start: str
    end: str
    distance_m: float
    score: float


class ReplaceIn(BaseModel):
    place_id: uuid.UUID | None = Field(default=None, description="Pick a specific alternative")


class ReplaceOut(BaseModel):
    trip: TripOut
    replaced_item_id: uuid.UUID
    reason: str | None
    alternatives: list[AlternativeOut]


class LockIn(BaseModel):
    locked: bool


def _hhmm(minutes: int) -> str:
    return format_hhmm(minutes)


def trip_out(trip: Trip, entries: dict[uuid.UUID, CatalogEntry]) -> TripOut:
    items = []
    for item in sorted(trip.items, key=lambda i: (i.day, i.position)):
        entry = entries.get(item.place_id)
        place = entry.place if entry else None
        agg = entry.card.aggregated if entry and entry.card else {}
        items.append(
            ItemOut(
                id=item.id,
                day=item.day,
                date=trip.date_of(item.day),
                position=item.position,
                start=_hhmm(item.start_min),
                end=_hhmm(item.end_min),
                place_id=item.place_id,
                place_name=place.name if place else "?",
                category=place.category.value if place else "?",
                address=place.address if place else None,
                lat=place.lat if place else None,
                lng=place.lng if place else None,
                price=format_price(agg.get("price")),
                travel_from_prev=item.travel_from_prev,
                cost_estimate=item.cost_estimate,
                note=item.note,
                locked=item.locked,
            )
        )
    costs = [i.cost_estimate for i in trip.items if i.cost_estimate]
    return TripOut(
        id=trip.id,
        title=trip.title,
        area=trip.area,
        start_date=trip.start_date,
        end_date=trip.end_date,
        preferences=trip.preferences,
        routing_source=trip.routing_source,
        total_cost_estimate=sum(costs) if costs else None,
        unscheduled=trip.unscheduled,
        items=items,
        created_at=trip.created_at,
        updated_at=trip.updated_at,
    )


def _not_found(what: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found")


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_trip(body: TripIn, user: CurrentUserDep, planner: PlannerDep) -> TripOut:
    try:
        trip = await planner.create(
            user.id,
            title=body.title,
            area=body.area,
            start_date=body.start_date,
            end_date=body.end_date,
            preferences=body.preferences,
        )
    except InvalidTripError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return trip_out(trip, await planner.entries(user.id))


@router.get("")
async def list_trips(user: CurrentUserDep, planner: PlannerDep) -> list[TripOut]:
    entries = await planner.entries(user.id)
    return [trip_out(t, entries) for t in await planner.list_trips(user.id)]


@router.get("/{trip_id}")
async def get_trip(trip_id: uuid.UUID, user: CurrentUserDep, planner: PlannerDep) -> TripOut:
    try:
        trip = await planner.get(trip_id, user.id)
    except TripNotFoundError as exc:
        raise _not_found("Trip") from exc
    return trip_out(trip, await planner.entries(user.id))


@router.delete("/{trip_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trip(trip_id: uuid.UUID, user: CurrentUserDep, planner: PlannerDep) -> None:
    try:
        await planner.delete(trip_id, user.id)
    except TripNotFoundError as exc:
        raise _not_found("Trip") from exc


@router.post("/{trip_id}/items/{item_id}/replace")
async def replace_item(
    trip_id: uuid.UUID,
    item_id: uuid.UUID,
    body: ReplaceIn,
    user: CurrentUserDep,
    planner: PlannerDep,
) -> ReplaceOut:
    """Delete a stop and get a replacement that fits the same slot."""
    try:
        result = await planner.replace_item(trip_id, item_id, user.id, body.place_id)
    except TripNotFoundError as exc:
        raise _not_found("Trip") from exc
    except TripItemNotFoundError as exc:
        raise _not_found("Item") from exc
    except TripItemLockedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "This stop is locked") from exc
    except NoReplacementError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    assert result.item is not None
    return ReplaceOut(
        trip=trip_out(result.trip, await planner.entries(user.id)),
        replaced_item_id=result.item.id,
        reason=result.item.note,
        alternatives=[
            AlternativeOut(
                place_id=a.place_id,
                name=a.name,
                category=a.category,
                start=_hhmm(a.start_min),
                end=_hhmm(a.end_min),
                distance_m=a.distance_m,
                score=a.score,
            )
            for a in result.alternatives
        ],
    )


@router.delete("/{trip_id}/items/{item_id}")
async def remove_item(
    trip_id: uuid.UUID, item_id: uuid.UUID, user: CurrentUserDep, planner: PlannerDep
) -> TripOut:
    """Remove a stop without replacing it."""
    try:
        trip = await planner.remove_item(trip_id, item_id, user.id)
    except TripNotFoundError as exc:
        raise _not_found("Trip") from exc
    except TripItemNotFoundError as exc:
        raise _not_found("Item") from exc
    except TripItemLockedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "This stop is locked") from exc
    return trip_out(trip, await planner.entries(user.id))


@router.patch("/{trip_id}/items/{item_id}")
async def lock_item(
    trip_id: uuid.UUID, item_id: uuid.UUID, body: LockIn, user: CurrentUserDep, planner: PlannerDep
) -> TripOut:
    try:
        trip = await planner.set_locked(trip_id, item_id, user.id, body.locked)
    except TripNotFoundError as exc:
        raise _not_found("Trip") from exc
    except TripItemNotFoundError as exc:
        raise _not_found("Item") from exc
    return trip_out(trip, await planner.entries(user.id))
