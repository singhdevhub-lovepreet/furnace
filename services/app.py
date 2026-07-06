from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.agent.base import AgentRunner
from services.agent.fake import FakeAgentRunner
from services.agent.llm import LlmAgentRunner
from services.api.routes import build_router, install_websocket_routes
from services.config import Settings
from services.db.session import build_engine, build_sessionmaker, create_all
from services.github.client import GitHubAppClient
from services.github.service import GitHubCloner, GitHubService, NoopCloner, RepoCloner
from services.llm.router import ModelRouter
from services.scheduler.pool import PoolController
from services.scheduler.provisioner.base import MacProvisioner
from services.scheduler.provisioner.fake import FakeProvisioner
from services.sessions.orchestrator import SessionOrchestrator
from services.sessions.pubsub import SessionEventHub
from services.vault.key_vault import KeyVault


def build_provisioner(settings: Settings) -> MacProvisioner:
    if settings.provisioner == "fake":
        return FakeProvisioner(
            queue_acquire=settings.fake_queue_acquire,
            max_slots=settings.fake_max_slots,
        )
    raise NotImplementedError(f"unknown provisioner {settings.provisioner!r}")


def build_pool_controller(
    settings: Settings,
    provisioner: MacProvisioner,
) -> PoolController:
    _ = settings
    return PoolController(
        provisioner=provisioner,
        capacity_override=settings.pool_capacity_override,
        estimated_session_seconds=settings.pool_estimated_session_seconds,
        scale_up_threshold=settings.pool_scale_up_threshold,
    )


def build_key_vault(settings: Settings) -> KeyVault | None:
    if settings.master_encryption_key is None:
        return None
    return KeyVault.from_base64_key(settings.master_encryption_key)


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


def build_model_router(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    key_vault: KeyVault | None,
) -> ModelRouter | None:
    if key_vault is None:
        return None
    _ = settings
    return ModelRouter(sessionmaker=sessionmaker, key_vault=key_vault)


def build_agent_runner(
    settings: Settings,
    model_router: ModelRouter | None,
    provisioner: MacProvisioner,
) -> AgentRunner:
    if settings.agent_runner == "fake":
        return FakeAgentRunner(step_delay_seconds=settings.session_step_delay_seconds)
    if settings.agent_runner == "llm":
        if model_router is None:
            raise RuntimeError(
                "FURNACE_AGENT_RUNNER=llm requires a ModelRouter; set "
                "FURNACE_MASTER_ENCRYPTION_KEY so BYOK routing can be constructed"
            )
        return LlmAgentRunner(
            router=model_router,
            provisioner=provisioner,
            max_steps=settings.agent_max_steps,
            command_timeout_seconds=settings.agent_command_timeout_seconds,
        )
    raise NotImplementedError(f"unknown agent runner {settings.agent_runner!r}")


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = build_engine(app_settings.database_url)
        sessionmaker = build_sessionmaker(engine)
        provisioner = build_provisioner(app_settings)
        pool_controller = build_pool_controller(app_settings, provisioner)
        event_hub = SessionEventHub()
        github_http_client: httpx.AsyncClient | None = None
        github_service, github_http_client = build_github_service(
            app_settings,
            sessionmaker,
            github_http_client,
        )
        key_vault = build_key_vault(app_settings)
        model_router = build_model_router(app_settings, sessionmaker, key_vault)
        repo_cloner: RepoCloner = NoopCloner()
        if github_service is not None:
            repo_cloner = GitHubCloner(github=github_service, provisioner=provisioner)
        agent_runner = build_agent_runner(app_settings, model_router, provisioner)

        orchestrator = SessionOrchestrator(
            sessionmaker=sessionmaker,
            provisioner=provisioner,
            pool_controller=pool_controller,
            hub=event_hub,
            artifacts_dir=Path(app_settings.artifacts_dir),
            repo_cloner=repo_cloner,
            agent_runner=agent_runner,
            step_delay_seconds=app_settings.session_step_delay_seconds,
        )

        app.state.settings = app_settings
        app.state.engine = engine
        app.state.sessionmaker = sessionmaker
        app.state.provisioner = provisioner
        app.state.pool_controller = pool_controller
        app.state.event_hub = event_hub
        app.state.github_service = github_service
        app.state.key_vault = key_vault
        app.state.model_router = model_router
        app.state.agent_runner = agent_runner
        app.state.orchestrator = orchestrator

        await pool_controller.reconcile()
        if app_settings.auto_create_schema:
            await create_all(engine)

        yield
        if github_http_client is not None:
            await github_http_client.aclose()
        await engine.dispose()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[app_settings.web_origin],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(build_router())
    install_websocket_routes(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
