#!/usr/bin/env bash
set -euo pipefail

# ReticulumPi Bootstrap Script
# Target: Fresh Raspberry Pi 5 running 64-bit Raspberry Pi OS (Bookworm+)

INSTALL_DIR="/opt/reticulumpi"
CONFIG_DIR="/etc/reticulumpi"
DATA_DIR="/var/lib/reticulumpi"
SERVICE_USER="reticulumpi"

echo "=== ReticulumPi Bootstrap ==="

# 1. System packages
echo "[1/6] Installing system packages..."
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git

# 2. Create service user (if not exists)
echo "[2/6] Setting up service user..."
if ! id "$SERVICE_USER" &>/dev/null; then
    sudo useradd --system --create-home --home-dir "/home/$SERVICE_USER" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
# Add hardware access groups (dialout=serial, gpio/spi/i2c=Pi hardware)
sudo usermod -aG dialout "$SERVICE_USER" 2>/dev/null || true
for grp in gpio spi i2c; do
    getent group "$grp" &>/dev/null && sudo usermod -aG "$grp" "$SERVICE_USER" 2>/dev/null || true
done

# 3. Install project
echo "[3/6] Installing ReticulumPi..."
if [ -d "$INSTALL_DIR/.git" ]; then
    sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" pull
else
    # If running from a cloned repo, copy it; otherwise clone from remote
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
    if [ -f "$PROJECT_DIR/pyproject.toml" ]; then
        sudo cp -r "$PROJECT_DIR" "$INSTALL_DIR"
        sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
    else
        echo "Error: Run this script from within the reticulumPi project directory."
        exit 1
    fi
fi

# 4. Python venv + install
echo "[4/6] Setting up Python environment..."
sudo -u "$SERVICE_USER" python3 -m venv "$INSTALL_DIR/.venv"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR"

# 5. Config directories
echo "[5/6] Setting up configuration..."
sudo mkdir -p "$CONFIG_DIR" "$DATA_DIR"
sudo chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR" "$DATA_DIR"
if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
    sudo cp "$INSTALL_DIR/config/reticulumpi/config.example.yaml" "$CONFIG_DIR/config.yaml"
    echo "  Created $CONFIG_DIR/config.yaml from example. Edit as needed."
fi

# Set up Reticulum config directory for the service user
RETICULUM_DIR="/home/$SERVICE_USER/.reticulum"
sudo -u "$SERVICE_USER" mkdir -p "$RETICULUM_DIR"
if [ ! -f "$RETICULUM_DIR/config" ]; then
    sudo -u "$SERVICE_USER" cp "$INSTALL_DIR/config/reticulum/config.example" "$RETICULUM_DIR/config"
    echo "  Created $RETICULUM_DIR/config from example. Edit as needed."
fi

# 6. Install systemd service
echo "[6/6] Installing systemd service..."
sudo cp "$INSTALL_DIR/systemd/reticulumpi.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable reticulumpi.service

echo ""
echo "=== Bootstrap complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit $CONFIG_DIR/config.yaml for plugin settings"
echo "  2. Edit $RETICULUM_DIR/config for Reticulum interfaces"
echo "  3. Start: sudo systemctl start reticulumpi"
echo "  4. Logs:  journalctl -u reticulumpi -f"
echo ""
echo "Optional — for I2P anonymous networking support:"
echo "  sudo apt install i2pd"
echo "  sudo systemctl enable --now i2pd"
echo "  Then uncomment the [I2P Interface] section in $RETICULUM_DIR/config"
