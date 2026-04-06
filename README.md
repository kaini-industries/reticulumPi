# ReticulumPi

An extensible [Reticulum](https://reticulum.network/) network node for Raspberry Pi.

ReticulumPi wraps the Reticulum cryptographic networking stack in a plugin-based architecture so you can add custom features without forking Reticulum itself. Upstream updates merge cleanly via `pip install --upgrade rns`.

## Features

- **Plugin system** -- add capabilities by dropping Python files into a directory
- **20 built-in plugins** -- heartbeat, LXMF echo, info bot, system metrics, NomadNet, MeshChat, web dashboard, network map, mesh telemetry, remote control, alerts, file transfer, sensor framework, emergency broadcast, transport monitor, connectivity monitor, path warmer, transport health, Meshtastic gateway, messaging hub
- **Auto-discovery** -- automatically maintains a pool of community hub connections with health probing, exponential backoff, regional diversity, and peer-to-peer hub exchange
- **Mesh-aware** -- passively maps network topology, shares telemetry with peers, broadcasts emergencies across the mesh
- **Remote management** -- manage nodes over Reticulum Links with zero IP dependency (SSH not required)
- **Web dashboard** -- real-time monitoring UI with auth, WebSocket updates, routing table visualization, mesh node and sensor views
- **Event bus** -- decoupled inter-plugin communication via publish/subscribe
- **Plugin hot-reload** -- enable/disable plugins at runtime without restarting
- **Persistent identity** -- stable cryptographic identity across restarts
- **Shared or standalone mode** -- coexists with `rnsd` or runs interfaces directly
- **Deployment automation** -- bootstrap script, systemd service, Docker support
- **CI/CD** -- GitHub Actions with lint + test matrix (Python 3.9--3.12)
- **No Reticulum fork** -- installs `rns` as a pip dependency, always upgradeable

## Documentation

| Guide | Description |
|-------|-------------|
| **[Built-in Plugins](docs/plugins.md)** | All 20 plugins with configuration options |
| **[Plugin Development](docs/plugin-development.md)** | Write your own plugin (lifecycle, events, LXMF, SQLite, testing) |
| **[API Reference](docs/api-reference.md)** | REST API and WebSocket endpoint documentation |
| **[Connectivity Guide](docs/connectivity-guide.md)** | LoRa, serial, packet radio, I2P hardware and setup |
| **[Troubleshooting](docs/troubleshooting.md)** | Common problems and solutions |
| **[Install Layout](docs/install-layout.md)** | How files move from git clone to running system |
| **[Contributing](CONTRIBUTING.md)** | How to contribute code, plugins, and docs |
| **[Security](SECURITY.md)** | Security model, best practices, vulnerability reporting |

## Requirements

- Python 3.9+
- Raspberry Pi 5 (or any Linux/macOS system) running 64-bit OS
- Optional: LoRa radio hardware for long-range mesh (see [Connectivity Guide](docs/connectivity-guide.md) -- boards from ~$15)

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

# With MeshChat web messaging UI:
sudo bash scripts/bootstrap.sh --with-meshchat

# With both NomadNet and MeshChat:
sudo bash scripts/bootstrap.sh --with-nomadnet --with-meshchat

# With LoRa/RNode support (installs rnodeconf for firmware flashing):
sudo bash scripts/bootstrap.sh --with-lora

# With the real-time web dashboard (installs aiohttp):
sudo bash scripts/bootstrap.sh --with-dashboard

# With I2P anonymous networking (installs i2pd for global overlay transport):
sudo bash scripts/bootstrap.sh --with-i2p

# Set a custom node name (default: ReticulumPi-<hostname>):
sudo bash scripts/bootstrap.sh --node-name "MyCabin" --with-nomadnet

# Install to a custom directory (default: /opt/reticulumpi):
sudo bash scripts/bootstrap.sh --install-dir /srv/reticulumpi --with-nomadnet

# Or install in-place (run directly from the cloned repo):
sudo bash scripts/bootstrap.sh --install-dir . --with-nomadnet
```

This will:

1. Install system packages (`python3`, `python3-venv`, `git`, + `nodejs`/`npm` if `--with-meshchat`)
2. Create a `reticulumpi` system user with hardware access groups (`dialout`, `gpio`, `spi`, `i2c`)
3. Copy the project to the install directory (default `/opt/reticulumpi`, or in-place with `--install-dir .`)
4. Create a Python venv and install dependencies (+ NomadNet if `--with-nomadnet`, + MeshChat if `--with-meshchat`, + `rnodeconf` if `--with-lora`, + `i2pd` if `--with-i2p`)
5. Set up config directories at `/etc/reticulumpi/` and `/home/reticulumpi/.reticulum/`
6. Set the node name (from `--node-name`, interactive prompt, or default `ReticulumPi-<hostname>`)
7. Create all runtime directories required by the systemd service sandboxing
8. Set up NomadNet directories, example pages, and auto-configure `use_shared_instance: true` + enable the `nomadnet_server` plugin (if `--with-nomadnet`)
9. Clone MeshChat, create isolated venv, build frontend, and auto-enable the `meshchat_server` plugin (if `--with-meshchat`)
10. Install and enable systemd services (`reticulumpi` + `rnsd` if NomadNet or MeshChat enabled)

For a detailed explanation of how files move from your git clone through bootstrap to the running system, see [docs/install-layout.md](docs/install-layout.md).

After bootstrap, configure and start:

```bash
# Edit the Reticulum config (network interfaces)
sudo nano /home/reticulumpi/.reticulum/config

# Optionally edit the app config (plugin settings, identity path, etc.)
sudo nano /etc/reticulumpi/config.yaml

# Start the service (use both if --with-nomadnet was used)
sudo systemctl start rnsd reticulumpi

# Watch the logs
journalctl -u reticulumpi -f
```

### Manual Setup

```bash
# Install on the Pi (from the cloned repo directory)
python3 -m venv .venv
.venv/bin/pip install .

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

This pulls the repo, upgrades all dependencies (including NomadNet and MeshChat if installed), rebuilds the MeshChat frontend if source changed, syncs any changed systemd service files, and restarts the services.

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

This builds the project as a wheel, installs it into a clean Debian Bookworm container, and runs the test suite.

## Configuration

ReticulumPi uses two separate config files:

### App Config (`config.yaml`)

Controls the application, plugins, and identity. Default location: `~/.config/reticulumpi/config.yaml`

```yaml
reticulumpi:
  # A friendly name for this node — used by NomadNet, LXMF Echo, and announces.
  # Defaults to "ReticulumPi-<hostname>" if not set, so every node is unique.
  node_name: MyCabin

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
      # display_name defaults to "<node_name> Echo" — override here if needed

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

The included example enables AutoInterface and TCP Server by default. It also contains documented, commented-out blocks for every supported interface type: TCP Client, RNode LoRa, RNode Multi, Serial, KISS TNC, AX.25 KISS, UDP, I2P, Pipe, and Backbone. See the [Connectivity Guide](docs/connectivity-guide.md) for details on each.

## Connectivity Guide

Reticulum can communicate over virtually any medium -- WiFi, Ethernet, LoRa radio, serial, packet radio, I2P, and more. You can enable multiple interfaces simultaneously and Reticulum automatically meshes traffic across all of them.

| Connection Method | Cost | Range | Best For |
|---|---|---|---|
| WiFi/Ethernet (Auto) | Free | LAN | Getting started |
| TCP Client/Server | Free | Global | Internet gateway |
| RNode LoRa | $15--150 | 1--100+ km | Off-grid mesh |
| Serial / HC-12 | $5--50 | Varies | Cheap radio links |
| KISS TNC | $35--500 | 10--50 km | Amateur radio |
| I2P | Free | Global | Anonymous networking |

> **Best starter LoRa pick:** LilyGO T-Beam v1.1 (~$25) -- GPS, battery holder, excellent community support. Flash with RNode firmware and plug into USB.

For complete hardware recommendations, configuration examples, frequency guides, range expectations, and troubleshooting, see **[docs/connectivity-guide.md](docs/connectivity-guide.md)**.

## Built-in Plugins

ReticulumPi ships with 20 built-in plugins. Enable any combination in your `config.yaml`:

| Plugin | Description |
|--------|-------------|
| **heartbeat_announce** | Periodic network presence announcements |
| **message_echo** | LXMF echo responder + auto propagation node selection |
| **info_bot** | LXMF command bot (`!weather`, `!mesh`, `!help`) |
| **system_monitor** | CPU, temp, memory, disk metric collection |
| **nomadnet_server** | NomadNet page server (subprocess manager) |
| **meshchat_server** | MeshChat web UI (subprocess manager) |
| **web_dashboard** | Real-time monitoring web UI with auth + WebSocket |
| **network_map** | Passive mesh topology mapper (SQLite) |
| **mesh_telemetry** | Distributed node metrics sharing |
| **remote_control** | Remote management over RNS Links (no SSH needed) |
| **alert_system** | LXMF threshold alerts (CPU, disk, crashes) |
| **file_transfer** | File sharing via RNS.Resource |
| **sensor_framework** | DS18B20, BME280, ADC, command sensors + logging |
| **emergency_broadcast** | Flood-style mesh-wide priority messaging |
| **transport_monitor** | TCP hub failover + auto-discovery + hub exchange |
| **connectivity_monitor** | Transport health + routing diagnostics |
| **path_warmer** | Proactive path refreshing for known nodes |
| **transport_health** | Transport relay node reliability tracking |
| **meshtastic_gateway** | Meshtastic LoRa mesh bridge (serial + MQTT) |
| **messaging_hub** | Unified message store + chat UI (LXMF + Meshtastic) |

For complete configuration options, see **[docs/plugins.md](docs/plugins.md)** and the annotated `config/reticulumpi/config.example.yaml`.

## Node Identities

A deployed ReticulumPi node has multiple Reticulum identities. Each LXMF plugin creates its own identity so that plugins can run independently without destination collisions.

| Service | Purpose | Identity File |
|---|---|---|
| **reticulumpi** (node) | Shared node identity for RNS destinations (heartbeat, mesh telemetry, network map, remote control, file transfer, sensors, emergency) | `~/.config/reticulumpi/identity` |
| **message_echo** | Echo bot — replies to LXMF messages | `~/.local/share/reticulumpi/lxmf/identity` |
| **info_bot** | Info bot — responds to `!` commands | `~/.local/share/reticulumpi/info_bot_lxmf/identity` |
| **alert_system** | LXMF alerts — separate identity for sending | Creates its own `RNS.Identity()` at runtime |
| **meshtastic_gateway** | Meshtastic↔LXMF bridge — receives LXMF messages to forward | `~/.local/share/reticulumpi/meshtastic_gw_lxmf/identity` |
| **meshtastic_gateway** (MQTT node) | Persistent Meshtastic node number for MQTT mode | `~/.local/share/reticulumpi/meshtastic_gw_lxmf/meshtastic_node_num` |
| **NomadNet daemon** | Page server — browsable via NomadNet TUI | `~/.nomadnet/storage/identity` |
| **NomadNet TUI** | Browse-only client (no node hosting) | `~/.nomadnet-tui/storage/identity` |
| **MeshChat** | Web UI chat over LXMF | `<install_dir>/storage/identity` |

To find your LXMF plugin addresses, check the startup logs:

```bash
sudo journalctl -u reticulumpi -g "active at" --no-pager
```

Or compute them from identity files:

```bash
sudo -u reticulumpi /opt/reticulumpi/.venv/bin/python3 -c "
import RNS
RNS.Reticulum('/home/reticulumpi/.reticulum', loglevel=RNS.LOG_CRITICAL)
for label, path in [
    ('message_echo', '/home/reticulumpi/.local/share/reticulumpi/lxmf/identity'),
    ('info_bot', '/home/reticulumpi/.local/share/reticulumpi/info_bot_lxmf/identity'),
    ('NomadNet daemon', '/home/reticulumpi/.nomadnet/storage/identity'),
]:
    i = RNS.Identity.from_file(path)
    d = RNS.Destination(i, RNS.Destination.IN, RNS.Destination.SINGLE, 'lxmf', 'delivery')
    print(f'{label:20s} {RNS.prettyhexrep(d.hash)}')
"
```

The **message_echo** and **info_bot** LXMF addresses are the ones to give to other users — they can message them from [Sideband](https://unsigned.io/sideband/) or MeshChat.

## Writing Custom Plugins

Plugins are Python files that define a class inheriting from `PluginBase`:

```python
# my_plugins/my_plugin.py
from reticulumpi.plugin_base import PluginBase

class MyPlugin(PluginBase):
    plugin_name = "my_plugin"
    plugin_version = "1.0.0"
    plugin_description = "Short description shown in --list-plugins"

    def start(self):
        self._active = True

    def stop(self):
        self._active = False
```

Enable it:

```yaml
plugin_paths:
  - ~/my_plugins

plugins:
  my_plugin:
    enabled: true
```

Every plugin gets access to `self.rns` (Reticulum), `self.identity` (node key), `self.config` (YAML config), `self.event_bus` (pub/sub), and `self.log` (logger). Use `self.app.get_plugin("name")` for inter-plugin communication.

Copy the scaffold to get started: `cp plugins/example_plugin.py ~/my_plugins/my_plugin.py`

For the complete guide covering LXMF messaging, SQLite storage, background threads, event handling, testing patterns, and dashboard integration, see **[docs/plugin-development.md](docs/plugin-development.md)**.

## Project Structure

```
reticulumPi/
├── pyproject.toml                  # Dependencies and entry point
├── Makefile                        # install, dev, test, lint, format targets
├── LICENSE                         # MIT license
├── CHANGELOG.md                    # Version history
├── .github/workflows/ci.yml       # GitHub Actions: lint + test (Python 3.9-3.12)
├── CONTRIBUTING.md                 # How to contribute
├── SECURITY.md                     # Security policy and best practices
├── docs/
│   ├── install-layout.md           # Detailed install directory & file flow docs
│   ├── plugins.md                  # Built-in plugin reference (all 20 plugins)
│   ├── plugin-development.md       # Plugin development guide (full walkthrough)
│   ├── api-reference.md            # REST API & WebSocket documentation
│   ├── connectivity-guide.md       # Hardware, radio, and interface guide
│   └── troubleshooting.md          # FAQ and common issues
├── config/
│   ├── nomadnet/
│   │   └── pages/                  # NomadNet pages (.mu files)
│   │       ├── index.mu            # Landing page with navigation
│   │       ├── help.mu             # Markup reference
│   │       └── status.mu           # Dynamic system + network status page
│   ├── reticulum/
│   │   ├── config.example          # Reticulum interface config (all interfaces)
│   │   └── config.minimal          # Minimal safe config (AutoInterface only)
│   └── reticulumpi/
│       └── config.example.yaml     # App + plugin config (all plugins documented)
├── src/reticulumpi/
│   ├── __init__.py                 # Package version
│   ├── app.py                      # Core orchestrator (plugin hot-reload)
│   ├── cli.py                      # CLI entry point (+ remote control client)
│   ├── config.py                   # YAML config loader with validation
│   ├── event_bus.py                # Thread-safe publish/subscribe event bus
│   ├── events.py                   # Event type constants
│   ├── identity_manager.py         # Persistent identity
│   ├── plugin_base.py              # Abstract plugin base class
│   ├── plugin_loader.py            # Plugin discovery
│   ├── remote_client.py            # Remote control CLI client
│   ├── data/
│   │   └── community_hubs.yaml     # Curated community TCP hub list for auto-discovery
│   └── builtin_plugins/            # Built-in plugins (shipped with package)
│       ├── heartbeat_announce.py   # Network presence announcer
│       ├── message_echo.py         # LXMF echo responder
│       ├── info_bot.py             # LXMF command bot (weather, etc.)
│       ├── system_monitor.py       # System metrics collector
│       ├── nomadnet_server.py      # NomadNet page server manager
│       ├── meshchat_server.py      # MeshChat web UI manager
│       ├── network_map.py          # Passive mesh topology mapper
│       ├── mesh_telemetry.py       # Distributed node metrics sharing
│       ├── remote_control.py       # Remote management over RNS Links
│       ├── alert_system.py         # LXMF threshold alerts
│       ├── file_transfer.py        # File transfer via RNS.Resource
│       ├── sensor_framework.py     # Config-driven sensor reading + logging
│       ├── emergency_broadcast.py  # Mesh-wide flood-style messaging
│       ├── transport_monitor.py    # TCP hub health + failover + auto-discovery + hub exchange
│       ├── connectivity_monitor.py # Transport health + routing diagnostics
│       ├── path_warmer.py          # Proactive path refreshing for known nodes
│       ├── transport_health.py     # Transport relay node reliability tracking
│       ├── meshtastic_gateway.py  # Meshtastic LoRa ↔ LXMF bridge (serial + MQTT)
│       ├── messaging_hub.py        # Unified messaging store + LXMF/Meshtastic adapters
│       ├── web_dashboard/          # Secure web dashboard (aiohttp)
│       └── example_plugin.py       # Scaffold — copy to start your own plugin
├── plugins/
│   └── example_plugin.py           # Scaffold copy (for easy access)
├── scripts/
│   ├── bootstrap.sh                # Fresh Pi setup
│   ├── update.sh                   # Pull + upgrade + restart
│   ├── nomadnet-tui.sh             # Launch NomadNet TUI over SSH
│   └── meshchat_launcher.py        # MeshChat wrapper (timeout patching + logging)
├── systemd/
│   ├── reticulumpi.service         # Systemd unit file
│   └── rnsd.service                # Reticulum daemon (for shared instance mode)
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── entrypoint.sh              # Container entrypoint (starts rnsd + reticulumpi)
└── tests/                          # 610+ tests (pytest)
    ├── conftest.py
    ├── test_app.py                  # App orchestrator tests
    ├── test_cli.py                  # CLI entry point tests
    ├── test_config.py
    ├── test_config_validation.py    # Config error-path tests
    ├── test_plugin_base.py          # Base class helper tests
    ├── test_plugin_loader.py
    ├── test_event_bus.py            # Event bus thread-safety tests
    ├── test_message_echo.py         # LXMF echo + propagation selection tests
    ├── test_info_bot.py             # Info bot command + weather tests
    ├── test_nomadnet_server.py      # NomadNet plugin tests
    ├── test_meshchat_server.py      # MeshChat plugin tests
    ├── test_identity_manager.py
    ├── test_network_map.py          # Network map + SQLite tests
    ├── test_mesh_telemetry.py       # Mesh telemetry tests
    ├── test_remote_control.py       # Remote control auth + handler tests
    ├── test_remote_client.py        # Remote client format + command tests
    ├── test_alert_system.py         # Alert rules + cooldown tests
    ├── test_file_transfer.py        # File transfer + safety tests
    ├── test_sensor_framework.py     # Sensor drivers + storage tests
    ├── test_emergency_broadcast.py  # Emergency flood + dedup tests
    ├── test_transport_monitor.py    # Transport hub health + failover + auto-discovery + hub exchange tests
    ├── test_connectivity_monitor.py # Connectivity + routing data tests
    ├── test_path_warmer.py          # Path warming + ensure_path tests
    ├── test_transport_health.py     # Transport node tracking + SQLite tests
    ├── test_meshtastic_gateway.py   # Meshtastic gateway serial + MQTT + rate limiting tests
    ├── test_messaging_hub.py        # Messaging hub + adapters + message store tests
    ├── test_routing_api.py          # Routing API endpoint tests
    └── test_web_dashboard.py        # Dashboard auth + API + WebSocket tests
```

## CLI Usage

```
reticulumpi [--version] [--config PATH] [--reticulum-config DIR] [--log-level 0-7]
            [--check] [--list-plugins]
            [--remote HASH] [--command CMD] [--timeout SECS]
            [--backup-identity PATH] [--restore-identity PATH] [--hash-password]
```

| Flag | Description |
|------|-------------|
| `--version`, `-V` | Show version and exit |
| `--config`, `-c` | Path to app config YAML (default: `~/.config/reticulumpi/config.yaml`) |
| `--reticulum-config` | Override Reticulum config directory |
| `--log-level` | Override log level: 0=critical, 1=error, 2-3=warning, 4=info, 5-7=debug |
| `--check` | Validate configuration and plugin discovery without starting (dry run) |
| `--list-plugins` | List all discoverable plugins and exit |
| `--remote HASH` | Connect to a remote node's `remote_control` plugin over Reticulum |
| `--command CMD` | Execute a single remote command and exit (use with `--remote`) |
| `--timeout SECS` | Remote connection timeout in seconds (default: 30) |
| `--backup-identity PATH` | Back up the node identity file to the given path |
| `--restore-identity PATH` | Restore a node identity from the given path |
| `--hash-password` | Hash a password for use in web_dashboard config (interactive) |

## Architecture

ReticulumPi installs Reticulum (`rns`) as a standard pip dependency -- it never patches, forks, or imports internal Reticulum modules. This means:

- `pip install --upgrade rns` merges upstream updates with zero conflicts
- All Reticulum features work as documented
- Plugins use only the public `RNS.*` and `LXMF.*` APIs

The application lifecycle:

1. Load YAML config
2. Initialize `RNS.Reticulum` (connects to `rnsd` or opens interfaces directly)
3. Load or create a persistent `RNS.Identity`
4. Create the event bus for inter-plugin communication
5. Discover and instantiate enabled plugins
6. Call `start()` on each plugin (publishes `PLUGIN_STARTED` events)
7. Wait for SIGTERM/SIGINT
8. Call `stop()` on each plugin in reverse order (publishes `PLUGIN_STOPPED` events)

Plugins can be enabled/disabled at runtime via `app.enable_plugin(name)` / `app.disable_plugin(name)` (used by the remote control plugin).

## License

MIT
