#!/bin/bash
# Prepare an EC2 Mac host (mac2.metal) to run tart session VMs.
set -euo pipefail

if ! command -v brew >/dev/null 2>&1; then
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  eval "$(/opt/homebrew/bin/brew shellenv)"
fi

brew install cirruslabs/cli/tart socat

# Pull the Xcode-preloaded macOS base image (large; one-time).
tart pull ghcr.io/cirruslabs/macos-sequoia-xcode:latest

echo "Host ready. Next: clone raven-golden and run 02-setup-guest.sh inside it."
