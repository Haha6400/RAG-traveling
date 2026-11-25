import uuid
from typing import Any

from travelrag.places.domain import Category, Place
from travelrag.places.ports import CatalogEntry, PlaceCard
from travelrag.qa.domain import Intent, Interpretation, clean_citations, matches_filters, rrf


def interp(**kw: Any) -> Interpretation:
    base: dict[str, Any] = {
        "intent": Intent.FILTERED_SEARCH, "language": "vi", "search_query": "q", "place_names": [],
        "category": None, "area": None, "price_min": None, "price_max": None, "weekday": None,
        "open_at": None, "open_after": None, "route_from": None, "route_to": None,
        "travel_mode": None,
    }  # fmt: skip
    base.update(kw)
    return Interpretation.model_validate(base)


def entry(
    category: Category, area: str, hours: str | None, price: tuple[int, int] | None
) -> CatalogEntry:
    place = Place(uuid.uuid4(), uuid.uuid4(), "x", category, area=area)
    agg = {
        "opening_hours": {"value": hours} if hours else None,
        "price": {"min": price[0], "max": price[1], "currency": "VND"} if price else None,
    }
    return CatalogEntry(place, PlaceCard(place.id, place.user_id, agg, [], "x"))


def test_rrf_rewards_agreement_across_lists() -> None:
    fused = rrf({"dense": ["a", "b", "c"], "sparse": ["b", "c", "a"], "trigram": ["b"]})
    assert fused[0] == "b"
    assert set(fused) == {"a", "b", "c"}
    assert rrf({}) == []


def test_clean_citations_drops_unknown_labels() -> None:
    text, used = clean_citations("Mở 7h [S2], đóng 22h [S9] [S2] và [S1].", {"S1", "S2"})
    assert text == "Mở 7h [S2], đóng 22h [S2] và [S1]."
    assert used == ["S2", "S1"]


def test_filters_category_area_price() -> None:
    cafe = entry(Category.CAFE, "Hoàn Kiếm, Hà Nội", "Mo-Su 07:00-22:00", (30000, 45000))
    assert matches_filters(cafe, interp(category=Category.CAFE, area="Hoàn Kiếm"))
    assert not matches_filters(cafe, interp(category=Category.RESTAURANT))
    assert not matches_filters(cafe, interp(area="Ba Đình"))
    assert matches_filters(cafe, interp(price_max=30000))
    assert not matches_filters(cafe, interp(price_max=20000))
    assert not matches_filters(cafe, interp(price_min=50000))


def test_filters_opening_hours_and_unknowns() -> None:
    bar = entry(Category.BAR, "Hoàn Kiếm", "Mo-Su 16:00-01:00", None)
    museum = entry(Category.ATTRACTION, "Ba Đình", "Tu-Su 08:00-17:00; Mo off", None)
    unknown = entry(Category.CAFE, "Hoàn Kiếm", None, None)
    assert matches_filters(bar, interp(open_after="23:00"))
    assert not matches_filters(museum, interp(open_after="23:00"))
    assert matches_filters(museum, interp(open_at="10:00"))
    assert not matches_filters(museum, interp(open_at="10:00", weekday=0))  # closed Monday
    assert not matches_filters(unknown, interp(open_after="22:00"))  # cannot claim it
    assert not matches_filters(bar, interp(price_max=10))  # unknown price never matches
