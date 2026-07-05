from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.db.models import LlmKey, UsageRecord
from services.db.models import Session as SessionRow
from services.llm.policy import ModelPolicy, resolve
from services.vault.key_vault import KeyVault


class _CompletionMessage(Protocol):
    content: str | None


class _CompletionChoice(Protocol):
    message: _CompletionMessage


class _CompletionUsage(Protocol):
    prompt_tokens: int | None
    completion_tokens: int | None


class _CompletionResponse(Protocol):
    choices: Sequence[_CompletionChoice]
    usage: _CompletionUsage | None


@dataclass(slots=True)
class CompletionResult:
    text: str
    prompt_tokens: int
    completion_tokens: int


CompletionFn = Callable[[str, str, list[dict[str, str]], str], Awaitable[CompletionResult]]


async def _default_completion_fn(
    provider: str,
    model: str,
    messages: list[dict[str, str]],
    api_key: str,
) -> CompletionResult:
    from litellm import acompletion as litellm_acompletion

    raw_response = await litellm_acompletion(model=model, messages=messages, api_key=api_key)
    response = cast(_CompletionResponse, raw_response)
    text = response.choices[0].message.content or ""
    usage = response.usage
    prompt_tokens = 0 if usage is None or usage.prompt_tokens is None else usage.prompt_tokens
    completion_tokens = (
        0 if usage is None or usage.completion_tokens is None else usage.completion_tokens
    )
    _ = provider
    return CompletionResult(
        text=text, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
    )


@dataclass(slots=True)
class ModelRouter:
    sessionmaker: async_sessionmaker[AsyncSession]
    key_vault: KeyVault
    completion_fn: CompletionFn = _default_completion_fn

    async def complete(self, session_id: UUID, role: str, messages: list[dict[str, str]]) -> str:
        async with self.sessionmaker() as db:
            session = await db.get(SessionRow, session_id)
            if session is None:
                raise LookupError(f"session {session_id} not found")
            policy = ModelPolicy.model_validate(session.model_policy)
            selection = resolve(policy, role)
            key_row = await self._load_user_key(db, session.user_id, selection.provider.value)
            api_key = self.key_vault.decrypt(key_row.enc_key)
            result = await self.completion_fn(
                selection.provider.value, selection.model, messages, api_key
            )
            await self._apply_usage(db, session.id, result.prompt_tokens, result.completion_tokens)
            await db.commit()
            return result.text

    async def _load_user_key(
        self,
        db: AsyncSession,
        user_id: UUID,
        provider: str,
    ) -> LlmKey:
        result = await db.execute(
            select(LlmKey)
            .where(LlmKey.user_id == user_id, LlmKey.provider == provider)
            .order_by(LlmKey.created_at)
        )
        key_row = result.scalar_one_or_none()
        if key_row is None:
            raise LookupError(f"no LLM key found for provider {provider!r}")
        return key_row

    async def _apply_usage(
        self,
        db: AsyncSession,
        session_id: UUID,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        result = await db.execute(select(UsageRecord).where(UsageRecord.session_id == session_id))
        usage = result.scalar_one_or_none()
        if usage is None:
            usage = UsageRecord(
                session_id=session_id,
                mac_seconds=0,
                prompt_tokens=0,
                completion_tokens=0,
                mac_cost_usd=Decimal("0.0000"),
            )
            db.add(usage)
        usage.prompt_tokens += prompt_tokens
        usage.completion_tokens += completion_tokens
