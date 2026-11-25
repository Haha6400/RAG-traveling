"""Phase 2 against the seeded Hanoi demo: intent routing, metadata filters, hybrid retrieval,
citations, special cases. The LLM is scripted, so these tests pin the pipeline around it."""

import re
import uuid
from typing import Any

import httpx

from tests.integration.conftest import World
from tests.unit.test_qa_domain import interp
from travelrag.places.domain import Category
from travelrag.qa.domain import GeneratedAnswer, Intent


def answer_citing(*labels: str, insufficient: bool = False) -> Any:
    def handler(prompt: str) -> GeneratedAnswer:
        text = "Trả lời " + " ".join(f"[{label}]" for label in labels)
        return GeneratedAnswer(answer=text, cited=list(labels), insufficient=insufficient)

    return handler


async def ask(
    client: httpx.AsyncClient, headers: dict[str, str], question: str, **kw: Any
) -> dict[str, Any]:
    response = await client.post("/api/ask", json={"question": question, **kw}, headers=headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


def sources_text(prompt: str) -> str:
    return prompt.split("Sources:", 1)[1]


async def test_detail_question_cites_the_right_video_segment(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, seeded: uuid.UUID
) -> None:
    world.llm.handlers["interpret"] = lambda q: interp(
        intent=Intent.DETAIL_QA, search_query="chỗ ngồi quán phở Lò Đúc ghế nhựa chật",
        place_names=["phở Lò Đúc"],
    )  # fmt: skip

    def cite_seating(prompt: str) -> GeneratedAnswer:
        # cite whichever source holds the seating remark, plus a label that does not exist
        blocks = re.split(r"\n\n(?=\[S\d+\])", sources_text(prompt))
        label = next(
            re.match(r"\s*\[(S\d+)\]", b).group(1)
            for b in blocks
            if "ghế nhựa thấp" in b and "Video '" in b
        )  # type: ignore[union-attr]
        return GeneratedAnswer(
            answer=f"Chỗ ngồi chật [{label}] [S77].", cited=[label], insufficient=False
        )

    world.llm.handlers["answer"] = cite_seating
    result = await ask(
        client, auth_headers, "Người review có nói gì về chỗ ngồi ở quán phở Lò Đúc không?"
    )

    assert result["intent"] == "detail_qa" and not result["no_data"]
    assert "[S77]" not in result["answer"]
    [source] = result["sources"]
    assert source["video_name"] == "review_pho_ha_noi.mp4"
    assert (source["start_ms"], source["end_ms"]) == (32_000, 66_000)  # the seating segment
    assert source["place_name"] == "Phở Thìn Lò Đúc"
    assert result["places"][0]["name"] == "Phở Thìn Lò Đúc"
    assert result["answer"] == "Chỗ ngồi chật [S3]." or result["answer"].endswith("].")
    # retrieval was scoped to the named place: only its card and its segments reached the LLM
    prompt = sources_text(world.llm.calls[-1][1])
    assert set(re.findall(r"Place card: (.+)", prompt)) == {"Phở Thìn Lò Đúc"}
    assert set(re.findall(r", about (.+)", prompt)) == {"Phở Thìn Lò Đúc"}


async def test_filtered_search_uses_metadata_not_vectors(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, seeded: uuid.UUID
) -> None:
    world.llm.handlers["interpret"] = lambda q: interp(
        intent=Intent.FILTERED_SEARCH, search_query="quán mở muộn", open_after="23:00"
    )
    world.llm.handlers["answer"] = answer_citing("S1", "S2")
    result = await ask(client, auth_headers, "Quán nào mở muộn sau 11 giờ đêm?")

    prompt = sources_text(world.llm.calls[-1][1])
    cards = set(re.findall(r"Place card: (.+)", prompt))
    # 01:00 (bia hơi), 23:30 (Cộng), 23:00 OSM-normalised (Xôi Yến 06:00-23:00 is NOT after 23:00)
    assert cards == {"Bia hơi Tạ Hiện", "Cộng Cà Phê Tràng Tiền"}
    assert result["intent"] == "filtered_search"


async def test_filters_with_no_match_answer_without_llm(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, seeded: uuid.UUID
) -> None:
    world.llm.handlers["interpret"] = lambda q: interp(
        intent=Intent.FILTERED_SEARCH, category=Category.BAR, area="Ba Đình"
    )
    result = await ask(client, auth_headers, "Có quán bar nào ở Ba Đình không?")
    assert result["no_data"] and "chưa có dữ liệu" in result["answer"]
    assert world.llm.tasks() == ["interpret"]  # no generation call: nothing to make up from


async def test_conflicting_sources_reach_the_llm_with_dates(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, seeded: uuid.UUID
) -> None:
    world.llm.handlers["interpret"] = lambda q: interp(
        intent=Intent.PLACE_LOOKUP,
        search_query="cà phê Giảng giờ mở cửa",
        place_names=["cà phê Giảng"],
    )
    world.llm.handlers["answer"] = answer_citing("S1")
    await ask(client, auth_headers, "Cà phê Giảng mở cửa mấy giờ?")
    prompt = sources_text(world.llm.calls[-1][1])
    assert "Mo-Su 07:00-22:00" in prompt and "Mo-Su 08:00-23:00" in prompt
    assert "2026-10-01" in prompt and "2026-11-01" in prompt


async def test_unknown_place_and_out_of_scope(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, seeded: uuid.UUID
) -> None:
    world.llm.handlers["interpret"] = lambda q: interp(
        intent=Intent.PLACE_LOOKUP, search_query="Ginzan Onsen", place_names=["Ginzan Onsen"]
    )
    result = await ask(client, auth_headers, "Ginzan Onsen có gì hay?")
    assert result["no_data"] and result["sources"] == []

    world.llm.handlers["interpret"] = lambda q: interp(intent=Intent.OUT_OF_SCOPE, language="en")
    result = await ask(client, auth_headers, "What is the capital of France?")
    assert result["intent"] == "out_of_scope" and "outside your own data" in result["answer"]


async def test_ask_about_this_place_is_scoped(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, seeded: uuid.UUID
) -> None:
    places = {
        p["name"]: p["id"] for p in (await client.get("/api/places", headers=auth_headers)).json()
    }
    world.llm.handlers["interpret"] = lambda q: interp(
        intent=Intent.DETAIL_QA, search_query="giá bao nhiêu"
    )
    world.llm.handlers["answer"] = answer_citing("S1")
    result = await ask(client, auth_headers, "Giá bao nhiêu?", place_id=places["Chả cá Thăng Long"])
    assert {s["place_name"] for s in result["sources"]} == {"Chả cá Thăng Long"}
    prompt = sources_text(world.llm.calls[-1][1])
    assert "Chả cá Thăng Long" in prompt and "Phở Thìn" not in prompt


async def test_route_questions_use_the_router_not_the_llm(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, seeded: uuid.UUID
) -> None:
    world.llm.handlers["interpret"] = lambda q: interp(
        intent=Intent.ROUTE, route_from="Văn Miếu", route_to="Phở Thìn Lò Đúc"
    )
    result = await ask(client, auth_headers, "Từ Văn Miếu tới phở Thìn bao xa?")
    assert result["intent"] == "route"
    assert re.search(r"khoảng \d+\.\d km, ~\d+ phút xe máy \(ước lượng", result["answer"])
    assert world.llm.tasks() == ["interpret"]


async def test_without_llm_the_relevant_passages_are_listed(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, seeded: uuid.UUID
) -> None:
    result = await ask(client, auth_headers, "cà phê trứng Giảng")  # no handlers at all
    assert not result["llm_used"]
    assert result["answer"].startswith("Chưa cấu hình LLM")
    assert any(s["place_name"] == "Cà phê Giảng" for s in result["sources"])


async def test_insufficient_sources_are_reported_as_no_data(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, seeded: uuid.UUID
) -> None:
    world.llm.handlers["interpret"] = lambda q: interp(
        intent=Intent.DETAIL_QA, search_query="wifi phở Thìn"
    )
    world.llm.handlers["answer"] = answer_citing(insufficient=True)
    result = await ask(client, auth_headers, "Phở Thìn có wifi không?")
    assert result["no_data"] and result["sources"] == []


async def test_other_users_data_is_never_retrieved(
    client: httpx.AsyncClient, auth_headers: dict[str, str], world: World, seeded: uuid.UUID
) -> None:
    creds = {"email": f"other-{uuid.uuid4().hex[:8]}@example.com", "password": "secret-pass"}
    await client.post("/api/auth/register", json=creds)
    token = (await client.post("/api/auth/login", json=creds)).json()["access_token"]
    world.llm.handlers["interpret"] = lambda q: interp(
        intent=Intent.DETAIL_QA, search_query="phở Thìn"
    )
    result = await ask(client, {"Authorization": f"Bearer {token}"}, "phở Thìn ngon không?")
    assert result["no_data"] and result["sources"] == []
