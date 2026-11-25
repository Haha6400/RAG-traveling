"""Hybrid retrieval in Postgres: pgvector dense (cosine), pgvector sparsevec (inner product,
bge-m3 lexical weights) and pg_trgm (unaccented trigram word similarity) over transcript
chunks and place cards. Every query is filtered by user_id first."""

import uuid
from typing import Any

from pgvector import SparseVector as PgSparse
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from travelrag.core.text import unaccent
from travelrag.ml.ports import Embedding
from travelrag.qa.domain import Doc

# Chunks count as "about a place" when they overlap one of its mentions (+/- this margin).
MENTION_MARGIN_MS = 30_000

_CHUNK_SCOPE = """
  AND EXISTS (
    SELECT 1 FROM place_mentions m
    WHERE m.video_id = c.video_id AND m.place_id = ANY(:place_ids)
      AND c.start_ms <= m.source_end_ms + :margin AND c.end_ms >= m.source_start_ms - :margin
  )"""


def _vec(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


class SqlRetriever:
    def __init__(self, session: AsyncSession, sparse_dim: int) -> None:
        self._s = session
        self._dim = sparse_dim

    async def _keys(self, sql: str, params: dict[str, Any]) -> list[str]:
        rows = await self._s.execute(text(sql), params)
        return [str(r[0]) for r in rows]

    async def ranked(
        self,
        user_id: uuid.UUID,
        *,
        query: Embedding,
        keywords: str,
        place_ids: list[uuid.UUID] | None,
        limit: int,
    ) -> dict[str, list[str]]:
        # pgvector >= 0.8: keep scanning the HNSW index until enough rows pass the filters.
        await self._s.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        sparse = PgSparse(
            dict(zip(query.sparse.indices, query.sparse.values, strict=True)), self._dim
        )
        params: dict[str, Any] = {
            "uid": user_id,
            "dense": _vec(query.dense),
            "sparse": sparse.to_text(),
            "kw": unaccent(keywords),
            "lim": limit,
            "place_ids": place_ids or [],
            "margin": MENTION_MARGIN_MS,
        }
        chunk_scope = _CHUNK_SCOPE if place_ids else ""
        card_scope = " AND p.place_id = ANY(:place_ids)" if place_ids else ""

        def chunk(order: str, extra: str = "") -> str:
            return (
                f"SELECT 'c:' || c.id FROM transcript_chunks c WHERE c.user_id = :uid"
                f"{extra}{chunk_scope} ORDER BY {order} LIMIT :lim"
            )

        def card(order: str, extra: str = "") -> str:
            return (
                f"SELECT 'p:' || p.place_id FROM place_cards p WHERE p.user_id = :uid"
                f"{extra}{card_scope} ORDER BY {order} LIMIT :lim"
            )

        dense_order = "{t}.dense <=> CAST(:dense AS vector)"
        sparse_order = "{t}.sparse <#> CAST(:sparse AS sparsevec)"
        trgm_where = " AND :kw <% {t}.search_text"
        trgm_order = "word_similarity(:kw, {t}.search_text) DESC"

        # Six ranked lists (3 branches x 2 document types); RRF fuses them, so chunks and cards
        # compete on rank, not on incomparable raw scores.
        out: dict[str, list[str]] = {
            "dense_chunks": await self._keys(
                chunk(dense_order.format(t="c"), " AND c.dense IS NOT NULL"), params
            ),
            "dense_cards": await self._keys(
                card(dense_order.format(t="p"), " AND p.dense IS NOT NULL"), params
            ),
            "sparse_chunks": await self._keys(
                chunk(sparse_order.format(t="c"), " AND c.sparse IS NOT NULL"), params
            ),
            "sparse_cards": await self._keys(
                card(sparse_order.format(t="p"), " AND p.sparse IS NOT NULL"), params
            ),
        }
        if params["kw"].strip():
            out["trigram_chunks"] = await self._keys(
                chunk(trgm_order.format(t="c"), trgm_where.format(t="c")), params
            )
            out["trigram_cards"] = await self._keys(
                card(trgm_order.format(t="p"), trgm_where.format(t="p")), params
            )
        return out

    async def load(self, user_id: uuid.UUID, keys: list[str]) -> dict[str, Doc]:
        chunk_ids = [uuid.UUID(k[2:]) for k in keys if k.startswith("c:")]
        card_ids = [uuid.UUID(k[2:]) for k in keys if k.startswith("p:")]
        docs: dict[str, Doc] = {}
        if chunk_ids:
            rows = await self._s.execute(
                text(
                    """
                    SELECT c.id, c.text, c.ocr_text, c.start_ms, c.end_ms, v.id, v.original_name,
                           v.created_at,
                           (SELECT m.place_id FROM place_mentions m
                             WHERE m.video_id = c.video_id AND m.place_id IS NOT NULL
                               AND c.start_ms <= m.source_end_ms AND c.end_ms >= m.source_start_ms
                             ORDER BY m.source_start_ms LIMIT 1) AS place_id
                    FROM transcript_chunks c JOIN videos v ON v.id = c.video_id
                    WHERE c.user_id = :uid AND c.id IN :ids
                    """
                ).bindparams(bindparam("ids", expanding=True)),
                {"uid": user_id, "ids": chunk_ids},
            )
            place_ids = set()
            raw = []
            for r in rows:
                raw.append(r)
                if r.place_id:
                    place_ids.add(r.place_id)
            names = await self._place_names(user_id, list(place_ids))
            for cid, body, ocr, start, end, vid, vname, vdate, pid in raw:
                body_text = f"{body}\n(Trên màn hình: {ocr})" if ocr else body
                docs[f"c:{cid}"] = Doc(
                    key=f"c:{cid}",
                    kind="chunk",
                    text=body_text,
                    place_id=pid,
                    place_name=names.get(pid) if pid else None,
                    video_id=vid,
                    video_name=vname,
                    video_date=vdate,
                    start_ms=start,
                    end_ms=end,
                )
        if card_ids:
            rows = await self._s.execute(
                text(
                    "SELECT c.place_id, c.card_text, p.name FROM place_cards c "
                    "JOIN places p ON p.id = c.place_id "
                    "WHERE c.user_id = :uid AND c.place_id IN :ids"
                ).bindparams(bindparam("ids", expanding=True)),
                {"uid": user_id, "ids": card_ids},
            )
            for pid, body, name in rows:
                docs[f"p:{pid}"] = Doc(
                    key=f"p:{pid}", kind="card", text=body, place_id=pid, place_name=name
                )
        return docs

    async def _place_names(self, user_id: uuid.UUID, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not ids:
            return {}
        rows = await self._s.execute(
            text("SELECT id, name FROM places WHERE user_id = :uid AND id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"uid": user_id, "ids": ids},
        )
        return {r[0]: r[1] for r in rows}
