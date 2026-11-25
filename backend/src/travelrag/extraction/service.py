"""LLM extraction of place mentions from a video's chunks."""

import uuid
from dataclasses import dataclass
from typing import Any

from travelrag.core.text import format_timestamp
from travelrag.extraction.schema import ExtractedMention, ExtractionOutput
from travelrag.geo import opening_hours
from travelrag.llm.ports import StructuredLLM
from travelrag.places.domain import Mention

PROMPT_VERSION = "extract-v2"
MIN_CONFIDENCE = 0.3

SYSTEM = """You extract travel places from short review videos (TikTok-style).
You receive the video's transcript split into numbered chunks with timestamps, plus text read
from the screen (OCR), which often shows the place name, address or prices.

Rules:
- One entry per distinct place the reviewer actually talks about (restaurants, cafes, temples,
  hotels, attractions, shops...). Skip places only named in passing with nothing said about them.
- Use only what the video says. Never fill fields from your own knowledge; use null instead.
- Keep the place name in its original language and spelling as said or shown.
- Videos often show the place name with its address on screen (e.g. "PHỞ THÌN" then
  "13 Lò Đúc"). Put such a street address in address_hint; it is how the place is found on
  the map. On-screen lines are separated by " | " and may be OCR-garbled; repeated fragments
  across chunks are the same overlay.
- Prices: integers in the stated currency (e.g. "45 nghìn" -> 45000, currency VND).
- opening_hours_osm: OpenStreetMap syntax only when the stated hours are exact; otherwise null.
- pros / cons: short phrases in the video's language.
- chunk_numbers: every chunk that talks about this place.
- confidence: low if the name is unclear (e.g. ASR garbled it and OCR does not confirm it)."""


@dataclass(frozen=True)
class ChunkView:
    seq: int
    start_ms: int
    end_ms: int
    text: str
    ocr_text: str | None


def render_chunks(chunks: list[ChunkView]) -> str:
    lines = []
    for c in chunks:
        header = f"[{c.seq}] {format_timestamp(c.start_ms)}-{format_timestamp(c.end_ms)}"
        body = c.text or "(no speech)"
        if c.ocr_text:
            body += f"\n    On screen: {c.ocr_text}"
        lines.append(f"{header}\n    {body}")
    return "\n".join(lines)


def _clean(m: ExtractedMention) -> dict[str, Any]:
    payload = m.model_dump(mode="json", exclude={"chunk_numbers", "confidence"})
    if payload["opening_hours_osm"] and opening_hours.parse(payload["opening_hours_osm"]) is None:
        payload["opening_hours_osm"] = None  # keep the raw text, drop an invalid normalization
    rec = payload.get("recommendation")
    if rec is not None:
        payload["recommendation"] = min(5, max(1, int(rec)))
    for key in ("price_min", "price_max"):
        if payload[key] is not None and payload[key] < 0:
            payload[key] = None
    payload["pros"] = [p.strip() for p in payload["pros"] if p.strip()]
    payload["cons"] = [p.strip() for p in payload["cons"] if p.strip()]
    return payload


async def extract_mentions(
    llm: StructuredLLM,
    *,
    user_id: uuid.UUID,
    video_id: uuid.UUID,
    video_name: str,
    chunks: list[ChunkView],
) -> list[Mention]:
    if not chunks:
        return []
    prompt = f"Video file: {video_name}\n\nChunks:\n{render_chunks(chunks)}"
    result = await llm.generate(
        task="extract",
        system=SYSTEM,
        prompt=prompt,
        output=ExtractionOutput,
        effort="medium",
        max_tokens=16000,
    )
    by_seq = {c.seq: c for c in chunks}
    mentions = []
    for m in result.mentions:
        if not m.name.strip() or m.confidence < MIN_CONFIDENCE:
            continue
        cited = [by_seq[n] for n in m.chunk_numbers if n in by_seq] or chunks
        payload = _clean(m)
        payload["prompt_version"] = PROMPT_VERSION
        mentions.append(
            Mention(
                id=uuid.uuid4(),
                user_id=user_id,
                video_id=video_id,
                name=m.name.strip(),
                payload=payload,
                source_start_ms=min(c.start_ms for c in cited),
                source_end_ms=max(c.end_ms for c in cited),
                confidence=max(0.0, min(1.0, m.confidence)),
            )
        )
    return mentions
