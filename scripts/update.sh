#!/usr/bin/env bash
set -euo pipefail

# ReticulumPi Update Script
# Pulls latest code, upgrades dependencies, and restarts the service.

INSTALL_DIR="/opt/reticulumpi"
SERVICE_USER="reticulumpi"

echo "=== ReticulumPi Update ==="

# 1. Verify install directory
if [ ! -d "$INSTALL_DIR/.git" ]; then
    echo "Error: $INSTALL_DIR is not a git repository. Run bootstrap.sh first."
    exit 1
fi

# 2. Pull latest code (run as current user who has SSH/git credentials)
echo "[1/3] Pulling latest code..."
if ! git -C "$INSTALL_DIR" pull; then
    echo "Error: git pull failed. Check network and repository state."
    exit 1
fi
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# 3. Upgrade all dependencies and reinstall
echo "[2/3] Upgrading dependencies..."
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
if ! sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade -e "$INSTALL_DIR"; then
    echo "Error: pip install failed. Check dependencies."
    exit 1
fi

# 4. Restart service
echo "[3/3] Restarting service..."
sudo systemctl restart reticulumpi

echo ""
echo "=== Update complete ==="
echo "Check status: sudo systemctl status reticulumpi"
echo "View logs:    journalctl -u reticulumpi -f"
