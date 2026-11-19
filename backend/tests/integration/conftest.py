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
