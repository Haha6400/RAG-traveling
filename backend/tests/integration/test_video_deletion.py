"""Deleting a video must take everything it contributed with it: places only it mentioned,
facts on shared place cards, aliases, summaries, index entries, and their use in Q&A/trips."""

import asyncio
import uuid
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text

from tests.integration.conftest import World, run_worker_until_idle
from tests.media_samples import make_video
from tests.unit.test_phase1 import extracted
from tests.unit.test_qa_domain import interp
from travelrag.core.db import get_sessionmaker
from travelrag.extraction.schema import ExtractionOutput
from travelrag.qa.domain import GeneratedAnswer, Intent


async def videos(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, str]:
    return {
        v["original_name"]: v["id"]
        for v in (await client.get("/api/videos", headers=headers)).json()
    }


async def places(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, dict[str, Any]]:
    return {p["name"]: p for p in (await client.get("/api/places", headers=headers)).json()}


async def card(client: httpx.AsyncClient, headers: dict[str, str], place_id: str) -> dict[str, Any]:
    return dict((await client.get(f"/api/places/{place_id}", headers=headers)).json())


async def test_shared_place_is_recomputed_and_exclusive_place_removed(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, seeded: uuid.UUID
) -> None:
    before = await places(client, auth_headers)
    pho_id = before["Phở Thìn Lò Đúc"]["id"]
    pho_before = await card(client, auth_headers, pho_id)
    assert pho_before["card"]["videos"] == 2
    assert any("ghế nhựa" in c["text"] for c in pho_before["card"]["cons"])
    assert "Bún bò Nam Bộ Bà Đạt" in before  # only in review_pho_ha_noi.mp4

    # give the card a summary that quotes the video about to be deleted
    async with get_sessionmaker()() as session:
        await session.execute(
            text(
                "UPDATE place_cards SET summary_text = 'Chỗ ngồi chật, ghế nhựa thấp.', summary_stale = false WHERE place_id = :p"
            ),
            {"p": pho_id},
        )
        await session.commit()

    video_id = (await videos(client, auth_headers))["review_pho_ha_noi.mp4"]
    assert (await client.delete(f"/api/videos/{video_id}", headers=auth_headers)).status_code == 204

    after = await places(client, auth_headers)
    assert "Bún bò Nam Bộ Bà Đạt" not in after  # no video mentions it any more
    assert len(after) == len(before) - 1
    pho = await card(client, auth_headers, pho_id)
    assert pho["card"]["videos"] == 1
    assert [m["video_name"] for m in pho["mentions"]] == ["an_sap_pho_co_ha_noi.mp4"]
    assert pho["card"]["cons"] == []  # "xếp hàng", "ghế nhựa" came from the deleted video
    assert pho["card"]["price"]["min"] == 60000 and pho["card"]["price"]["videos"] == 1
    assert pho["place"]["aliases"] == []  # "quán phở bò tái lăn Lò Đúc" was only said there
    assert pho["summary"] is None  # the old summary could quote the deleted video

    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                text(
                    "SELECT card_text, version, indexed_version FROM place_cards WHERE place_id = :p"
                ),
                {"p": pho_id},
            )
        ).one()
        chunks = (
            await session.execute(
                text("SELECT count(*) FROM transcript_chunks WHERE video_id = :v"), {"v": video_id}
            )
        ).scalar_one()
    assert "ghế nhựa" not in row.card_text and "bò tái lăn" not in row.card_text
    assert row.indexed_version == row.version  # re-embedded after the change
    assert chunks == 0


async def test_conflict_disappears_with_the_video_that_caused_it(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, seeded: uuid.UUID
) -> None:
    giang_id = (await places(client, auth_headers))["Cà phê Giảng"]["id"]
    assert (await card(client, auth_headers, giang_id))["conflicts"]

    video_id = (await videos(client, auth_headers))["ha_noi_ve_dem.mp4"]
    await client.delete(f"/api/videos/{video_id}", headers=auth_headers)

    giang = await card(client, auth_headers, giang_id)
    assert giang["conflicts"] == []
    assert giang["card"]["opening_hours"]["value"] == "Mo-Su 07:00-22:00"
    after = await places(client, auth_headers)
    for gone in (
        "Bia hơi Tạ Hiện",
        "Chả cá Thăng Long",
        "Kem Tràng Tiền",
        "Hanoi Old Quarter Hotel",
    ):
        assert gone not in after


