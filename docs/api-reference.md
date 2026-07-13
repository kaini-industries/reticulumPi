# Web Dashboard API Reference

The ReticulumPi web dashboard exposes a REST API and WebSocket endpoint for monitoring and managing a node. All endpoints return JSON in a standard envelope:

```json
{
  "ok": true,
  "data": { ... },
  "timestamp": 1712345678.123
}
```

Error responses:

```json
{
  "ok": false,
  "error": "Description of error",
  "code": 400
}
```

## Authentication

All endpoints except login and static files require authentication.

### Token Flow

1. Authenticate via `POST /api/auth/login` to get a session token
2. Include the token in subsequent requests as either:
   - Header: `Authorization: Bearer <token>`
   - Cookie: `session=<token>` (set automatically by login)

### Local-service token

There is no anonymous localhost bypass. Optional local automation uses a separate mode-`0600`
Bearer token and must originate from `127.0.0.1` or `::1`. Its fixed scope is read-only
`GET` access to `/api/version`, `/api/status`, `/api/node`, and `/api/interfaces`; it cannot
mutate state or open WebSockets.

```yaml
web_dashboard:
  local_api:
    enabled: true
    # Defaults to /run/reticulumpi/local_api.token and rotates at startup.
```

Normal session authentication is checked first, including for localhost clients. Never
forward this token through a reverse proxy.

### Rate Limiting

5 failed login attempts per IP per 60-second window. Returns HTTP 429 with `Retry-After` header.

### Sessions

- Tokens are 64-character hex strings (256 bits via `secrets.token_hex`)
- Default timeout: 86,400 seconds (24 hours)
- Maximum 5 sessions per password (LRU eviction)
- Logout, expiry, and password rotation revoke associated WebSockets

---

## Auth Endpoints

### POST /api/auth/login

Authenticate and receive a session token.

**Request body:**
```json
{ "password": "your_password" }
```

**Response (200):**
```json
{
  "ok": true,
  "data": {
    "message": "Login successful",
    "password_change_required": false
  }
}
```

**Errors:** 401 (invalid password), 429 (rate limited)

Sets `session` cookie (HttpOnly, SameSite=Lax, and Secure for direct TLS or an explicitly
configured trusted reverse proxy reporting an exact `X-Forwarded-Proto: https`).

When `password_change_required` is true, that session may access only password change and
logout endpoints until the bootstrap credential is replaced.

---

### POST /api/auth/password

Durably replace an auto-managed dashboard password. Requires an authenticated session and
the normal `X-Requested-With` CSRF header.

```json
{
  "current_password": "temporary bootstrap value",
  "new_password": "a new password of at least 12 characters"
}
```

Success invalidates all sessions, closes associated WebSockets, removes the bootstrap file,
deletes the current session cookie, and requires a new login. Returns 409 when the password
is managed by environment or system configuration. The response is always `no-store`.

---

### POST /auth/login

Form-based login for browsers. Accepts form-encoded `password` field. Redirects to `/` on success, `/login.html?error=<code>` on failure.

Error codes: `rate_limited`, `invalid`, `empty`

---

### POST /api/auth/logout

Invalidate the current session.

**Response (200):**
```json
{
  "ok": true,
  "data": { "message": "Logged out" }
}
```

---

## Node & System Endpoints

### GET /api/node

Node identity and version info.

**Response:**
```json
{
  "ok": true,
  "data": {
    "node_name": "MyNode",
    "identity_hash": "<hex>",
    "version": "0.2.0",
    "uptime": 86400
  }
}
```

---

### GET /api/status

