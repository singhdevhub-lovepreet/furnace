from __future__ import annotations

import asyncio
import hmac
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx
import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, MockTransport
from sqlalchemy import select

from services.api.routes import build_router, install_websocket_routes
from services.api.schemas import RepoOut
from services.config import Settings
from services.db.models import Event, GithubInstallation, Repo, User
from services.db.models import Session as SessionRow
from services.db.session import build_engine, build_sessionmaker, create_all
from services.github.client import GitHubAppClient
from services.github.service import GitHubCloner, GitHubService, NoopCloner
from services.scheduler.pool import PoolController
from services.scheduler.provisioner.base import SessionSpec
from services.scheduler.provisioner.fake import FakeProvisioner
from services.sessions.orchestrator import SessionOrchestrator
from services.sessions.pubsub import SessionEventHub
from services.sessions.state_machine import SessionStatus


class GitHubApiState:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.installation_token: str = "installation-token-1"
        self.installation_expires_at: datetime = datetime.now(UTC) + timedelta(hours=1)
        self.repositories: list[dict[str, object]] = [
            {"id": 101, "full_name": "octo/example", "default_branch": "main"},
        ]


def generate_private_key_pem() -> tuple[str, rsa.RSAPrivateKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    return pem, private_key


@pytest_asyncio.fixture
async def github_app(tmp_path: Path) -> AsyncGenerator[FastAPI, None]:
    private_key_pem, _ = generate_private_key_pem()
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'github.db'}",
        artifacts_dir=str(tmp_path / "artifacts"),
        auto_create_schema=True,
        agent_runner="fake",
        github_app_id="12345",
        github_app_slug="raven-sample",
        github_app_private_key=private_key_pem,
        github_webhook_secret="webhook-secret",
        github_api_base="https://api.github.test",
    )
    app = FastAPI()
    engine = build_engine(settings.database_url)
    sessionmaker = build_sessionmaker(engine)
    provisioner = FakeProvisioner(max_slots=4)
    pool_controller = PoolController(
        provisioner=provisioner,
        capacity_override=4,
        estimated_session_seconds=30,
        scale_up_threshold=1,
    )
    event_hub = SessionEventHub()
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker
    app.state.provisioner = provisioner
    app.state.pool_controller = pool_controller
    app.state.event_hub = event_hub
    app.state.github_service = None
    app.state.orchestrator = SessionOrchestrator(
        sessionmaker=sessionmaker,
        provisioner=provisioner,
        pool_controller=pool_controller,
        hub=event_hub,
        artifacts_dir=tmp_path / "artifacts",
        repo_cloner=NoopCloner(),
        step_delay_seconds=0.01,
    )
    app.include_router(build_router())
    install_websocket_routes(app)
    await create_all(engine)
    try:
        yield app
    finally:
        await engine.dispose()


