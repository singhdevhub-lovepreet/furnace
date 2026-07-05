from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from services.db.models import Artifact, Event, Repo, UsageRecord
from services.db.models import Session as SessionRow
from services.github.service import NoopCloner, RepoCloner
from services.scheduler.provisioner.base import (
    AcquisitionState,
    MacProvisioner,
    SessionHandle,
    SessionSpec,
)
from services.sessions.pubsub import SessionEventHub
from services.sessions.state_machine import (
    IllegalSessionTransitionError,
    SessionStateMachine,
    SessionStatus,
)


@dataclass(slots=True)
class SessionRuntime:
    handle: SessionHandle | None = None
    task: asyncio.Task[None] | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(slots=True)
class SessionOrchestrator:
    sessionmaker: async_sessionmaker[AsyncSession]
    provisioner: MacProvisioner
    hub: SessionEventHub
    artifacts_dir: Path
    repo_cloner: RepoCloner = field(default_factory=NoopCloner)
    state_machine: SessionStateMachine = field(default_factory=SessionStateMachine)
    step_delay_seconds: float = 0.05
    runtimes: dict[UUID, SessionRuntime] = field(default_factory=dict)

    async def start(self, session_id: UUID) -> None:
        runtime = self.runtimes.setdefault(session_id, SessionRuntime())
        runtime.task = asyncio.create_task(self._run(session_id, runtime))

    async def record_event(
        self,
        session_id: UUID,
        event_type: str,
        payload: dict[str, object],
    ) -> Event:
        return await self._append_event(session_id, event_type, payload)

    async def cancel(self, session_id: UUID) -> None:
        runtime = self.runtimes.setdefault(session_id, SessionRuntime())
        runtime.cancel_event.set()
        if runtime.handle is not None:
            await self.provisioner.release(runtime.handle)
        if runtime.task is not None:
            runtime.task.cancel()

    async def _append_event(
        self,
        session_id: UUID,
        event_type: str,
        payload: dict[str, object],
    ) -> Event:
        async with self.sessionmaker() as db:
            event = Event(session_id=session_id, type=event_type, payload=payload)
            db.add(event)
            await db.flush()
            await db.commit()
            await db.refresh(event)
            await self.hub.publish(session_id, event)
            return event

    async def _set_status(
        self,
        session_id: UUID,
        target: SessionStatus,
        *,
        ended_at: datetime | None = None,
        pr_number: int | None = None,
    ) -> None:
        async with self.sessionmaker() as db:
            row = await db.get(SessionRow, session_id)
            if row is None:
                raise LookupError(f"session {session_id} not found")
            current = SessionStatus(row.status)
            try:
                self.state_machine.transition(current, target)
            except IllegalSessionTransitionError:
                if current == target:
                    return
                raise
            row.status = target.value
            if pr_number is not None:
                row.pr_number = pr_number
            if ended_at is not None:
                row.ended_at = ended_at
            await db.commit()

    async def _create_artifact(
        self,
        session_id: UUID,
        kind: str,
        object_key: str,
        meta: dict[str, object],
    ) -> None:
        async with self.sessionmaker() as db:
            db.add(Artifact(session_id=session_id, kind=kind, object_key=object_key, meta=meta))
            await db.commit()

    async def _create_usage(self, session_id: UUID, mac_seconds: int) -> None:
        async with self.sessionmaker() as db:
            db.add(
                UsageRecord(
                    session_id=session_id,
                    mac_seconds=mac_seconds,
                    prompt_tokens=0,
                    completion_tokens=0,
                    mac_cost_usd=Decimal("0.0000"),
                )
            )
            await db.commit()

    async def _load_session_and_repo(self, session_id: UUID) -> tuple[SessionRow, Repo]:
        async with self.sessionmaker() as db:
            result = await db.execute(
                select(SessionRow)
                .options(selectinload(SessionRow.repo).selectinload(Repo.installation))
                .where(SessionRow.id == session_id)
            )
            session_row = result.scalar_one_or_none()
            if session_row is None:
                raise LookupError(f"session {session_id} not found")
            repo = session_row.repo
            return session_row, repo

    async def _run(self, session_id: UUID, runtime: SessionRuntime) -> None:
        try:
            session_row, repo = await self._load_session_and_repo(session_id)
            _ = session_row
            spec = SessionSpec(
                image_id="sample-image",
                cpu=4,
                memory_hint_mb=8192,
                ttl_seconds=3600,
                idle_timeout_seconds=300,
                warm=True,
                features_required=frozenset({"ARTIFACTS"}),
            )
            await self._append_event(
                session_id,
                "session_started",
                {"status": SessionStatus.QUEUED.value},
            )
            await self._set_status(session_id, SessionStatus.PROVISIONING)
            await self._append_event(
                session_id,
                "status_changed",
                {"status": SessionStatus.PROVISIONING.value},
            )
            outcome = await self.provisioner.acquire(spec)
            runtime.handle = outcome.handle
            if outcome.state == AcquisitionState.QUEUED:
                await self._append_event(
                    session_id,
                    "provisioner_queued",
                    {"eta_seconds": outcome.eta_seconds},
                )
                if runtime.cancel_event.is_set():
                    return
            await asyncio.sleep(self.step_delay_seconds)
            if runtime.cancel_event.is_set():
                return
            await self._set_status(session_id, SessionStatus.CLONING_REPO)
            await self._append_event(
                session_id,
                "status_changed",
                {"status": SessionStatus.CLONING_REPO.value},
            )
            assert runtime.handle is not None
            await self.repo_cloner.prepare(runtime.handle, repo)
            await asyncio.sleep(self.step_delay_seconds)
            if runtime.cancel_event.is_set():
                return
            await self._set_status(session_id, SessionStatus.RUNNING)
            await self._append_event(
                session_id,
                "status_changed",
                {"status": SessionStatus.RUNNING.value},
            )
            await asyncio.sleep(self.step_delay_seconds)
            if runtime.cancel_event.is_set():
                return
            await self._set_status(session_id, SessionStatus.RECORDING)
            await self._append_event(
                session_id,
                "status_changed",
                {"status": SessionStatus.RECORDING.value},
            )
            await asyncio.sleep(self.step_delay_seconds)
            if runtime.cancel_event.is_set():
                return
            await self._set_status(session_id, SessionStatus.RUNNING)
            await self._append_event(
                session_id,
                "status_changed",
                {"status": SessionStatus.RUNNING.value},
            )
            await asyncio.sleep(self.step_delay_seconds)
            if runtime.cancel_event.is_set():
                return
            await self._set_status(session_id, SessionStatus.OPENING_PR)
            await self._append_event(
                session_id,
                "status_changed",
                {"status": SessionStatus.OPENING_PR.value},
            )

            artifacts_dir = self.artifacts_dir / str(session_id)
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = artifacts_dir / "screenshot.png"
            video_path = artifacts_dir / "demo.mp4"
            screenshot_bytes = await self.provisioner.get_file(
                runtime.handle,
                "artifacts/screenshot.png",
            )
            video_bytes = await self.provisioner.get_file(runtime.handle, "artifacts/demo.mp4")
            screenshot_path.write_bytes(screenshot_bytes)
            video_path.write_bytes(video_bytes)
            await self._create_artifact(
                session_id,
                "screenshot",
                str(screenshot_path),
                {"filename": screenshot_path.name},
            )
            await self._create_artifact(
                session_id,
                "video",
                str(video_path),
                {"filename": video_path.name},
            )
            await self._create_usage(session_id, mac_seconds=8)
            await self._append_event(
                session_id,
                "status_changed",
                {"status": SessionStatus.SUCCEEDED.value},
            )
            await self._set_status(
                session_id,
                SessionStatus.SUCCEEDED,
                ended_at=datetime.now(UTC),
                pr_number=1,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - surfaced by tests
            await self._append_event(session_id, "error", {"message": str(exc)})
            try:
                await self._set_status(
                    session_id,
                    SessionStatus.FAILED,
                    ended_at=datetime.now(UTC),
                )
            except IllegalSessionTransitionError:
                pass
        finally:
            if runtime.handle is not None:
                await self.provisioner.release(runtime.handle)
