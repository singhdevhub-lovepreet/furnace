from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

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

_FIXTURES_DIR = Path(__file__).with_name("fixtures")
_CANNED_SCREENSHOT_PATH = _FIXTURES_DIR / "canned_screenshot.png"
_CANNED_VIDEO_PATH = _FIXTURES_DIR / "canned_video.mp4"


@dataclass(slots=True)
class FakeChannel:
    kind: ChannelKind
    payload: bytes
    closed: bool = False

    def read(self) -> bytes:
        return self.payload

    def close(self) -> None:
        self.closed = True


@dataclass(slots=True)
class FakeProvisioner(MacProvisioner):
    queue_acquire: bool = False
    queued_eta_seconds: int = 120
    provider_name: str = "fake"
    release_calls: list[SessionHandle] = field(default_factory=list)
    exec_calls: list[tuple[SessionHandle, str, list[str], dict[str, str], int]] = field(
        default_factory=list
    )
    put_files: list[tuple[SessionHandle, str, bytes]] = field(default_factory=list)
    _handles: dict[UUID, SessionHandle] = field(default_factory=dict)
    _states: dict[UUID, ProvisionerSessionStatus] = field(default_factory=dict)

    async def acquire(self, spec: SessionSpec) -> AcquireOutcome:
        _ = spec
        handle = SessionHandle(
            id=uuid4(),
            provider=self.provider_name,
            created_at=datetime.now(UTC),
            tool_endpoint="in-memory://fake",
        )
        self._handles[handle.id] = handle
        if self.queue_acquire:
            self._states[handle.id] = ProvisionerSessionStatus.QUEUED
            return AcquireOutcome(AcquisitionState.QUEUED, handle, self.queued_eta_seconds)
        self._states[handle.id] = ProvisionerSessionStatus.READY
        return AcquireOutcome(AcquisitionState.READY, handle, None)

    async def release(self, handle: SessionHandle) -> None:
        self.release_calls.append(handle)
        self._handles.pop(handle.id, None)
        self._states.pop(handle.id, None)

    async def status(self, handle: SessionHandle) -> ProvisionerSessionStatus:
        state = self._states.get(handle.id)
        if state is not None:
            return state
        return ProvisionerSessionStatus.GONE

    async def exec(
        self,
        handle: SessionHandle,
        command: str,
        argv: list[str],
        env: dict[str, str],
        timeout_seconds: int,
    ) -> tuple[int, str, str]:
        self.exec_calls.append((handle, command, argv, env, timeout_seconds))
        return 0, "ok", ""

    async def open_channel(self, handle: SessionHandle, kind: ChannelKind) -> Channel:
        _ = handle
        payload = b""
        if kind == ChannelKind.ARTIFACTS:
            payload = _CANNED_SCREENSHOT_PATH.read_bytes() + b"\n" + _CANNED_VIDEO_PATH.read_bytes()
        return FakeChannel(kind=kind, payload=payload)

    async def put_file(self, handle: SessionHandle, path: str, content: bytes) -> None:
        self.put_files.append((handle, path, content))

    async def get_file(self, handle: SessionHandle, path: str) -> bytes:
        _ = handle
        if path.endswith(".png"):
            return _CANNED_SCREENSHOT_PATH.read_bytes()
        if path.endswith(".mp4"):
            return _CANNED_VIDEO_PATH.read_bytes()
        return b""

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            max_sessions_per_host=1,
            nested_vm_cap=0,
            silicon_gen="fake",
            supports_vnc=False,
            min_alloc_seconds=0,
        )

    async def capacity(self) -> CapacityReport:
        active = len(self._handles)
        return CapacityReport(
            free_slots=max(0, 1 - active),
            total_slots=1,
            warm_hosts=1,
            provisioning_hosts=0,
            est_new_slot_seconds=0,
        )

    async def reconcile(self) -> list[SessionHandle]:
        return list(self._handles.values())

    def canned_artifact_paths(self, artifacts_dir: Path) -> tuple[Path, Path]:
        screenshot = artifacts_dir / "screenshot.png"
        video = artifacts_dir / "demo.mp4"
        screenshot.write_bytes(_CANNED_SCREENSHOT_PATH.read_bytes())
        video.write_bytes(_CANNED_VIDEO_PATH.read_bytes())
        return screenshot, video
