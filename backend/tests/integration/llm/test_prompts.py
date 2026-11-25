"""Prompt-quality checks against recorded real Claude responses.

Every test drives the production code that builds the prompt (extraction, summary, Q&A,
planner); only the LLM is a cassette. Replay is free and offline; recording calls the API:

    TRAVELRAG_LLM_RECORD=1 TRAVELRAG_TEST_DATABASE_URL=... uv run pytest tests/integration/llm

See docs/testing.md ("Prompt checks with recorded responses").
"""

import json
import re
import uuid
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx
import pytest

from tests.fakes import FakeML
from tests.integration.llm.cassette import FIXTURES, CassetteLLM
from travelrag.core.db import get_sessionmaker
from travelrag.demo.hanoi import VIDEOS
from travelrag.demo.offline import DemoGeocoder
from travelrag.extraction.service import ChunkView, extract_mentions
from travelrag.geo import opening_hours
from travelrag.geo.adapters.routing import StraightLineRouter
from travelrag.places.factory import build_places_service
from travelrag.planning.domain import Trip, TripItem, TripPreferences
from travelrag.planning.factory import build_planner
from travelrag.planning.service import Alternative
from travelrag.qa.factory import build_qa_service

Cassette = Callable[[str], CassetteLLM]

# Real ASR + OCR output for scripts/make_test_video.py's two-shop video (2026-11-25):
# note the ASR mistakes ("hàng mảnh", "cà phê đing", "hồ gơm") that OCR partly corrects.
TWO_SHOPS = [
    ChunkView(
        0, 0, 26_900,
        "Hôm nay mình review 2 quán ở Phố Cổ Hà Nội. Quán đầu tiên là bún chả đắc kim ở số 1 "
        "hàng mảnh, bún chả 50.000 một suất, chả nướng thơm nhưng nước chấm hơi ngọt, quán mở "
        "từ 9h sáng đến 9h tối. Quán thứ 2 là cà phê đing trên phố Đinh Tiên Hoàng, cà phê trứng "
        "35.000, view nhìn ra hồ gơm rất đẹp, nên đi buổi tối, mình chấm cà phê đing 5 sao?",
        "BÚN CHẢ ĐẮC KIM | HÀNG MÀNH",
    )
]  # fmt: skip


def fold(text: str | None) -> str:
    from travelrag.core.text import unaccent

    return unaccent(text or "")


def recorded(case: str, task: str) -> list[dict[str, Any]]:
    """Outputs recorded for a case (read from the fixture file; no API call)."""
    path = FIXTURES / f"{case}.json"
    if not path.exists():
        pytest.fail(f"missing fixture {path.name}; run the recording first")
    return [c["output"] for c in json.loads(path.read_text())["calls"] if c["task"] == task]


# --- extraction -----------------------------------------------------------------------------


async def _extract(llm: CassetteLLM, name: str, chunks: list[ChunkView]) -> list[Any]:
    return await extract_mentions(
        llm, user_id=uuid.uuid4(), video_id=uuid.uuid4(), video_name=name, chunks=chunks
    )


async def test_extract_two_shops_from_noisy_asr(cassette: Cassette) -> None:
    mentions = await _extract(cassette("extract-two-shops"), "review_2_quan.mp4", TWO_SHOPS)
    by_name = {fold(m.name): m for m in mentions}
    assert len(mentions) == 2, [m.name for m in mentions]
    bun = next(m for n, m in by_name.items() if "dac kim" in n)
    cafe = next(m for n, m in by_name.items() if "ca phe" in n)
    assert bun.payload["category"] == "restaurant" and cafe.payload["category"] == "cafe"
    assert (bun.payload["price_min"], bun.payload["price_max"], bun.payload["currency"]) == (
        50000,
        50000,
        "VND",
    )
    assert "hang manh" in fold(bun.payload["address_hint"])  # OCR beats the ASR "hàng mảnh"
    hours = opening_hours.parse(bun.payload["opening_hours_osm"])
    assert hours is not None and hours.is_open(2, 10 * 60) and not hours.is_open(2, 22 * 60)
    assert any("ngot" in fold(c) for c in bun.payload["cons"])
    assert cafe.payload["price_min"] == 35000
    assert "toi" in fold(cafe.payload["best_time"])
    assert cafe.payload["recommendation"] == 5  # "chấm 5 sao"


async def test_extract_demo_pho_review(cassette: Cassette) -> None:
    video = next(v for v in VIDEOS if v["key"] == "review_pho")
    chunks = [ChunkView(*c) for c in video["chunks"]]
    mentions = await _extract(cassette("extract-review-pho"), video["name"], chunks)
    names = [fold(m.name) for m in mentions]
    assert len(mentions) == 2, names
    pho = next(m for m in mentions if "lo duc" in fold(m.name))
    bun_bo = next(m for m in mentions if "bun bo" in fold(m.name))
    cons = " ".join(fold(c) for c in pho.payload["cons"])
    assert "xep hang" in cons and ("cho ngoi" in cons or "ghe" in cons)
    assert bun_bo.payload["price_min"] == 65000
    assert (pho.source_start_ms, pho.source_end_ms) == (0, 66_000)  # chunks 0-1 cover the shop