async def test_qa_and_trips_no_longer_use_deleted_data(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, seeded: uuid.UUID
) -> None:
    trip = (
        await client.post(
            "/api/trips",
            json={
                "start_date": "2026-11-24",
                "end_date": "2026-11-24",
                "preferences": {"priorities": ["nightlife"], "day_end": "23:00"},
            },
            headers=auth_headers,
        )
    ).json()
    assert "Bia hơi Tạ Hiện" in {i["place_name"] for i in trip["items"]}

    video_id = (await videos(client, auth_headers))["ha_noi_ve_dem.mp4"]
    await client.delete(f"/api/videos/{video_id}", headers=auth_headers)

    # the trip loses the stop of the deleted place
    trip_after = (await client.get(f"/api/trips/{trip['id']}", headers=auth_headers)).json()
    assert "Bia hơi Tạ Hiện" not in {i["place_name"] for i in trip_after["items"]}

    # "open late" no longer finds the bar, and no chunk of the video is retrieved
    world.llm.handlers["interpret"] = lambda q: interp(
        intent=Intent.FILTERED_SEARCH, open_after="23:00"
    )
    world.llm.handlers["answer"] = lambda prompt: GeneratedAnswer(
        answer="x", cited=[], insufficient=False
    )
    await client.post("/api/ask", json={"question": "Quán nào mở muộn?"}, headers=auth_headers)
    prompt = world.llm.calls[-1][1]
    assert "Bia hơi" not in prompt and "Cộng Cà Phê" not in prompt

    world.llm.handlers["interpret"] = lambda q: interp(
        intent=Intent.DETAIL_QA, search_query="bia hơi Tạ Hiện ghế nhựa"
    )
    result = (
        await client.post(
            "/api/ask", json={"question": "Bia hơi Tạ Hiện thế nào?"}, headers=auth_headers
        )
    ).json()
    assert all(s["video_name"] != "ha_noi_ve_dem.mp4" for s in result["sources"])


async def test_review_left_without_candidates_becomes_a_new_place(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, tmp_path: Path
) -> None:
    world.ml.segments = [(0, 31_000, "Bún chả Hương Liên ngon.")]
    world.llm.handlers["extract"] = lambda _: ExtractionOutput(
        mentions=[extracted("Bún chả Hương Liên", area="Hai Bà Trưng, Hà Nội", chunk_numbers=[0])]
    )
    first = make_video(tmp_path / "a.mp4", seconds=1)
    data = await asyncio.to_thread(first.read_bytes)
    v1 = (
        await client.post(
            "/api/videos", files={"file": ("a.mp4", data, "video/mp4")}, headers=auth_headers
        )
    ).json()
    await run_worker_until_idle()

    world.llm.handlers["extract"] = lambda _: ExtractionOutput(
        mentions=[extracted("Bún chả Hương", area="Ba Đình, Hà Nội", chunk_numbers=[0])]
    )
    second = make_video(tmp_path / "b.mp4", seconds=1.5)
    data = await asyncio.to_thread(second.read_bytes)
    v2 = (
        await client.post(
            "/api/videos", files={"file": ("b.mp4", data, "video/mp4")}, headers=auth_headers
        )
    ).json()
    await run_worker_until_idle()
    assert len((await client.get("/api/reviews", headers=auth_headers)).json()) == 1

    # the only candidate place disappears with the first video
    await client.delete(f"/api/videos/{v1['id']}", headers=auth_headers)
    assert (await client.get("/api/reviews", headers=auth_headers)).json() == []
    assert set(await places(client, auth_headers)) == {"Bún chả Hương"}
    await run_worker_until_idle()  # the waiting job was re-enqueued
    job = (await client.get(f"/api/videos/{v2['id']}", headers=auth_headers)).json()["latest_job"]
    assert job["status"] == "done"
