#!/usr/bin/env bash
# install-service.sh — Install the context graph API as a launchd service (macOS)
#
# Usage:
#   ./scripts/install-service.sh [--python /path/to/python3]
#
# Defaults:
#   --python   auto-detected via `which python3` (or venv/bin/python3 if present)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$REPO_ROOT/service/com.contextgraph.api.plist.template"
PLIST_NAME="com.contextgraph.api.plist"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
DEST="$LAUNCH_AGENTS/$PLIST_NAME"

# --- Resolve Python path ---
PYTHON_PATH=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --python) PYTHON_PATH="$2"; shift 2 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

if [[ -z "$PYTHON_PATH" ]]; then
    if [[ -f "$REPO_ROOT/venv/bin/python3" ]]; then
        PYTHON_PATH="$REPO_ROOT/venv/bin/python3"
    else
        PYTHON_PATH="$(which python3)"
    fi
fi

if [[ ! -x "$PYTHON_PATH" ]]; then
    echo "ERROR: Python not found at: $PYTHON_PATH"
    echo "Run with --python /path/to/python3"
    exit 1
fi

echo "  INSTALL_PATH : $REPO_ROOT"
echo "  PYTHON_PATH  : $PYTHON_PATH"
echo "  Destination  : $DEST"
echo ""

# --- Render template ---
mkdir -p "$LAUNCH_AGENTS"
sed \
    -e "s|INSTALL_PATH|$REPO_ROOT|g" \
    -e "s|PYTHON_PATH|$PYTHON_PATH|g" \
    "$TEMPLATE" > "$DEST"

echo "Plist written to: $DEST"

# --- Load (or reload) the service ---
if launchctl list | grep -q "com.contextgraph.api"; then
    echo "Reloading existing service..."
    launchctl unload "$DEST"
fi

launchctl load "$DEST"
echo "Service loaded. Check logs: tail -f /tmp/contextgraph-api.log"
echo ""
echo "To verify: curl http://localhost:8300/health"
