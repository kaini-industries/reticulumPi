#!/usr/bin/env bash
set -euo pipefail

# ReticulumPi Bootstrap Script
# Target: Fresh Raspberry Pi 5 running 64-bit Raspberry Pi OS (Bookworm+)

AUTO_START=false
WITH_NOMADNET=false
for arg in "$@"; do
    case "$arg" in
        --start) AUTO_START=true ;;
        --with-nomadnet) WITH_NOMADNET=true ;;
    esac
done

INSTALL_DIR="/opt/reticulumpi"
CONFIG_DIR="/etc/reticulumpi"
DATA_DIR="/var/lib/reticulumpi"
SERVICE_USER="reticulumpi"

echo "=== ReticulumPi Bootstrap ==="

# 1. System packages
echo "[1/7] Installing system packages..."
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git

# 2. Create service user (if not exists)
echo "[2/7] Setting up service user..."
if ! id "$SERVICE_USER" &>/dev/null; then
    sudo useradd --system --create-home --home-dir "/home/$SERVICE_USER" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
# Add hardware access groups (dialout=serial, gpio/spi/i2c=Pi hardware)
sudo usermod -aG dialout "$SERVICE_USER" 2>/dev/null || true
for grp in gpio spi i2c; do
    getent group "$grp" &>/dev/null && sudo usermod -aG "$grp" "$SERVICE_USER" 2>/dev/null || true
done

# 3. Install project
echo "[3/7] Installing ReticulumPi..."
if [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" pull
    sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
else
    # If running from a cloned repo, copy it; otherwise clone from remote
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
    if [ -f "$PROJECT_DIR/pyproject.toml" ]; then
        sudo mkdir -p "$INSTALL_DIR"
        # Exclude dev artifacts that won't work on the target
        rsync -a \
            --exclude='.git' \
            --exclude='.venv' \
            --exclude='__pycache__' \
            --exclude='*.pyc' \
            --exclude='.ruff_cache' \
            "$PROJECT_DIR/" "$INSTALL_DIR/"
        sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
    else
        echo "Error: Run this script from within the reticulumPi project directory."
        exit 1
    fi
fi

# 4. Python venv + install
echo "[4/7] Setting up Python environment..."
sudo -u "$SERVICE_USER" python3 -m venv "$INSTALL_DIR/.venv"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR"

# 4b. Optional: Install NomadNet
if [ "$WITH_NOMADNET" = true ]; then
    echo "[4b/7] Installing NomadNet..."
    sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install nomadnet
fi

# 5. Config directories
echo "[5/7] Setting up configuration..."
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
    echo "  TIP: For local-mesh-only operation (no TCP server), use the minimal config instead:"
    echo "       sudo -u $SERVICE_USER cp $INSTALL_DIR/config/reticulum/config.minimal $RETICULUM_DIR/config"
fi

# Create all directories required by systemd ReadWritePaths.
# systemd's ProtectHome=read-only + ReadWritePaths fails with exit 226
# if any listed path does not exist at service start time.
sudo -u "$SERVICE_USER" mkdir -p \
    "/home/$SERVICE_USER/.config/reticulumpi" \
    "/home/$SERVICE_USER/.local/share/reticulumpi" \
    "/home/$SERVICE_USER/.local/share" \
    "/home/$SERVICE_USER/.nomadnet"

# 6. NomadNet directories and config (if enabled)
if [ "$WITH_NOMADNET" = true ]; then
    echo "[6/7] Setting up NomadNet..."
    NOMADNET_DIR="/home/$SERVICE_USER/.nomadnet"
    sudo -u "$SERVICE_USER" mkdir -p "$NOMADNET_DIR/storage/pages" "$NOMADNET_DIR/storage/files"

    # Install example pages if none exist
    if [ -d "$INSTALL_DIR/config/nomadnet/pages" ] && [ ! -f "$NOMADNET_DIR/storage/pages/index.mu" ]; then
        sudo -u "$SERVICE_USER" cp "$INSTALL_DIR/config/nomadnet/pages/"*.mu "$NOMADNET_DIR/storage/pages/"
        echo "  Installed example NomadNet pages to $NOMADNET_DIR/storage/pages/"
    fi

    # Auto-configure: NomadNet requires shared instance mode and the plugin enabled.
    # Set use_shared_instance: true (required so reticulumPi and NomadNet share rnsd)
    sudo sed -i 's/^  use_shared_instance: false$/  use_shared_instance: true/' "$CONFIG_DIR/config.yaml"
    echo "  Set use_shared_instance: true in $CONFIG_DIR/config.yaml"

    # Uncomment and enable the nomadnet_server plugin
    sudo sed -i '/^    #nomadnet_server:$/,/^    #  max_restarts:/{
s/^    #nomadnet_server:/    nomadnet_server:/
s/^    #  enabled: false/      enabled: true/
s/^    #  config_dir:/      config_dir:/
s/^    #  health_check_interval:/      health_check_interval:/
s/^    #  auto_restart:/      auto_restart:/
s/^    #  max_restarts:/      max_restarts:/
}' "$CONFIG_DIR/config.yaml"
    echo "  Enabled nomadnet_server plugin in $CONFIG_DIR/config.yaml"
