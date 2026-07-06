from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Literal

from services.agent.base import AgentContext, AgentResult, AgentRunner, EmitFn
from services.llm.router import ModelRouter
from services.scheduler.provisioner.base import MacProvisioner


@dataclass(slots=True)
class LlmAgentRunner(AgentRunner):
    router: ModelRouter
    provisioner: MacProvisioner
    max_steps: int = 12
    command_timeout_seconds: int = 60

    async def run(
        self,
        ctx: AgentContext,
        *,
        emit: EmitFn,
        cancel_event: asyncio.Event,
    ) -> AgentResult:
        if cancel_event.is_set():
            return AgentResult(success=False, summary="cancelled", steps=0, changed_files=[])

        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": self._system_prompt(ctx.repo_full_name),
            },
            {"role": "user", "content": ctx.prompt},
        ]
        changed_files: set[str] = set()
        emitted_steps = 0

        if cancel_event.is_set():
            return AgentResult(success=False, summary="cancelled", steps=0, changed_files=[])

        planner_text = await self.router.complete(ctx.session_id, "planner", messages)
        if cancel_event.is_set():
            return AgentResult(
                success=False,
                summary="cancelled",
                steps=emitted_steps,
                changed_files=sorted(changed_files),
            )
        messages.append({"role": "assistant", "content": planner_text})
        plan = self._parse_plan(planner_text, ctx.prompt, ctx.repo_full_name)
        await self._emit(emit, cancel_event, "agent_plan", {"plan": plan})
        emitted_steps += 1
        if cancel_event.is_set():
            return AgentResult(
                success=False,
                summary="cancelled",
                steps=emitted_steps,
                changed_files=sorted(changed_files),
            )

        parse_failures = 0
        for _ in range(self.max_steps):
            if cancel_event.is_set():
                return AgentResult(
                    success=False,
                    summary="cancelled",
                    steps=emitted_steps,
                    changed_files=sorted(changed_files),
                )
            coder_text = await self.router.complete(ctx.session_id, "coder", messages)
            if cancel_event.is_set():
                return AgentResult(
                    success=False,
                    summary="cancelled",
                    steps=emitted_steps,
                    changed_files=sorted(changed_files),
                )

            tool_call = self._parse_json_object(coder_text)
            if tool_call is None:
                parse_failures += 1
                messages.append({"role": "assistant", "content": coder_text})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your last message was not valid JSON matching a tool schema. "
                            "Reply with exactly one JSON tool object."
                        ),
                    }
                )
                if parse_failures >= 3:
                    await self._emit(
                        emit,
                        cancel_event,
                        "agent_message",
                        {"text": "model did not produce valid tool calls"},
                    )
                    emitted_steps += 1
                    return AgentResult(
                        success=False,
                        summary="model did not produce valid tool calls",
                        steps=emitted_steps,
                        changed_files=sorted(changed_files),
                    )
                continue

            parse_failures = 0
            messages.append({"role": "assistant", "content": coder_text})
            tool_name = self._string_value(tool_call.get("tool"))
            args = self._dict_value(tool_call.get("args"))
            if tool_name == "finish":
                summary = self._string_value(tool_call.get("summary"), default="finished")
                reported_changed = self._string_list(tool_call.get("changed_files"))
                changed_files.update(reported_changed)
                await self._emit(emit, cancel_event, "agent_message", {"text": summary})
                emitted_steps += 1
                return AgentResult(
                    success=True,
                    summary=summary,
                    steps=emitted_steps,
                    changed_files=sorted(changed_files),
                )

            await self._emit(emit, cancel_event, "agent_action", {"tool": tool_name, "args": args})
            emitted_steps += 1
            observation = await self._dispatch_tool(ctx, tool_name, args, changed_files)
            if cancel_event.is_set():
                return AgentResult(
                    success=False,
                    summary="cancelled",
                    steps=emitted_steps,
                    changed_files=sorted(changed_files),
                )
            await self._emit(emit, cancel_event, "agent_observation", observation)
            emitted_steps += 1
            messages.append(
                {"role": "user", "content": json.dumps(observation, ensure_ascii=False)}
            )

        await self._emit(
            emit,
            cancel_event,
            "agent_message",
            {"text": "reached step limit without finishing"},
        )
        emitted_steps += 1
        return AgentResult(
            success=False,
            summary="reached step limit without finishing",
            steps=emitted_steps,
            changed_files=sorted(changed_files),
        )

    async def _dispatch_tool(
        self,
        ctx: AgentContext,
        tool_name: str,
        args: dict[str, object],
        changed_files: set[str],
    ) -> dict[str, object]:
        try:
            if tool_name == "list_files":
                path = self._string_value(args.get("path"), default=".")
                if not path:
                    return {"ok": False, "error": "path must not be empty"}
                rc, stdout, stderr = await self.provisioner.exec(
                    ctx.handle,
                    "ls",
                    ["-a", path],
                    {},
                    self.command_timeout_seconds,
                )
                return {"ok": rc == 0, "stdout": stdout, "stderr": stderr, "rc": rc}
            if tool_name == "read_file":
                path = self._string_value(args.get("path"), default="")
                if not path:
                    return {"ok": False, "error": "path must not be empty"}
                content_bytes = await self.provisioner.get_file(ctx.handle, path)
                content = content_bytes.decode("utf-8", errors="replace")
                truncated = False
                if len(content) > 8000:
                    content = content[:8000]
                    truncated = True
                return {"ok": True, "content": content, "truncated": truncated}
            if tool_name == "write_file":
                path = self._string_value(args.get("path"), default="")
                if not path:
                    return {"ok": False, "error": "path must not be empty"}
                content = self._string_value(args.get("content"), default="")
                await self.provisioner.put_file(ctx.handle, path, content.encode("utf-8"))
                changed_files.add(path)
                return {"ok": True, "detail": f"wrote {path}"}
            if tool_name == "run":
                argv = self._string_list(args.get("argv"))
                if not argv:
                    return {"ok": False, "error": "argv must not be empty"}
                rc, stdout, stderr = await self.provisioner.exec(
                    ctx.handle,
                    argv[0],
                    argv[1:],
                    {},
                    self.command_timeout_seconds,
                )
                return {"ok": rc == 0, "rc": rc, "stdout": stdout, "stderr": stderr}
            return {"ok": False, "error": "unknown tool"}
        except Exception as exc:  # pragma: no cover - exercised by tests via fake provisioner
            return {"ok": False, "error": str(exc)}

    async def _emit(
        self,
        emit: EmitFn,
        cancel_event: asyncio.Event,
        event_type: Literal["agent_plan", "agent_action", "agent_observation", "agent_message"],
        payload: dict[str, object],
    ) -> None:
        if cancel_event.is_set():
            return
        await emit(event_type, payload)
        if cancel_event.is_set():
            return

    def _system_prompt(self, repo_full_name: str) -> str:
        return (
            "You are an autonomous iOS/repo coding agent working on repository "
            f"{repo_full_name}. Reply with exactly one JSON object and nothing else. "
            'During planning, the JSON object must be {"plan": ["..."]}. During '
            "tool use, the JSON object must have a tool field and only one tool schema:\n"
            '- list_files: {"tool":"list_files","args":{"path":"..."}}\n'
            '- read_file: {"tool":"read_file","args":{"path":"..."}}\n'
            "- write_file: "
            '{"tool":"write_file","args":{"path":"...","content":"..."}}\n'
            '- run: {"tool":"run","args":{"argv":["cmd","arg1"]}}\n'
            '- finish: {"tool":"finish","summary":"...","changed_files":["..."]}\n'
            "Do not wrap JSON in prose. Do not emit markdown."
        )

    def _parse_json_object(self, text: str) -> dict[str, object] | None:
        payload = self._strip_code_fence(text)
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
        return None

    def _parse_plan(self, text: str, prompt: str, repo_full_name: str) -> list[str]:
        parsed = self._parse_json_object(text)
        if parsed is None:
            return [self._fallback_plan_item(prompt, repo_full_name)]
        plan_value = parsed.get("plan")
        if not isinstance(plan_value, list):
            return [self._fallback_plan_item(prompt, repo_full_name)]
        plan: list[str] = []
        for item in plan_value:
            plan.append(str(item))
        if not plan:
            return [self._fallback_plan_item(prompt, repo_full_name)]
        return plan

    def _strip_code_fence(self, text: str) -> str:
        stripped = text.strip()
        if not stripped.startswith("```"):
            return stripped
        lines = stripped.splitlines()
        if len(lines) < 3:
            return stripped
        if not lines[0].startswith("```"):
            return stripped
        if not lines[-1].strip().startswith("```"):
            return stripped
        return "\n".join(lines[1:-1]).strip()

    def _fallback_plan_item(self, prompt: str, repo_full_name: str) -> str:
        cleaned = prompt.strip()
        if cleaned:
            return cleaned
        return f"work on {repo_full_name}"

    def _string_value(self, value: object, default: str = "") -> str:
        if isinstance(value, str):
            return value
        if value is None:
            return default
        return str(value)

    def _string_list(self, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value]

    def _dict_value(self, value: object) -> dict[str, object]:
        if isinstance(value, dict):
            return {str(key): item for key, item in value.items()}
        return {}
