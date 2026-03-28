#!/usr/bin/env bash
set -euo pipefail

# ReticulumPi Update Script
# Pulls latest code (or rsyncs from source), upgrades dependencies, and restarts.

# Auto-detect install directory from this script's location.
# Works whether installed at /opt/reticulumpi, in-place, or anywhere else.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE_USER="reticulumpi"

if [ ! -f "$INSTALL_DIR/pyproject.toml" ]; then
    echo "Error: Cannot find reticulumPi project at $INSTALL_DIR"
    exit 1
fi

echo "=== ReticulumPi Update ==="
echo "Install directory: $INSTALL_DIR"

# 1. Update code — git pull if it's a repo, otherwise already up-to-date (in-place)
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "[1/5] Pulling latest code..."
    # Detect the repo owner so we run git as the right user (SSH keys, known_hosts)
    REPO_OWNER="$(stat -c '%U' "$INSTALL_DIR/.git")"
    if ! sudo -u "$REPO_OWNER" git -C "$INSTALL_DIR" pull; then
        echo "Warning: git pull failed. Continuing with local code."
    fi
else
    echo "[1/5] No git repo — assuming code is already synced."
fi

# 2. Upgrade all dependencies and reinstall
echo "[2/5] Upgrading dependencies..."

# Ensure the service user can traverse to the source tree (editable install)
INSTALL_PARENT="$(dirname "$INSTALL_DIR")"
if ! sudo -u "$SERVICE_USER" test -x "$INSTALL_PARENT"; then
    echo "  Fixing permissions: $INSTALL_PARENT needs o+x for $SERVICE_USER"
    sudo chmod o+x "$INSTALL_PARENT"
fi
if ! sudo -u "$SERVICE_USER" test -r "$INSTALL_DIR/src"; then
    echo "  Fixing permissions: $INSTALL_DIR needs o+rx for $SERVICE_USER"
    sudo chmod -R o+rX "$INSTALL_DIR/src"
fi

VENV="$INSTALL_DIR/.venv"
sudo "$VENV/bin/pip" install --upgrade pip
if ! sudo "$VENV/bin/pip" install --upgrade -e "$INSTALL_DIR"; then
    echo "Error: pip install failed. Check dependencies."
    exit 1
fi
# Ensure the service user can read installed packages
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$VENV"

# Upgrade aiohttp if installed (web dashboard)
if sudo "$VENV/bin/pip" show aiohttp &>/dev/null; then
    echo "  Upgrading aiohttp (web dashboard)..."
    sudo "$VENV/bin/pip" install --upgrade "aiohttp>=3.9,<4.0"
fi

# Upgrade rnodeconf if installed
if sudo "$VENV/bin/pip" show rnodeconf &>/dev/null; then
    echo "  Upgrading rnodeconf (LoRa/RNode tools)..."
    sudo "$VENV/bin/pip" install --upgrade rnodeconf
fi

# Upgrade NomadNet if installed
if sudo "$VENV/bin/pip" show nomadnet &>/dev/null; then
    echo "  Upgrading NomadNet..."
    sudo "$VENV/bin/pip" install --upgrade nomadnet
fi

# Upgrade MeshChat if installed
MESHCHAT_DIR="$INSTALL_DIR/meshchat"
if [ -d "$MESHCHAT_DIR/.git" ]; then
    echo "  Upgrading MeshChat..."
    MESHCHAT_OWNER="$(stat -c '%U' "$MESHCHAT_DIR/.git")"
    MESHCHAT_OLD=$(git -C "$MESHCHAT_DIR" rev-parse HEAD)
    sudo -u "$MESHCHAT_OWNER" git -C "$MESHCHAT_DIR" pull || echo "  Warning: MeshChat git pull failed."
    MESHCHAT_NEW=$(git -C "$MESHCHAT_DIR" rev-parse HEAD)
    sudo "$MESHCHAT_DIR/.venv/bin/pip" install -r "$MESHCHAT_DIR/requirements.txt"
    sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$MESHCHAT_DIR/.venv"
    # Rebuild frontend if source changed
    if [ "$MESHCHAT_OLD" != "$MESHCHAT_NEW" ] && [ -f "$MESHCHAT_DIR/package.json" ]; then
        echo "  Rebuilding MeshChat frontend..."
        (cd "$MESHCHAT_DIR" && npm install --omit=dev && npm run build-frontend)
    fi
fi

# 3. Update systemd service files if they changed (template paths)
echo "[3/5] Updating systemd services..."
SERVICES_CHANGED=false
for svc in reticulumpi.service rnsd.service; do
    src="$INSTALL_DIR/systemd/$svc"
    dest="/etc/systemd/system/$svc"
    if [ -f "$src" ] && [ -f "$dest" ]; then
        # Template only the venv/binary path — leave other /opt/reticulumpi paths
        # (e.g., meshchat) untouched since they may be installed separately
        templated=$(sed "s|/opt/reticulumpi/\.venv|$INSTALL_DIR/.venv|g" "$src")
        if ! echo "$templated" | diff -q - "$dest" &>/dev/null; then
            echo "$templated" | sudo tee "$dest" >/dev/null
            SERVICES_CHANGED=true
            echo "  Updated $svc"
        fi
    fi
done
if [ "$SERVICES_CHANGED" = true ]; then
    sudo systemctl daemon-reload
fi

# 4. Ensure all ReadWritePaths directories exist (systemd namespace mount fails otherwise)
echo "[4/5] Pre-creating ReadWritePaths directories..."
for svc in reticulumpi.service; do
    dest="/etc/systemd/system/$svc"
    if [ -f "$dest" ]; then
        rwpaths=$(grep -oP '^ReadWritePaths=\K.*' "$dest" || true)
        for dir in $rwpaths; do
            if [ ! -d "$dir" ]; then
                echo "  Creating: $dir"
                sudo mkdir -p "$dir"
                sudo chown "$SERVICE_USER:$SERVICE_USER" "$dir"
            fi
        done
    fi
done

# 5. Restart services
echo "[5/5] Restarting services..."
if systemctl is-active --quiet rnsd; then
    sudo systemctl restart rnsd
    echo "  Waiting for rnsd to initialize..."
    sleep 3
fi
sudo systemctl restart reticulumpi

echo ""
echo "=== Update complete ==="
echo "Check status: sudo systemctl status reticulumpi"
echo "View logs:    journalctl -u reticulumpi -f"
