"""EC2 Mac + tart provisioner (M3).

Driver layout:

    Ec2TartProvisioner
      ├── HostDriver        — dedicated-host lifecycle (allocate / list / release)
      ├── TartVmRuntime     — tart clone/run/delete + worker boot on a mac host
      └── WorkerToolClient  — HTTP tool-RPC to the worker daemon inside the VM

The AWS and SSH edges live behind ``HostDriver`` / ``TartVmRuntime`` so the
whole session lifecycle is unit-testable on Linux; the real implementations
are in ``services/scheduler/provisioner/aws.py``.
"""

from __future__ import annotations

import asyncio
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol
from uuid import UUID, uuid4

import httpx

from apps.worker.client import WorkerToolClient
from services.scheduler.provisioner.base import (
    AcquireOutcome,
    AcquisitionState,
    CapacityReport,
    Channel,
    ChannelKind,
    MacProvisioner,
    ProviderCapabilities,
    ProvisionerSessionStatus,
    SessionHandle,
    SessionSpec,
)

VM_NAME_PREFIX = "raven-"

# EC2 Mac dedicated hosts have a 24h minimum allocation period.
MAC_HOST_MIN_ALLOC_SECONDS = 24 * 60 * 60


class HostState(str, Enum):
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    RELEASED = "RELEASED"


@dataclass(frozen=True, slots=True)
class MacHostInfo:
    host_id: str
    state: HostState
    address: str
    allocated_at: datetime
    min_release_at: datetime


class HostDriver(ABC):
    """Dedicated EC2 Mac host lifecycle (allocation, discovery, release)."""

    @abstractmethod
    async def list_hosts(self) -> list[MacHostInfo]:
        raise NotImplementedError

    @abstractmethod
    async def provision_host(self) -> MacHostInfo:
        raise NotImplementedError

    @abstractmethod
    async def release_host(self, host_id: str) -> None:
        raise NotImplementedError


