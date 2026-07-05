#!/usr/bin/env bash

set -euo pipefail

log() {
  printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: run_spike.sh [options]

Options:
  --project PATH          Path to .xcodeproj or .xcworkspace
  --scheme NAME           Scheme to build; auto-resolved if omitted
  --device NAME           Simulator device name (default: iPhone 15)
  --os VERSION            iOS runtime version or "latest" (default: latest)
  --repo URL              Git URL to clone instead of the SampleApp
  --record-seconds N      Seconds to record video (default: 8)
  --out PATH              Artifact output directory (default: ./artifacts)
  --help                  Show this help text

Examples:
  ./spike/run_spike.sh
  ./spike/run_spike.sh --repo https://github.com/example/app.git
EOF
}

require_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || die "missing required command: $cmd"
}

abspath() {
  python3 -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "$1"
}

project_flag_for_path() {
  case "$1" in
    *.xcworkspace) printf '%s' '-workspace' ;;
    *.xcodeproj) printf '%s' '-project' ;;
    *) die "project path must end with .xcworkspace or .xcodeproj: $1" ;;
  esac
}

discover_project_path() {
  local root="$1"
  local candidate=""

  candidate="$(find "$root" -type d -name '*.xcworkspace' | sort | head -n 1 || true)"
  if [[ -n "$candidate" ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi

  candidate="$(find "$root" -type d -name '*.xcodeproj' | sort | head -n 1 || true)"
  if [[ -n "$candidate" ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi

  return 1
}

resolve_scheme() {
  local project_flag="$1"
  local project_path="$2"
  local json=""
  local scheme=""

  json="$("$XCODEBUILD_BIN" -list -json "$project_flag" "$project_path")"
  scheme="$(printf '%s' "$json" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
schemes = []
for key in ("project", "workspace"):
    node = data.get(key) or {}
    schemes = node.get("schemes") or []
    if schemes:
        break

if not schemes:
    raise SystemExit("no schemes found in xcodebuild -list -json output")

print(schemes[0])
')"

  [[ -n "$scheme" ]] || die "failed to resolve scheme from $project_path"
  printf '%s\n' "$scheme"
}

resolve_runtime() {
  local requested_os="$1"
  local json=""
  local runtime_id=""

  json="$("$XCRUN_BIN" simctl list -j runtimes)"
  runtime_id="$(printf '%s' "$json" | python3 -c '
import json
import re
import sys

requested = sys.argv[1]
data = json.load(sys.stdin)
runtimes = data.get("runtimes", [])
ios_runtimes = []

for runtime in runtimes:
    if not runtime.get("isAvailable", False):
        continue
    name = runtime.get("name", "")
    if not name.startswith("iOS"):
        continue
    version = runtime.get("version", "")
    identifier = runtime.get("identifier", "")
    ios_runtimes.append((version, identifier))

if not ios_runtimes:
    raise SystemExit("no available iOS simulator runtimes found")

def version_key(version):
    parts = re.findall(r"\d+", str(version))
    return tuple(int(part) for part in parts)

if requested == "latest":
    ios_runtimes.sort(key=lambda item: version_key(item[0]))
    print(ios_runtimes[-1][1])
    raise SystemExit(0)

for version, identifier in ios_runtimes:
    if version == requested or version.startswith(requested):
        print(identifier)
        raise SystemExit(0)

raise SystemExit(f"requested iOS runtime {requested!r} is not installed")
' "$requested_os")"

  printf '%s\n' "$runtime_id"
}

resolve_device_type() {
  local device_name="$1"
  local json=""
  local device_type_id=""

  json="$("$XCRUN_BIN" simctl list -j devicetypes)"
  device_type_id="$(printf '%s' "$json" | python3 -c '
import json
import sys

requested = sys.argv[1]
data = json.load(sys.stdin)
for device in data.get("devicetypes", []):
    if device.get("name") == requested:
        print(device.get("identifier", ""))
        raise SystemExit(0)

raise SystemExit(f"device type {requested!r} not found")
' "$device_name")"

  [[ -n "$device_type_id" ]] || die "device type $device_name not found"
  printf '%s\n' "$device_type_id"
}

find_existing_device_udid() {
  local device_name="$1"
  local runtime_id="$2"
  local json=""
  local udid=""

  json="$("$XCRUN_BIN" simctl list -j devices "$runtime_id")"
  udid="$(printf '%s' "$json" | python3 -c '
import json
import sys

requested = sys.argv[1]
data = json.load(sys.stdin)
for runtime_devices in data.get("devices", {}).values():
    for device in runtime_devices:
        if device.get("name") == requested:
            print(device.get("udid", ""))
            raise SystemExit(0)

raise SystemExit(1)
' "$device_name")"

  if [[ -n "$udid" ]]; then
    printf '%s\n' "$udid"
    return 0
  fi

  return 1
}

extract_bundle_id() {
  local app_path="$1"
  local plist="$app_path/Info.plist"
  local bundle_id=""

  if bundle_id="$(plutil -extract CFBundleIdentifier raw -o - "$plist" 2>/dev/null)"; then
    :
  else
    bundle_id="$(defaults read "$plist" CFBundleIdentifier 2>/dev/null || true)"
  fi

  [[ -n "$bundle_id" ]] || die "could not read CFBundleIdentifier from $plist"
  printf '%s\n' "$bundle_id"
}

cleanup() {
  local exit_code=$?

  if [[ -n "${RECORD_PID:-}" ]]; then
    if kill -0 "$RECORD_PID" >/dev/null 2>&1; then
      kill -INT "$RECORD_PID" >/dev/null 2>&1 || true
      wait "$RECORD_PID" >/dev/null 2>&1 || true
    fi
    RECORD_PID=""
  fi

  if [[ "${SHOULD_SHUTDOWN_SIM:-0}" -eq 1 && -n "${SIM_UDID:-}" ]]; then
    "$XCRUN_BIN" simctl shutdown "$SIM_UDID" >/dev/null 2>&1 || true
  fi

  if [[ -n "${TEMP_WORKDIR:-}" && -d "${TEMP_WORKDIR:-}" ]]; then
    rm -rf "$TEMP_WORKDIR"
  fi

  exit "$exit_code"
}

PROJECT_PATH_RAW=""
SCHEME=""
DEVICE_NAME="iPhone 15"
OS_VERSION="latest"
REPO_URL=""
RECORD_SECONDS=8
OUT_DIR="./artifacts"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)
      [[ $# -ge 2 ]] || die "--project requires a path"
      PROJECT_PATH_RAW="$2"
      shift 2
      ;;
    --scheme)
      [[ $# -ge 2 ]] || die "--scheme requires a value"
      SCHEME="$2"
      shift 2
      ;;
    --device)
      [[ $# -ge 2 ]] || die "--device requires a value"
      DEVICE_NAME="$2"
      shift 2
      ;;
    --os)
      [[ $# -ge 2 ]] || die "--os requires a value"
      OS_VERSION="$2"
      shift 2
      ;;
    --repo)
      [[ $# -ge 2 ]] || die "--repo requires a URL"
      REPO_URL="$2"
      shift 2
      ;;
    --record-seconds)
      [[ $# -ge 2 ]] || die "--record-seconds requires a value"
      RECORD_SECONDS="$2"
      shift 2
      ;;
    --out)
      [[ $# -ge 2 ]] || die "--out requires a path"
      OUT_DIR="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[[ "$RECORD_SECONDS" =~ ^[0-9]+$ ]] || die "--record-seconds must be an integer"
[[ "$RECORD_SECONDS" -gt 0 ]] || die "--record-seconds must be greater than zero"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAMPLE_APP_DIR="$SCRIPT_DIR/SampleApp"
DEFAULT_SAMPLE_PROJECT="$SAMPLE_APP_DIR/RavenSample.xcodeproj"
XCODEBUILD_BIN="$(command -v xcodebuild || true)"
XCRUN_BIN="$(command -v xcrun || true)"

[[ -x "$XCODEBUILD_BIN" ]] || die "xcodebuild is required but was not found in PATH"
[[ -x "$XCRUN_BIN" ]] || die "xcrun is required but was not found in PATH"
command -v xcode-select >/dev/null 2>&1 || die "xcode-select is required but was not found in PATH"
xcode-select -p >/dev/null 2>&1 || die "xcode-select -p failed; install Xcode and select it with sudo xcode-select --switch /Applications/Xcode.app"
"$XCRUN_BIN" simctl help >/dev/null 2>&1 || die "xcrun simctl is unavailable; install Xcode with Simulator support"

USE_SAMPLE_APP=0
SOURCE_ROOT="$(pwd)"
TEMP_WORKDIR=""

if [[ -n "$REPO_URL" ]]; then
  TEMP_WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/raven-spike.XXXXXX")"
  log "cloning repository into $TEMP_WORKDIR"
  git clone "$REPO_URL" "$TEMP_WORKDIR/repo"
  SOURCE_ROOT="$TEMP_WORKDIR/repo"
else
  USE_SAMPLE_APP=1
  [[ -d "$SAMPLE_APP_DIR" ]] || die "SampleApp directory is missing: $SAMPLE_APP_DIR"
  command -v xcodegen >/dev/null 2>&1 || die "xcodegen is required to generate the SampleApp project; install it with: brew install xcodegen"
  log "generating SampleApp project with xcodegen"
  (
    cd "$SAMPLE_APP_DIR"
    xcodegen generate
  )
fi

if [[ -z "$PROJECT_PATH_RAW" ]]; then
  if [[ "$USE_SAMPLE_APP" -eq 1 ]]; then
    PROJECT_PATH_RAW="$DEFAULT_SAMPLE_PROJECT"
  else
    PROJECT_PATH_RAW="$(discover_project_path "$SOURCE_ROOT")" || die "could not find a .xcodeproj or .xcworkspace under $SOURCE_ROOT; pass --project explicitly"
  fi
fi

if [[ "$PROJECT_PATH_RAW" != /* ]]; then
  if [[ "$USE_SAMPLE_APP" -eq 1 ]]; then
    PROJECT_PATH="$(abspath "$PROJECT_PATH_RAW")"
  else
    PROJECT_PATH="$(abspath "$SOURCE_ROOT/$PROJECT_PATH_RAW")"
  fi
else
  PROJECT_PATH="$PROJECT_PATH_RAW"
fi

[[ -e "$PROJECT_PATH" ]] || die "project path does not exist: $PROJECT_PATH"

PROJECT_FLAG="$(project_flag_for_path "$PROJECT_PATH")"

if [[ -z "$SCHEME" ]]; then
  log "resolving scheme from Xcode metadata"
  SCHEME="$(resolve_scheme "$PROJECT_FLAG" "$PROJECT_PATH")"
fi

mkdir -p "$OUT_DIR"
DERIVED_DATA_PATH="$OUT_DIR/DerivedData"
SCREENSHOT_PATH="$OUT_DIR/screenshot.png"
VIDEO_PATH="$OUT_DIR/demo.mp4"

RUNTIME_ID="$(resolve_runtime "$OS_VERSION")"
DEVICE_TYPE_ID="$(resolve_device_type "$DEVICE_NAME")"

SIM_UDID=""
SHOULD_SHUTDOWN_SIM=0
SIM_UDID="$(find_existing_device_udid "$DEVICE_NAME" "$RUNTIME_ID" 2>/dev/null || true)"

if [[ -z "$SIM_UDID" ]]; then
  RUNTIME_VERSION=""
  RUNTIME_VERSION="$("$XCRUN_BIN" simctl list -j runtimes | python3 -c '
import json
import sys

runtime_id = sys.argv[1]
data = json.load(sys.stdin)
for runtime in data.get("runtimes", []):
    if runtime.get("identifier") == runtime_id:
        print(runtime.get("version", ""))
        raise SystemExit(0)

raise SystemExit(1)
' "$RUNTIME_ID")"
  DEVICE_INSTANCE_NAME="Raven ${DEVICE_NAME} ${RUNTIME_VERSION}"
  log "creating simulator device $DEVICE_INSTANCE_NAME"
  SIM_UDID="$("$XCRUN_BIN" simctl create "$DEVICE_INSTANCE_NAME" "$DEVICE_TYPE_ID" "$RUNTIME_ID")"
  SHOULD_SHUTDOWN_SIM=1
else
  log "using existing simulator $SIM_UDID"
  DEVICE_STATE="$("$XCRUN_BIN" simctl list -j devices "$RUNTIME_ID" | python3 -c '
import json
import sys

udid = sys.argv[1]
data = json.load(sys.stdin)
for runtime_devices in data.get("devices", {}).values():
    for device in runtime_devices:
        if device.get("udid") == udid:
            print(device.get("state", ""))
            raise SystemExit(0)

raise SystemExit(1)
' "$SIM_UDID")"
  if [[ "$DEVICE_STATE" != "Booted" ]]; then
    SHOULD_SHUTDOWN_SIM=1
  fi
fi

trap cleanup EXIT INT TERM HUP

log "booting simulator $SIM_UDID"
"$XCRUN_BIN" simctl boot "$SIM_UDID" >/dev/null 2>&1 || true
"$XCRUN_BIN" simctl bootstatus "$SIM_UDID" -b
open -a Simulator >/dev/null 2>&1 || true

log "building scheme $SCHEME"
BUILD_CMD=("$XCODEBUILD_BIN" -scheme "$SCHEME" -destination "platform=iOS Simulator,id=$SIM_UDID" -derivedDataPath "$DERIVED_DATA_PATH" -configuration Debug build)
if [[ "$PROJECT_FLAG" == "-workspace" ]]; then
  BUILD_CMD+=(-workspace "$PROJECT_PATH")
else
  BUILD_CMD+=(-project "$PROJECT_PATH")
fi
"${BUILD_CMD[@]}"

mapfile -t APP_CANDIDATES < <(find "$DERIVED_DATA_PATH/Build/Products" -type d -name '*.app' | sort)
[[ "${#APP_CANDIDATES[@]}" -gt 0 ]] || die "could not find a built .app under $DERIVED_DATA_PATH/Build/Products"
APP_PATH="${APP_CANDIDATES[0]}"

BUNDLE_ID="$(extract_bundle_id "$APP_PATH")"
log "installing $APP_PATH"
"$XCRUN_BIN" simctl install "$SIM_UDID" "$APP_PATH"

log "launching bundle id $BUNDLE_ID"
LAUNCH_OUTPUT="$("$XCRUN_BIN" simctl launch "$SIM_UDID" "$BUNDLE_ID")"
log "$LAUNCH_OUTPUT"

log "capturing screenshot to $SCREENSHOT_PATH"
"$XCRUN_BIN" simctl io "$SIM_UDID" screenshot "$SCREENSHOT_PATH"

log "recording video to $VIDEO_PATH for ${RECORD_SECONDS}s"
"$XCRUN_BIN" simctl io "$SIM_UDID" recordVideo "$VIDEO_PATH" &
RECORD_PID=$!
sleep "$RECORD_SECONDS"
kill -INT "$RECORD_PID" >/dev/null 2>&1 || true
wait "$RECORD_PID" >/dev/null 2>&1 || true
RECORD_PID=""

printf '\nSummary:\n'
printf '  Project:   %s\n' "$PROJECT_PATH"
printf '  Scheme:    %s\n' "$SCHEME"
printf '  Device:    %s (%s)\n' "$DEVICE_NAME" "$SIM_UDID"
printf '  Output:    %s\n' "$OUT_DIR"
printf '  Screenshot %s\n' "$SCREENSHOT_PATH"
printf '  Video:     %s\n' "$VIDEO_PATH"
printf '  DerivedData %s\n' "$DERIVED_DATA_PATH"
