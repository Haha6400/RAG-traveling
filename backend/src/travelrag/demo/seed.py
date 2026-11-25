"""Load the Hanoi demo dataset into a user's account, as if four videos had been processed.

    uv run python -m travelrag.demo.seed --email demo@example.com --password demo-pass
    uv run python -m travelrag.demo.seed --email demo@example.com --fake-models   # no ml-service

It runs the real resolution, aggregation and indexing code; only ASR/OCR/LLM extraction are
replaced by the dataset, and geocoding by the dataset's coordinates. Re-running with --reset
replaces the previous demo data of that user.
"""

import argparse
import asyncio
import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select

from travelrag.core.db import SessionUnitOfWork, dispose_engine, get_sessionmaker
from travelrag.core.users import UserRow, create_user, get_user_by_email
from travelrag.demo.hanoi import VIDEOS
from travelrag.demo.offline import DemoGeocoder, HashEmbedder
from travelrag.indexing.service import IndexingService
from travelrag.ingestion.adapters.sqlalchemy_repo import SqlAlchemyVideoRepository, VideoRow
from travelrag.ingestion.domain import Video, VideoSourceKind
from travelrag.llm.ports import StructuredLLM
from travelrag.media.adapters.chunk_repo import SqlAlchemyChunkRepository
from travelrag.media.chunking import ChunkDraft
from travelrag.ml.factory import get_ml_client
from travelrag.ml.ports import Embedder
from travelrag.places.adapters.sqlalchemy_repo import (
    MentionRow,
    PlaceRow,
    SqlAlchemyPlaceRepository,
)
from travelrag.places.domain import Mention
from travelrag.places.factory import resolve_settings
from travelrag.places.service import PlacesService

DEMO_PREFIX = "demo/"


class _NoLLM:
    async def generate(self, **_: Any) -> Any:
        from travelrag.llm.ports import LLMUnavailableError

        raise LLMUnavailableError("seed does not call the LLM")


def demo_storage_key(video_key: str) -> str:
    return f"{DEMO_PREFIX}{video_key}.mp4"


async def reset_demo(session: Any, user_id: uuid.UUID) -> None:
    await session.execute(
        delete(VideoRow).where(
            VideoRow.user_id == user_id, VideoRow.storage_key.startswith(DEMO_PREFIX)
        )
    )
    orphans = select(PlaceRow.id).where(
        PlaceRow.user_id == user_id,
        ~select(MentionRow.id).where(MentionRow.place_id == PlaceRow.id).exists(),
    )
    await session.execute(delete(PlaceRow).where(PlaceRow.id.in_(orphans)))
    await session.commit()


async def seed_user(
    session: Any,
    user_id: uuid.UUID,
    embedder: Embedder,
    *,
    llm: StructuredLLM | None = None,
    auto_accept: bool = True,
) -> dict[str, int]:
    """Insert the dataset for `user_id`; returns counts. Used by tests too."""
    videos = SqlAlchemyVideoRepository(session)
    chunks = SqlAlchemyChunkRepository(session)
    places = PlacesService(
        SqlAlchemyPlaceRepository(session), DemoGeocoder(), llm or _NoLLM(),
        SessionUnitOfWork(session), resolve_settings(),
    )  # fmt: skip
    indexer = IndexingService(session, embedder)
    reviewed = 0
    for spec in VIDEOS:
        created = datetime.fromisoformat(spec["date"]).replace(tzinfo=UTC)
        video = Video(
            id=uuid.uuid4(),
            user_id=user_id,
            source=VideoSourceKind.LOCAL,
            storage_key=demo_storage_key(spec["key"]),
            original_name=spec["name"],
            content_type="video/mp4",
            size_bytes=0,
            sha256=hashlib.sha256(f"{user_id}:{spec['key']}".encode()).hexdigest(),
            duration_ms=spec["chunks"][-1][2],
            width=720,
            height=1280,
            has_audio=True,
            created_at=created,
            language="vi",
            transcript=[
                {"start_ms": s, "end_ms": e, "text": t} for _, s, e, t, _ in spec["chunks"]
            ],
            ocr=[{"ts_ms": s, "lines": [o]} for _, s, _, _, o in spec["chunks"] if o],
            extracted_at=created,
        )
        await videos.add(video)
        await chunks.add_chunks(user_id, video.id, [ChunkDraft(*c) for c in spec["chunks"]])
        by_seq = {c[0]: c for c in spec["chunks"]}
        mentions = []
        for m in spec["mentions"]:
            cited = [by_seq[n] for n in m["chunk_numbers"]]
            payload = {
                k: v
                for k, v in m.items()
                if k not in ("place", "name", "chunk_numbers", "confidence")
            }
            mentions.append(
                Mention(
                    id=uuid.uuid4(),
                    user_id=user_id,
                    video_id=video.id,
                    name=m["name"],
                    payload=payload,
                    source_start_ms=min(c[1] for c in cited),
                    source_end_ms=max(c[2] for c in cited),
                    confidence=m["confidence"],
                    created_at=created,
                )
            )
        await places.add_mentions(mentions)
        touched, needs_review = await places.resolve_video(video.id)
        if needs_review and auto_accept:
            for item in await places.pending_reviews(user_id):
                if item.mention.video_id == video.id and item.candidates:
                    await places.accept_candidate(item.candidates[0][0].id, user_id)
                    reviewed += 1
            touched, _ = await places.resolve_video(video.id)
        await places.rebuild_cards(touched)
        await indexer.index_video_chunks(video.id)
    await indexer.index_cards(user_id)
    catalog = await SqlAlchemyPlaceRepository(session).catalog(user_id)
    return {"videos": len(VIDEOS), "places": len(catalog), "auto_accepted_reviews": reviewed}


async def _main(args: argparse.Namespace) -> None:
    async with get_sessionmaker()() as session:
        user: UserRow | None = await get_user_by_email(session, args.email)
        if user is None:
            if not args.password:
                raise SystemExit("user does not exist; pass --password to create it")
            user = await create_user(session, args.email, args.password)
            print(f"created user {user.email}")
        existing = await session.execute(
            select(VideoRow.id).where(
                VideoRow.user_id == user.id, VideoRow.storage_key.startswith(DEMO_PREFIX)
            )
        )
        if existing.first() is not None:
            if not args.reset:
                raise SystemExit("demo data already loaded; pass --reset to replace it")
            await reset_demo(session, user.id)
        embedder: Embedder = HashEmbedder() if args.fake_models else get_ml_client()
        counts = await seed_user(session, user.id, embedder)
        print(f"seeded {counts} for {user.email}")
    await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", help="create the user with this password if missing")
    parser.add_argument("--reset", action="store_true", help="replace existing demo data")
    parser.add_argument(
        "--fake-models", action="store_true",
        help="hash embeddings instead of ml-service (Q&A quality will be poor)",
    )  # fmt: skip
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
