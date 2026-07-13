# Built-in Plugins

This guide explains configuration and operation for commonly deployed built-ins. The
complete plugin count, names, versions, descriptions, and source files are generated from
class metadata in the [built-in plugin inventory](generated-code-reference.md#built-in-plugins).
Enable any combination in your `config.yaml`; see the annotated
`config/reticulumpi/config.example.yaml` for syntax.

For writing your own plugins, see the [Plugin Development Guide](plugin-development.md).

## Heartbeat Announce

Periodically announces the node's presence on the Reticulum network. Other nodes running `rnstatus` or transport-aware applications will see your node.

| Option | Default | Description |
|--------|---------|-------------|
| `interval_seconds` | 300 | Seconds between announcements |
| `app_name` | reticulumpi | Application name for the destination |
| `aspects` | [node, heartbeat] | Destination aspects |
| `include_telemetry` | false | Attach hostname, CPU%, memory% to announcement |

## Message Echo

Listens for incoming [LXMF](https://github.com/markqvist/lxmf) messages and replies with an echo. Useful for testing end-to-end connectivity.

The plugin also **automatically selects the nearest LXMF propagation node** for store-and-forward message delivery. On a fresh install, LXMF's built-in auto-selection only considers nodes with trust level `TRUSTED`, which no nodes have by default. This plugin listens for propagation node announces and picks the closest active one by hop count, enabling offline message delivery without manual configuration. The selected node is also written to NomadNet's peersettings so the daemon and TUI benefit too.

| Option | Default | Description |
|--------|---------|-------------|
| `display_name` | \<node_name\> Echo | Name shown to message senders |
| `storage_path` | /var/lib/reticulumpi/.local/share/reticulumpi/lxmf | LXMF message storage directory |

Send a test message from another device using [Sideband](https://unsigned.io/sideband/) or `lxmf_send`.

## Info Bot

Responds to LXMF command messages with information fetched from the internet. Send `!help` to see available commands, or `!weather <city>` to get current weather conditions. Uses the free [Open-Meteo](https://open-meteo.com/) API (no API key required).

| Option | Default | Description |
|--------|---------|-------------|
| `display_name` | \<node_name\> Info | Name shown to message senders |
| `storage_path` | /var/lib/reticulumpi/.local/share/reticulumpi/info_bot_lxmf | LXMF message and identity storage |

Available commands:

| Command | Example | Description |
|---------|---------|-------------|
| `!weather <location>` | `!weather Austin, TX` | Current temperature, conditions, humidity, wind |
| `!mesh` | `!mesh` | Meshtastic gateway status (connection, mode, message counts) |
| `!mesh nodes` | `!mesh nodes` | List discovered Meshtastic nodes (name, ID, SNR) |
| `!help` | `!help` | List available commands |

## System Monitor

Collects system metrics on a timer. Other plugins can read metrics via `app.get_plugin("system_monitor").latest_metrics`.

| Option | Default | Description |
|--------|---------|-------------|
| `collect_interval_seconds` | 60 | Seconds between metric collections |
| `metrics` | all four | List of metrics to collect |

Available metrics: `cpu_percent`, `cpu_temp`, `memory_percent`, `disk_percent`

## NomadNet Page Server

Manages a [NomadNet](https://github.com/markqvist/NomadNet) daemon as a subprocess, serving pages and files over Reticulum. Other NomadNet users can connect to your node to browse content.

**Requires:** `pip install nomadnet` (or `--with-nomadnet` during bootstrap)

**Important:** NomadNet creates its own Reticulum instance, so both reticulumPi and NomadNet must connect to a shared `rnsd` daemon. Set `use_shared_instance: true` in your config when this plugin is enabled.

| Option | Default | Description |
|--------|---------|-------------|
| `config_dir` | /var/lib/reticulumpi/.nomadnet | NomadNet config and storage directory |
| `node_name` | \<node_name\> | NomadNet node name |
| `enable_propagation` | false | Run as an LXMF propagation node |
| `auto_restart` | true | Restart NomadNet if it crashes |
| `max_restarts` | 5 | Maximum restart attempts |

### Dynamic Pages

NomadNet supports executable `.mu` pages -- Python scripts that generate content dynamically on each request. The included `status.mu` page shows live system stats and network status. Create your own by writing a Python script with a shebang, making it executable, and placing it in the pages directory.

### Accessing the TUI over SSH

```bash
sudo -u reticulumpi /srv/reticulumpi/current/.venv/bin/nomadnet \
  --textui \
  --config /var/lib/reticulumpi/.nomadnet-tui \
  --rnsconfig /var/lib/reticulumpi/.reticulum
```

The TUI uses a separate browse-only config directory so the daemon continues serving pages uninterrupted.

## MeshChat Web UI

Manages a [MeshChat](https://github.com/liamcottle/reticulum-meshchat) web UI server as a subprocess. MeshChat provides browser-based messaging over Reticulum/LXMF.

**Requires:** a separately reviewed MeshChat checkout and virtual environment. The
transactional installer does not install or update MeshChat.

Production additionally requires the complete source and virtual-environment tree under a
root-owned, non-writable path (the supported example is
`/srv/reticulumpi-external/meshchat`) and a matching `kind: tree` record in the root-owned
external-artifact manifest. Keep `storage_dir` under `/var/lib/reticulumpi`, outside that
immutable tree. Missing, mutable, writable, moved, or digest-mismatched deployments are rejected
before the plugin is constructed.

**Important:** Set `use_shared_instance: true` when this plugin is enabled.

| Option | Default | Description |
|--------|---------|-------------|
| `install_dir` | required | External MeshChat source directory |
| `host` | 0.0.0.0 | Web UI listen address |
| `port` | 8000 | Web UI port |
| `storage_dir` | \<install_dir\>/storage | MeshChat data/identity storage |
| `health_check_interval` | 10 | Seconds between process health checks |
| `auto_restart` | true | Restart MeshChat if it crashes |
| `max_restarts` | 5 | Maximum restart attempts |
| `link_timeout` | 75 | Seconds to wait for link establishment |
| `path_lookup_timeout` | 15 | Seconds to wait for path discovery |

> **Tip:** MeshChat's built-in link timeout is 15 seconds, which is too short for 3+ hop destinations. The default `link_timeout: 75` covers up to 12 hops.

## Web Dashboard

Secure real-time web UI for monitoring your node. Shows system metrics, plugin status, network interfaces, connectivity health, routing table with charts, transport hub status, mesh nodes, peer telemetry, sensor data, messages, and emergency broadcasts -- all updating live over WebSocket.

**Requires:** `pip install aiohttp` (or `--with-dashboard` during bootstrap)

| Option | Default | Description |
|--------|---------|-------------|
| `host` | 127.0.0.1 | Listen address (`0.0.0.0` to expose on network) |
| `port` | 8080 | Web UI port |
| `session_timeout` | 86400 | Session lifetime in seconds (24h) |
| `max_sessions` | 5 | Maximum concurrent sessions |
| `metrics_interval` | 5 | WebSocket push interval in seconds |
| `max_websocket_clients` | 10 | Maximum concurrent WebSocket connections |

On first start, the generated password is written only to the mode-`0600`
`/var/lib/reticulumpi/.config/reticulumpi/dashboard_password.txt` bootstrap file and is
never logged. Login is
restricted until that bootstrap value is durably replaced through the password-change flow;
the file is removed only after the successful change. See
[Dashboard Operations](dashboard-operations.md) for rotation and recovery.

For the full API reference, see [API Reference](api-reference.md).

## Network Map

Passively monitors all Reticulum announces to build a live map of every reachable node. Tracks destination hashes, hop counts, app names, and announce frequency. Stores history in SQLite. Discovered nodes appear in the web dashboard.

| Option | Default | Description |
|--------|---------|-------------|
| `db_path` | /var/lib/reticulumpi/.local/share/reticulumpi/network_map.db | SQLite database path |
| `max_history_days` | 30 | Days to retain history |

## Mesh Telemetry

Broadcasts your node's system metrics over Reticulum announces. Receiving nodes store peer metrics, creating a distributed monitoring network. No IP connectivity needed.

| Option | Default | Description |
|--------|---------|-------------|
| `announce_interval` | 300 | Seconds between telemetry announces |
| `include_metrics` | all four | List of metrics to broadcast |

Reads from `system_monitor` -- enable both for full functionality.

## Remote Control

Accept authenticated RNS Link connections for remote node management. Only identities in `allowed_identities` can connect. All communication is encrypted end-to-end. No IP, SSH, or VPN required.

| Option | Default | Description |
|--------|---------|-------------|
| `allowed_identities` | [] | List of hex identity hashes authorized to connect |
| `log_buffer_lines` | 500 | Number of log lines to keep in ring buffer |

Available commands: `ping`, `status`, `metrics`, `plugins`, `interfaces`, `config`, `logs`, `announce`, `enable <plugin>`, `disable <plugin>`.

Connect from another machine:

```bash
reticulumpi --remote <destination_hash>              # interactive shell
reticulumpi --remote <destination_hash> --command ping  # single command
```

## Alert System

Sends LXMF messages to configured recipients when thresholds are breached. Monitors CPU temperature, memory, disk usage, plugin crashes, and node reboots.

| Option | Default | Description |
|--------|---------|-------------|
| `recipients` | [] | LXMF address hashes to notify |
| `cooldown_seconds` | 300 | Minimum seconds between duplicate alerts |
| `rules` | cpu_temp>80, disk>90, mem>90 | List of threshold rules |
| `alert_on_plugin_crash` | true | Alert when a plugin crashes |
| `alert_on_reboot` | true | Alert on node reboot detection |

## File Transfer

Send and receive files over Reticulum using `RNS.Resource` for chunked, compressed transfers with integrity checking.

| Option | Default | Description |
|--------|---------|-------------|
| `shared_dir` | /var/lib/reticulumpi/.local/share/reticulumpi/shared_files | Directory for shared files |
| `max_file_size_mb` | 50 | Maximum accepted file size |
| `access_policy` | deny | Authorization mode: `deny`, `allowlist`, or intentional public `open` |
| `allowed_identities` | [] | Exact hashes consulted only when `access_policy: allowlist` |
| `auto_accept` | true | Automatically accept incoming files |

New configurations fail closed with `access_policy: deny`. Use `allowlist` for normal
authenticated exchange. `open` must be an explicit operator decision; an empty allowlist
never silently opens a new endpoint.

## Sensor Framework

Config-driven sensor reading with SQLite/CSV logging and optional mesh broadcast. Supports DS18B20 (1-Wire), BME280 (I2C), ADC (sysfs), and custom shell commands.

**Requires:** `smbus2` for I2C sensors

| Option | Default | Description |
|--------|---------|-------------|
| `read_interval` | 60 | Seconds between sensor reads |
| `sensors` | [] | List of sensor configurations |
| `storage.type` | sqlite | Storage backend: sqlite, csv, or none |
| `storage.retention_days` | 30 | Days to retain readings |
| `broadcast.enabled` | false | Broadcast readings over Reticulum |
| `broadcast.interval` | 300 | Seconds between broadcasts |

Example sensor config:

```yaml
sensors:
  - name: cpu_temp
    driver: command
    command: "cat /sys/class/thermal/thermal_zone0/temp | awk '{printf \"%.1f\", $1/1000}'"
    reading_name: temperature
  - name: outdoor_temp
    driver: ds18b20
    address: "28-0000abcdef"
```

## Emergency Broadcast

Flood-style priority messaging across the mesh. Messages propagate via announce re-broadcasting with TTL decrement. Deduplication via SHA-256 prevents broadcast storms.

| Option | Default | Description |
|--------|---------|-------------|
| `max_ttl` | 5 | Maximum hops for propagation |
| `max_stored_messages` | 100 | Local message buffer size |
| `rebroadcast` | true | Re-broadcast received emergencies |
| `rebroadcast_delay` | 5 | Seconds before re-broadcasting |

## Transport Monitor

Watches TCP transport hub health and automatically connects to fallback hubs when primaries go down. Shows real-time hub status on the dashboard.

| Option | Default | Description |
|--------|---------|-------------|
| `check_interval` | 15 | Seconds between health checks |
| `down_threshold` | 60 | Seconds primaries must be down before fallback |
| `auto_teardown_fallback` | true | Tear down fallback when primary recovers |
| `fallback_hubs` | [] | Ordered list of fallback hubs |

### Auto-Discovery

Automatically maintains a pool of healthy community hub connections with health probing, exponential backoff, and regional diversity.

| Option | Default | Description |
|--------|---------|-------------|
| `auto_discovery.enabled` | false | Enable automatic hub pool |
| `auto_discovery.target_connections` | 3 | Pool connections to maintain |
| `auto_discovery.probe_interval` | 120 | Seconds between health probes |
| `auto_discovery.cooldown_seconds` | 300 | Min wait before retrying failed hub |
| `auto_discovery.prefer_diverse_regions` | true | Spread across geographic regions |
| `auto_discovery.exchange_interval` | 900 | Seconds between peer hub exchange |

### Hub Exchange Protocol

Nodes share working hub lists over Reticulum Links, extending discovery beyond the bundled list. New hubs from peers are merged automatically.

## Connectivity Monitor

Monitors transport stack health -- rnsd, interfaces, I2P, and routing table. Provides routing diagnostics including hop distribution, path freshness, rate limiting, and blackhole tracking.

| Option | Default | Description |
|--------|---------|-------------|
| `check_interval` | 30 | Seconds between diagnostic cycles |
| `log_path` | /var/lib/reticulumpi/.local/share/reticulumpi/connectivity.log | Diagnostic log (5 MB rotating) |
| `sam_port` | 7656 | i2pd SAM API port to probe |

## Path Warmer

Proactively refreshes paths to known and important nodes before they go stale. Exposes `ensure_path()` for LXMF plugins to call before transmitting.

| Option | Default | Description |
|--------|---------|-------------|
| `warm_interval` | 120 | Seconds between warming cycles |
| `max_requests_per_cycle` | 10 | Max path requests per cycle |
| `path_age_threshold` | 1200 | Seconds before a path is stale (20 min) |
| `pre_send_timeout` | 8 | Seconds to block in ensure_path() |
| `priority_nodes` | [] | Hex hashes to always keep warm |

## Transport Health

Tracks reliability of transport (relay) nodes by analyzing routing table `via` fields. Monitors availability over time and alerts on critical relay failures.

| Option | Default | Description |
|--------|---------|-------------|
| `check_interval` | 60 | Seconds between health checks |
| `db_path` | /var/lib/reticulumpi/.local/share/reticulumpi/transport_health.db | SQLite database |
| `history_retention_hours` | 168 | Hours of history (7 days) |
| `down_threshold_checks` | 3 | Consecutive absences = down |
| `critical_path_count` | 5 | Min paths relayed to be "critical" |

## Meshtastic Gateway

Bridges [Meshtastic](https://meshtastic.org/) LoRa mesh with Reticulum/LXMF. Text messages are translated between the two networks at the application layer.

**Requires:** `pip install reticulumpi[meshtastic]`

| Mode | Connection | Hardware | Best For |
|------|-----------|----------|----------|
| **serial** | USB device | RAK 4631, T-Beam, etc. | Production, direct radio |
| **mqtt** | MQTT broker | None | Testing, no-hardware setups |

> **Warning (MQTT):** Messages may be rebroadcast over LoRa, creating "MQTT pollution". Use a private channel or keep rate limits low.

| Option | Default | Description |
|--------|---------|-------------|
| `mode` | serial | `serial` or `mqtt` |
| `serial_port` | auto | Serial port (serial mode) |
| `meshtastic_channel` | 0 | Channel index (0--7) |
| `display_name` | \<node_name\> Mesh Gateway | Gateway name |
| `max_messages_per_minute` | 2 | LXMF-to-Meshtastic rate limit |
| `lxmf_recipients` | [] | LXMF hashes to forward Meshtastic messages to |
| `meshtastic_allow_list` | [] | Meshtastic node IDs to accept (empty = all) |
| `lxmf_allow_list` | [] | LXMF senders allowed to forward (empty = all) |

### Dual-Radio Setup

A Pi can run both networks using two USB LoRa radios -- one with Meshtastic firmware (this plugin), one with RNode firmware (Reticulum interfaces).

### Dashboard & Info Bot Integration

- Dashboard shows Meshtastic section with nodes, status, and message counts
- Send `!mesh` or `!mesh nodes` to the info bot for gateway status

## Messaging Hub

Unified message store and chat UI. Bridges LXMF and Meshtastic into a single conversation view with SQLite persistence.

| Option | Default | Description |
|--------|---------|-------------|
| `db_path` | /var/lib/reticulumpi/.local/share/reticulumpi/messaging_hub.db | Message database |
| `message_history_limit` | 500 | Max stored messages (0 = unlimited) |
| `lxmf.enabled` | true | Enable LXMF adapter |
| `lxmf.storage_path` | /var/lib/reticulumpi/.local/share/reticulumpi/messaging_hub_lxmf | LXMF storage |
| `lxmf.display_name` | \<node_name\> Messages | LXMF display name |
| `meshtastic.enabled` | true | Enable Meshtastic adapter (requires gateway) |

The messaging hub appears in the web dashboard as a chat interface with transport badges, contact selection, and real-time message delivery via WebSocket.

Future transports can register via `hub.register_adapter()` with zero hub/dashboard changes.

## Yggdrasil Transport

Monitors the [Yggdrasil](https://yggdrasil-network.github.io/) encrypted IPv6 overlay network and optionally auto-configures a Reticulum TCP interface for global mesh reachability via the Yggdrasil address space.

| Option | Default | Description |
|--------|---------|-------------|
| `check_interval` | 30 | Health-check period in seconds (min 10) |
| `admin_socket` | auto-detect | Path to Yggdrasil admin Unix socket |
| `auto_configure_rns` | false | Automatically add a TCPServerInterface to Reticulum config |
| `rns_listen_port` | 4242 | Port for the auto-configured Reticulum interface |

The plugin queries the Yggdrasil admin API (Unix socket with `yggdrasilctl` CLI fallback) to collect:
- IPv6 address and subnet
- Connected peers and their addresses
- Uptime, traffic counters, build version

State transitions (online/offline, peer count changes) are published as events for other plugins to consume. Health data is exposed on the dashboard.

**Auto-RNS configuration:** When `auto_configure_rns: true`, the plugin adds a `[[Yggdrasil TCP Interface]]` section to the Reticulum config on first run, listening on the node's Yggdrasil IPv6 address. This makes the node reachable by any other Reticulum node on the Yggdrasil network without manual config.

**Requirements:** `sudo apt-get install yggdrasil && sudo systemctl enable --now yggdrasil`. The bootstrap script handles this with `--with-yggdrasil`.

## Example Plugin

A fully working scaffold at `plugins/example_plugin.py` demonstrating config validation, destinations, packet handling, threads, inter-plugin communication, and status reporting. Copy and modify to start your own plugin.
