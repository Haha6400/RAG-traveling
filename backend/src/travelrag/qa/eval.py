"""Retrieval evaluation on the Hanoi demo set (see demo/hanoi.py QA_EVAL).

    uv run python -m travelrag.qa.eval --email demo@example.com

Needs the demo data seeded for that user (python -m travelrag.demo.seed). Measures the
retrieval stage only (no LLM): recall@20 of the fused candidates before reranking, and
nDCG@5 after reranking. Use --fake-models to run without ml-service (numbers then reflect
the hash embedder, not bge-m3).
"""

import argparse
import asyncio
import math
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from travelrag.core.config import get_settings
from travelrag.core.db import dispose_engine, get_sessionmaker
from travelrag.core.users import get_user_by_email
from travelrag.demo.hanoi import PLACES, QA_EVAL
from travelrag.demo.offline import HashEmbedder, OverlapReranker
from travelrag.demo.seed import demo_storage_key
from travelrag.ingestion.adapters.sqlalchemy_repo import VideoRow
from travelrag.media.adapters.chunk_repo import TranscriptChunkRow
from travelrag.ml.factory import get_ml_client
from travelrag.ml.ports import Embedder, Reranker
from travelrag.places.adapters.sqlalchemy_repo import PlaceRow
from travelrag.qa.adapters.sql_retriever import SqlRetriever
from travelrag.qa.domain import rrf
from travelrag.qa.ports import Retriever


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    return len(set(ranked[:k]) & relevant) / len(relevant) if relevant else 0.0


def ndcg_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(1 / math.log2(i + 2) for i, key in enumerate(ranked[:k]) if key in relevant)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(k, len(relevant))))
    return dcg / ideal if ideal else 0.0


@dataclass(frozen=True)
class EvalResult:
    question: str
    recall_at_20: float
    ndcg_at_5: float


async def evaluate(
    user_id: uuid.UUID,
    retriever: Retriever,
    embedder: Embedder,
    reranker: Reranker,
    key_map: dict[str, str],
    *,
    candidates: int = 20,
) -> list[EvalResult]:
    results = []
    for case in QA_EVAL:
        relevant = {key_map[k] for k in case["relevant"] if k in key_map}
        [emb] = await embedder.embed([case["question"]])
        ranked = rrf(
            await retriever.ranked(
                user_id, query=emb, keywords=case["question"], place_ids=None, limit=candidates
            )
        )[:candidates]
        docs = await retriever.load(user_id, ranked)
        texts = [docs[k].text for k in ranked if k in docs]
        scores = await reranker.rerank(case["question"], texts)
        reranked = [
            k
            for _, k in sorted(
                zip(scores, [k for k in ranked if k in docs], strict=True), reverse=True
            )
        ]
        results.append(
            EvalResult(
                case["question"],
                recall_at_k(ranked, relevant, candidates),
                ndcg_at_k(reranked, relevant, 5),
            )
        )
    return results


async def demo_key_map(session: object, user_id: uuid.UUID) -> dict[str, str]:
    """Dataset keys ('place:pho_thin', 'chunk:review_pho#1') -> retrieval keys ('p:<id>', ...)."""
    out: dict[str, str] = {}
    osm_to_key = {p["osm_id"]: k for k, p in PLACES.items()}
    rows = await session.execute(  # type: ignore[attr-defined]
        select(PlaceRow.id, PlaceRow.osm_id).where(PlaceRow.user_id == user_id)
    )
    for pid, osm_id in rows:
        if osm_id in osm_to_key:
            out[f"place:{osm_to_key[osm_id]}"] = f"p:{pid}"
    rows = await session.execute(  # type: ignore[attr-defined]
        select(TranscriptChunkRow.id, TranscriptChunkRow.seq, VideoRow.storage_key)
        .join(VideoRow, VideoRow.id == TranscriptChunkRow.video_id)
        .where(VideoRow.user_id == user_id)
    )
    by_storage = {
        demo_storage_key(v): v
        for v in {
            case.split(":")[1].split("#")[0]
            for c in QA_EVAL
            for case in c["relevant"]
            if case.startswith("chunk:")
        }
    }
    for cid, seq, storage_key in rows:
        if storage_key in by_storage:
            out[f"chunk:{by_storage[storage_key]}#{seq}"] = f"c:{cid}"
    return out


async def _main(args: argparse.Namespace) -> None:
    async with get_sessionmaker()() as session:
        user = await get_user_by_email(session, args.email)
        if user is None:
            raise SystemExit("unknown user; seed it first with python -m travelrag.demo.seed")
        key_map = await demo_key_map(session, user.id)
        if args.fake_models:
            embedder: Embedder = HashEmbedder()
            reranker: Reranker = OverlapReranker()
        else:
            embedder = reranker = get_ml_client()
        results = await evaluate(
            user.id, SqlRetriever(session, get_settings().sparse_dim), embedder, reranker, key_map
        )
    await dispose_engine()
    print(f"{'recall@20':>9}  {'nDCG@5':>6}  question")
    for r in results:
        print(f"{r.recall_at_20:9.2f}  {r.ndcg_at_5:6.2f}  {r.question}")
    n = len(results)
    print(
        f"{sum(r.recall_at_20 for r in results) / n:9.2f}  {sum(r.ndcg_at_5 for r in results) / n:6.2f}  MEAN"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--email", required=True)
    parser.add_argument("--fake-models", action="store_true")
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
