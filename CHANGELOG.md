# Changelog

All notable changes to ReticulumPi will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries below 0.2.5 describe historical behavior at their release date and are not current
installation, path, authentication, or security guidance.

## [Unreleased]

The 0.2.5–0.3.1 entries and the current 0.3.7 entry are release candidates. They have not been
promoted until their exact artifacts, signatures, CI records, hardware qualification, and approvals
are complete. Versions 0.3.2–0.3.6 were withdrawn after failed qualification and will not be
reused.

## [0.3.7] - Unreleased

### Added

- Added canonical, generation-fenced ownership for USB serial radios so RNS, Meshtastic,
  MeshCore, the LoRa Link Tester, and direct GPS cannot silently claim the same physical device.
- Added bounded active health probes and observable recovery state for Meshtastic and MeshCore
  radios, plus a durable reset circuit breaker and explicit USB identity guard for Meshtastic.
- Added crash-safe Meshtastic MQTT packet-ID reservations and broker-CONNACK admission so encrypted
  messages cannot reuse a packet nonce after restart or report readiness before broker acceptance.
- Added a board/firmware compatibility matrix and hardware recovery sequence without permanently
  pinning device firmware.

### Changed

- Require stable `/dev/serial/by-id` paths or dedicated udev aliases for direct multi-radio
  consumers, and keep SDK-internal reconnects from bypassing lease identity validation.
- Continue reconnecting Meshtastic, MeshCore, Link Tester, and direct-GPS services with bounded
  backoff by default instead of permanently abandoning a temporarily unavailable peripheral.
- Use the official digest-pinned Python 3.14.7 multi-architecture container base, whose native
  standard library contains the required security fixes, without a local interpreter patch; an
  exact-product temporary VEX bridges current scanner-data lag while full reports remain
  unsuppressed.
- Retire the v0.3.6 publication-workflow path and require an exact explicit `release` environment
  approval before any registry authentication. Administrator bypass is disabled and the exact
  `v0.3.7` tag is required at both protected environments; signed-candidate assembly and publication
  also refuse workflow reruns.

### Fixed

- Recover a Link Tester after both fast USB send errors and hung calls, reject accidental negative
  unlimited test counts, and isolate its Meshtastic packets from the gateway's global pubsub.
- Escalate a Meshtastic soft recovery safely when a radio never reopens, serialize private SDK
  health and recovery operations after timeouts, honor proactive probe cadence even while traffic
  is flowing, and keep suspect or guarded recovery states visibly nonhealthy.
- Reject error-shaped MeshCore device-info events, reserve every supported RNS serial interface,
  and parse valid ConfigObj quoting, inline comments, and indentation consistently.
- Accept the exact firmware metadata emitted by older MeshCore companions and keep Observer JWT
  authentication functional with MeshCore's exported expanded Ed25519 key format.
- Preserve whether an RTL-SDR selection came from `device_serial` or `device_index` through plugin
  configuration, scheduler arbitration, device claims, refresh, and release. Zero-padded index
  values remain indexes instead of being reinterpreted as serial numbers.

### Pending promotion evidence

- Fresh signed-tag CI, offline Minisign assembly, exact-artifact production qualification, and the
  Pi 5 72-hour soak are required. No v0.3.6 evidence may be relabeled or carried forward.

## [0.3.6] - Withdrawn 2026-08-13

### Added

- Added canonical, generation-fenced ownership for USB serial radios so RNS, Meshtastic,
  MeshCore, the LoRa Link Tester, and direct GPS cannot silently claim the same physical device.
- Added bounded active health probes and observable recovery state for Meshtastic and MeshCore
  radios, plus a durable reset circuit breaker and explicit USB identity guard for Meshtastic.
- Added crash-safe Meshtastic MQTT packet-ID reservations and broker-CONNACK admission so encrypted
  messages cannot reuse a packet nonce after restart or report readiness before broker acceptance.
- Added a board/firmware compatibility matrix and hardware recovery sequence without permanently
  pinning device firmware.

### Changed

- Require stable `/dev/serial/by-id` paths or dedicated udev aliases for direct multi-radio
  consumers, and keep SDK-internal reconnects from bypassing lease identity validation.
