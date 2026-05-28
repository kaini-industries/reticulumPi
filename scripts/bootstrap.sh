#!/usr/bin/env bash
set -euo pipefail

# ReticulumPi Bootstrap Script
# Target: Fresh Raspberry Pi 5 running 64-bit Raspberry Pi OS (Bookworm+)

AUTO_START=false
WITH_NOMADNET=false
WITH_MESHCHAT=false
WITH_DASHBOARD=false
WITH_LORA=false
WITH_I2P=false
WITH_YGGDRASIL=false
WITH_SIGNALS=false
INSTALL_DIR="/opt/reticulumpi"
NODE_NAME=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --start) AUTO_START=true; shift ;;
        --with-nomadnet) WITH_NOMADNET=true; shift ;;
        --with-meshchat) WITH_MESHCHAT=true; shift ;;
        --with-dashboard) WITH_DASHBOARD=true; shift ;;
        --with-lora) WITH_LORA=true; shift ;;
        --with-i2p) WITH_I2P=true; shift ;;
        --with-yggdrasil) WITH_YGGDRASIL=true; shift ;;
        --with-signals) WITH_SIGNALS=true; shift ;;
        --install-dir) INSTALL_DIR="${2:?--install-dir requires a value}"; shift 2 ;;
        --install-dir=*) INSTALL_DIR="${1#*=}"; shift ;;
        --node-name) NODE_NAME="${2:?--node-name requires a value}"; shift 2 ;;
        --node-name=*) NODE_NAME="${1#*=}"; shift ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done
CONFIG_DIR="/etc/reticulumpi"
DATA_DIR="/var/lib/reticulumpi"
SERVICE_USER="reticulumpi"

echo "=== ReticulumPi Bootstrap ==="

# 1. System packages
echo "[1/7] Installing system packages..."
sudo apt-get update
PACKAGES="python3 python3-venv python3-pip git avahi-daemon avahi-utils"
if [ "$WITH_MESHCHAT" = true ]; then
    PACKAGES="$PACKAGES nodejs npm"
fi
sudo apt-get install -y $PACKAGES

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
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
INSTALL_DIR="$(realpath "$INSTALL_DIR")"

if [ ! -f "$PROJECT_DIR/pyproject.toml" ]; then
    echo "Error: Run this script from within the reticulumPi project directory."
    exit 1
fi

if [ "$INSTALL_DIR" = "$(realpath "$PROJECT_DIR")" ]; then
    # In-place install: use the repo directory directly, skip copy
    echo "  Installing in-place at $INSTALL_DIR (skipping copy)"
    sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
elif [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" pull
    sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
else
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

# 4c. Optional: Install MeshChat
if [ "$WITH_MESHCHAT" = true ]; then
    echo "[4c/7] Installing MeshChat..."
    MESHCHAT_DIR="$INSTALL_DIR/meshchat"
    if [ -d "$MESHCHAT_DIR/.git" ]; then
        git -C "$MESHCHAT_DIR" pull
    else
        sudo -u "$SERVICE_USER" git clone https://github.com/liamcottle/reticulum-meshchat "$MESHCHAT_DIR"
    fi
    sudo -u "$SERVICE_USER" python3 -m venv "$MESHCHAT_DIR/.venv"
    sudo -u "$SERVICE_USER" "$MESHCHAT_DIR/.venv/bin/pip" install --upgrade pip
    sudo -u "$SERVICE_USER" "$MESHCHAT_DIR/.venv/bin/pip" install -r "$MESHCHAT_DIR/requirements.txt"
    sudo -u "$SERVICE_USER" mkdir -p "$MESHCHAT_DIR/storage"

    # Build the frontend (required — MeshChat serves from public/)
    if [ ! -d "$MESHCHAT_DIR/public" ]; then
        echo "  Building MeshChat frontend..."
        cd "$MESHCHAT_DIR"
        sudo -u "$SERVICE_USER" npm install --omit=dev
        sudo -u "$SERVICE_USER" npm run build-frontend
        cd - >/dev/null
    fi
fi

# 4d. Optional: Install web dashboard dependencies
if [ "$WITH_DASHBOARD" = true ]; then
    echo "[4d/7] Installing web dashboard dependencies..."
    sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install "aiohttp>=3.9,<4.0"
fi

# 4e. Optional: Install LoRa/RNode tools
if [ "$WITH_LORA" = true ]; then
    echo "[4e/7] Installing LoRa/RNode tools (rnodeconf)..."
    sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install rnodeconf
fi

# 4f. Optional: Install I2P transport
if [ "$WITH_I2P" = true ]; then
    echo "[4f/7] Installing I2P router (i2pd)..."
    sudo apt-get install -y i2pd

    # Ensure SAM API is explicitly enabled (required by RNS I2PInterface)
    if grep -q '^\[sam\]' /etc/i2pd/i2pd.conf 2>/dev/null; then
        sudo sed -i '/^\[sam\]/,/^$/{
s/^# enabled = false/enabled = true/
s/^# address = 127.0.0.1/address = 127.0.0.1/
s/^# port = 7656/port = 7656/
}' /etc/i2pd/i2pd.conf
        echo "  Enabled SAM API in /etc/i2pd/i2pd.conf"
    fi

    sudo systemctl enable --now i2pd
    # Verify SAM API is available
    sleep 3
    if nc -z 127.0.0.1 7656 2>/dev/null; then
        echo "  I2P SAM API is available on port 7656"
    else
        echo "  Warning: I2P SAM API not yet available. It may take a moment to start."
    fi
