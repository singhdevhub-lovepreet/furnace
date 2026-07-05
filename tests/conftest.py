from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from services.app import create_app
from services.config import Settings


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
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield app, http_client
