from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from services.app import create_app
from services.config import Settings
from tests.auth_helpers import signup_user

TEST_AUTH_SECRET = "test-jwt-secret-0123456789abcdef012345"
TEST_MASTER_ENCRYPTION_KEY = "3Gpj/b3hxFS9uyDR2f96QvMElqOqu5HGWwFmg4vYjEM="


@pytest.fixture
def temp_paths(tmp_path: Path) -> dict[str, Path]:
    db_path = tmp_path / "furnace.db"
    artifacts_dir = tmp_path / "artifacts"
    return {"db": db_path, "artifacts": artifacts_dir}


@pytest_asyncio.fixture
async def client(
    temp_paths: dict[str, Path],
) -> AsyncGenerator[tuple[FastAPI, AsyncClient], None]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{temp_paths['db']}",
        artifacts_dir=str(temp_paths["artifacts"]),
        session_step_delay_seconds=0.02,
        auto_create_schema=True,
        agent_runner="fake",
        auth_jwt_secret=TEST_AUTH_SECRET,
        master_encryption_key=TEST_MASTER_ENCRYPTION_KEY,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            user_id, token, user = await signup_user(
                http_client,
                "user@example.com",
                "password123",
                plan="pro",
            )
            http_client.headers["Authorization"] = f"Bearer {token}"
            app.state.auth_user_id = user_id
            app.state.auth_token = token
            app.state.auth_user = user
            yield app, http_client
