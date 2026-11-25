"""Trips and the deterministic planning rules. Pure Python."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from travelrag.geo import opening_hours
from travelrag.geo.domain import TravelMode
from travelrag.places.domain import Category
from travelrag.places.ports import CatalogEntry


class Pace(StrEnum):
    RELAXED = "relaxed"
    BALANCED = "balanced"
    TIGHT = "tight"


class Priority(StrEnum):
    FOOD = "food"
    PHOTO = "photo"
    CULTURE = "culture"
    SHOPPING = "shopping"
    NIGHTLIFE = "nightlife"


@dataclass(frozen=True)
class PaceRules:
    buffer_min: int  # slack added after every visit
    max_stops: int  # per day


PACE_RULES = {
    Pace.RELAXED: PaceRules(buffer_min=30, max_stops=4),
    Pace.BALANCED: PaceRules(buffer_min=15, max_stops=6),
    Pace.TIGHT: PaceRules(buffer_min=5, max_stops=8),
}

VISIT_MINUTES = {
    Category.RESTAURANT: 60,
    Category.CAFE: 45,
    Category.BAR: 60,
    Category.TEMPLE: 40,
    Category.ATTRACTION: 75,
    Category.SHOP: 40,
    Category.OTHER: 45,
}
FOOD_CATEGORIES = {Category.RESTAURANT}
MEAL_SLOTS: dict[str, tuple[int, int]] = {
    "lunch": (11 * 60, 13 * 60 + 30),
    "dinner": (17 * 60 + 30, 20 * 60 + 30),
}
MEAL_WINDOWS: list[tuple[int, int]] = list(MEAL_SLOTS.values())
PRIORITY_CATEGORIES = {
    Priority.FOOD: {Category.RESTAURANT, Category.CAFE},
    Priority.PHOTO: {Category.ATTRACTION, Category.TEMPLE, Category.CAFE},
    Priority.CULTURE: {Category.TEMPLE, Category.ATTRACTION},
    Priority.SHOPPING: {Category.SHOP},
    Priority.NIGHTLIFE: {Category.BAR},
}
MAX_TRIP_DAYS = 14


def _minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


class TripPreferences(BaseModel):
    budget: int | None = Field(default=None, ge=0, description="Total budget for paid visits")
    currency: str = "VND"
    pace: Pace = Pace.BALANCED
    priorities: list[Priority] = Field(default_factory=list)
    transport: TravelMode = TravelMode.MOTORBIKE
    day_start: str = "08:00"
    day_end: str = "21:30"
    hotel_place_id: uuid.UUID | None = None
    exclude_place_ids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("day_start", "day_end")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        try:
            minutes = _minutes(value)
        except ValueError as exc:
            raise ValueError("use HH:MM") from exc
        if not 0 <= minutes <= 24 * 60:
            raise ValueError("use HH:MM")
        return value

    @model_validator(mode="after")
    def _order(self) -> "TripPreferences":
        if self.day_end_min - self.day_start_min < 120:
            raise ValueError("day_end must be at least 2 hours after day_start")
        return self

    @property
    def day_start_min(self) -> int:
        return _minutes(self.day_start)

    @property
    def day_end_min(self) -> int:
        return _minutes(self.day_end)

    @property
    def rules(self) -> PaceRules:
        return PACE_RULES[self.pace]


@dataclass
class TripItem:
    id: uuid.UUID
    day: int
    position: int
    place_id: uuid.UUID
    start_min: int
    end_min: int
    travel_from_prev: dict[str, Any] | None = None
    cost_estimate: int | None = None
    note: str | None = None
    locked: bool = False


@dataclass
class Trip:
    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    area: str | None
    start_date: date
    end_date: date
    preferences: TripPreferences
    routing_source: str = "estimate"
    unscheduled: list[dict[str, Any]] = field(default_factory=list)
    items: list[TripItem] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    def date_of(self, day: int) -> date:
        return self.start_date + timedelta(days=day)

    def day_items(self, day: int) -> list[TripItem]:
        return sorted((i for i in self.items if i.day == day), key=lambda i: i.position)


# --- candidate rules ---------------------------------------------------------------------------


def visit_minutes(category: Category) -> int:
    return VISIT_MINUTES.get(category, 45)


def cost_estimate(entry: CatalogEntry, currency: str) -> int | None:
    price = (entry.card.aggregated.get("price") if entry.card else None) or None
    if not price or (price.get("currency") or currency) != currency:
        return None
    return int((price["min"] + price["max"]) / 2)


def preference_score(entry: CatalogEntry, prefs: TripPreferences) -> float:
    """0..~2: how much this place is worth a slot. Videos' recommendation is the base."""
    rec = (entry.card.aggregated.get("recommendation") if entry.card else None) or {}
    base = float(rec.get("mean", 3.0)) / 5.0
    matched = sum(1 for p in prefs.priorities if entry.place.category in PRIORITY_CATEGORIES[p])
    videos = int((entry.card.aggregated.get("videos") if entry.card else 0) or 0)
    return base + 0.5 * min(matched, 2) + 0.05 * min(videos, 4)


def open_windows(
    entry: CatalogEntry, on: date, day_start: int, day_end: int
) -> list[tuple[int, int]]:
    """Minutes-of-day intervals in which a visit can take place on `on`, clipped to the day.
    Unknown hours are treated as open all day (flagged elsewhere). Restaurants are further
    limited to meal windows."""
    hours = (entry.card.aggregated.get("opening_hours") if entry.card else None) or {}
    schedule = opening_hours.parse(hours.get("value") or entry.place.osm_opening_hours)
    windows = [(day_start, day_end)] if schedule is None else schedule.intervals(on.weekday())
    if entry.place.category in FOOD_CATEGORIES:
        windows = intersect(windows, MEAL_WINDOWS)
    return intersect(windows, [(day_start, day_end)])


def intersect(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out = []
    for s1, e1 in a:
        for s2, e2 in b:
            s, e = max(s1, s2), min(e1, e2)
            if s < e:
                out.append((s, e))
    return sorted(out)


def fits(
    windows: list[tuple[int, int]], earliest: int, latest_end: int, duration: int
) -> int | None:
    """Earliest start >= `earliest` such that [start, start+duration] lies inside one window
    and ends by `latest_end`; None if impossible."""
    for s, e in windows:
        start = max(s, earliest)
        if start + duration <= min(e, latest_end):
            return start
    return None
