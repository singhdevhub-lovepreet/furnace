#!/bin/bash
# Provision the guest macOS VM that becomes the raven-golden tart image.
# Run inside the VM (default cirruslabs credentials: admin/admin).
set -euo pipefail

RAVEN_REPO_URL="${RAVEN_REPO_URL:-https://github.com/singhdevhub-lovepreet/furnace.git}"
RAVEN_DIR="$HOME/raven"

# 1. Verify Xcode (preinstalled in cirruslabs *-xcode images).
xcode-select -p
xcodebuild -version

# 2. Ensure an iOS Simulator runtime is present.
xcrun simctl list runtimes | grep -q iOS || xcodebuild -downloadPlatform iOS

# 3. Node + xcodebuildmcp (tool backend the worker can shell out to).
command -v brew >/dev/null 2>&1 || \
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || true
brew install node python@3.12
npm install -g xcodebuildmcp || echo "WARN: xcodebuildmcp install failed; worker falls back to raw xcodebuild/simctl"

# 4. Raven worker daemon (apps/worker) + Python deps.
if [ ! -d "$RAVEN_DIR" ]; then
  git clone --depth 1 "$RAVEN_REPO_URL" "$RAVEN_DIR"
fi
python3 -m pip install --user fastapi uvicorn httpx pydantic

# 5. Make `python3 -m apps.worker` resolvable from $HOME.
cat > "$HOME/.raven-worker-env" <<EOF
export PYTHONPATH="$RAVEN_DIR"
export FURNACE_WORKER_MODE=real
export FURNACE_WORKER_WORKSPACE="$HOME/workspace"
EOF
grep -q raven-worker-env "$HOME/.zprofile" 2>/dev/null || \
  echo 'source "$HOME/.raven-worker-env"' >> "$HOME/.zprofile"
mkdir -p "$HOME/workspace"

# 6. Smoke test: the worker boots and reports healthy.
(
  source "$HOME/.raven-worker-env"
  FURNACE_WORKER_TOKEN=smoke python3 -m apps.worker &
  WORKER_PID=$!
  sleep 3
  curl -fsS http://localhost:8787/healthz
  kill "$WORKER_PID"
)

echo "Guest ready. Stop the VM (tart stop raven-golden) to freeze the golden image."
