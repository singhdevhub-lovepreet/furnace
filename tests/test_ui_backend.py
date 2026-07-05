from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from services.db.models import Artifact, GithubInstallation, Repo, User


async def seed_repo(app: FastAPI) -> UUID:
    sessionmaker = app.state.sessionmaker
    async with sessionmaker() as db:
        user = User(email="ui@example.com", plan="pro")
        installation = GithubInstallation(user=user, installation_id=1234, account_login="octo")
        repo = Repo(installation=installation, full_name="example/repo", default_branch="main")
        db.add_all([user, installation, repo])
        await db.commit()
        await db.refresh(repo)
        return repo.id


@pytest.mark.asyncio
async def test_sessions_list_orders_most_recent_first(client: tuple[FastAPI, AsyncClient]) -> None:
    app, http_client = client
    repo_id = await seed_repo(app)

    first = await http_client.post(
        "/v1/sessions",
        json={"repo_id": str(repo_id), "prompt": "first", "model_policy": {}},
    )
    assert first.status_code == 200
    await asyncio.sleep(0.02)
    second = await http_client.post(
        "/v1/sessions",
        json={"repo_id": str(repo_id), "prompt": "second", "model_policy": {}},
    )
    assert second.status_code == 200

    response = await http_client.get("/v1/sessions")
    assert response.status_code == 200
    items = response.json()
    assert [item["id"] for item in items[:2]] == [second.json()["id"], first.json()["id"]]


@pytest.mark.asyncio
async def test_artifact_content_endpoint_serves_file(client: tuple[FastAPI, AsyncClient]) -> None:
    app, http_client = client
    repo_id = await seed_repo(app)
    response = await http_client.post(
        "/v1/sessions",
        json={"repo_id": str(repo_id), "prompt": "artifact", "model_policy": {}},
    )
    assert response.status_code == 200
    session_id = UUID(response.json()["id"])

    artifacts_dir = Path(app.state.settings.artifacts_dir) / str(session_id)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifacts_dir / "preview.png"
    artifact_bytes = b"\x89PNG\r\n\x1a\nfake"
    artifact_path.write_bytes(artifact_bytes)

    async with app.state.sessionmaker() as db:
        artifact = Artifact(
            session_id=session_id,
            kind="screenshot",
            object_key=str(artifact_path),
            meta={"filename": artifact_path.name},
        )
        db.add(artifact)
        await db.commit()
        await db.refresh(artifact)
        artifact_id = artifact.id

    artifact_response = await http_client.get(
        f"/v1/sessions/{session_id}/artifacts/{artifact_id}/content"
    )
    assert artifact_response.status_code == 200
    assert artifact_response.headers["content-type"].startswith("image/png")
    assert artifact_response.content == artifact_bytes

    missing_response = await http_client.get(
        f"/v1/sessions/{session_id}/artifacts/{uuid4()}/content"
    )
    assert missing_response.status_code == 404
