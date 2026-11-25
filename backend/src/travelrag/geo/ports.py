from typing import Protocol

from travelrag.geo.domain import BBox, LatLng, OsmPlace, TravelMatrix, TravelMode


class GeoUnavailableError(Exception):
    pass


class Geocoder(Protocol):
    async def area_bbox(self, area: str) -> BBox | None:
        """Bounding box of a district/city name, or None if unknown."""
        ...

    async def search(self, name: str, bbox: BBox | None, limit: int = 5) -> list[OsmPlace]:
        """POIs whose name resembles `name`, inside `bbox` when given."""
        ...


class RouteProvider(Protocol):
    async def matrix(self, points: list[LatLng], mode: TravelMode) -> TravelMatrix: ...
