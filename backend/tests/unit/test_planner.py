"""OR-Tools solver on small hand-made problems, plus planning rules."""

import uuid
from datetime import date

from travelrag.places.domain import Category, Place
from travelrag.places.ports import CatalogEntry, PlaceCard
from travelrag.planning.adapters.ortools_solver import OrToolsSolver
from travelrag.planning.domain import (
    MEAL_WINDOWS,
    Priority,
    TripPreferences,
    fits,
    intersect,
    open_windows,
    preference_score,
)
from travelrag.planning.solver import SolverNode, SolverProblem

DAY_START, DAY_END = 8 * 60, 21 * 60


def node(
    key: str, service: int, windows: list[list[tuple[int, int]]], penalty: int = 1000, **kw: object
) -> SolverNode:
    return SolverNode(key=key, service_min=service, windows=windows, penalty=penalty, **kw)  # type: ignore[arg-type]


def problem(
    nodes: list[SolverNode], days: int = 1, travel: int = 10, **kw: object
) -> SolverProblem:
    n = len(nodes)
    matrix = [
        [0 if i == j or i == 0 or j == 0 else travel for j in range(n + 1)] for i in range(n + 1)
    ]
    defaults: dict[str, object] = {
        "buffer_min": 0,
        "max_stops": 8,
        "time_limit_seconds": 1,
    }
    defaults.update(kw)
    return SolverProblem(days=days, day_start=DAY_START, day_end=DAY_END, nodes=nodes,
                         travel_min=matrix, **defaults)  # type: ignore[arg-type]  # fmt: skip


def test_respects_time_windows_and_travel() -> None:
    morning = node("museum", 60, [[(8 * 60, 10 * 60)]])
    evening = node("bar", 60, [[(18 * 60, 20 * 60)]])
    lunch = node("lunch", 60, [[(11 * 60, 12 * 60)]], meal="lunch")
    sol = OrToolsSolver().solve(problem([evening, lunch, morning], travel=20))
    order = [s.node for s in sol.stops]
    assert order == [2, 1, 0] and sol.dropped == []
    by_node = {s.node: s.start_min for s in sol.stops}
    assert 8 * 60 <= by_node[2] <= 10 * 60
    assert 11 * 60 <= by_node[1] <= 12 * 60
    assert by_node[1] >= by_node[2] + 60 + 20  # visit + travel before the next start


def test_closed_days_and_multi_day_split() -> None:
    closed_day0 = node("citadel", 90, [[], [(8 * 60, 15 * 60)]])  # closed on day 0
    sol = OrToolsSolver().solve(problem([closed_day0], days=2))
    assert [(s.day, s.node) for s in sol.stops] == [(1, 0)]


def test_drops_low_value_nodes_when_over_capacity() -> None:
    nodes = [
        node(f"n{i}", 60, [[(DAY_START, DAY_END - 60)]], penalty=100 * (i + 1)) for i in range(5)
    ]
    sol = OrToolsSolver().solve(problem(nodes, max_stops=3))
    assert len(sol.stops) == 3
    assert sorted(sol.dropped) == ["n0", "n1"]  # the two cheapest to leave out


def test_one_lunch_and_one_dinner_per_day() -> None:
    # three restaurants, each offered as a lunch and a dinner variant (same key)
    meals = [
        node(
            f"r{i}",
            45,
            [[(11 * 60, 12 * 60 + 45)]] if slot == "lunch" else [[(17 * 60 + 30, 19 * 60)]],
            meal=slot,
        )
        for i in range(3)
        for slot in ("lunch", "dinner")
    ]
    sol = OrToolsSolver().solve(problem(meals))
    assert sorted(problem(meals).nodes[s.node].meal or "" for s in sol.stops) == ["dinner", "lunch"]
    assert (
        len({problem(meals).nodes[s.node].key for s in sol.stops}) == 2
    )  # never the same place twice


def test_budget_capacity() -> None:
    pricey = [node(f"p{i}", 30, [[(DAY_START, DAY_END - 30)]], cost=100) for i in range(3)]
    sol = OrToolsSolver().solve(problem(pricey, budget_per_day=250))
    assert len(sol.stops) == 2


def test_impossible_node_is_dropped() -> None:
    sol = OrToolsSolver().solve(problem([node("never", 30, [[]])]))
    assert sol.stops == [] and sol.dropped == ["never"]


# --- planning rules ----------------------------------------------------------------------------


def catalog_entry(category: Category, hours: str | None, rec: float | None = None) -> CatalogEntry:
    place = Place(uuid.uuid4(), uuid.uuid4(), "x", category, lat=21.0, lng=105.8)
    agg = {
        "opening_hours": {"value": hours} if hours else None,
        "recommendation": {"mean": rec, "count": 1} if rec else None,
        "videos": 1,
    }
    return CatalogEntry(place, PlaceCard(place.id, place.user_id, agg, [], "x"))


def test_open_windows_restaurants_only_at_meal_times() -> None:
    monday = date(2026, 11, 23)
    resto = catalog_entry(Category.RESTAURANT, "Mo-Su 06:00-21:00")
    assert open_windows(resto, monday, DAY_START, DAY_END) == MEAL_WINDOWS
    museum = catalog_entry(Category.ATTRACTION, "Tu-Su 08:00-17:00; Mo off")
    assert open_windows(museum, monday, DAY_START, DAY_END) == []
    unknown = catalog_entry(Category.CAFE, None)
    assert open_windows(unknown, monday, DAY_START, DAY_END) == [(DAY_START, DAY_END)]
    bar = catalog_entry(Category.BAR, "Mo-Su 16:00-01:00")
    assert open_windows(bar, monday, DAY_START, 22 * 60) == [(16 * 60, 22 * 60)]


def test_fits_and_intersect() -> None:
    assert intersect([(0, 100), (200, 300)], [(50, 250)]) == [(50, 100), (200, 250)]
    windows = [(600, 700), (800, 1000)]
    assert fits(windows, 650, 1000, 60) == 800  # does not fit before 700
    assert fits(windows, 500, 900, 60) == 600
    assert fits(windows, 950, 1000, 60) is None


def test_preference_score_prefers_matches_and_ratings() -> None:
    prefs = TripPreferences(priorities=[Priority.FOOD])
    good_food = catalog_entry(Category.RESTAURANT, None, rec=5)
    temple = catalog_entry(Category.TEMPLE, None, rec=5)
    weak_food = catalog_entry(Category.RESTAURANT, None, rec=2)
    assert preference_score(good_food, prefs) > preference_score(temple, prefs)
    assert preference_score(good_food, prefs) > preference_score(weak_food, prefs)
