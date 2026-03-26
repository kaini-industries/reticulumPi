#!/usr/bin/env bash
set -euo pipefail

# ReticulumPi Update Script
# Pulls latest code, upgrades dependencies, and restarts the service.

INSTALL_DIR="/opt/reticulumpi"
SERVICE_USER="reticulumpi"

echo "=== ReticulumPi Update ==="

# 1. Pull latest code
echo "[1/3] Pulling latest code..."
sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" pull

# 2. Upgrade dependencies
echo "[2/3] Upgrading dependencies..."
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade rns lxmf
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR"

# 3. Restart service
echo "[3/3] Restarting service..."
sudo systemctl restart reticulumpi

echo ""
echo "=== Update complete ==="
echo "Check status: sudo systemctl status reticulumpi"
echo "View logs:    journalctl -u reticulumpi -f"
