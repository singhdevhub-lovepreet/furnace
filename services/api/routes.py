from __future__ import annotations

import hmac
import mimetypes
import secrets
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.schemas import (
    ArtifactOut,
    AuthLoginRequest,
    AuthSignupRequest,
    AuthTokenResponse,
    CreateSessionRequest,
    EventOut,
    LlmKeyCreateRequest,
    LlmKeyOut,
    PoolStatusOut,
    RepoOut,
    SessionOut,
    UsageOut,
    UserOut,
)
from services.auth.dependencies import get_current_user, resolve_user_from_token_string
from services.auth.jwt import create_access_token
from services.auth.password import hash_password, verify_password
from services.config import Settings
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
from services.scheduler.pool import PoolController
from services.sessions.orchestrator import SessionOrchestrator
from services.sessions.state_machine import SessionStatus
from services.vault.key_vault import KeyVault


async def get_db_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    async_sessionmaker_factory = request.app.state.sessionmaker
    async with async_sessionmaker_factory() as db:
        yield db


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


def _get_pool_controller(request: Request) -> PoolController:
    pool_controller: PoolController | None = request.app.state.pool_controller
    if pool_controller is None:
        raise HTTPException(status_code=500, detail="Pool controller is not configured")
    return pool_controller


def _install_state(request: Request, user_id: UUID) -> str:
    settings = request.app.state.settings
    secret = settings.github_webhook_secret or settings.github_app_private_key
    if secret is None:
        raise HTTPException(status_code=500, detail="GitHub install state secret is not configured")
    nonce = secrets.token_urlsafe(32)
    payload = f"{user_id}.{nonce}"
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), sha256).hexdigest()
    return f"{payload}.{digest}"


async def _load_owned_repo(db: AsyncSession, user_id: UUID, repo_id: UUID) -> Repo:
    result = await db.execute(
        select(Repo)
        .join(GithubInstallation)
        .where(Repo.id == repo_id, GithubInstallation.user_id == user_id)
    )
    repo = result.scalar_one_or_none()
    if repo is None:
        raise HTTPException(status_code=404, detail="repo not found")
    return repo


