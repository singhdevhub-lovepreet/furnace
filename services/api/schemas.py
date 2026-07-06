from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.llm.policy import ModelCatalog, ModelPolicy, ProviderName
from services.sessions.state_machine import SessionStatus


class ApiModel(BaseModel):
    model_config = ConfigDict(protected_namespaces=())


class CreateSessionRequest(ApiModel):
    repo_id: UUID
    prompt: str
    model_policy: ModelPolicy = Field(default_factory=ModelPolicy)


class UserOut(ApiModel):
    id: UUID
    email: str
    plan: str
    created_at: datetime


class AuthSignupRequest(ApiModel):
    email: str
    password: str
    plan: str | None = None

    @field_validator("email", "password")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("plan")
    @classmethod
    def _optional_plan(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("must not be empty")
        return value


class AuthLoginRequest(ApiModel):
    email: str
    password: str

    @field_validator("email", "password")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value


class AuthTokenResponse(ApiModel):
    access_token: str
    token_type: Literal["bearer"]
    user: UserOut


class SessionOut(ApiModel):
    id: UUID
    user_id: UUID
    repo_id: UUID
    prompt: str
    status: SessionStatus
    branch: str
    pr_number: int | None
    model_policy: dict[str, object]
    created_at: datetime
    ended_at: datetime | None


class EventOut(ApiModel):
    id: UUID
    session_id: UUID
    type: str
    payload: dict[str, object]
    ts: datetime


class ArtifactOut(ApiModel):
    id: UUID
    session_id: UUID
    kind: str
    object_key: str
    meta: dict[str, object]


class UsageOut(ApiModel):
    id: UUID
    session_id: UUID
    mac_seconds: int
    prompt_tokens: int
    completion_tokens: int
    mac_cost_usd: str


class RepoOut(ApiModel):
    id: UUID
    installation_id: UUID
    full_name: str
    default_branch: str


class PoolQueueItem(ApiModel):
    session_id: UUID
    position: int
    eta_seconds: int


class PoolScaleDecisionOut(ApiModel):
    current_hosts: int
    desired_hosts: int
    scale_up_by: int
    total_slots: int
    free_slots: int
    active_sessions: int
    queued_sessions: int


class PoolStatusOut(ApiModel):
    active_sessions: int
    capacity: int
    queue_depth: int
    queued: list[PoolQueueItem]
    scale_decision: PoolScaleDecisionOut


class LlmKeyCreateRequest(ApiModel):
    provider: ProviderName
    label: str
    key: str

    @field_validator("label", "key")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value


class LlmKeyOut(ApiModel):
    id: UUID
    provider: ProviderName
    label: str
    created_at: datetime


class LlmKeyListOut(ApiModel):
    items: list[LlmKeyOut]


class ModelCatalogOut(ApiModel):
    catalog: ModelCatalog