fi

# 7. Install systemd services (template paths to match INSTALL_DIR)
echo "[7/7] Installing systemd services..."
sudo sed "s|/opt/reticulumpi|$INSTALL_DIR|g" "$INSTALL_DIR/systemd/reticulumpi.service" \
    > /etc/systemd/system/reticulumpi.service
if [ "$WITH_NOMADNET" = true ]; then
    sudo sed "s|/opt/reticulumpi|$INSTALL_DIR|g" "$INSTALL_DIR/systemd/rnsd.service" \
        > /etc/systemd/system/rnsd.service
    sudo systemctl daemon-reload
    sudo systemctl enable rnsd.service
    sudo systemctl enable reticulumpi.service
    echo "  Installed rnsd.service (required for NomadNet shared instance)"
else
    sudo systemctl daemon-reload
    sudo systemctl enable reticulumpi.service
fi

echo ""
echo "=== Bootstrap complete ==="
echo ""

if [ "$AUTO_START" = true ]; then
    echo "Starting ReticulumPi service..."
    sudo systemctl start reticulumpi.service
    echo "Service started. Check logs with: journalctl -u reticulumpi -f"
else
    echo "Next steps:"
    echo "  1. Edit $CONFIG_DIR/config.yaml for plugin settings"
    echo "  2. Edit $RETICULUM_DIR/config for Reticulum interfaces"
    echo ""
    # Interactive prompt (only if running in a terminal)
    if [ -t 0 ]; then
        read -rp "Start ReticulumPi service now? [y/N] " answer
        case "$answer" in
            [Yy]*)
                sudo systemctl start reticulumpi.service
                echo "Service started. Check logs with: journalctl -u reticulumpi -f"
                ;;
            *)
                echo "To start later: sudo systemctl start reticulumpi"
                echo "Logs: journalctl -u reticulumpi -f"
                ;;
        esac
    else
        echo "  3. Start: sudo systemctl start reticulumpi"
        echo "  4. Logs:  journalctl -u reticulumpi -f"
    fi
fi

if [ "$WITH_NOMADNET" = true ]; then
    echo ""
    echo "NomadNet is installed and configured. To customize:"
    echo "  Edit pages in /home/$SERVICE_USER/.nomadnet/storage/pages/"
    echo "  Start services: sudo systemctl start rnsd reticulumpi"
else
    echo ""
    echo "Optional — for NomadNet page serving:"
    echo "  Re-run with: sudo bash $0 --with-nomadnet"
fi

echo ""
echo "Optional — for I2P anonymous networking support:"
echo "  sudo apt install i2pd"
echo "  sudo systemctl enable --now i2pd"
echo "  Then uncomment the [I2P Interface] section in $RETICULUM_DIR/config"