fi

# 4g. Optional: Install Yggdrasil encrypted IPv6 overlay
if [ "$WITH_YGGDRASIL" = true ]; then
    echo "[4g/7] Installing Yggdrasil encrypted IPv6 overlay..."

    # Add Yggdrasil repo key and source
    sudo mkdir -p /usr/local/apt-keys
    gpg --fetch-keys https://neilalexander.s3.dualstack.eu-west-2.amazonaws.com/deb/key.txt 2>/dev/null
    gpg --export 1C5162E133015D81A811239D1840CDAC6011C5EA | \
        sudo tee /usr/local/apt-keys/yggdrasil-keyring.gpg > /dev/null
    echo 'deb [signed-by=/usr/local/apt-keys/yggdrasil-keyring.gpg] http://neilalexander.s3.dualstack.eu-west-2.amazonaws.com/deb/ debian yggdrasil' | \
        sudo tee /etc/apt/sources.list.d/yggdrasil.list > /dev/null
    sudo apt-get update
    sudo apt-get install -y yggdrasil

    # Generate config if it doesn't exist
    if [ ! -f /etc/yggdrasil.conf ]; then
        sudo yggdrasil -genconf | sudo tee /etc/yggdrasil.conf > /dev/null
        echo "  Generated /etc/yggdrasil.conf"
    fi

    # Allow reticulumpi user to read Yggdrasil admin socket
    # by adding the user to the yggdrasil group (if the group exists)
    if getent group yggdrasil >/dev/null 2>&1; then
        sudo usermod -aG yggdrasil "$SERVICE_USER"
        echo "  Added $SERVICE_USER to yggdrasil group"
    fi

    sudo systemctl enable --now yggdrasil
    sleep 2

    # Show the node's Yggdrasil address
    if command -v yggdrasilctl &>/dev/null; then
        YGG_ADDR=$(yggdrasilctl getself 2>/dev/null | grep -oP 'address: \K[0-9a-f:]+' || echo "pending")
        echo "  Yggdrasil address: $YGG_ADDR"
    else
        echo "  Yggdrasil installed (address will be assigned on start)"
    fi
fi

