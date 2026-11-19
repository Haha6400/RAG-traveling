import asyncio
import uuid

import httpx

from tests.integration.conftest import run_worker_until_idle
from travelrag.core.config import get_settings
from travelrag.jobs.adapters.pg_events import PgListenJobEventSubscriber
from travelrag.jobs.domain import JobEvent


async def _create(client: httpx.AsyncClient, headers: dict[str, str], **options: object) -> str:
    response = await client.post(
        "/api/jobs/dummy", json={"step_seconds": 0, **options}, headers=headers
    )
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "pending"
    return str(response.json()["id"])


async def _status(client: httpx.AsyncClient, headers: dict[str, str], job_id: str) -> dict:  # type: ignore[type-arg]
    response = await client.get(f"/api/jobs/{job_id}", headers=headers)
    assert response.status_code == 200
    return dict(response.json())


async def test_dummy_job_runs_to_done(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    job_id = await _create(client, auth_headers)
    await run_worker_until_idle()
    job = await _status(client, auth_headers, job_id)
    assert (job["status"], job["stage"], job["progress"]) == ("done", "index", 100)

    listed = (await client.get("/api/jobs", headers=auth_headers)).json()
    assert [j["id"] for j in listed] == [job_id]


async def test_failure_then_retry(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    job_id = await _create(client, auth_headers, fail_at="extract")
    await run_worker_until_idle()
    job = await _status(client, auth_headers, job_id)
    assert job["status"] == "failed"
    assert "extract" in job["error"]

    retried = await client.post(f"/api/jobs/{job_id}/retry", headers=auth_headers)
    assert retried.status_code == 202
    assert retried.json()["status"] == "pending"

    # Retrying a pending job is not allowed.
    again = await client.post(f"/api/jobs/{job_id}/retry", headers=auth_headers)
    assert again.status_code == 409


async def test_review_then_approve(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    job_id = await _create(client, auth_headers, needs_review=True)
    await run_worker_until_idle()
    assert (await _status(client, auth_headers, job_id))["status"] == "needs_review"

    approved = await client.post(f"/api/jobs/{job_id}/review/approve", headers=auth_headers)
    assert approved.status_code == 200
    assert approved.json()["status"] == "done"


async def test_jobs_are_private(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    job_id = await _create(client, auth_headers)
    creds = {"email": f"other-{uuid.uuid4().hex[:8]}@example.com", "password": "secret-pass"}
    await client.post("/api/auth/register", json=creds)
    token = (await client.post("/api/auth/login", json=creds)).json()["access_token"]
    other = {"Authorization": f"Bearer {token}"}
    assert (await client.get(f"/api/jobs/{job_id}", headers=other)).status_code == 404


async def test_state_changes_are_notified(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    settings = get_settings()
    subscriber = PgListenJobEventSubscriber(settings.psycopg_conninfo, heartbeat_seconds=0.2)
    async with subscriber.subscribe() as events:
        job_id = await _create(client, auth_headers)
        await run_worker_until_idle()

        seen: list[JobEvent] = []

        async def collect() -> None:
            async for event in events:
                if event is None:
                    if seen and seen[-1].status == "done":
                        return
                    continue
                if str(event.job_id) == job_id:
                    seen.append(event)

        await asyncio.wait_for(collect(), timeout=10)

    statuses = [e.status.value for e in seen]
    assert statuses[0] == "pending"
    assert "processing" in statuses
    assert statuses[-1] == "done"
