"""Integration tests run against a real Postgres.

They need TRAVELRAG_TEST_DATABASE_URL pointing at a *disposable* database (docker compose creates
`travelrag_test`): migrations are applied automatically and the procrastinate queue is truncated
before each test.
"""

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from travelrag.api.main import create_app
from travelrag.core.db import dispose_engine, get_engine
from travelrag.worker.app import app as procrastinate_app

BACKEND_DIR = Path(__file__).resolve().parents[2]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    skip = pytest.mark.skip(reason="TRAVELRAG_TEST_DATABASE_URL is not set")
    for item in items:
        if "integration" in item.path.parts:
            item.add_marker(pytest.mark.integration)
            if not os.environ.get("TRAVELRAG_TEST_DATABASE_URL"):
                item.add_marker(skip)


@pytest.fixture(scope="session")
def _migrated() -> None:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(config, "head")


@pytest.fixture
async def client(_migrated: None) -> AsyncIterator[httpx.AsyncClient]:
    async with get_engine().begin() as conn:
        await conn.execute(text("TRUNCATE procrastinate_jobs CASCADE"))
    app = create_app()
    # ASGITransport does not run the lifespan, so open procrastinate here.
    async with (
        procrastinate_app.open_async(),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http,
    ):
        yield http
    await dispose_engine()


@pytest.fixture
async def auth_headers(client: httpx.AsyncClient) -> dict[str, str]:
    creds = {"email": f"user-{uuid.uuid4().hex[:8]}@example.com", "password": "secret-pass"}
    assert (await client.post("/api/auth/register", json=creds)).status_code == 201
    token = (await client.post("/api/auth/login", json=creds)).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def run_worker_until_idle() -> None:
    await procrastinate_app.run_worker_async(wait=False, install_signal_handlers=False)


# --- Phase 1-3: fake external services + demo data ---------------------------------------------

from dataclasses import dataclass  # noqa: E402

from tests.fakes import FakeML, ScriptedLLM  # noqa: E402
from travelrag.core.db import get_sessionmaker  # noqa: E402
from travelrag.demo.offline import DemoGeocoder, HashEmbedder  # noqa: E402
from travelrag.demo.seed import seed_user  # noqa: E402
from travelrag.geo.adapters.routing import StraightLineRouter  # noqa: E402


@dataclass
class World:
    llm: ScriptedLLM
    ml: FakeML


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    """Replace every external service (Claude, ml-service, Nominatim, OSRM) where the app
    looks it up, so API and worker code paths run unchanged against fakes."""
    w = World(llm=ScriptedLLM(), ml=FakeML())
    for target in (
        "travelrag.places.factory.get_llm",
        "travelrag.planning.factory.get_llm",
        "travelrag.qa.factory.get_llm",
        "travelrag.worker.tasks.get_llm",
    ):
        monkeypatch.setattr(target, lambda: w.llm)
    for target in (
        "travelrag.qa.factory.get_ml_client",
        "travelrag.worker.tasks.get_ml_client",
        "travelrag.api.routers.places.get_ml_client",
    ):
        monkeypatch.setattr(target, lambda: w.ml)
    monkeypatch.setattr("travelrag.places.factory.get_geocoder", lambda: DemoGeocoder())
    for target in ("travelrag.planning.factory.get_router", "travelrag.qa.factory.get_router"):
        monkeypatch.setattr(target, lambda: StraightLineRouter())
    return w


@pytest.fixture
async def seeded(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> uuid.UUID:
    """The Hanoi demo dataset loaded for the logged-in test user; returns the user id."""
    user_id = uuid.UUID((await client.get("/api/auth/me", headers=auth_headers)).json()["id"])
    async with get_sessionmaker()() as session:
        await seed_user(session, user_id, HashEmbedder())
    return user_id
