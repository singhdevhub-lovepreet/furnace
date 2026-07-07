from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.config import Settings
from services.db.models import GithubInstallation, Repo
from services.github.client import GitHubAppClient
from services.github.webhooks import (
    InstallationRepositoriesEvent,
    InstallationWebhookEvent,
    parse_event,
    verify_signature,
)
from services.scheduler.provisioner.base import MacProvisioner, SessionHandle


class RepoCloner(Protocol):
    async def prepare(self, handle: SessionHandle, repo: Repo) -> None: ...


@dataclass(slots=True)
class NoopCloner:
    async def prepare(self, handle: SessionHandle, repo: Repo) -> None:
        _ = (handle, repo)


@dataclass(slots=True)
class GitHubService:
    sessionmaker: async_sessionmaker[AsyncSession]
    client: GitHubAppClient
    settings: Settings

    def build_install_url(self, state: str) -> str:
        slug = self.settings.github_app_slug
        if slug is None:
            raise ValueError("GitHub app slug is not configured")
        return f"https://github.com/apps/{slug}/installations/new?state={quote(state)}"

    def _secret(self) -> str:
        secret = self.settings.github_webhook_secret or self.settings.github_app_private_key
        if secret is None:
            raise ValueError("GitHub webhook/app secret is not configured")
        return secret

    def verify_webhook(self, body: bytes, signature_header: str | None) -> bool:
        return verify_signature(self._secret(), body, signature_header)

    async def bind_installation(self, installation_id: int, user_id: UUID) -> None:
        installation_details = await self.client.get_installation(installation_id)
        async with self.sessionmaker() as db:
            result = await db.execute(
                select(GithubInstallation).where(
                    GithubInstallation.installation_id == installation_id
                )
            )
            installation = result.scalar_one_or_none()
            if installation is None:
                installation = GithubInstallation(
                    user_id=user_id,
                    installation_id=installation_id,
                    account_login=installation_details.account.login,
                )
                db.add(installation)
            else:
                installation.user_id = user_id
                installation.account_login = installation_details.account.login
            await db.flush()
            await self._sync_installation_repos(db, installation)
            await db.commit()

    async def handle_webhook(self, event_name: str, body: bytes) -> None:
        event = parse_event(event_name, body)
        if isinstance(event, InstallationWebhookEvent):
            if event.action == "created":
                await self._sync_existing_installation_and_repos(
                    event.installation.id,
                    event.installation.account.login,
                )
            elif event.action == "deleted":
                await self._delete_installation(event.installation.id)
            return
        if isinstance(event, InstallationRepositoriesEvent):
            if event.action in {"added", "removed"}:
                await self._sync_existing_installation(event.installation.id)
            return
        raise ValueError("unhandled webhook event")

    async def mint_repo_installation_token(self, repo: Repo) -> str:
        async with self.sessionmaker() as db:
            installation = await self._get_installation(db, repo)
            token = await self.client.mint_installation_token(
                installation.installation_id,
                permissions={"contents": "read"},
            )
            return token.token

    async def _get_installation(self, db: AsyncSession, repo: Repo) -> GithubInstallation:
        result = await db.execute(
            select(GithubInstallation).where(GithubInstallation.id == repo.installation_id)
        )
        installation = result.scalar_one_or_none()
        if installation is None:
            raise ValueError("repo is not connected to a GitHub installation")
        return installation

    async def _sync_existing_installation_and_repos(
        self,
        installation_id: int,
        account_login: str,
    ) -> None:
        async with self.sessionmaker() as db:
            result = await db.execute(
                select(GithubInstallation).where(
                    GithubInstallation.installation_id == installation_id
                )
            )
            installation = result.scalar_one_or_none()
            if installation is None:
                return
            installation.account_login = account_login
            await db.flush()
            await self._sync_installation_repos(db, installation)
            await db.commit()

    async def _sync_existing_installation(self, installation_id: int) -> None:
        async with self.sessionmaker() as db:
            result = await db.execute(
                select(GithubInstallation).where(
                    GithubInstallation.installation_id == installation_id
                )
            )
            installation = result.scalar_one_or_none()
            if installation is None:
                return
            await self._sync_installation_repos(db, installation)
            await db.commit()

    async def _delete_installation(self, installation_id: int) -> None:
        async with self.sessionmaker() as db:
            result = await db.execute(
                select(GithubInstallation).where(
                    GithubInstallation.installation_id == installation_id
                )
            )
            installation = result.scalar_one_or_none()
            if installation is None:
                return
            await db.execute(delete(Repo).where(Repo.installation_id == installation.id))
            await db.delete(installation)
            await db.commit()

    async def _sync_installation_repos(
        self,
        db: AsyncSession,
        installation: GithubInstallation,
    ) -> None:
        token = await self.client.mint_installation_token(
            installation.installation_id,
            permissions={"contents": "read"},
        )
        repositories = await self.client.list_installation_repos(token.token)
        result = await db.execute(select(Repo).where(Repo.installation_id == installation.id))
        existing_by_name = {row.full_name: row for row in result.scalars().all()}
        for repository in repositories:
            existing = existing_by_name.pop(repository.full_name, None)
            if existing is None:
                db.add(
                    Repo(
                        installation_id=installation.id,
                        full_name=repository.full_name,
                        default_branch=repository.default_branch,
                    )
                )
            else:
                existing.default_branch = repository.default_branch
        if existing_by_name:
            await db.execute(
                delete(Repo).where(
                    Repo.installation_id == installation.id,
                    Repo.full_name.in_(list(existing_by_name.keys())),
                )
            )


@dataclass(slots=True)
class GitHubCloner:
    github: GitHubService
    provisioner: MacProvisioner
    clone_base_dir: str = "/tmp/raven-clones"
    token_file_path: str = "/tmp/raven-github-installation-token"

    async def prepare(self, handle: SessionHandle, repo: Repo) -> None:
        token = await self.github.mint_repo_installation_token(repo)
        await self.provisioner.put_file(handle, self.token_file_path, token.encode("utf-8"))
        destination = f"{self.clone_base_dir}/{repo.id}"
        command = (
            "set -euo pipefail; "
            f"trap 'rm -f {shlex.quote(self.token_file_path)}' EXIT; "
            f"mkdir -p {shlex.quote(self.clone_base_dir)}; "
            f"TOKEN=$(cat {shlex.quote(self.token_file_path)}); "
            "AUTH=$(printf 'x-access-token:%s' \"$TOKEN\" | base64 | tr -d '\n'); "
            f'git -c http.extraHeader="AUTHORIZATION: basic $AUTH" clone '
            f"https://github.com/{repo.full_name}.git {shlex.quote(destination)}"
        )
        await self.provisioner.exec(handle, "bash", ["-lc", command], {}, timeout_seconds=600)
