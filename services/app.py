from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from services.api.routes import build_router, install_websocket_routes
from services.config import Settings
from services.db.session import build_engine, build_sessionmaker, create_all
from services.scheduler.provisioner.base import MacProvisioner
from services.scheduler.provisioner.fake import FakeProvisioner
from services.sessions.orchestrator import SessionOrchestrator
from services.sessions.pubsub import SessionEventHub


def build_provisioner(settings: Settings) -> MacProvisioner:
    if settings.provisioner == "fake":
        return FakeProvisioner(queue_acquire=settings.fake_queue_acquire)
    raise NotImplementedError(f"unknown provisioner {settings.provisioner!r}")


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = build_engine(app_settings.database_url)
        sessionmaker = build_sessionmaker(engine)
        provisioner = build_provisioner(app_settings)
        event_hub = SessionEventHub()
        orchestrator = SessionOrchestrator(
            sessionmaker=sessionmaker,
            provisioner=provisioner,
            hub=event_hub,
            artifacts_dir=Path(app_settings.artifacts_dir),
            step_delay_seconds=app_settings.session_step_delay_seconds,
        )

        app.state.settings = app_settings
        app.state.engine = engine
        app.state.sessionmaker = sessionmaker
        app.state.provisioner = provisioner
        app.state.event_hub = event_hub
        app.state.orchestrator = orchestrator

        if app_settings.auto_create_schema:
            await create_all(engine)

        yield
        await engine.dispose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(build_router())
    install_websocket_routes(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