- Continue reconnecting Meshtastic, MeshCore, Link Tester, and direct-GPS services with bounded
  backoff by default instead of permanently abandoning a temporarily unavailable peripheral.
- Use the official digest-pinned Python 3.14.7 multi-architecture container base, whose native
  standard library contains the required security fixes, without a local interpreter patch; an
  exact-product temporary VEX bridges current scanner-data lag while full reports remain
  unsuppressed.

### Fixed

- Recover a Link Tester after both fast USB send errors and hung calls, reject accidental negative
  unlimited test counts, and isolate its Meshtastic packets from the gateway's global pubsub.
- Escalate a Meshtastic soft recovery safely when a radio never reopens, serialize private SDK
  health and recovery operations after timeouts, honor proactive probe cadence even while traffic
  is flowing, and keep suspect or guarded recovery states visibly nonhealthy.
- Reject error-shaped MeshCore device-info events, reserve every supported RNS serial interface,
  and parse valid ConfigObj quoting, inline comments, and indentation consistently.
- Accept the exact firmware metadata emitted by older MeshCore companions and keep Observer JWT
  authentication functional with MeshCore's exported expanded Ed25519 key format.
- Preserved whether an RTL-SDR selection came from `device_serial` or `device_index` through
  plugin configuration, scheduler arbitration, device claims, refresh, and release. Zero-padded
  index values remain indexes instead of being reinterpreted as serial numbers.

### Withdrawal

- The signed tag, source CI, both offline Minisign rounds, and exact signed-candidate assembly
  completed, but the protected `release` environment was administratively skipped before Gate 85.
- The interrupted promotion published three unqualified GHCR version tags before cancellation.
  They were removed without reuse; no GitHub Release, release attestation, production deployment,
  hardware qualification, reboot check, or 72-hour soak completed.
- Version 0.3.6 remains permanently withdrawn. Its signed Git tag, workflow runs, artifact hashes,
  and incident record are retained as evidence and cannot qualify a successor.

## [0.3.5] - Withdrawn 2026-08-08

### Fixed

- Preserved whether an RTL-SDR selection came from `device_serial` or `device_index` through
  plugin configuration, scheduler arbitration, device claims, refresh, and release. Zero-padded
  index values remain indexes instead of being reinterpreted as serial numbers.

### Withdrawal

- The signed tag and source CI completed, but terminal R31 evidence records no promotable final
  signed output and no production qualification. Its evidence cannot be relabeled for a successor.
- The protected promotion run remained behind its environment gate and was cancelled before any
  publication step. No stable GitHub Release, production cutover, hardware qualification, or
  72-hour soak occurred, and this version will not be moved or reused.

## [0.3.4] - Withdrawn 2026-07-14

### Fixed

- Accepted YAML's valid indentationless block-sequence form when it belongs to a
  projection-irrelevant recovery configuration field, including the production
  `sensor_framework.sensors` shape emitted by PyYAML.
- Kept recovery projection fail-closed for orphan sequences, relevant migration keys, malformed
  mixed collections, duplicate relevant fields, and relevant-looking fields nested inside ignored
  sequence items.

### Withdrawal

- Signed tag CI and two-round offline Minisign candidate assembly completed, but production
  qualification found that a zero-padded value sourced from `device_index` could be interpreted as
  an RTL-SDR serial rather than the explicitly configured index.
- The protected `release` publication job remained unapproved and was canceled before executing
  any publication step. No stable GitHub Release, versioned container promotion, hardware
  qualification, or 72-hour soak occurred, and this version will not be moved or reused.

## [0.3.3] - Withdrawn 2026-07-14

### Fixed

- Preserved annotated OpenPGP tag objects in every release workflow checkout and bound the package
  build to the exact tag event commit before executing repository build code.
- Preserved the pinned 0.2.4 full-refactor coverage boundary for 0.3.3 instead of treating the
  withdrawn 0.3.2 tag as completed coverage evidence.
- Matched the Noble legacy-bridge fixture to production's omitted external-artifact policy and
  qualified all 14 observed production features, including `offline-tools`.

### Withdrawal

