#!/usr/bin/env bash
# Default first-creation provisioning script for sprites.
#
# This runs inside the sprite on first creation only (never on resume).
# Override via terminal.sprites.provision_script in config.yaml, or
# use terminal.sprites.provision_inline for an inline script.
#
# The sprite runs a minimal Linux image. This script installs common
# development tools so the agent can build, test, and run code without
# a first-round of package installs on every session.
set -euo pipefail

echo "[provision] starting first-creation setup..."

# Detect the package manager
if command -v apt-get &>/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq git curl wget build-essential python3-dev 2>/dev/null || true
elif command -v apk &>/dev/null; then
    apk add --no-cache git curl wget build-base python3-dev 2>/dev/null || true
elif command -v dnf &>/dev/null; then
    dnf install -y git curl wget gcc make python3-devel 2>/dev/null || true
fi

echo "[provision] setup complete."