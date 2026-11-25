"""Phase 1 end to end: upload a real (tiny) video, run the worker with fake models, and check
places, cards, transcript, review flow and resumption."""

import asyncio
from pathlib import Path
from typing import Any

import httpx

from tests.integration.conftest import World, run_worker_until_idle
from tests.media_samples import make_video
from tests.unit.test_phase1 import extracted
from travelrag.extraction.schema import ExtractionOutput
from travelrag.places.domain import Category

SEGMENTS = [
    (0, 31_000, "Mở màn là phở Thìn Lò Đúc, nước dùng béo, sáu mươi đến bảy lăm nghìn."),
    (31_000, 64_000, "Rồi qua bún chả Hương Liên, bốn mươi nghìn, chỗ Obama từng ăn."),
]


def _extract(*mentions: Any) -> Any:
    return lambda _prompt: ExtractionOutput(mentions=list(mentions))


async def _upload(client: httpx.AsyncClient, headers: dict[str, str], path: Path) -> dict[str, Any]:
    data = await asyncio.to_thread(path.read_bytes)
    files = {"file": (path.name, data, "video/mp4")}
    response = await client.post("/api/videos", files=files, headers=headers)
    assert response.status_code == 202, response.text
    return dict(response.json())


async def _status(
    client: httpx.AsyncClient, headers: dict[str, str], video_id: str
) -> dict[str, Any]:
    return dict((await client.get(f"/api/videos/{video_id}", headers=headers)).json()["latest_job"])


async def test_video_becomes_places_with_cards_and_sources(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, tmp_path: Path
) -> None:
    world.ml.segments = SEGMENTS
    world.ml.ocr = ["PHỞ THÌN 13 LÒ ĐÚC"]
    world.llm.handlers["extract"] = _extract(
        extracted("Phở Thìn Lò Đúc", area="Hai Bà Trưng, Hà Nội", price_min=60000, price_max=75000,
                  currency="VND", opening_hours_osm="Mo-Su 06:00-21:00", pros=["nước dùng béo"],
                  recommendation=5, chunk_numbers=[0]),
        extracted("Bún chả Hương Liên", area="Hai Bà Trưng, Hà Nội", price_min=40000,
                  price_max=40000, currency="VND", chunk_numbers=[1]),
    )  # fmt: skip
    video = await _upload(client, auth_headers, make_video(tmp_path / "an_sap.mp4", seconds=2))
    await run_worker_until_idle()

    job = await _status(client, auth_headers, video["id"])
    assert job["status"] == "done", job
    assert job["progress"] == 100

    transcript = (
        await client.get(f"/api/videos/{video['id']}/transcript", headers=auth_headers)
    ).json()
    assert transcript[0]["text"].startswith("Mở màn là phở Thìn")
    assert "PHỞ THÌN" in transcript[0]["ocr_text"]

    places = {p["name"]: p for p in (await client.get("/api/places", headers=auth_headers)).json()}
    assert set(places) == {"Phở Thìn Lò Đúc", "Bún chả Hương Liên"}
    pho = places["Phở Thìn Lò Đúc"]
    assert pho["lat"] is not None  # matched to the (demo) OSM record
    assert pho["price"] == "60.000–75.000 VND"

    detail = (await client.get(f"/api/places/{pho['id']}", headers=auth_headers)).json()
    assert detail["osm"]["opening_hours"] == "Mo-Su 06:00-21:00"
    assert detail["card"]["opening_hours"]["source"] == "osm"
    [mention] = detail["mentions"]
    assert (mention["video_id"], mention["start_ms"]) == (video["id"], 0)
    assert detail["summary"] is None and detail["summary_stale"]  # no summary LLM scripted


