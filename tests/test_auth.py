

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.database import get_session
from app.main import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.asyncio
async def test_t9_register_login_me(session):
    app.dependency_overrides[get_session] = lambda: session

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # 1. Register
        reg_resp = await client.post(
            "/v1/auth/register",
            json={"email": "alice@rakki.local", "password": "password123"},
        )
        assert reg_resp.status_code == 201
        reg_data = reg_resp.json()
        assert "token" in reg_data
        token = reg_data["token"]
        user_id = reg_data["user"]["id"]
        assert reg_data["user"]["email"] == "alice@rakki.local"

        # 2. Get /me with token
        me_resp = await client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me_resp.status_code == 200
        me_data = me_resp.json()
        assert me_data["id"] == user_id
        assert me_data["email"] == "alice@rakki.local"

        # 3. Login with credentials
        login_resp = await client.post(
            "/v1/auth/login",
            json={"email": "alice@rakki.local", "password": "password123"},
        )
        assert login_resp.status_code == 200
        login_data = login_resp.json()
        assert "token" in login_data
        assert login_data["user"]["id"] == user_id

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_t10_duplicate_register_conflict(session):
    app.dependency_overrides[get_session] = lambda: session

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp1 = await client.post(
            "/v1/auth/register",
            json={"email": "bob@rakki.local", "password": "password123"},
        )
        assert resp1.status_code == 201

        resp2 = await client.post(
            "/v1/auth/register",
            json={"email": "bob@rakki.local", "password": "password456"},
        )
        assert resp2.status_code == 409
        assert "already exists" in resp2.json()["detail"].lower()

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_t11_invalid_tokens_unauthorized(session):
    app.dependency_overrides[get_session] = lambda: session

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Without token
        resp_no_token = await client.get("/v1/auth/me")
        assert resp_no_token.status_code == 401

        # Garbage token
        resp_garbage = await client.get(
            "/v1/auth/me",
            headers={"Authorization": "Bearer not-a-valid-token-string"},
        )
        assert resp_garbage.status_code == 401

        # Wrong password login
        resp_wrong_login = await client.post(
            "/v1/auth/login",
            json={"email": "nonexistent@rakki.local", "password": "pass"},
        )
        assert resp_wrong_login.status_code == 401

    app.dependency_overrides.clear()