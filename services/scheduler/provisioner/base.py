from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable
from uuid import UUID


class ChannelKind(str, Enum):
    LOGS = "LOGS"
    VNC = "VNC"
    ARTIFACTS = "ARTIFACTS"
    TOOL_RPC = "TOOL_RPC"


class AcquisitionState(str, Enum):
    READY = "READY"
    QUEUED = "QUEUED"


class ProvisionerSessionStatus(str, Enum):
    PROVISIONING = "PROVISIONING"
    QUEUED = "QUEUED"
    READY = "READY"
    DRAINING = "DRAINING"
    GONE = "GONE"


@dataclass(frozen=True, slots=True)
class SessionSpec:
    image_id: str
    cpu: int
    memory_hint_mb: int
    ttl_seconds: int
    idle_timeout_seconds: int
    warm: bool = True
    features_required: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class SessionHandle:
    id: UUID
    provider: str
    created_at: datetime
    tool_endpoint: str


@dataclass(frozen=True, slots=True)
class AcquireOutcome:
    state: AcquisitionState
    handle: SessionHandle
    eta_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    max_sessions_per_host: int
    nested_vm_cap: int
    silicon_gen: str
    supports_vnc: bool
    min_alloc_seconds: int
    features: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class CapacityReport:
    free_slots: int
    total_slots: int
    warm_hosts: int
    provisioning_hosts: int
    est_new_slot_seconds: int


@runtime_checkable
class Channel(Protocol):
    kind: ChannelKind

    def read(self) -> bytes: ...

    def close(self) -> None: ...


class MacProvisioner(ABC):
    @abstractmethod
    async def acquire(self, spec: SessionSpec) -> AcquireOutcome:
        raise NotImplementedError

    @abstractmethod
    async def release(self, handle: SessionHandle) -> None:
        raise NotImplementedError

    @abstractmethod
    async def status(self, handle: SessionHandle) -> ProvisionerSessionStatus:
        raise NotImplementedError

    @abstractmethod
    async def exec(
        self,
        handle: SessionHandle,
        command: str,
        argv: list[str],
        env: dict[str, str],
        timeout_seconds: int,
    ) -> tuple[int, str, str]:
        raise NotImplementedError

    @abstractmethod
    async def open_channel(self, handle: SessionHandle, kind: ChannelKind) -> Channel:
        raise NotImplementedError

    @abstractmethod
    async def put_file(self, handle: SessionHandle, path: str, content: bytes) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_file(self, handle: SessionHandle, path: str) -> bytes:
        raise NotImplementedError

    @abstractmethod
    async def capabilities(self) -> ProviderCapabilities:
        raise NotImplementedError

    @abstractmethod
    async def capacity(self) -> CapacityReport:
        raise NotImplementedError

    @abstractmethod
    async def reconcile(self) -> list[SessionHandle]:
        raise NotImplementedError
