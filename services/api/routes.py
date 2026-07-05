from __future__ import annotations

import hmac
import secrets
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.schemas import (
    ArtifactOut,
    CreateSessionRequest,
    EventOut,
    LlmKeyCreateRequest,
    LlmKeyOut,
    RepoOut,
    SessionOut,
    UsageOut,
)
from services.db.models import (
    Artifact,
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
from services.github.service import GitHubService
from services.llm.policy import MODEL_CATALOG, ModelCatalog
from services.llm.router import ModelRouter
from services.sessions.orchestrator import SessionOrchestrator
from services.sessions.state_machine import SessionStatus
from services.vault.key_vault import KeyVault


async def get_db_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    async_sessionmaker_factory = request.app.state.sessionmaker
    async with async_sessionmaker_factory() as db:
        yield db


async def _resolve_user_id(db: AsyncSession) -> UUID:
    result = await db.execute(select(User.id).order_by(User.created_at).limit(1))
    user_id = result.scalar_one_or_none()
    if user_id is None:
        raise HTTPException(status_code=404, detail="no user exists")
    return user_id


def _get_github_service(request: Request) -> GitHubService:
    github_service: GitHubService | None = request.app.state.github_service
    if github_service is None:
        raise HTTPException(status_code=500, detail="GitHub service is not configured")
    return github_service


def _get_key_vault(request: Request) -> KeyVault:
    key_vault: KeyVault | None = request.app.state.key_vault
    if key_vault is None:
        raise HTTPException(status_code=500, detail="Key vault is not configured")
    return key_vault


def _get_model_router(request: Request) -> ModelRouter:
    model_router: ModelRouter | None = request.app.state.model_router
    if model_router is None:
        raise HTTPException(status_code=500, detail="Model router is not configured")
    return model_router


def _install_state(request: Request) -> str:
    settings = request.app.state.settings
    secret = settings.github_webhook_secret or settings.github_app_private_key
    if secret is None:
        raise HTTPException(status_code=500, detail="GitHub install state secret is not configured")
    nonce = secrets.token_urlsafe(32)
    digest = hmac.new(secret.encode("utf-8"), nonce.encode("utf-8"), sha256).hexdigest()
    # Binding install state to a user is a later auth milestone.
    return f"{nonce}.{digest}"


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
            model_policy=payload.model_policy.model_dump(mode="json"),
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
                    status_code=409,
                    detail=f"session is not cancellable from {session.status}",
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

    @router.get("/github/install-url")
    async def github_install_url(request: Request) -> dict[str, str]:
        state = _install_state(request)
        github_service = request.app.state.github_service
        if github_service is None:
            slug = request.app.state.settings.github_app_slug
            if slug is None:
                raise HTTPException(status_code=500, detail="GitHub app slug is not configured")
            url = f"https://github.com/apps/{slug}/installations/new?state={quote(state)}"
        else:
            url = github_service.build_install_url(state)
        return {"url": url}

    @router.post("/webhooks/github")
    async def github_webhook(request: Request) -> Response:
        github_service = _get_github_service(request)
        body = await request.body()
        signature_header = request.headers.get("X-Hub-Signature-256")
        if not github_service.verify_webhook(body, signature_header):
            raise HTTPException(status_code=401, detail="invalid GitHub webhook signature")
        event_name = request.headers.get("X-GitHub-Event")
        if event_name is None:
            raise HTTPException(status_code=400, detail="missing X-GitHub-Event header")
        await github_service.handle_webhook(event_name, body)
        return Response(status_code=204)

    @router.get("/repos", response_model=list[RepoOut])
    async def list_repos(
        db: Annotated[AsyncSession, Depends(get_db_session)],
        installation_id: UUID | None = None,
    ) -> list[RepoOut]:
        statement = select(Repo)
        if installation_id is not None:
            statement = statement.where(Repo.installation_id == installation_id)
        result = await db.execute(statement)
        repos = result.scalars().all()
        return [RepoOut.model_validate(repo, from_attributes=True) for repo in repos]

    @router.get("/keys", response_model=list[LlmKeyOut])
    async def list_keys(
        request: Request,
        db: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> list[LlmKeyOut]:
        user_id = await _resolve_user_id(db)
        result = await db.execute(
            select(LlmKey).where(LlmKey.user_id == user_id).order_by(LlmKey.created_at)
        )
        keys = result.scalars().all()
        return [LlmKeyOut.model_validate(row, from_attributes=True) for row in keys]

    @router.post("/keys", response_model=LlmKeyOut)
    async def create_key(
        request: Request,
        payload: LlmKeyCreateRequest,
        db: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> LlmKeyOut:
        key_vault = _get_key_vault(request)
        user_id = await _resolve_user_id(db)
        encrypted = key_vault.encrypt(payload.key)
        key_row = LlmKey(
            user_id=user_id,
            provider=payload.provider.value,
            label=payload.label,
            enc_key=encrypted,
        )
        db.add(key_row)
        await db.commit()
        await db.refresh(key_row)
        return LlmKeyOut.model_validate(key_row, from_attributes=True)

    @router.delete("/keys/{key_id}", status_code=204)
    async def delete_key(
        request: Request,
        key_id: UUID,
        db: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> Response:
        user_id = await _resolve_user_id(db)
        key_row = await db.get(LlmKey, key_id)
        if key_row is None or key_row.user_id != user_id:
            raise HTTPException(status_code=404, detail="key not found")
        await db.delete(key_row)
        await db.commit()
        return Response(status_code=204)

    @router.get("/models", response_model=ModelCatalog)
    async def list_models() -> ModelCatalog:
        return MODEL_CATALOG

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
        queue = hub.subscribe(session_id)
        try:
            while True:
                event = await queue.get()
                await websocket.send_json(
                    EventOut.model_validate(event, from_attributes=True).model_dump(mode="json")
                )
        finally:
            hub.unsubscribe(session_id, queue)
