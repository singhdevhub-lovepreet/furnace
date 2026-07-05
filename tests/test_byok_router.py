from __future__ import annotations

import base64
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from services.app import create_app
from services.config import Settings
from services.db.models import (
    Event,
    GithubInstallation,
    LlmKey,
    Repo,
    UsageRecord,
    User,
)
from services.db.models import (
    Session as SessionRow,
)
from services.llm.policy import ModelPolicy, ModelSelection, ProviderName, resolve
from services.llm.router import CompletionResult, ModelRouter
from services.vault.key_vault import KeyVault, VaultError


def master_key_b64() -> str:
    return base64.b64encode(os.urandom(32)).decode("ascii")


@pytest.fixture
def vault() -> KeyVault:
    return KeyVault.from_base64_key(master_key_b64())


@pytest_asyncio.fixture
async def test_app(tmp_path: Path) -> AsyncGenerator[FastAPI, None]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'byok.db'}",
        artifacts_dir=str(tmp_path / "artifacts"),
        auto_create_schema=True,
        master_encryption_key=master_key_b64(),
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app


async def seed_user(app: FastAPI) -> UUID:
    sessionmaker = app.state.sessionmaker
    async with sessionmaker() as db:
        user = User(email="byok@example.com", plan="pro")
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user.id


async def seed_repo(app: FastAPI, user_id: UUID) -> UUID:
    sessionmaker = app.state.sessionmaker
    async with sessionmaker() as db:
        installation = GithubInstallation(
            user_id=user_id,
            installation_id=555,
            account_login="octo",
        )
        repo = Repo(
            installation=installation,
            full_name="octo/example",
            default_branch="main",
        )
        db.add_all([installation, repo])
        await db.commit()
        await db.refresh(repo)
        return repo.id


@pytest.mark.asyncio
async def test_vault_roundtrip_and_tamper(vault: KeyVault) -> None:
    ciphertext_one = vault.encrypt("secret-token")
    ciphertext_two = vault.encrypt("secret-token")
    assert ciphertext_one != ciphertext_two
    assert vault.decrypt(ciphertext_one) == "secret-token"
    assert vault.decrypt(ciphertext_two) == "secret-token"
    tampered = bytearray(ciphertext_one)
    tampered[-1] ^= 0x01
    with pytest.raises(VaultError):
        vault.decrypt(bytes(tampered))
    other_vault = KeyVault.from_base64_key(master_key_b64())
    with pytest.raises(VaultError):
        other_vault.decrypt(ciphertext_one)


@pytest.mark.asyncio
async def test_key_crud_hides_secret_and_persists_ciphertext(test_app: FastAPI) -> None:
    await seed_user(test_app)
    plaintext = "sk-test-secret"
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        create_response = await client.post(
            "/v1/keys",
            json={"provider": ProviderName.OPENAI.value, "label": "main", "key": plaintext},
        )
        assert create_response.status_code == 200
        payload = create_response.json()
        assert payload["label"] == "main"
        assert payload["provider"] == ProviderName.OPENAI.value
        assert "key" not in payload

        async with test_app.state.sessionmaker() as db:
            key_row = (
                await db.execute(select(LlmKey).where(LlmKey.id == UUID(payload["id"])))
            ).scalar_one()
            assert key_row.enc_key != plaintext.encode("utf-8")
            assert test_app.state.key_vault.decrypt(key_row.enc_key) == plaintext

        list_response = await client.get("/v1/keys")
        assert list_response.status_code == 200
        listed = list_response.json()
        assert len(listed) == 1
        assert listed[0]["label"] == "main"

        delete_response = await client.delete(f"/v1/keys/{payload['id']}")
        assert delete_response.status_code == 204

        list_response = await client.get("/v1/keys")
        assert list_response.json() == []


