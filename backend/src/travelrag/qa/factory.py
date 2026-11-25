import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from travelrag.core.config import get_settings
from travelrag.geo.factory import get_router
from travelrag.geo.ports import RouteProvider
from travelrag.llm.factory import get_llm
from travelrag.llm.ports import StructuredLLM
from travelrag.ml.factory import get_ml_client
from travelrag.ml.ports import Embedder, Reranker
from travelrag.places.adapters.sqlalchemy_repo import SqlAlchemyPlaceRepository
from travelrag.qa.adapters.sql_retriever import SqlRetriever
from travelrag.qa.service import QAService


class _NoTrips:
    """Trip planning arrives in Phase 3; until then plan questions find no trip."""

    async def trip_context(
        self, user_id: uuid.UUID, trip_id: uuid.UUID | None
    ) -> dict[str, Any] | None:
        return None


def build_qa_service(
    session: AsyncSession,
    *,
    llm: StructuredLLM | None = None,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
    router: RouteProvider | None = None,
) -> QAService:
    s = get_settings()
    ml = get_ml_client()
    return QAService(
        llm=llm or get_llm(),
        embedder=embedder or ml,
        reranker=reranker or ml,
        retriever=SqlRetriever(session, s.sparse_dim),
        catalog=SqlAlchemyPlaceRepository(session),
        trips=_NoTrips(),
        router=router or get_router(),
        candidates=s.retrieval_candidates,
        keep=s.retrieval_keep,
        min_score=s.rerank_min_score,
    )
