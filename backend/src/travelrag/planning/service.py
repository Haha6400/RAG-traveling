"""Trip planning: candidate selection -> travel matrix -> solver -> LLM notes; replacement."""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel

from travelrag.core.text import area_matches, format_hhmm
from travelrag.geo.domain import LatLng, TravelMatrix, haversine_m
from travelrag.geo.ports import RouteProvider
from travelrag.jobs.ports import UnitOfWork
from travelrag.llm.ports import LLMRefusalError, LLMUnavailableError, StructuredLLM
from travelrag.places.domain import Category, format_price
from travelrag.places.ports import CatalogEntry, PlaceCatalog
from travelrag.planning.domain import (
    FOOD_CATEGORIES,
    MAX_TRIP_DAYS,
    MEAL_SLOTS,
    Trip,
    TripItem,
    TripPreferences,
    cost_estimate,
    fits,
    intersect,
    open_windows,
    preference_score,
    visit_minutes,
)
from travelrag.planning.ports import TripRepository
from travelrag.planning.solver import ItinerarySolver, SolverNode, SolverProblem

logger = logging.getLogger(__name__)

REPLACE_RADIUS_M = 3000
MAX_ALTERNATIVES = 5
BUDGET_UNIT = 1000  # solver works in thousands of the currency


class TripNotFoundError(Exception):
    pass


class TripItemNotFoundError(Exception):
    pass


class TripItemLockedError(Exception):
    pass


class InvalidTripError(Exception):
    pass


class NoReplacementError(Exception):
    pass


@dataclass(frozen=True)
class Alternative:
    place_id: uuid.UUID
    name: str
    category: str
    start_min: int
    end_min: int
    distance_m: float
    score: float
    reason: str | None = None


@dataclass(frozen=True)
class ReplaceResult:
    trip: Trip
    item: TripItem | None
    alternatives: list[Alternative]


class _ItemNote(BaseModel):
    item_id: str
    note: str


class _Notes(BaseModel):
    notes: list[_ItemNote]


class _Pick(BaseModel):
    place_id: str
    reason: str


NOTES_SYSTEM = """You write short notes for a travel itinerary that has already been scheduled.
For each item write 1-2 sentences in Vietnamese: why it is worth the stop (from the review
notes), and one practical tip (best time, price, what to order) if the notes give one.
Use only the information provided. Never change times, order or places; never invent prices
or facts. Return one note per item_id you were given."""

PICK_SYSTEM = """You choose a replacement stop for a travel itinerary. Every option given already
fits the time slot, opening hours and travel time; pick the one that best matches the
traveller's preferences and the review notes, and explain the choice in one Vietnamese sentence.
Answer with the place_id exactly as given."""


def _loc(entry: CatalogEntry) -> LatLng:
    place = entry.place
    if place.lat is None or place.lng is None:
        raise ValueError(f"{place.name} has no location")
    return LatLng(place.lat, place.lng)


def _entry_brief(entry: CatalogEntry) -> dict[str, Any]:
    agg = entry.card.aggregated if entry.card else {}
    return {
        "name": entry.place.name,
        "category": entry.place.category.value,
        "price": format_price(agg.get("price")),
        "rating": (agg.get("recommendation") or {}).get("mean"),
        "pros": [p["text"] for p in agg.get("pros") or []][:4],
        "cons": [c["text"] for c in agg.get("cons") or []][:3],
        "best_time": [b["text"] for b in agg.get("best_time") or []][:2],
        "summary": entry.card.summary_text if entry.card else None,
    }


