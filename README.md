# ReticulumPi

An extensible [Reticulum](https://reticulum.network/) network node for Raspberry Pi.

ReticulumPi wraps the Reticulum cryptographic networking stack in a plugin-based architecture
so you can add custom features without forking Reticulum itself. Development environments may
upgrade `rns` independently; production releases install the reviewed, hash-locked dependency
set carried by the signed ReticulumPi bundle.

## Features

- **Plugin system** -- add capabilities by dropping Python files into a directory
- **Built-in plugins** -- messaging, mesh networking, LoRa diagnostics, RTL-SDR radio, ADS-B aircraft tracking, AIS marine tracking, ACARS aviation messages, NOAA weather satellite imagery, radiosonde balloon tracking, weather alerts, satellite tracking, GPS telemetry, NTP time sync, spectrum analysis, FM receiver, MeshCore bridging, and more
- **Web dashboard** -- real-time monitoring UI with auth, WebSocket updates, interface management, routing table visualization, mesh topology, sensor sparklines, chat messaging, spectrum waterfall with full-page view, ADS-B radar, AIS marine map, GPS map, satellite tracking, node tracker, and weather alerts
- **SDR dongle scheduler** -- priority-based time-sharing of RTL-SDR dongles across multiple signal plugins (weather alerts > satellite passes > continuous decoders), so a single $25 dongle can power ADS-B, AIS, ACARS, FM, spectrum, and weather monitoring
- **RTL-SDR radio** -- spectrum waterfall, LoRa band scanning, ADS-B aircraft tracking, AIS marine vessel tracking, ACARS aviation messages, NOAA satellite images, radiosonde balloon tracking, SAME weather alerts, ISM band device decoding, and FM/AM receiver using cheap USB SDR dongles
- **Multi-mesh bridging** -- bidirectional relay between Meshtastic and MeshCore networks with loop prevention and rate limiting
- **Interface management** -- enable/disable Reticulum network interfaces from the dashboard with one-click service restart
- **Server-side pagination** -- mesh network table with 11,000+ nodes paginated at the SQLite layer; targeted reachability scoring for visible nodes only
- **Auto-discovery** -- automatically maintains a pool of community hub connections with health probing, exponential backoff, regional diversity, and peer-to-peer hub exchange
- **Mesh-aware** -- passively maps network topology, shares telemetry with peers, broadcasts emergencies across the mesh
- **Remote management** -- manage nodes over Reticulum Links with zero IP dependency (SSH not required)
- **Internet resilience** -- asymmetric-hysteresis connectivity probe publishes online/offline events so plugins gracefully degrade when the network drops
- **Node tracker** -- search, track, and filter mesh nodes on the map with persistent tracking chips
- **Event bus** -- a code-derived event catalog for decoupled inter-plugin communication via publish/subscribe
- **Plugin hot-reload** -- enable/disable plugins at runtime without restarting
- **Persistent identity** -- stable cryptographic identity across restarts
- **Shared or standalone mode** -- coexists with `rnsd` or runs interfaces directly
- **Deployment automation** -- bootstrap script, systemd service, Docker support
- **CI/CD** -- configured lint, format, package, container, and test lanes for Python
  3.11--3.14; release approval additionally requires signed artifacts and hardware evidence
- **No Reticulum fork** -- installs `rns` as a pip dependency, always upgradeable

## Documentation

| Guide | Description |
|-------|-------------|
| **[Built-in Plugins](docs/plugins.md)** | Configuration guidance for built-in plugins |
| **[Generated Code Reference](docs/generated-code-reference.md)** | Current plugin, event, route, and core-default inventories |
| **[Plugin Development](docs/plugin-development.md)** | Write your own plugin (lifecycle, events, LXMF, SQLite, testing) |
| **[API Reference](docs/api-reference.md)** | REST API and WebSocket endpoint documentation |
| **[Connectivity Guide](docs/connectivity-guide.md)** | LoRa, serial, packet radio, I2P hardware and setup |
| **[Solar Power Build](docs/solar-power-build.md)** | Off-grid solar-powered node hardware guide |
| **[Troubleshooting](docs/troubleshooting.md)** | Common problems and solutions |
| **[Install Layout](docs/install-layout.md)** | How files move from git clone to running system |
| **[Upgrade and Rollback](docs/upgrade-and-rollback.md)** | Transactional releases, backups, and recovery |
| **[Container Deployment](docs/container-deployment.md)** | Persistent data, networking, and hardware access |
| **[Dashboard Operations](docs/dashboard-operations.md)** | Authentication, TLS, local tokens, and recovery |
| **[Hardware Validation](docs/hardware-validation.md)** | Raspberry Pi release qualification |
| **[Contributing](CONTRIBUTING.md)** | How to contribute code, plugins, and docs |
| **[Security](SECURITY.md)** | Security model, best practices, vulnerability reporting |

## Requirements

### Core

- Python 3.11--3.14 for development and CI; production uses Python 3.11 on Bookworm or
  Python 3.12 on Ubuntu Noble
- Raspberry Pi 5 (ARM64) with either 64-bit Raspberry Pi OS Bookworm or Ubuntu 24.04 LTS
  Noble for production; Linux/macOS are supported development environments
- System packages: `python3`, `python3-venv`, `python3-pip`, `git`

> **Note:** Bootstrap is now a thin launcher for transactional `reticulumpi-admin`. Install
> required OS packages for the selected production lane explicitly; bootstrap never runs `apt`, follows a Git branch,
> or mutates a live environment.

### Optional Hardware

- LoRa radio hardware for long-range mesh (see [Connectivity Guide](docs/connectivity-guide.md) -- boards from ~$15)
- RTL-SDR USB dongle (~$25) for spectrum analysis, ADS-B aircraft tracking, AIS marine tracking, ACARS aviation messages, NOAA weather satellite imagery, radiosonde balloon tracking, SAME weather alerts, ISM band device decoding, FM radio, and LoRa band scanning
- USB GPS receiver for telemetry, node mapping, and GPS-disciplined NTP time sync
- Meshtastic LoRa radio ($20--60) for Meshtastic mesh network bridging
- MeshCore LoRa radio ($20--60) for MeshCore mesh network bridging

### Optional System Packages

Install these OS packages separately. Bootstrap flags select packaged Python extras or
reviewed helpers; they do not mutate the operating-system package set.

| Package | Bootstrap Flag | Purpose |
|---------|---------------|---------|
| `nodejs`, `npm` | Manual reviewed MeshChat deployment | MeshChat frontend build |
| `i2pd` | Install before `--with-i2p` reminder | I2P anonymous network router (SAM API) |
| `yggdrasil` | Install before `--with-yggdrasil` reminder | Encrypted IPv6 overlay network |
| `aiohttp` (pip) | `--with-dashboard` | Web dashboard HTTP server |
| `rnodeconf` (pip) | `--with-lora` | RNode firmware flashing |

### External Tools (per plugin)

Some plugins invoke external binaries at runtime. These must be installed separately and available on `PATH`:

| Tool | Package | Used By |
|------|---------|---------|
| `rtl_fm`, `rtl_test`, `rtl_power` | `rtl-sdr` | spectrum_scanner, fm_receiver, lora_scanner, radiosonde_tracker, weather_alert, noaa_apt_decoder |
| `dump1090` (or `dump1090-fa`, `readsb`) | `dump1090-mutability` / `dump1090-fa` | adsb_radar |
| `rtl_433` | `rtl-433` | ism_decoder |
| `acarsdec` | `acarsdec` | acars_decoder |
| `AIS-catcher` (or `rtl_ais`) | `AIS-catcher` / `rtl-ais` | ais_receiver |
| `noaa-apt` (or `wxtoimg`) | `noaa-apt` | noaa_apt_decoder |
| `rs41mod` | Build from source | radiosonde_tracker |
| `multimon-ng` | `multimon-ng` | weather_alert (SAME decoding) |
| `chronyc` | `chrony` | ntp_server through the scoped control broker |
| `gpsd` | `gpsd` | gps_telemetry (alternative to direct serial) |
| `hostapd` | `hostapd` | hotspot_monitor (reads config only) |

For RTL-SDR plugins, the default DVB-T kernel driver must be blacklisted:

```bash
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf
sudo modprobe -r dvb_usb_rtl28xxu
```

### Hardware Access Groups

Add the `reticulumpi` service user only to hardware groups required by that node:

| Group | Devices |
|-------|---------|
| `dialout` | Serial ports (GPS, Meshtastic, RNode) |
| `gpio` | Raspberry Pi GPIO pins |
| `spi` | SPI interface |
| `i2c` | I2C bus (BME280 sensors) |

## Quick Start (Development)

```bash
git clone https://github.com/kaini-industries/reticulumPi.git
cd reticulumPi
make dev            # creates venv + installs in editable mode with dev deps
make test           # runs the test suite
make test-serial    # deterministic single-process test run
make lint           # runs ruff linter
make format-check   # verifies formatting without changing files
make format         # auto-format code with ruff
make package-check  # builds and verifies the wheel and packaged dashboard
```

Run locally:

```bash
.venv/bin/reticulumpi --config config/reticulumpi/config.example.yaml
```

## Raspberry Pi Deployment

### Automated Setup

Bootstrap validates arguments and delegates to an independently installed transactional
administrator. It defaults to a non-mutating dry run. It never imports Python or selects an
administrator from the release bundle, checkout, `PATH`, or mutable `current` release.

Before the first production dry run or apply, install the separately signed ReticulumPi
recovery-administrator package. It must provide a regular, root-owned, non-group/other-writable
`/usr/sbin/reticulumpi-admin` (or `/usr/bin/reticulumpi-admin`) beneath equally protected path
components. If that prerequisite is absent, both compatibility launchers fail closed; a first
install cannot safely bootstrap its privileged verifier from the artifact it has not yet
verified. Obtain the recovery package through the release channel, not from inside the candidate
bundle.

Production dry runs and applies require an extracted, signed release bundle with its
`SHA256SUMS` and `SHA256SUMS.minisig`; review the plan, then apply the same bundle as root:

Before the first production run, provision the independently obtained ReticulumPi release
public key as a regular root-owned, non-writable file at
`/usr/share/reticulumpi/release.pub`, and install `/usr/bin/minisign`. Do not copy the trust key
from the bundle it is meant to verify.

```bash
# Review and install the exact signed release bundle:
/usr/sbin/reticulumpi-admin install \
  --bundle /path/to/extracted-reticulumpi-bundle --dry-run
sudo /usr/sbin/reticulumpi-admin install \
  --bundle /path/to/extracted-reticulumpi-bundle --apply --start

# The compatibility launcher is available only after the same prerequisite:
bash scripts/bootstrap.sh --bundle /path/to/extracted-reticulumpi-bundle --dry-run

# With NomadNet page server support:
sudo bash scripts/bootstrap.sh --bundle /path/to/extracted-reticulumpi-bundle \
  --apply --with-nomadnet --start

# With LoRa/RNode support (installs rnodeconf for firmware flashing):
sudo bash scripts/bootstrap.sh --bundle /path/to/extracted-reticulumpi-bundle \
  --apply --with-lora

# With the real-time web dashboard (installs aiohttp):
sudo bash scripts/bootstrap.sh --bundle /path/to/extracted-reticulumpi-bundle \
  --apply --with-dashboard --start

# Select reviewed privileged helpers only when their plugins are used:
sudo bash scripts/bootstrap.sh --bundle /path/to/extracted-reticulumpi-bundle \
  --apply --with-captive-portal --with-chrony-control

# Install to a custom directory (default: /srv/reticulumpi):
sudo bash scripts/bootstrap.sh --bundle /path/to/extracted-reticulumpi-bundle \
  --apply --install-root /srv/reticulumpi --with-nomadnet

```

For local planning only, a non-root developer with the trusted system administrator already
installed can explicitly allow an unsigned checkout:

```bash
RETICULUMPI_ADMIN_ALLOW_UNSIGNED_DEV=1 bash scripts/bootstrap.sh --dry-run
```

That escape is rejected for root and can never be used with `--apply`.

Production in-place installs are rejected. Application code and virtual environments are
root-owned; the service account can write only runtime state. Privileged operations use the
root-owned control broker; bootstrap installs no sudo rules.

The administrator durably records `state: preparing` before it creates the candidate release or
virtual environment. It creates an isolated root-owned release, installs selected Python extras,
backs up configuration and state before path/configuration mutation, atomically switches
`current`, renders selected
units/helpers, and rolls back on readiness failure. MeshChat, OS packages, hardware groups,
node naming, and external decoders remain explicit operator steps.

For a detailed explanation of how files move from your git clone through bootstrap to the running system, see [docs/install-layout.md](docs/install-layout.md).

After bootstrap, configure and start:

```bash
# Edit the Reticulum config (network interfaces)
sudo nano /var/lib/reticulumpi/.reticulum/config

# Optionally edit the app config (plugin settings, identity path, etc.)
sudo nano /etc/reticulumpi/config.yaml

# Start the service (use both if --with-nomadnet was used)
sudo systemctl start rnsd reticulumpi

# Watch the logs
journalctl -u reticulumpi -f
```

### Developer / Non-Service Setup

The following per-user paths are for local development only. They are not a supported
production installation and are never used by the systemd services.

```bash
# Install on the Pi (from the cloned repo directory)
python3 -m venv .venv
.venv/bin/pip install .

# Copy example configs
mkdir -p ~/.config/reticulumpi
cp config/reticulumpi/config.development.example.yaml \
  ~/.config/reticulumpi/config.yaml
mkdir -p ~/.reticulum
cp config/reticulum/config.example ~/.reticulum/config

# Run
reticulumpi --config ~/.config/reticulumpi/config.yaml

# Validate config without starting
reticulumpi --check --config ~/.config/reticulumpi/config.yaml

# List available plugins
reticulumpi --list-plugins
```

### Updating

Run both phases against the same signed release bundle using the independently installed,
root-owned administrator. An unsigned source checkout is valid only for the non-root development
dry run shown above and can never be applied:

```bash
bash scripts/update.sh --bundle /path/to/extracted-reticulumpi-bundle --dry-run
sudo bash scripts/update.sh --bundle /path/to/extracted-reticulumpi-bundle --apply

# Or use the administrator directly
reticulumpi-admin upgrade --bundle /path/to/extracted-reticulumpi-bundle \
  --install-root /srv/reticulumpi --dry-run
sudo reticulumpi-admin upgrade --bundle /path/to/extracted-reticulumpi-bundle \
  --install-root /srv/reticulumpi --apply
sudo reticulumpi-admin rollback --dry-run
sudo reticulumpi-admin rollback --apply
```

The updater never performs `git pull` and never changes the active virtual environment in
place. It builds a candidate release, runs `pip check`, records a backup and transaction
journal, switches the `current` symlink atomically, and restores the previous release if
the service does not become active. See [Upgrade and Rollback](docs/upgrade-and-rollback.md).

## Web Dashboard

The web dashboard provides real-time monitoring and management of your ReticulumPi node. Enable it with `--with-dashboard` during bootstrap or by enabling the `web_dashboard` plugin in your config.

### Dashboard Sections

| Section | Description |
|---------|-------------|
| **System Metrics** | CPU, temperature, memory, disk usage |
| **Network Interfaces** | Enable/disable interfaces, view status and traffic |
| **Connectivity Health** | rnsd, I2P, SAM, interface, and path diagnostics |
| **Routing** | Interactive path table with pagination, hop distribution and interface breakdown charts, path freshness stats |
| **Transport Hubs** | Hub connection status, live throughput rates, auto-discovery pool |
| **Path Warming** | Proactive path refresh status for known nodes |
| **Mesh Network** | Server-side paginated topology view with sorting, filtering, reachability scores |
| **Mesh Bridge** | Meshtastic ↔ MeshCore relay status with pause/resume control |
| **Peer Telemetry** | Distributed node metrics from mesh peers |
| **Messages** | Tabbed chat UI with LXMF, Meshtastic, MeshCore, and MQTT feed views |
| **Meshtastic Gateway** | Meshtastic node list, channels, device info, SNR metrics |
| **MeshCore Gateway** | MeshCore device status and contact list |
| **MeshCore Observer** | MeshCore letsmesh.net analyzer companion |
| **LoRa Diagnostics** | LoRa peer tracking, announce beaconing, signal stats |
| **LoRa Link Tester** | RF link quality probe/ACK measurements to Meshtastic peers |
| **SDR Spectrum** | RTL-SDR waterfall sweep with configurable presets |
| **LoRa Spectrum** | Dedicated LoRa-band spectrum scanner |
| **ADS-B Radar** | Aircraft tracking with position, altitude, and heading (RTL-SDR + dump1090) |
| **AIS Marine** | Vessel tracking with MMSI, ship type, speed, and heading (RTL-SDR + AIS-catcher) |
| **ACARS Decoder** | Aviation message decoding with flight, label, and message text (RTL-SDR + acarsdec) |
| **Weather Alerts** | NOAA Weather Radio SAME alert monitoring with severity and area codes |
| **Radiosonde Tracker** | Weather balloon tracking with altitude, temperature, humidity, and wind (RS41/DFM) |
| **NOAA Satellite** | Weather satellite APT image capture during overhead passes (NOAA 15/18/19) |
| **ISM Decoder** | IoT device, weather station, and TPMS decoding via rtl_433 |
| **VHF Radio** | FM/AM radio receiver with tuning, squelch, favorites, recording, and audio streaming |
| **GPS Telemetry** | Live GPS fix, satellite count, and accuracy metrics |
| **Node Tracker** | Search, track, and filter mesh nodes on the map with persistent chips |
| **Node Map** | Leaflet map showing node position from GPS with tracker overlay |
| **Wi-Fi Hotspot** | Hotspot (hostapd) status, connected clients, and signal strength |
| **Space Tracker** | Satellite positions, upcoming passes, launches, space weather |
| **NTP / Time Sync** | GPS-disciplined NTP status and chrony source monitoring |
| **Alerts** | Threshold-based alerts for CPU, disk, crashes |
| **Emergency Broadcasts** | Mesh-wide priority message log |
| **Shared Files** | Files available via RNS.Resource transfer |
| **Sensors** | Live sensor readings with sparkline trend charts |
| **Plugins** | Enabled plugin list with status |
| **Configuration** | Read-only view of current config |

### Interface Management

The dashboard lets you enable/disable Reticulum network interfaces (TCP, LoRa, I2P, etc.) directly from the UI. When you toggle an interface:

1. The dashboard updates the service-managed Reticulum interface state through the durable,
   atomic Reticulum-config writer; it does not rewrite the root-owned application configuration
2. A "Configuration changed" banner appears with a **Restart Services** button
3. Clicking restart sends an enumerated request to the root-owned control broker; it does
   not invoke `sudo` or execute a script from the service-owned tree

The socket-activated broker checks the caller UID, accepts at most 4 KiB, and can execute
only fixed operations with validated arguments. A successful dashboard request returns
HTTP 202 plus an operation ID. If the broker is unavailable, restart manually:

```bash
sudo systemctl restart rnsd reticulumpi
```

### Dashboard Authentication

The dashboard requires password authentication. On first run, a random password is written
only to `/var/lib/reticulumpi/.config/reticulumpi/dashboard_password.txt` with mode `0600`;
plaintext secrets
are never logged. After bootstrap login, the dashboard requires a durable password change
before deleting that file or enabling normal API/WebSocket access. Password rotation
invalidates existing sessions and their WebSockets.

For permanent configuration, generate a hash and add it to `config.yaml`:

```bash
# Generate a password hash interactively
reticulumpi --hash-password
```

```yaml
plugins:
  web_dashboard:
    enabled: true
    host: "0.0.0.0"
    port: 8080
    password_hash: "scrypt:..."
    session_timeout: 3600
    local_api:
      enabled: false
      # Defaults to a mode-0600 token at /run/reticulumpi/local_api.token.
      # The value rotates on every dashboard start.
```

For unattended provisioning, place only the generated `password_hash` in the root-owned
`/etc/reticulumpi/config.yaml` (mode `0640`, group `reticulumpi`). The legacy plaintext
`RETICULUMPI_DASHBOARD_PASSWORD` and `password` inputs remain compatibility shims through
0.4.x, emit a critical warning, and must not be used for new deployments.

### SSL/TLS

For HTTPS, provide certificate and key paths:

```yaml
plugins:
  web_dashboard:
    enabled: true
    ssl:
      enabled: true
      cert_file: /etc/ssl/certs/reticulumpi.pem
      key_file: /etc/ssl/private/reticulumpi.key
      extra_hostnames: ["reticulumpi.example.net"]
```

Both files and an explicit expected SAN are required for operator-supplied TLS. Private keys
must grant no access broader than mode `0600` or root-owned/group-readable `0640`. Managed certificates
are renewed before expiry; unsafe files, validity errors, and certificate/key mismatches fail
closed. For TLS termination at a reverse proxy, configure the proxy's exact trusted CIDRs as
described in [Dashboard Security](docs/dashboard-security.md#tls).

## Docker

Docker is the easiest way to run ReticulumPi without installing anything on the host. The
image definition targets native ARM64 and AMD64 builds from one wheel. A stable multi-architecture
image is not considered released until its digests, scans, persistence checks, and signed
candidate evidence appear in the matching release-verification record.

### Quick Start

```bash
# Build the single wheel consumed by the image
make dev
make package-wheel
mkdir -p docker/config
cp docker/config.example.yaml docker/config/config.yaml
cd docker

# Edit the container-specific /data paths in the provided template
nano config/config.yaml

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

# Rebuild the wheel and image after code changes
cd ..
make package-wheel
cd docker
docker compose up --build -d

# Stop the node
docker compose down

# Stop and remove all data (identity, LXMF storage)
docker compose down -v
```

### Configuration

The container mounts only the ignored `docker/config/config.yaml` file at
`/config/config.yaml`. Create it from the tracked, secret-free template, then edit your local
copy:

```bash
mkdir -p docker/config
cp docker/config.example.yaml docker/config/config.yaml
nano docker/config/config.yaml
```

The container sets `HOME=/data`, its XDG config/data/state roots below `/data`, and
`XDG_CACHE_HOME=/cache`. Identity, Reticulum configuration, NomadNet content, dashboard
secrets, and application databases therefore share the `reticulumpi-data` volume. `/cache`
is a bounded disposable tmpfs that is recreated with the container. The host configuration mount is
read-only. To customize Reticulum configuration:

```bash
# Optional: provide a custom Reticulum config
docker compose run --rm --entrypoint /bin/sh reticulumpi -c \
  "cp /dev/stdin /data/.reticulum/config" < ../config/reticulum/config.example
```

Compose enforces the fixed UID/GID, read-only image root, dropped capabilities,
`no-new-privileges`, PID limit, bounded `/tmp`, `/cache`, and `/run/reticulumpi` tmpfs mounts, and a
60-second stop deadline. Do not add `privileged: true`; pass only required devices and groups.

Or exec into a running container:

```bash
docker exec -it docker-reticulumpi-1 sh
```

### Networking

Host networking is enabled by default (`network_mode: host`), which is required for Reticulum's AutoInterface (IPv6 multicast discovery), UDP, and TCP interfaces. This means the container shares your host's network stack -- no port mapping needed.

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
  - ./config/config.yaml:/config/config.yaml:ro
  - ./my_plugins:/plugins:ro
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

NomadNet data (identity, pages, files) is persisted with the rest of `HOME` in the
`reticulumpi-data` volume. Edit pages by exec-ing into the container:

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

The Make targets build one project wheel, install its runtime dependencies from the hashed
universal production profile in a Bookworm/Python 3.11 container, install that wheel, and run
the test suite.

## Configuration

ReticulumPi uses two separate config files:

### App Config (`config.yaml`)

Controls the application, plugins, and identity. Production uses the root-owned
`/etc/reticulumpi/config.yaml`; a non-service CLI invocation may explicitly select a
per-user development file.

```yaml
reticulumpi:
  # A friendly name for this node — used by NomadNet, LXMF Echo, and announces.
  # Defaults to "ReticulumPi-<hostname>" if not set, so every node is unique.
  node_name: MyCabin

  # Connect to running rnsd (true) or open interfaces directly (false)
  # Use false for a dedicated node; use true if also running Sideband, NomadNet, etc.
  use_shared_instance: false

  # Persistent cryptographic identity file (created automatically)
  identity_path: /var/lib/reticulumpi/.config/reticulumpi/identity

  # Reticulum log level: 0=critical ... 4=info ... 7=extreme
  log_level: 4

  # Additional directories to scan for plugins
  plugin_paths:
    - /opt/reticulumpi-plugins

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

### Reticulum Config (`/var/lib/reticulumpi/.reticulum/config` in production)

Standard Reticulum configuration. The dashboard can modify only supported interface fields
through the durable config writer; application-level dashboard overrides remain in the
separate allowlisted runtime state file. See the
[Reticulum manual](https://reticulum.network/manual/interfaces.html) for full documentation.

The included example enables AutoInterface and TCP Server by default. It also contains documented, commented-out blocks for every supported interface type: TCP Client, RNode LoRa, RNode Multi, Serial, KISS TNC, AX.25 KISS, UDP, I2P, Pipe, and Backbone. See the [Connectivity Guide](docs/connectivity-guide.md) for details on each.

## Connectivity Guide

Reticulum can communicate over virtually any medium -- WiFi, Ethernet, LoRa radio, serial, packet radio, I2P, and more. You can enable multiple interfaces simultaneously and Reticulum automatically meshes traffic across all of them.

| Connection Method | Cost | Range | Best For |
|---|---|---|---|
| WiFi/Ethernet (Auto) | Free | LAN | Getting started |
| TCP Client/Server | Free | Global | Internet gateway |
| RNode LoRa | $15--150 | 1--100+ km | Off-grid mesh |
| Meshtastic | $20--60 | 1--50+ km | LoRa mesh with existing Meshtastic network |
| MeshCore | $20--60 | 1--50+ km | LoRa mesh with MeshCore network |
| RTL-SDR | $25--35 | Receive only | Spectrum, ADS-B, AIS, ACARS, weather radio, NOAA satellite, radiosondes, ISM devices, FM/AM radio |
| Serial / HC-12 | $5--50 | Varies | Cheap radio links |
| KISS TNC | $35--500 | 10--50 km | Amateur radio |
| I2P | Free | Global | Anonymous networking |
| Yggdrasil | Free | Global | Encrypted IPv6 overlay |

> **Best starter LoRa pick:** LilyGO T-Beam v1.1 (~$25) -- GPS, battery holder, excellent community support. Flash with RNode firmware and plug into USB.

For complete hardware recommendations, configuration examples, frequency guides, range expectations, and troubleshooting, see **[docs/connectivity-guide.md](docs/connectivity-guide.md)**.

## Built-in Plugins

ReticulumPi ships with the plugins in the code-derived
[built-in inventory](docs/generated-code-reference.md#built-in-plugins). Enable any
combination in your `config.yaml`:

**Core & Messaging**

| Plugin | Description |
|--------|-------------|
| **heartbeat_announce** | Periodic network presence announcements |
| **message_echo** | LXMF echo responder + auto propagation node selection |
| **info_bot** | LXMF command bot (`!weather`, `!mesh`, `!help`) |
| **system_monitor** | CPU, temp, memory, disk metric collection |
| **messaging_hub** | Unified message store + chat UI (LXMF + Meshtastic + MeshCore) |
| **alert_system** | LXMF threshold alerts (CPU, disk, crashes) |
| **emergency_broadcast** | Flood-style mesh-wide priority messaging |
| **file_transfer** | File sharing via RNS.Resource |
| **remote_control** | Remote management over RNS Links (no SSH needed) |

**Mesh Networking**

| Plugin | Description |
|--------|-------------|
| **network_map** | Passive mesh topology mapper (SQLite, server-side pagination) |
| **mesh_telemetry** | Distributed node metrics sharing |
| **transport_monitor** | TCP hub failover + auto-discovery + hub exchange |
| **connectivity_monitor** | Transport health + routing diagnostics |
| **path_warmer** | Proactive path refreshing for known nodes |
| **transport_health** | Transport relay node reliability tracking |
| **meshtastic_gateway** | Meshtastic LoRa mesh bridge (serial + MQTT) |
| **meshtastic_responder** | Auto-replies to Meshtastic DMs with configurable commands |
| **meshcore_gateway** | MeshCore LoRa mesh bridge with MQTT |
| **meshcore_observer** | MeshCore companion observer for letsmesh.net analyzer |
| **mesh_bridge** | Bidirectional relay between Meshtastic and MeshCore networks |

**LoRa & Radio (RTL-SDR)**

| Plugin | Description |
|--------|-------------|
| **lora_diagnostics** | LoRa traffic monitoring, announce beaconing, and peer tracking |
| **lora_scanner** | Dedicated RTL-SDR LoRa-band scanner with channel analysis |
| **lora_link_tester** | Meshtastic LoRa link quality tester (dedicated radio) |
| **lora_decode** | LoRa PHY-layer codec -- Gray, interleave, Hamming FEC, whiten, CRC |
| **lora_analysis** | LoRaWAN-aware signal processing utilities |
| **spectrum_scanner** | RTL-SDR spectrum sweep + waterfall feed |
| **fm_receiver** | FM/AM radio receiver via RTL-SDR with favorites and recording |
| **adsb_radar** | ADS-B aircraft tracker using RTL-SDR and dump1090 |

**Signal Intelligence (RTL-SDR)**

| Plugin | Description |
|--------|-------------|
| **ism_decoder** | ISM band device decoder (weather stations, TPMS, IoT) via rtl_433 |
| **acars_decoder** | ACARS aircraft message decoder via acarsdec |
| **ais_receiver** | AIS marine vessel tracker via AIS-catcher or rtl_ais |
| **noaa_apt_decoder** | NOAA 15/18/19 weather satellite APT image capture and decode |
| **radiosonde_tracker** | RS41/DFM weather balloon tracker via rs41mod |
| **weather_alert** | NOAA Weather Radio SAME alert monitor via multimon-ng |

**Hardware & Sensors**

| Plugin | Description |
|--------|-------------|
| **sensor_framework** | DS18B20, BME280, ADC, command sensors + logging |
| **gps_telemetry** | NMEA GPS receiver telemetry |
| **ntp_server** | GPS-disciplined NTP time synchronization via chrony |
| **space_tracker** | Satellite tracking, launch schedule, and space weather |
| **hotspot_monitor** | Wi-Fi hotspot (hostapd) status and connected client monitoring |

**Infrastructure**

| Plugin | Description |
|--------|-------------|
| **web_dashboard** | Real-time monitoring web UI with auth + WebSocket + interface management |
| **nomadnet_server** | NomadNet page server (subprocess manager) |
| **meshchat_server** | MeshChat web UI (subprocess manager) |
| **yggdrasil_transport** | Yggdrasil IPv6 overlay monitoring + auto-RNS interface setup |
| **example_plugin** | Scaffold -- copy to start your own plugin |

For complete configuration options, see **[docs/plugins.md](docs/plugins.md)** and the annotated `config/reticulumpi/config.example.yaml`.

## Node Identities

A deployed ReticulumPi node has multiple Reticulum identities. Each LXMF plugin creates its own identity so that plugins can run independently without destination collisions.

| Service | Purpose | Identity File |
|---|---|---|
| **reticulumpi** (node) | Shared node identity for RNS destinations (heartbeat, mesh telemetry, network map, remote control, file transfer, sensors, emergency) | `/var/lib/reticulumpi/.config/reticulumpi/identity` |
| **message_echo** | Echo bot -- replies to LXMF messages | `/var/lib/reticulumpi/.local/share/reticulumpi/lxmf/identity` |
| **info_bot** | Info bot -- responds to `!` commands | `/var/lib/reticulumpi/.local/share/reticulumpi/info_bot_lxmf/identity` |
| **alert_system** | LXMF alerts -- separate identity for sending | Creates its own `RNS.Identity()` at runtime |
| **meshtastic_gateway** | Meshtastic<>LXMF bridge -- receives LXMF messages to forward | `/var/lib/reticulumpi/.local/share/reticulumpi/meshtastic_gw_lxmf/identity` |
| **meshtastic_gateway** (MQTT node) | Persistent Meshtastic node number for MQTT mode | `/var/lib/reticulumpi/.local/share/reticulumpi/meshtastic_gw_lxmf/meshtastic_node_num` |
| **NomadNet daemon** | Page server -- browsable via NomadNet TUI | `/var/lib/reticulumpi/.nomadnet/storage/identity` |
| **NomadNet TUI** | Browse-only client (no node hosting) | `/var/lib/reticulumpi/.nomadnet-tui/storage/identity` |
| **MeshChat** | Web UI chat over LXMF | `/var/lib/reticulumpi/meshchat/storage/identity` |

> **Note:** Reticulum interfaces (RNode, TCP, I2P) are transport pipes -- they do not have their own identities or addresses. Identities belong to destinations. The Transport Instance and Probe Responder hashes visible in `rnstatus` are destinations that *use* interfaces, not properties of the interfaces themselves.

To find your LXMF plugin addresses, check the startup logs:

```bash
sudo journalctl -u reticulumpi -g "active at" --no-pager
```

Or compute them from identity files:

```bash
sudo -u reticulumpi /srv/reticulumpi/current/.venv/bin/python3 -c "
import RNS
RNS.Reticulum('/var/lib/reticulumpi/.reticulum', loglevel=RNS.LOG_CRITICAL)
for label, path in [
    ('message_echo', '/var/lib/reticulumpi/.local/share/reticulumpi/lxmf/identity'),
    ('info_bot', '/var/lib/reticulumpi/.local/share/reticulumpi/info_bot_lxmf/identity'),
    ('NomadNet daemon', '/var/lib/reticulumpi/.nomadnet/storage/identity'),
]:
    i = RNS.Identity.from_file(path)
    d = RNS.Destination(i, RNS.Destination.IN, RNS.Destination.SINGLE, 'lxmf', 'delivery')
    print(f'{label:20s} {RNS.prettyhexrep(d.hash)}')
"
```

The **message_echo** and **info_bot** LXMF addresses are the ones to give to other users -- they can message them from [Sideband](https://unsigned.io/sideband/) or MeshChat.

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

Every plugin gets access to `self.rns` (Reticulum), `self.identity` (node key), `self.config`
(YAML config), `self.event_bus` (pub/sub), and `self.log` (logger). New code should use
`self.app.get_ready_plugin("name")` for hard runtime dependencies. `get_plugin()` remains a
compatibility API through 0.4.x but does not assert readiness.

Copy the scaffold to get started: `cp plugins/example_plugin.py ~/my_plugins/my_plugin.py`

For the complete guide covering LXMF messaging, SQLite storage, background threads, event handling, testing patterns, and dashboard integration, see **[docs/plugin-development.md](docs/plugin-development.md)**.

## REST API

The web dashboard exposes the code-derived
[HTTP/WebSocket route inventory](docs/generated-code-reference.md#dashboard-routes). Protected
routes require a session cookie obtained from `POST /api/auth/login`, subject to the scoped
local-token exceptions documented in the API reference.

### Key Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/auth/login` | Authenticate and receive session cookie |
| `GET` | `/api/status` | Node status, metrics, plugins, interfaces |
| `GET` | `/api/interfaces/config` | List all Reticulum interfaces from config file |
| `POST` | `/api/interfaces/{name}/toggle` | Toggle interface enabled/disabled in config |
| `POST` | `/api/interfaces/add` | Add a new interface section to config |
| `POST` | `/api/services/restart` | Queue a broker-controlled service restart (HTTP 202) |
| `GET` | `/api/mesh/nodes` | Paginated mesh node list (`page`, `per_page`, `sort`, `order`, `search`) |
| `GET` | `/api/reachability` | Node reachability scores (`hashes` for targeted, or paginated) |
| `GET` | `/api/routing` | Paginated routing table with sort/filter |
| `GET` | `/api/sensors/history` | Time-series sensor data |
| `GET` | `/api/messages` | Message history with transport/direction filters |
| `POST` | `/api/messages/send` | Send a message via LXMF, Meshtastic, or MeshCore |
| `GET` | `/api/messages/conversations` | Conversation threads with unread counts |
| `GET` | `/api/meshtastic/status` | Meshtastic connection status and node list |
| `GET` | `/api/meshcore/status` | MeshCore connection status and contacts |
| `GET,POST` | `/api/mesh_bridge/{status,running}` | Mesh bridge relay status and pause/resume |
| `GET` | `/api/lora` | LoRa diagnostics (peers, stats, announce mode) |
| `GET,POST` | `/api/link_tester` | LoRa link quality test start/stop/results |
| `GET` | `/api/gps` | GPS fix, satellite count, accuracy |
| `GET` | `/api/ntp` | NTP sync status and chrony sources |
| `GET` | `/api/adsb` | ADS-B aircraft positions |
| `GET` | `/api/space` | Satellite positions, launches, space weather |
| `POST` | `/api/radio/tune` | Tune FM/AM radio to frequency |
| `GET` | `/api/radio/audio` | Audio stream from FM receiver |
| `GET,POST` | `/api/radio/favorites` | FM station favorites list and add |
| `POST,GET` | `/api/radio/recording` | Start/stop recording, list/download recordings |
| `GET` | `/api/spectrum/presets` | Spectrum scanner preset list |
| `GET` | `/api/config` | Sanitized read-only config view |
| `WS` | `/ws` | WebSocket for real-time updates (metrics, mesh deltas, messages, spectrum, signals) |

For complete API documentation, see **[docs/api-reference.md](docs/api-reference.md)**.

## Project Structure

```
reticulumPi/
├── pyproject.toml                  # Dependencies and entry point
├── Makefile                        # install, dev, test, lint, format targets
├── LICENSE                         # MIT license
├── CHANGELOG.md                    # Version history
├── .github/workflows/ci.yml       # GitHub Actions: Python 3.11-3.14 + release gates
├── CONTRIBUTING.md                 # How to contribute
├── SECURITY.md                     # Security policy and best practices
├── docs/
│   ├── plugins.md                  # Built-in plugin configuration guide
│   ├── generated-code-reference.md # Generated plugin/event/route/default inventory
│   ├── plugin-development.md       # Plugin development guide (full walkthrough)
│   ├── api-reference.md            # REST API & WebSocket documentation
│   ├── connectivity-guide.md       # Hardware, radio, and interface guide
│   ├── install-layout.md           # Detailed install directory & file flow docs
│   ├── troubleshooting.md          # FAQ and common issues
│   └── solar-power-build.md        # Off-grid solar-powered node hardware guide
├── config/
│   ├── nomadnet/
│   │   └── pages/                  # NomadNet pages (.mu files)
│   │       ├── index.mu            # Landing page with navigation
│   │       ├── help.mu             # Markup reference
│   │       └── status.mu           # Dynamic system + network status page
│   ├── reticulum/
│   │   ├── config.example          # Reticulum interface config (all interfaces)
│   │   └── config.minimal          # Minimal safe config (AutoInterface only)
│   ├── reticulumpi/
│   │   ├── config.example.yaml     # App + plugin config (all plugins documented)
│   │   └── offline_profile.yaml    # Narrow forced-offline runtime overlay
├── src/reticulumpi/
│   ├── __init__.py                 # Package metadata
│   ├── admin_cli.py                # Transactional root administrator
│   ├── control_broker.py           # Enumerated privileged operation broker
│   ├── migrations.py               # Transactional SQLite migrations
│   ├── _paths.py                   # Path resolution utilities
│   ├── announce_dispatcher.py      # Centralized announce handler multiplexer
│   ├── app.py                      # Core orchestrator (plugin hot-reload)
│   ├── cli.py                      # CLI entry point (+ remote control client)
│   ├── config.py                   # YAML config loader with validation
│   ├── event_bus.py                # Thread-safe publish/subscribe event bus
│   ├── events.py                   # Event constants (generated inventory in docs/)
│   ├── geo.py                      # Shared geodetic utilities (haversine, bearing)
│   ├── identity_manager.py         # Persistent identity
│   ├── internet_probe.py           # Periodic internet connectivity probe with hysteresis
│   ├── mtu.py                      # MTU calculation for different interfaces
│   ├── plugin_base.py              # Abstract plugin base class
│   ├── plugin_loader.py            # Plugin discovery
│   ├── reachability.py             # Path discovery and scoring
│   ├── remote_client.py            # Remote control CLI client
│   ├── rns_config.py               # Reticulum config parser (line-preserving)
│   ├── rtlsdr.py                   # RTL-SDR device enumeration + serial resolver
│   ├── sdr_scheduler.py            # Priority-based SDR dongle time-sharing scheduler
│   ├── data/
│   │   └── community_hubs.yaml     # Curated community TCP hub list for auto-discovery
│   └── builtin_plugins/            # Built-in plugins (shipped with package)
│       ├── heartbeat_announce.py   # Network presence announcer
│       ├── message_echo.py         # LXMF echo responder
│       ├── info_bot.py             # LXMF command bot (weather, etc.)
│       ├── system_monitor.py       # System metrics collector
│       ├── messaging_hub.py        # Unified messaging store + adapters
│       ├── alert_system.py         # LXMF threshold alerts
│       ├── emergency_broadcast.py  # Mesh-wide flood-style messaging
│       ├── file_transfer.py        # File transfer via RNS.Resource
│       ├── remote_control.py       # Remote management over RNS Links
│       ├── network_map.py          # Passive mesh topology mapper
│       ├── mesh_telemetry.py       # Distributed node metrics sharing
│       ├── transport_monitor.py    # TCP hub health + failover + auto-discovery
│       ├── connectivity_monitor.py # Transport health + routing diagnostics
│       ├── path_warmer.py          # Proactive path refreshing for known nodes
│       ├── transport_health.py     # Transport relay node reliability tracking
│       ├── meshtastic_gateway.py   # Meshtastic LoRa ↔ LXMF bridge
│       ├── meshtastic_responder.py # Meshtastic DM auto-reply bot
│       ├── meshcore_gateway.py     # MeshCore LoRa mesh bridge (MQTT)
│       ├── meshcore_observer.py    # MeshCore letsmesh.net observer
│       ├── mesh_bridge.py          # Meshtastic ↔ MeshCore bidirectional relay
│       ├── lora_diagnostics.py     # LoRa peer tracking + announce beaconing
│       ├── lora_scanner.py         # RTL-SDR LoRa-band spectrum scanner
│       ├── lora_link_tester.py     # RF link quality probe/ACK measurements
│       ├── lora_decode.py          # LoRa PHY-layer codec (Gray/Hamming/CRC)
│       ├── lora_analysis.py        # LoRaWAN-aware signal processing
│       ├── spectrum_scanner.py     # RTL-SDR sweep-based waterfall
│       ├── fm_receiver.py          # FM/AM radio receiver via rtl_fm
│       ├── adsb_radar.py           # ADS-B aircraft tracker (dump1090)
│       ├── signal_plugin_base.py   # Base class for SDR-scheduler-managed signal plugins
│       ├── ism_decoder.py          # ISM band decoder via rtl_433
│       ├── acars_decoder.py        # ACARS aircraft message decoder via acarsdec
│       ├── ais_receiver.py         # AIS marine vessel tracker via AIS-catcher
│       ├── noaa_apt_decoder.py     # NOAA APT weather satellite image capture
│       ├── radiosonde_tracker.py   # RS41/DFM weather balloon tracker
│       ├── weather_alert.py        # NOAA SAME weather alert monitor
│       ├── hotspot_monitor.py      # Wi-Fi hotspot (hostapd) status monitor
│       ├── gps_telemetry.py        # NMEA GPS receiver telemetry
│       ├── space_tracker.py        # Satellite tracking + space weather (SGP4)
│       ├── ntp_server.py           # GPS-disciplined NTP via chrony
│       ├── sensor_framework.py     # Config-driven sensor reading + logging
│       ├── nomadnet_server.py      # NomadNet page server manager
│       ├── meshchat_server.py      # MeshChat web UI manager
│       ├── yggdrasil_transport.py  # Yggdrasil IPv6 overlay monitor + auto-RNS setup
│       ├── web_dashboard/          # Secure web dashboard (aiohttp)
│       │   ├── plugin.py           # Plugin class + aiohttp server lifecycle
│       │   ├── server.py           # Server initialization + middleware
│       │   ├── auth.py             # Session auth + scrypt password hashing
│       │   ├── api.py              # Core REST API handlers
│       │   ├── api_services.py     # Service API handlers (LoRa, messaging, alerts, sensors)
│       │   ├── api_mesh.py         # Mesh network API (nodes, routing, reachability)
│       │   ├── api_interfaces.py   # Interface management API (toggle, add)
│       │   ├── api_radio.py        # Radio API (FM tuning, favorites, recording, audio)
│       │   ├── websocket_handler.py # WebSocket broadcast (delta mode)
│       │   ├── broadcast_registry.py # Tiered plugin data collection for WebSocket
│       │   ├── ssl_utils.py        # SSL/TLS certificate utilities
│       │   └── static/             # Frontend assets
│       │       ├── index.html      # Dashboard layout (36+ sections)
│       │       ├── app.js          # Core dashboard logic (~1,800 lines)
│       │       ├── mesh.js         # Mesh network panel
│       │       ├── routing.js      # Routing table panel
│       │       ├── lora.js         # LoRa diagnostics panel
│       │       ├── lora_spectrum.js # LoRa spectrum panel
│       │       ├── spectrum.html   # Full-page spectrum waterfall view
│       │       ├── spectrum.js     # SDR spectrum waterfall panel
│       │       ├── spectrum_page.js # Standalone spectrum page logic
│       │       ├── spectrum_common.js # Shared spectrum rendering
│       │       ├── meshtastic.js   # Meshtastic panel
│       │       ├── meshcore.js     # MeshCore panel
│       │       ├── mesh_bridge_panel.js # Mesh bridge panel
│       │       ├── messages_panel.js # Messages tab controller
│       │       ├── messages_lxmf.js # LXMF messages tab
│       │       ├── messages_meshtastic_lora.js # Meshtastic messages tab
│       │       ├── messages_meshcore.js # MeshCore messages tab
│       │       ├── mqtt_feed.js    # MQTT feed tab
│       │       ├── link_tester.js  # LoRa link tester panel
│       │       ├── adsb.js         # ADS-B radar panel
│       │       ├── ais.js          # AIS marine vessel panel
│       │       ├── acars.js        # ACARS decoder panel
│       │       ├── weather_alert.js # Weather alert panel
│       │       ├── radiosonde.js   # Radiosonde tracker panel
│       │       ├── noaa.js         # NOAA satellite panel
│       │       ├── hotspot.js      # Wi-Fi hotspot panel
│       │       ├── node_tracker.js # Node tracker panel
│       │       ├── gps.js          # GPS telemetry panel
│       │       ├── map.js          # Leaflet node map
│       │       ├── space.js        # Space tracker panel
│       │       ├── radio.js        # FM radio panel
│       │       ├── ntp.js          # NTP time sync panel
│       │       ├── login.html      # Login page
│       │       ├── login.js        # Login form handler (CSP-compliant)
│       │       └── style.css       # Dashboard styles
│       └── example_plugin.py       # Scaffold — copy to start your own plugin
├── plugins/
│   └── example_plugin.py           # Scaffold copy (for easy access)
├── scripts/
│   ├── bootstrap.sh                # Compatibility launcher for reticulumpi-admin
│   ├── update.sh                   # Compatibility launcher for transactional upgrade
│   ├── simulate_offline.sh         # Broker-only firewall/runtime-overlay helper
│   ├── nomadnet-tui.sh             # Launch NomadNet TUI over SSH
│   └── meshchat_launcher.py        # MeshChat wrapper (timeout patching + logging)
├── systemd/
│   ├── reticulumpi.service         # Systemd unit file (security-hardened)
│   └── rnsd.service                # Reticulum daemon (for shared instance mode)
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── entrypoint.sh              # Container entrypoint (starts rnsd + reticulumpi)
└── tests/                          # Pytest unit, integration, security, and regression suite
    ├── conftest.py
    ├── test_app.py                  # App orchestrator tests
    ├── test_cli.py                  # CLI entry point tests
    ├── test_config.py
    ├── test_config_validation.py    # Config error-path tests
    ├── test_plugin_base.py          # Base class helper tests
    ├── test_plugin_loader.py
    ├── test_event_bus.py            # Event bus thread-safety tests
    ├── test_announce_dispatcher.py  # Announce dispatcher tests
    ├── test_reachability.py         # Path discovery + scoring tests
    ├── test_rns_config.py           # Config parser round-trip tests
    ├── test_rtlsdr.py              # RTL-SDR device enumeration tests
    ├── test_sdr_scheduler.py        # SDR scheduler priority + preemption tests
    ├── test_internet_probe.py       # Internet probe hysteresis tests
    ├── test_geo.py                  # Geodetic utility tests
    ├── test_mtu.py                  # MTU calculation tests
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
    ├── test_transport_monitor.py    # Transport hub health + failover tests
    ├── test_connectivity_monitor.py # Connectivity + routing data tests
    ├── test_path_warmer.py          # Path warming + ensure_path tests
    ├── test_transport_health.py     # Transport node tracking + SQLite tests
    ├── test_meshtastic_gateway.py   # Meshtastic gateway serial + MQTT tests
    ├── test_meshtastic_responder.py # Meshtastic auto-reply tests
    ├── test_meshchat_announce_mux.py # MeshChat announce mux tests
    ├── test_messaging_hub.py        # Messaging hub + adapters tests
    ├── test_meshcore_gateway.py     # MeshCore gateway tests
    ├── test_meshcore_observer.py    # MeshCore observer tests
    ├── test_mesh_bridge.py          # Mesh bridge relay + loop prevention tests
    ├── test_lora_diagnostics.py     # LoRa peer tracking tests
    ├── test_lora_scanner.py         # LoRa scanner tests
    ├── test_lora_link_tester.py     # Link tester probe/ACK tests
    ├── test_lora_decode.py          # LoRa codec (Gray/Hamming/CRC) tests
    ├── test_lora_analysis.py        # LoRa signal analysis tests
    ├── test_spectrum_scanner.py     # Spectrum scanner tests
    ├── test_fm_receiver.py          # FM receiver tests
    ├── test_adsb_radar.py           # ADS-B radar tests
    ├── test_ism_decoder.py          # ISM decoder tests
    ├── test_hotspot_monitor.py      # Hotspot monitor tests
    ├── test_gps_telemetry.py        # GPS telemetry tests
    ├── test_space_tracker.py        # Satellite tracking tests
    ├── test_yggdrasil_transport.py  # Yggdrasil transport tests
    ├── test_routing_api.py          # Routing API endpoint tests
    ├── test_web_dashboard.py        # Dashboard auth + API tests
    ├── test_websocket_handler.py    # WebSocket broadcast + delta tests
    ├── test_broadcast_registry.py   # Broadcast registry tests
    ├── test_api_write_endpoints.py  # API write endpoint tests
    └── test_server_middleware.py    # Server middleware + auth bypass tests
```

## CLI Usage

```
reticulumpi [--version] [--config PATH] [--reticulum-config DIR] [--log-level 0-7]
            [--log-format {text,json}] [--check] [--list-plugins]
            [--remote HASH] [--command CMD] [--timeout SECS]
            [--backup-identity PATH] [--restore-identity PATH] [--hash-password]
            [--mesh-bridge {status,pause,resume}]
```

| Flag | Description |
|------|-------------|
| `--version`, `-V` | Show version and exit |
| `--config`, `-c` | Path to app config YAML (systemd passes `/etc/reticulumpi/config.yaml`; an interactive CLI checks the current user's development config) |
| `--reticulum-config` | Override Reticulum config directory |
| `--log-level` | Override log level: 0=critical, 1=error, 2-3=warning, 4=info, 5-7=debug |
| `--log-format` | Log output format: `text` (default) or `json` for structured logging |
| `--check` | Validate configuration and plugin discovery without starting (dry run) |
| `--list-plugins` | List all discoverable plugins and exit |
| `--remote HASH` | Connect to a remote node's `remote_control` plugin over Reticulum |
| `--command CMD` | Execute a single remote command and exit (use with `--remote`) |
| `--timeout SECS` | Remote connection timeout in seconds (default: 30) |
| `--backup-identity PATH` | Back up the node identity file to the given path |
| `--restore-identity PATH` | Restore a node identity from the given path |
| `--hash-password` | Hash a password for use in web_dashboard config (interactive) |
| `--mesh-bridge` | Control the Meshtastic ↔ MeshCore bridge: `status`, `pause`, or `resume` |

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
6. Start plugins in validated dependency order; API v2 plugins publish `PLUGIN_STARTED` only
   after `mark_ready()`, while compatible API v1 plugins become ready when `start()` returns
7. Wait for SIGTERM/SIGINT
8. Request stop and run managed cleanup in reverse order (publishes `PLUGIN_STOPPED` after
   cleanup; timeouts remain `HUNG` and block same-process re-enable)

Plugins can be enabled/disabled at runtime via `app.enable_plugin(name)` / `app.disable_plugin(name)` (used by the remote control plugin).

### Key Design Decisions

- **Line-preserving config parser** (`rns_config.py`): Reticulum uses an INI-like format with `[[double brackets]]` for interfaces. Python's `configparser` can't represent this and drops comments on round-trip. ReticulumPi uses a custom line-based parser that preserves every byte of the original file except the specific values it modifies. Durable writes use a same-directory temporary file, `fsync`, atomic replacement, and directory `fsync`.

- **Announce dispatcher** (`announce_dispatcher.py`): A centralized announce handler multiplexer replaces per-plugin RNS callback registration. Each subscriber has a bounded mailbox and an isolated daemon worker, so a full or hung subscriber can be disabled without blocking healthy delivery or interpreter shutdown.

- **Broadcast registry** (`broadcast_registry.py`): A declarative plugin data collection system for WebSocket broadcasting. Each plugin sets `broadcast_tier` and `broadcast_keys` class attributes and optionally overrides `broadcast_snapshot()`. The registry iterates plugins by tier while respecting a configurable time budget, keeping the broadcast loop responsive even as plugins grow.

- **WebSocket delta broadcasting**: The mesh network can have 10,000+ known nodes. Instead of broadcasting the full list every cycle, the WebSocket sends `{known_nodes: count, version: N, recent_announces: [...]}`. The frontend re-fetches the current page only when the version changes.

- **Targeted reachability scoring**: Instead of scoring all known nodes (expensive path lookups), the frontend sends only the hashes of nodes currently visible on screen. The API scores just those, keeping response times fast even with large networks.

- **Packaged frontend**: Panel-specific source modules are compiled into verified,
  content-addressed dashboard, spectrum, and login bundles. Installations are Node-free;
  all optional panel chunks are gated by plugin availability and panel opening or proximity.
  See [the asset pipeline](docs/dashboard-assets.md).

- **SDR dongle scheduler** (`sdr_scheduler.py`): A priority-based time-sharing system that lets multiple signal plugins share a single RTL-SDR dongle. Three priority tiers -- critical (weather alerts), scheduled (satellite passes, radiosonde windows), and background (continuous decoders like AIS, ACARS) -- ensure safety-of-life signals always preempt background monitoring. Plugins implement `SignalPluginBase` and the scheduler handles dongle lifecycle, preemption, and cooldown between handoffs.

- **Internet probe** (`internet_probe.py`): A lightweight connectivity monitor using TCP connect checks with asymmetric hysteresis (multiple failures to go offline, single success to recover). Publishes `INTERNET_ONLINE` / `INTERNET_OFFLINE` events so plugins can gracefully degrade without internet access.

- **Shared RTL-SDR management** (`rtlsdr.py`): A device enumeration and serial-to-index resolver shared across all SDR-using plugins (spectrum scanner, LoRa scanner, FM receiver, ADS-B radar, AIS, ACARS, etc.), supporting multi-SDR setups where each plugin is assigned a different dongle by serial number.

- **Geodetic utilities** (`geo.py`): Shared haversine distance and bearing calculations used by ADS-B, AIS, GPS, and node map features.

## License

MIT