async def wait_for_status(app: FastAPI, session_id: UUID, expected: SessionStatus) -> SessionStatus:
    deadline = asyncio.get_running_loop().time() + 5.0
    sessionmaker = app.state.sessionmaker
    while True:
        async with sessionmaker() as db:
            row = await db.get(SessionRow, session_id)
            assert row is not None
            status = SessionStatus(row.status)
            if status == expected:
                return status
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {expected}")
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_github_app_jwt_and_caching() -> None:
    private_key_pem, private_key = generate_private_key_pem()
    state = GitHubApiState()

    def handler(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/app/installations/99/access_tokens"
        auth = request.headers["Authorization"]
        assert auth.startswith("Bearer ")
        body = json.loads(request.content.decode("utf-8"))
        if len(state.requests) == 1:
            assert body == {}
        else:
            assert body == {"repository_ids": [1, 2], "permissions": {"contents": "read"}}
        jwt_token = auth.removeprefix("Bearer ")
        header = jwt.get_unverified_header(jwt_token)
        assert header["alg"] == "RS256"
        payload = jwt.decode(
            jwt_token,
            private_key.public_key(),
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
        assert payload["iss"] == "12345"
        assert isinstance(payload["iat"], int)
        assert isinstance(payload["exp"], int)
        assert payload["exp"] > payload["iat"]
        return httpx.Response(
            201,
            json={
                "token": state.installation_token,
                "expires_at": state.installation_expires_at.isoformat(),
            },
        )

    async with AsyncClient(
        transport=MockTransport(handler), base_url="https://api.github.test"
    ) as http_client:
        client = GitHubAppClient(
            http_client,
            app_id="12345",
            private_key_pem=private_key_pem,
            api_base="https://api.github.test",
        )
        first = await client.mint_installation_token(99)
        second = await client.mint_installation_token(99)
        assert first.token == state.installation_token
        assert second.token == state.installation_token
        assert len(state.requests) == 1

        scoped = await client.mint_installation_token(
            99,
            repository_ids=[1, 2],
            permissions={"contents": "read"},
        )
        assert scoped.token == state.installation_token
        assert len(state.requests) == 2


@pytest.mark.asyncio
async def test_list_installation_repos() -> None:
    private_key_pem, _ = generate_private_key_pem()
    state = GitHubApiState()

    def handler(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/installation/repositories"
        assert request.headers["Authorization"] == "Bearer installation-token-1"
        return httpx.Response(200, json={"repositories": state.repositories})

    async with AsyncClient(
        transport=MockTransport(handler), base_url="https://api.github.test"
    ) as http_client:
        client = GitHubAppClient(
            http_client,
            app_id="12345",
            private_key_pem=private_key_pem,
            api_base="https://api.github.test",
        )
        repos = await client.list_installation_repos("installation-token-1")
        assert [repo.full_name for repo in repos] == ["octo/example"]
        assert repos[0].default_branch == "main"


@pytest.mark.asyncio
async def test_install_url_and_webhook_sync(github_app: FastAPI) -> None:
    sessionmaker = github_app.state.sessionmaker
    async with sessionmaker() as db:
        user = User(email="github@example.com", plan="pro")
        db.add(user)
        await db.commit()

    state = GitHubApiState()

    def handler(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        if request.url.path == "/app/installations/9876/access_tokens":
            return httpx.Response(
                201,
                json={
                    "token": state.installation_token,
                    "expires_at": state.installation_expires_at.isoformat(),
                },
            )
        if request.url.path == "/installation/repositories":
            return httpx.Response(200, json={"repositories": state.repositories})
        raise AssertionError(f"unexpected path {request.url.path}")

    async with AsyncClient(
        transport=MockTransport(handler), base_url="https://api.github.test"
    ) as http_client:
        client = GitHubAppClient(
            http_client,
            app_id="12345",
            private_key_pem=github_app.state.settings.github_app_private_key,
            api_base="https://api.github.test",
        )
        github_app.state.github_service = GitHubService(
            sessionmaker=sessionmaker,
            client=client,
            settings=github_app.state.settings,
        )

        async with AsyncClient(
            transport=ASGITransport(app=github_app), base_url="http://test"
        ) as api_client:
            install_url_response = await api_client.get("/v1/github/install-url")
            assert install_url_response.status_code == 200
            parsed = urlparse(install_url_response.json()["url"])
            assert parsed.netloc == "github.com"
            assert parsed.path == "/apps/raven-sample/installations/new"
            assert "." in parse_qs(parsed.query)["state"][0]

            body = {
                "action": "created",
                "installation": {"id": 9876, "account": {"login": "octo"}},
            }
            raw_body = json.dumps(body).encode("utf-8")
            signature = (
                "sha256="
                + hmac.new(
                    github_app.state.settings.github_webhook_secret.encode("utf-8"),
                    raw_body,
                    digestmod="sha256",
                ).hexdigest()
            )
            response = await api_client.post(
                "/v1/webhooks/github",
                content=raw_body,
                headers={"X-GitHub-Event": "installation", "X-Hub-Signature-256": signature},
            )
            assert response.status_code == 204

            repos_response = await api_client.get("/v1/repos")
            assert repos_response.status_code == 200
            repos = [RepoOut.model_validate(item) for item in repos_response.json()]
            assert [repo.full_name for repo in repos] == ["octo/example"]

        async with sessionmaker() as db:
            installation = (
                await db.execute(
                    select(GithubInstallation).where(GithubInstallation.installation_id == 9876)
                )
            ).scalar_one()
            assert installation.account_login == "octo"
            repo_rows = (
                (await db.execute(select(Repo).where(Repo.installation_id == installation.id)))
                .scalars()
                .all()
            )
            assert len(repo_rows) == 1

        state.repositories = [
            {"id": 101, "full_name": "octo/example", "default_branch": "main"},
            {"id": 102, "full_name": "octo/second", "default_branch": "develop"},
        ]
        repo_event = {
            "action": "added",
            "installation": {"id": 9876, "account": {"login": "octo"}},
            "repositories_added": [],
            "repositories_removed": [],
        }
        repo_event_raw = json.dumps(repo_event).encode("utf-8")
        repo_event_signature = (
            "sha256="
            + hmac.new(
                github_app.state.settings.github_webhook_secret.encode("utf-8"),
                repo_event_raw,
                digestmod="sha256",
            ).hexdigest()
        )
        async with AsyncClient(
            transport=ASGITransport(app=github_app), base_url="http://test"
        ) as api_client:
            response = await api_client.post(
                "/v1/webhooks/github",
                content=repo_event_raw,
                headers={
                    "X-GitHub-Event": "installation_repositories",
                    "X-Hub-Signature-256": repo_event_signature,
                },
            )
            assert response.status_code == 204

        async with sessionmaker() as db:
            repo_rows = (
                (
                    await db.execute(
                        select(Repo)
                        .join(GithubInstallation)
                        .where(GithubInstallation.installation_id == 9876)
                    )
                )
                .scalars()
                .all()
            )
            assert {row.full_name for row in repo_rows} == {"octo/example", "octo/second"}


@pytest.mark.asyncio
async def test_webhook_signature_rejects(github_app: FastAPI) -> None:
    async with AsyncClient(
        transport=MockTransport(lambda request: httpx.Response(500)),
        base_url="https://api.github.test",
    ) as http_client:
        client = GitHubAppClient(
            http_client,
            app_id="12345",
            private_key_pem=github_app.state.settings.github_app_private_key,
            api_base="https://api.github.test",
        )
        github_app.state.github_service = GitHubService(
            sessionmaker=github_app.state.sessionmaker,
            client=client,
            settings=github_app.state.settings,
        )
        async with AsyncClient(
            transport=ASGITransport(app=github_app), base_url="http://test"
        ) as api_client:
            response = await api_client.post(
                "/v1/webhooks/github",
                content=b"{}",
                headers={"X-GitHub-Event": "installation", "X-Hub-Signature-256": "sha256=bad"},
            )
            assert response.status_code == 401


@pytest.mark.asyncio
async def test_github_cloner_uses_token_without_persisting_to_events(github_app: FastAPI) -> None:
    sessionmaker = github_app.state.sessionmaker
    async with sessionmaker() as db:
        user = User(email="github@example.com", plan="pro")
        installation = GithubInstallation(user=user, installation_id=9876, account_login="octo")
        repo = Repo(installation=installation, full_name="octo/example", default_branch="main")
        db.add_all([user, installation, repo])
        await db.commit()
        await db.refresh(repo)
        repo_id = repo.id

    token_value = "installation-token-1"
    state = GitHubApiState()
    state.installation_token = token_value

    def handler(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        if request.url.path == "/app/installations/9876/access_tokens":
            return httpx.Response(
                201,
                json={
                    "token": token_value,
                    "expires_at": state.installation_expires_at.isoformat(),
                },
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    async with AsyncClient(
        transport=MockTransport(handler), base_url="https://api.github.test"
    ) as http_client:
        client = GitHubAppClient(
            http_client,
            app_id="12345",
            private_key_pem=github_app.state.settings.github_app_private_key,
            api_base="https://api.github.test",
        )
        github_service = GitHubService(
            sessionmaker=sessionmaker,
            client=client,
            settings=github_app.state.settings,
        )
        fake_provisioner = FakeProvisioner()
        cloner = GitHubCloner(github=github_service, provisioner=fake_provisioner)

        async with sessionmaker() as db:
            repo = await db.get(Repo, repo_id)
            assert repo is not None

        outcome = await fake_provisioner.acquire(
            SessionSpec(
                image_id="sample-image",
                cpu=4,
                memory_hint_mb=4096,
                ttl_seconds=60,
                idle_timeout_seconds=30,
            )
        )
        await cloner.prepare(outcome.handle, repo)
        assert len(fake_provisioner.put_files) == 1
        assert len(fake_provisioner.exec_calls) == 1
        assert fake_provisioner.put_files[0][2].decode("utf-8") == token_value
        assert token_value not in fake_provisioner.exec_calls[0][2][1]

        pool_controller = PoolController(
            provisioner=fake_provisioner,
            capacity_override=4,
            estimated_session_seconds=30,
            scale_up_threshold=1,
        )
        orchestrator = SessionOrchestrator(
            sessionmaker=sessionmaker,
            provisioner=fake_provisioner,
            pool_controller=pool_controller,
            hub=SessionEventHub(),
            artifacts_dir=Path(github_app.state.settings.artifacts_dir),
            repo_cloner=cloner,
            step_delay_seconds=0.01,
        )
        github_app.state.orchestrator = orchestrator

        async with AsyncClient(
            transport=ASGITransport(app=github_app), base_url="http://test"
        ) as api_client:
            response = await api_client.post(
                "/v1/sessions",
                json={
                    "repo_id": str(repo_id),
                    "prompt": "clone securely",
                    "model_policy": {},
                },
            )
            assert response.status_code == 200
            session_id = UUID(response.json()["id"])
            await wait_for_status(github_app, session_id, SessionStatus.SUCCEEDED)

        async with sessionmaker() as db:
            events = (
                (
                    await db.execute(
                        select(Event).where(Event.session_id == session_id).order_by(Event.ts)
                    )
                )
                .scalars()
                .all()
            )
            serialized = json.dumps([event.payload for event in events])
            assert token_value not in serialized