- The signed candidate completed CI and offline assembly but failed closed during production
  preflight before any administrator transaction or runtime cutover: its dependency-free recovery
  parser rejected the valid indentationless `sensor_framework.sensors` sequence in the legacy
  configuration.
- The deployment contingency restored the exact legacy configuration bytes and metadata, removed
  the staged external manifest, and left the legacy service active. No cutover, hardware soak, or
  stable promotion occurred, and this version will not be moved or reused.

## [0.3.2] - Withdrawn 2026-07-13

### Added

- Added secret-free operational lifecycle, process, SDR, and callback metrics.
- Added code-derived documentation inventories and release-verification records.

### Security

- Removed first-party inline styles and handlers from the dashboard and enforced strict CSP
  through local static and cross-browser regression gates.

### Withdrawal

- The annotated tag was never published as a GitHub Release. Its tag-only CI failed closed because
  the checkout action replaced the local annotated tag ref with the peeled commit before both tag
  consumers verified it. No Minisign request or deployable candidate was produced, and this version
  will not be moved or reused.

## [0.3.1] - Unreleased

### Added

- Added semantic landmarks, native disclosure buttons and dialogs, named controls, table
  captions/scopes, live regions, keyboard/pointer spectrum controls, reduced-motion and
  forced-colors behavior, and responsive layouts from 320 px through 4K.
- Added 20 content-addressed optional feature chunks gated by plugin availability and panel
  opening or proximity, plus network-first service-worker navigation fallback.

### Pending promotion evidence

- Manual Edge/assistive-technology review, interrupted-update/rollback drills, and all field
  LCP/INP/CLS/network/WebSocket/frame-rate budgets remain required.

## [0.3.0] - Unreleased

### Changed

- Adopted `setuptools-scm` as the single package-version source with strict release tags and
  source-archive fallback metadata.
- Added hashed universal production core, Dashboard/NomadNet, and build dependency profiles
  shared by the qualified Bookworm/Python 3.11 and Noble/Python 3.12 lanes; release CI and
  Docker builds now consume one prebuilt wheel.
- Consolidated the service and RNS runtime under `HOME=/var/lib/reticulumpi`, with
  conventional XDG state below that root and disposable caches under
  `/var/cache/reticulumpi`; the former service home is migration input only.
- Compatibility launchers now require an independently installed, fixed, root-owned
  `reticulumpi-admin`; they never import administrator code from the candidate bundle,
  checkout, `PATH`, or mutable current release.

### Added

- Added lifecycle API v2, readiness-aware dependencies, reverse-order managed cleanup,
  bounded callback isolation, and hung-worker containment while retaining the API v1 adapter.
- Added transactional SQLite migrations, supervised process groups, canonical SDR leases,
  deterministic hardware recovery fixes, and a root-owned transactional administrator.
- The administrator now persists its `preparing` journal before candidate release, virtualenv,
  or package creation; power-loss recovery safely removes a recorded partial candidate, while
  path and configuration mutation begins only after a verified backup checkpoint.
- Production roots now require immutable root-owned ancestry. Signed manifests, constraints,
  source, archives, and wheels are consumed only from a private no-follow snapshot with digest
  checks immediately before installation; external-path replacement cannot change installed code.
- Administration journals moved outside service-owned/swapped state, migration targets and locks
  reject service-created symlinks, and obsolete Dashboard credential drop-ins are snapshotted and
  removed transactionally.
- Scheduler-backed radio decoders now release SDR ownership for every restart backoff and
  reacquire through normal arbitration; stale completed acquisitions run decoder cleanup before
  returning the physical lease, and scheduler duration accounting uses a monotonic clock.
- Replaced runtime sudo with a root-owned, socket-activated, peer-credential-checking control
  broker and adopted notify-based service readiness.

### Pending promotion evidence

- Signed multi-architecture artifacts, systemd-capable Bookworm install/rollback CI, Pi 5 and
  representative radio/GPS qualification, and release coverage remain required.

## [0.2.5] - Unreleased

### Security

- **Upgrade advisory:** operators upgrading from 0.2.4 or older must remove obsolete
  service-owned sudo helpers/rules, rotate any dashboard password that may have appeared in
  historical journals, and invalidate all sessions. See
  [`docs/security-advisory-0.2.5.md`](docs/security-advisory-0.2.5.md).