class PlannerService:
    def __init__(
        self,
        trips: TripRepository,
        catalog: PlaceCatalog,
        router: RouteProvider,
        solver: ItinerarySolver,
        llm: StructuredLLM,
        uow: UnitOfWork,
        time_limit_seconds: float = 3.0,
    ) -> None:
        self._trips = trips
        self._catalog = catalog
        self._router = router
        self._solver = solver
        self._llm = llm
        self._uow = uow
        self._time_limit = time_limit_seconds

    # --- reads ----------------------------------------------------------------------------------

    async def get(self, trip_id: uuid.UUID, user_id: uuid.UUID) -> Trip:
        trip = await self._trips.get(trip_id)
        if trip is None or trip.user_id != user_id:
            raise TripNotFoundError(trip_id)
        return trip

    async def list_trips(self, user_id: uuid.UUID) -> list[Trip]:
        return await self._trips.list_for_user(user_id)

    async def delete(self, trip_id: uuid.UUID, user_id: uuid.UUID) -> None:
        await self.get(trip_id, user_id)
        await self._trips.delete(trip_id)
        await self._uow.commit()

    async def entries(self, user_id: uuid.UUID) -> dict[uuid.UUID, CatalogEntry]:
        return {e.place.id: e for e in await self._catalog.catalog(user_id)}

    # --- create ---------------------------------------------------------------------------------

    async def create(
        self,
        user_id: uuid.UUID,
        *,
        title: str | None,
        area: str | None,
        start_date: date,
        end_date: date,
        preferences: TripPreferences,
    ) -> Trip:
        days = (end_date - start_date).days + 1
        if days < 1 or days > MAX_TRIP_DAYS:
            raise InvalidTripError(f"a trip must last 1-{MAX_TRIP_DAYS} days")
        trip = Trip(
            id=uuid.uuid4(),
            user_id=user_id,
            title=title or f"{area or 'Chuyến đi'} {start_date.isoformat()}",
            area=area,
            start_date=start_date,
            end_date=end_date,
            preferences=preferences,
        )
        entries = await self.entries(user_id)
        hotel = entries.get(preferences.hotel_place_id) if preferences.hotel_place_id else None
        if preferences.hotel_place_id and (hotel is None or not hotel.place.has_location):
            raise InvalidTripError("the hotel must be one of your places, with a location")

        candidates, skipped = self._select(trip, list(entries.values()))
        trip.unscheduled = skipped
        if candidates:
            await self._schedule(trip, candidates, hotel)
        await self._write_notes(trip, entries)
        await self._trips.add(trip)
        await self._uow.commit()
        return trip

    def _select(
        self, trip: Trip, entries: list[CatalogEntry]
    ) -> tuple[list[CatalogEntry], list[dict[str, Any]]]:
        prefs = trip.preferences
        chosen, skipped = [], []
        for e in entries:
            reason = None
            if e.place.category is Category.HOTEL or e.place.id in prefs.exclude_place_ids:
                continue
            if trip.area and area_matches(trip.area, e.place.area or e.place.address) is False:
                continue
            if not e.place.has_location:
                reason = "missing_location"
            elif not any(
                open_windows(e, trip.date_of(d), prefs.day_start_min, prefs.day_end_min)
                for d in range(trip.days)
            ):
                reason = "closed_during_trip"
            if reason:
                skipped.append(
                    {"place_id": str(e.place.id), "name": e.place.name, "reason": reason}
                )
            else:
                chosen.append(e)
        return chosen, skipped

    async def _matrix(self, points: list[LatLng], trip: Trip) -> TravelMatrix:
        return await self._router.matrix(points, trip.preferences.transport)

    async def _schedule(
        self, trip: Trip, candidates: list[CatalogEntry], hotel: CatalogEntry | None
    ) -> None:
        prefs = trip.preferences
        points = [_loc(e) for e in candidates]
        if hotel is not None:
            points.insert(0, _loc(hotel))
        matrix = await self._matrix(points, trip)
        trip.routing_source = matrix.source
        offset = 1 if hotel else 0

        # Solver nodes: one per candidate, except restaurants, which get a lunch and a dinner
        # variant in the same group (at most one is used) so a day never has two lunches.
        nodes: list[SolverNode] = []
        owner: list[int] = []  # solver node -> candidate index
        for ci, e in enumerate(candidates):
            duration = visit_minutes(e.place.category)
            per_day = [
                open_windows(e, trip.date_of(d), prefs.day_start_min, prefs.day_end_min)
                for d in range(trip.days)
            ]
            meals: list[str | None] = (
                list(MEAL_SLOTS) if e.place.category in FOOD_CATEGORIES else [None]
            )
            for meal in meals:
                slot = [MEAL_SLOTS[meal]] if meal else None
                windows = [
                    [
                        (s, e2 - duration)
                        for s, e2 in (intersect(day, slot) if slot else day)
                        if e2 - duration >= s
                    ]
                    for day in per_day
                ]
                nodes.append(
                    SolverNode(
                        key=str(e.place.id),
                        service_min=duration,
                        windows=windows,
                        penalty=int(1000 * preference_score(e, prefs)),
                        meal=meal,
                        cost=(cost_estimate(e, prefs.currency) or 0) // BUDGET_UNIT,
                    )
                )
                owner.append(ci)

        n = len(nodes)
        travel = [[0] * (n + 1) for _ in range(n + 1)]
        for i in range(n + 1):
            for j in range(n + 1):
                if i == j or ((i == 0 or j == 0) and hotel is None):
                    continue
                mi = 0 if i == 0 else owner[i - 1] + offset
                mj = 0 if j == 0 else owner[j - 1] + offset
                travel[i][j] = round(matrix.durations_s[mi][mj] / 60)

        problem = SolverProblem(
            days=trip.days,
            day_start=prefs.day_start_min,
            day_end=prefs.day_end_min,
            nodes=nodes,
            travel_min=travel,
            buffer_min=prefs.rules.buffer_min,
            max_stops=prefs.rules.max_stops,
            budget_per_day=(prefs.budget // trip.days // BUDGET_UNIT)
            if prefs.budget is not None
            else None,
            time_limit_seconds=self._time_limit,
        )
        solution = self._solver.solve(problem)

        last_by_day: dict[int, int] = {}
        scheduled: set[int] = set()
        for stop in solution.stops:
            ci = owner[stop.node]
            entry = candidates[ci]
            prev = last_by_day.get(stop.day)
            leg = None
            if prev is not None:
                leg = self._leg(matrix, prev + offset, ci + offset)
            elif hotel is not None:
                leg = self._leg(matrix, 0, ci + offset)
            last_by_day[stop.day] = ci
            scheduled.add(ci)
            trip.items.append(
                TripItem(
                    id=uuid.uuid4(),
                    day=stop.day,
                    position=len(trip.day_items(stop.day)),
                    place_id=entry.place.id,
                    start_min=stop.start_min,
                    end_min=stop.start_min + nodes[stop.node].service_min,
                    travel_from_prev=leg,
                    cost_estimate=cost_estimate(entry, prefs.currency),
                )
            )
        trip.unscheduled += [
            {"place_id": str(c.place.id), "name": c.place.name, "reason": "did_not_fit"}
            for ci, c in enumerate(candidates)
            if ci not in scheduled
        ]

    @staticmethod
    def _leg(matrix: TravelMatrix, i: int, j: int) -> dict[str, Any]:
        return {
            "distance_m": round(matrix.distances_m[i][j]),
            "duration_min": round(matrix.durations_s[i][j] / 60),
            "source": matrix.source,
        }

    async def _write_notes(self, trip: Trip, entries: dict[uuid.UUID, CatalogEntry]) -> None:
        if not trip.items:
            return
        payload = [
            {
                "item_id": str(item.id),
                "day": item.day + 1,
                "time": f"{format_hhmm(item.start_min)}-{format_hhmm(item.end_min)}",
                **_entry_brief(entries[item.place_id]),
            }
            for item in sorted(trip.items, key=lambda i: (i.day, i.position))
        ]
        prompt = (
            f"Traveller preferences: {trip.preferences.model_dump_json(include={'pace', 'priorities', 'budget', 'currency'})}\n"
            f"Items:\n{json.dumps(payload, ensure_ascii=False, indent=1)}"
        )
        notes: dict[str, str] = {}
        try:
            result = await self._llm.generate(
                task="trip_notes",
                system=NOTES_SYSTEM,
                prompt=prompt,
                output=_Notes,
                effort="low",
                max_tokens=8000,
            )
            notes = {n.item_id: n.note.strip() for n in result.notes}
        except (LLMUnavailableError, LLMRefusalError) as exc:
            logger.info("trip notes from templates: %s", exc)
        for item in trip.items:
            item.note = notes.get(str(item.id)) or self._template_note(entries[item.place_id])

    @staticmethod
    def _template_note(entry: CatalogEntry) -> str:
        brief = _entry_brief(entry)
        parts = []
        if brief["pros"]:
            parts.append("Điểm mạnh: " + ", ".join(brief["pros"][:2]))
        if brief["price"]:
            parts.append(f"Giá: {brief['price']}")
        if brief["best_time"]:
            parts.append("Nên đi: " + brief["best_time"][0])
        return ". ".join(parts) or entry.place.category.value

    # --- edits ----------------------------------------------------------------------------------

    def _find_item(self, trip: Trip, item_id: uuid.UUID) -> TripItem:
        for item in trip.items:
            if item.id == item_id:
                return item
        raise TripItemNotFoundError(item_id)

    async def set_locked(
        self, trip_id: uuid.UUID, item_id: uuid.UUID, user_id: uuid.UUID, locked: bool
    ) -> Trip:
        trip = await self.get(trip_id, user_id)
        self._find_item(trip, item_id).locked = locked
        await self._save(trip)
        return trip

    async def remove_item(self, trip_id: uuid.UUID, item_id: uuid.UUID, user_id: uuid.UUID) -> Trip:
        trip = await self.get(trip_id, user_id)
        item = self._find_item(trip, item_id)
        if item.locked:
            raise TripItemLockedError(item_id)
        trip.items.remove(item)
        await self._relink_day(trip, item.day, await self.entries(user_id))
        await self._save(trip)
        return trip

    async def replace_item(
        self,
        trip_id: uuid.UUID,
        item_id: uuid.UUID,
        user_id: uuid.UUID,
        place_id: uuid.UUID | None = None,
    ) -> ReplaceResult:
        """Swap one stop for a nearby place of the same kind that fits the same slot.

        Candidates are found with filters (category, distance, price, not already planned),
        checked for feasibility (opening hours, travel to/from neighbours, meal windows), and
        only then handed to the LLM to choose and explain. Other stops never move."""
        trip = await self.get(trip_id, user_id)
        item = self._find_item(trip, item_id)
        if item.locked:
            raise TripItemLockedError(item_id)
        entries = await self.entries(user_id)
        old = entries.get(item.place_id)
        day_items = trip.day_items(item.day)
        idx = day_items.index(item)
        prev = day_items[idx - 1] if idx > 0 else None
        nxt = day_items[idx + 1] if idx + 1 < len(day_items) else None
        alternatives = await self._alternatives(trip, item, old, prev, nxt, entries)

        if place_id is not None:
            chosen = next((a for a in alternatives if a.place_id == place_id), None)
            if chosen is None:
                raise NoReplacementError("that place does not fit this slot")
            chosen = Alternative(**{**chosen.__dict__, "reason": "Bạn chọn địa điểm này."})
        elif not alternatives:
            raise NoReplacementError("no nearby place of the same kind fits this time slot")
        else:
            chosen = await self._pick(trip, alternatives, entries)

        entry = entries[chosen.place_id]
        item.place_id = chosen.place_id
        item.start_min, item.end_min = chosen.start_min, chosen.end_min
        item.cost_estimate = cost_estimate(entry, trip.preferences.currency)
        item.note = chosen.reason or self._template_note(entry)
        await self._relink_day(trip, item.day, entries)
        await self._save(trip)
        others = [a for a in alternatives if a.place_id != chosen.place_id]
        return ReplaceResult(trip, item, others)

    async def _alternatives(
        self,
        trip: Trip,
        item: TripItem,
        old: CatalogEntry | None,
        prev: TripItem | None,
        nxt: TripItem | None,
        entries: dict[uuid.UUID, CatalogEntry],
    ) -> list[Alternative]:
        prefs = trip.preferences
        planned = {i.place_id for i in trip.items}
        category = old.place.category if old else None
        anchor = _loc(old) if old and old.place.has_location else None
        old_price = (old.card.aggregated.get("price") if old and old.card else None) or None

        pool = []
        for e in entries.values():
            p = e.place
            if p.id in planned or p.id in prefs.exclude_place_ids or not p.has_location:
                continue
            if category is not None and p.category is not category:
                continue
            distance = haversine_m(anchor, _loc(e)) if anchor else 0.0
            if distance > REPLACE_RADIUS_M:
                continue
            price = (e.card.aggregated.get("price") if e.card else None) or None
            if (
                old_price
                and price
                and (price["min"] > old_price["max"] * 1.5 or price["max"] < old_price["min"] * 0.5)
            ):
                continue
            pool.append((e, distance))
        if not pool:
            return []

        # travel times between the neighbours and every candidate
        neighbours = [entries[i.place_id] for i in (prev, nxt) if i is not None]
        points = [_loc(n) for n in neighbours]
        points += [_loc(e) for e, _ in pool]
        matrix = await self._matrix(points, trip)
        k = len(neighbours)
        buffer = prefs.rules.buffer_min
        on = trip.date_of(item.day)

        out = []
        for c, (entry, distance) in enumerate(pool):
            ci = k + c
            duration = visit_minutes(entry.place.category)
            earliest = prefs.day_start_min
            if prev is not None:
                earliest = prev.end_min + buffer + round(matrix.durations_s[0][ci] / 60)
            latest_end = prefs.day_end_min
            if nxt is not None:
                ni = k - 1
                latest_end = nxt.start_min - buffer - round(matrix.durations_s[ci][ni] / 60)
            windows = open_windows(entry, on, prefs.day_start_min, prefs.day_end_min)
            start = fits(windows, earliest, latest_end, duration)
            if start is None:
                continue
            score = preference_score(entry, prefs) - distance / 10_000
            out.append(
                Alternative(
                    place_id=entry.place.id,
                    name=entry.place.name,
                    category=entry.place.category.value,
                    start_min=start,
                    end_min=start + duration,
                    distance_m=round(distance),
                    score=round(score, 3),
                )
            )
        return sorted(out, key=lambda a: a.score, reverse=True)[:MAX_ALTERNATIVES]

    async def _pick(
        self, trip: Trip, alternatives: list[Alternative], entries: dict[uuid.UUID, CatalogEntry]
    ) -> Alternative:
        options = [
            {
                "place_id": str(a.place_id),
                "distance_m": a.distance_m,
                **_entry_brief(entries[a.place_id]),
            }
            for a in alternatives
        ]
        prompt = (
            f"Preferences: {trip.preferences.model_dump_json(include={'pace', 'priorities', 'budget'})}\n"
            f"Options:\n{json.dumps(options, ensure_ascii=False, indent=1)}"
        )
        try:
            pick = await self._llm.generate(
                task="replace_pick",
                system=PICK_SYSTEM,
                prompt=prompt,
                output=_Pick,
                effort="low",
                max_tokens=1000,
            )
            for a in alternatives:
                if str(a.place_id) == pick.place_id.strip():
                    return Alternative(**{**a.__dict__, "reason": pick.reason.strip()})
            logger.warning("LLM picked an unknown place id %r; using the top score", pick.place_id)
        except (LLMUnavailableError, LLMRefusalError) as exc:
            logger.info("replacement picked by score: %s", exc)
        best = alternatives[0]
        return Alternative(
            **{**best.__dict__, "reason": "Gần nhất và phù hợp nhất với sở thích của bạn."}
        )

    async def _relink_day(
        self, trip: Trip, day: int, entries: dict[uuid.UUID, CatalogEntry]
    ) -> None:
        """Recompute positions and travel legs of one day after an edit (times unchanged)."""
        items = trip.day_items(day)
        for pos, item in enumerate(items):
            item.position = pos
        hotel_id = trip.preferences.hotel_place_id
        hotel = entries.get(hotel_id) if hotel_id else None
        stops = ([hotel] if hotel else []) + [entries[i.place_id] for i in items]
        if len(stops) < 2:
            for item in items:
                item.travel_from_prev = None
            return
        points = [_loc(e) for e in stops]
        matrix = await self._matrix(points, trip)
        trip.routing_source = matrix.source
        offset = 1 if hotel else 0
        for pos, item in enumerate(items):
            if pos == 0 and hotel is None:
                item.travel_from_prev = None
            else:
                item.travel_from_prev = self._leg(matrix, pos - 1 + offset, pos + offset)

    async def _save(self, trip: Trip) -> None:
        trip.updated_at = datetime.now(UTC)
        await self._trips.save(trip)
        await self._uow.commit()
