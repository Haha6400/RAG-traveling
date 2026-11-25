"""OsmGeocoder's choice among Nominatim results, on responses recorded from the public API."""

from typing import Any

import pytest

from travelrag.geo.adapters.osm import OsmGeocoder
from travelrag.geo.domain import BBox, LatLng


def _r(category: str, lat: float, lon: float, box: list[float], name: str) -> dict[str, Any]:
    return {
        "category": category, "lat": str(lat), "lon": str(lon),
        "boundingbox": [str(v) for v in box], "display_name": name,
    }  # fmt: skip


RESPONSES: dict[str, list[dict[str, Any]]] = {
    # "Phố cổ" ranks the restaurant "Phở Cồ" first; the Old Quarter comes third
    "Phố cổ, Hà Nội": [
        _r("amenity", 21.0457, 105.7862, [21.0456, 21.0457, 105.7861, 105.7862], "Phở Cồ"),
        _r("amenity", 21.0229, 105.8009, [21.0228, 21.0229, 105.8008, 105.8009], "Phở Cồ"),
        _r("place", 21.0353, 105.8500, [21.0253, 21.0453, 105.8400, 105.8600], "Khu phố cổ"),
    ],
    "Bún bò Huế": [
        _r("amenity", 21.03, 105.85, [21.0299, 21.0301, 105.8499, 105.8501], "Bún bò Huế")
    ],
    # house numbers are rarely mapped: the answer is the street
    "39 Hàng Cót": [
        _r("highway", 21.0376, 105.8471, [21.0366, 21.0387, 105.8471, 105.8471], "Phố Hàng Cót")
    ],
    "Hoàn Kiếm": [
        _r("boundary", 21.03, 105.85, [21.0244, 21.0408, 105.8426, 105.8602], "Phường Hoàn Kiếm")
    ],
}


@pytest.fixture
def geocoder(monkeypatch: pytest.MonkeyPatch) -> OsmGeocoder:
    geo = OsmGeocoder("https://nominatim.invalid", None, "test")
    seen: list[dict[str, Any]] = []

    async def fake_get_json(url: str, params: dict[str, Any]) -> Any:
        seen.append(params)
        return RESPONSES.get(params["q"], [])

    monkeypatch.setattr(geo, "_get_json", fake_get_json)
    geo.seen = seen  # type: ignore[attr-defined]
    return geo


async def test_area_bbox_skips_points_of_interest(geocoder: OsmGeocoder) -> None:
    assert await geocoder.area_bbox("Phố cổ, Hà Nội") == BBox(21.0253, 105.8400, 21.0453, 105.8600)
    assert await geocoder.area_bbox("Bún bò Huế") is None  # only a POI: not an area


async def test_address_resolves_to_street_level_point_inside_the_box(geocoder: OsmGeocoder) -> None:
    hanoi = BBox(20.56, 105.29, 21.39, 106.02)
    assert await geocoder.geocode_address("39 Hàng Cót", hanoi) == LatLng(21.0376, 105.8471)
    assert geocoder.seen[-1]["bounded"] == 1  # type: ignore[attr-defined]


async def test_address_that_only_matches_a_ward_gives_no_point(geocoder: OsmGeocoder) -> None:
    assert await geocoder.geocode_address("Hoàn Kiếm", None) is None
