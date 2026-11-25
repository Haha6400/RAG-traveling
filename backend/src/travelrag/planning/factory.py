from sqlalchemy.ext.asyncio import AsyncSession

from travelrag.core.config import get_settings
from travelrag.core.db import SessionUnitOfWork
from travelrag.geo.factory import get_router
from travelrag.geo.ports import RouteProvider
from travelrag.llm.factory import get_llm
from travelrag.llm.ports import StructuredLLM
from travelrag.places.adapters.sqlalchemy_repo import SqlAlchemyPlaceRepository
from travelrag.planning.adapters.ortools_solver import OrToolsSolver
from travelrag.planning.adapters.sqlalchemy_repo import SqlAlchemyTripRepository
from travelrag.planning.service import PlannerService


def build_planner(
    session: AsyncSession,
    *,
    router: RouteProvider | None = None,
    llm: StructuredLLM | None = None,
) -> PlannerService:
    return PlannerService(
        trips=SqlAlchemyTripRepository(session),
        catalog=SqlAlchemyPlaceRepository(session),
        router=router or get_router(),
        solver=OrToolsSolver(),
        llm=llm or get_llm(),
        uow=SessionUnitOfWork(session),
        time_limit_seconds=get_settings().planner_time_limit_seconds,
    )
