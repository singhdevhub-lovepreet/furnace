"""Control-plane client for the worker daemon's tool RPC.

`Ec2TartProvisioner` (M3) embeds this client to implement `MacProvisioner.exec`
/ `put_file` / `get_file` against a real session VM, and the agent's iOS tool
calls dispatch through it.
"""

from __future__ import annotations

import httpx
from pydantic import JsonValue

from apps.worker.protocol import HealthReport, ToolName, ToolRequest, ToolResult


class WorkerToolClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 660.0,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        self._client = http_client or httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=timeout_seconds
        )
        if http_client is not None and token is not None:
            self._client.headers["Authorization"] = f"Bearer {token}"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def healthz(self) -> HealthReport:
        response = await self._client.get("/healthz")
        response.raise_for_status()
        return HealthReport.model_validate(response.json())

    async def call(self, tool: ToolName, args: dict[str, JsonValue] | None = None) -> ToolResult:
        request = ToolRequest(tool=tool, args=args or {})
        response = await self._client.post("/rpc/tool", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return ToolResult.model_validate(response.json())

    async def exec(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 600,
    ) -> ToolResult:
        return await self.call(
            ToolName.EXEC,
            {
                "argv": list(argv),
                "env": dict(env or {}),
                "timeout_seconds": timeout_seconds,
            },
        )

    async def list_files(self, path: str = ".") -> ToolResult:
        return await self.call(ToolName.LIST_FILES, {"path": path})

    async def read_file(self, path: str) -> ToolResult:
        return await self.call(ToolName.READ_FILE, {"path": path})

    async def write_file(self, path: str, content: str) -> ToolResult:
        return await self.call(ToolName.WRITE_FILE, {"path": path, "content": content})

    async def build_app(
        self, project_path: str, scheme: str, configuration: str = "Debug"
    ) -> ToolResult:
        return await self.call(
            ToolName.BUILD_APP,
            {"project_path": project_path, "scheme": scheme, "configuration": configuration},
        )

    async def boot_simulator(self, device: str) -> ToolResult:
        return await self.call(ToolName.BOOT_SIMULATOR, {"device": device})

    async def install_app(self, device: str, app_path: str) -> ToolResult:
        return await self.call(ToolName.INSTALL_APP, {"device": device, "app_path": app_path})

    async def launch_app(self, device: str, bundle_id: str) -> ToolResult:
        return await self.call(ToolName.LAUNCH_APP, {"device": device, "bundle_id": bundle_id})

    async def screenshot(self, device: str, dest_path: str) -> ToolResult:
        return await self.call(ToolName.SCREENSHOT, {"device": device, "dest_path": dest_path})

    async def record_video(
        self, device: str, dest_path: str, duration_seconds: int = 10
    ) -> ToolResult:
        return await self.call(
            ToolName.RECORD_VIDEO,
            {"device": device, "dest_path": dest_path, "duration_seconds": duration_seconds},
        )

    async def run_ui_test(self, project_path: str, scheme: str, device: str) -> ToolResult:
        return await self.call(
            ToolName.RUN_UI_TEST,
            {"project_path": project_path, "scheme": scheme, "device": device},
        )