@pytest.mark.asyncio
async def test_model_policy_validation_and_models_catalog(test_app: FastAPI) -> None:
    user_id = await seed_user(test_app)
    repo_id = await seed_repo(test_app, user_id)
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        valid_response = await client.post(
            "/v1/sessions",
            json={
                "repo_id": str(repo_id),
                "prompt": "do the thing",
                "model_policy": {
                    "default": {"provider": "openrouter", "model": "openai/gpt-4o-mini"},
                    "roles": {
                        "coder": {"provider": "anthropic", "model": "claude-3-5-sonnet-latest"},
                    },
                },
            },
        )
        assert valid_response.status_code == 200

        invalid_response = await client.post(
            "/v1/sessions",
            json={
                "repo_id": str(repo_id),
                "prompt": "bad policy",
                "model_policy": {"default": {"provider": "bogus", "model": ""}},
            },
        )
        assert invalid_response.status_code == 422

        catalog_response = await client.get("/v1/models")
        assert catalog_response.status_code == 200
        catalog = catalog_response.json()
        providers = {item["provider"] for item in catalog["providers"]}
        assert ProviderName.OPENROUTER.value in providers
        assert ProviderName.OPENAI.value in providers

    policy = ModelPolicy.model_validate(
        {
            "default": {"provider": "openai", "model": "gpt-4o-mini"},
            "roles": {"coder": {"provider": "anthropic", "model": "claude-3-5-sonnet-latest"}},
        }
    )
    assert resolve(policy, "coder") == ModelSelection(
        provider=ProviderName.ANTHROPIC,
        model="claude-3-5-sonnet-latest",
    )
    assert resolve(policy, "reviewer") == ModelSelection(
        provider=ProviderName.OPENAI,
        model="gpt-4o-mini",
    )


@pytest.mark.asyncio
async def test_model_router_decrypts_and_counts_tokens(test_app: FastAPI) -> None:
    user_id = await seed_user(test_app)
    sessionmaker = test_app.state.sessionmaker
    secret = "sk-byok-secret"
    async with sessionmaker() as db:
        key_row = LlmKey(
            user_id=user_id,
            provider=ProviderName.OPENAI.value,
            label="main",
            enc_key=test_app.state.key_vault.encrypt(secret),
        )
        repo = Repo(
            installation=GithubInstallation(
                user_id=user_id,
                installation_id=777,
                account_login="octo",
            ),
            full_name="octo/example",
            default_branch="main",
        )
        session_row = SessionRow(
            user_id=user_id,
            repo=repo,
            prompt="write code",
            status="RUNNING",
            branch="main",
            model_policy={
                "default": {"provider": "openai", "model": "gpt-4o-mini"},
                "roles": {
                    "coder": {"provider": "openai", "model": "gpt-4o-mini"},
                },
            },
        )
        db.add_all([key_row, repo, session_row])
        await db.commit()
        await db.refresh(session_row)

    calls: list[dict[str, object]] = []

    async def fake_completion(
        provider: str,
        model: str,
        messages: list[dict[str, str]],
        api_key: str,
    ) -> CompletionResult:
        calls.append(
            {
                "provider": provider,
                "model": model,
                "messages": messages,
                "api_key": api_key,
            }
        )
        return CompletionResult(text="assistant reply", prompt_tokens=11, completion_tokens=7)

    router = ModelRouter(
        sessionmaker=sessionmaker,
        key_vault=test_app.state.key_vault,
        completion_fn=fake_completion,
    )
    text = await router.complete(
        session_row.id,
        "coder",
        [{"role": "user", "content": "hello"}],
    )
    assert text == "assistant reply"
    assert calls == [
        {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hello"}],
            "api_key": secret,
        }
    ]

    async with sessionmaker() as db:
        usage = (
            await db.execute(select(UsageRecord).where(UsageRecord.session_id == session_row.id))
        ).scalar_one()
        assert usage.prompt_tokens == 11
        assert usage.completion_tokens == 7
        events = (
            (await db.execute(select(Event).where(Event.session_id == session_row.id)))
            .scalars()
            .all()
        )
        assert events == []
        key_rows = (
            (await db.execute(select(LlmKey).where(LlmKey.user_id == user_id))).scalars().all()
        )
        assert all(secret not in key.enc_key.decode("latin1", errors="ignore") for key in key_rows)
