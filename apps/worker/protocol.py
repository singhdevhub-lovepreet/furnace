"""Tool-RPC contract between the control plane and the macOS worker daemon.

The worker daemon ships inside the tart golden image (M2) and exposes this
contract over HTTP. `Ec2TartProvisioner` (M3) embeds `WorkerToolClient` to
satisfy the `MacProvisioner` exec/file operations and the iOS tool calls the
agent makes during a session.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, JsonValue


class ToolName(str, Enum):
    EXEC = "exec"
    LIST_FILES = "list_files"
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    BUILD_APP = "build_app"
    BOOT_SIMULATOR = "boot_simulator"
    INSTALL_APP = "install_app"
    LAUNCH_APP = "launch_app"
    SCREENSHOT = "screenshot"
    RECORD_VIDEO = "record_video"
    RUN_UI_TEST = "run_ui_test"


class ToolRequest(BaseModel):
    tool: ToolName
    args: dict[str, JsonValue] = Field(default_factory=dict)


class ToolResult(BaseModel):
    ok: bool
    rc: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    data: dict[str, JsonValue] = Field(default_factory=dict)


class HealthReport(BaseModel):
    status: str
    mode: str