# 4h. Optional: Install signal decoder tools
if [ "$WITH_SIGNALS" = true ]; then
    echo "[4h/7] Installing signal decoder tools..."

    # Build tools + decoder packages
    sudo apt-get install -y cmake build-essential multimon-ng sox librtlsdr-dev libusb-1.0-0-dev

    # AIS-catcher (AIS vessel tracking) — build from source for ARM64
    if ! command -v AIS-catcher &>/dev/null; then
        echo "  Building AIS-catcher from source..."
        BUILD_DIR=$(mktemp -d)
        trap 'rm -rf "$BUILD_DIR"' EXIT
        git clone --depth 1 https://github.com/jvde-github/AIS-catcher.git "$BUILD_DIR/AIS-catcher"
        (
            cd "$BUILD_DIR/AIS-catcher"
            mkdir build && cd build
            cmake .. -DCMAKE_BUILD_TYPE=Release
            make -j"$(nproc)"
            sudo make install
        )
        rm -rf "$BUILD_DIR"
        trap - EXIT
        echo "  AIS-catcher installed."
    else
        echo "  AIS-catcher already installed."
    fi

    # acarsdec (ACARS aircraft message decoding) — build from source for ARM64
    if ! command -v acarsdec &>/dev/null; then
        echo "  Building acarsdec from source..."
        sudo apt-get install -y libacars-dev
        BUILD_DIR=$(mktemp -d)
        trap 'rm -rf "$BUILD_DIR"' EXIT
        git clone --depth 1 https://github.com/TLeconte/acarsdec.git "$BUILD_DIR/acarsdec"
        (
            cd "$BUILD_DIR/acarsdec"
            mkdir build && cd build
            cmake .. -Drtl=ON -DCMAKE_BUILD_TYPE=Release
            make -j"$(nproc)"
            sudo make install
        )
        rm -rf "$BUILD_DIR"
        trap - EXIT
        echo "  acarsdec installed."
    else
        echo "  acarsdec already installed."
    fi

    # noaa-apt (NOAA APT satellite image decoder)
    if ! command -v noaa-apt &>/dev/null; then
        echo "  noaa-apt must be installed manually for ARM64."
        echo "  See: https://noaa-apt.mbernardi.com.ar/download.html"
    else
        echo "  noaa-apt already installed."
    fi

    # rs41mod (radiosonde decoder) from radiosonde_auto_rx
    if ! command -v rs41mod &>/dev/null; then
        echo "  rs41mod must be built from radiosonde_auto_rx source."
        echo "  See: https://github.com/projecthorus/radiosonde_auto_rx"
    else
        echo "  rs41mod already installed."
    fi

    echo "  Signal decoder tools setup complete."
fi

# 5. Config directories
echo "[5/7] Setting up configuration..."
sudo mkdir -p "$CONFIG_DIR" "$DATA_DIR"
sudo chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR" "$DATA_DIR"
if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
    sudo cp "$INSTALL_DIR/config/reticulumpi/config.example.yaml" "$CONFIG_DIR/config.yaml"
    echo "  Created $CONFIG_DIR/config.yaml from example. Edit as needed."
fi

# Set node name — prompt interactively, use flag, or default to hostname
if ! grep -q '^\s*node_name:' "$CONFIG_DIR/config.yaml"; then
    if [ -z "$NODE_NAME" ] && [ -t 0 ]; then
        DEFAULT_NAME="ReticulumPi-$(hostname)"
        read -rp "Node name [$DEFAULT_NAME]: " NODE_NAME
        NODE_NAME="${NODE_NAME:-$DEFAULT_NAME}"
    fi
    if [ -n "$NODE_NAME" ]; then
        # Escape sed special characters in the node name (& \ |)
        ESCAPED_NAME=$(printf '%s\n' "$NODE_NAME" | sed 's/[&\\/|]/\\&/g')
        sudo sed -i "s|^  #node_name:.*|  node_name: $ESCAPED_NAME|" "$CONFIG_DIR/config.yaml"
        echo "  Node name set to: $NODE_NAME"
    fi
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
    "/home/$SERVICE_USER/.nomadnet" \
    "/home/$SERVICE_USER/.nomadnet-tui"

# Also create MeshChat storage dir if installing (required by ReadWritePaths)
if [ "$WITH_MESHCHAT" = true ]; then
    sudo -u "$SERVICE_USER" mkdir -p "$INSTALL_DIR/meshchat/storage"
fi

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

# 6b. MeshChat config (if enabled)
if [ "$WITH_MESHCHAT" = true ]; then
    echo "[6b/7] Setting up MeshChat config..."

    # MeshChat also requires shared instance mode
    sudo sed -i 's/^  use_shared_instance: false$/  use_shared_instance: true/' "$CONFIG_DIR/config.yaml"
    echo "  Set use_shared_instance: true in $CONFIG_DIR/config.yaml"

    # Enable the meshchat_server plugin: uncomment if present, otherwise append
    if grep -q '#meshchat_server:' "$CONFIG_DIR/config.yaml"; then
        sudo sed -i '/^    #meshchat_server:$/,/^    #  max_restarts: 5/{
