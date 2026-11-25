from dataclasses import dataclass
from typing import Protocol


class MLUnavailableError(Exception):
    """The model server cannot be reached or returned an error."""


@dataclass(frozen=True)
class SparseVector:
    indices: list[int]
    values: list[float]


@dataclass(frozen=True)
class Embedding:
    dense: list[float]
    sparse: SparseVector


@dataclass(frozen=True)
class TranscriptSegment:
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class Transcript:
    language: str | None
    segments: list[TranscriptSegment]


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[Embedding]: ...


class Reranker(Protocol):
    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        """Relevance score in [0, 1] for each document, in input order."""
        ...


class Transcriber(Protocol):
    async def transcribe(self, wav_16k_mono: bytes, language: str | None = None) -> Transcript: ...


class OcrEngine(Protocol):
    async def read(self, images: list[bytes]) -> list[list[OcrLine]]:
        """Text lines per image, in input order."""
        ...
