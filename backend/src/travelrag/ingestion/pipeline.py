"""The `process_video` job.

Stages: probe -> asr -> ocr (+ chunking) -> extract -> resolve -> aggregate -> index.
Every stage is idempotent and skipped when its output already exists, so a retried or
resumed job (e.g. after the user reviewed place matches) continues where it stopped.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from travelrag.extraction.service import ChunkView as ExtractionChunk
from travelrag.extraction.service import extract_mentions
from travelrag.ingestion.domain import Video, metadata_problem
from travelrag.ingestion.ports import VideoRepository, VideoStorage
from travelrag.jobs.domain import JobStage
from travelrag.jobs.ports import UnitOfWork
from travelrag.jobs.service import JobService
from travelrag.llm.ports import StructuredLLM
from travelrag.media.chunking import build_chunks
from travelrag.media.domain import InvalidVideoError
from travelrag.media.ports import ChunkStore, MediaReader, VideoProber
from travelrag.ml.ports import OcrEngine, Transcriber
from travelrag.places.service import PlacesService

OCR_BATCH = 8
OCR_MIN_CONFIDENCE = 0.4


class Indexer(Protocol):
    async def index_video_chunks(self, video_id: uuid.UUID) -> int: ...

    async def index_cards(
        self, user_id: uuid.UUID, place_ids: set[uuid.UUID] | None = None
    ) -> int: ...


@dataclass
class PipelineDeps:
    jobs: JobService
    videos: VideoRepository
    storage: VideoStorage
    uow: UnitOfWork
    prober: VideoProber
    media: MediaReader
    transcriber: Transcriber
    ocr: OcrEngine | None  # None = OCR disabled
    chunks: ChunkStore
    llm: StructuredLLM
    places: PlacesService
    indexer: Indexer
    max_video_seconds: int
    ocr_sample_seconds: float = 1.5


async def process_video(job_id: uuid.UUID, deps: PipelineDeps) -> None:
    jobs = deps.jobs
    job = await jobs.start(job_id)
    video = await deps.videos.get(job.video_id) if job.video_id else None
    if video is None:
        await jobs.fail(job_id, "The video no longer exists")
        return
    path = deps.storage.local_path(video.storage_key)

    # --- probe: make sure the file is usable before spending ASR/LLM time on it
    if video.duration_ms is None:
        await jobs.advance(job_id, JobStage.PROBE, 0)
        try:
            metadata = await deps.prober.probe(path)
        except InvalidVideoError as exc:
            await jobs.fail(job_id, f"The file could not be read as a video ({exc})")
            return
        if problem := metadata_problem(metadata, deps.max_video_seconds):
            await jobs.fail(job_id, problem)
            return
        video.apply_metadata(metadata)
        await _save(deps, video)

    # --- asr
    if video.transcript is None:
        await jobs.advance(job_id, JobStage.ASR, 10)
        wav = await deps.media.extract_wav(path) if video.has_audio else None
        if wav:
            transcript = await deps.transcriber.transcribe(wav)
            video.language = transcript.language
            video.transcript = [
                {"start_ms": s.start_ms, "end_ms": s.end_ms, "text": s.text}
                for s in transcript.segments
            ]
        else:
            video.transcript = []
        await _save(deps, video)

    # --- ocr + chunking
    if video.ocr is None:
        await jobs.advance(job_id, JobStage.OCR, 30)
        video.ocr = await _read_screen_text(deps, video)
        await _save(deps, video)
    if await deps.chunks.count_for_video(video.id) == 0:
        drafts = build_chunks(video.transcript or [], video.ocr or [])
        await deps.chunks.add_chunks(video.user_id, video.id, drafts)
        await deps.uow.commit()

    # --- extract
    if video.extracted_at is None:
        await jobs.advance(job_id, JobStage.EXTRACT, 50)
        chunks = await deps.chunks.for_video(video.id)
        mentions = await extract_mentions(
            deps.llm,
            user_id=video.user_id,
            video_id=video.id,
            video_name=video.original_name,
            chunks=[
                ExtractionChunk(c.seq, c.start_ms, c.end_ms, c.text, c.ocr_text) for c in chunks
            ],
        )
        await deps.places.add_mentions(mentions)
        video.extracted_at = datetime.now(UTC)
        await _save(deps, video)

    # --- resolve
    await jobs.advance(job_id, JobStage.RESOLVE, 65)
    touched, needs_review = await deps.places.resolve_video(video.id)

    # --- aggregate (resolved places are usable even while others wait for review)
    await jobs.advance(job_id, JobStage.AGGREGATE, 80)
    await deps.places.rebuild_cards(touched)

    # --- index
    await jobs.advance(job_id, JobStage.INDEX, 90)
    await deps.indexer.index_video_chunks(video.id)
    await deps.indexer.index_cards(video.user_id, touched)

    if needs_review:
        await jobs.request_review(job_id)
    else:
        await jobs.complete(job_id)


async def _save(deps: PipelineDeps, video: Video) -> None:
    await deps.videos.update(video)
    await deps.uow.commit()


async def _read_screen_text(deps: PipelineDeps, video: Video) -> list[dict[str, object]]:
    if deps.ocr is None:
        return []
    frames = await deps.media.sample_frames(
        deps.storage.local_path(video.storage_key), deps.ocr_sample_seconds
    )
    results: list[dict[str, object]] = []
    for start in range(0, len(frames), OCR_BATCH):
        batch = frames[start : start + OCR_BATCH]
        lines = await deps.ocr.read([f.jpeg for f in batch])
        for frame, frame_lines in zip(batch, lines, strict=True):
            texts = [
                line.text.strip()
                for line in frame_lines
                if line.confidence >= OCR_MIN_CONFIDENCE and len(line.text.strip()) >= 2
            ]
            if texts:
                results.append({"ts_ms": frame.ts_ms, "lines": texts})
    return results
