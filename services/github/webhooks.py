from __future__ import annotations

import hashlib
import hmac
import json
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class GitHubWebhookError(RuntimeError):
    pass


class GitHubWebhookEvent(str, Enum):
    INSTALLATION = "installation"
    INSTALLATION_REPOSITORIES = "installation_repositories"


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="ignore", protected_namespaces=())


class GitHubInstallationAccount(ApiModel):
    login: str


class GitHubInstallationPayload(ApiModel):
    id: int
    account: GitHubInstallationAccount


class GitHubRepositoryPayload(ApiModel):
    id: int
    full_name: str
    default_branch: str = Field(default="main")


class InstallationWebhookEvent(ApiModel):
    action: str
    installation: GitHubInstallationPayload


class InstallationRepositoriesEvent(ApiModel):
    action: str
    installation: GitHubInstallationPayload
    repositories_added: list[GitHubRepositoryPayload] = Field(default_factory=list)
    repositories_removed: list[GitHubRepositoryPayload] = Field(default_factory=list)


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if signature_header is None or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def parse_event(
    event_name: str, body: bytes
) -> InstallationWebhookEvent | InstallationRepositoriesEvent:
    payload = json.loads(body)
    if event_name == GitHubWebhookEvent.INSTALLATION.value:
        return InstallationWebhookEvent.model_validate(payload)
    if event_name == GitHubWebhookEvent.INSTALLATION_REPOSITORIES.value:
        return InstallationRepositoriesEvent.model_validate(payload)
    raise GitHubWebhookError(f"unsupported GitHub webhook event: {event_name}")
