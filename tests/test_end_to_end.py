from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from services.db.models import Artifact, Event, GithubInstallation, Repo, UsageRecord
from services.db.models import (
    Session as SessionRow,
)
from services.sessions.state_machine import SessionStatus


async def seed_repo(app: FastAPI) -> UUID:
    sessionmaker = app.state.sessionmaker
    async with sessionmaker() as db:
        user_id = app.state.auth_user_id
        installation = GithubInstallation(
            user_id=user_id,
            installation_id=1234,
            account_login="octo",
        )
        repo = Repo(installation=installation, full_name="example/repo", default_branch="main")
        db.add_all([installation, repo])
        await db.commit()
        await db.refresh(repo)
        return repo.id


async def wait_for_status(
    client: AsyncClient, session_id: UUID, target: set[SessionStatus]
) -> SessionStatus:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while True:
        response = await client.get(f"/v1/sessions/{session_id}")
        response.raise_for_status()
        payload = response.json()
        status = SessionStatus(payload["status"])
        if status in target:
            return status
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {target}")
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_session_lifecycle_succeeds(client: tuple[FastAPI, AsyncClient]) -> None:
    app, http_client = client
    repo_id = await seed_repo(app)

    response = await http_client.post(
        "/v1/sessions",
        json={
            "repo_id": str(repo_id),
            "prompt": "make it work",
            "model_policy": {"planner": "claude", "coder": "claude", "summarizer": "gpt"},
        },
    )
    assert response.status_code == 200
    session_id = UUID(response.json()["id"])

    status = await wait_for_status(http_client, session_id, {SessionStatus.SUCCEEDED})
    assert status == SessionStatus.SUCCEEDED

    sessionmaker = app.state.sessionmaker
    async with sessionmaker() as db:
        session_row = (
            await db.execute(select(SessionRow).where(SessionRow.id == session_id))
        ).scalar_one()
        assert session_row.status == SessionStatus.SUCCEEDED.value

        usage_row = (
            await db.execute(select(UsageRecord).where(UsageRecord.session_id == session_id))
        ).scalar_one()
        assert usage_row.mac_seconds == 8

        artifact_rows = (
            (await db.execute(select(Artifact).where(Artifact.session_id == session_id)))
            .scalars()
            .all()
        )
        assert {artifact.kind for artifact in artifact_rows} == {"screenshot", "video"}

        event_rows = (
            (
                await db.execute(
                    select(Event).where(Event.session_id == session_id).order_by(Event.ts)
                )
            )
            .scalars()
            .all()
        )
        statuses = [
            event.payload["status"] for event in event_rows if event.type == "status_changed"
        ]
        assert statuses[:6] == [
            SessionStatus.PROVISIONING.value,
            SessionStatus.CLONING_REPO.value,
            SessionStatus.RUNNING.value,
            SessionStatus.RECORDING.value,
            SessionStatus.RUNNING.value,
            SessionStatus.OPENING_PR.value,
        ]
        assert statuses[-1] == SessionStatus.SUCCEEDED.value

    artifacts = await http_client.get(f"/v1/sessions/{session_id}/artifacts")
    assert artifacts.status_code == 200
    artifact_payload = artifacts.json()
    assert {item["kind"] for item in artifact_payload} == {"screenshot", "video"}

    usage = await http_client.get(f"/v1/sessions/{session_id}/usage")
    assert usage.status_code == 200
    assert usage.json()["mac_seconds"] == 8


@pytest.mark.asyncio
async def test_session_cancel_mid_run(client: tuple[FastAPI, AsyncClient]) -> None:
    app, http_client = client
    repo_id = await seed_repo(app)

    response = await http_client.post(
        "/v1/sessions",
        json={
            "repo_id": str(repo_id),
            "prompt": "cancel me",
            "model_policy": {},
        },
    )
    assert response.status_code == 200
    session_id = UUID(response.json()["id"])

    await wait_for_status(
        http_client,
        session_id,
        {SessionStatus.PROVISIONING, SessionStatus.CLONING_REPO, SessionStatus.RUNNING},
    )
    cancel_response = await http_client.post(f"/v1/sessions/{session_id}/cancel")
    assert cancel_response.status_code == 200
    assert cancel_response.json()["status"] == SessionStatus.CANCELLED.value

    final_status = await wait_for_status(http_client, session_id, {SessionStatus.CANCELLED})
    assert final_status == SessionStatus.CANCELLED

    assert app.state.provisioner.release_calls
