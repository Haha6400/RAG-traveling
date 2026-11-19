"""Job events over Postgres LISTEN/NOTIFY.

NOTIFY issued inside a transaction is delivered only on commit, so events never get ahead of
the data they describe.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from travelrag.jobs.domain import JobEvent

CHANNEL = "job_events"


class PgNotifyJobEventPublisher:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def publish(self, event: JobEvent) -> None:
        await self._session.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": CHANNEL, "payload": event.to_json()},
        )


class PgListenJobEventSubscriber:
    """Opens one dedicated connection per subscription (fine for the MVP; a shared fan-out
    listener can replace it if the number of concurrent clients grows)."""

    def __init__(self, conninfo: str, heartbeat_seconds: float) -> None:
        self._conninfo = conninfo
        self._heartbeat_seconds = heartbeat_seconds

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[JobEvent | None]]:
        conn = await psycopg.AsyncConnection.connect(self._conninfo, autocommit=True)
        try:
            await conn.execute(f"LISTEN {CHANNEL}")
            yield self._events(conn)
        finally:
            await conn.close()

    async def _events(self, conn: psycopg.AsyncConnection) -> AsyncIterator[JobEvent | None]:
        while True:
            async for notify in conn.notifies(timeout=self._heartbeat_seconds):
                yield JobEvent.from_json(notify.payload)
            yield None
