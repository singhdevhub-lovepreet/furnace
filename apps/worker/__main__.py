"""Run the worker daemon: `python -m apps.worker`.

Environment:
  FURNACE_WORKER_MODE       fake | real (default: fake)
  FURNACE_WORKER_WORKSPACE  workspace root (default: ./workspace)
  FURNACE_WORKER_TOKEN      bearer token required on /rpc/tool (default: none)
  FURNACE_WORKER_HOST       bind host (default: 0.0.0.0)
  FURNACE_WORKER_PORT       bind port (default: 8787)
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from apps.worker.daemon import create_worker_app


def main() -> None:
    app = create_worker_app(
        mode=os.environ.get("FURNACE_WORKER_MODE", "fake"),
        workspace_root=Path(os.environ.get("FURNACE_WORKER_WORKSPACE", "./workspace")),
        auth_token=os.environ.get("FURNACE_WORKER_TOKEN"),
    )
    uvicorn.run(
        app,
        host=os.environ.get("FURNACE_WORKER_HOST", "0.0.0.0"),
        port=int(os.environ.get("FURNACE_WORKER_PORT", "8787")),
    )


if __name__ == "__main__":
    main()
