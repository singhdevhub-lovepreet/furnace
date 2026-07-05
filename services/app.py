from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.api.routes import build_router, install_websocket_routes
from services.config import Settings
from services.db.session import build_engine, build_sessionmaker, create_all
from services.github.client import GitHubAppClient
from services.github.service import GitHubCloner, GitHubService, NoopCloner, RepoCloner
from services.scheduler.provisioner.base import MacProvisioner
from services.scheduler.provisioner.fake import FakeProvisioner
from services.sessions.orchestrator import SessionOrchestrator
from services.sessions.pubsub import SessionEventHub


def build_provisioner(settings: Settings) -> MacProvisioner:
    if settings.provisioner == "fake":
        return FakeProvisioner(queue_acquire=settings.fake_queue_acquire)
    raise NotImplementedError(f"unknown provisioner {settings.provisioner!r}")


def build_github_service(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    github_http_client: httpx.AsyncClient | None,
) -> tuple[GitHubService | None, httpx.AsyncClient | None]:
    required = (
        settings.github_app_id,
        settings.github_app_slug,
        settings.github_app_private_key,
        settings.github_webhook_secret,
    )
    if any(value is None for value in required):
        return None, github_http_client
    assert settings.github_app_id is not None
    assert settings.github_app_private_key is not None
    if github_http_client is None:
        github_http_client = httpx.AsyncClient(base_url=settings.github_api_base, timeout=30.0)
    client = GitHubAppClient(
        github_http_client,
        app_id=settings.github_app_id,
        private_key_pem=settings.github_app_private_key,
        api_base=settings.github_api_base,
    )
    service = GitHubService(sessionmaker=sessionmaker, client=client, settings=settings)
    return service, github_http_client


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = build_engine(app_settings.database_url)
        sessionmaker = build_sessionmaker(engine)
        provisioner = build_provisioner(app_settings)
        event_hub = SessionEventHub()
        github_http_client: httpx.AsyncClient | None = None
        github_service, github_http_client = build_github_service(
            app_settings,
            sessionmaker,
            github_http_client,
        )
        repo_cloner: RepoCloner = NoopCloner()
        if github_service is not None:
            repo_cloner = GitHubCloner(github=github_service, provisioner=provisioner)

        orchestrator = SessionOrchestrator(
            sessionmaker=sessionmaker,
            provisioner=provisioner,
            hub=event_hub,
            artifacts_dir=Path(app_settings.artifacts_dir),
            repo_cloner=repo_cloner,
            step_delay_seconds=app_settings.session_step_delay_seconds,
        )

        app.state.settings = app_settings
        app.state.engine = engine
        app.state.sessionmaker = sessionmaker
        app.state.provisioner = provisioner
        app.state.event_hub = event_hub
        app.state.github_service = github_service
        app.state.orchestrator = orchestrator

        if app_settings.auto_create_schema:
            await create_all(engine)

        yield
        if github_http_client is not None:
            await github_http_client.aclose()
        await engine.dispose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(build_router())
    install_websocket_routes(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
