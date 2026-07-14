# macOS Worker Daemon + xcodebuildmcp Seam

Status: implemented (`apps/worker/`) — Linux-mockable; the real backend runs on macOS only.

## What this is

The worker daemon is the process that ships inside the tart golden image (M2) and runs in
every session VM. It exposes the **tool RPC** seam from the LLD (§1 "Worker Daemon",
`ChannelKind.TOOL_RPC`): a small HTTP contract the control plane uses to execute commands,
move files, and drive the iOS toolchain (xcodebuild / simctl, later xcodebuildmcp) inside
the VM.

```
control plane                            session VM (tart)
─────────────                            ─────────────────
Ec2TartProvisioner (M3)
  └── WorkerToolClient ── HTTP+bearer ──► worker daemon (apps/worker)
                                            ├── WorkspaceOps   exec / list / read / write
                                            └── IosToolBackend build / boot / install /
                                                launch / screenshot / record / ui-test
                                                  ├── RealIosToolBackend  (xcodebuild, simctl)
                                                  └── FakeIosToolBackend  (Linux/CI canned)
```

## Contract (`apps/worker/protocol.py`)

- `POST /rpc/tool` with `ToolRequest{tool, args}` → `ToolResult{ok, rc, stdout, stderr, error, data}`
- `GET /healthz` → `{status, mode}`
- Auth: `Authorization: Bearer $FURNACE_WORKER_TOKEN` (constant-time compare); the token is
  injected per-VM at provision time, like the GitHub clone token.

Tools:

| Tool | Required args | Result `data` |
| --- | --- | --- |
| `exec` | `argv` (+ `env`, `timeout_seconds`) | — (rc/stdout/stderr) |
| `list_files` | `path` | `entries` |
| `read_file` | `path` | `content`, `truncated` |
| `write_file` | `path`, `content` | `path` |
| `build_app` | `project_path`, `scheme` (+ `configuration`) | `app_path` (real: from xcodebuild) |
| `boot_simulator` | `device` | `state` |
| `install_app` | `device`, `app_path` | — |
| `launch_app` | `device`, `bundle_id` | `pid` |
| `screenshot` | `device`, `dest_path` | `path` |
| `record_video` | `device`, `dest_path` (+ `duration_seconds`) | `path`, `duration_seconds` |
| `run_ui_test` | `project_path`, `scheme`, `device` | test summary |

Artifacts (screenshot/video) are **written into the VM workspace** and fetched by the
control plane via `read_file`/`get_file` — mirroring how `simctl io` works. All paths are
resolved inside the workspace root; escapes are rejected.

## Backends

- `RealIosToolBackend` — thin adapters over `xcodebuild` / `xcrun simctl` (macOS only;
  validated on the EC2 Mac in M2/M3). `record_video` stops `simctl io recordVideo` with
  SIGINT so the file is finalized. xcodebuildmcp can later replace these adapters behind
  the same `IosToolBackend` interface.
- `FakeIosToolBackend` — deterministic canned results; screenshot/record write fixture
  bytes. This is what CI and Linux dev use (`FURNACE_WORKER_MODE=fake`, the default).

## Running

```bash
FURNACE_WORKER_MODE=fake FURNACE_WORKER_TOKEN=dev python -m apps.worker   # :8787
```

## How M3 consumes this

`Ec2TartProvisioner` will, per session: clone the tart VM, start the daemon with a fresh
`FURNACE_WORKER_TOKEN`, set `SessionHandle.tool_endpoint` to the daemon URL, and implement
`MacProvisioner.exec/put_file/get_file` by delegating to `WorkerToolClient`. The agent's
iOS tools then dispatch over the same client. Nothing in the current fake-provisioner
runtime path changes until then.
