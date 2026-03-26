#!/usr/bin/env bash
set -euo pipefail

# Launch the NomadNet TUI interactively over SSH.
# Temporarily stops the NomadNet daemon; it auto-restarts after you exit.
#
# Usage (from SSH):
#   sudo -u reticulumpi bash <install_dir>/scripts/nomadnet-tui.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(dirname "$SCRIPT_DIR")"
VENV_BIN="$INSTALL_DIR/.venv/bin"
NOMADNET_CONFIG="/home/reticulumpi/.nomadnet"
RNS_CONFIG="/home/reticulumpi/.reticulum"

# Kill the running NomadNet daemon (spawned by nomadnet_server plugin)
NOMADNET_PID=$(pgrep -f "nomadnet --daemon" || true)
if [ -n "$NOMADNET_PID" ]; then
    echo "Stopping NomadNet daemon (PID: $NOMADNET_PID)..."
    kill "$NOMADNET_PID"
    sleep 2
fi

# Launch NomadNet TUI (blocks until user exits with Ctrl+Q)
echo "Starting NomadNet TUI... (Ctrl+Q to exit)"
"$VENV_BIN/nomadnet" --textui --config "$NOMADNET_CONFIG" --rnsconfig "$RNS_CONFIG"

echo "NomadNet TUI exited. The daemon will auto-restart within 30 seconds."
