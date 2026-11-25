"""Turn ASR segments and OCR frames into timestamped chunks for extraction and retrieval."""

from dataclasses import dataclass
from typing import Any

MIN_CHUNK_MS = 30_000
MAX_CHUNK_MS = 60_000
OCR_ONLY_WINDOW_MS = 30_000
_SENTENCE_END = (".", "!", "?", "…", "。")


@dataclass(frozen=True)
class ChunkDraft:
    seq: int
    start_ms: int
    end_ms: int
    text: str
    ocr_text: str | None


def _ocr_between(frames: list[dict[str, Any]], start_ms: int, end_ms: int) -> str | None:
    seen: dict[str, None] = {}
    for frame in frames:
        if start_ms <= frame["ts_ms"] < end_ms:
            for line in frame["lines"]:
                seen.setdefault(line.strip(), None)
    text = " | ".join(line for line in seen if line)
    return text or None


def build_chunks(segments: list[dict[str, Any]], frames: list[dict[str, Any]]) -> list[ChunkDraft]:
    """Group segments into ~30-60 s chunks ending at sentence boundaries. The last segment of a
    chunk is repeated at the start of the next one, so a sentence split across the boundary is
    still retrievable as a whole. Without speech, OCR text alone forms 30 s chunks."""
    if not segments:
        return _ocr_only_chunks(frames)

    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for seg in segments:
        current.append(seg)
        span = current[-1]["end_ms"] - current[0]["start_ms"]
        at_sentence_end = seg["text"].rstrip().endswith(_SENTENCE_END)
        if span >= MAX_CHUNK_MS or (span >= MIN_CHUNK_MS and at_sentence_end):
            groups.append(current)
            current = [seg]  # overlap: carry the boundary segment forward
    if current and (not groups or current != [groups[-1][-1]]):
        groups.append(current)

    chunks = []
    prev_end = 0
    for seq, group in enumerate(groups):
        start, end = group[0]["start_ms"], group[-1]["end_ms"]
        # OCR windows do not overlap even though chunks do: chunk i takes the frames shown
        # between the end of chunk i-1 and its own end (the last one also takes a short tail).
        ocr_end = end if seq + 1 < len(groups) else end + 5_000
        chunks.append(
            ChunkDraft(
                seq=seq,
                start_ms=start,
                end_ms=end,
                text=" ".join(s["text"].strip() for s in group),
                ocr_text=_ocr_between(frames, prev_end, ocr_end),
            )
        )
        prev_end = end
    return chunks


def _ocr_only_chunks(frames: list[dict[str, Any]]) -> list[ChunkDraft]:
    if not frames:
        return []
    last = max(f["ts_ms"] for f in frames)
    chunks: list[ChunkDraft] = []
    for start in range(0, last + 1, OCR_ONLY_WINDOW_MS):
        end = start + OCR_ONLY_WINDOW_MS
        ocr = _ocr_between(frames, start, end)
        if ocr:
            chunks.append(ChunkDraft(len(chunks), start, end, "", ocr))
    return chunks
