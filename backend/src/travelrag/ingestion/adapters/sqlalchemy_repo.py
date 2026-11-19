import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from travelrag.core.db import Base
from travelrag.core.users import UserRow
from travelrag.ingestion.domain import DuplicateVideoError, Video, VideoSourceKind


class VideoRow(Base):
    __tablename__ = "videos"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(UserRow.id, ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(16))
    storage_key: Mapped[str] = mapped_column(String(512))
    original_name: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    has_audio: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


_FIELDS = (
    "user_id",
    "storage_key",
    "original_name",
    "content_type",
    "size_bytes",
    "sha256",
    "duration_ms",
    "width",
    "height",
    "has_audio",
    "created_at",
)


def _to_domain(row: VideoRow) -> Video:
    return Video(
        id=row.id,
        source=VideoSourceKind(row.source),
        **{name: getattr(row, name) for name in _FIELDS},
    )


def _copy_into(row: VideoRow, video: Video) -> None:
    row.source = video.source.value
    for name in _FIELDS:
        setattr(row, name, getattr(video, name))


class SqlAlchemyVideoRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, video: Video) -> None:
        row = VideoRow(id=video.id)
        _copy_into(row, video)
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # Lost a race with a concurrent upload of the same file.
            await self._session.rollback()
            raise DuplicateVideoError(None) from exc

    async def get(self, video_id: uuid.UUID) -> Video | None:
        row = await self._session.get(VideoRow, video_id, populate_existing=True)
        return _to_domain(row) if row else None

    async def find_by_hash(self, user_id: uuid.UUID, sha256: str) -> Video | None:
        result = await self._session.execute(
            select(VideoRow).where(VideoRow.user_id == user_id, VideoRow.sha256 == sha256)
        )
        row = result.scalar_one_or_none()
        return _to_domain(row) if row else None

    async def list_for_user(self, user_id: uuid.UUID, limit: int = 100) -> list[Video]:
        result = await self._session.execute(
            select(VideoRow)
            .where(VideoRow.user_id == user_id)
            .order_by(VideoRow.created_at.desc())
            .limit(limit)
        )
        return [_to_domain(row) for row in result.scalars()]

    async def update(self, video: Video) -> None:
        row = await self._session.get(VideoRow, video.id)
        if row is None:
            raise LookupError(f"video {video.id} does not exist")
        _copy_into(row, video)
        await self._session.flush()

    async def delete(self, video_id: uuid.UUID) -> None:
        await self._session.execute(delete(VideoRow).where(VideoRow.id == video_id))
