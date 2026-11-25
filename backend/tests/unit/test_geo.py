import pytest

from travelrag.core.text import area_matches, normalize_name, unaccent
from travelrag.geo import opening_hours
from travelrag.geo.adapters.routing import StraightLineRouter
from travelrag.geo.domain import LatLng, TravelMode, haversine_m


def test_unaccent_and_normalize() -> None:
    assert unaccent("Phở Thìn Lò Đúc") == "pho thin lo duc"
    assert normalize_name("Quán Phở Thìn") == normalize_name("phở thìn")
    assert normalize_name("Cà phê Giảng") == "giang"
    assert normalize_name("Quán") == "quan"  # never empty
    assert area_matches("Hoàn Kiếm", "Quận Hoàn Kiếm, Hà Nội") is True
    assert area_matches("Ba Đình", "Hoàn Kiếm") is False
    assert area_matches(None, "Hoàn Kiếm") is None


def test_opening_hours_basic_rules() -> None:
    s = opening_hours.parse("Mo-Fr 08:00-17:00; Sa,Su 09:00-12:00,14:00-18:00")
    assert s is not None
    assert s.is_open(0, 8 * 60) and not s.is_open(0, 17 * 60)
    assert s.is_open(5, 15 * 60) and not s.is_open(5, 13 * 60)


def test_opening_hours_past_midnight_and_overrides() -> None:
    s = opening_hours.parse("Mo-Su 18:00-02:00; Mo off")
    assert s is not None
    assert not s.is_open(0, 20 * 60)  # Monday overridden
    assert not s.is_open(1, 1 * 60)  # Tuesday 01:00 would be Monday night, which is off
    assert s.is_open(2, 1 * 60)  # Wednesday 01:00 = Tuesday night
    assert s.open_after(1, 23 * 60)
    assert s.any_day_open_after(22 * 60)


def test_opening_hours_24_7_and_unknown() -> None:
    always = opening_hours.parse("24/7")
    assert always is not None and always.is_open(6, 3 * 60)
    plain = opening_hours.parse("07:00-22:00")
    assert plain is not None and plain.is_open(3, 21 * 60)
    for bad in ["", None, "sunrise-sunset", "Mo-Fr 8h-17h", "Jan-Mar 08:00-12:00"]:
        assert opening_hours.parse(bad) is None


async def test_straight_line_router() -> None:
    a, b = LatLng(21.0285, 105.8542), LatLng(21.0368, 105.8346)  # Hoàn Kiếm -> Ba Đình
    direct = haversine_m(a, b)
    assert 2000 < direct < 2400
    m = await StraightLineRouter().matrix([a, b], TravelMode.MOTORBIKE)
    assert m.source == "estimate"
    assert m.distances_m[0][0] == 0
    assert m.distances_m[0][1] == pytest.approx(direct * 1.35)
    walk = await StraightLineRouter().matrix([a, b], TravelMode.WALK)
    assert walk.durations_s[0][1] > m.durations_s[0][1]
