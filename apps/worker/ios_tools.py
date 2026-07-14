"""iOS tool backends: thin adapters over xcodebuild / simctl (xcodebuildmcp seam).

`RealIosToolBackend` shells out to the Xcode toolchain and only works inside the
macOS VM (golden image). `FakeIosToolBackend` returns deterministic canned
results so the daemon, its contract, and its clients are fully exercisable on
Linux and in CI.
"""

from __future__ import annotations

import asyncio
import base64
import signal
from abc import ABC, abstractmethod
from dataclasses import dataclass

from apps.worker.protocol import ToolResult
from apps.worker.workspace import WorkspaceOps

# 1x1 transparent PNG.
FAKE_SCREENSHOT_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
# Minimal MP4 'ftyp' box — deterministic stand-in for a recording.
FAKE_VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"


class IosToolBackend(ABC):
    @abstractmethod
    async def build_app(self, project_path: str, scheme: str, configuration: str) -> ToolResult:
        raise NotImplementedError

    @abstractmethod
    async def boot_simulator(self, device: str) -> ToolResult:
        raise NotImplementedError

    @abstractmethod
    async def install_app(self, device: str, app_path: str) -> ToolResult:
        raise NotImplementedError

    @abstractmethod
    async def launch_app(self, device: str, bundle_id: str) -> ToolResult:
        raise NotImplementedError

    @abstractmethod
    async def screenshot(self, device: str, dest_path: str) -> ToolResult:
        raise NotImplementedError

    @abstractmethod
    async def record_video(self, device: str, dest_path: str, duration_seconds: int) -> ToolResult:
        raise NotImplementedError

    @abstractmethod
    async def run_ui_test(self, project_path: str, scheme: str, device: str) -> ToolResult:
        raise NotImplementedError


@dataclass(slots=True)
class RealIosToolBackend(IosToolBackend):
    """Adapters over xcodebuild / xcrun simctl; requires a macOS host with Xcode."""

    workspace: WorkspaceOps
    command_timeout_seconds: int = 1800

    async def build_app(self, project_path: str, scheme: str, configuration: str) -> ToolResult:
        container_flag = "-workspace" if project_path.endswith(".xcworkspace") else "-project"
        return await self.workspace.exec(
            [
                "xcodebuild",
                container_flag,
                project_path,
                "-scheme",
                scheme,
                "-configuration",
                configuration,
                "-destination",
                "generic/platform=iOS Simulator",
                "build",
            ],
            {},
            self.command_timeout_seconds,
        )

    async def boot_simulator(self, device: str) -> ToolResult:
        result = await self.workspace.exec(
            ["xcrun", "simctl", "boot", device], {}, self.command_timeout_seconds
        )
        if not result.ok and "current state: Booted" in result.stderr:
            return ToolResult(ok=True, rc=result.rc, stdout=result.stdout, stderr=result.stderr)
        return result

    async def install_app(self, device: str, app_path: str) -> ToolResult:
        return await self.workspace.exec(
            ["xcrun", "simctl", "install", device, app_path], {}, self.command_timeout_seconds
        )

    async def launch_app(self, device: str, bundle_id: str) -> ToolResult:
        return await self.workspace.exec(
            ["xcrun", "simctl", "launch", device, bundle_id], {}, self.command_timeout_seconds
        )

    async def screenshot(self, device: str, dest_path: str) -> ToolResult:
        target = self.workspace.resolve(dest_path)
        if target is None:
            return ToolResult(ok=False, error="path escapes workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        result = await self.workspace.exec(
            ["xcrun", "simctl", "io", device, "screenshot", str(target)],
            {},
            self.command_timeout_seconds,
        )
        if result.ok:
            result.data = {"path": dest_path}
        return result

    async def record_video(self, device: str, dest_path: str, duration_seconds: int) -> ToolResult:
        target = self.workspace.resolve(dest_path)
        if target is None:
            return ToolResult(ok=False, error="path escapes workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            process = await asyncio.create_subprocess_exec(
                "xcrun",
                "simctl",
                "io",
                device,
                "recordVideo",
                "--force",
                str(target),
                cwd=self.workspace.root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return ToolResult(ok=False, error=str(exc))
        await asyncio.sleep(duration_seconds)
        # simctl finalizes the file on SIGINT.
        process.send_signal(signal.SIGINT)
        stdout_bytes, stderr_bytes = await process.communicate()
        if not target.is_file():
            return ToolResult(
                ok=False,
                error="recording produced no file",
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
            )
        return ToolResult(ok=True, data={"path": dest_path, "duration_seconds": duration_seconds})

    async def run_ui_test(self, project_path: str, scheme: str, device: str) -> ToolResult:
        container_flag = "-workspace" if project_path.endswith(".xcworkspace") else "-project"
        return await self.workspace.exec(
            [
                "xcodebuild",
                container_flag,
                project_path,
                "-scheme",
                scheme,
                "-destination",
                f"platform=iOS Simulator,name={device}",
                "test",
            ],
            {},
            self.command_timeout_seconds,
        )


@dataclass(slots=True)
class FakeIosToolBackend(IosToolBackend):
    """Deterministic canned results for Linux/CI; writes fixture artifact bytes."""

    workspace: WorkspaceOps

    async def build_app(self, project_path: str, scheme: str, configuration: str) -> ToolResult:
        app_path = f"build/{configuration}-iphonesimulator/{scheme}.app"
        return ToolResult(
            ok=True,
            rc=0,
            stdout=f"BUILD SUCCEEDED (fake) {project_path} scheme={scheme}",
            data={"app_path": app_path},
        )

    async def boot_simulator(self, device: str) -> ToolResult:
        return ToolResult(ok=True, rc=0, data={"device": device, "state": "Booted"})

    async def install_app(self, device: str, app_path: str) -> ToolResult:
        return ToolResult(ok=True, rc=0, data={"device": device, "app_path": app_path})

    async def launch_app(self, device: str, bundle_id: str) -> ToolResult:
        return ToolResult(
            ok=True, rc=0, data={"device": device, "bundle_id": bundle_id, "pid": 4242}
        )

    async def screenshot(self, device: str, dest_path: str) -> ToolResult:
        _ = device
        target = self.workspace.resolve(dest_path)
        if target is None:
            return ToolResult(ok=False, error="path escapes workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(FAKE_SCREENSHOT_BYTES)
        return ToolResult(ok=True, data={"path": dest_path})

    async def record_video(self, device: str, dest_path: str, duration_seconds: int) -> ToolResult:
        _ = device
        target = self.workspace.resolve(dest_path)
        if target is None:
            return ToolResult(ok=False, error="path escapes workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(FAKE_VIDEO_BYTES)
        return ToolResult(ok=True, data={"path": dest_path, "duration_seconds": duration_seconds})

    async def run_ui_test(self, project_path: str, scheme: str, device: str) -> ToolResult:
        return ToolResult(
            ok=True,
            rc=0,
            stdout=f"TEST SUCCEEDED (fake) {project_path} scheme={scheme} device={device}",
            data={"tests_run": 1, "failures": 0},
        )
