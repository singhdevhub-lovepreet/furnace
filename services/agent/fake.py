from __future__ import annotations

import asyncio
from dataclasses import dataclass

from services.agent.base import AgentContext, AgentResult, AgentRunner, EmitFn


@dataclass(slots=True)
class FakeAgentRunner(AgentRunner):
    step_delay_seconds: float = 0.0

    async def run(
        self,
        ctx: AgentContext,
        *,
        emit: EmitFn,
        cancel_event: asyncio.Event,
    ) -> AgentResult:
        prompt_excerpt = ctx.prompt.strip()[:48]
        if len(ctx.prompt.strip()) > 48:
            prompt_excerpt = f"{prompt_excerpt}…"
        plan_steps: list[str] = [
            f"inspect repository {ctx.repo_full_name}",
            f"apply change for prompt: {prompt_excerpt}",
            "build for simulator",
            "run UI test",
        ]
        await emit("agent_plan", {"plan": plan_steps})
        emitted_steps = 1
        if cancel_event.is_set():
            return AgentResult(
                success=False, summary="cancelled", steps=emitted_steps, changed_files=[]
            )

        scripted_steps: list[tuple[str, dict[str, object], dict[str, object]]] = [
            (
                "agent_action",
                {"tool": "read_file", "args": {"path": "Sources/ContentView.swift"}},
                {"ok": True, "detail": "read current view"},
            ),
            (
                "agent_action",
                {"tool": "apply_patch", "args": {"path": "Sources/ContentView.swift"}},
                {"ok": True, "detail": f"applied prompt-driven change for {prompt_excerpt}"},
            ),
            (
                "agent_action",
                {"tool": "xcodebuild", "args": {"scheme": "App", "destination": "simulator"}},
                {"ok": True, "detail": "simulator build passed"},
            ),
            (
                "agent_action",
                {"tool": "run_ui_test", "args": {"target": "SessionDetail"}},
                {"ok": True, "detail": "ui test passed"},
            ),
        ]

        for action_type, action_payload, observation_payload in scripted_steps:
            if cancel_event.is_set():
                return AgentResult(
                    success=False,
                    summary="cancelled",
                    steps=emitted_steps,
                    changed_files=[],
                )
            if self.step_delay_seconds > 0:
                await asyncio.sleep(self.step_delay_seconds)
            await emit(action_type, action_payload)
            emitted_steps += 1
            if cancel_event.is_set():
                return AgentResult(
                    success=False,
                    summary="cancelled",
                    steps=emitted_steps,
                    changed_files=[],
                )
            await emit("agent_observation", observation_payload)
            emitted_steps += 1

        if cancel_event.is_set():
            return AgentResult(
                success=False, summary="cancelled", steps=emitted_steps, changed_files=[]
            )
        await emit(
            "agent_message",
            {"text": f"Applied change: {prompt_excerpt}"},
        )
        emitted_steps += 1
        return AgentResult(
            success=True,
            summary="Applied change for prompt",
            steps=emitted_steps,
            changed_files=["Sources/ContentView.swift"],
        )
