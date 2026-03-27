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
    echo "[1/4] Pulling latest code..."
    if ! git -C "$INSTALL_DIR" pull; then
        echo "Error: git pull failed. Check network and repository state."
        exit 1
    fi
    sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
else
    echo "[1/4] No git repo — assuming code is already synced."
fi

# 2. Upgrade all dependencies and reinstall
echo "[2/4] Upgrading dependencies..."
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
if ! sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade -e "$INSTALL_DIR"; then
    echo "Error: pip install failed. Check dependencies."
    exit 1
fi

# Upgrade NomadNet if installed
if sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" show nomadnet &>/dev/null; then
    echo "  Upgrading NomadNet..."
    sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade nomadnet
fi

# Upgrade MeshChat if installed
MESHCHAT_DIR="$INSTALL_DIR/meshchat"
if [ -d "$MESHCHAT_DIR/.git" ]; then
    echo "  Upgrading MeshChat..."
    MESHCHAT_OLD=$(git -C "$MESHCHAT_DIR" rev-parse HEAD)
    git -C "$MESHCHAT_DIR" pull
    MESHCHAT_NEW=$(git -C "$MESHCHAT_DIR" rev-parse HEAD)
    sudo -u "$SERVICE_USER" "$MESHCHAT_DIR/.venv/bin/pip" install -r "$MESHCHAT_DIR/requirements.txt"
    # Rebuild frontend if source changed
    if [ "$MESHCHAT_OLD" != "$MESHCHAT_NEW" ] && [ -f "$MESHCHAT_DIR/package.json" ]; then
        echo "  Rebuilding MeshChat frontend..."
        cd "$MESHCHAT_DIR"
        sudo -u "$SERVICE_USER" npm install --omit=dev
        sudo -u "$SERVICE_USER" npm run build-frontend
        cd - >/dev/null
    fi
fi

# 3. Update systemd service files if they changed (template paths)
echo "[3/4] Updating systemd services..."
SERVICES_CHANGED=false
for svc in reticulumpi.service rnsd.service; do
    src="$INSTALL_DIR/systemd/$svc"
    dest="/etc/systemd/system/$svc"
    if [ -f "$src" ] && [ -f "$dest" ]; then
        # Template the install dir and compare against the installed version
        templated=$(sed "s|/opt/reticulumpi|$INSTALL_DIR|g" "$src")
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

# 4. Restart services
echo "[4/4] Restarting services..."
if systemctl is-active --quiet rnsd; then
    sudo systemctl restart rnsd
fi
sudo systemctl restart reticulumpi

echo ""
echo "=== Update complete ==="
echo "Check status: sudo systemctl status reticulumpi"
echo "View logs:    journalctl -u reticulumpi -f"
