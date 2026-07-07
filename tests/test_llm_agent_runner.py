from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.agent.base import AgentContext
from services.agent.llm import LlmAgentRunner
from services.app import create_app
from services.auth.password import hash_password
from services.config import Settings
from services.db.models import GithubInstallation, LlmKey, Repo, UsageRecord, User
from services.db.models import Session as SessionRow
from services.llm.policy import ProviderName
from services.llm.router import CompletionResult, ModelRouter
from services.scheduler.provisioner.base import SessionHandle
from services.scheduler.provisioner.fake import FakeProvisioner
from services.sessions.orchestrator import SessionOrchestrator
from services.vault.key_vault import KeyVault


def master_key_b64() -> str:
    return base64.b64encode(os.urandom(32)).decode("ascii")


@pytest_asyncio.fixture
async def test_app(tmp_path: Path) -> AsyncGenerator[FastAPI, None]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'llm-agent.db'}",
        artifacts_dir=str(tmp_path / "artifacts"),
        auto_create_schema=True,
        agent_runner="fake",
        auth_jwt_secret="test-jwt-secret-0123456789abcdef012345",
        master_encryption_key=master_key_b64(),
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app


async def seed_user(app: FastAPI) -> UUID:
    sessionmaker: async_sessionmaker[AsyncSession] = app.state.sessionmaker
    async with sessionmaker() as db:
        user = User(
            email="agent@example.com",
            plan="pro",
            password_hash=hash_password("password123"),
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user.id


async def seed_session(
    app: FastAPI,
    user_id: UUID,
    model_policy: dict[str, object],
) -> tuple[UUID, UUID]:
    sessionmaker: async_sessionmaker[AsyncSession] = app.state.sessionmaker
    key_vault: KeyVault = app.state.key_vault
    async with sessionmaker() as db:
        installation = GithubInstallation(
            user_id=user_id,
            installation_id=9901,
            account_login="octo",
        )
        repo = Repo(
            installation=installation,
            full_name="octo/example",
            default_branch="main",
        )
        key = LlmKey(
            user_id=user_id,
            provider=ProviderName.OPENROUTER.value,
            label="main",
            enc_key=key_vault.encrypt("sk-openrouter"),
        )
        session = SessionRow(
            user_id=user_id,
            repo=repo,
            prompt="update the session UI",
            status="RUNNING",
            branch="main",
            model_policy=model_policy,
        )
        db.add_all([installation, repo, key, session])
        await db.commit()
        await db.refresh(session)
        return session.id, repo.id


@dataclass(slots=True)
class ScriptedCompletionFn:
    responses: list[str]
    prompt_tokens: int = 5
    completion_tokens: int = 7
    calls: list[dict[str, object]] = field(default_factory=list)
    index: int = 0

    async def __call__(
        self,
        provider: str,
        model: str,
        messages: list[dict[str, str]],
        api_key: str,
    ) -> CompletionResult:
        response_index = self.index
        if response_index >= len(self.responses):
            response_index = len(self.responses) - 1
        response = self.responses[response_index]
        self.index += 1
        self.calls.append(
            {
                "provider": provider,
                "model": model,
                "messages": [dict(message) for message in messages],
                "api_key": api_key,
            }
        )
        return CompletionResult(
            text=response,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
        )


def build_context(session_id: UUID, handle_id: int = 1) -> AgentContext:
    return AgentContext(
        session_id=session_id,
        prompt="update the session UI",
        repo_full_name="octo/example",
        handle=SessionHandle(
            id=UUID(int=handle_id),
            provider="fake",
            created_at=datetime.now(UTC),
            tool_endpoint="in-memory://fake",
        ),
    )


@pytest.mark.asyncio
async def test_llm_agent_runner_happy_path(test_app: FastAPI) -> None:
    user_id = await seed_user(test_app)
    model_policy = {
        "default": {"provider": ProviderName.OPENROUTER.value, "model": "gpt-4o-mini"},
        "roles": {
            "coder": {"provider": ProviderName.OPENROUTER.value, "model": "gpt-4o-mini"},
        },
    }
    session_id, _repo_id = await seed_session(test_app, user_id, model_policy)
    completion = ScriptedCompletionFn(
        responses=[
            '{"plan":["inspect repository","apply change","build","finish"]}',
            '{"tool":"read_file","args":{"path":"Sources/ContentView.swift"}}',
            (
                '{"tool":"write_file","args":{"path":"Sources/ContentView.swift",'
                '"content":"import SwiftUI\\n"}}'
            ),
            '{"tool":"finish","summary":"done","changed_files":["Sources/ContentView.swift"]}',
        ]
    )
    sessionmaker: async_sessionmaker[AsyncSession] = test_app.state.sessionmaker
    key_vault: KeyVault = test_app.state.key_vault
    router = ModelRouter(sessionmaker=sessionmaker, key_vault=key_vault, completion_fn=completion)
    provisioner = FakeProvisioner(max_slots=1)
    runner = LlmAgentRunner(
        router=router,
        provisioner=provisioner,
        max_steps=8,
        command_timeout_seconds=30,
    )
    events: list[tuple[str, dict[str, object]]] = []
    cancel_event = asyncio.Event()

    async def emit(event_type: str, payload: dict[str, object]) -> None:
        events.append((event_type, payload))

    result = await runner.run(build_context(session_id), emit=emit, cancel_event=cancel_event)

    assert result.success is True
    assert result.summary == "done"
    assert result.changed_files == ["Sources/ContentView.swift"]
    assert result.steps == len(events)
    assert [event_type for event_type, _ in events] == [
        "agent_plan",
        "agent_action",
        "agent_observation",
        "agent_action",
        "agent_observation",
        "agent_message",
    ]
    assert "agent_result" not in [event_type for event_type, _ in events]
    assert any(call["api_key"] == "sk-openrouter" for call in completion.calls)
    assert len(provisioner.put_files) == 1
    put_handle, put_path, put_content = provisioner.put_files[0]
    assert put_handle.id == build_context(session_id).handle.id
    assert put_handle.provider == "fake"
    assert put_path == "Sources/ContentView.swift"
    assert put_content == b"import SwiftUI\n"

    async with sessionmaker() as db:
        usage = (
            await db.execute(select(UsageRecord).where(UsageRecord.session_id == session_id))
        ).scalar_one()
        assert usage.prompt_tokens == 20
        assert usage.completion_tokens == 28


@pytest.mark.asyncio
async def test_llm_agent_runner_cancellation_before_start(test_app: FastAPI) -> None:
    user_id = await seed_user(test_app)
    session_id, _repo_id = await seed_session(
        test_app,
        user_id,
        {
            "default": {"provider": ProviderName.OPENROUTER.value, "model": "gpt-4o-mini"},
            "roles": {},
        },
    )
    completion = ScriptedCompletionFn(
        responses=['{"plan":["inspect repository"]}', '{"tool":"finish","summary":"done"}']
    )
    sessionmaker: async_sessionmaker[AsyncSession] = test_app.state.sessionmaker
    key_vault: KeyVault = test_app.state.key_vault
    router = ModelRouter(sessionmaker=sessionmaker, key_vault=key_vault, completion_fn=completion)
    runner = LlmAgentRunner(router=router, provisioner=FakeProvisioner())
    cancel_event = asyncio.Event()
    cancel_event.set()

    events: list[tuple[str, dict[str, object]]] = []

    async def emit(event_type: str, payload: dict[str, object]) -> None:
        events.append((event_type, payload))

    result = await runner.run(build_context(session_id), emit=emit, cancel_event=cancel_event)

    assert result.success is False
    assert result.summary == "cancelled"
    assert result.steps == 0
    assert events == []
    assert completion.calls == []


@pytest.mark.asyncio
async def test_llm_agent_runner_step_limit(test_app: FastAPI) -> None:
    user_id = await seed_user(test_app)
    session_id, _repo_id = await seed_session(
        test_app,
        user_id,
        {
            "default": {"provider": ProviderName.OPENROUTER.value, "model": "gpt-4o-mini"},
            "roles": {},
        },
    )
    completion = ScriptedCompletionFn(
        responses=[
            '{"plan":["inspect repository","keep going"]}',
            '{"tool":"read_file","args":{"path":"Sources/ContentView.swift"}}',
            '{"tool":"read_file","args":{"path":"Sources/ContentView.swift"}}',
            '{"tool":"read_file","args":{"path":"Sources/ContentView.swift"}}',
            '{"tool":"read_file","args":{"path":"Sources/ContentView.swift"}}',
        ]
    )
    sessionmaker: async_sessionmaker[AsyncSession] = test_app.state.sessionmaker
    key_vault: KeyVault = test_app.state.key_vault
    router = ModelRouter(sessionmaker=sessionmaker, key_vault=key_vault, completion_fn=completion)
    runner = LlmAgentRunner(router=router, provisioner=FakeProvisioner(), max_steps=3)
    events: list[tuple[str, dict[str, object]]] = []
    cancel_event = asyncio.Event()

    async def emit(event_type: str, payload: dict[str, object]) -> None:
        events.append((event_type, payload))

    result = await runner.run(build_context(session_id), emit=emit, cancel_event=cancel_event)

    assert result.success is False
    assert result.summary == "reached step limit without finishing"
    assert result.steps == len(events)
    assert [event_type for event_type, _ in events][-1] == "agent_message"
    assert len(completion.calls) == 4


@pytest.mark.asyncio
async def test_llm_agent_runner_recovers_from_parse_failure(test_app: FastAPI) -> None:
    user_id = await seed_user(test_app)
    session_id, _repo_id = await seed_session(
        test_app,
        user_id,
        {
            "default": {"provider": ProviderName.OPENROUTER.value, "model": "gpt-4o-mini"},
            "roles": {},
        },
    )
    completion = ScriptedCompletionFn(
        responses=[
            '{"plan":["inspect repository","finish"]}',
            "not json at all",
            '{"tool":"finish","summary":"recovered"}',
        ]
    )
    sessionmaker: async_sessionmaker[AsyncSession] = test_app.state.sessionmaker
    key_vault: KeyVault = test_app.state.key_vault
    router = ModelRouter(sessionmaker=sessionmaker, key_vault=key_vault, completion_fn=completion)
    runner = LlmAgentRunner(router=router, provisioner=FakeProvisioner())
    events: list[tuple[str, dict[str, object]]] = []
    cancel_event = asyncio.Event()

    async def emit(event_type: str, payload: dict[str, object]) -> None:
        events.append((event_type, payload))

    result = await runner.run(build_context(session_id), emit=emit, cancel_event=cancel_event)

    assert result.success is True
    assert result.summary == "recovered"
    assert result.steps == len(events)
    assert [event_type for event_type, _ in events] == [
        "agent_plan",
        "agent_message",
    ]
    second_call = completion.calls[2]
    messages = second_call["messages"]
    assert isinstance(messages, list)
    last_message = messages[-1]
    assert isinstance(last_message, dict)
    content = last_message["content"]
    assert isinstance(content, str)
    assert "not valid JSON matching a tool schema" in content


@pytest.mark.asyncio
async def test_orchestrator_usage_creation_preserves_existing_tokens(
    test_app: FastAPI,
) -> None:
    user_id = await seed_user(test_app)
    session_id, _repo_id = await seed_session(
        test_app,
        user_id,
        {
            "default": {"provider": ProviderName.OPENROUTER.value, "model": "gpt-4o-mini"},
            "roles": {},
        },
    )
    sessionmaker: async_sessionmaker[AsyncSession] = test_app.state.sessionmaker
    async with sessionmaker() as db:
        existing = UsageRecord(
            session_id=session_id,
            mac_seconds=5,
            prompt_tokens=13,
            completion_tokens=17,
            mac_cost_usd=Decimal("0.0000"),
        )
        db.add(existing)
        await db.commit()

    orchestrator: SessionOrchestrator = test_app.state.orchestrator
    await orchestrator._create_usage(session_id, mac_seconds=8)

    async with sessionmaker() as db:
        usage_rows = (
            (await db.execute(select(UsageRecord).where(UsageRecord.session_id == session_id)))
            .scalars()
            .all()
        )

    assert len(usage_rows) == 1
    usage = usage_rows[0]
    assert usage.mac_seconds == 13
    assert usage.prompt_tokens == 13
    assert usage.completion_tokens == 17
