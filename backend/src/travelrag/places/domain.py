"""Places, mentions and the deterministic rules around them. Pure Python."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from rapidfuzz import fuzz

from travelrag.core.text import area_matches, normalize_name
from travelrag.geo import opening_hours
from travelrag.geo.domain import OsmPlace


class Category(StrEnum):
    RESTAURANT = "restaurant"
    CAFE = "cafe"
    BAR = "bar"
    TEMPLE = "temple"
    HOTEL = "hotel"
    ATTRACTION = "attraction"
    SHOP = "shop"
    OTHER = "other"


class MentionStatus(StrEnum):
    PENDING = "pending"  # not resolved yet
    REVIEW = "review"  # waiting for the user to confirm a match
    RESOLVED = "resolved"  # attached to a place


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Place:
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    category: Category
    area: str | None = None
    address: str | None = None
    lat: float | None = None
    lng: float | None = None
    osm_type: str | None = None
    osm_id: int | None = None
    osm_opening_hours: str | None = None
    aliases: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    @property
    def has_location(self) -> bool:
        return self.lat is not None and self.lng is not None

    def add_alias(self, name: str) -> None:
        norm = normalize_name(name)
        if norm != normalize_name(self.name) and norm not in {
            normalize_name(a) for a in self.aliases
        }:
            self.aliases.append(name)


@dataclass
class Mention:
    """What one video says about one place. `payload` follows extraction.schema.MentionPayload."""

    id: uuid.UUID
    user_id: uuid.UUID
    video_id: uuid.UUID
    name: str
    payload: dict[str, Any]
    source_start_ms: int
    source_end_ms: int
    confidence: float
    status: MentionStatus = MentionStatus.PENDING
    place_id: uuid.UUID | None = None
    created_at: datetime = field(default_factory=_now)

    @property
    def category(self) -> Category:
        try:
            return Category(self.payload.get("category") or "other")
        except ValueError:
            return Category.OTHER

    @property
    def area(self) -> str | None:
        value = self.payload.get("area")
        return value if isinstance(value, str) else None


@dataclass(frozen=True)
class MentionSource:
    """A mention plus the facts about its video that aggregation needs."""

    mention: Mention
    video_name: str
    video_date: datetime


@dataclass
class MatchCandidate:
    id: uuid.UUID
    user_id: uuid.UUID
    mention_id: uuid.UUID
    score: float
    place_id: uuid.UUID | None = None
    osm: OsmPlace | None = None
    status: str = "pending"


# --- resolution scoring ------------------------------------------------------------------------

NAME_WEIGHT, AREA_WEIGHT, CATEGORY_WEIGHT = 0.6, 0.25, 0.15


def name_similarity(a: str, b: str) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    return max(fuzz.token_set_ratio(na, nb), fuzz.ratio(na, nb)) / 100.0


def match_score(
    name: str,
    area: str | None,
    category: Category,
    candidate_names: list[str],
    candidate_area: str | None,
    candidate_category: str | None,
) -> float:
    """Weighted similarity in [0, 1]. Mentions carry no coordinates, so location agreement is
    judged by area (district/city); OSM candidates are already searched inside the area."""
    name_sim = max(name_similarity(name, c) for c in candidate_names)
    area_ok = area_matches(area, candidate_area)
    area_score = 0.5 if area_ok is None else float(area_ok)
    if candidate_category is None or category is Category.OTHER:
        cat_score = 0.5
    else:
        cat_score = 1.0 if candidate_category == category.value else 0.0
    return NAME_WEIGHT * name_sim + AREA_WEIGHT * area_score + CATEGORY_WEIGHT * cat_score


# --- aggregation -------------------------------------------------------------------------------


def _video_ref(src: MentionSource) -> dict[str, Any]:
    return {
        "video_id": str(src.mention.video_id),
        "video_name": src.video_name,
        "date": src.video_date.date().isoformat(),
        "start_ms": src.mention.source_start_ms,
    }


def aggregate(place: Place, sources: list[MentionSource]) -> tuple[dict[str, Any], list[Any]]:
    """Merge all mentions of a place with fixed rules (no LLM). Returns (aggregated, conflicts).

    - price: overall [min, max]; disjoint ranges are a conflict
    - opening hours: OSM wins; else the newest video. Different values are a conflict.
    - best time / pros / cons: union, deduplicated, with sources
    - recommendation: mean and count
    """
    ordered = sorted(sources, key=lambda s: s.video_date)
    conflicts: list[dict[str, Any]] = []

    prices: list[tuple[MentionSource, int, int]] = []
    for s in ordered:
        lo: Any = s.mention.payload.get("price_min")
        hi: Any = s.mention.payload.get("price_max")
        if lo is None and hi is None:
            continue
        prices.append((s, int(lo if lo is not None else hi), int(hi if hi is not None else lo)))
    price: dict[str, Any] | None = None
    if prices:
        lows = [lo for _, lo, _ in prices]
        highs = [hi for _, _, hi in prices]
        currency = next(
            (
                s.mention.payload.get("currency")
                for s, _, _ in prices
                if s.mention.payload.get("currency")
            ),
            None,
        )
        price = {
            "min": min(lows),
            "max": max(highs),
            "currency": currency or "VND",
            "videos": len(prices),
        }
        if max(lows) > min(highs):  # some ranges do not overlap at all
            conflicts.append(
                {
                    "field": "price",
                    "values": [
                        {**_video_ref(s), "value": f"{lo}-{hi}"}
                        for (s, _, _), lo, hi in zip(prices, lows, highs, strict=True)
                    ],
                }
            )

    hours_values: list[tuple[MentionSource, str]] = []
    for s in ordered:
        value = s.mention.payload.get("opening_hours_osm") or s.mention.payload.get(
            "opening_hours_text"
        )
        if value:
            hours_values.append((s, str(value)))
    hours: dict[str, Any] | None = None
    if place.osm_opening_hours:
        hours = {"value": place.osm_opening_hours, "source": "osm"}
    elif hours_values:
        newest, value = hours_values[-1]
        hours = {"value": value, "source": "video", **_video_ref(newest)}
    # Only machine-comparable values (OSM syntax) can conflict; free text such as "mở tới
    # 23h30" is kept for display but not compared.
    comparable = [
        (s, str(s.mention.payload["opening_hours_osm"]))
        for s in ordered
        if s.mention.payload.get("opening_hours_osm")
    ]
    distinct = {v.replace(" ", "").lower() for _, v in comparable}
    if place.osm_opening_hours:
        distinct.add(place.osm_opening_hours.replace(" ", "").lower())
    if len(distinct) > 1:
        values: list[dict[str, Any]] = [{**_video_ref(s), "value": v} for s, v in comparable]
        if place.osm_opening_hours:
            values.insert(0, {"source": "osm", "value": place.osm_opening_hours})
        conflicts.append({"field": "opening_hours", "values": values})
    if hours is not None:
        hours["parsed"] = opening_hours.parse(hours["value"]) is not None

    def collect(key: str) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for s in ordered:
            raw = s.mention.payload.get(key)
            items = raw if isinstance(raw, list) else [raw] if raw else []
            for item in items:
                text = str(item).strip()
                if text and text.lower() not in seen:
                    seen[text.lower()] = {"text": text, **_video_ref(s)}
        return list(seen.values())

    ratings = [
        s.mention.payload["recommendation"]
        for s in ordered
        if isinstance(s.mention.payload.get("recommendation"), int)
    ]
    aggregated = {
        "name": place.name,
        "category": place.category.value,
        "area": place.area,
        "address": place.address,
        "price": price,
        "opening_hours": hours,
        "best_time": collect("best_time"),
        "pros": collect("pros"),
        "cons": collect("cons"),
        "recommendation": (
            {"mean": round(sum(ratings) / len(ratings), 2), "count": len(ratings)}
            if ratings
            else None
        ),
        "videos": len({s.mention.video_id for s in ordered}),
        "first_seen": ordered[0].video_date.isoformat() if ordered else None,
        "last_seen": ordered[-1].video_date.isoformat() if ordered else None,
    }
    return aggregated, conflicts


def format_price(price: dict[str, Any] | None) -> str | None:
    if not price:
        return None
    lo, hi, cur = price["min"], price["max"], price.get("currency") or ""

    def fmt(value: int) -> str:
        text = f"{value:,}"
        return text.replace(",", ".") if cur == "VND" else text

    return f"{fmt(lo)} {cur}" if lo == hi else f"{fmt(lo)}–{fmt(hi)} {cur}"


def card_text(
    place: Place, aggregated: dict[str, Any], conflicts: list[Any], summary: str | None
) -> str:
    """Plain-text card used for embedding and as LLM context."""
    lines = [f"{place.name} ({place.category.value}{', ' + place.area if place.area else ''})"]
    if place.aliases:
        lines.append("Còn gọi là: " + ", ".join(place.aliases))
    if place.address:
        lines.append(f"Địa chỉ: {place.address}")
    if price := format_price(aggregated.get("price")):
        lines.append(f"Giá: {price}")
    if hours := aggregated.get("opening_hours"):
        lines.append(f"Giờ mở cửa: {hours['value']}")
    for key, label in (("best_time", "Nên đi"), ("pros", "Điểm mạnh"), ("cons", "Điểm yếu")):
        if items := aggregated.get(key):
            lines.append(f"{label}: " + "; ".join(i["text"] for i in items))
    if rec := aggregated.get("recommendation"):
        lines.append(f"Mức khuyên đi: {rec['mean']}/5 ({rec['count']} video)")
    if summary:
        lines.append(f"Tóm tắt: {summary}")
    for c in conflicts:
        parts = [
            f"'{v['value']}' ({v.get('video_name', 'OSM')}, {v.get('date', '')})".replace(
                ", )", ")"
            )
            for v in c["values"]
        ]
        lines.append(f"Thông tin mâu thuẫn về {c['field']}: " + " vs ".join(parts))
    return "\n".join(lines)
