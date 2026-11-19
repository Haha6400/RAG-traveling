from functools import cache
from pathlib import Path
from typing import Literal, Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_JWT_SECRET = "dev-only-insecure-secret-change-me-32b"


class Settings(BaseSettings):
    """Application settings, read from TRAVELRAG_* environment variables (or backend/.env)."""

    model_config = SettingsConfigDict(env_prefix="TRAVELRAG_", env_file=".env", extra="ignore")

    environment: Literal["dev", "test", "prod"] = "dev"
    database_url: str = "postgresql+psycopg://travelrag:travelrag@localhost:5432/travelrag"

    jwt_secret: str = _DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24

    # JSON list in the environment, e.g. TRAVELRAG_CORS_ORIGINS='["http://localhost:5173"]'
    cors_origins: list[str] = ["http://localhost:5173"]
    log_level: str = "INFO"
    sse_heartbeat_seconds: float = 15.0

    # Uploaded videos. Must be shared between the api and worker processes.
    media_dir: Path = Path("data/media")
    max_upload_bytes: int = 500 * 1024 * 1024
    max_video_seconds: int = 20 * 60

    @model_validator(mode="after")
    def _require_real_secret_in_prod(self) -> Self:
        if self.environment == "prod" and self.jwt_secret == _DEFAULT_JWT_SECRET:
            raise ValueError("TRAVELRAG_JWT_SECRET must be set in prod")
        return self

    @property
    def psycopg_conninfo(self) -> str:
        """Plain libpq URL (without the SQLAlchemy driver suffix) for psycopg / procrastinate."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


@cache
def get_settings() -> Settings:
    return Settings()
