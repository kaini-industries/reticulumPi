# ReticulumPi

An extensible [Reticulum](https://reticulum.network/) network node for Raspberry Pi.

ReticulumPi wraps the Reticulum cryptographic networking stack in a plugin-based architecture so you can add custom features without forking Reticulum itself. Upstream updates merge cleanly via `pip install --upgrade rns`.

## Features

- **Plugin system** -- add capabilities by dropping Python files into a directory
- **Four built-in plugins** -- heartbeat announce, LXMF message echo, system metrics, NomadNet page server
- **Persistent identity** -- stable cryptographic identity across restarts
- **Shared or standalone mode** -- coexists with `rnsd` or runs interfaces directly
- **Deployment automation** -- bootstrap script, systemd service, Docker support
- **No Reticulum fork** -- installs `rns` as a pip dependency, always upgradeable

## Requirements

- Python 3.9+
- Raspberry Pi 5 (or any Linux/macOS system) running 64-bit OS
- Optional: LoRa radio hardware for long-range mesh (see [LoRa Radio with RNode](#lora-radio-with-rnode) -- boards from ~$15)

## Quick Start (Development)

```bash
git clone https://github.com/kaini-industries/reticulumPi.git
cd reticulumPi
make dev            # creates venv + installs in editable mode with dev deps
make test           # runs the test suite
make lint           # runs ruff linter
make format         # auto-format code with ruff
```

Run locally:

```bash
.venv/bin/reticulumpi --config config/reticulumpi/config.example.yaml
```

## Raspberry Pi Deployment

### Automated Setup

The bootstrap script handles everything on a fresh Raspberry Pi 5 running Raspberry Pi OS (Bookworm+):

```bash
# From the cloned repo on your Pi:
sudo bash scripts/bootstrap.sh

# With NomadNet page server support:
sudo bash scripts/bootstrap.sh --with-nomadnet
```

This will:

1. Install system packages (`python3`, `python3-venv`, `git`)
2. Create a `reticulumpi` system user with hardware access groups (`dialout`, `gpio`, `spi`, `i2c`)
3. Copy the project to `/opt/reticulumpi`
4. Create a Python venv and install dependencies (+ NomadNet if `--with-nomadnet`)
5. Set up config directories at `/etc/reticulumpi/` and `/home/reticulumpi/.reticulum/`
6. Set up NomadNet directories and example pages (if `--with-nomadnet`)
7. Install and enable systemd services (`reticulumpi` + `rnsd` if NomadNet enabled)

After bootstrap, configure and start:

```bash
# Edit the app config (plugin settings, identity path, etc.)
sudo nano /etc/reticulumpi/config.yaml

# Edit the Reticulum config (network interfaces)
sudo nano /home/reticulumpi/.reticulum/config

# Start the service
sudo systemctl start reticulumpi

# Watch the logs
journalctl -u reticulumpi -f
```

### Manual Setup

```bash
# Install on the Pi
python3 -m venv /opt/reticulumpi/.venv
/opt/reticulumpi/.venv/bin/pip install .

# Copy example configs
mkdir -p ~/.config/reticulumpi
cp config/reticulumpi/config.example.yaml ~/.config/reticulumpi/config.yaml
cp config/reticulum/config.example ~/.reticulum/config

# Run
reticulumpi --config ~/.config/reticulumpi/config.yaml

# Validate config without starting
reticulumpi --check --config ~/.config/reticulumpi/config.yaml

# List available plugins
reticulumpi --list-plugins
```

### Updating

Pull the latest code and upgrade dependencies:

```bash
sudo bash scripts/update.sh
```

This pulls the repo, upgrades all dependencies, reinstalls the project, and restarts the service.

## Docker

Docker is the easiest way to run ReticulumPi without installing anything on the host. The container runs on ARM64 natively (Apple Silicon, Raspberry Pi) and on x86 via QEMU emulation.

### Quick Start

```bash
cd docker

# Copy and edit config
mkdir -p config
cp ../config/reticulumpi/config.example.yaml config/config.yaml

# Build and run
docker compose up --build -d
```

### Common Operations

```bash
# View live logs
docker compose logs -f

# Check container health and status
docker compose ps

# Restart after config changes
docker compose restart

# Rebuild after code changes
docker compose up --build -d

# Stop the node
docker compose down

# Stop and remove all data (identity, LXMF storage)
docker compose down -v
```

### Configuration

The container mounts `docker/config/` as `/config`. Edit your config there:

```bash
# Edit the reticulumPi app config
nano docker/config/config.yaml
```

The Reticulum config (`~/.reticulum/config`) lives inside the container's home directory and is persisted in the `reticulumpi-data` volume. To customize it, you can copy one in before starting:

```bash
# Optional: provide a custom Reticulum config
docker compose run --rm reticulumpi sh -c \
  "cp /dev/stdin ~/.reticulum/config" < ../config/reticulum/config.minimal
```

Or exec into a running container:

```bash
docker exec -it docker-reticulumpi-1 sh
```

### Networking

Host networking is enabled by default (`network_mode: host`), which is required for Reticulum's AutoInterface (IPv6 multicast discovery), UDP, and TCP interfaces. This means the container shares your host's network stack — no port mapping needed.

### Serial Devices (LoRa, RNode)

To pass through a USB serial device, uncomment the `devices` section in `docker/docker-compose.yml`:

```yaml
devices:
  - /dev/ttyUSB0:/dev/ttyUSB0
```

On **macOS with Docker Desktop**, USB serial passthrough is not supported. Use a native install or a Linux VM for LoRa hardware.

### Viewing the Network

You can run Reticulum tools inside the container:

```bash
# Show interfaces and network status
docker exec docker-reticulumpi-1 rnstatus

# List available plugins
docker exec docker-reticulumpi-1 reticulumpi --list-plugins

# Validate config
docker exec docker-reticulumpi-1 reticulumpi --check --config /config/config.yaml
```

### Custom Plugins

To load custom plugins into the container, add a volume mount in `docker-compose.yml`:

```yaml
volumes:
  - ./config:/config
  - ./my_plugins:/plugins
  - reticulumpi-data:/data
```

Then add the path to your `config.yaml`:

```yaml
plugin_paths:
  - /plugins
```

### NomadNet in Docker

The Docker image includes NomadNet. The container entrypoint automatically starts `rnsd` in the background, enabling shared instance mode for both reticulumPi and NomadNet.

To enable the NomadNet page server, edit your `docker/config/config.yaml`:

```yaml
reticulumpi:
  use_shared_instance: true

  plugins:
    nomadnet_server:
      enabled: true
```

NomadNet data (identity, pages, files) is persisted in the `nomadnet-data` volume. Edit pages by exec-ing into the container:

```bash
docker exec -it docker-reticulumpi-1 sh
vi ~/.nomadnet/storage/pages/index.mu
```

### Testing in Docker

Run the full test suite inside a container to verify the installed package works correctly:

```bash
make docker-test          # test on your host architecture
make docker-test-arm64    # test on ARM64 (Pi architecture, uses QEMU on x86)
```

This builds the project as a wheel, installs it into a clean Debian Bookworm container, and runs all 71 tests.

## Configuration

ReticulumPi uses two separate config files:

### App Config (`config.yaml`)

Controls the application, plugins, and identity. Default location: `~/.config/reticulumpi/config.yaml`

```yaml
reticulumpi:
  # Connect to running rnsd (true) or open interfaces directly (false)
  # Use false for a dedicated node; use true if also running Sideband, NomadNet, etc.
  use_shared_instance: false

  # Persistent cryptographic identity file (created automatically)
  identity_path: ~/.config/reticulumpi/identity

  # Reticulum log level: 0=critical ... 4=info ... 7=extreme
  log_level: 4

  # Additional directories to scan for plugins
  plugin_paths:
    - /home/pi/my_plugins

  # Plugin settings (only enabled plugins are loaded)
  plugins:
    heartbeat_announce:
      enabled: true
      interval_seconds: 300
      include_telemetry: true

    message_echo:
      enabled: true
      display_name: "My Pi Node"

    system_monitor:
      enabled: true
      collect_interval_seconds: 60
      metrics:
        - cpu_percent
        - cpu_temp
        - memory_percent
        - disk_percent
```

### Reticulum Config (`~/.reticulum/config`)

Standard Reticulum configuration. ReticulumPi does not modify this file. See the [Reticulum manual](https://reticulum.network/manual/interfaces.html) for full documentation.

The included example enables AutoInterface and TCP Server by default. It also contains documented, commented-out blocks for every supported interface type: TCP Client, RNode LoRa, RNode Multi, Serial, KISS TNC, AX.25 KISS, UDP, I2P, Pipe, and Backbone. See the [Connectivity Guide](#connectivity-guide) below for details on each.

## Connectivity Guide

Reticulum can communicate over virtually any medium. The Raspberry Pi supports all of these connection methods, and you can enable multiple interfaces simultaneously -- Reticulum automatically meshes traffic across all of them.

### At a Glance

| Connection Method | Hardware Needed | Cost | Range | Best For |
|---|---|---|---|---|
| WiFi/Ethernet (Auto) | Built-in | Free | LAN | Local mesh, getting started |
| TCP Client/Server | Internet connection | Free | Global | Internet gateway, remote nodes |
| RNode LoRa | USB LoRa board (from ~$15) | $15--150 | 1--100+ km | Long-range off-grid mesh |
| RNode Multi | RNode (firmware v1.74+) | $15--150 | Multi-channel | Simultaneous frequencies |
| Serial | USB-serial adapter + radio | $5--50 | Varies | Data radios, laser links, direct wiring |
| KISS TNC | Radio + TNC or sound card | $35--500 | 10--50 km | Amateur radio (VHF/UHF) |
| AX.25 KISS | Same as KISS TNC | $35--500 | 10--50 km | Ham radio with FCC-compliant ID |
| UDP | Network interface | Free | LAN | Bridging VLANs, special topologies |
| I2P | i2pd software | Free | Global | Anonymous, censorship-resistant |
| Pipe | Custom program | Free | Varies | Experimental transports |
| Backbone | Linux TCP | Free | Global | High-throughput transport nodes |

### WiFi and Ethernet (AutoInterface)

Works immediately with no configuration. The Pi's built-in WiFi (`wlan0`) and Ethernet (`eth0`) are automatically discovered. Peers on the same LAN find each other via IPv6 link-local multicast.

This is enabled by default in the example config. For most local setups, this is all you need.

### TCP Client and Server

Connect to remote Reticulum nodes anywhere on the Internet. The **TCP Server** listens for incoming connections (open port 4242 on your router). The **TCP Client** connects outbound to an existing Reticulum transport hub or another Pi node.

Combine with other interfaces to create an Internet gateway -- for example, a Pi with both an RNode radio and a TCP Server bridges local LoRa traffic to the wider Internet-connected Reticulum network.

### LoRa Radio with RNode

[RNode](https://unsigned.io/rnode/) is an open-source LoRa transceiver designed specifically for Reticulum. It uses raw LoRa modulation (not LoRaWAN) and delivers long-range, low-power wireless mesh connectivity. The Raspberry Pi has no built-in LoRa hardware -- you need an external radio transceiver connected via USB.

#### What Hardware to Buy

The cheapest path to LoRa on a Pi is a **LilyGO T-Beam** (~$25) or **Heltec LoRa32 v3** (~$15). These are ESP32-based development boards with LoRa radios built in. You flash them with RNode firmware and plug them into the Pi's USB port -- that's it.

**Recommended boards (sorted by value):**

| Board | Price | Frequency | Notes |
|-------|-------|-----------|-------|
| **LilyGO T3-S3** | ~$15 | 868/915 MHz | Cheapest option, compact, no GPS |
| **Heltec LoRa32 v3** | ~$15 | 868/915 MHz | Built-in OLED display, compact |
| **LilyGO T-Beam v1.1** | ~$25 | 868/915 MHz | GPS, 18650 battery holder, most popular choice |
| **LilyGO T-Beam Supreme** | ~$30 | 868/915 MHz | Upgraded T-Beam with better GPS and SX1262 radio |
| **LilyGO T-Deck** | ~$45 | 868/915 MHz | Built-in keyboard and screen |
| **RAK4631 (WisBlock)** | ~$30--50 | 868/915 MHz | Modular industrial system, very reliable |
| **Heltec LoRa32 v2** | ~$20 | 868/915 MHz | Older but widely available |
| **Unsigned RNode v2.x** | ~$100--150 | 868/915 MHz | Purpose-built for Reticulum, premium build quality |

> **Best starter pick:** LilyGO T-Beam v1.1 (~$25). It has GPS (useful for location-aware plugins), a battery holder for portable use, and excellent community support. Pair it with a 915 MHz antenna (or 868 MHz in EU) for dramatically better range than the stock stubby antenna.

**You also need:**
- A **USB-A to USB-C cable** (most boards use USB-C; some older ones use Micro-USB)
- An **antenna** matched to your frequency band. The stock stub antenna works but a 1/4 wave whip (~$5) or a directional Yagi (~$20) vastly improves range

#### Frequency Bands by Region

| Region | Frequency | ISM Band |
|--------|-----------|----------|
| Americas (US, Canada, South America) | 915 MHz | ISM 902--928 MHz |
| Europe, Africa, Middle East | 868 MHz | ISM 863--870 MHz |
| Asia (varies by country) | 433 MHz or 868 MHz | Check local regulations |
| Worldwide (short range) | 2.4 GHz | ISM 2.4 GHz |

No amateur radio license is required for LoRa on ISM bands at legal power levels.

#### Flashing RNode Firmware

The boards listed above ship with stock firmware -- you need to flash them with RNode firmware before they work with Reticulum. Do this from the Pi itself:

```bash
# Install the RNode configuration tool
.venv/bin/pip install rnodeconf

# Auto-detect the connected board and flash firmware
.venv/bin/rnodeconf --autoinstall
```

The `--autoinstall` command will:
1. Detect the board type on the USB port
2. Download the correct firmware
3. Flash it to the board
4. Configure default radio parameters

After flashing, the device shows up as `/dev/ttyUSB0` or `/dev/ttyACM0`.

#### Connecting to the Pi

1. Plug the flashed RNode into any USB port on the Pi
2. Verify it appears:
   ```bash
   ls /dev/ttyUSB* /dev/ttyACM*
   ```
3. If using the bootstrap install, the `reticulumpi` user already has `dialout` group access for serial devices. For manual installs:
   ```bash
   sudo usermod -aG dialout $USER
   # Log out and back in for the group change to take effect
   ```

#### Reticulum Configuration

Uncomment and edit the `[RNode LoRa Interface]` section in your Reticulum config (`~/.reticulum/config` or `/home/reticulumpi/.reticulum/config`):

```ini
[RNode LoRa Interface]
  type = RNodeInterface
  enabled = yes
  port = /dev/ttyUSB0
  frequency = 915000000
  bandwidth = 125000
  txpower = 7
  spreadingfactor = 8
  codingrate = 5
```

**Key parameters:**

| Parameter | Description | Typical Values |
|-----------|-------------|----------------|
| `port` | Serial port where the RNode appears | `/dev/ttyUSB0`, `/dev/ttyACM0` |
| `frequency` | Center frequency in Hz | `915000000`, `868000000`, `433000000` |
| `bandwidth` | LoRa bandwidth in Hz | `125000` (standard), `250000` (faster), `62500` (longer range) |
| `txpower` | Transmit power in dBm | `2`--`17` (check local regulations) |
| `spreadingfactor` | LoRa spreading factor | `7` (fastest) to `12` (longest range) |
| `codingrate` | Forward error correction rate | `5` (4/5), `6` (4/6), `7` (4/7), `8` (4/8) |

**Tuning for range vs. speed:**
- **Maximum range:** `spreadingfactor = 12`, `bandwidth = 62500`, `codingrate = 8` -- very slow (~0.3 kbps) but reaches the farthest
- **Balanced (default):** `spreadingfactor = 8`, `bandwidth = 125000`, `codingrate = 5` -- good range with reasonable throughput (~2.5 kbps)
- **Maximum speed:** `spreadingfactor = 7`, `bandwidth = 500000`, `codingrate = 5` -- shortest range but highest throughput (~11 kbps)

#### Range Expectations

| Environment | Antenna | Typical Range |
|-------------|---------|---------------|
| Urban, stock stub antenna | Included | 0.5--2 km |
| Urban, 1/4 wave whip | ~$5 | 2--5 km |
| Suburban/rural, whip antenna | ~$5 | 5--20 km |
| Hilltop/tower, directional Yagi | ~$20 | 20--100+ km |
| Line-of-sight, both ends elevated | Yagi | 100+ km documented |

A real-world test achieved a **15.75 km usable SSH link at 2.6 kbps** using standard RNode hardware.

#### Multiple RNodes and Multi-Channel

You can connect **multiple RNode devices** to a single Pi (one per USB port), each on a different frequency. Reticulum will mesh traffic across all of them.

Alternatively, boards with RNode firmware **v1.74 or later** support **multi-channel mode** -- a single device operates on multiple frequencies simultaneously:

```ini
[RNode Multi Interface]
  type = RNodeMultiInterface
  enabled = yes
  port = /dev/ttyUSB0

  [RNode Multi Interface/Channel 1]
    frequency = 915000000
    bandwidth = 125000
    txpower = 7
    spreadingfactor = 8
    codingrate = 5

  [RNode Multi Interface/Channel 2]
    frequency = 868000000
    bandwidth = 125000
    txpower = 7
    spreadingfactor = 8
    codingrate = 5
```

#### Troubleshooting LoRa

| Problem | Solution |
|---------|----------|
| `/dev/ttyUSB0` not appearing | Try a different USB cable (data cables only, not charge-only). Check `dmesg \| tail` for errors |
| Permission denied on serial port | Add your user to the `dialout` group: `sudo usermod -aG dialout $USER` and re-login |
| Very short range | Replace the stock stub antenna with a proper 1/4 wave or Yagi antenna. Ensure the antenna matches your frequency band |
| No peers discovered | Verify both nodes use the same frequency, bandwidth, spreading factor, and coding rate. All parameters must match exactly |
| `rnodeconf --autoinstall` fails | Try specifying the port manually: `rnodeconf --autoinstall /dev/ttyUSB0`. Ensure no other program is using the serial port |

### Serial Interface

Sends raw Reticulum packets over any serial connection. The Pi has a built-in UART on GPIO pins 14 (TX) and 15 (RX), accessible as `/dev/ttyAMA0`. Any USB-serial adapter shows up as `/dev/ttyUSB0`.

**Hardware you can connect:**

| Device | Price | Range | Connection |
|--------|-------|-------|------------|
| **HC-12 radio pair** | ~$5 each | ~1 km | Wire to Pi GPIO UART (3.3V logic) |
| **3DR/SiK radio pair** | ~$30--50 | ~1 km | USB, plug-and-play |
| **Direct wire pair** (Pi-to-Pi) | ~$2 | Same room | GPIO UART cross-wired (TX→RX, RX→TX) |
| **Laser data link** (DIY) | ~$20--50 | Line-of-sight | Serial output to transmitter |
| **Any serial data radio** | Varies | Varies | USB-serial adapter or GPIO UART |

**Pi UART setup:**
1. Enable serial: `sudo raspi-config` > Interface Options > Serial Port > Enable
2. The port appears as `/dev/ttyAMA0`
3. For HC-12 or similar 3.3V serial radios, wire directly to GPIO pins 8 (TX) and 10 (RX) plus ground

**The HC-12 pair (~$10 total) is the cheapest possible radio link** -- wire one to each Pi's UART and you have a ~1 km serial bridge with zero software complexity.

### KISS TNC (Packet Radio)

Connects to [KISS](https://en.wikipedia.org/wiki/KISS_(TNC))-compatible packet radio modems for amateur radio operation on VHF/UHF bands. Requires an **amateur radio license** to transmit.

**What you need:**

| Setup | Hardware | Total Cost | Notes |
|-------|----------|------------|-------|
| **Software TNC** | Pi + USB sound card (~$10) + VHF/UHF radio (~$25 for Baofeng UV-5R) + audio cable | ~$35 | Runs [Dire Wolf](https://github.com/wb2osz/direwolf) on the Pi itself as a software modem |
| **Hardware TNC** | [Mobilinkd TNC4](http://www.mobilinkd.com/) (~$130) + any VHF/UHF radio | ~$155+ | Plug-and-play USB/Bluetooth TNC |
| **Open-source TNC** | [OpenModem](https://unsigned.io/openmodem/) (~$100) + radio | ~$125+ | Open-source packet radio modem, USB |

**Dire Wolf software TNC setup (cheapest):**
1. Install Dire Wolf: `sudo apt install direwolf`
2. Connect a USB sound card to the Pi
3. Wire the sound card audio in/out to your radio's mic/speaker jack
4. Configure Dire Wolf to expose a KISS TCP port
5. Point Reticulum's `KISSInterface` at the Dire Wolf KISS port

**Range:** 10--50 km line-of-sight typical for VHF/UHF. Supports configurable preamble, TX tail, persistence, and slot time for CSMA channel access.

### AX.25 KISS Interface

Same hardware as KISS TNC above, but adds AX.25 protocol framing with mandatory station identification beaconing. **Use this instead of plain KISS when operating on amateur radio frequencies** -- FCC Part 97 (US) and equivalent regulations elsewhere require periodic callsign identification.

Adds some per-packet overhead compared to plain KISS. Only needed for regulatory compliance.

### UDP Interface

Broadcasts Reticulum packets over UDP. Useful for bridging VLANs or network segments where IPv6 multicast (AutoInterface) doesn't work. Not needed on most standard networks where AutoInterface is sufficient.

### I2P Interface (Anonymous Networking)

Connects to the [Invisible Internet Project (I2P)](https://geti2p.net/) for anonymous, censorship-resistant global connectivity. Traffic is routed through multiple encrypted hops so neither endpoint's IP address is exposed.

**Setup on Pi:**
1. Install the I2P router: `sudo apt install i2pd`
2. Start it: `sudo systemctl enable --now i2pd`
3. Uncomment the `[I2P Interface]` section in your Reticulum config
4. On first start, Reticulum generates a persistent I2P address (this can take several minutes)

No port forwarding or public IP address is required.

### Pipe Interface

Bridges Reticulum packets through any external program's stdin/stdout. This is the most flexible interface -- it can wrap netcat tunnels, SSH connections, custom hardware drivers, or any command-line tool that reads and writes binary data.

```
# Example: tunnel Reticulum over an SSH connection
command = ssh user@remote "cat"
```

### Backbone Interface

A high-performance TCP server interface that uses Linux `epoll` for efficient handling of many simultaneous connections. Best suited for dedicated transport nodes that relay traffic for the broader network. Functionally similar to TCP Server but optimized for throughput on Linux systems (the Pi qualifies).

### Combining Multiple Interfaces

A key strength of Reticulum is that you can enable many interfaces at once. For example, a single Pi could run:

- **AutoInterface** for local WiFi/Ethernet peers
- **RNode** for long-range LoRa mesh
- **TCP Server** for Internet-connected nodes
- **I2P** for anonymous global reach

Reticulum automatically routes and meshes traffic across all active interfaces. Enable `enable_transport = True` in your Reticulum config to let the Pi relay traffic between interfaces, turning it into a full transport node.

See `config/reticulum/config.example` for ready-to-use configuration blocks for every interface type. For a safe starting point with only local mesh discovery (no TCP server), use `config/reticulum/config.minimal` instead.

## Built-in Plugins

### Heartbeat Announce

Periodically announces the node's presence on the Reticulum network. Other nodes running `rnstatus` or transport-aware applications will see your node.

| Option | Default | Description |
|--------|---------|-------------|
| `interval_seconds` | 300 | Seconds between announcements |
| `app_name` | reticulumpi | Application name for the destination |
| `aspects` | [node, heartbeat] | Destination aspects |
| `include_telemetry` | false | Attach hostname, CPU%, memory% to announcement |

### Message Echo

Listens for incoming [LXMF](https://github.com/markqvist/lxmf) messages and replies with an echo. Useful for testing end-to-end connectivity.

| Option | Default | Description |
|--------|---------|-------------|
| `display_name` | ReticulumPi Echo | Name shown to message senders |
| `storage_path` | ~/.local/share/reticulumpi/lxmf | LXMF message storage directory |

Send a test message from another device using [Sideband](https://unsigned.io/sideband/) or `lxmf_send`.

### System Monitor

Collects system metrics on a timer. Other plugins can read metrics via `app.get_plugin("system_monitor").latest_metrics`.

| Option | Default | Description |
|--------|---------|-------------|
| `collect_interval_seconds` | 60 | Seconds between metric collections |
| `metrics` | all four | List of metrics to collect |

Available metrics: `cpu_percent`, `cpu_temp`, `memory_percent`, `disk_percent`

### NomadNet Page Server

Manages a [NomadNet](https://github.com/markqvist/NomadNet) daemon as a subprocess, serving pages and files over Reticulum. Other NomadNet users can connect to your node to browse content.

**Requires:** `pip install nomadnet` (or `make install-nomadnet`)

**Important:** NomadNet creates its own Reticulum instance, so both reticulumPi and NomadNet must connect to a shared `rnsd` daemon. Set `use_shared_instance: true` in your config when this plugin is enabled.

| Option | Default | Description |
|--------|---------|-------------|
| `config_dir` | ~/.nomadnet | NomadNet config and storage directory |
| `health_check_interval` | 30 | Seconds between process health checks |
| `auto_restart` | true | Restart NomadNet if it crashes |
| `max_restarts` | 5 | Maximum restart attempts before giving up |

Example config:

```yaml
reticulumpi:
  use_shared_instance: true  # Required for NomadNet

  plugins:
    nomadnet_server:
      enabled: true
      config_dir: ~/.nomadnet
```

Pages are served from `~/.nomadnet/storage/pages/` (micron markup `.mu` files). Files are served from `~/.nomadnet/storage/files/`. Example pages are installed automatically on first start.

## Writing Custom Plugins

Plugins are Python files that define a class inheriting from `PluginBase`. Drop your plugin file into any directory listed in `plugin_paths` in your config.

### Minimal Plugin

```python
# my_plugins/my_plugin.py
from reticulumpi.plugin_base import PluginBase

class MyPlugin(PluginBase):
    plugin_name = "my_plugin"
    plugin_version = "1.0.0"
    plugin_description = "Short description shown in --list-plugins"

    def start(self):
        self._active = True
        # Set up destinations, start threads, register handlers

    def stop(self):
        self._active = False
        # Clean up resources
```

Add your plugin directory and enable it in `config.yaml`:

```yaml
plugin_paths:
  - /home/pi/my_plugins

plugins:
  my_plugin:
    enabled: true
    my_custom_option: "value"
```

### Plugin API

Every plugin receives these through its constructor:

| Attribute | Type | Description |
|-----------|------|-------------|
| `self.app` | ReticulumPiApp | The application instance |
| `self.rns` | RNS.Reticulum | The Reticulum instance |
| `self.identity` | RNS.Identity | The node's persistent identity |
| `self.config` | dict | This plugin's config section from YAML |
| `self.log` | logging.Logger | Logger namespaced to `reticulumpi.plugin.<name>` |

#### Lifecycle

1. **Discovery** -- `PluginLoader` scans the built-in plugins directory and any `plugin_paths` for `.py` files containing `PluginBase` subclasses
2. **Instantiation** -- Only plugins with `enabled: true` in config are instantiated
3. **Start** -- `start()` is called on each enabled plugin
4. **Shutdown** -- `stop()` is called in reverse order on SIGTERM/SIGINT

#### Inter-Plugin Communication

Plugins can query other running plugins:

```python
monitor = self.app.get_plugin("system_monitor")
if monitor:
    metrics = monitor.latest_metrics
    cpu = metrics.get("cpu_percent", 0)
```

#### Optional Status Method

Override `get_status()` to expose monitoring data:

```python
def get_status(self):
    return {"active": self._active, "messages_handled": self._count}
```

### Example Scaffold

The file `plugins/example_plugin.py` (also at `src/reticulumpi/builtin_plugins/example_plugin.py`) is a fully working example you can copy and modify. It demonstrates all the key plugin features in one place:

- **Config validation** — `validate_config()` checks settings at construction time
- **Destination + announcing** — creates a Reticulum destination and announces periodically
- **Packet handling** — receives incoming data packets and sends proof acknowledgements
- **Background threads** — `_start_thread()` for daemon threads, `_sleep_while_active()` for interruptible sleep
- **Inter-plugin communication** — reads metrics from `system_monitor` via `self.app.get_plugin()`
- **Status reporting** — custom `get_status()` with packet count
- **Graceful shutdown** — `_join_threads()` in `stop()`

To use it as a starting point:

```bash
mkdir -p ~/my_plugins
cp plugins/example_plugin.py ~/my_plugins/my_plugin.py
```

Then add your plugin directory and enable it in `config.yaml`:

```yaml
plugin_paths:
  - ~/my_plugins

plugins:
  my_plugin:
    enabled: true
    app_name: reticulumpi
    aspect: myaspect
    announce_interval: 300
    display_name: "My Node"
```

Discover available plugins at any time with:

```bash
reticulumpi --list-plugins
```

## Project Structure

```
reticulumPi/
├── pyproject.toml                  # Dependencies and entry point
├── Makefile                        # install, dev, test, lint, format targets
├── LICENSE                         # MIT license
├── CHANGELOG.md                    # Version history
├── config/
│   ├── nomadnet/
│   │   └── pages/                  # Example NomadNet pages (.mu files)
│   ├── reticulum/
│   │   ├── config.example          # Reticulum interface config (all interfaces)
│   │   └── config.minimal          # Minimal safe config (AutoInterface only)
│   └── reticulumpi/
│       └── config.example.yaml     # App + plugin config
├── src/reticulumpi/
│   ├── __init__.py                 # Package version
│   ├── app.py                      # Core orchestrator
│   ├── cli.py                      # CLI entry point
│   ├── config.py                   # YAML config loader with validation
│   ├── identity_manager.py         # Persistent identity
│   ├── plugin_base.py              # Abstract plugin base class
│   ├── plugin_loader.py            # Plugin discovery
│   └── builtin_plugins/            # Built-in plugins (shipped with package)
│       ├── heartbeat_announce.py   # Network presence announcer
│       ├── message_echo.py         # LXMF echo responder
│       ├── system_monitor.py       # System metrics collector
│       ├── nomadnet_server.py      # NomadNet page server manager
│       └── example_plugin.py       # Scaffold — copy to start your own plugin
├── plugins/
│   └── example_plugin.py           # Scaffold copy (for easy access)
├── scripts/
│   ├── bootstrap.sh                # Fresh Pi setup
│   └── update.sh                   # Pull + upgrade + restart
├── systemd/
│   ├── reticulumpi.service         # Systemd unit file
│   └── rnsd.service                # Reticulum daemon (for shared instance mode)
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── entrypoint.sh              # Container entrypoint (starts rnsd + reticulumpi)
└── tests/
    ├── conftest.py
    ├── test_app.py                  # App orchestrator tests
    ├── test_cli.py                  # CLI entry point tests
    ├── test_config.py
    ├── test_config_validation.py    # Config error-path tests
    ├── test_plugin_base.py          # Base class helper tests
    ├── test_plugin_loader.py
    ├── test_nomadnet_server.py      # NomadNet plugin tests
    └── test_identity_manager.py
```

## CLI Usage

```
reticulumpi [--version] [--config PATH] [--reticulum-config DIR] [--log-level 0-7] [--check] [--list-plugins]
```

| Flag | Description |
|------|-------------|
| `--version`, `-V` | Show version and exit |
| `--config`, `-c` | Path to app config YAML (default: `~/.config/reticulumpi/config.yaml`) |
| `--reticulum-config` | Override Reticulum config directory |
| `--log-level` | Override log level: 0=critical, 1=error, 2-3=warning, 4=info, 5-7=debug |
| `--check` | Validate configuration and plugin discovery without starting (dry run) |
| `--list-plugins` | List all discoverable plugins and exit |

## Architecture

ReticulumPi installs Reticulum (`rns`) as a standard pip dependency -- it never patches, forks, or imports internal Reticulum modules. This means:

- `pip install --upgrade rns` merges upstream updates with zero conflicts
- All Reticulum features work as documented
- Plugins use only the public `RNS.*` and `LXMF.*` APIs

The application lifecycle:

1. Load YAML config
2. Initialize `RNS.Reticulum` (connects to `rnsd` or opens interfaces directly)
3. Load or create a persistent `RNS.Identity`
4. Discover and instantiate enabled plugins
5. Call `start()` on each plugin
6. Wait for SIGTERM/SIGINT
7. Call `stop()` on each plugin in reverse order

## License

MIT
