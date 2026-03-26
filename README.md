# ReticulumPi

An extensible [Reticulum](https://reticulum.network/) network node for Raspberry Pi.

ReticulumPi wraps the Reticulum cryptographic networking stack in a plugin-based architecture so you can add custom features without forking Reticulum itself. Upstream updates merge cleanly via `pip install --upgrade rns`.

## Features

- **Plugin system** -- add capabilities by dropping Python files into a directory
- **Three built-in plugins** -- heartbeat announce, LXMF message echo, system metrics
- **Persistent identity** -- stable cryptographic identity across restarts
- **Shared or standalone mode** -- coexists with `rnsd` or runs interfaces directly
- **Deployment automation** -- bootstrap script, systemd service, Docker support
- **No Reticulum fork** -- installs `rns` as a pip dependency, always upgradeable

## Requirements

- Python 3.9+
- Raspberry Pi 5 (or any Linux system) running 64-bit OS
- Optional: LoRa radio hardware ([RNode](https://unsigned.io/rnode/)) for long-range mesh

## Quick Start (Development)

```bash
git clone https://github.com/kaini-industries/reticulumPi.git
cd reticulumPi
make dev            # creates venv + installs in editable mode with dev deps
make test           # runs the test suite
make lint           # runs ruff linter
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
```

This will:

1. Install system packages (`python3`, `python3-venv`, `git`)
2. Create a `reticulumpi` system user with hardware access groups (`dialout`, `gpio`, `spi`, `i2c`)
3. Copy the project to `/opt/reticulumpi`
4. Create a Python venv and install dependencies
5. Set up config directories at `/etc/reticulumpi/` and `/home/reticulumpi/.reticulum/`
6. Install and enable the systemd service

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
```

### Updating

Pull the latest code and upgrade dependencies:

```bash
sudo bash scripts/update.sh
```

This pulls the repo, upgrades `rns` and `lxmf`, reinstalls the project, and restarts the service.

## Docker

```bash
cd docker

# Copy and edit config
mkdir -p config
cp ../config/reticulumpi/config.example.yaml config/config.yaml

# Build and run
docker compose up -d

# View logs
docker compose logs -f
```

To pass through a serial radio device, uncomment the `devices` section in `docker/docker-compose.yml`:

```yaml
devices:
  - /dev/ttyUSB0:/dev/ttyUSB0
```

Host networking is enabled by default, which is required for Reticulum's UDP/TCP interfaces.

## Configuration

ReticulumPi uses two separate config files:

### App Config (`config.yaml`)

Controls the application, plugins, and identity. Default location: `~/.config/reticulumpi/config.yaml`

```yaml
reticulumpi:
  # Connect to running rnsd (true) or open interfaces directly (false)
  use_shared_instance: true

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
| RNode LoRa | RNode USB transceiver | $60--150 | 1--100+ km | Long-range off-grid mesh |
| RNode Multi | RNode (firmware v1.74+) | $60--150 | Multi-channel | Simultaneous frequencies |
| Serial | USB-serial adapter + radio | $5--50 | Varies | Data radios, laser links, direct wiring |
| KISS TNC | Packet radio modem | $100--500 | 10--50 km | Amateur radio (VHF/UHF) |
| AX.25 KISS | KISS TNC | $100--500 | 10--50 km | Ham radio with FCC-compliant ID |
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

### RNode LoRa Radio

[RNode](https://unsigned.io/rnode/) is an open-source LoRa transceiver designed specifically for Reticulum. It uses raw LoRa modulation (not LoRaWAN) and delivers long-range, low-power wireless mesh connectivity.

**Compatible boards:**
- LilyGO T-Beam (v1.0, v1.1, Supreme), T3S3, T-Deck
- Heltec LoRa32 v2.0, v3.0, v4.0
- RAK4631-based boards
- Unsigned RNode v2.x

**Setup on Pi:**
1. Connect the board via USB (appears as `/dev/ttyUSB0` or `/dev/ttyACM0`)
2. Flash RNode firmware: `pip install rnodeconf && rnodeconf --autoinstall`
3. Uncomment the `[RNode LoRa Interface]` section in your Reticulum config
4. Set the frequency for your region: 915 MHz (Americas), 868 MHz (EU), 433 MHz

**Range:** Several kilometers in urban environments, over 100 km line-of-sight with clear path. A documented test achieved a 15.75 km usable SSH link at 2.6 kbps.

**Frequencies:** 433 MHz, 868 MHz, 915 MHz, 2.4 GHz depending on board and regional regulations.

### RNode Multi-Channel

Requires RNode firmware v1.74 or later. Allows a single RNode device to operate on multiple LoRa frequencies simultaneously -- for example, monitoring 915 MHz and 868 MHz at the same time. Each channel is independently configurable.

### Serial Interface

Sends raw Reticulum packets over any serial connection. Use the Pi's built-in UART (`/dev/ttyAMA0` on GPIO pins 14/15) or any USB-serial adapter (`/dev/ttyUSB0`).

**Use cases:**
- Direct wire-pair connections between two Pis
- Data radios with serial interfaces
- Free-space optical (laser) links with serial output
- Any device that sends/receives raw bytes

Enable the Pi's serial port with `sudo raspi-config` > Interface Options > Serial Port.

### KISS TNC (Packet Radio)

Connects to [KISS](https://en.wikipedia.org/wiki/KISS_(TNC))-compatible packet radio modems for amateur radio operation. KISS is a standard protocol for communicating with Terminal Node Controllers (TNCs).

**Compatible hardware:**
- [OpenModem](https://unsigned.io/openmodem/) -- open-source packet radio modem
- [Dire Wolf](https://github.com/wb2osz/direwolf) -- software modem (uses Pi sound card as radio interface)
- Any standard KISS-compatible TNC

**Range:** 10--50 km line-of-sight typical for VHF/UHF amateur radio. Supports configurable preamble, TX tail, persistence, and slot time for CSMA channel access.

### AX.25 KISS Interface

Same as KISS but adds AX.25 protocol framing with mandatory station identification beaconing. Required for amateur radio regulatory compliance (FCC Part 97 in the US, similar regulations elsewhere) where periodic callsign transmission is mandatory.

Adds some per-packet overhead compared to plain KISS. Use this only when regulatory compliance requires it.

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

See `config/reticulum/config.example` for ready-to-use configuration blocks for every interface type.

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
| `storage_path` | /tmp/reticulumpi_lxmf | LXMF message storage directory |

Send a test message from another device using [Sideband](https://unsigned.io/sideband/) or `lxmf_send`.

### System Monitor

Collects system metrics on a timer. Other plugins can read metrics via `app.get_plugin("system_monitor").latest_metrics`.

| Option | Default | Description |
|--------|---------|-------------|
| `collect_interval_seconds` | 60 | Seconds between metric collections |
| `metrics` | all four | List of metrics to collect |

Available metrics: `cpu_percent`, `cpu_temp`, `memory_percent`, `disk_percent`

## Writing Custom Plugins

Plugins are Python files that define a class inheriting from `PluginBase`. Drop your plugin file into the `plugins/` directory or any path listed in `plugin_paths`.

### Minimal Plugin

```python
# plugins/my_plugin.py
from reticulumpi.plugin_base import PluginBase

class MyPlugin(PluginBase):
    plugin_name = "my_plugin"
    plugin_version = "1.0.0"

    def start(self):
        self._active = True
        # Set up destinations, start threads, register handlers

    def stop(self):
        self._active = False
        # Clean up resources
```

Enable it in `config.yaml`:

```yaml
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

#### Lifecycle

1. **Discovery** -- `PluginLoader` scans directories for `.py` files, imports them, finds `PluginBase` subclasses
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

### Plugin Examples

**Create a Reticulum destination and announce:**

```python
import RNS

def start(self):
    self.destination = RNS.Destination(
        self.identity,
        RNS.Destination.IN,
        RNS.Destination.SINGLE,
        "myapp",
        "myaspect",
    )
    self.destination.announce()
```

**Listen for LXMF messages:**

```python
import LXMF

def start(self):
    self.router = LXMF.LXMRouter(storagepath="/tmp/my_lxmf")
    self.dest = self.router.register_delivery_identity(self.identity)
    self.router.register_delivery_callback(self.on_message)

def on_message(self, message):
    print(f"From {RNS.prettyhexrep(message.source_hash)}: {message.content_as_string()}")
```

**Run a background thread:**

```python
import threading, time

def start(self):
    self._active = True
    self._thread = threading.Thread(target=self._loop, daemon=True)
    self._thread.start()

def _loop(self):
    while self._active:
        # do work
        for _ in range(60):  # sleep in 1s increments for fast shutdown
            if not self._active:
                return
            time.sleep(1)
```

## Project Structure

```
reticulumPi/
├── pyproject.toml                  # Dependencies and entry point
├── Makefile                        # install, dev, test, lint targets
├── config/
│   ├── reticulum/
│   │   └── config.example          # Reticulum interface config
│   └── reticulumpi/
│       └── config.example.yaml     # App + plugin config
├── src/reticulumpi/
│   ├── __init__.py                 # Package version
│   ├── app.py                      # Core orchestrator
│   ├── cli.py                      # CLI entry point
│   ├── config.py                   # YAML config loader
│   ├── identity_manager.py         # Persistent identity
│   ├── plugin_base.py              # Abstract plugin base class
│   └── plugin_loader.py            # Plugin discovery
├── plugins/
│   ├── heartbeat_announce.py       # Network presence announcer
│   ├── message_echo.py             # LXMF echo responder
│   └── system_monitor.py           # System metrics collector
├── scripts/
│   ├── bootstrap.sh                # Fresh Pi setup
│   └── update.sh                   # Pull + upgrade + restart
├── systemd/
│   └── reticulumpi.service         # Systemd unit file
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
└── tests/
    ├── conftest.py
    ├── test_config.py
    ├── test_plugin_loader.py
    └── test_identity_manager.py
```

## CLI Usage

```
reticulumpi [--config PATH] [--reticulum-config DIR] [--log-level 0-7]
```

| Flag | Description |
|------|-------------|
| `--config`, `-c` | Path to app config YAML (default: `~/.config/reticulumpi/config.yaml`) |
| `--reticulum-config` | Override Reticulum config directory |
| `--log-level` | Override log level (0=critical, 4=info, 7=extreme) |

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
