import uuid
from typing import Any, Protocol

from travelrag.ml.ports import Embedding
from travelrag.qa.domain import Doc


class Retriever(Protocol):
    async def ranked(
        self,
        user_id: uuid.UUID,
        *,
        query: Embedding,
        keywords: str,
        place_ids: list[uuid.UUID] | None,
        limit: int,
    ) -> dict[str, list[str]]:
        """Ranked doc keys per branch (dense / sparse / trigram), always filtered by user and,
        when given, restricted to the places in `place_ids`."""
        ...

    async def load(self, user_id: uuid.UUID, keys: list[str]) -> dict[str, Doc]: ...


class TripReader(Protocol):
    async def trip_context(
        self, user_id: uuid.UUID, trip_id: uuid.UUID | None
    ) -> dict[str, Any] | None:
        """The trip (given, or the latest) as plain data for answering plan questions."""
        ...
