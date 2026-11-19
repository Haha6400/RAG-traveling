import uuid

import httpx


async def test_register_login_me(client: httpx.AsyncClient) -> None:
    email = f"User-{uuid.uuid4().hex[:8]}@Example.com"
    creds = {"email": email, "password": "secret-pass"}

    assert (await client.post("/api/auth/register", json=creds)).status_code == 201
    assert (await client.post("/api/auth/register", json=creds)).status_code == 409

    bad = await client.post("/api/auth/login", json={**creds, "password": "wrong-pass"})
    assert bad.status_code == 401

    token = (await client.post("/api/auth/login", json=creds)).json()["access_token"]
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == email.lower()


async def test_protected_routes_need_a_token(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/jobs")).status_code == 401
    garbage = {"Authorization": "Bearer not-a-token"}
    assert (await client.get("/api/jobs", headers=garbage)).status_code == 401


async def test_health(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/health")).json() == {"status": "ok", "database": "ok"}
