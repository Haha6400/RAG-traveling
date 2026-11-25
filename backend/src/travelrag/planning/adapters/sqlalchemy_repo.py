import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, delete, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from travelrag.core.db import Base
from travelrag.core.users import UserRow
from travelrag.places.adapters.sqlalchemy_repo import PlaceRow
from travelrag.planning.domain import Trip, TripItem, TripPreferences


class TripRow(Base):
    __tablename__ = "trips"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(UserRow.id, ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(256))
    area: Mapped[str | None] = mapped_column(String(256))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSONB)
    routing_source: Mapped[str] = mapped_column(String(16))
    unscheduled: Mapped[list[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TripItemRow(Base):
    __tablename__ = "trip_items"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    trip_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(TripRow.id, ondelete="CASCADE"))
    day: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    place_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(PlaceRow.id, ondelete="CASCADE"))
    start_min: Mapped[int] = mapped_column(Integer)
    end_min: Mapped[int] = mapped_column(Integer)
    travel_from_prev: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    cost_estimate: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    locked: Mapped[bool] = mapped_column(Boolean)


def _item(row: TripItemRow) -> TripItem:
    return TripItem(
        id=row.id,
        day=row.day,
        position=row.position,
        place_id=row.place_id,
        start_min=row.start_min,
        end_min=row.end_min,
        travel_from_prev=row.travel_from_prev,
        cost_estimate=row.cost_estimate,
        note=row.note,
        locked=row.locked,
    )


class SqlAlchemyTripRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def _load(self, row: TripRow) -> Trip:
        items = await self._s.execute(
            select(TripItemRow)
            .where(TripItemRow.trip_id == row.id)
            .order_by(TripItemRow.day, TripItemRow.position)
            .execution_options(populate_existing=True)
        )
        return Trip(
            id=row.id,
            user_id=row.user_id,
            title=row.title,
            area=row.area,
            start_date=row.start_date,
            end_date=row.end_date,
            preferences=TripPreferences.model_validate(row.preferences),
            routing_source=row.routing_source,
            unscheduled=list(row.unscheduled),
            items=[_item(i) for i in items.scalars()],
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def add(self, trip: Trip) -> None:
        row = TripRow(id=trip.id, user_id=trip.user_id, created_at=trip.created_at)
        self._s.add(row)
        await self._write(row, trip)

    async def save(self, trip: Trip) -> None:
        row = await self._s.get(TripRow, trip.id)
        assert row is not None
        await self._write(row, trip)

    async def _write(self, row: TripRow, trip: Trip) -> None:
        row.title, row.area = trip.title, trip.area
        row.start_date, row.end_date = trip.start_date, trip.end_date
        row.preferences = trip.preferences.model_dump(mode="json")
        row.routing_source, row.unscheduled = trip.routing_source, trip.unscheduled
        row.updated_at = trip.updated_at
        await self._s.flush()
        await self._s.execute(delete(TripItemRow).where(TripItemRow.trip_id == trip.id))
        for item in trip.items:
            self._s.add(
                TripItemRow(
                    id=item.id,
                    trip_id=trip.id,
                    day=item.day,
                    position=item.position,
                    place_id=item.place_id,
                    start_min=item.start_min,
                    end_min=item.end_min,
                    travel_from_prev=item.travel_from_prev,
                    cost_estimate=item.cost_estimate,
                    note=item.note,
                    locked=item.locked,
                )
            )
        await self._s.flush()

    async def get(self, trip_id: uuid.UUID) -> Trip | None:
        row = await self._s.get(TripRow, trip_id, populate_existing=True)
        return await self._load(row) if row else None

    async def latest_for_user(self, user_id: uuid.UUID) -> Trip | None:
        row = (
            await self._s.execute(
                select(TripRow)
                .where(TripRow.user_id == user_id)
                .order_by(TripRow.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return await self._load(row) if row else None

    async def list_for_user(self, user_id: uuid.UUID) -> list[Trip]:
        rows = await self._s.execute(
            select(TripRow).where(TripRow.user_id == user_id).order_by(TripRow.created_at.desc())
        )
        return [await self._load(r) for r in rows.scalars()]

    async def delete(self, trip_id: uuid.UUID) -> None:
        await self._s.execute(delete(TripRow).where(TripRow.id == trip_id))
