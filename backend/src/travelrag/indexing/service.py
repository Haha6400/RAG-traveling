"""Embeddings for transcript chunks and place cards (dense + sparse from bge-m3)."""

import uuid

from pgvector import SparseVector as PgSparse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from travelrag.core.config import get_settings
from travelrag.media.adapters.chunk_repo import SqlAlchemyChunkRepository
from travelrag.ml.ports import Embedder, SparseVector
from travelrag.places.adapters.sqlalchemy_repo import PlaceCardRow


def to_pg_sparse(vector: SparseVector, dim: int) -> PgSparse:
    return PgSparse(dict(zip(vector.indices, vector.values, strict=True)), dim)


class IndexingService:
    """Writes embeddings directly through the session (it is an index, not domain state)."""

    def __init__(self, session: AsyncSession, embedder: Embedder) -> None:
        self._s = session
        self._embedder = embedder
        self._chunks = SqlAlchemyChunkRepository(session)
        self._dim = get_settings().sparse_dim

    async def index_video_chunks(self, video_id: uuid.UUID) -> int:
        chunks = await self._chunks.missing_embeddings(video_id)
        if not chunks:
            return 0
        embeddings = await self._embedder.embed([c.full_text for c in chunks])
        for chunk, emb in zip(chunks, embeddings, strict=True):
            await self._chunks.set_embedding(
                chunk.id, emb.dense, to_pg_sparse(emb.sparse, self._dim)
            )
        await self._s.commit()
        return len(chunks)

    async def index_cards(self, user_id: uuid.UUID, place_ids: set[uuid.UUID] | None = None) -> int:
        """(Re-)embed cards whose text changed since they were last indexed."""
        query = select(PlaceCardRow).where(
            PlaceCardRow.user_id == user_id,
            (PlaceCardRow.indexed_version.is_(None))
            | (PlaceCardRow.indexed_version != PlaceCardRow.version),
        )
        if place_ids is not None:
            query = query.where(PlaceCardRow.place_id.in_(place_ids))
        rows = list((await self._s.execute(query)).scalars())
        if not rows:
            return 0
        embeddings = await self._embedder.embed([r.card_text for r in rows])
        for row, emb in zip(rows, embeddings, strict=True):
            await self._s.execute(
                update(PlaceCardRow)
                .where(PlaceCardRow.place_id == row.place_id)
                .values(
                    dense=emb.dense,
                    sparse=to_pg_sparse(emb.sparse, self._dim),
                    indexed_version=row.version,
                )
            )
        await self._s.commit()
        return len(rows)
