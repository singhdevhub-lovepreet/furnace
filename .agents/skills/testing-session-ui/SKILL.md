---
name: testing-session-ui
description: End-to-end test the Furnace/Raven Next.js session UI against the FastAPI control plane using the FakeProvisioner. Use when verifying the web UI (session create, live WS event stream, artifacts, cancel) or BYOK keys UI/API changes.
---

# Testing the Furnace session UI end-to-end

The control plane runs fully on Linux with an in-memory `FakeProvisioner` (canned screenshot + video), so the whole UI can be exercised without a Mac. No real LLM, GitHub, or macOS execution is involved.

## Setup

1. Backend (repo root):
   - `.env` for tests: `FURNACE_DATABASE_URL=sqlite+aiosqlite:///./furnace_test.db`, `FURNACE_ARTIFACTS_DIR=./artifacts_test`, `FURNACE_PROVISIONER=fake`, `FURNACE_WEB_ORIGIN=http://localhost:3000`, `FURNACE_MASTER_ENCRYPTION_KEY=<base64 32 bytes>` (enables BYOK), `FURNACE_FAKE_MAX_SLOTS=4`.
   - Start: `set -a && . ./.env && set +a && python3 -m uvicorn services.app:create_app --factory --host 0.0.0.0 --port 8000`
   - Env vars override `.env`; e.g. `FURNACE_SESSION_STEP_DELAY_SECONDS=10` widens the per-step window (see cancel note).
2. Seed a repo so the form's Repo dropdown is populated: run `seed_test.py` (creates a user + installation + repo `raven-demo/SampleApp`). Without a seeded repo the dropdown is empty.
3. Frontend: `cd apps/web && NEXT_PUBLIC_API_BASE=http://localhost:8000 npm run dev` (serves on :3000).
4. Verify: `curl localhost:8000/healthz` and `curl localhost:8000/v1/repos` both return 200.

## What to verify (golden path)

- Session create from `/` form → navigates to `/sessions/<uuid>` (proves `/v1/repos` + CORS + create wiring).
- `/sessions/[id]`: "Stream: connected", ordered events QUEUED→PROVISIONING→CLONING_REPO→RUNNING→RECORDING→RUNNING→OPENING_PR→SUCCEEDED, badge ends SUCCEEDED, PR 1.
- Artifacts render; content endpoint `GET /v1/sessions/{id}/artifacts/{artifact_id}/content` returns `image/png` and `video/mp4`, and 404 for a bogus artifact id. The screenshot fixture is a tiny valid PNG; the video is a 15-byte stub (won't play — only correct serving matters).
- `/` list shows newest session first.
- `/keys`: create/list/delete a BYOK key; the raw secret must never appear in the row, page HTML, or `/v1/keys` (returns metadata only).

## Agent runner seam (PR #7 and later)

During the RUNNING phase the orchestrator runs an `AgentRunner` (`services/agent/`); the default `FakeAgentRunner` emits a deterministic, prompt-driven script that flows into the same live event stream. To verify:
- The event stream MUST include, in order between RUNNING and RECORDING: `agent_plan` → (`agent_action` + `agent_observation`) ×4 (tools `read_file`, `apply_patch`, `xcodebuild`, `run_ui_test`) → `agent_message` → `agent_result`.
- Prompt-echo is the strongest signal: `agent_plan` contains `"apply change for prompt: <prompt>"` and `agent_message` contains `"Applied change: <prompt truncated to 48 chars + …>"`. A broken/unwired seam shows ONLY `session_started`/`status_changed` with no `agent_*` events — that's the fail state.
- `agent_result` payload: `success:true, steps:10, changed_files:["Sources/ContentView.swift"]`.
- The event panel (`apps/web/app/sessions/[id]/page.tsx`) renders every event type generically (type + JSON payload), so no frontend change is needed for new event types — read the stripped DOM to assert payloads precisely.
- Persistence check: `session_id` is stored WITHOUT dashes in SQLite, so `select ... where session_id='<uuid-with-dashes>'` returns nothing. Query by the dash-less id, or just `group by type` over all events. Expect 19 rows for one run (agent_plan 1, agent_action 4, agent_observation 4, agent_message 1, agent_result 1, session_started 1, status_changed 7).
- Tune `FURNACE_SESSION_STEP_DELAY_SECONDS` (the fake agent reuses it as its inter-step delay): ~1.2s gives a readable stream over ~15s.

Note: the sidekick may delete untracked helper files (e.g. `seed_test.py`, `test-report.md`) when cleaning the worktree — keep a copy or be ready to recreate them.

## Cancel (important timing note)

Cancel is gated to **QUEUED / PROVISIONING / RUNNING** in both the UI (`apps/web/lib/api.ts: canCancel`) and backend (`services/api/routes.py: cancel_session`). CLONING_REPO / RECORDING / OPENING_PR are **not** cancellable, so the Cancel button is disabled during those states and a click does nothing. Cancelling from a terminal state returns `409 session is not cancellable`.

To reliably demonstrate a successful cancel with the slow browser-tool latency, raise `FURNACE_SESSION_STEP_DELAY_SECONDS` (e.g. 10) and click Cancel during the first PROVISIONING window right after creation. A successful cancel sets the badge to CANCELLED, emits a `status_changed`→CANCELLED event, sets Ended, and disables the button.

## Gotchas

- Restarting uvicorn: it drains background session tasks on SIGTERM ("Waiting for background tasks to complete"), holding port 8000. Use `pkill -9 -f "uvicorn services.app"` and confirm the port is free before relaunching, or the new process fails to bind.
- The detail page polls `GET /v1/sessions/{id}` every ~5s, so the badge can briefly lag the WS event stream (the stream is the source of truth for ordering).

## Devin Secrets Needed

None. All external dependencies (LLM, GitHub, macOS) are faked/mocked; `FURNACE_MASTER_ENCRYPTION_KEY` is generated locally for the test run.
