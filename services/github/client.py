from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from pydantic import BaseModel, ConfigDict, Field


class GitHubClientError(RuntimeError):
    pass


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="ignore", protected_namespaces=())


class GitHubRepo(ApiModel):
    id: int
    full_name: str
    default_branch: str = Field(default="main")


class GitHubInstallationToken(ApiModel):
    token: str
    expires_at: datetime


class GitHubInstallationReposResponse(ApiModel):
    repositories: list[GitHubRepo]


class GitHubInstallationAccount(ApiModel):
    login: str


class GitHubInstallationDetails(ApiModel):
    account: GitHubInstallationAccount


@dataclass(slots=True)
class CachedInstallationToken:
    token: str
    expires_at: datetime


class GitHubAppClient:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        app_id: str,
        private_key_pem: str,
        api_base: str,
    ) -> None:
        self._http_client = http_client
        self._app_id = app_id
        self._private_key_pem = private_key_pem
        self._api_base = api_base.rstrip("/")
        self._cache: dict[int, CachedInstallationToken] = {}

    def build_app_jwt(self) -> str:
        issued_at = datetime.now(UTC) - timedelta(seconds=60)
        expires_at = issued_at + timedelta(minutes=9)
        private_key = serialization.load_pem_private_key(
            self._private_key_pem.encode("utf-8"),
            password=None,
        )
        if not isinstance(private_key, RSAPrivateKey):
            raise GitHubClientError("GitHub app private key must be RSA")
        payload = {
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
            "iss": self._app_id,
        }
        token = jwt.encode(payload, private_key, algorithm="RS256")
        if not isinstance(token, str):
            raise GitHubClientError("failed to encode GitHub app JWT")
        return token

    async def mint_installation_token(
        self,
        installation_id: int,
        repository_ids: list[int] | None = None,
        permissions: dict[str, str] | None = None,
    ) -> GitHubInstallationToken:
        if repository_ids is None and permissions is None:
            cached = self._cache.get(installation_id)
            if cached is not None and cached.expires_at > datetime.now(UTC) + timedelta(minutes=1):
                return GitHubInstallationToken(token=cached.token, expires_at=cached.expires_at)

        headers = {
            "Authorization": f"Bearer {self.build_app_jwt()}",
            "Accept": "application/vnd.github+json",
        }
        payload: dict[str, object] = {}
        if repository_ids is not None:
            payload["repository_ids"] = repository_ids
        if permissions is not None:
            payload["permissions"] = permissions

        response = await self._http_client.post(
            f"{self._api_base}/app/installations/{installation_id}/access_tokens",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        token = GitHubInstallationToken.model_validate(response.json())
        if repository_ids is None and permissions is None:
            self._cache[installation_id] = CachedInstallationToken(
                token=token.token,
                expires_at=token.expires_at,
            )
        return token

    async def list_installation_repos(self, installation_token: str) -> list[GitHubRepo]:
        response = await self._http_client.get(
            f"{self._api_base}/installation/repositories",
            headers={
                "Authorization": f"Bearer {installation_token}",
                "Accept": "application/vnd.github+json",
            },
        )
        response.raise_for_status()
        payload = GitHubInstallationReposResponse.model_validate(response.json())
        return payload.repositories

    async def get_installation(self, installation_id: int) -> GitHubInstallationDetails:
        response = await self._http_client.get(
            f"{self._api_base}/app/installations/{installation_id}",
            headers={
                "Authorization": f"Bearer {self.build_app_jwt()}",
                "Accept": "application/vnd.github+json",
            },
        )
        response.raise_for_status()
        return GitHubInstallationDetails.model_validate(response.json())
