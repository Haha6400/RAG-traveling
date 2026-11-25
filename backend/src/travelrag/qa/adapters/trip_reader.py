"""Plan questions are answered from structured trip state, not from retrieval."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from travelrag.core.text import format_hhmm
from travelrag.places.adapters.sqlalchemy_repo import SqlAlchemyPlaceRepository
from travelrag.planning.adapters.sqlalchemy_repo import SqlAlchemyTripRepository


def _hhmm(minutes: int) -> str:
    return format_hhmm(minutes)


class SqlTripReader:
    def __init__(self, session: AsyncSession) -> None:
        self._trips = SqlAlchemyTripRepository(session)
        self._places = SqlAlchemyPlaceRepository(session)

    async def trip_context(
        self, user_id: uuid.UUID, trip_id: uuid.UUID | None
    ) -> dict[str, Any] | None:
        trip = (
            await self._trips.get(trip_id)
            if trip_id
            else await self._trips.latest_for_user(user_id)
        )
        if trip is None or trip.user_id != user_id:
            return None
        names = {e.place.id: e.place for e in await self._places.catalog(user_id)}
        days = []
        for day in range(trip.days):
            items = []
            for item in trip.day_items(day):
                place = names.get(item.place_id)
                leg = item.travel_from_prev or {}
                items.append(
                    {
                        "time": f"{_hhmm(item.start_min)}-{_hhmm(item.end_min)}",
                        "place": place.name if place else str(item.place_id),
                        "category": place.category.value if place else None,
                        "address": place.address if place else None,
                        "travel_from_previous": (
                            f"{leg['distance_m'] / 1000:.1f} km, {leg['duration_min']} min ({leg['source']})"
                            if leg
                            else None
                        ),
                        "cost_estimate": item.cost_estimate,
                        "note": item.note,
                        "locked": item.locked,
                    }
                )
            days.append({"day": day + 1, "date": trip.date_of(day).isoformat(), "items": items})
        costs = [i.cost_estimate for i in trip.items if i.cost_estimate]
        return {
            "title": trip.title,
            "area": trip.area,
            "start_date": trip.start_date.isoformat(),
            "end_date": trip.end_date.isoformat(),
            "preferences": trip.preferences.model_dump(mode="json", exclude={"exclude_place_ids"}),
            "total_cost_estimate": sum(costs) if costs else None,
            "currency": trip.preferences.currency,
            "days": days,
            "not_scheduled": trip.unscheduled,
        }
