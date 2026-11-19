import os

# Point the app at the test database before any travelrag module reads settings.
_TEST_DB = os.environ.get("TRAVELRAG_TEST_DATABASE_URL")
if _TEST_DB:
    os.environ["TRAVELRAG_DATABASE_URL"] = _TEST_DB
os.environ.setdefault("TRAVELRAG_ENVIRONMENT", "test")
os.environ.setdefault("TRAVELRAG_SSE_HEARTBEAT_SECONDS", "0.5")
