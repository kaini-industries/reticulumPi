# Dashboard Security

## Authentication order

Public login/static routes are handled first. Every other request is checked in this order:

1. A valid dashboard session or Bearer session token.
2. For fixed read-only paths only, a valid local-service Bearer token from loopback.
3. Otherwise a redirect to login for HTML or HTTP 401 for API clients.

Authenticated state-changing requests also require `X-Requested-With`. Local-service tokens
cannot mutate state or establish WebSockets.

Password verification and rotation run in a dedicated two-worker pool with four admission
slots. Saturation or an unavailable admission boundary returns HTTP 503; requests are never
placed into an unbounded authentication queue.

## Local-service token

```yaml
reticulumpi:
  plugins:
    web_dashboard:
      local_api:
        enabled: true
        # token_file: /custom/runtime/path/local_api.token
```

By default, the token is atomically written to `/run/reticulumpi/local_api.token` as mode
`0600` and rotated on every dashboard start. The service falls back to its secret directory
only in development environments where the runtime directory is unavailable, with a
warning. `token_file` is an explicit path override, not a request to persist token value
across restarts. Use the current file only from a process running as the service account:

```bash
TOKEN=$(cat /run/reticulumpi/local_api.token)
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/api/status
```

The token's fixed scope is version, status, node, and interface reads. Do not place it in a
URL, log, browser storage, Nomad page output, or proxy configuration. Reverse proxies often
appear as loopback; they must strip any client-supplied `Authorization` value before adding
their own dashboard credentials and must never use the local token.

## Sessions and WebSockets

Sessions are bounded and persisted. Logout, expiry, and password rotation close associated
WebSockets immediately; passive sockets are periodically revalidated. WebSocket capacity is
reserved atomically and inbound messages are limited to 64 KiB.

## TLS

For remote binding, use operator-managed certificates:

```yaml
web_dashboard:
  host: "0.0.0.0"
  ssl:
    enabled: true
    cert_file: /etc/ssl/certs/reticulumpi.pem
    key_file: /etc/ssl/private/reticulumpi.key
    extra_hostnames: ["reticulumpi.example.net"]
```

Both files and at least one expected SAN are required. The private key must be a single-link
regular file with no access broader than owner read/write plus optional group read (normally
mode `0600` or root-owned/group-readable `0640`); the certificate must not be
group/other writable. Symlinks, unsafe ownership, an incomplete pair, key mismatch, missing
configured SAN, corrupt PEM, a future-dated certificate, or a certificate inside the one-day
validity guard fail closed. Operator files are validation-only inputs:
ReticulumPi never repairs, replaces, or deletes them, even if `auto_generate` is also set.

For a managed self-signed certificate:

```yaml
web_dashboard:
  host: "0.0.0.0"
  ssl:
    enabled: true
    auto_generate: true
    cert_dir: /var/lib/reticulumpi/.config/reticulumpi/web_certs
    extra_hostnames: ["reticulumpi.local"]
```

Managed certificate and key material is stored together in a mode-`0600`
`dashboard.pem` bundle, making publication one same-directory atomic replacement. The
former `dashboard.crt`/`dashboard.key` managed layout is migrated to this bundle after its
pair is verified. A check runs daily or sooner when the 30-day renewal boundary approaches,
and renews when validity reaches that boundary or its required SAN set changes. The new bundle is parsed in a fresh TLS context before the
live context is reloaded; existing TLS connections finish normally and new handshakes use
the renewed certificate.

If live reload fails while the prior certificate is still valid, the prior bundle is
restored and plugin health becomes degraded so the next daily check can retry. If no valid
fallback exists—or operator material becomes invalid—the HTTPS listener stops and remains
failed closed until the files are corrected and the service is restarted. Terminating TLS
at a reverse proxy is also acceptable when the proxy-to-dashboard path is trusted and
authentication headers are handled as described above. Configure that trust explicitly:

```yaml
web_dashboard:
  reverse_proxy:
    enabled: true
    trusted_networks: ["127.0.0.1/32", "::1/128"]
```

The immediate proxy must replace (not append to) `X-Forwarded-Proto` with the external scheme.
Only an exact `https` value from a peer in `trusted_networks` causes Secure cookies and HSTS.
An untrusted peer, missing header, or comma-separated value fails closed as plain HTTP. Keep
`trusted_networks` limited to proxy addresses; never place a client or LAN subnet in it.

## Bootstrap password

An automatically generated password is written atomically to
`dashboard_password.txt` as mode `0600`; its value is never logged. A successful login does
not delete this file. The login response sets `password_change_required: true`, and the
session is restricted to password change and logout until `POST /api/auth/password`
durably replaces `dashboard_secret`. The change revokes all sessions, closes their
WebSockets, deletes the bootstrap file, and requires a fresh login. Passwords supplied by
environment or configuration remain operator-managed and cannot be overwritten through
the dashboard.

During a bridge upgrade, the root administrator rotates only credentials with concrete legacy
exposure evidence: a plaintext Dashboard setting, a plaintext value embedded in the replaced
legacy unit, an outstanding bootstrap file, or a legacy/malformed auto-managed hash. It removes
the plaintext configuration line while the service is stopped, writes a new auto-managed hash
and mode-`0600` bootstrap file atomically, deletes the session database/WAL/SHM, and never emits
the generated value to stdout, stderr, the journal, or logs. A plaintext unit environment can
be rotated automatically only when a complete signed source/install bundle replaces that unit;
a wheel-only upgrade fails closed. Explicit modern `password_hash` configuration is
operator-managed and is preserved. Credential-bearing `reticulumpi.service.d` fragments are
different: fragments containing plaintext, a password hash, or any `EnvironmentFile=` directive
are included in the verified managed-file backup and removed while the service is stopped. An
automatic rollback restores them. Operators that intentionally provision credentials through a
protected environment must review and recreate that override explicitly after a successful
upgrade; the transaction does not trust an opaque environment file across the boundary.

## Response policy

Security headers apply to successes, redirects, and errors. Authenticated API caching is
private and varies by credentials; auth, configuration, restart, and other sensitive
responses use `no-store`. Tile responses are authenticated, streamed with size limits, and
require an `image/png` media type plus the full PNG signature before caching. Concurrent
misses share one temporary lock, which is discarded after the last waiter completes.

## Content Security Policy

Dashboard scripts and styles are packaged, content-addressed resources served from the same
origin. The response policy does not grant `unsafe-inline`: `script-src` and `style-src`
allow only `'self'`, while `script-src-attr` and `style-src-attr` are explicitly `'none'`.
Templates and first-party panel renderers therefore must not add `<style>` elements, style
attributes, `cssText`, or inline event handlers. Live telemetry may update bounded CSSOM
properties such as a progress bar's width after validating and clamping the numeric value.

A reverse proxy may add stricter directives, but should not relax this policy. Treat a CSP
violation whose blocked URL is a ReticulumPi resource as a defect and capture the directive,
blocked URL, page, browser version, and dashboard version when reporting it.
