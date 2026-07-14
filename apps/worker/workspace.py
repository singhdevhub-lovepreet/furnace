"""Workspace-rooted file and process operations for the worker daemon."""

from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass
from pathlib import Path

from apps.worker.protocol import ToolResult

_MAX_OUTPUT_CHARS = 16000
_MAX_READ_CHARS = 64000


@dataclass(slots=True)
class WorkspaceOps:
    root: Path

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, path: str) -> Path | None:
        candidate = (self.root / path).resolve()
        if candidate == self.root or candidate.is_relative_to(self.root):
            return candidate
        return None

    async def exec(
        self,
        argv: list[str],
        env: dict[str, str],
        timeout_seconds: int,
    ) -> ToolResult:
        if not argv:
            return ToolResult(ok=False, error="argv must not be empty")
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.root,
                env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin", **env},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return ToolResult(ok=False, error=str(exc))
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return ToolResult(ok=False, error=f"timed out after {timeout_seconds}s")
        rc = process.returncode if process.returncode is not None else -1
        return ToolResult(
            ok=rc == 0,
            rc=rc,
            stdout=stdout_bytes.decode("utf-8", errors="replace")[:_MAX_OUTPUT_CHARS],
            stderr=stderr_bytes.decode("utf-8", errors="replace")[:_MAX_OUTPUT_CHARS],
        )

    def list_files(self, path: str) -> ToolResult:
        target = self.resolve(path)
        if target is None:
            return ToolResult(ok=False, error="path escapes workspace")
        if not target.is_dir():
            return ToolResult(ok=False, error=f"not a directory: {path}")
        entries = sorted(entry.name + ("/" if entry.is_dir() else "") for entry in target.iterdir())
        return ToolResult(ok=True, data={"entries": list(entries)})

    def read_file(self, path: str, *, encoding: str = "utf-8") -> ToolResult:
        target = self.resolve(path)
        if target is None:
            return ToolResult(ok=False, error="path escapes workspace")
        if not target.is_file():
            return ToolResult(ok=False, error=f"not a file: {path}")
        raw = target.read_bytes()
        if encoding == "base64":
            return ToolResult(
                ok=True,
                data={"content_b64": base64.b64encode(raw).decode("ascii")},
            )
        content = raw.decode("utf-8", errors="replace")
        truncated = len(content) > _MAX_READ_CHARS
        return ToolResult(
            ok=True,
            data={"content": content[:_MAX_READ_CHARS], "truncated": truncated},
        )

    def write_file(self, path: str, content: str, *, encoding: str = "utf-8") -> ToolResult:
        target = self.resolve(path)
        if target is None:
            return ToolResult(ok=False, error="path escapes workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        if encoding == "base64":
            try:
                raw = base64.b64decode(content, validate=True)
            except binascii.Error:
                return ToolResult(ok=False, error="content is not valid base64")
            target.write_bytes(raw)
        else:
            target.write_text(content, encoding="utf-8")
        return ToolResult(ok=True, data={"path": path})
