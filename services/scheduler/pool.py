from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from math import ceil
from uuid import UUID

from services.scheduler.provisioner.base import (
    AcquireOutcome,
    CapacityReport,
    MacProvisioner,
    ProviderCapabilities,
    SessionHandle,
    SessionSpec,
)

QueuedCallback = Callable[[int, int], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class QueueEntrySnapshot:
    session_id: UUID
    position: int
    eta_seconds: int


@dataclass(frozen=True, slots=True)
class ScaleDecision:
    current_hosts: int
    desired_hosts: int
    scale_up_by: int
    total_slots: int
    free_slots: int
    active_sessions: int
    queued_sessions: int


@dataclass(frozen=True, slots=True)
class PoolSnapshot:
    active_sessions: int
    capacity: int
    queue_depth: int
    queued: list[QueueEntrySnapshot]
    scale_decision: ScaleDecision


@dataclass(slots=True)
class _QueueEntry:
    session_id: UUID
    spec: SessionSpec
    future: asyncio.Future[None]
    eta_seconds: int
    position: int
    on_queued: QueuedCallback | None


@dataclass(slots=True)
class PoolController:
    provisioner: MacProvisioner
    capacity_override: int | None = None
    estimated_session_seconds: int = 30
    scale_up_threshold: int = 1
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _queue: deque[_QueueEntry] = field(default_factory=deque)
    _active_handles: dict[UUID, SessionHandle] = field(default_factory=dict)
    _active_handles_by_id: dict[UUID, SessionHandle] = field(default_factory=dict)
    _reserved_sessions: set[UUID] = field(default_factory=set)
    _reconciled_handles: dict[UUID, SessionHandle] = field(default_factory=dict)
    _capacity_report: CapacityReport | None = None
    _capabilities: ProviderCapabilities | None = None

    async def acquire(
        self,
        session_id: UUID,
        spec: SessionSpec,
        on_queued: QueuedCallback | None = None,
    ) -> SessionHandle:
        while True:
            await self._refresh_metadata()
            async with self._lock:
                if session_id in self._active_handles:
                    return self._active_handles[session_id]
                if self._can_admit_locked():
                    self._reserved_sessions.add(session_id)
                    break
                loop = asyncio.get_running_loop()
                future: asyncio.Future[None] = loop.create_future()
                position = len(self._queue) + 1
                eta_seconds = self._queued_eta_locked(position)
                self._queue.append(
                    _QueueEntry(
                        session_id=session_id,
                        spec=spec,
                        future=future,
                        eta_seconds=eta_seconds,
                        position=position,
                        on_queued=on_queued,
                    )
                )
            if on_queued is not None:
                await on_queued(eta_seconds, position)
            try:
                await future
            except asyncio.CancelledError:
                await self._cancel_queued(session_id)
                raise
            return await self._finalize_acquire(session_id, spec)

        return await self._finalize_acquire(session_id, spec)

    async def release(self, session_id: UUID, handle: SessionHandle) -> None:
        await self.provisioner.release(handle)
        await self._refresh_metadata()
        async with self._lock:
            removed = self._active_handles.pop(session_id, None)
            if removed is not None:
                self._active_handles_by_id.pop(removed.id, None)
            else:
                self._active_handles_by_id.pop(handle.id, None)
            self._reconciled_handles.pop(handle.id, None)
            await self._drain_locked()

    async def status(self, handle: SessionHandle) -> str:
        _ = handle
        return "READY"

    async def snapshot(self) -> PoolSnapshot:
        await self._refresh_metadata()
        async with self._lock:
            return self._snapshot_locked()

    async def reconcile(self) -> list[SessionHandle]:
        handles = await self.provisioner.reconcile()
        await self._refresh_metadata()
        async with self._lock:
            active_handle_ids = set(self._active_handles_by_id)
            self._reconciled_handles = {
                handle.id: handle for handle in handles if handle.id not in active_handle_ids
            }
            await self._drain_locked()
        return handles

    async def _refresh_metadata(self) -> None:
        capacity_report, capabilities = await asyncio.gather(
            self.provisioner.capacity(), self.provisioner.capabilities()
        )
        async with self._lock:
            self._capacity_report = capacity_report
            self._capabilities = capabilities

    def _capacity_locked(self) -> int:
        report = self._capacity_report
        if report is None:
            return 0
        if self.capacity_override is None:
            return report.total_slots
        return min(report.total_slots, self.capacity_override)

    def _active_count_locked(self) -> int:
        return (
            len(self._active_handles) + len(self._reconciled_handles) + len(self._reserved_sessions)
        )

    def _free_slots_locked(self) -> int:
        return max(0, self._capacity_locked() - self._active_count_locked())

    def _can_admit_locked(self) -> bool:
        return not self._queue and self._free_slots_locked() > 0

    def _queued_eta_locked(self, position: int) -> int:
        return position * self.estimated_session_seconds

    async def _cancel_queued(self, session_id: UUID) -> None:
        async with self._lock:
            for _index, entry in enumerate(self._queue):
                if entry.session_id == session_id:
                    self._queue.remove(entry)
                    break
            # If the session was already reserved by _drain_locked (its future was
            # resolved) but is cancelled before finalizing the acquire, drop the
            # reservation so the freed slot is not leaked.
            self._reserved_sessions.discard(session_id)
            await self._drain_locked()

    async def _finalize_acquire(self, session_id: UUID, spec: SessionSpec) -> SessionHandle:
        try:
            outcome: AcquireOutcome = await self.provisioner.acquire(spec)
        except asyncio.CancelledError:
            async with self._lock:
                self._reserved_sessions.discard(session_id)
                await self._drain_locked()
            raise
        async with self._lock:
            self._reserved_sessions.discard(session_id)
            self._active_handles[session_id] = outcome.handle
            self._active_handles_by_id[outcome.handle.id] = outcome.handle
            self._reconciled_handles.pop(outcome.handle.id, None)
            await self._drain_locked()
            return outcome.handle

    async def _drain_locked(self) -> None:
        while self._queue and self._free_slots_locked() > 0:
            entry = self._queue.popleft()
            if entry.future.done():
                continue
            self._reserved_sessions.add(entry.session_id)
            entry.future.set_result(None)

    def _snapshot_locked(self) -> PoolSnapshot:
        report = self._capacity_report
        capabilities = self._capabilities
        active_sessions = (
            len(self._active_handles) + len(self._reconciled_handles) + len(self._reserved_sessions)
        )
        capacity = self._capacity_locked()
        queue_depth = len(self._queue)
        queued = [
            QueueEntrySnapshot(
                session_id=entry.session_id,
                position=index,
                eta_seconds=entry.eta_seconds,
            )
            for index, entry in enumerate(self._queue, start=1)
        ]
        current_hosts = 0 if report is None else report.warm_hosts + report.provisioning_hosts
        max_sessions_per_host = (
            1 if capabilities is None else max(1, capabilities.max_sessions_per_host)
        )
        demand = active_sessions + queue_depth
        desired_total_slots = capacity
        if queue_depth > 0 or self._free_slots_locked() < self.scale_up_threshold:
            desired_total_slots = max(capacity, demand)
        desired_hosts = ceil(desired_total_slots / max_sessions_per_host)
        scale_up_by = max(0, desired_hosts - current_hosts)
        scale_decision = ScaleDecision(
            current_hosts=current_hosts,
            desired_hosts=desired_hosts,
            scale_up_by=scale_up_by,
            total_slots=capacity,
            free_slots=self._free_slots_locked(),
            active_sessions=active_sessions,
            queued_sessions=queue_depth,
        )
        return PoolSnapshot(
            active_sessions=active_sessions,
            capacity=capacity,
            queue_depth=queue_depth,
            queued=queued,
            scale_decision=scale_decision,
        )
