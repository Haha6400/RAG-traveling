import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import SPARSEVEC, VECTOR
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    delete,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from travelrag.core.db import Base
from travelrag.core.text import normalize_name, unaccent
from travelrag.core.users import UserRow
from travelrag.geo.domain import OsmPlace
from travelrag.ingestion.adapters.sqlalchemy_repo import VideoRow
from travelrag.places.domain import (
    Category,
    MatchCandidate,
    Mention,
    MentionSource,
    MentionStatus,
    Place,
)
from travelrag.places.ports import CatalogEntry, PlaceCard

DENSE_DIM, SPARSE_DIM = 1024, 250002


class PlaceRow(Base):
    __tablename__ = "places"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(UserRow.id, ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(256))
    name_norm: Mapped[str] = mapped_column(String(256))
    category: Mapped[str] = mapped_column(String(32))
    area: Mapped[str | None] = mapped_column(String(256))
    address: Mapped[str | None] = mapped_column(String(512))
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    osm_type: Mapped[str | None] = mapped_column(String(16))
    osm_id: Mapped[int | None] = mapped_column(BigInteger)
    osm_opening_hours: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PlaceAliasRow(Base):
    __tablename__ = "place_aliases"

    place_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(PlaceRow.id, ondelete="CASCADE"), primary_key=True
    )
    alias: Mapped[str] = mapped_column(String(256))
    alias_norm: Mapped[str] = mapped_column(String(256), primary_key=True)


