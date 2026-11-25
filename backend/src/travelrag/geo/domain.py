import math
from dataclasses import dataclass, field
from enum import StrEnum


class TravelMode(StrEnum):
    WALK = "walk"
    MOTORBIKE = "motorbike"
    CAR = "car"


# Door-to-door urban speeds for straight-line estimates (km/h), Hanoi-like traffic.
ESTIMATE_SPEED_KMH = {TravelMode.WALK: 4.5, TravelMode.MOTORBIKE: 20.0, TravelMode.CAR: 16.0}
DETOUR_FACTOR = 1.35  # road distance / straight-line distance


@dataclass(frozen=True)
class LatLng:
    lat: float
    lng: float


@dataclass(frozen=True)
class BBox:
    south: float
    west: float
    north: float
    east: float


def haversine_m(a: LatLng, b: LatLng) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp, dl = p2 - p1, math.radians(b.lng - a.lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


@dataclass(frozen=True)
class TravelMatrix:
    """durations_s[i][j] / distances_m[i][j] between points i and j."""

    durations_s: list[list[float]]
    distances_m: list[list[float]]
    source: str  # "osrm" | "estimate"


@dataclass(frozen=True)
class OsmPlace:
    osm_type: str  # node | way | relation
    osm_id: int
    name: str
    lat: float
    lng: float
    category: str | None
    address: str | None
    opening_hours: str | None
    tags: dict[str, str] = field(default_factory=dict)


# OSM tag -> our category (see extraction.schema.Category)
_OSM_CATEGORY = {
    ("amenity", "restaurant"): "restaurant",
    ("amenity", "fast_food"): "restaurant",
    ("amenity", "food_court"): "restaurant",
    ("amenity", "cafe"): "cafe",
    ("amenity", "bar"): "bar",
    ("amenity", "pub"): "bar",
    ("amenity", "place_of_worship"): "temple",
    ("tourism", "hotel"): "hotel",
    ("tourism", "hostel"): "hotel",
    ("tourism", "guest_house"): "hotel",
    ("tourism", "attraction"): "attraction",
    ("tourism", "museum"): "attraction",
    ("tourism", "viewpoint"): "attraction",
    ("historic", "*"): "attraction",
    ("leisure", "park"): "attraction",
    ("shop", "*"): "shop",
}


def osm_category(key: str | None, value: str | None) -> str | None:
    if key is None:
        return None
    return _OSM_CATEGORY.get((key, value or "")) or _OSM_CATEGORY.get((key, "*"))