async def _load_owned_session(db: AsyncSession, user_id: UUID, session_id: UUID) -> SessionRow:
    result = await db.execute(
        select(SessionRow).where(SessionRow.id == session_id, SessionRow.user_id == user_id)
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


def _auth_response(settings: Settings, user: User) -> AuthTokenResponse:
    auth_secret = settings.auth_jwt_secret
    auth_ttl_seconds = settings.auth_access_token_ttl_seconds
    if auth_secret is None:
        raise HTTPException(status_code=500, detail="auth JWT secret is not configured")
    token = create_access_token(auth_secret, user.id, auth_ttl_seconds)
    return AuthTokenResponse(
        access_token=token,
        token_type="bearer",
        user=UserOut.model_validate(user, from_attributes=True),
    )


def _artifact_media_type(artifact: Artifact) -> str:
    if artifact.kind == "screenshot":
        return "image/png"
    if artifact.kind == "video":
        return "video/mp4"
    guessed, _ = mimetypes.guess_type(artifact.object_key)
    return guessed or "application/octet-stream"


def build_router() -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.post("/auth/signup", response_model=AuthTokenResponse)
    async def signup(
        request: Request,
        payload: AuthSignupRequest,
        db: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> AuthTokenResponse:
        existing = await db.execute(select(User).where(User.email == payload.email))
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(status_code=409, detail="email already registered")
        user = User(
            email=payload.email,
            plan=payload.plan or "pro",
            password_hash=hash_password(payload.password),
        )
        db.add(user)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(status_code=409, detail="email already registered") from exc
        await db.refresh(user)
        return _auth_response(request.app.state.settings, user)

    @router.post("/auth/login", response_model=AuthTokenResponse)
    async def login(
        request: Request,
        payload: AuthLoginRequest,
        db: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> AuthTokenResponse:
        result = await db.execute(select(User).where(User.email == payload.email))
        user = result.scalar_one_or_none()
        if user is None or not verify_password(payload.password, user.password_hash):
            raise HTTPException(
                status_code=401,
                detail="invalid credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return _auth_response(request.app.state.settings, user)

    @router.get("/auth/me", response_model=UserOut)
    async def me(current_user: Annotated[User, Depends(get_current_user)]) -> UserOut:
        return UserOut.model_validate(current_user, from_attributes=True)

    @router.post("/sessions", response_model=SessionOut)
    async def create_session(
        payload: CreateSessionRequest,
        request: Request,
        db: Annotated[AsyncSession, Depends(get_db_session)],
        current_user: Annotated[User, Depends(get_current_user)],
    ) -> SessionOut:
        repo = await _load_owned_repo(db, current_user.id, payload.repo_id)

        session = SessionRow(
            user_id=current_user.id,
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
        session_id: UUID,
        db: Annotated[AsyncSession, Depends(get_db_session)],
        current_user: Annotated[User, Depends(get_current_user)],
    ) -> SessionOut:
        session = await _load_owned_session(db, current_user.id, session_id)
        return SessionOut.model_validate(session, from_attributes=True)

    @router.get("/sessions", response_model=list[SessionOut])
    async def list_sessions(
        db: Annotated[AsyncSession, Depends(get_db_session)],
        current_user: Annotated[User, Depends(get_current_user)],
    ) -> list[SessionOut]:
        result = await db.execute(
            select(SessionRow)
            .where(SessionRow.user_id == current_user.id)
            .order_by(SessionRow.created_at.desc(), SessionRow.id.desc())
        )
        sessions = result.scalars().all()
        return [SessionOut.model_validate(row, from_attributes=True) for row in sessions]

    @router.post("/sessions/{session_id}/cancel", response_model=SessionOut)
    async def cancel_session(
        session_id: UUID,
        request: Request,
        db: Annotated[AsyncSession, Depends(get_db_session)],
        current_user: Annotated[User, Depends(get_current_user)],
    ) -> SessionOut:
        session = await _load_owned_session(db, current_user.id, session_id)
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
        current_user: Annotated[User, Depends(get_current_user)],
    ) -> list[ArtifactOut]:
        await _load_owned_session(db, current_user.id, session_id)
        result = await db.execute(select(Artifact).where(Artifact.session_id == session_id))
        return [
            ArtifactOut.model_validate(row, from_attributes=True) for row in result.scalars().all()
        ]

    @router.get("/sessions/{session_id}/artifacts/{artifact_id}/content")
    async def get_artifact_content(
        session_id: UUID,
        artifact_id: UUID,
        db: Annotated[AsyncSession, Depends(get_db_session)],
        current_user: Annotated[User, Depends(get_current_user)],
    ) -> FileResponse:
        await _load_owned_session(db, current_user.id, session_id)
        artifact = await db.get(Artifact, artifact_id)
        if artifact is None or artifact.session_id != session_id:
            raise HTTPException(status_code=404, detail="artifact not found")
        path = Path(artifact.object_key)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="artifact file not found")
        return FileResponse(
            path,
            media_type=_artifact_media_type(artifact),
            filename=path.name,
        )

    @router.get("/sessions/{session_id}/usage", response_model=UsageOut)
    async def get_usage(
        session_id: UUID,
        db: Annotated[AsyncSession, Depends(get_db_session)],
        current_user: Annotated[User, Depends(get_current_user)],
    ) -> UsageOut:
        await _load_owned_session(db, current_user.id, session_id)
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
    async def github_install_url(
        request: Request,
        current_user: Annotated[User, Depends(get_current_user)],
    ) -> dict[str, str]:
        state = _install_state(request, current_user.id)
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
        current_user: Annotated[User, Depends(get_current_user)],
        installation_id: UUID | None = None,
    ) -> list[RepoOut]:
        statement = (
            select(Repo)
            .join(GithubInstallation)
            .where(GithubInstallation.user_id == current_user.id)
        )
        if installation_id is not None:
            statement = statement.where(Repo.installation_id == installation_id)
        result = await db.execute(statement)
        repos = result.scalars().all()
        return [RepoOut.model_validate(repo, from_attributes=True) for repo in repos]

    @router.get("/keys", response_model=list[LlmKeyOut])
    async def list_keys(
        db: Annotated[AsyncSession, Depends(get_db_session)],
        current_user: Annotated[User, Depends(get_current_user)],
    ) -> list[LlmKeyOut]:
        result = await db.execute(
            select(LlmKey).where(LlmKey.user_id == current_user.id).order_by(LlmKey.created_at)
        )
        keys = result.scalars().all()
        return [LlmKeyOut.model_validate(row, from_attributes=True) for row in keys]

    @router.post("/keys", response_model=LlmKeyOut)
    async def create_key(
        request: Request,
        payload: LlmKeyCreateRequest,
        db: Annotated[AsyncSession, Depends(get_db_session)],
        current_user: Annotated[User, Depends(get_current_user)],
    ) -> LlmKeyOut:
        key_vault = _get_key_vault(request)
        encrypted = key_vault.encrypt(payload.key)
        key_row = LlmKey(
            user_id=current_user.id,
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
        key_id: UUID,
        db: Annotated[AsyncSession, Depends(get_db_session)],
        current_user: Annotated[User, Depends(get_current_user)],
    ) -> Response:
        key_row = await db.get(LlmKey, key_id)
        if key_row is None or key_row.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="key not found")
        await db.delete(key_row)
        await db.commit()
        return Response(status_code=204)

    @router.get("/models", response_model=ModelCatalog)
    async def list_models() -> ModelCatalog:
        return MODEL_CATALOG

    @router.get("/pool", response_model=PoolStatusOut)
    async def pool_status(request: Request) -> PoolStatusOut:
        pool_controller = _get_pool_controller(request)
        snapshot = await pool_controller.snapshot()
        return PoolStatusOut.model_validate(snapshot, from_attributes=True)

    return router


def install_websocket_routes(app: FastAPI) -> None:
    @app.websocket("/ws/sessions/{session_id}")
    async def session_websocket(websocket: WebSocket, session_id: UUID) -> None:
        token = websocket.query_params.get("token")
        if token is None or not token.strip():
            await websocket.close(code=4401)
            return
        try:
            current_user = await resolve_user_from_token_string(websocket, token.strip())
        except HTTPException as exc:
            if exc.status_code == 401:
                await websocket.close(code=4401)
            else:
                await websocket.close(code=4403)
            return
        sessionmaker_factory = app.state.sessionmaker
        try:
            async with sessionmaker_factory() as db:
                await _load_owned_session(db, current_user.id, session_id)
        except HTTPException:
            await websocket.close(code=4403)
            return
        await websocket.accept()
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
