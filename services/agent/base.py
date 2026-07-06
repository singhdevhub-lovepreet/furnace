from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from services.scheduler.provisioner.base import SessionHandle

EmitFn = Callable[[str, dict[str, object]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class AgentContext:
    session_id: UUID
    prompt: str
    repo_full_name: str
    handle: SessionHandle


@dataclass(frozen=True, slots=True)
class AgentEvent:
    type: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class AgentResult:
    success: bool
    summary: str
    steps: int
    changed_files: list[str]


class AgentRunner(ABC):
    @abstractmethod
    async def run(
        self,
        ctx: AgentContext,
        *,
        emit: EmitFn,
        cancel_event: asyncio.Event,
    ) -> AgentResult:
        raise NotImplementedError