async def test_uncertain_match_waits_for_review_then_resumes(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, tmp_path: Path
) -> None:
    # first video: the place lands on the map
    world.ml.segments = SEGMENTS[1:]
    world.llm.handlers["extract"] = _extract(
        extracted("Bún chả Hương Liên", area="Hai Bà Trưng, Hà Nội", chunk_numbers=[0])
    )
    await _upload(client, auth_headers, make_video(tmp_path / "a.mp4", seconds=1))
    await run_worker_until_idle()

    # second video: similar name, but said to be in another district -> ask the user
    world.llm.handlers["extract"] = _extract(
        extracted("Bún chả Hương", area="Ba Đình, Hà Nội", category=Category.RESTAURANT,
                  pros=["chả nướng thơm"], chunk_numbers=[0])
    )  # fmt: skip
    second = await _upload(client, auth_headers, make_video(tmp_path / "b.mp4", seconds=1.5))
    await run_worker_until_idle()
    assert (await _status(client, auth_headers, second["id"]))["status"] == "needs_review"

    [review] = (await client.get("/api/reviews", headers=auth_headers)).json()
    assert review["name"] == "Bún chả Hương"
    [candidate] = review["candidates"]
    assert candidate["place"]["name"] == "Bún chả Hương Liên"
    assert 0.6 <= candidate["score"] < 0.85

    accepted = await client.post(
        f"/api/reviews/candidates/{candidate['id']}/accept", headers=auth_headers
    )
    assert accepted.status_code == 200
    assert (await client.get("/api/reviews", headers=auth_headers)).json() == []

    await run_worker_until_idle()  # the job was re-enqueued and continues where it stopped
    assert (await _status(client, auth_headers, second["id"]))["status"] == "done"
    places = (await client.get("/api/places", headers=auth_headers)).json()
    [place] = places
    assert place["aliases"] == ["Bún chả Hương"]
    assert place["videos"] == 2
    assert world.llm.tasks().count("extract") == 2  # not re-extracted on resume


async def test_rejecting_candidates_creates_a_new_place(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, tmp_path: Path
) -> None:
    world.ml.segments = SEGMENTS[1:]
    world.llm.handlers["extract"] = _extract(
        extracted("Bún chả Hương Liên", area="Hai Bà Trưng, Hà Nội", chunk_numbers=[0])
    )
    await _upload(client, auth_headers, make_video(tmp_path / "a.mp4", seconds=1))
    await run_worker_until_idle()
    world.llm.handlers["extract"] = _extract(
        extracted("Bún chả Hương", area="Ba Đình, Hà Nội", chunk_numbers=[0])
    )
    second = await _upload(client, auth_headers, make_video(tmp_path / "b.mp4", seconds=1.5))
    await run_worker_until_idle()

    [review] = (await client.get("/api/reviews", headers=auth_headers)).json()
    created = await client.post(
        f"/api/reviews/mentions/{review['mention_id']}/reject", headers=auth_headers
    )
    assert created.json()["name"] == "Bún chả Hương"
    await run_worker_until_idle()
    assert (await _status(client, auth_headers, second["id"]))["status"] == "done"
    names = sorted(
        p["name"] for p in (await client.get("/api/places", headers=auth_headers)).json()
    )
    assert names == ["Bún chả Hương", "Bún chả Hương Liên"]

    # a place unknown to OSM can be pinned by hand
    new = next(
        p
        for p in (await client.get("/api/places", headers=auth_headers)).json()
        if p["name"] == "Bún chả Hương"
    )
    pinned = await client.patch(
        f"/api/places/{new['id']}/location",
        json={"lat": 21.03, "lng": 105.84},
        headers=auth_headers,
    )
    assert (pinned.json()["lat"], pinned.json()["lng"]) == (21.03, 105.84)


async def test_seeded_demo_merges_names_and_flags_conflicts(
    client: httpx.AsyncClient, auth_headers: dict[str, str], seeded: object
) -> None:
    places = {p["name"]: p for p in (await client.get("/api/places", headers=auth_headers)).json()}
    assert len(places) == 16
    assert set(places["Phở Thìn Lò Đúc"]["aliases"]) == {"quán phở bò tái lăn Lò Đúc"}
    assert places["Phở Thìn Lò Đúc"]["videos"] == 2
    conflicted = [name for name, p in places.items() if p["has_conflicts"]]
    assert conflicted == ["Cà phê Giảng"]
    giang = (
        await client.get(f"/api/places/{places['Cà phê Giảng']['id']}", headers=auth_headers)
    ).json()
    [conflict] = giang["conflicts"]
    assert [v["value"] for v in conflict["values"]] == ["Mo-Su 07:00-22:00", "Mo-Su 08:00-23:00"]
    assert [v["date"] for v in conflict["values"]] == ["2026-10-01", "2026-11-01"]
