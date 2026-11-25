"""Q&A types and deterministic helpers (filters, fusion, citation cleanup). Pure Python."""

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from travelrag.core.text import area_matches
from travelrag.geo import opening_hours
from travelrag.geo.domain import TravelMode
from travelrag.places.domain import Category
from travelrag.places.ports import CatalogEntry


class Intent(StrEnum):
    PLACE_LOOKUP = "place_lookup"
    FILTERED_SEARCH = "filtered_search"
    DETAIL_QA = "detail_qa"
    PLAN_QA = "plan_qa"
    ROUTE = "route"
    OUT_OF_SCOPE = "out_of_scope"


class Interpretation(BaseModel):
    """Structured reading of the question (one LLM call: intent + filters)."""

    intent: Intent = Field(
        description="place_lookup: about one named place; filtered_search: find places by "
        "conditions (area, type, price, hours); detail_qa: details said in the videos; "
        "plan_qa: about the user's itinerary; route: distance/travel time between two "
        "places; out_of_scope: not answerable from the user's own videos, places or trips"
    )
    language: str = Field(description="Language of the question: 'vi' or 'en'")
    search_query: str = Field(description="The question rewritten as a standalone search query")
    place_names: list[str] = Field(description="Place names mentioned in the question")
    category: Category | None = Field(description="Requested place type, if any")
    area: str | None = Field(description="Requested district/city, if any")
    price_min: int | None
    price_max: int | None = Field(description="Upper price limit, as an integer amount")
    weekday: int | None = Field(description="0=Monday..6=Sunday if a day is named")
    open_at: str | None = Field(description="HH:MM the place must be open at, if asked")
    open_after: str | None = Field(
        description="HH:MM the place must still be open after (e.g. 'open late' -> '22:00')"
    )
    route_from: str | None = Field(description="Origin place name for route questions")
    route_to: str | None = Field(description="Destination place name for route questions")
    travel_mode: TravelMode | None


class GeneratedAnswer(BaseModel):
    answer: str = Field(description="The answer, citing sources inline like [S1]")
    cited: list[str] = Field(description="Source labels used, e.g. ['S1', 'S3']")
    insufficient: bool = Field(description="True if the sources do not answer the question")


@dataclass(frozen=True)
class Doc:
    """A retrievable unit: a transcript chunk ('c:<id>') or a place card ('p:<place id>')."""

    key: str
    kind: str  # chunk | card
    text: str
    place_id: uuid.UUID | None = None
    place_name: str | None = None
    video_id: uuid.UUID | None = None
    video_name: str | None = None
    video_date: datetime | None = None
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass(frozen=True)
class Source:
    label: str
    kind: str
    place_id: uuid.UUID | None
    place_name: str | None
    video_id: uuid.UUID | None
    video_name: str | None
    start_ms: int | None
    end_ms: int | None
    snippet: str
    score: float | None = None


@dataclass(frozen=True)
class Answer:
    answer: str
    intent: Intent
    language: str
    sources: list[Source] = field(default_factory=list)
    places: list[dict[str, Any]] = field(default_factory=list)  # [{id, name, lat, lng}]
    no_data: bool = False
    llm_used: bool = True


def rrf(rankings: dict[str, list[str]], k: int = 60) -> list[str]:
    """Reciprocal rank fusion: score(d) = sum over lists of 1 / (k + rank)."""
    scores: dict[str, float] = {}
    for ranked in rankings.values():
        for rank, key in enumerate(ranked, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda key: scores[key], reverse=True)


_LABEL = re.compile(r"\[(S\d+)\]")


def clean_citations(text: str, valid: set[str]) -> tuple[str, list[str]]:
    """Drop citation markers that point at no provided source; return (text, labels used)."""
    used: list[str] = []

    def keep(match: re.Match[str]) -> str:
        label = match.group(1)
        if label in valid:
            if label not in used:
                used.append(label)
            return match.group(0)
        return ""

    cleaned = _LABEL.sub(keep, text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+([.,;:!?])", r"\1", cleaned)  # "[S3] ." -> "[S3]."
    return cleaned.strip(), used


def _minutes(value: str | None) -> int | None:
    if not value:
        return None
    try:
        hours, minutes = value.split(":")
        return int(hours) * 60 + int(minutes)
    except ValueError:
        return None


def matches_filters(entry: CatalogEntry, q: Interpretation) -> bool:
    """Metadata filter applied before any vector search. Unknown values never match a filter
    that needs them (we cannot claim a place is open late if we do not know its hours)."""
    place, agg = entry.place, (entry.card.aggregated if entry.card else {})
    if q.category and place.category is not q.category:
        return False
    if q.area and not area_matches(q.area, place.area or place.address):
        return False
    price = agg.get("price")
    if q.price_max is not None and (not price or price["min"] > q.price_max):
        return False
    if q.price_min is not None and (not price or price["max"] < q.price_min):
        return False
    at, after = _minutes(q.open_at), _minutes(q.open_after)
    if at is not None or after is not None:
        hours = agg.get("opening_hours") or {}
        schedule = opening_hours.parse(hours.get("value") or place.osm_opening_hours)
        if schedule is None:
            return False
        days = [q.weekday] if q.weekday is not None else list(range(7))
        if at is not None and not any(schedule.is_open(d, at) for d in days):
            return False
        if after is not None and not any(schedule.open_after(d, after) for d in days):
            return False
    return True


def has_filters(q: Interpretation) -> bool:
    return any(
        v is not None
        for v in (q.category, q.area, q.price_min, q.price_max, q.open_at, q.open_after)
    )


NO_DATA = {
    "vi": "Mình chưa có dữ liệu về điều này trong các video và địa điểm bạn đã lưu.",
    "en": "I don't have anything about that in your saved videos and places yet.",
}
OUT_OF_SCOPE = {
    "vi": "Câu hỏi này nằm ngoài dữ liệu của bạn (video, địa điểm, lịch trình), nên mình không trả lời được.",
    "en": "That is outside your own data (videos, places, trips), so I can't answer it.",
}
NO_TRIP = {
    "vi": "Bạn chưa có lịch trình nào.",
    "en": "You don't have a trip plan yet.",
}
LLM_MISSING = {
    "vi": "Chưa cấu hình LLM (TRAVELRAG_ANTHROPIC_API_KEY), nên mình chỉ liệt kê các đoạn liên quan nhất:",
    "en": "No LLM is configured (TRAVELRAG_ANTHROPIC_API_KEY); here are the most relevant passages:",
}
