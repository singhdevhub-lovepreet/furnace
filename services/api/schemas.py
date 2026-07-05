from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.sessions.state_machine import SessionStatus


class ApiModel(BaseModel):
    model_config = ConfigDict(protected_namespaces=())


class ModelPolicy(ApiModel):
    planner: str | None = None
    coder: str | None = None
    summarizer: str | None = None

    @field_validator("planner", "coder", "summarizer")
    @classmethod
    def _non_empty(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not value.strip():
            raise ValueError("must not be empty")
        return value


class CreateSessionRequest(ApiModel):
    repo_id: UUID
    prompt: str
    model_policy: ModelPolicy = Field(default_factory=ModelPolicy)


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
