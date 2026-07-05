from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.schemas import ArtifactOut, CreateSessionRequest, EventOut, SessionOut, UsageOut
from services.db.models import (
    Artifact,
    Event,
    GithubInstallation,
    Repo,
    UsageRecord,
)
from services.db.models import (
    Session as SessionRow,
)
from services.sessions.orchestrator import SessionOrchestrator
from services.sessions.state_machine import SessionStatus


async def get_db_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    async_sessionmaker_factory = request.app.state.sessionmaker
    async with async_sessionmaker_factory() as db:
        yield db


def build_router() -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.post("/sessions", response_model=SessionOut)
    async def create_session(
        payload: CreateSessionRequest,
        request: Request,
        db: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> SessionOut:
        repo = await db.get(Repo, payload.repo_id)
        if repo is None:
            raise HTTPException(status_code=404, detail="repo not found")
        installation = await db.get(GithubInstallation, repo.installation_id)
        if installation is None:
            raise HTTPException(status_code=404, detail="installation not found")

        session = SessionRow(
            user_id=installation.user_id,
            repo_id=repo.id,
            prompt=payload.prompt,
            status=SessionStatus.QUEUED.value,
            branch=repo.default_branch,
            model_policy=payload.model_policy.model_dump(),
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)

        orchestrator: SessionOrchestrator = request.app.state.orchestrator
        await orchestrator.start(session.id)
        return SessionOut.model_validate(session, from_attributes=True)

    @router.get("/sessions/{session_id}", response_model=SessionOut)
    async def get_session(
        session_id: UUID, db: Annotated[AsyncSession, Depends(get_db_session)]
    ) -> SessionOut:
        session = await db.get(SessionRow, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return SessionOut.model_validate(session, from_attributes=True)

    @router.post("/sessions/{session_id}/cancel", response_model=SessionOut)
    async def cancel_session(
        session_id: UUID,
        request: Request,
        db: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> SessionOut:
        session = await db.get(SessionRow, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        try:
            if SessionStatus(session.status) not in {
                SessionStatus.QUEUED,
                SessionStatus.PROVISIONING,
                SessionStatus.RUNNING,
            }:
                raise HTTPException(
                    status_code=409, detail=f"session is not cancellable from {session.status}"
                )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        orchestrator: SessionOrchestrator = request.app.state.orchestrator
        await orchestrator.cancel(session_id)
        await orchestrator.record_event(
            session_id,
            "status_changed",
            {"status": SessionStatus.CANCELLED.value},
        )
        session.status = SessionStatus.CANCELLED.value
        session.ended_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(session)
        return SessionOut.model_validate(session, from_attributes=True)

    @router.get("/sessions/{session_id}/artifacts", response_model=list[ArtifactOut])
    async def get_artifacts(
        session_id: UUID,
        db: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> list[ArtifactOut]:
        result = await db.execute(select(Artifact).where(Artifact.session_id == session_id))
        return [
            ArtifactOut.model_validate(row, from_attributes=True) for row in result.scalars().all()
        ]

    @router.get("/sessions/{session_id}/usage", response_model=UsageOut)
    async def get_usage(
        session_id: UUID, db: Annotated[AsyncSession, Depends(get_db_session)]
    ) -> UsageOut:
        result = await db.execute(select(UsageRecord).where(UsageRecord.session_id == session_id))
        usage = result.scalar_one_or_none()
        if usage is None:
            raise HTTPException(status_code=404, detail="usage record not found")
        return UsageOut(
            id=usage.id,
            session_id=usage.session_id,
            mac_seconds=usage.mac_seconds,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            mac_cost_usd=str(usage.mac_cost_usd),
        )

    @router.get("/keys")
    async def keys_stub() -> JSONResponse:
        return JSONResponse({"detail": "not implemented in this milestone"}, status_code=501)

    @router.get("/github/install-url")
    async def github_install_url_stub() -> JSONResponse:
        return JSONResponse({"detail": "not implemented in this milestone"}, status_code=501)

    @router.get("/repos")
    async def repos_stub() -> JSONResponse:
        return JSONResponse({"detail": "not implemented in this milestone"}, status_code=501)

    @router.post("/webhooks/github")
    async def github_webhook_stub() -> JSONResponse:
        return JSONResponse({"detail": "not implemented in this milestone"}, status_code=501)

    return router


def install_websocket_routes(app: FastAPI) -> None:
    @app.websocket("/ws/sessions/{session_id}")
    async def session_websocket(websocket: WebSocket, session_id: UUID) -> None:
        await websocket.accept()
        sessionmaker_factory = app.state.sessionmaker
        hub = app.state.event_hub
        async with sessionmaker_factory() as db:
            result = await db.execute(
                select(Event).where(Event.session_id == session_id).order_by(Event.ts)
            )
            for event in result.scalars().all():
                await websocket.send_json(
                    EventOut.model_validate(event, from_attributes=True).model_dump(mode="json")
                )
        queue: asyncio.Queue[Event] = hub.subscribe(session_id)
        try:
            while True:
                event = await queue.get()
                await websocket.send_json(
                    EventOut.model_validate(event, from_attributes=True).model_dump(mode="json")
                )
        finally:
            hub.unsubscribe(session_id, queue)