async def test_extract_address_shown_only_on_screen(cassette: Cassette) -> None:
    """Real video: ASR garbles everything and never says the address; the overlay
    "LAN CAN / 39 Hàng Cót" is the only way to put the cafe on the map."""
    data = json.loads((FIXTURES / "inputs" / "lan-can.json").read_text())
    chunks = [ChunkView(**c) for c in data["chunks"]]
    mentions = await _extract(cassette("extract-lan-can"), "Unknown.mp4", chunks)
    [lan_can] = [m for m in mentions if "lan can" in fold(m.name)] or [None]
    assert lan_can is not None, [m.name for m in mentions]
    assert lan_can.payload["category"] == "cafe"
    assert re.search(r"\b39\b.*hang cot", fold(lan_can.payload["address_hint"]))


@pytest.mark.xfail(
    strict=True,
    reason="known prompt issue: extraction invents a 1-5 rating when the video gives none (recorded 2026-11-25: 3, 3, 4)",
)
def test_known_issue_extract_does_not_invent_ratings() -> None:
    """Neither the bún chả nor the demo videos give a score, so recommendation must be null."""
    two = recorded("extract-two-shops", "extract")[0]["mentions"]
    pho = recorded("extract-review-pho", "extract")[0]["mentions"]
    unrated = [m for m in two if "dac kim" in fold(m["name"])] + pho
    assert [m["recommendation"] for m in unrated] == [None] * len(unrated), [
        (m["name"], m["recommendation"]) for m in unrated
    ]


# --- summary --------------------------------------------------------------------------------


async def test_summary_of_conflicting_place(
    client: httpx.AsyncClient, auth_headers: dict[str, str], seeded: uuid.UUID, cassette: Cassette
) -> None:
    llm = cassette("summary-cafe-giang")
    async with get_sessionmaker()() as session:
        service = build_places_service(session, geocoder=DemoGeocoder(), llm=llm)
        entry = next(
            e for e in await service._repo.catalog(seeded) if e.place.name == "Cà phê Giảng"
        )
        card = await service.ensure_summary(entry.place.id, force=True)
    assert card is not None and card.summary_text
    summary = fold(card.summary_text)
    assert "trung" in summary  # egg coffee, from the notes
    assert "obama" not in summary and "bun cha" not in summary  # nothing from other places


@pytest.mark.xfail(
    strict=True,
    reason="known prompt issue: the summary prompt gets no conflicts, so it claims the videos agree",
)
def test_known_issue_summary_does_not_deny_conflicts() -> None:
    """Cà phê Giảng has conflicting opening hours; the summary must not claim agreement."""
    summary = fold(recorded("summary-cafe-giang", "summary")[0]["summary"])
    assert not re.search(
        r"khong co (y kien )?(trai nguoc|mau thuan)|khong mau thuan|deu dong y", summary
    ), summary


# --- Q&A: interpretation + answer -----------------------------------------------------------

QUESTIONS: list[tuple[str, str]] = [
    ("ask-seating", "Người review có nói gì về chỗ ngồi ở quán phở Lò Đúc không?"),
    ("ask-late", "Quán nào mở muộn sau 11 giờ đêm?"),
    ("ask-giang-hours", "Cà phê Giảng mở cửa mấy giờ?"),
    ("ask-cheap-cafe", "Quán cà phê nào dưới 40 nghìn ở Hoàn Kiếm?"),
    ("ask-route", "Từ Văn Miếu tới phở Thìn bao xa?"),
    ("ask-out-of-scope", "Thủ đô của Pháp là gì?"),
    ("ask-plan", "Ngày 2 trong lịch trình đi đâu?"),
]


async def _ask(seeded: uuid.UUID, llm: CassetteLLM, question: str) -> Any:
    ml = FakeML()
    async with get_sessionmaker()() as session:
        qa = build_qa_service(
            session, llm=llm, embedder=ml, reranker=ml, router=StraightLineRouter()
        )
        return await qa.ask(seeded, question)


