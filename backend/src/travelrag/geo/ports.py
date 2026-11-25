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

    async def geocode_address(self, address: str, bbox: BBox | None) -> LatLng | None:
        """A point for a street address (house or street level), or None. Never a whole
        district or city: a pin there would look precise and be wrong."""
        ...


class RouteProvider(Protocol):
    async def matrix(self, points: list[LatLng], mode: TravelMode) -> TravelMatrix: ...