class MentionRow(Base):
    __tablename__ = "place_mentions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(UserRow.id, ondelete="CASCADE"))
    video_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(VideoRow.id, ondelete="CASCADE"))
    place_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(PlaceRow.id, ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(256))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    source_start_ms: Mapped[int] = mapped_column(Integer)
    source_end_ms: Mapped[int] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CandidateRow(Base):
    __tablename__ = "place_match_candidates"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(UserRow.id, ondelete="CASCADE"))
    mention_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(MentionRow.id, ondelete="CASCADE"))
    place_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(PlaceRow.id, ondelete="CASCADE"))
    osm: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    score: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PlaceCardRow(Base):
    __tablename__ = "place_cards"

    place_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(PlaceRow.id, ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(UserRow.id, ondelete="CASCADE"))
    aggregated: Mapped[dict[str, Any]] = mapped_column(JSONB)
    conflicts: Mapped[list[Any]] = mapped_column(JSONB)
    summary_text: Mapped[str | None] = mapped_column(Text)
    summary_stale: Mapped[bool] = mapped_column(Boolean)
    version: Mapped[int] = mapped_column(Integer)
    indexed_version: Mapped[int | None] = mapped_column(Integer)
    card_text: Mapped[str] = mapped_column(Text)
    search_text: Mapped[str] = mapped_column(Text)
    dense: Mapped[Any] = mapped_column(VECTOR(DENSE_DIM), nullable=True)
    sparse: Mapped[Any] = mapped_column(SPARSEVEC(SPARSE_DIM), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def _now() -> datetime:
    return datetime.now(UTC)


def _place(row: PlaceRow, aliases: list[str]) -> Place:
    return Place(
        id=row.id,
        user_id=row.user_id,
        name=row.name,
        category=Category(row.category),
        area=row.area,
        address=row.address,
        lat=row.lat,
        lng=row.lng,
        osm_type=row.osm_type,
        osm_id=row.osm_id,
        osm_opening_hours=row.osm_opening_hours,
        aliases=aliases,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _mention(row: MentionRow) -> Mention:
    return Mention(
        id=row.id,
        user_id=row.user_id,
        video_id=row.video_id,
        name=row.name,
        payload=dict(row.payload),
        source_start_ms=row.source_start_ms,
        source_end_ms=row.source_end_ms,
        confidence=row.confidence,
        status=MentionStatus(row.status),
        place_id=row.place_id,
        created_at=row.created_at,
    )


def _card(row: PlaceCardRow) -> PlaceCard:
    return PlaceCard(
        place_id=row.place_id,
        user_id=row.user_id,
        aggregated=dict(row.aggregated),
        conflicts=list(row.conflicts),
        card_text=row.card_text,
        summary_text=row.summary_text,
        summary_stale=row.summary_stale,
        version=row.version,
        indexed_version=row.indexed_version,
    )


def _candidate(row: CandidateRow) -> MatchCandidate:
    return MatchCandidate(
        id=row.id,
        user_id=row.user_id,
        mention_id=row.mention_id,
        score=row.score,
        place_id=row.place_id,
        osm=OsmPlace(**row.osm) if row.osm else None,
        status=row.status,
    )


class SqlAlchemyPlaceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- places --------------------------------------------------------------------------------

    async def _aliases(self, place_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
        if not place_ids:
            return {}
        rows = await self._s.execute(
            select(PlaceAliasRow).where(PlaceAliasRow.place_id.in_(place_ids))
        )
        out: dict[uuid.UUID, list[str]] = {}
        for row in rows.scalars():
            out.setdefault(row.place_id, []).append(row.alias)
        return out

    async def _many(self, rows: list[PlaceRow]) -> list[Place]:
        aliases = await self._aliases([r.id for r in rows])
        return [_place(r, aliases.get(r.id, [])) for r in rows]

    async def add_place(self, place: Place) -> None:
        row = PlaceRow(id=place.id, created_at=place.created_at)
        self._s.add(row)
        await self._write_place(row, place)

    async def update_place(self, place: Place) -> None:
        row = await self._s.get(PlaceRow, place.id)
        assert row is not None
        await self._write_place(row, place)

    async def _write_place(self, row: PlaceRow, place: Place) -> None:
        row.user_id, row.name, row.name_norm = place.user_id, place.name, normalize_name(place.name)
        row.category, row.area, row.address = place.category.value, place.area, place.address
        row.lat, row.lng = place.lat, place.lng
        row.osm_type, row.osm_id, row.osm_opening_hours = (
            place.osm_type,
            place.osm_id,
            place.osm_opening_hours,
        )
        row.updated_at = _now()
        await self._s.flush()
        await self._s.execute(delete(PlaceAliasRow).where(PlaceAliasRow.place_id == place.id))
        seen: set[str] = set()
        for alias in place.aliases:
            norm = normalize_name(alias)
            if norm not in seen:
                seen.add(norm)
                self._s.add(PlaceAliasRow(place_id=place.id, alias=alias, alias_norm=norm))
        await self._s.flush()

    async def get_place(self, place_id: uuid.UUID) -> Place | None:
        row = await self._s.get(PlaceRow, place_id, populate_existing=True)
        return (await self._many([row]))[0] if row else None

    async def list_places(self, user_id: uuid.UUID) -> list[Place]:
        rows = await self._s.execute(
            select(PlaceRow).where(PlaceRow.user_id == user_id).order_by(PlaceRow.name)
        )
        return await self._many(list(rows.scalars()))

    async def find_similar(self, user_id: uuid.UUID, name: str, limit: int = 5) -> list[Place]:
        norm = normalize_name(name)
        alias_match = (
            select(PlaceAliasRow.place_id)
            .where(func.similarity(PlaceAliasRow.alias_norm, norm) > 0.3)
            .scalar_subquery()
        )
        score = func.similarity(PlaceRow.name_norm, norm)
        rows = await self._s.execute(
            select(PlaceRow)
            .where(PlaceRow.user_id == user_id)
            .where((score > 0.3) | PlaceRow.id.in_(alias_match))
            .order_by(score.desc())
            .limit(limit)
        )
        return await self._many(list(rows.scalars()))

    async def find_by_osm(self, user_id: uuid.UUID, osm_type: str, osm_id: int) -> Place | None:
        row = (
            await self._s.execute(
                select(PlaceRow).where(
                    PlaceRow.user_id == user_id,
                    PlaceRow.osm_type == osm_type,
                    PlaceRow.osm_id == osm_id,
                )
            )
        ).scalar_one_or_none()
        return (await self._many([row]))[0] if row else None

    # --- mentions ------------------------------------------------------------------------------

    async def add_mention(self, mention: Mention) -> None:
        row = MentionRow(id=mention.id, created_at=mention.created_at)
        self._s.add(row)
        await self._write_mention(row, mention)

    async def update_mention(self, mention: Mention) -> None:
        row = await self._s.get(MentionRow, mention.id)
        assert row is not None
        await self._write_mention(row, mention)

    async def _write_mention(self, row: MentionRow, mention: Mention) -> None:
        row.user_id, row.video_id, row.place_id = (
            mention.user_id,
            mention.video_id,
            mention.place_id,
        )
        row.name, row.payload = mention.name, mention.payload
        row.source_start_ms, row.source_end_ms = mention.source_start_ms, mention.source_end_ms
        row.confidence, row.status = mention.confidence, mention.status.value
        await self._s.flush()

    async def get_mention(self, mention_id: uuid.UUID) -> Mention | None:
        row = await self._s.get(MentionRow, mention_id, populate_existing=True)
        return _mention(row) if row else None

    async def mentions_for_video(self, video_id: uuid.UUID) -> list[Mention]:
        rows = await self._s.execute(
            select(MentionRow)
            .where(MentionRow.video_id == video_id)
            .order_by(MentionRow.source_start_ms)
            .execution_options(populate_existing=True)
        )
        return [_mention(r) for r in rows.scalars()]

    async def mentions_in_review(self, user_id: uuid.UUID) -> list[Mention]:
        rows = await self._s.execute(
            select(MentionRow)
            .where(MentionRow.user_id == user_id, MentionRow.status == MentionStatus.REVIEW.value)
            .order_by(MentionRow.created_at)
        )
        return [_mention(r) for r in rows.scalars()]

    async def sources_for_place(self, place_id: uuid.UUID) -> list[MentionSource]:
        rows = await self._s.execute(
            select(MentionRow, VideoRow.original_name, VideoRow.created_at)
            .join(VideoRow, VideoRow.id == MentionRow.video_id)
            .where(MentionRow.place_id == place_id)
            .order_by(VideoRow.created_at)
            .execution_options(populate_existing=True)
        )
        return [MentionSource(_mention(m), name, created) for m, name, created in rows.all()]

    # --- candidates ----------------------------------------------------------------------------

    async def add_candidate(self, candidate: MatchCandidate) -> None:
        self._s.add(
            CandidateRow(
                id=candidate.id,
                user_id=candidate.user_id,
                mention_id=candidate.mention_id,
                place_id=candidate.place_id,
                osm=asdict(candidate.osm) if candidate.osm else None,
                score=candidate.score,
                status=candidate.status,
                created_at=_now(),
            )
        )
        await self._s.flush()

    async def get_candidate(self, candidate_id: uuid.UUID) -> MatchCandidate | None:
        row = await self._s.get(CandidateRow, candidate_id, populate_existing=True)
        return _candidate(row) if row else None

    async def candidates_for_mention(self, mention_id: uuid.UUID) -> list[MatchCandidate]:
        rows = await self._s.execute(
            select(CandidateRow)
            .where(CandidateRow.mention_id == mention_id)
            .order_by(CandidateRow.score.desc())
            .execution_options(populate_existing=True)
        )
        return [_candidate(r) for r in rows.scalars()]

    async def update_candidate(self, candidate: MatchCandidate) -> None:
        row = await self._s.get(CandidateRow, candidate.id)
        assert row is not None
        row.status = candidate.status
        await self._s.flush()

    # --- cards ---------------------------------------------------------------------------------

    async def catalog(self, user_id: uuid.UUID) -> list[CatalogEntry]:
        places = await self.list_places(user_id)
        rows = await self._s.execute(select(PlaceCardRow).where(PlaceCardRow.user_id == user_id))
        cards = {r.place_id: _card(r) for r in rows.scalars()}
        return [CatalogEntry(p, cards.get(p.id)) for p in places]

    async def get_card(self, place_id: uuid.UUID) -> PlaceCard | None:
        row = await self._s.get(PlaceCardRow, place_id, populate_existing=True)
        return _card(row) if row else None

    async def save_card(self, card: PlaceCard) -> None:
        row = await self._s.get(PlaceCardRow, card.place_id)
        if row is None:
            row = PlaceCardRow(place_id=card.place_id)
            self._s.add(row)
        row.user_id, row.aggregated, row.conflicts = card.user_id, card.aggregated, card.conflicts
        row.summary_text, row.summary_stale = card.summary_text, card.summary_stale
        row.version, row.indexed_version = card.version, card.indexed_version
        row.card_text, row.search_text = card.card_text, unaccent(card.card_text)
        row.updated_at = _now()
        await self._s.flush()