class TartVmRuntime(ABC):
    """tart VM lifecycle + worker daemon boot, executed on a mac host."""

    @abstractmethod
    async def clone_vm(self, host: MacHostInfo, source_image: str, vm_name: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def start_vm(self, host: MacHostInfo, vm_name: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def start_worker(self, host: MacHostInfo, vm_name: str, token: str) -> str:
        """Start the worker daemon in the VM; returns its base URL."""
        raise NotImplementedError

    @abstractmethod
    async def stop_vm(self, host: MacHostInfo, vm_name: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def delete_vm(self, host: MacHostInfo, vm_name: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_vms(self, host: MacHostInfo) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    async def worker_endpoint(self, host: MacHostInfo, vm_name: str) -> str:
        """Recompute the worker base URL for an already-running VM (reconcile)."""
        raise NotImplementedError


class WorkerClientFactory(Protocol):
    def __call__(self, base_url: str, token: str) -> WorkerToolClient: ...


def _default_client_factory(base_url: str, token: str) -> WorkerToolClient:
    return WorkerToolClient(base_url, token=token)


def vm_name_for(handle_id: UUID) -> str:
    return f"{VM_NAME_PREFIX}{handle_id}"


def _handle_id_from_vm_name(vm_name: str) -> UUID | None:
    if not vm_name.startswith(VM_NAME_PREFIX):
        return None
    try:
        return UUID(vm_name[len(VM_NAME_PREFIX) :])
    except ValueError:
        return None


@dataclass(slots=True)
class _VmSession:
    handle: SessionHandle
    host: MacHostInfo
    vm_name: str
    token: str
    client: WorkerToolClient


@dataclass(slots=True)
class _EndpointChannel:
    kind: ChannelKind
    payload: bytes
    closed: bool = False

    def read(self) -> bytes:
        return self.payload

    def close(self) -> None:
        self.closed = True


@dataclass(slots=True)
class Ec2TartProvisioner(MacProvisioner):
    """EC2 Mac + tart driver: one tart VM cloned per session on a warm host."""

    host_driver: HostDriver
    vm_runtime: TartVmRuntime
    golden_vm: str
    vms_per_host: int = 2
    silicon_gen: str = "m2"
    provider_name: str = "ec2_tart"
    est_new_slot_seconds: int = 900
    health_retries: int = 10
    health_retry_delay_seconds: float = 1.0
    release_idle_hosts: bool = True
    client_factory: WorkerClientFactory = _default_client_factory
    _sessions: dict[UUID, _VmSession] = field(default_factory=dict)
    _queued: set[UUID] = field(default_factory=set)
    _reconciled: dict[UUID, tuple[MacHostInfo, str]] = field(default_factory=dict)
    _provision_tasks: set[asyncio.Task[MacHostInfo]] = field(default_factory=set)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def acquire(self, spec: SessionSpec) -> AcquireOutcome:
        _ = spec
        async with self._lock:
            host = await self._find_free_host_locked()
            if host is None:
                handle = SessionHandle(
                    id=uuid4(),
                    provider=self.provider_name,
                    created_at=datetime.now(UTC),
                    tool_endpoint="pending://ec2-tart",
                )
                self._queued.add(handle.id)
                self._kick_host_provisioning_locked()
                return AcquireOutcome(AcquisitionState.QUEUED, handle, self.est_new_slot_seconds)
            handle_id = uuid4()
            vm_name = vm_name_for(handle_id)
            # Reserve the slot before releasing the lock for the slow clone/boot.
            placeholder = SessionHandle(
                id=handle_id,
                provider=self.provider_name,
                created_at=datetime.now(UTC),
                tool_endpoint="pending://ec2-tart",
            )
            token = secrets.token_urlsafe(32)
            reserved = _VmSession(
                handle=placeholder,
                host=host,
                vm_name=vm_name,
                token=token,
                client=self.client_factory("http://pending.invalid", token),
            )
            self._sessions[handle_id] = reserved
        try:
            await self.vm_runtime.clone_vm(host, self.golden_vm, vm_name)
            await self.vm_runtime.start_vm(host, vm_name)
            endpoint = await self.vm_runtime.start_worker(host, vm_name, token)
            client = self.client_factory(endpoint, token)
            await self._wait_healthy(client)
        except Exception:
            async with self._lock:
                self._sessions.pop(handle_id, None)
            await self._cleanup_vm(host, vm_name)
            raise
        handle = SessionHandle(
            id=handle_id,
            provider=self.provider_name,
            created_at=placeholder.created_at,
            tool_endpoint=endpoint,
        )
        async with self._lock:
            await reserved.client.aclose()
            self._sessions[handle_id] = _VmSession(
                handle=handle, host=host, vm_name=vm_name, token=token, client=client
            )
        return AcquireOutcome(AcquisitionState.READY, handle, None)

    async def release(self, handle: SessionHandle) -> None:
        async with self._lock:
            session = self._sessions.pop(handle.id, None)
            self._queued.discard(handle.id)
            reconciled = self._reconciled.pop(handle.id, None)
        if session is not None:
            await session.client.aclose()
            await self._cleanup_vm(session.host, session.vm_name)
        elif reconciled is not None:
            host, vm_name = reconciled
            await self._cleanup_vm(host, vm_name)
        if self.release_idle_hosts:
            await self._release_idle_hosts()

    async def status(self, handle: SessionHandle) -> ProvisionerSessionStatus:
        async with self._lock:
            if handle.id in self._sessions or handle.id in self._reconciled:
                return ProvisionerSessionStatus.READY
            if handle.id in self._queued:
                return ProvisionerSessionStatus.QUEUED
        return ProvisionerSessionStatus.GONE

    async def exec(
        self,
        handle: SessionHandle,
        command: str,
        argv: list[str],
        env: dict[str, str],
        timeout_seconds: int,
    ) -> tuple[int, str, str]:
        client = self._client_for(handle)
        result = await client.exec([command, *argv], env=env, timeout_seconds=timeout_seconds)
        rc = result.rc if result.rc is not None else (0 if result.ok else -1)
        stderr = result.stderr if result.stderr else result.error
        return rc, result.stdout, stderr

    async def open_channel(self, handle: SessionHandle, kind: ChannelKind) -> Channel:
        if kind is not ChannelKind.TOOL_RPC:
            raise NotImplementedError(f"channel kind {kind.value} not supported by ec2_tart")
        session = self._sessions.get(handle.id)
        if session is None:
            raise KeyError(f"unknown session handle {handle.id}")
        return _EndpointChannel(kind=kind, payload=session.handle.tool_endpoint.encode())

    async def put_file(self, handle: SessionHandle, path: str, content: bytes) -> None:
        client = self._client_for(handle)
        result = await client.write_file_bytes(path, content)
        if not result.ok:
            raise RuntimeError(f"put_file failed for {path!r}: {result.error}")

    async def get_file(self, handle: SessionHandle, path: str) -> bytes:
        client = self._client_for(handle)
        return await client.read_file_bytes(path)

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            max_sessions_per_host=self.vms_per_host,
            nested_vm_cap=self.vms_per_host,
            silicon_gen=self.silicon_gen,
            supports_vnc=False,
            min_alloc_seconds=MAC_HOST_MIN_ALLOC_SECONDS,
            features=frozenset({"tart", "worker_rpc"}),
        )

    async def capacity(self) -> CapacityReport:
        hosts = await self.host_driver.list_hosts()
        ready = [host for host in hosts if host.state is HostState.READY]
        provisioning = [host for host in hosts if host.state is HostState.PROVISIONING]
        total_slots = len(ready) * self.vms_per_host
        async with self._lock:
            active = len(self._sessions) + len(self._reconciled)
        return CapacityReport(
            free_slots=max(0, total_slots - active),
            total_slots=total_slots,
            warm_hosts=len(ready),
            provisioning_hosts=len(provisioning),
            est_new_slot_seconds=self.est_new_slot_seconds,
        )

    async def reconcile(self) -> list[SessionHandle]:
        hosts = await self.host_driver.list_hosts()
        handles: list[SessionHandle] = []
        async with self._lock:
            known = set(self._sessions)
            self._reconciled.clear()
            for host in hosts:
                if host.state is not HostState.READY:
                    continue
                for vm_name in await self.vm_runtime.list_vms(host):
                    handle_id = _handle_id_from_vm_name(vm_name)
                    if handle_id is None:
                        continue
                    if handle_id in known:
                        handles.append(self._sessions[handle_id].handle)
                        continue
                    endpoint = await self.vm_runtime.worker_endpoint(host, vm_name)
                    handle = SessionHandle(
                        id=handle_id,
                        provider=self.provider_name,
                        created_at=host.allocated_at,
                        tool_endpoint=endpoint,
                    )
                    self._reconciled[handle_id] = (host, vm_name)
                    handles.append(handle)
        return handles

    def _client_for(self, handle: SessionHandle) -> WorkerToolClient:
        session = self._sessions.get(handle.id)
        if session is None:
            raise KeyError(f"unknown session handle {handle.id}")
        return session.client

    async def _find_free_host_locked(self) -> MacHostInfo | None:
        hosts = await self.host_driver.list_hosts()
        used: dict[str, int] = {}
        for session in self._sessions.values():
            used[session.host.host_id] = used.get(session.host.host_id, 0) + 1
        for host, vm_name in self._reconciled.values():
            _ = vm_name
            used[host.host_id] = used.get(host.host_id, 0) + 1
        for host in hosts:
            if host.state is not HostState.READY:
                continue
            if used.get(host.host_id, 0) < self.vms_per_host:
                return host
        return None

    def _kick_host_provisioning_locked(self) -> None:
        if self._provision_tasks:
            return
        task = asyncio.ensure_future(self.host_driver.provision_host())
        self._provision_tasks.add(task)
        task.add_done_callback(self._provision_tasks.discard)

    async def _wait_healthy(self, client: WorkerToolClient) -> None:
        last_error: Exception | None = None
        for _attempt in range(self.health_retries):
            try:
                await client.healthz()
                return
            except (httpx.HTTPError, OSError) as exc:
                last_error = exc
                await asyncio.sleep(self.health_retry_delay_seconds)
        raise RuntimeError(f"worker daemon never became healthy: {last_error}")

    async def _cleanup_vm(self, host: MacHostInfo, vm_name: str) -> None:
        try:
            await self.vm_runtime.stop_vm(host, vm_name)
        finally:
            await self.vm_runtime.delete_vm(host, vm_name)

    async def _release_idle_hosts(self) -> None:
        now = datetime.now(UTC)
        async with self._lock:
            busy_host_ids = {session.host.host_id for session in self._sessions.values()}
            busy_host_ids |= {host.host_id for host, _ in self._reconciled.values()}
        for host in await self.host_driver.list_hosts():
            if host.state is not HostState.READY:
                continue
            if host.host_id in busy_host_ids:
                continue
            if now < host.min_release_at:
                continue
            await self.host_driver.release_host(host.host_id)
