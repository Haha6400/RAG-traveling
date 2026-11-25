from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from travelrag.api.routers import ask, auth, health, jobs, places, videos
from travelrag.core.config import get_settings
from travelrag.core.db import dispose_engine
from travelrag.core.logging import configure_logging
from travelrag.worker.app import app as procrastinate_app


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging(get_settings().log_level)
    async with procrastinate_app.open_async():
        yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="RAG-traveling API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for router in (
        health.router,
        auth.router,
        jobs.router,
        videos.router,
        places.router,
        ask.router,
    ):
        app.include_router(router, prefix="/api")
    return app


app = create_app()
