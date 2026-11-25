from functools import cache

from travelrag.core.config import get_settings
from travelrag.geo.adapters.osm import OsmGeocoder
from travelrag.geo.adapters.routing import OsrmRouter, StraightLineRouter
from travelrag.geo.ports import Geocoder, RouteProvider


@cache
def get_geocoder() -> Geocoder:
    s = get_settings()
    return OsmGeocoder(s.nominatim_url, s.overpass_url, s.geo_user_agent)


@cache
def get_router() -> RouteProvider:
    s = get_settings()
    return OsrmRouter(s.osrm_url) if s.osrm_url else StraightLineRouter()
