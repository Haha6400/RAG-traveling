from functools import cache

from travelrag.core.config import get_settings
from travelrag.ml.adapters.http_client import MLServiceClient


@cache
def get_ml_client() -> MLServiceClient:
    settings = get_settings()
    return MLServiceClient(settings.ml_service_url, settings.ml_timeout_seconds)
