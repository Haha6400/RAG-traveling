import os

# Point the app at the test database before any travelrag module reads settings.
_TEST_DB = os.environ.get("TRAVELRAG_TEST_DATABASE_URL")
if _TEST_DB:
    os.environ["TRAVELRAG_DATABASE_URL"] = _TEST_DB
os.environ.setdefault("TRAVELRAG_ENVIRONMENT", "test")
os.environ.setdefault("TRAVELRAG_SSE_HEARTBEAT_SECONDS", "0.5")
os.environ.setdefault("TRAVELRAG_PLANNER_TIME_LIMIT_SECONDS", "0.5")

# Uploaded files go to a throwaway directory, never to the developer's data/.
if "TRAVELRAG_MEDIA_DIR" not in os.environ:
    import tempfile

    os.environ["TRAVELRAG_MEDIA_DIR"] = tempfile.mkdtemp(prefix="travelrag-media-")