s/^    #meshchat_server:/    meshchat_server:/
s/^    #  enabled: false/      enabled: true/
s/^    #  install_dir:/      install_dir:/
s/^    #  host:/      host:/
s/^    #  port:/      port:/
s/^    #  storage_dir:/      storage_dir:/
s/^    #  health_check_interval:/      health_check_interval:/
s/^    #  auto_restart:/      auto_restart:/
s/^    #  max_restarts:/      max_restarts:/
}' "$CONFIG_DIR/config.yaml"
    elif ! grep -q 'meshchat_server:' "$CONFIG_DIR/config.yaml"; then
        cat >> "$CONFIG_DIR/config.yaml" <<MESHCHAT

    meshchat_server:
      enabled: true
      install_dir: $INSTALL_DIR/meshchat
      host: "0.0.0.0"
      port: 8000
      storage_dir: $INSTALL_DIR/meshchat/storage
      health_check_interval: 10
      auto_restart: true
      max_restarts: 5
MESHCHAT
    fi
    echo "  Enabled meshchat_server plugin in $CONFIG_DIR/config.yaml"
fi

# 6d. Web Dashboard config (if enabled)
if [ "$WITH_DASHBOARD" = true ]; then
    echo "[6d/7] Setting up web dashboard..."

    # Enable the web_dashboard plugin in config (no password needed — auto-generated on first run)
    if grep -q '#web_dashboard:' "$CONFIG_DIR/config.yaml"; then
        sudo sed -i '/^    #web_dashboard:$/,/^    #    cert_dir:/{
s/^    #web_dashboard:/    web_dashboard:/
s/^    #  enabled: false/      enabled: true/
s/^    #  host:/      host:/
s/^    #  port:/      port:/
s/^    #  session_timeout:/      session_timeout:/
s/^    #  max_sessions:/      max_sessions:/
s/^    #  metrics_interval:/      metrics_interval:/
s/^    #  max_websocket_clients:/      max_websocket_clients:/
}' "$CONFIG_DIR/config.yaml"
    elif ! grep -q 'web_dashboard:' "$CONFIG_DIR/config.yaml"; then
        cat >> "$CONFIG_DIR/config.yaml" <<DASHBOARD

    web_dashboard:
      enabled: true
      host: "127.0.0.1"
      port: 8080
DASHBOARD
    fi
    echo "  Enabled web_dashboard plugin in $CONFIG_DIR/config.yaml"
fi

# 7. Install systemd services (template paths to match INSTALL_DIR)
echo "[7/7] Installing systemd services..."
ESCAPED_DIR=$(printf '%s\n' "$INSTALL_DIR" | sed 's/[&|\\]/\\&/g')
sed "s|/opt/reticulumpi|$ESCAPED_DIR|g" "$INSTALL_DIR/systemd/reticulumpi.service" \
    | sudo tee /etc/systemd/system/reticulumpi.service >/dev/null
if [ "$WITH_NOMADNET" = true ] || [ "$WITH_MESHCHAT" = true ]; then
    sed "s|/opt/reticulumpi|$ESCAPED_DIR|g" "$INSTALL_DIR/systemd/rnsd.service" \
        | sudo tee /etc/systemd/system/rnsd.service >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable rnsd.service
    sudo systemctl enable reticulumpi.service
    echo "  Installed rnsd.service (required for shared instance mode)"
else
    sudo systemctl daemon-reload
    sudo systemctl enable reticulumpi.service
fi

# 7b. Sudoers rule for dashboard service restart
echo "  Installing sudoers rule for dashboard service restart..."
sudo install -m 0440 "$INSTALL_DIR/config/sudoers.d/reticulumpi-services" /etc/sudoers.d/reticulumpi-services
if sudo visudo -cf /etc/sudoers.d/reticulumpi-services >/dev/null 2>&1; then
    echo "  Sudoers rule installed and validated"
else
    echo "  WARNING: sudoers syntax check failed — removing to prevent lockout"
    sudo rm -f /etc/sudoers.d/reticulumpi-services
fi

# 7c. Sudoers rule for offline simulation script
echo "  Installing sudoers rule for offline simulation..."
sudo install -m 0440 "$INSTALL_DIR/config/sudoers.d/reticulumpi-offline" /etc/sudoers.d/reticulumpi-offline
if sudo visudo -cf /etc/sudoers.d/reticulumpi-offline >/dev/null 2>&1; then
    echo "  Sudoers rule installed and validated"