- Hardened first-start identity creation against concurrent writers and partial persistence.
- Replaced implicit file-transfer trust with explicit deny, allowlist, and open policies.
- Removed plaintext dashboard passwords from logs and added scoped loopback API tokens.
- Required generated bootstrap credentials to be durably replaced before normal dashboard
  access; password changes now revoke sessions/WebSockets, and local API tokens rotate in
  runtime storage on every start.
- Moved sudo-executed helper scripts into a root-owned `/usr/libexec` boundary.
- Separated administrator-owned configuration from service-owned runtime overrides.
- TLS now rejects unsafe operator key metadata, future/near-expiry certificates, and missing
  required SANs; checks are scheduled at expiry guards. Secure cookies and HSTS trust
  forwarded HTTPS only from explicitly configured proxy CIDRs, and config views conservatively
  redact secret-like keys such as Meshtastic channel PSKs.
- Generated bootstrap credentials remain in their protected mode-`0600` file after login
  and are removed only after a successful durable password change; that change closes every
  session and WebSocket.

### Fixed

- Packaged the complete dashboard static tree in wheels and added an installed-wheel check.
- Corrected Docker persistence, offline verification counters, service deadlines, dependency
  floors, developer extras, and Python/toolchain drift.
- Added protected tag-only publication that promotes the exact tested wheel, sdist, SBOM,
  signed ARM64 install bundle, and per-architecture container archives without rebuilding;
  a trusted-fingerprint OpenPGP preflight and Bookworm/systemd rollback gate run before
  publication, and release assets receive a global Minisign manifest, GitHub attestations, and
  an immutable GHCR multi-architecture digest.

### Documentation

- Added security, dashboard, container, release, rollback, accessibility,
  hardware-validation, architecture-decision, and audit-remediation guides.

### Pending promotion evidence

- Signed 0.2.5 artifacts and verification record, final CI, and the required Pi 5 plus
  representative-device qualification remain required.

## [0.2.4] - 2026-06-12

### Fixed
- **Dashboard boot fragility** — the live-data pipeline (WebSocket connect, 2s HTTP
  fallback, periodic refresh) is now armed at the very top of app.js inside a guarded
  `boot()` call, so an exception anywhere in the ~2,000 lines of later DOM wiring can no
  longer freeze the main metrics silently (the v0.2.3 incident class). All DOM/event
  wiring is wrapped in per-block `safeWire` isolation — one broken block can't kill the
  rest.
- **Silent "no data ever" state** — the stale-data banner now appears ~20s after boot if
  neither the WebSocket nor the HTTP fallback ever delivered data (previously it stayed
  hidden forever in exactly that case).
- **Connection badge** — after WebSocket reconnect attempts are exhausted the badge now
  correctly reads "polling (10s)" instead of "disconnected".

### Added
- **Client-side error reporting** — new `errlog.js` (loaded before app.js) catches
  uncaught JS errors and unhandled promise rejections and POSTs them to the new
  auth+CSRF-protected `/api/client_error` endpoint, which logs a single-line WARNING to
  the journal (per-IP rate limited, fields sanitized/truncated against log injection).
  Browser-side failures are now diagnosable from `journalctl -u reticulumpi`.

## [0.2.3] - 2026-06-11

### Fixed
- **Broadcast pipeline slowness** — messaging_hub's conversation query ran a windowed
  full-table scan with no LIMIT on nearly every cycle (up to 13s live), and its snapshot
  cache was defeated by per-message invalidation. Rewritten index-assisted and bounded
  (200 newest conversations), with an 8s min-recompute interval and a 5s transport-
  availability cache that removes cross-plugin lock acquisition from the broadcast thread.
- **Stale dashboard panels** — broadcast budget overruns skipped the same plugins every
  cycle; per-tier collection order now rotates so no panel is permanently starved.
  lora_diagnostics no longer re-parses the rnsd config from disk every 2s (15s snapshot
  TTL + mtime-gated parse); network_map's mesh summary (full scans of a 42MB table)
  computes in the maintenance loop instead of the broadcast thread; meshtastic_gateway's
  periodic node-cache disk save moved off the broadcast thread.
