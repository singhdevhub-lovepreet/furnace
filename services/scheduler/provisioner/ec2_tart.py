from __future__ import annotations

from services.scheduler.provisioner.base import (
    AcquireOutcome,
    CapacityReport,
    Channel,
    ChannelKind,
    MacProvisioner,
    ProviderCapabilities,
    ProvisionerSessionStatus,
    SessionHandle,
    SessionSpec,
)

_MSG = "Ec2TartProvisioner will be implemented in M3."


class Ec2TartProvisioner(MacProvisioner):
    """EC2+tart driver placeholder for M3."""

    async def acquire(self, spec: SessionSpec) -> AcquireOutcome:
        raise NotImplementedError(_MSG)

    async def release(self, handle: SessionHandle) -> None:
        raise NotImplementedError(_MSG)

    async def status(self, handle: SessionHandle) -> ProvisionerSessionStatus:
        raise NotImplementedError(_MSG)

    async def exec(
        self,
        handle: SessionHandle,
        command: str,
        argv: list[str],
        env: dict[str, str],
        timeout_seconds: int,
    ) -> tuple[int, str, str]:
        raise NotImplementedError(_MSG)

    async def open_channel(self, handle: SessionHandle, kind: ChannelKind) -> Channel:
        raise NotImplementedError(_MSG)

    async def put_file(self, handle: SessionHandle, path: str, content: bytes) -> None:
        raise NotImplementedError(_MSG)

    async def get_file(self, handle: SessionHandle, path: str) -> bytes:
        raise NotImplementedError(_MSG)

    async def capabilities(self) -> ProviderCapabilities:
        raise NotImplementedError(_MSG)

    async def capacity(self) -> CapacityReport:
        raise NotImplementedError(_MSG)

    async def reconcile(self) -> list[SessionHandle]:
        raise NotImplementedError(_MSG)
