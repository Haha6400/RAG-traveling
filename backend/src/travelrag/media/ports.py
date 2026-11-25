import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from travelrag.media.chunking import ChunkDraft
from travelrag.media.domain import SampledFrame, VideoMetadata


class VideoProber(Protocol):
    async def probe(self, path: Path) -> VideoMetadata:
        """Read container metadata. Raises InvalidVideoError if the file is not decodable."""
        ...


class MediaReader(Protocol):
    async def extract_wav(self, path: Path) -> bytes | None:
        """16 kHz mono 16-bit WAV of the audio track, or None if there is no audio."""
        ...

    async def sample_frames(self, path: Path, every_seconds: float) -> list[SampledFrame]: ...


class ChunkView(Protocol):
    @property
    def seq(self) -> int: ...
    @property
    def start_ms(self) -> int: ...
    @property
    def end_ms(self) -> int: ...
    @property
    def text(self) -> str: ...
    @property
    def ocr_text(self) -> str | None: ...


class ChunkStore(Protocol):
    async def count_for_video(self, video_id: uuid.UUID) -> int: ...

    async def add_chunks(
        self, user_id: uuid.UUID, video_id: uuid.UUID, drafts: list[ChunkDraft]
    ) -> None: ...

    async def for_video(self, video_id: uuid.UUID) -> Sequence[ChunkView]: ...
