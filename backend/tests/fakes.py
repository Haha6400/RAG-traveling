"""In-memory implementations of the jobs ports, for unit tests."""

import copy
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from travelrag.jobs.domain import Job, JobEvent
from travelrag.jobs.service import JobService


class FakeJobRepository:
    def __init__(self) -> None:
        self.jobs: dict[uuid.UUID, Job] = {}

    async def add(self, job: Job) -> None:
        self.jobs[job.id] = copy.deepcopy(job)

    async def get(self, job_id: uuid.UUID) -> Job | None:
        job = self.jobs.get(job_id)
        return copy.deepcopy(job) if job else None

    async def list_for_user(self, user_id: uuid.UUID, limit: int = 50) -> list[Job]:
        jobs = [j for j in self.jobs.values() if j.user_id == user_id]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)[:limit]

    async def latest_for_videos(self, video_ids: list[uuid.UUID]) -> dict[uuid.UUID, Job]:
        latest: dict[uuid.UUID, Job] = {}
        for job in sorted(self.jobs.values(), key=lambda j: j.created_at):
            if job.video_id in video_ids:
                latest[job.video_id] = copy.deepcopy(job)
        return latest

    async def update(self, job: Job) -> None:
        if job.id not in self.jobs:
            raise LookupError(job.id)
        self.jobs[job.id] = copy.deepcopy(job)


class FakeUnitOfWork:
    def __init__(self, publisher: "FakePublisher") -> None:
        self._publisher = publisher
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1
        self._publisher.flush()


class FakePublisher:
    """Mimics NOTIFY: events become visible only after commit."""

    def __init__(self) -> None:
        self._pending: list[JobEvent] = []
        self.delivered: list[JobEvent] = []

    async def publish(self, event: JobEvent) -> None:
        self._pending.append(event)

    def flush(self) -> None:
        self.delivered.extend(self._pending)
        self._pending.clear()


class FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[uuid.UUID] = []

    async def enqueue(self, job: Job) -> None:
        self.enqueued.append(job.id)


class FakeSubscriber:
    def __init__(self, events: list[JobEvent | None]) -> None:
        self._events = events
        self.listening = False

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[JobEvent | None]]:
        self.listening = True

        async def gen() -> AsyncIterator[JobEvent | None]:
            for event in self._events:
                yield event

        yield gen()


class JobHarness:
    def __init__(self) -> None:
        self.repo = FakeJobRepository()
        self.publisher = FakePublisher()
        self.uow = FakeUnitOfWork(self.publisher)
        self.queue = FakeQueue()
        self.service = JobService(self.repo, self.publisher, self.uow, self.queue)


# --- Phase 1-3 fakes ---------------------------------------------------------------------------

from collections.abc import Callable, Sequence  # noqa: E402
from typing import Any  # noqa: E402

from pydantic import BaseModel  # noqa: E402

from travelrag.demo.offline import HashEmbedder, OverlapReranker  # noqa: E402
from travelrag.llm.ports import LLMUnavailableError  # noqa: E402
from travelrag.media.chunking import ChunkDraft  # noqa: E402
from travelrag.ml.ports import OcrLine, Transcript, TranscriptSegment  # noqa: E402

Handler = Callable[[str], BaseModel | Exception]


class ScriptedLLM:
    """LLM fake: one handler per task name; unknown tasks behave like a missing API key."""

    def __init__(self, handlers: dict[str, Handler] | None = None) -> None:
        self.handlers = handlers or {}
        self.calls: list[tuple[str, str]] = []

    async def generate(self, *, task: str, system: str, prompt: str, output: type, **_: Any) -> Any:
        self.calls.append((task, prompt))
        handler = self.handlers.get(task)
        if handler is None:
            raise LLMUnavailableError(f"no scripted response for {task}")
        result = handler(prompt)
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, output), f"{task}: expected {output}, got {type(result)}"
        return result

    def tasks(self) -> list[str]:
        return [t for t, _ in self.calls]


class FakeML(HashEmbedder, OverlapReranker):
    """Embedder + reranker (hash based), transcriber and OCR returning canned output."""

    def __init__(
        self, segments: list[tuple[int, int, str]] | None = None, ocr: list[str] | None = None
    ) -> None:
        self.segments = segments or []
        self.ocr = ocr or []
        self.transcribed = 0

    async def transcribe(self, wav_16k_mono: bytes, language: str | None = None) -> Transcript:
        self.transcribed += 1
        return Transcript("vi", [TranscriptSegment(s, e, t) for s, e, t in self.segments])

    async def read(self, images: list[bytes]) -> list[list[OcrLine]]:
        return [[OcrLine(text, 0.9) for text in self.ocr] for _ in images]


class MemoryChunks:
    def __init__(self) -> None:
        self.chunks: dict[uuid.UUID, list[ChunkDraft]] = {}

    async def count_for_video(self, video_id: uuid.UUID) -> int:
        return len(self.chunks.get(video_id, []))

    async def add_chunks(
        self, user_id: uuid.UUID, video_id: uuid.UUID, drafts: list[ChunkDraft]
    ) -> None:
        self.chunks.setdefault(video_id, []).extend(drafts)

    async def for_video(self, video_id: uuid.UUID) -> Sequence[ChunkDraft]:
        return self.chunks.get(video_id, [])


class StubPlaces:
    """Records what the pipeline hands to the places module."""

    def __init__(self, needs_review: bool = False) -> None:
        self.mentions: list[Any] = []
        self.needs_review = needs_review
        self.rebuilt: list[set[uuid.UUID]] = []

    async def add_mentions(self, mentions: list[Any]) -> None:
        self.mentions.extend(mentions)

    async def resolve_video(self, video_id: uuid.UUID) -> tuple[set[uuid.UUID], bool]:
        return {uuid.UUID(int=1)}, self.needs_review

    async def rebuild_cards(self, place_ids: set[uuid.UUID]) -> None:
        self.rebuilt.append(place_ids)


class StubIndexer:
    def __init__(self) -> None:
        self.videos: list[uuid.UUID] = []

    async def index_video_chunks(self, video_id: uuid.UUID) -> int:
        self.videos.append(video_id)
        return 0

    async def index_cards(self, user_id: uuid.UUID, place_ids: set[uuid.UUID] | None = None) -> int:
        return 0
