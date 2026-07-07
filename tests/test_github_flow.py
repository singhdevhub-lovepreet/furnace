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
from services.db.models import Event, GithubInstallation, Repo
from services.db.models import Session as SessionRow
from services.db.session import build_engine, build_sessionmaker, create_all
from services.github.client import GitHubAppClient
from services.github.service import GitHubCloner, GitHubService, NoopCloner
from services.github.state import build_install_state, verify_install_state
from services.scheduler.pool import PoolController
from services.scheduler.provisioner.base import SessionSpec
from services.scheduler.provisioner.fake import FakeProvisioner
from services.sessions.orchestrator import SessionOrchestrator
from services.sessions.pubsub import SessionEventHub
from services.sessions.state_machine import SessionStatus
from tests.auth_helpers import signup_user


class GitHubApiState:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.installation_token: str = "installation-token-1"
        self.installation_expires_at: datetime = datetime.now(UTC) + timedelta(hours=1)
        self.installation_account_login: str = "octo"
        self.repositories: list[dict[str, object]] = [
            {"id": 101, "full_name": "octo/example", "default_branch": "main"},
        ]


def sign_webhook(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, digestmod="sha256").hexdigest()


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
        auth_jwt_secret="test-jwt-secret-0123456789abcdef012345",
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
async def test_install_state_round_trip_and_validation() -> None:
    secret = "state-secret"
    user_id = UUID("11111111-2222-3333-4444-555555555555")
    state = build_install_state(secret, user_id)
    assert verify_install_state(secret, state) == user_id
    parts = state.split(".")
    assert verify_install_state(secret, f"{parts[0]}.{parts[1]}.deadbeef") is None
    assert verify_install_state("wrong-secret", state) is None
    assert verify_install_state(secret, "not-a-valid-state") is None