@pytest.mark.parametrize(("case", "question"), QUESTIONS, ids=[c for c, _ in QUESTIONS])
async def test_ask(
    case: str,
    question: str,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: uuid.UUID,
    cassette: Cassette,
) -> None:
    llm = cassette(case)
    answer = await _ask(seeded, llm, question)
    q = llm.outputs("interpret")[0]
    text = fold(answer.answer)
    if case == "ask-late":
        assert q["intent"] == "filtered_search" and q["open_after"] in ("23:00", "23:01")
        assert "bia hoi" in text and "cong" in text
    elif case == "ask-giang-hours":
        assert any("giang" in fold(n) for n in q["place_names"])
        assert ("22" in text or "10 gio toi" in text) and (
            "23" in text or "11 gio" in text
        )  # both versions
    elif case == "ask-cheap-cafe":
        assert q["intent"] == "filtered_search"
        assert (
            q["category"] == "cafe" and q["price_max"] == 40000 and "hoan kiem" in fold(q["area"])
        )
    elif case == "ask-route":
        assert (
            q["intent"] == "route"
            and "van mieu" in fold(q["route_from"])
            and "pho thin" in fold(q["route_to"])
        )
        assert "km" in text and not llm.outputs("answer")  # distance from the router, not the LLM
    elif case == "ask-out-of-scope":
        assert q["intent"] == "out_of_scope" and answer.sources == []
    elif case == "ask-plan":
        assert q["intent"] == "plan_qa"
    elif case == "ask-seating":
        assert any("lo duc" in fold(n) for n in q["place_names"])
        assert "ghe" in text or "cho ngoi" in text
    if llm.outputs("answer"):
        cited = llm.outputs("answer")[0]["cited"]
        assert cited and all(re.fullmatch(r"S\d+", c) for c in cited)


def test_detail_questions_are_not_filtered_search() -> None:
    """Questions about what a video said (or about one named place) must reach the video
    transcripts: detail_qa / place_lookup, not filtered_search. (Seen misclassified in a manual
    run on 2026-11-25 but not in this recording: the issue is intermittent.)"""
    intents = {
        case: recorded(case, "interpret")[0]["intent"]
        for case in ("ask-seating", "ask-giang-hours")
    }
    assert all(i in ("detail_qa", "place_lookup") for i in intents.values()), intents


# --- planner: notes and replacement pick -----------------------------------------------------

NOTE_STOPS = [
    ("Văn Miếu - Quốc Tử Giám", 8 * 60),
    ("Bún chả Hương Liên", 11 * 60),
    ("Cà phê Giảng", 13 * 60),
    ("Đền Ngọc Sơn", 15 * 60),
]


def _numbers(text: str) -> set[str]:
    return {n.replace(".", "").replace(",", "") for n in re.findall(r"\d[\d.,]*\d|\d", text)}


async def test_trip_notes_stay_within_the_reviews(
    client: httpx.AsyncClient, auth_headers: dict[str, str], seeded: uuid.UUID, cassette: Cassette
) -> None:
    llm = cassette("trip-notes")
    async with get_sessionmaker()() as session:
        planner = build_planner(session, router=StraightLineRouter(), llm=llm)
        entries = await planner.entries(seeded)
        by_name = {e.place.name: e for e in entries.values()}
        trip = Trip(uuid.uuid4(), seeded, "t", "Hà Nội", date(2026, 11, 24), date(2026, 11, 24),
                    TripPreferences(priorities=["food", "culture"]))  # fmt: skip
        trip.items = [
            TripItem(uuid.uuid4(), 0, i, by_name[name].place.id, start, start + 60)
            for i, (name, start) in enumerate(NOTE_STOPS)
        ]
        await planner._write_notes(trip, entries)
    prompt = llm.calls[0]["prompt"]
    notes = llm.outputs("trip_notes")[0]["notes"]
    assert len(notes) == len(NOTE_STOPS) and all(n["note"].strip() for n in notes)
    assert all(
        item.note and item.note != by_name[n].place.category.value
        for item, (n, _) in zip(trip.items, NOTE_STOPS, strict=True)
    )
    invented = {n for note in notes for n in _numbers(note["note"]) if len(n) >= 4} - _numbers(
        prompt
    )
    assert not invented, f"numbers not in the review notes: {invented}"


async def test_replacement_pick_is_one_of_the_options(
    client: httpx.AsyncClient, auth_headers: dict[str, str], seeded: uuid.UUID, cassette: Cassette
) -> None:
    llm = cassette("replace-pick")
    async with get_sessionmaker()() as session:
        planner = build_planner(session, router=StraightLineRouter(), llm=llm)
        entries = await planner.entries(seeded)
        by_name = {e.place.name: e for e in entries.values()}
        trip = Trip(uuid.uuid4(), seeded, "t", "Hà Nội", date(2026, 11, 24), date(2026, 11, 24),
                    TripPreferences(priorities=["food"]))  # fmt: skip
        options = [
            Alternative(by_name[n].place.id, n, "cafe", 14 * 60, 14 * 60 + 45, d, s)
            for n, d, s in [
                ("Cộng Cà Phê Tràng Tiền", 900.0, 1.2),
                ("Kem Tràng Tiền", 950.0, 1.1),
                ("Cà phê Giảng", 400.0, 1.4),
            ]
        ]
        chosen = await planner._pick(trip, options, entries)
    assert chosen.place_id in {o.place_id for o in options}
    assert (
        chosen.reason and chosen.reason != "Gần nhất và phù hợp nhất với sở thích của bạn."
    )  # LLM, not fallback


def test_replacement_reason_uses_only_given_facts() -> None:
    reason = fold(recorded("replace-pick", "replace_pick")[0]["reason"])
    assert "noi tieng" not in reason, reason  # "famous" is not in any review note
