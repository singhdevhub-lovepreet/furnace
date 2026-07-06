from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from services.db.models import GithubInstallation, Repo
from services.llm.policy import ProviderName
from tests.auth_helpers import bearer_headers, signup_user


async def seed_repo(app: FastAPI, user_id: UUID, installation_id: int, full_name: str) -> UUID:
    sessionmaker = app.state.sessionmaker
    async with sessionmaker() as db:
        installation = GithubInstallation(
            user_id=user_id,
            installation_id=installation_id,
            account_login=full_name.split("/", 1)[0],
        )
        repo = Repo(
            installation=installation,
            full_name=full_name,
            default_branch="main",
        )
        db.add_all([installation, repo])
        await db.commit()
        await db.refresh(repo)
        return repo.id


@pytest.mark.asyncio
async def test_signup_login_me_happy_and_failures(client: tuple[FastAPI, AsyncClient]) -> None:
    app, _ = client
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        signup = await anon.post(
            "/v1/auth/signup",
            json={"email": "auth@example.com", "password": "password123", "plan": "pro"},
        )
        assert signup.status_code == 200
        payload = signup.json()
        assert payload["token_type"] == "bearer"
        assert payload["user"]["email"] == "auth@example.com"
        access_token = payload["access_token"]

        me = await anon.get("/v1/auth/me", headers=bearer_headers(access_token))
        assert me.status_code == 200
        assert me.json()["email"] == "auth@example.com"

        login = await anon.post(
            "/v1/auth/login",
            json={"email": "auth@example.com", "password": "password123"},
        )
        assert login.status_code == 200
        assert login.json()["user"]["id"] == payload["user"]["id"]

        duplicate = await anon.post(
            "/v1/auth/signup",
            json={"email": "auth@example.com", "password": "password123"},
        )
        assert duplicate.status_code == 409

        bad_creds = await anon.post(
            "/v1/auth/login",
            json={"email": "auth@example.com", "password": "wrong-password"},
        )
        assert bad_creds.status_code == 401


@pytest.mark.asyncio
async def test_unauthenticated_request_requires_bearer(client: tuple[FastAPI, AsyncClient]) -> None:
    app, _ = client
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        response = await anon.get("/v1/keys")
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_cross_tenant_isolation(client: tuple[FastAPI, AsyncClient]) -> None:
    app, http_client = client
    a_user_id = app.state.auth_user_id
    a_repo_id = await seed_repo(app, a_user_id, 1001, "alice/repo")
    key_response = await http_client.post(
        "/v1/keys",
        json={
            "provider": ProviderName.OPENAI.value,
            "label": "primary",
            "key": "sk-alice",
        },
    )
    assert key_response.status_code == 200
    a_key_id = key_response.json()["id"]

    session_response = await http_client.post(
        "/v1/sessions",
        json={
            "repo_id": str(a_repo_id),
            "prompt": "build it",
            "model_policy": {},
        },
    )
    assert session_response.status_code == 200
    a_session_id = UUID(session_response.json()["id"])

    deadline = asyncio.get_running_loop().time() + 5.0
    while True:
        row_response = await http_client.get(f"/v1/sessions/{a_session_id}")
        row_response.raise_for_status()
        if row_response.json()["status"] == "SUCCEEDED":
            break
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for A session")
        await asyncio.sleep(0.02)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        b_user_id, b_token, _ = await signup_user(
            anon,
            "tenant-b@example.com",
            "password123",
        )

    http_client.headers["Authorization"] = f"Bearer {b_token}"
    await seed_repo(app, b_user_id, 1002, "bob/repo")

    sessions_response = await http_client.get("/v1/sessions")
    assert sessions_response.status_code == 200
    assert a_session_id not in {UUID(item["id"]) for item in sessions_response.json()}

    assert (await http_client.get(f"/v1/sessions/{a_session_id}")).status_code == 404
    assert (await http_client.post(f"/v1/sessions/{a_session_id}/cancel")).status_code == 404
    assert (await http_client.get(f"/v1/sessions/{a_session_id}/artifacts")).status_code == 404
    assert (await http_client.get(f"/v1/sessions/{a_session_id}/usage")).status_code == 404

    artifacts_response = await http_client.get(
        f"/v1/sessions/{a_session_id}/artifacts/{UUID(int=1)}/content"
    )
    assert artifacts_response.status_code == 404

    keys_response = await http_client.get("/v1/keys")
    assert keys_response.status_code == 200
    assert keys_response.json() == []
    assert (await http_client.delete(f"/v1/keys/{a_key_id}")).status_code == 404

    repos_response = await http_client.get("/v1/repos")
    assert repos_response.status_code == 200
    repo_names = {item["full_name"] for item in repos_response.json()}
    assert repo_names == {"bob/repo"}
