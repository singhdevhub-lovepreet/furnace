from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from apps.worker.client import WorkerToolClient
from apps.worker.daemon import create_worker_app
from apps.worker.ios_tools import FAKE_SCREENSHOT_BYTES, FAKE_VIDEO_BYTES
from apps.worker.protocol import ToolName

TOKEN = "worker-secret"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


@pytest.fixture
async def client(workspace: Path) -> AsyncIterator[WorkerToolClient]:
    app = create_worker_app(mode="fake", workspace_root=workspace, auth_token=TOKEN)
    transport = httpx.ASGITransport(app=app)
    http_client = httpx.AsyncClient(transport=transport, base_url="http://worker")
    tool_client = WorkerToolClient("http://worker", token=TOKEN, http_client=http_client)
    yield tool_client
    await tool_client.aclose()


async def test_healthz_reports_mode(client: WorkerToolClient) -> None:
    report = await client.healthz()
    assert report.status == "ok"
    assert report.mode == "fake"


async def test_rpc_requires_bearer_token(workspace: Path) -> None:
    app = create_worker_app(mode="fake", workspace_root=workspace, auth_token=TOKEN)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://worker") as raw:
        response = await raw.post("/rpc/tool", json={"tool": "list_files", "args": {"path": "."}})
        assert response.status_code == 401
        response = await raw.post(
            "/rpc/tool",
            json={"tool": "list_files", "args": {"path": "."}},
            headers={"Authorization": "Bearer wrong"},
        )
        assert response.status_code == 401


async def test_write_read_list_roundtrip(client: WorkerToolClient) -> None:
    write = await client.write_file("Sources/ContentView.swift", "struct ContentView {}")
    assert write.ok
    read = await client.read_file("Sources/ContentView.swift")
    assert read.ok
    assert read.data["content"] == "struct ContentView {}"
    assert read.data["truncated"] is False
    listing = await client.list_files(".")
    assert listing.ok
    assert listing.data["entries"] == ["Sources/"]


async def test_path_traversal_is_rejected(client: WorkerToolClient, workspace: Path) -> None:
    for path in ("../outside.txt", "/etc/passwd", "a/../../outside.txt"):
        result = await client.write_file(path, "nope")
        assert not result.ok
        assert result.error == "path escapes workspace"
    read = await client.read_file("../../etc/passwd")
    assert not read.ok
    assert not (workspace.parent / "outside.txt").exists()


async def test_exec_runs_in_workspace(client: WorkerToolClient) -> None:
    result = await client.exec(["pwd"])
    assert result.ok
    assert result.rc == 0
    assert result.stdout.strip().endswith("workspace")
    missing = await client.exec(["definitely-not-a-command-xyz"])
    assert not missing.ok
    empty = await client.call(ToolName.EXEC, {"argv": []})
    assert not empty.ok
    assert empty.error == "missing required arg: argv"


async def test_missing_required_args(client: WorkerToolClient) -> None:
    result = await client.call(ToolName.BUILD_APP, {"scheme": "SampleApp"})
    assert not result.ok
    assert result.error == "missing required arg: project_path"
    result = await client.call(ToolName.SCREENSHOT, {"device": "iPhone 15"})
    assert not result.ok
    assert result.error == "missing required arg: dest_path"


async def test_unknown_tool_is_rejected(client: WorkerToolClient, workspace: Path) -> None:
    app = create_worker_app(mode="fake", workspace_root=workspace, auth_token=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://worker") as raw:
        response = await raw.post("/rpc/tool", json={"tool": "rm_rf", "args": {}})
        assert response.status_code == 422


async def test_full_ios_tool_sequence(client: WorkerToolClient, workspace: Path) -> None:
    build = await client.build_app("SampleApp.xcodeproj", "SampleApp")
    assert build.ok
    assert "BUILD SUCCEEDED" in build.stdout
    app_path = build.data["app_path"]
    assert app_path == "build/Debug-iphonesimulator/SampleApp.app"

    boot = await client.boot_simulator("iPhone 15")
    assert boot.ok
    assert boot.data["state"] == "Booted"

    install = await client.install_app("iPhone 15", str(app_path))
    assert install.ok

    launch = await client.launch_app("iPhone 15", "com.raven.SampleApp")
    assert launch.ok
    assert launch.data["pid"] == 4242

    screenshot = await client.screenshot("iPhone 15", "artifacts/screenshot.png")
    assert screenshot.ok
    assert screenshot.data["path"] == "artifacts/screenshot.png"
    assert (workspace / "artifacts/screenshot.png").read_bytes() == FAKE_SCREENSHOT_BYTES

    video = await client.record_video("iPhone 15", "artifacts/demo.mp4", duration_seconds=3)
    assert video.ok
    assert video.data["duration_seconds"] == 3
    assert (workspace / "artifacts/demo.mp4").read_bytes() == FAKE_VIDEO_BYTES

    ui_test = await client.run_ui_test("SampleApp.xcodeproj", "SampleApp", "iPhone 15")
    assert ui_test.ok
    assert ui_test.data == {"tests_run": 1, "failures": 0}


async def test_artifact_paths_escape_rejected(client: WorkerToolClient) -> None:
    screenshot = await client.screenshot("iPhone 15", "../screenshot.png")
    assert not screenshot.ok
    video = await client.record_video("iPhone 15", "/tmp/demo.mp4")
    assert not video.ok
