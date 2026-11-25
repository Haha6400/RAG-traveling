"""Phase 1.2-1.7 rules that need no database: chunking, extraction cleanup, scoring, aggregation,
and the resumable pipeline."""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.fakes import FakeML, ScriptedLLM, StubPlaces
from tests.unit.test_ingestion import GOOD, FakeProber, FakeSource, Harness
from travelrag.extraction.schema import ExtractedMention, ExtractionOutput
from travelrag.extraction.service import ChunkView, extract_mentions
from travelrag.jobs.domain import JobStage, JobStatus
from travelrag.media.chunking import build_chunks
from travelrag.places.domain import (
    Category,
    Mention,
    MentionSource,
    Place,
    aggregate,
    card_text,
    format_price,
    match_score,
)

# --- chunking ----------------------------------------------------------------------------------


def seg(start_s: float, end_s: float, text: str) -> dict[str, object]:
    return {"start_ms": int(start_s * 1000), "end_ms": int(end_s * 1000), "text": text}


def test_chunks_end_at_sentences_within_30_to_60s_and_overlap() -> None:
    segments = [seg(i * 10, i * 10 + 10, f"Câu {i}." if i % 2 else f"phần {i}") for i in range(10)]
    chunks = build_chunks(segments, [])
    assert len(chunks) >= 2
    for c in chunks[:-1]:
        assert 30_000 <= c.end_ms - c.start_ms <= 60_000
        assert c.text.endswith(".") or c.end_ms - c.start_ms == 60_000
    # the boundary segment is repeated at the start of the next chunk
    assert chunks[1].start_ms == chunks[0].end_ms - 10_000
    assert [c.seq for c in chunks] == list(range(len(chunks)))


def test_ocr_text_is_attached_once_per_chunk_window() -> None:
    segments = [seg(0, 35, "Một."), seg(35, 70, "Hai.")]
    frames = [
        {"ts_ms": 1000, "lines": ["PHỞ THÌN"]},
        {"ts_ms": 2000, "lines": ["PHỞ THÌN", "13 LÒ ĐÚC"]},
        {"ts_ms": 50_000, "lines": ["BÚN CHẢ"]},
    ]
    chunks = build_chunks(segments, frames)
    assert chunks[0].ocr_text == "PHỞ THÌN | 13 LÒ ĐÚC"
    assert chunks[1].ocr_text == "BÚN CHẢ"


def test_video_without_speech_gets_ocr_only_chunks() -> None:
    chunks = build_chunks(
        [], [{"ts_ms": 5000, "lines": ["CHÙA"]}, {"ts_ms": 65_000, "lines": ["HỒ"]}]
    )
    assert [(c.start_ms, c.ocr_text, c.text) for c in chunks] == [
        (0, "CHÙA", ""),
        (60_000, "HỒ", ""),
    ]
    assert build_chunks([], []) == []


# --- extraction -----------------------------------------------------------------------------------


def extracted(name: str, **kw: object) -> ExtractedMention:
    base: dict[str, object] = {
        "name": name, "category": Category.RESTAURANT, "area": None, "address_hint": None,
        "price_min": None, "price_max": None, "currency": None, "price_unit": None,
        "opening_hours_text": None, "opening_hours_osm": None, "best_time": None,
        "pros": [], "cons": [], "recommendation": None, "chunk_numbers": [0], "confidence": 0.9,
    }  # fmt: skip
    base.update(kw)
    return ExtractedMention.model_validate(base)


