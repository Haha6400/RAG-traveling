import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from travelrag.core.db import Base
from travelrag.core.security import hash_password, verify_password


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmailTakenError(Exception):
    pass


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> UserRow | None:
    return await session.get(UserRow, user_id)


async def get_user_by_email(session: AsyncSession, email: str) -> UserRow | None:
    result = await session.execute(select(UserRow).where(UserRow.email == normalize_email(email)))
    return result.scalar_one_or_none()


async def create_user(session: AsyncSession, email: str, password: str) -> UserRow:
    user = UserRow(email=normalize_email(email), password_hash=hash_password(password))
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise EmailTakenError(email) from exc
    return user


async def authenticate(session: AsyncSession, email: str, password: str) -> UserRow | None:
    user = await get_user_by_email(session, email)
    if user is None or not verify_password(password, user.password_hash):
        return None
    return user
