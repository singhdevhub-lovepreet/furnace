from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from services.agent.base import AgentContext
from services.agent.fake import FakeAgentRunner
from services.db.models import Event, GithubInstallation, Repo, User
from services.scheduler.provisioner.base import SessionHandle
from services.sessions.state_machine import SessionStatus


def build_handle(identifier: int) -> SessionHandle:
    return SessionHandle(
        id=UUID(int=identifier),
        provider="fake",
        created_at=datetime.now(UTC),
        tool_endpoint="in-memory://fake",
    )


@pytest.mark.asyncio
async def test_fake_agent_runner_emits_deterministic_script() -> None:
    runner = FakeAgentRunner(step_delay_seconds=0.0)
    events: list[tuple[str, dict[str, object]]] = []
    cancel_event = asyncio.Event()
    ctx = AgentContext(
        session_id=UUID(int=1),
        prompt="update the session UI and keep it deterministic",
        repo_full_name="octo/example",
        handle=build_handle(2),
    )

    async def emit(event_type: str, payload: dict[str, object]) -> None:
        events.append((event_type, payload))

    result = await runner.run(ctx, emit=emit, cancel_event=cancel_event)

    assert result.success is True
    assert result.steps > 0
    assert result.changed_files == ["Sources/ContentView.swift"]
    assert result.summary == "Applied change for prompt"
    assert events[0][0] == "agent_plan"
    assert events[-1][0] == "agent_message"
    plan = events[0][1]["plan"]
    assert isinstance(plan, list)
    assert "apply change for prompt" in str(plan[1])
    assert any(token in str(plan[1]) for token in ctx.prompt.split())
    assert any(event_type == "agent_action" for event_type, _ in events)
    assert any(event_type == "agent_observation" for event_type, _ in events)
    assert any(event_type == "agent_message" for event_type, _ in events)


@pytest.mark.asyncio
async def test_fake_agent_runner_honors_cancel_event() -> None:
    runner = FakeAgentRunner(step_delay_seconds=0.0)
    events: list[tuple[str, dict[str, object]]] = []
    cancel_event = asyncio.Event()
    cancel_event.set()
    ctx = AgentContext(
        session_id=UUID(int=3),
        prompt="cancel me",
        repo_full_name="octo/example",
        handle=build_handle(4),
    )

    async def emit(event_type: str, payload: dict[str, object]) -> None:
        events.append((event_type, payload))

    result = await runner.run(ctx, emit=emit, cancel_event=cancel_event)

    assert result.success is False
    assert result.summary == "cancelled"
    assert result.steps == 1
    assert len(events) == 1
    assert events[0][0] == "agent_plan"


async def seed_repo(app: FastAPI) -> UUID:
    sessionmaker = app.state.sessionmaker
    async with sessionmaker() as db:
        user = User(email="agent@example.com", plan="pro")
        installation = GithubInstallation(user=user, installation_id=1234, account_login="octo")
        repo = Repo(installation=installation, full_name="example/repo", default_branch="main")
        db.add_all([user, installation, repo])
        await db.commit()
        await db.refresh(repo)
        return repo.id


@pytest.mark.asyncio
async def test_session_orchestrator_persists_agent_events(
    client: tuple[FastAPI, AsyncClient],
) -> None:
    app, http_client = client
    repo_id = await seed_repo(app)

    response = await http_client.post(
        "/v1/sessions",
        json={"repo_id": str(repo_id), "prompt": "refresh the session UI", "model_policy": {}},
    )
    assert response.status_code == 200
    session_id = UUID(response.json()["id"])

    deadline = asyncio.get_running_loop().time() + 5.0
    while True:
        session_response = await http_client.get(f"/v1/sessions/{session_id}")
        session_response.raise_for_status()
        if session_response.json()["status"] == SessionStatus.SUCCEEDED.value:
            break
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for session to complete")
        await asyncio.sleep(0.05)

    async with app.state.sessionmaker() as db:
        event_rows = (
            (
                await db.execute(
                    select(Event).where(Event.session_id == session_id).order_by(Event.ts)
                )
            )
            .scalars()
            .all()
        )

    event_types = [event.type for event in event_rows]
    assert "agent_plan" in event_types
    assert "agent_action" in event_types
    assert "agent_observation" in event_types
    assert "agent_message" in event_types
    assert event_types.index("agent_plan") < event_types.index("agent_message")
    assert any(
        event.type == "status_changed" and event.payload["status"] == SessionStatus.SUCCEEDED.value
        for event in event_rows
    )