async def test_extraction_cleans_llm_output_and_uses_chunk_times() -> None:
    llm = ScriptedLLM(
        {
            "extract": lambda _: ExtractionOutput(
                mentions=[
                    extracted("Phở Thìn", chunk_numbers=[1, 2], opening_hours_osm="6h-21h",
                              opening_hours_text="6h - 21h", recommendation=9, pros=[" ngon ", ""]),
                    extracted("???", confidence=0.1),
                    extracted("Bún chả", chunk_numbers=[42], price_min=-5),
                ]
            )
        }
    )  # fmt: skip
    chunks = [ChunkView(i, i * 30_000, i * 30_000 + 30_000, f"text {i}", None) for i in range(3)]
    mentions = await extract_mentions(
        llm, user_id=uuid.uuid4(), video_id=uuid.uuid4(), video_name="v.mp4", chunks=chunks
    )
    assert [m.name for m in mentions] == ["Phở Thìn", "Bún chả"]  # low confidence dropped
    pho, bun = mentions
    assert (pho.source_start_ms, pho.source_end_ms) == (30_000, 90_000)
    assert pho.payload["opening_hours_osm"] is None  # not valid OSM syntax -> dropped
    assert pho.payload["opening_hours_text"] == "6h - 21h"
    assert pho.payload["recommendation"] == 5
    assert pho.payload["pros"] == ["ngon"]
    assert (bun.source_start_ms, bun.source_end_ms) == (0, 90_000)  # unknown chunk -> whole video
    assert bun.payload["price_min"] is None
    assert "[1] 00:30-01:00" in llm.calls[0][1]


# --- resolution scoring -----------------------------------------------------------------------


def test_match_score_weights_name_area_and_category() -> None:
    same = match_score("Quán Phở Thìn", "Hai Bà Trưng", Category.RESTAURANT,
                       ["Phở Thìn Lò Đúc"], "Hai Bà Trưng, Hà Nội", "restaurant")  # fmt: skip
    other_area = match_score("Phở Thìn", "Ba Đình", Category.RESTAURANT,
                             ["Phở Thìn"], "Hai Bà Trưng", "restaurant")  # fmt: skip
    different = match_score("Bún chả Hương Liên", None, Category.RESTAURANT,
                            ["Phở Thìn"], None, "restaurant")  # fmt: skip
    assert same >= 0.85
    assert 0.6 <= other_area < 0.85
    assert different < 0.6


# --- aggregation --------------------------------------------------------------------------------


def source(payload: dict[str, object], day: int, name: str = "v.mp4") -> MentionSource:
    mention = Mention(
        uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "x", payload, day * 1000, day * 1000 + 500, 0.9
    )
    return MentionSource(mention, name, datetime(2026, 11, day, tzinfo=UTC))


def test_aggregate_merges_numbers_by_rules_and_flags_conflicts() -> None:
    place = Place(uuid.uuid4(), uuid.uuid4(), "Cà phê Giảng", Category.CAFE)
    sources = [
        source({"price_min": 30000, "price_max": 35000, "currency": "VND",
                "opening_hours_osm": "Mo-Su 07:00-22:00", "pros": ["ngon"], "recommendation": 5}, 1, "a.mp4"),
        source({"price_min": 40000, "price_max": 45000, "opening_hours_osm": "Mo-Su 08:00-23:00",
                "pros": ["Ngon", "rẻ"], "cons": ["đông"], "recommendation": 4}, 9, "b.mp4"),
        source({"opening_hours_text": "mở tới khuya"}, 5),  # free text: shown, never compared
    ]  # fmt: skip
    agg, conflicts = aggregate(place, sources)
    assert agg["price"] == {"min": 30000, "max": 45000, "currency": "VND", "videos": 2}
    assert agg["opening_hours"]["value"] == "Mo-Su 08:00-23:00"  # newest video wins
    assert agg["opening_hours"]["source"] == "video"
    assert [p["text"] for p in agg["pros"]] == ["ngon", "rẻ"]  # case-insensitive dedupe
    assert agg["recommendation"] == {"mean": 4.5, "count": 2}
    assert agg["videos"] == 3
    fields = {c["field"]: c for c in conflicts}
    assert set(fields) == {"price", "opening_hours"}  # 30-35k and 40-45k do not overlap
    assert [v["video_name"] for v in fields["opening_hours"]["values"]] == ["a.mp4", "b.mp4"]
    text = card_text(place, agg, conflicts, "Tóm tắt.")
    assert "Giờ mở cửa: Mo-Su 08:00-23:00" in text and "mâu thuẫn" in text