else
    echo "  WARNING: sudoers syntax check failed — removing to prevent lockout"
    sudo rm -f /etc/sudoers.d/reticulumpi-offline
fi

echo "  Installing sudoers rule for captive portal..."
sudo install -m 0440 "$INSTALL_DIR/config/sudoers.d/reticulumpi-captive-portal" /etc/sudoers.d/reticulumpi-captive-portal
if sudo visudo -cf /etc/sudoers.d/reticulumpi-captive-portal >/dev/null 2>&1; then
    echo "  Sudoers rule installed and validated"
else
    echo "  WARNING: sudoers syntax check failed — removing to prevent lockout"
    sudo rm -f /etc/sudoers.d/reticulumpi-captive-portal
fi

echo ""
echo "=== Bootstrap complete ==="
echo ""

# Detect if services were already running before bootstrap
RNSD_WAS_RUNNING=false
RETICULUMPI_WAS_RUNNING=false
systemctl is-active --quiet rnsd 2>/dev/null && RNSD_WAS_RUNNING=true
systemctl is-active --quiet reticulumpi 2>/dev/null && RETICULUMPI_WAS_RUNNING=true

if [ "$RETICULUMPI_WAS_RUNNING" = true ]; then
    echo "Restarting services (were already running)..."
    if [ "$RNSD_WAS_RUNNING" = true ]; then
        sudo systemctl restart rnsd
    fi
    sudo systemctl restart reticulumpi
    echo "Services restarted with new configuration."
    echo "Check logs with: journalctl -u reticulumpi -f"
elif [ "$AUTO_START" = true ]; then
    echo "Starting ReticulumPi service..."
    if systemctl is-enabled --quiet rnsd 2>/dev/null; then
        sudo systemctl start rnsd
    fi
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
                if systemctl is-enabled --quiet rnsd 2>/dev/null; then
                    sudo systemctl start rnsd
                fi
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

if [ "$WITH_MESHCHAT" = true ]; then
    echo ""
    echo "MeshChat is installed and configured."
    echo "  Web UI: http://$(hostname -I | awk '{print $1}'):8000"
    echo "  Start services: sudo systemctl start rnsd reticulumpi"
else
    echo ""
    echo "Optional — for MeshChat web messaging:"
    echo "  Re-run with: sudo bash $0 --with-meshchat"
fi

if [ "$WITH_DASHBOARD" = true ]; then
    echo ""
    echo "Web Dashboard is installed and configured."
    echo "  Dashboard: http://127.0.0.1:8080"
    echo "  Password is auto-generated on first start — check the logs:"
    echo "    journalctl -u reticulumpi | grep 'dashboard password'"
    echo "  To expose on the network, change host to 0.0.0.0 in $CONFIG_DIR/config.yaml"
    echo "  To reset password: delete /home/$SERVICE_USER/.config/reticulumpi/dashboard_secret"
else
    echo ""
    echo "Optional — for web dashboard monitoring:"
    echo "  Re-run with: sudo bash $0 --with-dashboard"
fi

if [ "$WITH_LORA" = true ]; then
    echo ""
    echo "LoRa/RNode tools installed. To flash a device:"
    echo "  sudo -u $SERVICE_USER $INSTALL_DIR/.venv/bin/rnodeconf --autoinstall"
else
    echo ""
    echo "Optional — for LoRa/RNode support:"
    echo "  Re-run with: sudo bash $0 --with-lora"
fi

if [ "$WITH_I2P" = true ]; then
    echo ""
    echo "I2P transport installed. The I2P interface will bootstrap automatically"
    echo "on next rnsd start (takes 2-5 minutes for initial tunnel setup)."
else
    echo ""
    echo "Optional — for I2P anonymous networking support:"
    echo "  Re-run with: sudo bash $0 --with-i2p"
fi

if [ "$WITH_YGGDRASIL" = true ]; then
    echo ""
    echo "Yggdrasil installed. Enable the yggdrasil_transport plugin in"
    echo "config.yaml and set auto_configure_rns: true to auto-add a"
    echo "Reticulum TCP interface on your Yggdrasil IPv6 address."
else
    echo ""
    echo "Optional — for Yggdrasil encrypted IPv6 overlay:"
    echo "  Re-run with: sudo bash $0 --with-yggdrasil"
fi
