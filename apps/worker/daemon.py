"""Worker daemon: HTTP tool-RPC server that runs inside the macOS session VM."""

from __future__ import annotations

import hmac
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import JsonValue

from apps.worker.ios_tools import FakeIosToolBackend, IosToolBackend, RealIosToolBackend
from apps.worker.protocol import HealthReport, ToolName, ToolRequest, ToolResult
from apps.worker.workspace import WorkspaceOps

_DEFAULT_EXEC_TIMEOUT_SECONDS = 600
_MAX_EXEC_TIMEOUT_SECONDS = 3600
_MAX_RECORD_SECONDS = 600


def _string_arg(args: dict[str, JsonValue], name: str, default: str | None = None) -> str | None:
    value = args.get(name, default)
    if isinstance(value, str) and value:
        return value
    return default if isinstance(default, str) else None


def _int_arg(args: dict[str, JsonValue], name: str, default: int) -> int:
    value = args.get(name)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default


def _string_list_arg(args: dict[str, JsonValue], name: str) -> list[str]:
    value = args.get(name)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _env_arg(args: dict[str, JsonValue], name: str) -> dict[str, str]:
    value = args.get(name)
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if isinstance(item, str)}


def _missing(name: str) -> ToolResult:
    return ToolResult(ok=False, error=f"missing required arg: {name}")


class ToolDispatcher:
    def __init__(self, workspace: WorkspaceOps, ios: IosToolBackend) -> None:
        self.workspace = workspace
        self.ios = ios

    async def dispatch(self, request: ToolRequest) -> ToolResult:
        args = request.args
        if request.tool is ToolName.EXEC:
            argv = _string_list_arg(args, "argv")
            if not argv:
                return _missing("argv")
            timeout = min(
                _int_arg(args, "timeout_seconds", _DEFAULT_EXEC_TIMEOUT_SECONDS),
                _MAX_EXEC_TIMEOUT_SECONDS,
            )
            return await self.workspace.exec(argv, _env_arg(args, "env"), timeout)
        if request.tool is ToolName.LIST_FILES:
            path = _string_arg(args, "path", ".")
            return self.workspace.list_files(path if path is not None else ".")
        if request.tool is ToolName.READ_FILE:
            path = _string_arg(args, "path")
            if path is None:
                return _missing("path")
            return self.workspace.read_file(path)
        if request.tool is ToolName.WRITE_FILE:
            path = _string_arg(args, "path")
            if path is None:
                return _missing("path")
            content = args.get("content")
            if not isinstance(content, str):
                return _missing("content")
            return self.workspace.write_file(path, content)
        if request.tool is ToolName.BUILD_APP:
            project_path = _string_arg(args, "project_path")
            scheme = _string_arg(args, "scheme")
            if project_path is None:
                return _missing("project_path")
            if scheme is None:
                return _missing("scheme")
            configuration = _string_arg(args, "configuration", "Debug")
            return await self.ios.build_app(
                project_path, scheme, configuration if configuration is not None else "Debug"
            )
        if request.tool is ToolName.BOOT_SIMULATOR:
            device = _string_arg(args, "device")
            if device is None:
                return _missing("device")
            return await self.ios.boot_simulator(device)
        if request.tool is ToolName.INSTALL_APP:
            device = _string_arg(args, "device")
            app_path = _string_arg(args, "app_path")
            if device is None:
                return _missing("device")
            if app_path is None:
                return _missing("app_path")
            return await self.ios.install_app(device, app_path)
        if request.tool is ToolName.LAUNCH_APP:
            device = _string_arg(args, "device")
            bundle_id = _string_arg(args, "bundle_id")
            if device is None:
                return _missing("device")
            if bundle_id is None:
                return _missing("bundle_id")
            return await self.ios.launch_app(device, bundle_id)
        if request.tool is ToolName.SCREENSHOT:
            device = _string_arg(args, "device")
            dest_path = _string_arg(args, "dest_path")
            if device is None:
                return _missing("device")
            if dest_path is None:
                return _missing("dest_path")
            return await self.ios.screenshot(device, dest_path)
        if request.tool is ToolName.RECORD_VIDEO:
            device = _string_arg(args, "device")
            dest_path = _string_arg(args, "dest_path")
            if device is None:
                return _missing("device")
            if dest_path is None:
                return _missing("dest_path")
            duration = min(_int_arg(args, "duration_seconds", 10), _MAX_RECORD_SECONDS)
            return await self.ios.record_video(device, dest_path, max(1, duration))
        if request.tool is ToolName.RUN_UI_TEST:
            project_path = _string_arg(args, "project_path")
            scheme = _string_arg(args, "scheme")
            device = _string_arg(args, "device")
            if project_path is None:
                return _missing("project_path")
            if scheme is None:
                return _missing("scheme")
            if device is None:
                return _missing("device")
            return await self.ios.run_ui_test(project_path, scheme, device)
        return ToolResult(ok=False, error=f"unknown tool: {request.tool}")


def create_worker_app(
    *,
    mode: str = "fake",
    workspace_root: Path,
    auth_token: str | None = None,
) -> FastAPI:
    workspace = WorkspaceOps(root=workspace_root)
    ios: IosToolBackend
    if mode == "fake":
        ios = FakeIosToolBackend(workspace=workspace)
    elif mode == "real":
        ios = RealIosToolBackend(workspace=workspace)
    else:
        raise ValueError(f"unknown worker mode {mode!r}")
    dispatcher = ToolDispatcher(workspace, ios)

    app = FastAPI(title="furnace-worker", version="0.1.0")

    def require_auth(authorization: str | None = Header(default=None)) -> None:
        if auth_token is None:
            return
        expected = f"Bearer {auth_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="invalid worker token")

    @app.get("/healthz")
    def healthz() -> HealthReport:
        return HealthReport(status="ok", mode=mode)

    @app.post("/rpc/tool")
    async def rpc_tool(request: ToolRequest, _: None = Depends(require_auth)) -> ToolResult:
        return await dispatcher.dispatch(request)

    return app
