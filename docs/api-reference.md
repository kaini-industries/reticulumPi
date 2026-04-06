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

### Localhost Bypass

When `allow_localhost_api: true` (default), requests from `127.0.0.1` or `::1` bypass authentication. Useful for local scripts and NomadNet page integration.

### Rate Limiting

5 failed login attempts per IP per 60-second window. Returns HTTP 429 with `Retry-After` header.

### Sessions

- Tokens are 64-character hex strings (256 bits via `secrets.token_hex`)
- Default timeout: 86,400 seconds (24 hours)
- Maximum 5 sessions per password (LRU eviction)

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
  "data": { "token": "<64-char hex>" }
}
```

**Errors:** 401 (invalid password), 429 (rate limited)

Sets `session` cookie (HttpOnly, Secure if SSL, SameSite=Lax).

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

Full application status including all plugin states.

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

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/auth/login` | No | JSON login |
| POST | `/auth/login` | No | Form login |
| POST | `/api/auth/logout` | Yes | Logout |
| GET | `/api/node` | Yes | Node info |
| GET | `/api/status` | Yes | Full status |
| GET | `/api/metrics` | Yes | System metrics |
| GET | `/api/config` | Yes | Sanitized config |
| GET | `/api/interfaces` | Yes | Network interfaces |
| GET | `/api/plugins` | Yes | All plugins |
| GET | `/api/plugins/{name}` | Yes | Plugin detail |
| GET | `/api/mesh/nodes` | Yes | Mesh nodes |
| GET | `/api/mesh/telemetry` | Yes | Peer telemetry |
| GET | `/api/reachability` | Yes | Reachability scores |
| GET | `/api/routing` | Yes | Routing table |
| GET | `/api/transport` | Yes | Transport hubs |
| GET | `/api/connectivity` | Yes | Connectivity health |
| GET | `/api/path_warming` | Yes | Path warmer stats |
| GET | `/api/transport_health` | Yes | Transport health |
| GET | `/api/sensors` | Yes | Sensor readings |
| GET | `/api/sensors/history` | Yes | Sensor history |
| GET | `/api/files` | Yes | Shared files |
| GET | `/api/emergency` | Yes | Emergency messages |
| GET | `/api/alerts` | Yes | Alert status |
| GET | `/api/nomadnet/auth` | Yes | NomadNet auth list |
| POST | `/api/nomadnet/auth/add` | Yes | Add identity |
| POST | `/api/nomadnet/auth/remove` | Yes | Remove identity |
| GET | `/api/meshtastic/status` | Yes | Gateway status |
| GET | `/api/meshtastic/nodes` | Yes | Meshtastic nodes |
| GET | `/api/messages` | Yes | Message history |
| POST | `/api/messages/send` | Yes | Send message |
| GET | `/api/messages/transports` | Yes | Transport list |
| GET | `/api/messages/contacts` | Yes | Contact list |
| GET | `/api/messages/stats` | Yes | Message stats |
| GET | `/ws/metrics` | Yes | WebSocket stream |
