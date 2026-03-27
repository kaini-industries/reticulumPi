#!/usr/bin/env bash
set -euo pipefail

# Launch the NomadNet TUI interactively over SSH.
# Uses a separate browse-only config so the daemon keeps running undisturbed.
#
# Usage (from SSH):
#   sudo -u reticulumpi bash <install_dir>/scripts/nomadnet-tui.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(dirname "$SCRIPT_DIR")"
VENV_BIN="$INSTALL_DIR/.venv/bin"
RNS_CONFIG="/home/reticulumpi/.reticulum"

# Use a separate config directory for TUI browsing.
# This lets the TUI connect to rnsd as its own client while the daemon
# continues serving pages uninterrupted.
TUI_CONFIG="/home/reticulumpi/.nomadnet-tui"

# Create the TUI config directory on first use
if [ ! -d "$TUI_CONFIG" ]; then
    mkdir -p "$TUI_CONFIG"
    # Write a minimal config: client-only, no node hosting
    cat > "$TUI_CONFIG/config" <<'NOMADCFG'
[logging]
loglevel = 4
destination = file

[client]
enable_client = yes
user_interface = text
announce_at_start = no
try_propagation_on_send_fail = yes

[textui]
intro_time = 1
theme = dark
colormode = 256
glyphs = unicode
mouse_enabled = True
editor = nano

[node]
enable_node = no
NOMADCFG
    echo "Created browse-only TUI config at $TUI_CONFIG"
fi

echo "Starting NomadNet TUI... (Ctrl+Q to exit)"
echo "  (The NomadNet daemon continues serving pages in the background)"
"$VENV_BIN/nomadnet" --textui --config "$TUI_CONFIG" --rnsconfig "$RNS_CONFIG"

echo "NomadNet TUI exited."
