from pathlib import Path

import httpx
import pytest

from tests.integration.conftest import World, run_worker_until_idle
from tests.media_samples import make_video
from travelrag.core.config import get_settings
from travelrag.extraction.schema import ExtractionOutput


def _upload(path: Path, name: str | None = None) -> dict[str, tuple[str, bytes, str]]:
    return {"file": (name or path.name, path.read_bytes(), "video/mp4")}


async def test_upload_creates_video_and_job_that_probes_it(
    client: httpx.AsyncClient, auth_headers: dict[str, str], tmp_path: Path, world: World
) -> None:
    world.llm.handlers["extract"] = lambda _: ExtractionOutput(mentions=[])
    sample = make_video(tmp_path / "trip.mp4", seconds=2)

    response = await client.post("/api/videos", files=_upload(sample), headers=auth_headers)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["original_name"] == "trip.mp4"
    assert body["size_bytes"] == sample.stat().st_size
    assert body["duration_ms"] is None
    assert body["latest_job"]["kind"] == "process_video"
    assert body["latest_job"]["status"] == "pending"
    assert body["latest_job"]["video_id"] == body["id"]

    await run_worker_until_idle()

    video = (await client.get(f"/api/videos/{body['id']}", headers=auth_headers)).json()
    assert video["latest_job"]["status"] == "done"
    assert (video["width"], video["height"], video["has_audio"]) == (64, 48, True)
    assert 1900 <= video["duration_ms"] <= 2200

    listed = (await client.get("/api/videos", headers=auth_headers)).json()
    assert [v["id"] for v in listed] == [body["id"]]


async def test_duplicate_upload_returns_existing_video(
    client: httpx.AsyncClient, auth_headers: dict[str, str], tmp_path: Path
) -> None:
    sample = make_video(tmp_path / "a.mp4")
    first = await client.post("/api/videos", files=_upload(sample), headers=auth_headers)
    again = await client.post(
        "/api/videos", files=_upload(sample, "renamed.mp4"), headers=auth_headers
    )
    assert again.status_code == 409
    assert again.json()["detail"]["video_id"] == first.json()["id"]


async def test_rejects_wrong_type_and_oversized(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text_file = tmp_path / "notes.txt"
    text_file.write_text("hello")
    wrong = await client.post("/api/videos", files=_upload(text_file), headers=auth_headers)
    assert wrong.status_code == 415

    monkeypatch.setattr(get_settings(), "max_upload_bytes", 100)
    big = await client.post(
        "/api/videos", files=_upload(make_video(tmp_path / "b.mp4")), headers=auth_headers
    )
    assert big.status_code == 413


async def test_garbage_file_fails_its_job(
    client: httpx.AsyncClient, auth_headers: dict[str, str], tmp_path: Path
) -> None:
    fake = tmp_path / "fake.mp4"
    fake.write_bytes(b"not really a video" * 100)
    body = (await client.post("/api/videos", files=_upload(fake), headers=auth_headers)).json()

    await run_worker_until_idle()

    job = (await client.get(f"/api/videos/{body['id']}", headers=auth_headers)).json()["latest_job"]
    assert job["status"] == "failed"
    assert "could not be read" in job["error"]


async def test_file_download_and_delete(
    client: httpx.AsyncClient, auth_headers: dict[str, str], tmp_path: Path
) -> None:
    sample = make_video(tmp_path / "a.mp4")
    body = (await client.post("/api/videos", files=_upload(sample), headers=auth_headers)).json()
    token = auth_headers["Authorization"].removeprefix("Bearer ")

    file_url = f"/api/videos/{body['id']}/file"
    full = await client.get(file_url, params={"token": token})
    assert full.status_code == 200
    assert full.content == sample.read_bytes()
    ranged = await client.get(file_url, params={"token": token}, headers={"Range": "bytes=0-9"})
    assert ranged.status_code == 206
    assert len(ranged.content) == 10

    deleted = await client.delete(f"/api/videos/{body['id']}", headers=auth_headers)
    assert deleted.status_code == 204
    assert (await client.get(f"/api/videos/{body['id']}", headers=auth_headers)).status_code == 404
    assert (
        await client.get(f"/api/jobs/{body['latest_job']['id']}", headers=auth_headers)
    ).status_code == 404
    assert (await client.get(file_url, params={"token": token})).status_code == 404


async def test_videos_are_private(
    client: httpx.AsyncClient, auth_headers: dict[str, str], tmp_path: Path
) -> None:
    body = (
        await client.post(
            "/api/videos", files=_upload(make_video(tmp_path / "a.mp4")), headers=auth_headers
        )
    ).json()
    creds = {"email": "intruder-" + body["id"][:8] + "@example.com", "password": "secret-pass"}
    await client.post("/api/auth/register", json=creds)
    token = (await client.post("/api/auth/login", json=creds)).json()["access_token"]
    other = {"Authorization": f"Bearer {token}"}

    assert (await client.get(f"/api/videos/{body['id']}", headers=other)).status_code == 404
    assert (await client.delete(f"/api/videos/{body['id']}", headers=other)).status_code == 404
    assert (
        await client.get(f"/api/videos/{body['id']}/file", params={"token": token})
    ).status_code == 404
    assert (await client.get("/api/videos", headers=other)).json() == []
