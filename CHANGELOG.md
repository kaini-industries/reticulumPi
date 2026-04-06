# Changelog

All notable changes to ReticulumPi will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-04-05

### Added
- **Messaging Hub** -- unified transport-agnostic messaging plugin with SQLite storage, LXMF adapter, and Meshtastic adapter. Dashboard chat UI with real-time WebSocket delivery, transport badges, contact selection, and message filtering
- **Meshtastic DM support** -- gateway `sendText()` now supports `destinationId` for direct messages (backward-compatible; broadcast remains default)
- **Meshtastic persistent identity** -- MQTT mode generates a stable node number saved to disk, surviving restarts with consistent `!XXXXXXXX` identity
- **Meshtastic NODEINFO** -- gateway announces identity via NODEINFO on connection and every 15 minutes in MQTT mode
- **Sensor sparkline charts** -- dashboard shows SVG sparkline trend graphs per sensor field with history fetched from new `/api/sensors/history` endpoint
- **Sensor rich cards** -- dashboard sensors display with auto-detected units (temperature, humidity, pressure, voltage), color-coded thresholds, freshness indicators, and error states
- **Dashboard routing section** -- interactive routing table with pagination, sorting, filtering by hop count/interface/hash prefix, hop distribution chart, interface breakdown chart, path freshness statistics, and expandable path table
- **Dashboard connectivity health** -- real-time diagnostics for rnsd, I2P, SAM, interfaces, and paths with issue indicators
- **Dashboard transport hubs** -- live throughput rates, connection status, auto-discovery pool status
- **Dashboard mesh telemetry** -- peer metric cards with signal strength and health data
- **Content-Security-Policy headers** -- dashboard serves strict CSP (`default-src 'self'`, WebSocket connect-src, inline styles allowed)
- **Sensor history API** -- `GET /api/sensors/history?sensor=<name>&limit=60` returns time-series data
- **Messaging REST API** -- 5 new endpoints: messages, send, transports, contacts, stats
- **WebSocket messaging push** -- new messages delivered via WebSocket with sub-second latency plus 5s polling fallback
- **Messaging events** -- `MESSAGE_RECEIVED`, `MESSAGE_SENT`, `MESSAGE_FAILED` event types for inter-plugin communication
- Meshtastic gateway `send_message()` public API method for programmatic message sending
- Meshtastic adapter resolves node names from gateway's node list for human-readable sender display
- Input validation hardening: length caps on all POST endpoints (messages 5000 chars, passwords 256, identities 128)
- WebSocket broadcast resilience: all plugin data fetches wrapped in try/except to prevent killing the broadcast loop
- Safe integer/float parsing on all query parameters with fallback defaults
- Project documentation suite: API reference, plugin development guide, troubleshooting FAQ, connectivity guide, contributing guide, security policy

### Changed
- README trimmed from 1468 to ~550 lines; detailed content extracted to `docs/` with cross-references
- Dashboard cache-busted to v=7 for CSS and JS
- Meshtastic gateway tracks `msgs_hub_to_mesh` counter separately from `msgs_lxmf_to_mesh`
- Login page inline script extracted to external `login.js` for CSP compliance
- Sensor section CSS spacing matches other dashboard sections

### Fixed
- CSP header blocking 42 inline style usages (bar charts zero-width, status colors invisible, section toggling broken) -- added `'unsafe-inline'` to style-src
- CSP header blocking login page inline script -- extracted to external file
- Unguarded `int()` on query params in routing API could raise 500 on malformed input
- Unguarded `int()`/`float()` on message API query params (limit, offset, since)
- Missing try/except around system_monitor, network_map, mesh_telemetry, and sensor_framework data fetches in WebSocket broadcast
- Unused imports and mock variables in test_messaging_hub.py

## [0.1.2] - 2026-03-27

### Added
- Message echo plugin automatically selects the nearest LXMF propagation node for store-and-forward delivery
- Selected propagation node is written to NomadNet peersettings so daemon and TUI also use it
- Bootstrap script supports `--install-dir <path>` for custom install locations (default remains `/opt/reticulumpi`)
- Bootstrap supports in-place install with `--install-dir .` (runs from cloned repo, no copy)
- Update script auto-detects install directory from its own location (no hardcoded path)
- Install layout documentation (`docs/install-layout.md`)
- Tests for propagation node auto-selection and NomadNet peersettings writing
- Node Identities section in README documenting the multiple identity files

### Changed
- README updated with propagation auto-selection docs, node identities section, and install layout reference

## [0.1.1] - 2026-03-26

### Fixed
- Reticulum config examples now use correct format (`[interfaces]` section + `[[double brackets]]` for interface definitions)
- Bootstrap script creates all directories required by systemd `ReadWritePaths` (fixes exit code 226 on first start)
- NomadNet server plugin falls back to checking the running venv when `shutil.which("nomadnet")` fails under systemd
- Systemd service sets `PATH` to include the venv bin directory
- TCP Client Interface example now points to a real community hub instead of `example.com`

### Changed
- Bootstrap `--with-nomadnet` now auto-configures `use_shared_instance: true` and enables the `nomadnet_server` plugin
- Update script (`update.sh`) now syncs changed systemd service files and runs `daemon-reload`

## [0.1.0] - 2025-01-01

### Added
- Plugin-based architecture with abstract `PluginBase` class
- Three built-in plugins: heartbeat announce, LXMF message echo, system monitor
- Persistent cryptographic identity management
- YAML configuration with validation and useful error messages
- CLI with `--version`, `--config`, `--reticulum-config`, and `--log-level` flags
- Bootstrap script for automated Raspberry Pi deployment
- Update script for pulling latest code and upgrading dependencies
- Systemd service with security hardening
- Docker support with health check
- Comprehensive Reticulum config example covering all 12 interface types
- Connectivity guide in README covering LoRa, serial, packet radio, I2P, and more
- `make format` and `make test-cov` targets
- MIT LICENSE file
