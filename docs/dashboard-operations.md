# Dashboard Operations

## First login

The dashboard binds to `127.0.0.1:8080` by default. After first start, retrieve the generated
password from the protected file—not the journal:

```bash
sudo cat /var/lib/reticulumpi/.config/reticulumpi/dashboard_password.txt
```

Sign in with that bootstrap value and use the required password-change dialog to choose a
new password of at least 12 characters. The bootstrap file remains present, and dashboard
API/WebSocket access remains restricted, until the replacement hash is atomically persisted.
Successful change removes the file, invalidates every session, closes associated WebSockets,
and returns you to login. Do not delete `dashboard_secret` as a normal rotation workflow.

Automation can perform the same authenticated operation with
`POST /api/auth/password`, supplying `current_password` and `new_password` JSON fields plus
the normal session cookie and `X-Requested-With` header. Passwords supplied through the
environment or system configuration are operator-managed and must be rotated at that source.

## Health and restart operations

```bash
systemctl status reticulumpi rnsd
journalctl -u reticulumpi --since "15 minutes ago"
reticulumpi-admin status --json
reticulumpi-admin doctor
```

Dashboard restart requests are asynchronous. A successful request returns HTTP 202 and an
operation identifier; poll the operation status instead of assuming a spawned subprocess
succeeded. The dashboard fails closed when the control broker is unavailable and never
invokes `sudo` or `systemctl`; an administrator may restart manually through systemd.

## Operational metrics

Authenticated `GET /api/status` includes a fixed-schema, secret-free `operational_metrics`
object. The same snapshot is available to local automation through the scoped read-only token.

| Required signal | Snapshot field | Semantics |
|---|---|---|
| Lifecycle and readiness duration | `lifecycle.states`, `lifecycle.health`, `lifecycle.readiness` | Current state counts plus readiness count/total/maximum duration |
| Hung or abandoned workers | `workers`, `dashboard.workers` | Process-lifetime lifecycle, EventBus, announce, and broadcast timeout totals where detectable |
| Cleanup failures | `lifecycle.cleanup_failures_total` | Process-lifetime count held by currently registered plugins |
| RNS resources | `rns_resources` | Current managed links, destinations, and request handlers |
| Thread and process counts | `threads`, `processes` | Current PluginBase/runtime thread gauges and live managed/raw child-process gauges |
| Callback drops | `callbacks`, `event_bus`, `announce_dispatcher` | Fixed-source pending, drop, disabled, and abandoned-worker counters |
| Process restarts | `processes.restarts`, `processes.restarts_total` | Current managed-group restart gauge plus a process-lifetime restart counter across managed and legacy supervisors |
| Migration results and SQLite failures | `migrations`, `sqlite` | Process-lifetime migration/restore outcomes, all instrumented runtime SQLite failures, and the migration-only failure subset |
| SDR leases | `sdr` | Current scheduler gauges plus canonical device claims, including leases held outside the scheduler |
| WebSocket close reasons | `dashboard.websocket.close_reasons` | Process-lifetime fixed close-code categories |
| Authentication saturation | `dashboard.auth_admission` | Attempts/outcomes plus bounded in-flight and peak gauges |
| Cache refreshes | `dashboard.api_cache_refresh` | Process-lifetime outcomes plus current pending gauge |
| Tile usage | `dashboard.tile_cache` | Process-lifetime outcomes plus last-reconciled byte usage and configured capacity gauges |
| Service-worker version | `dashboard.service_worker.version` | Bounded server-expected shell/cache version |

`threads.live` is retained as the compatibility name for PluginBase-managed threads;
`runtime_live` and `runtime_daemon` cover the complete interpreter. `processes.total_live`
does not include the ReticulumPi service process itself. A service restart resets cumulative
counters; current gauges are recomputed for each snapshot.

These metrics deliberately omit request paths, cache filenames, client addresses, local API
and session tokens, operator credentials, and tile upstream URLs. WebSocket codes and tile
errors are mapped to a fixed set of categories rather than emitted as unbounded labels. The
service-worker version is the bounded package version expected by the server for the versioned
browser cache. Compare it with browser diagnostics when investigating a stale controller; the
server metric does not claim to observe which worker is currently controlling a particular tab.

## Remote access

Prefer an SSH tunnel for occasional administration:

```bash
ssh -L 8080:127.0.0.1:8080 pi@node.local
```

For LAN binding, enable TLS, restrict `allowed_networks`, and place authentication rate-limit
logs under normal monitoring. Never expose the dashboard directly to the public internet.

Dashboard plugin status exposes a secret-free `tls` object with `managed`, `state`,
`last_check`, `last_renewal`, and `reason`. Managed certificates are checked daily or sooner
at the expiry guard and renew at 30 days remaining. `degraded` means the previous valid certificate was restored after a
renewal/reload failure; inspect the journal and either allow the next daily retry or restart
after correcting storage permissions. `failed_closed` means the HTTPS listener has stopped.
For operator-managed files, install a validated matching pair, declare its required SANs, and
restart the service; the running process deliberately does not overwrite or automatically
reopen operator material.

## Offline behavior

The service worker caches only versioned static shell assets. Navigation is network-first
with separate cached fallbacks for login, dashboard, and spectrum documents, so refreshing
the spectrum page can never replace the dashboard's offline shell. API and authentication
responses are never stored. The UI retains last-rendered values during disconnects but marks
them stale. Only idempotent GET requests are retried automatically.

The automated Chromium service-worker lane performs a real first install, inspects Cache
Storage to reject API/auth and unopened feature entries, forces the browser offline, and
reloads both dashboard and spectrum documents from their distinct worker-controlled shell
entries. Static regressions separately verify network-first navigation, install/fetch
lifetime attachment, and versioned cache retention.
Interrupted-update and rollback drills remain part of release-candidate browser qualification.

When diagnosing an offline node, distinguish network loss from a stale browser shell:

1. Check the connection and stale-data indicators.
2. Reload once while connected directly to the node LAN.
3. Inspect service logs and `/api/status` with a valid session/local token.
4. Clear site data only after preserving relevant browser console errors.

## Backups

Back up the node identity, dashboard secret directory, system configuration, and application
databases. Treat backups as secrets and verify the identity hash after restore. See
[Upgrade and Rollback](upgrade-and-rollback.md).
