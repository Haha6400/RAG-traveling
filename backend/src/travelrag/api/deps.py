import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from travelrag.core.config import get_settings
from travelrag.core.db import get_sessionmaker
from travelrag.core.security import InvalidTokenError, decode_access_token
from travelrag.core.users import UserRow, get_user
from travelrag.jobs.adapters.procrastinate_queue import ProcrastinateJobQueue
from travelrag.jobs.factory import build_job_service
from travelrag.jobs.ports import JobQueue
from travelrag.jobs.service import JobService
from travelrag.worker.app import app as procrastinate_app


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]

_bearer = HTTPBearer(auto_error=False)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def resolve_user(session: AsyncSession, token: str) -> UserRow:
    settings = get_settings()
    try:
        subject = decode_access_token(
            token, secret=settings.jwt_secret, algorithm=settings.jwt_algorithm
        )
        user_id = uuid.UUID(subject)
    except (InvalidTokenError, ValueError) as exc:
        raise _unauthorized() from exc
    user = await get_user(session, user_id)
    if user is None:
        raise _unauthorized()
    return user


async def get_current_user(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> UserRow:
    if credentials is None:
        raise _unauthorized()
    return await resolve_user(session, credentials.credentials)


CurrentUserDep = Annotated[UserRow, Depends(get_current_user)]


def get_job_queue() -> JobQueue:
    return ProcrastinateJobQueue(procrastinate_app)


async def get_job_service(
    session: SessionDep, queue: Annotated[JobQueue, Depends(get_job_queue)]
) -> JobService:
    return build_job_service(session, queue)


JobServiceDep = Annotated[JobService, Depends(get_job_service)]
