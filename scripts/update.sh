#!/usr/bin/env bash
set -euo pipefail

# ReticulumPi Update Script
# Pulls latest code (or rsyncs from source), upgrades dependencies, and restarts.

INSTALL_DIR="/opt/reticulumpi"
SERVICE_USER="reticulumpi"

echo "=== ReticulumPi Update ==="

# 1. Update code — git pull if it's a repo, otherwise rsync from source
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "[1/4] Pulling latest code..."
    if ! git -C "$INSTALL_DIR" pull; then
        echo "Error: git pull failed. Check network and repository state."
        exit 1
    fi
    sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
else
    # Find the source repo (the directory containing this script)
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    SOURCE_DIR="$(dirname "$SCRIPT_DIR")"
    if [ -f "$SOURCE_DIR/pyproject.toml" ] && [ "$SOURCE_DIR" != "$INSTALL_DIR" ]; then
        echo "[1/4] Syncing from $SOURCE_DIR..."
        rsync -a \
            --exclude='.git' \
            --exclude='.venv' \
            --exclude='__pycache__' \
            --exclude='*.pyc' \
            --exclude='.ruff_cache' \
            --exclude='.pytest_cache' \
            "$SOURCE_DIR/" "$INSTALL_DIR/"
        sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
    else
        echo "Error: $INSTALL_DIR is not a git repository and no source repo found."
        echo "Run this script from the cloned reticulumPi directory, or set up $INSTALL_DIR as a git repo."
        exit 1
    fi
fi

# 2. Upgrade all dependencies and reinstall
echo "[2/4] Upgrading dependencies..."
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
if ! sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade -e "$INSTALL_DIR"; then
    echo "Error: pip install failed. Check dependencies."
    exit 1
fi

# Upgrade NomadNet if installed
if "$INSTALL_DIR/.venv/bin/pip" show nomadnet &>/dev/null; then
    echo "  Upgrading NomadNet..."
    sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade nomadnet
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
