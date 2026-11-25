import uuid
from dataclasses import dataclass
from typing import Any

from pgvector.sqlalchemy import SPARSEVEC, VECTOR
from sqlalchemy import ForeignKey, Integer, Text, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from travelrag.core.db import Base
from travelrag.core.text import unaccent
from travelrag.core.users import UserRow
from travelrag.ingestion.adapters.sqlalchemy_repo import VideoRow
from travelrag.media.chunking import ChunkDraft


class TranscriptChunkRow(Base):
    __tablename__ = "transcript_chunks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(UserRow.id, ondelete="CASCADE"))
    video_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(VideoRow.id, ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    ocr_text: Mapped[str | None] = mapped_column(Text)
    search_text: Mapped[str] = mapped_column(Text)
    dense: Mapped[Any] = mapped_column(VECTOR(1024), nullable=True)
    sparse: Mapped[Any] = mapped_column(SPARSEVEC(250002), nullable=True)


@dataclass(frozen=True)
class StoredChunk:
    id: uuid.UUID
    video_id: uuid.UUID
    seq: int
    start_ms: int
    end_ms: int
    text: str
    ocr_text: str | None

    @property
    def full_text(self) -> str:
        return f"{self.text}\n{self.ocr_text}".strip() if self.ocr_text else self.text


def _stored(row: TranscriptChunkRow) -> StoredChunk:
    return StoredChunk(
        row.id, row.video_id, row.seq, row.start_ms, row.end_ms, row.text, row.ocr_text
    )


class SqlAlchemyChunkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def count_for_video(self, video_id: uuid.UUID) -> int:
        result = await self._s.execute(
            select(func.count()).where(TranscriptChunkRow.video_id == video_id)
        )
        return int(result.scalar_one())

    async def add_chunks(
        self, user_id: uuid.UUID, video_id: uuid.UUID, drafts: list[ChunkDraft]
    ) -> None:
        for d in drafts:
            full = f"{d.text} {d.ocr_text or ''}"
            self._s.add(
                TranscriptChunkRow(
                    id=uuid.uuid4(),
                    user_id=user_id,
                    video_id=video_id,
                    seq=d.seq,
                    start_ms=d.start_ms,
                    end_ms=d.end_ms,
                    text=d.text,
                    ocr_text=d.ocr_text,
                    search_text=unaccent(full),
                )
            )
        await self._s.flush()

    async def for_video(self, video_id: uuid.UUID) -> list[StoredChunk]:
        rows = await self._s.execute(
            select(TranscriptChunkRow)
            .where(TranscriptChunkRow.video_id == video_id)
            .order_by(TranscriptChunkRow.seq)
        )
        return [_stored(r) for r in rows.scalars()]

    async def missing_embeddings(self, video_id: uuid.UUID) -> list[StoredChunk]:
        rows = await self._s.execute(
            select(TranscriptChunkRow)
            .where(TranscriptChunkRow.video_id == video_id, TranscriptChunkRow.dense.is_(None))
            .order_by(TranscriptChunkRow.seq)
        )
        return [_stored(r) for r in rows.scalars()]

    async def set_embedding(self, chunk_id: uuid.UUID, dense: Any, sparse: Any) -> None:
        row = await self._s.get(TranscriptChunkRow, chunk_id)
        assert row is not None
        row.dense, row.sparse = dense, sparse
        await self._s.flush()