Full application status including all plugin states and the fixed-cardinality,
secret-free `operational_metrics` snapshot. The snapshot covers lifecycle/readiness,
workers/resources, thread/process and callback pressure, migrations/SQLite, SDR leases,
and dashboard security/cache counters. Field semantics are documented in
[Dashboard Operations](dashboard-operations.md#operational-metrics).

---

### GET /api/metrics

Latest system metrics from `system_monitor` plugin.

**Response:**
```json
{
  "ok": true,
  "data": {
    "cpu_percent": 25.5,
    "cpu_temp": 52.0,
    "memory_percent": 45.2,
    "disk_percent": 34.1
  }
}
```

Returns `{"message": "system_monitor plugin not available"}` if plugin is disabled.

---

### GET /api/config

Sanitized read-only configuration. Sensitive keys (`password`, `password_hash`) are stripped.

---

### GET /api/interfaces

Active Reticulum network interfaces with traffic statistics.

**Response:**
```json
{
  "ok": true,
  "data": {
    "interfaces": [
      {
        "name": "TCPInterface",
        "type": "TCPInterface",
        "online": true,
        "bitrate": 1000000,
        "rxb": 123456,
        "txb": 654321
      }
    ]
  }
}
```

In shared-instance mode, queries `Reticulum.get_interface_stats()`. Filters out internal LocalClient/LocalServer interfaces.

---

## Plugin Endpoints

### GET /api/plugins

All plugins with their status and address.

**Response:**
```json
{
  "ok": true,
  "data": {
    "plugins": {
      "system_monitor": {
        "name": "system_monitor",
        "version": "1.0.0",
        "description": "...",
        "status": { "active": true, ... },
        "address": "<hex hash or null>"
      }
    },
    "failed_plugins": [
      { "name": "bad_plugin", "error": "Import failed" }
    ]
  }
}
```

---

### GET /api/plugins/{name}

Single plugin details. Returns 404 if plugin not found.

---

## Mesh Network Endpoints

### GET /api/mesh/nodes

Known nodes from `network_map` plugin.

**Response:**
```json
{
  "ok": true,
  "data": {
    "nodes": [
      {
        "destination_hash": "<hex>",
        "app_name": "reticulumpi",
        "app_data": "node_name",
        "hops": 2,
        "last_seen": 1712345678.0,
        "announce_count": 15
      }
    ]
  }
}
```

---

### GET /api/mesh/telemetry

Peer metrics from `mesh_telemetry` plugin.

---

### GET /api/reachability

Scored node reachability ranking.

**Query params:**
| Param | Default | Description |
|-------|---------|-------------|
| `limit` | 50 | Max nodes (0 = all) |
| `search` | -- | Hex prefix filter |

**Response includes:**
- `nodes` array with `score`, `label` (high/good/fair/low/unlikely), `via_transports`, `via_direct`
- `summary` with distribution counts and averages

---

## Routing Endpoints

### GET /api/routing

Full routing table with pagination, filtering, and sorting.

**Query params:**
| Param | Default | Description |
|-------|---------|-------------|
| `page` | 1 | Page number |
| `per_page` | 100 | Items per page (max 500, 0 = summary only) |
| `sort` | `hops` | Sort field: `hops`, `timestamp`, `expires`, `hash`, `interface` |
| `order` | `asc` | Sort order: `asc`, `desc` |
| `interface` | -- | Substring filter on interface name |
| `min_hops` | -- | Minimum hop count |
| `max_hops` | -- | Maximum hop count |
| `search` | -- | Hex prefix filter on destination hash |

**Response:**
```json
{
  "ok": true,
  "data": {
    "summary": {
      "total_entries": 1250,
      "average_hops": 2.8,
      "interface_count": 5,
      "interface_names": ["TCP", "I2P"]
    },
    "paths": [
      {
        "destination_hash": "<hex>",
        "hops": 2,
        "timestamp": 1712345678.0,
        "expires": 1712432078.0,
        "interface": "TCPInterface",
        "reachable": true
      }
    ],
    "total_paths": 50,
    "page": 1,
    "per_page": 100,
    "pages": 1,
    "rate_table": [ ... ],
    "blackholed": { ... }
  }
}
```

---

## Transport & Connectivity Endpoints

### GET /api/transport

Transport hub health, fallback status, and auto-discovery pool.

---

### GET /api/connectivity

Connectivity diagnostics and health issues.

**Response:**
```json
{
  "ok": true,
  "data": {
    "issues": [
      {
        "type": "degraded_latency",
        "severity": "warning",
        "description": "Interface RTT increasing"
      }
    ]
  }
}
```

---

### GET /api/path_warming

Path warmer statistics (paths warmed, success rate, last warm time).

---

### GET /api/transport_health

Transport relay node health data with uptime and reliability metrics.

---

## Sensor Endpoints

### GET /api/sensors

Latest readings from all active sensors.

---

### GET /api/sensors/history

Time-series data for a specific sensor.

**Query params:**
| Param | Default | Description |
|-------|---------|-------------|
| `sensor` | -- | Sensor name (**required**) |
| `limit` | 60 | Max entries (max 500) |

**Response:**
```json
{
  "ok": true,
  "data": {
    "sensor": "cpu_temp",
    "history": [
      { "value": 52.0, "timestamp": 1712345678.0 },
      { "value": 51.5, "timestamp": 1712345618.0 }
    ]
  }
}
```

Returns 400 if `sensor` param is missing.

---

## File & Emergency Endpoints

### GET /api/files

Shared files from `file_transfer` plugin.

---

### GET /api/emergency

Recent emergency broadcast messages with status.

---

### GET /api/alerts

Alert system status and recent alert history.

---

## NomadNet Endpoints

### GET /api/nomadnet/auth

NomadNet page access control identity list.

---

### POST /api/nomadnet/auth/add

Add identity to NomadNet allow list.

**Request body:**
```json
{ "identity": "hex_hash_here" }
```

Identity max length: 128 characters.

---

### POST /api/nomadnet/auth/remove

Remove identity from NomadNet allow list.

**Request body:**
```json
{ "identity": "hex_hash_here" }
```

---

## Meshtastic Endpoints

### GET /api/meshtastic/status

Gateway status, connection info, and message counters.

---

### GET /api/meshtastic/nodes

Known Meshtastic mesh nodes with name, ID, hardware, SNR, and last-heard time.

---

## Messaging Endpoints

### GET /api/messages

Paginated message history.

**Query params:**
| Param | Default | Description |
|-------|---------|-------------|
| `limit` | 50 | Max messages (max 200) |
| `offset` | 0 | Skip first N messages |
| `transport` | -- | Filter: `lxmf`, `meshtastic` |
| `direction` | -- | Filter: `sent`, `received` |
| `since` | -- | Unix timestamp (float), only newer messages |

---

### POST /api/messages/send

Send a message via a transport.

**Request body:**
```json
{
  "transport": "lxmf",
  "destination": "<address>",
  "text": "Hello"
}
```

**Validation:**
- `transport`, `destination`, `text` all required
- `text` max 5000 characters

**Response:**
```json
{
  "ok": true,
  "data": { "sent": true, "msg_id": 42 }
}
```

---

### GET /api/messages/transports

Available message transports with status.

---

### GET /api/messages/contacts

Known contacts across transports.

**Query params:**
| Param | Default | Description |
|-------|---------|-------------|
| `transport` | -- | Filter by transport name |

---

### GET /api/messages/stats

Message counts grouped by transport and direction.

---

## WebSocket

### GET /ws/metrics

Real-time streaming of all dashboard data.

**Authentication:**
- Query param: `?token=<bearer_token>`
- Falls back to `session` cookie
- Close code `4001` if auth fails
- Close code `4002` if max clients exceeded

**Heartbeat:** Server sends ping every 30 seconds.

**Broadcast interval:** Configurable (default 5 seconds).

**Message format:**
```json
{
  "type": "update",
  "data": {
    "metrics": { ... },
    "plugins": { ... },
    "interfaces": [ ... ],
    "sensors": { ... },
    "emergency": { ... },
    "transport": { ... },
    "connectivity": { ... },
    "routing": { ... },
    "path_warming": { ... },
    "transport_health": { ... },
    "messaging": { ... },
    "mesh": { ... }
  },
  "timestamp": 1712345678.123
}
```

**Notes:**
- Mesh data only sent when changed (hash-based deduplication)
- Each data section is optional (depends on which plugins are enabled)
- Clients should handle missing/null fields gracefully

---

## Endpoint Summary

The complete method/path inventory is generated from aiohttp registrations and checked on
every documentation-CI run. See [Dashboard routes](generated-code-reference.md#dashboard-routes).
The same generated reference records the effective
[core configuration defaults](generated-code-reference.md#core-configuration-defaults).
