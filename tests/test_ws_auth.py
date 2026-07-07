from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from services.app import create_app
from services.config import Settings

TEST_AUTH_SECRET = "test-jwt-secret-0123456789abcdef012345"


@pytest.fixture
def websocket_app(tmp_path: Path) -> FastAPI:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'ws-auth.db'}",
        artifacts_dir=str(tmp_path / "artifacts"),
        auto_create_schema=True,
        agent_runner="fake",
        auth_jwt_secret=TEST_AUTH_SECRET,
    )
    return create_app(settings)


def test_websocket_rejects_missing_and_invalid_token(websocket_app: FastAPI) -> None:
    with TestClient(websocket_app) as client:
        with pytest.raises(WebSocketDisconnect) as missing_exc:
            with client.websocket_connect(f"/ws/sessions/{uuid4()}"):
                pass
        assert missing_exc.value.code == 4401

        with pytest.raises(WebSocketDisconnect) as invalid_exc:
            with client.websocket_connect(f"/ws/sessions/{uuid4()}?token=not-a-token"):
                pass
        assert invalid_exc.value.code == 4401
