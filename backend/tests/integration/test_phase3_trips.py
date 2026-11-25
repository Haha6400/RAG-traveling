"""Phase 3 against the seeded Hanoi demo: the generated itinerary must satisfy every hard
constraint; edits (replace / lock / remove) must keep the rest of the plan intact."""

import uuid
from datetime import date
from typing import Any

import httpx

from tests.integration.conftest import World
from tests.unit.test_qa_domain import interp
from travelrag.geo import opening_hours
from travelrag.planning.domain import MEAL_WINDOWS
from travelrag.qa.domain import GeneratedAnswer, Intent

MONDAY = date(2026, 11, 23)


def hm(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


async def create_trip(
    client: httpx.AsyncClient, headers: dict[str, str], **prefs: Any
) -> dict[str, Any]:
    body = {
        "title": "Hà Nội 2 ngày",
        "area": "Hà Nội",
        "start_date": MONDAY.isoformat(),
        "end_date": date(2026, 11, 24).isoformat(),
        "preferences": {"pace": "balanced", "priorities": ["food", "culture"], **prefs},
    }
    response = await client.post("/api/trips", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return dict(response.json())


async def place_hours(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, str | None]:
    out = {}
    for p in (await client.get("/api/places", headers=headers)).json():
        detail = (await client.get(f"/api/places/{p['id']}", headers=headers)).json()
        out[p["id"]] = ((detail["card"] or {}).get("opening_hours") or {}).get("value")
    return out


def assert_feasible(trip: dict[str, Any], hours: dict[str, str | None]) -> None:
    prefs = trip["preferences"]
    day_start, day_end = hm(prefs["day_start"]), hm(prefs["day_end"])
    by_day: dict[int, list[dict[str, Any]]] = {}
    for item in trip["items"]:
        by_day.setdefault(item["day"], []).append(item)
    for items in by_day.values():
        items.sort(key=lambda i: i["position"])
        weekday = date.fromisoformat(items[0]["date"]).weekday()
        assert len(items) <= 6  # balanced pace
        assert sum(1 for i in items if i["category"] == "restaurant") <= 2
        for prev, item in zip([None, *items], items, strict=False):
            start, end = hm(item["start"]), hm(item["end"])
            assert day_start <= start < end <= day_end, item
            schedule = opening_hours.parse(hours[str(item["place_id"])])
            if schedule is not None:  # open for the whole visit
                assert schedule.is_open(weekday, start) and schedule.is_open(weekday, end - 1), item
            if item["category"] == "restaurant":
                assert any(s <= start and end <= e for s, e in MEAL_WINDOWS), item
            if prev is not None:
                leg = item["travel_from_prev"]
                assert leg is not None and leg["source"] == "estimate"
                assert start >= hm(prev["end"]) + leg["duration_min"], (prev, item)


async def test_generated_itinerary_satisfies_hard_constraints(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, seeded: uuid.UUID
) -> None:
    trip = await create_trip(client, auth_headers)
    assert trip["items"], trip["unscheduled"]
    assert_feasible(trip, await place_hours(client, auth_headers))
    names_by_day = {(i["day"], i["place_name"]) for i in trip["items"]}
    assert (0, "Hoàng thành Thăng Long") not in names_by_day  # closed on Mondays
    assert all(i["category"] != "hotel" for i in trip["items"])
    assert trip["routing_source"] == "estimate"
    assert all(i["note"] for i in trip["items"])  # template notes without an LLM
    assert trip["total_cost_estimate"] == sum(i["cost_estimate"] or 0 for i in trip["items"])


async def test_llm_notes_are_used_but_cannot_change_the_schedule(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, seeded: uuid.UUID
) -> None:
    from travelrag.planning.service import _ItemNote, _Notes

    def notes(prompt: str) -> _Notes:
        import re

        ids = re.findall(r'"item_id": "([0-9a-f-]{36})"', prompt)
        return _Notes(notes=[_ItemNote(item_id=i, note="Ghi chú từ LLM") for i in ids] + [
            _ItemNote(item_id=str(uuid.uuid4()), note="ignored")])  # fmt: skip

    world.llm.handlers["trip_notes"] = notes
    trip = await create_trip(client, auth_headers)
    assert {i["note"] for i in trip["items"]} == {"Ghi chú từ LLM"}


async def test_budget_and_hotel(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, seeded: uuid.UUID
) -> None:
    places = {
        p["name"]: p["id"] for p in (await client.get("/api/places", headers=auth_headers)).json()
    }
    cheap = await create_trip(
        client, auth_headers, budget=200_000, hotel_place_id=places["Hanoi Old Quarter Hotel"]
    )
    per_day: dict[int, int] = {}
    for item in cheap["items"]:
        per_day[item["day"]] = per_day.get(item["day"], 0) + (item["cost_estimate"] or 0)
    assert all(total <= 100_000 for total in per_day.values())
    first = [i for i in cheap["items"] if i["position"] == 0]
    assert all(i["travel_from_prev"] is not None for i in first)  # leg from the hotel

    bad = await client.post(
        "/api/trips",
        json={"start_date": "2026-11-23", "end_date": "2026-11-22"},
        headers=auth_headers,
    )
    assert bad.status_code == 422


async def test_replace_keeps_slot_category_and_other_items(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, seeded: uuid.UUID
) -> None:
    trip = await create_trip(client, auth_headers)
    target = next(i for i in trip["items"] if i["category"] in ("restaurant", "cafe"))
    others = {
        i["id"]: (i["place_id"], i["start"]) for i in trip["items"] if i["id"] != target["id"]
    }

    response = await client.post(
        f"/api/trips/{trip['id']}/items/{target['id']}/replace", json={}, headers=auth_headers
    )
    assert response.status_code in (200, 409), response.text
    if response.status_code == 409:  # nothing of that kind fits nearby: plan unchanged
        return
    result = response.json()
    new_trip = result["trip"]
    replaced = next(i for i in new_trip["items"] if i["id"] == result["replaced_item_id"])
    assert replaced["place_id"] != target["place_id"]
    assert replaced["category"] == target["category"]
    assert replaced["note"]  # reason (template when no LLM)
    assert {
        i["id"]: (i["place_id"], i["start"]) for i in new_trip["items"] if i["id"] != target["id"]
    } == others
    assert_feasible(new_trip, await place_hours(client, auth_headers))
    planned = {i["place_id"] for i in new_trip["items"]}
    assert all(a["place_id"] not in planned for a in result["alternatives"])

    # choosing a specific alternative
    if result["alternatives"]:
        alt = result["alternatives"][0]
        chosen = await client.post(
            f"/api/trips/{trip['id']}/items/{replaced['id']}/replace",
            json={"place_id": alt["place_id"]},
            headers=auth_headers,
        )
        assert chosen.status_code == 200
        item = next(i for i in chosen.json()["trip"]["items"] if i["id"] == replaced["id"])
        assert item["place_id"] == alt["place_id"]


async def test_locked_items_cannot_be_replaced_or_removed(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, seeded: uuid.UUID
) -> None:
    trip = await create_trip(client, auth_headers)
    item = trip["items"][0]
    url = f"/api/trips/{trip['id']}/items/{item['id']}"
    locked = await client.patch(url, json={"locked": True}, headers=auth_headers)
    assert next(i for i in locked.json()["items"] if i["id"] == item["id"])["locked"]
    assert (await client.post(f"{url}/replace", json={}, headers=auth_headers)).status_code == 409
    assert (await client.delete(url, headers=auth_headers)).status_code == 409

    await client.patch(url, json={"locked": False}, headers=auth_headers)
    removed = await client.delete(url, headers=auth_headers)
    assert removed.status_code == 200
    remaining = removed.json()["items"]
    assert item["id"] not in {i["id"] for i in remaining}
    for day in {i["day"] for i in remaining}:
        positions = sorted(i["position"] for i in remaining if i["day"] == day)
        assert positions == list(range(len(positions)))


async def test_plan_questions_read_the_trip_state(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, seeded: uuid.UUID
) -> None:
    world.llm.handlers["interpret"] = lambda q: interp(intent=Intent.PLAN_QA)
    no_trip = await client.post(
        "/api/ask", json={"question": "Ngày 2 đi đâu?"}, headers=auth_headers
    )
    assert no_trip.json()["answer"] == "Bạn chưa có lịch trình nào."

    trip = await create_trip(client, auth_headers)
    world.llm.handlers["plan_answer"] = lambda prompt: GeneratedAnswer(
        answer="ok", cited=[], insufficient=False
    )
    result = (
        await client.post("/api/ask", json={"question": "Ngày 2 đi đâu?"}, headers=auth_headers)
    ).json()
    assert result["intent"] == "plan_qa" and result["answer"] == "ok"
    prompt = world.llm.calls[-1][1]
    assert world.llm.calls[-1][0] == "plan_answer"
    day2 = [i["place_name"] for i in trip["items"] if i["day"] == 1]
    assert all(name in prompt for name in day2)
    assert '"date": "2026-11-24"' in prompt


async def test_trips_are_private_and_deletable(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, seeded: uuid.UUID
) -> None:
    trip = await create_trip(client, auth_headers)
    creds = {"email": f"o-{uuid.uuid4().hex[:8]}@example.com", "password": "secret-pass"}
    await client.post("/api/auth/register", json=creds)
    token = (await client.post("/api/auth/login", json=creds)).json()["access_token"]
    other = {"Authorization": f"Bearer {token}"}
    assert (await client.get(f"/api/trips/{trip['id']}", headers=other)).status_code == 404
    assert (await client.get("/api/trips", headers=auth_headers)).json()[0]["id"] == trip["id"]
    assert (
        await client.delete(f"/api/trips/{trip['id']}", headers=auth_headers)
    ).status_code == 204
    assert (await client.get("/api/trips", headers=auth_headers)).json() == []