@pytest.mark.asyncio
async def test_github_setup_callback_binds_installation(github_app: FastAPI) -> None:
    sessionmaker = github_app.state.sessionmaker
    state = GitHubApiState()

    def handler(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        if request.url.path == "/app/installations/9876":
            return httpx.Response(
                200, json={"account": {"login": state.installation_account_login}}
            )
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
            user_id, token, _ = await signup_user(api_client, "github@example.com", "password123")
            api_client.headers["Authorization"] = f"Bearer {token}"

            install_url_response = await api_client.get("/v1/github/install-url")
            assert install_url_response.status_code == 200
            parsed = urlparse(install_url_response.json()["url"])
            assert parsed.netloc == "github.com"
            assert parsed.path == "/apps/raven-sample/installations/new"
            state_parts = parse_qs(parsed.query)["state"][0].split(".")
            assert state_parts[0] == str(user_id)
            assert (
                verify_install_state(
                    github_app.state.settings.github_webhook_secret,
                    parse_qs(parsed.query)["state"][0],
                )
                == user_id
            )

            response = await api_client.get(
                "/v1/github/setup",
                params={"installation_id": 9876, "state": parse_qs(parsed.query)["state"][0]},
            )
            assert response.status_code == 302
            assert (
                response.headers["location"]
                == f"{github_app.state.settings.web_origin.rstrip('/')}/"
            )

            async with sessionmaker() as db:
                installation = (
                    await db.execute(
                        select(GithubInstallation).where(GithubInstallation.installation_id == 9876)
                    )
                ).scalar_one()
                assert installation.user_id == user_id
                assert installation.account_login == state.installation_account_login
                repo_rows = (
                    (await db.execute(select(Repo).where(Repo.installation_id == installation.id)))
                    .scalars()
                    .all()
                )
                assert [row.full_name for row in repo_rows] == ["octo/example"]

            repos_response = await api_client.get("/v1/repos")
            assert repos_response.status_code == 200
            repos = [RepoOut.model_validate(item) for item in repos_response.json()]
            assert [repo.full_name for repo in repos] == ["octo/example"]


@pytest.mark.asyncio
async def test_github_setup_callback_rejects_invalid_state(github_app: FastAPI) -> None:
    state = GitHubApiState()

    def handler(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        raise AssertionError("github client should not be called for invalid state")

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
            sessionmaker=github_app.state.sessionmaker,
            client=client,
            settings=github_app.state.settings,
        )

        async with AsyncClient(
            transport=ASGITransport(app=github_app), base_url="http://test"
        ) as api_client:
            user_id, token, _ = await signup_user(api_client, "github@example.com", "password123")
            api_client.headers["Authorization"] = f"Bearer {token}"

            missing_response = await api_client.get(
                "/v1/github/setup",
                params={"installation_id": 9876},
            )
            assert missing_response.status_code == 400
            invalid_response = await api_client.get(
                "/v1/github/setup",
                params={"installation_id": 9876, "state": "broken.state"},
            )
            assert invalid_response.status_code == 400
            assert state.requests == []

            async with github_app.state.sessionmaker() as db:
                installation = (
                    await db.execute(
                        select(GithubInstallation).where(GithubInstallation.installation_id == 9876)
                    )
                ).scalar_one_or_none()
                assert installation is None

            assert user_id is not None


@pytest.mark.asyncio
async def test_webhook_installation_created_unknown_skips_binding(github_app: FastAPI) -> None:
    state = GitHubApiState()

    def handler(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        raise AssertionError("github client should not be called")

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
            sessionmaker=github_app.state.sessionmaker,
            client=client,
            settings=github_app.state.settings,
        )
        async with AsyncClient(
            transport=ASGITransport(app=github_app), base_url="http://test"
        ) as api_client:
            await signup_user(api_client, "alpha@example.com", "password123")
            await signup_user(api_client, "beta@example.com", "password123")

            body = {
                "action": "created",
                "installation": {"id": 9999, "account": {"login": "octo"}},
            }
            raw_body = json.dumps(body).encode("utf-8")
            response = await api_client.post(
                "/v1/webhooks/github",
                content=raw_body,
                headers={
                    "X-GitHub-Event": "installation",
                    "X-Hub-Signature-256": sign_webhook(
                        github_app.state.settings.github_webhook_secret,
                        raw_body,
                    ),
                },
            )
            assert response.status_code == 204

            async with github_app.state.sessionmaker() as db:
                installation = (
                    await db.execute(
                        select(GithubInstallation).where(GithubInstallation.installation_id == 9999)
                    )
                ).scalar_one_or_none()
                assert installation is None


@pytest.mark.asyncio
async def test_webhook_installation_created_updates_known_installation(github_app: FastAPI) -> None:
    sessionmaker = github_app.state.sessionmaker
    state = GitHubApiState()
    state.repositories = [
        {"id": 101, "full_name": "octo/example", "default_branch": "main"},
        {"id": 102, "full_name": "octo/second", "default_branch": "develop"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        if request.url.path == "/app/installations/9876":
            return httpx.Response(
                200, json={"account": {"login": state.installation_account_login}}
            )
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
            user_id, token, _ = await signup_user(api_client, "github@example.com", "password123")
            api_client.headers["Authorization"] = f"Bearer {token}"
            install_url_response = await api_client.get("/v1/github/install-url")
            state_value = parse_qs(urlparse(install_url_response.json()["url"]).query)["state"][0]
            await api_client.get(
                "/v1/github/setup",
                params={"installation_id": 9876, "state": state_value},
            )

            state.installation_account_login = "octo-updated"
            state.repositories = [
                {"id": 201, "full_name": "octo/example", "default_branch": "main"},
                {"id": 202, "full_name": "octo/second", "default_branch": "develop"},
            ]
            body = {
                "action": "created",
                "installation": {"id": 9876, "account": {"login": "octo-updated"}},
            }
            raw_body = json.dumps(body).encode("utf-8")
            response = await api_client.post(
                "/v1/webhooks/github",
                content=raw_body,
                headers={
                    "X-GitHub-Event": "installation",
                    "X-Hub-Signature-256": sign_webhook(
                        github_app.state.settings.github_webhook_secret,
                        raw_body,
                    ),
                },
            )
            assert response.status_code == 204

            async with sessionmaker() as db:
                installation = (
                    await db.execute(
                        select(GithubInstallation).where(GithubInstallation.installation_id == 9876)
                    )
                ).scalar_one()
                assert installation.account_login == "octo-updated"
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
async def test_github_repos_remain_tenant_scoped_after_install_binding(github_app: FastAPI) -> None:
    sessionmaker = github_app.state.sessionmaker
    state = GitHubApiState()
    state.repositories = [
        {"id": 101, "full_name": "octo/example", "default_branch": "main"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        if request.url.path == "/app/installations/9876":
            return httpx.Response(
                200, json={"account": {"login": state.installation_account_login}}
            )
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
            user_a_id, token_a, _ = await signup_user(
                api_client, "github@example.com", "password123"
            )
            api_client.headers["Authorization"] = f"Bearer {token_a}"
            install_url_response = await api_client.get("/v1/github/install-url")
            state_value = parse_qs(urlparse(install_url_response.json()["url"]).query)["state"][0]
            await api_client.get(
                "/v1/github/setup",
                params={"installation_id": 9876, "state": state_value},
            )

            repos_response_a = await api_client.get("/v1/repos")
            assert repos_response_a.status_code == 200
            repos_a = [RepoOut.model_validate(item) for item in repos_response_a.json()]
            assert [repo.full_name for repo in repos_a] == ["octo/example"]

            _, token_b, _ = await signup_user(api_client, "beta@example.com", "password123")
            api_client.headers["Authorization"] = f"Bearer {token_b}"
            repos_response_b = await api_client.get("/v1/repos")
            assert repos_response_b.status_code == 200
            repos_b = [RepoOut.model_validate(item) for item in repos_response_b.json()]
            assert repos_b == []
            assert user_a_id is not None


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
    async with AsyncClient(
        transport=ASGITransport(app=github_app), base_url="http://test"
    ) as api_client:
        user_id, token, _ = await signup_user(api_client, "github@example.com", "password123")
        api_client.headers["Authorization"] = f"Bearer {token}"

        async with sessionmaker() as db:
            installation = GithubInstallation(
                user_id=user_id,
                installation_id=9876,
                account_login="octo",
            )
            repo = Repo(installation=installation, full_name="octo/example", default_branch="main")
            db.add_all([installation, repo])
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