def test_osm_hours_win_and_count_as_a_source() -> None:
    place = Place(
        uuid.uuid4(), uuid.uuid4(), "X", Category.CAFE, osm_opening_hours="Mo-Su 06:00-21:00"
    )
    agg, conflicts = aggregate(place, [source({"opening_hours_osm": "Mo-Su 06:00-21:00"}, 2)])
    assert agg["opening_hours"]["source"] == "osm" and conflicts == []
    _, conflicts = aggregate(place, [source({"opening_hours_osm": "Mo-Su 07:00-21:00"}, 2)])
    assert conflicts[0]["values"][0] == {"source": "osm", "value": "Mo-Su 06:00-21:00"}


def test_format_price() -> None:
    assert format_price({"min": 30000, "max": 45000, "currency": "VND"}) == "30.000–45.000 VND"
    assert format_price({"min": 500, "max": 500, "currency": "JPY"}) == "500 JPY"
    assert format_price(None) is None


# --- pipeline -----------------------------------------------------------------------------------


async def _job(h: Harness) -> uuid.UUID:
    result = await h.ingestion.ingest(uuid.uuid4(), FakeSource("a.mp4", b"x"))
    assert result.job is not None
    return result.job.id


def _extract_llm() -> ScriptedLLM:
    return ScriptedLLM({"extract": lambda _: ExtractionOutput(mentions=[extracted("Phở Thìn")])})


async def test_pipeline_runs_all_stages_in_order(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    job_id = await _job(h)
    ml = FakeML(segments=[(0, 31_000, "Phở Thìn ngon.")], ocr=["PHỞ THÌN"])
    await h.run(job_id, FakeProber(GOOD), llm=_extract_llm(), ml=ml)

    stages = list(dict.fromkeys(e.stage for e in h.publisher.delivered if e.stage))
    assert stages == [JobStage.PROBE, JobStage.ASR, JobStage.OCR, JobStage.EXTRACT,
                      JobStage.RESOLVE, JobStage.AGGREGATE, JobStage.INDEX]  # fmt: skip
    job = await h.repo.get(job_id)
    assert job is not None and job.status is JobStatus.DONE
    video = await h.videos.get(job.video_id)  # type: ignore[arg-type]
    assert video is not None and video.language == "vi" and video.extracted_at is not None
    assert video.ocr == [
        {"ts_ms": 0, "lines": ["PHỞ THÌN"]},
        {"ts_ms": 40_000, "lines": ["PHỞ THÌN"]},
    ]
    [chunk] = await h.chunk_store.for_video(video.id)
    assert chunk.ocr_text == "PHỞ THÌN" and chunk.text == "Phở Thìn ngon."
    assert [m.name for m in h.places.mentions] == ["Phở Thìn"]
    assert h.indexer.videos == [video.id]


async def test_pipeline_stops_in_review_and_resumes_without_redoing_work(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    job_id = await _job(h)
    llm = _extract_llm()
    ml = FakeML(segments=[(0, 31_000, "Phở Thìn ngon.")])
    await h.run(job_id, FakeProber(GOOD), llm=llm, ml=ml, places=StubPlaces(needs_review=True))
    job = await h.repo.get(job_id)
    assert job is not None and job.status is JobStatus.NEEDS_REVIEW

    # resume after the review: same video state, places now fully resolved
    chunks = h.chunk_store
    places = StubPlaces(needs_review=False)
    h.chunk_store = chunks
    await h.service.resume(job_id)
    await h.run(job_id, FakeProber(GOOD), llm=llm, ml=ml, places=places)
    job = await h.repo.get(job_id)
    assert job is not None and job.status is JobStatus.DONE
    assert llm.tasks() == ["extract"]  # extraction ran once
    assert ml.transcribed == 1  # ASR ran once
    assert places.mentions == []  # nothing re-extracted


async def test_pipeline_fails_clearly_without_llm(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    job_id = await _job(h)
    with pytest.raises(Exception, match="no scripted response"):
        await h.run(
            job_id, FakeProber(GOOD), llm=ScriptedLLM(), ml=FakeML(segments=[(0, 1000, "x")])
        )