- **SQLite WAL bloat** — messaging_hub, network_map, and the auth sessions store now run
  periodic `wal_checkpoint(TRUNCATE)`; network_map enables `auto_vacuum=INCREMENTAL` for
  new installs.
- **asyncio "Future exception was never retrieved" noise** — a WebSocket send timeout now
  aborts the stalled client's transport immediately (previously the dead socket lingered
  ~15min until kernel ETIMEDOUT fired an orphaned aiohttp drain-waiter); a scoped loop
  exception handler downgrades the residual signature to debug.

### Added
- **TLS support hardened for real use** — auto-generated self-signed certs now carry SANs
  for the hostname, `<hostname>.local`, loopback, and LAN IPs (plus configurable
  `ssl.extra_hostnames`); HSTS header when serving HTTPS; mDNS advertises `_https._tcp`
  when TLS is active.
- **Failed-login audit logging** — throttled WARNING (one per IP per 10s with suppressed
  count); login rate limiter is now configurable via `web_dashboard.rate_limit`.
- **Optional IP allowlist** — `web_dashboard.allowed_networks` CIDR list enforced ahead of
  all other middleware; non-members receive 404. Default empty (allow all).
- **Tile proxy now requires auth** — `/tiles/` moved behind the session, closing anonymous
  use of the node as an OSM tile proxy; the map is unaffected for logged-in users.
- Broadcast health log now reports per-tier collection timings and skipped count;
  registry slow-threshold and tier budget factors are configurable.

## [0.2.2] - 2026-06-10

### Fixed
- **node_location_tracker `get_history` newest-N** — with `limit_per_node` set, the query
  returned the *oldest* N rows in the window instead of the newest, so a busy node's trail
  silently dropped its most recent positions. The limited path now selects newest-N and
  returns them in ascending order (contract unchanged).
- **Map trail fetch race** — switching filter tabs (or toggling trails off) mid-fetch no
  longer renders trails on the wrong view or raises a late no-data toast.
- **Map trail cache keyed on node set** — the 15s trail cache now also matches on the
  sorted tracked-node key set, so a just-added node's trail appears immediately instead of
  serving a stale cached set.
- **node_location_tracker `_last_pos` prune** — the in-memory last-position cache is now
  pruned alongside DB retention instead of growing unbounded.

### Added
- **Tracked-node source persistence** — the map tracker now stores `{id: source}` in
  localStorage instead of a bare id array, so meshcore/meshtastic key prefixes survive
  reloads without regex inference (legacy array payloads still load). Ids resolving to
  reticulum/rns are omitted from trail queries.
- **API test coverage for `/api/node_tracker/history`** — new `tests/test_api_services.py`
  covering parameter validation, hours/limit caps, CSV parsing, limit semantics, and
  error paths; tightened `get_history` tests to assert *which* rows are returned.

## [0.2.1] - 2026-06-02

### Added
- **Mesh Bridge plugin** (`mesh_bridge`) — bidirectional relay between Meshtastic and MeshCore mesh networks. Subscribes to `MESHTASTIC_MESSAGE_RECEIVED` and `MESHCORE_MESSAGE_RECEIVED` events, re-sends broadcasts (and optionally DMs) on the opposite network via `messaging_hub.send_message()`. Features: origin tag prefix (`[via Mesh]` / `[via Core]`), two-layer loop prevention (regex + 60s dedup cache with opposite-side pre-seeding), MTU-aware truncation, per-pair allow/deny regex filters, and optional DM bridging with explicit identity pairs.
- **Mesh Bridge runtime pause/resume** — operator can pause/resume relaying without restarting the service via (1) dashboard toggle card, (2) `POST /api/mesh_bridge/running` endpoint, or (3) `reticulumpi --mesh-bridge {status,pause,resume}` CLI. Runtime state persists to `~/.local/share/reticulumpi/mesh_bridge_state.json` across restarts. Rate-based circuit breaker auto-pauses if traffic exceeds a configurable threshold (default 20 relays/60s).
- `GET /api/mesh_bridge/status` and `POST /api/mesh_bridge/running` API endpoints.

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
