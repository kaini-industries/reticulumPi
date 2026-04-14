# Security Policy

## Reporting Vulnerabilities

If you discover a security vulnerability in ReticulumPi, please report it responsibly:

1. **Do NOT open a public GitHub issue** for security vulnerabilities
2. Email the maintainers directly or use GitHub's private vulnerability reporting feature
3. Include:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if you have one)

We aim to acknowledge reports within 48 hours and provide a fix within 7 days for critical issues.

## Security Model

### Network Security

ReticulumPi inherits its network security from the [Reticulum](https://reticulum.network/) cryptographic networking stack:

- **All traffic is end-to-end encrypted** using X25519 key exchange and Fernet symmetric encryption
- **Identity verification** via Ed25519 signatures
- **No IP addresses** are required or exposed for Reticulum communications
- **Zero trust** -- nodes authenticate via cryptographic identity, not network position

ReticulumPi does not modify, fork, or patch Reticulum's cryptographic primitives.

### Web Dashboard Security

The web dashboard (`web_dashboard` plugin) implements:

| Feature | Implementation |
|---------|----------------|
| **Password hashing** | scrypt (N=2^14, r=8, p=2, dklen=32) |
| **Session tokens** | 64-character hex (256 bits of entropy via `secrets.token_hex`) |
| **Rate limiting** | 5 failed logins per IP per 60 seconds |
| **Session management** | Configurable timeout (default 24h), max 5 sessions, LRU eviction |
| **CSP headers** | `default-src 'self'; connect-src 'self' ws: wss:; style-src 'self' 'unsafe-inline'` |
| **Security headers** | `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` |
| **Config sanitization** | Passwords and hashes stripped from `/api/config` response |
| **Input validation** | Length limits on all POST body fields |
| **SSL/TLS** | Optional, with auto-generated self-signed cert support |

**Default bind address:** `127.0.0.1` (loopback only). You must explicitly set `host: 0.0.0.0` to expose the dashboard on the network. When doing so, SSL is strongly recommended.

### Plugin Security

- **Remote Control** (`remote_control` plugin) requires explicit identity whitelisting via `allowed_identities` config
- **File Transfer** (`file_transfer` plugin) supports identity whitelisting via `allowed_identities`
- **NomadNet Auth** supports per-page access control via identity lists
- **Meshtastic Gateway** supports both `meshtastic_allow_list` and `lxmf_allow_list` for message filtering

### Identity Management

- Node identities are persistent Ed25519 key pairs stored as files
- Each LXMF plugin maintains its own identity (no shared keys between services)
- Identity files should have restrictive permissions (the bootstrap script sets these)
- Use `reticulumpi --backup-identity` / `--restore-identity` for safe key management

## Best Practices

### For Operators

1. **Use password hashes, not plaintext passwords** in config files:
   ```bash
   reticulumpi --hash-password
   ```
   Or set the `RETICULUMPI_DASHBOARD_PASSWORD_HASH` environment variable.

2. **Enable SSL** when exposing the dashboard beyond localhost:
   ```yaml
   web_dashboard:
     host: "0.0.0.0"
     ssl:
       enabled: true
       auto_generate: true
   ```

3. **Restrict remote control access** to known identities only:
   ```yaml
   remote_control:
     allowed_identities:
       - "your_identity_hash_here"
   ```

4. **Back up your identity files** -- losing them means losing your network identity:
   ```bash
   reticulumpi --backup-identity /safe/location/identity.bak
   ```

5. **Keep dependencies updated**:
   ```bash
   sudo bash scripts/update.sh
   ```

6. **Review systemd sandboxing** -- the service file uses `ProtectSystem`, `ProtectHome`, `ReadWritePaths`, and other hardening directives

### For Plugin Developers

1. **Never store secrets in plugin status** -- `get_status()` output is visible via the API
2. **Validate all config inputs** in `validate_config()`
3. **Use `self.log`** instead of `print()` for all output
4. **Handle exceptions in event callbacks** -- an unhandled exception in one subscriber doesn't crash others, but it does log a warning
5. **Use `self._sleep_while_active()`** instead of `time.sleep()` in loops for clean shutdown
6. **Sanitize user input** from Reticulum packets -- treat all received data as untrusted

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.2.x   | Current   |
| 0.1.x   | Bug fixes only |
| < 0.1   | Not supported |

## Dependencies

ReticulumPi's security depends on these upstream projects:

- [Reticulum (rns)](https://github.com/markqvist/Reticulum) -- cryptographic networking
- [LXMF](https://github.com/markqvist/lxmf) -- message transport
- [aiohttp](https://docs.aiohttp.org/) -- web server (dashboard only)
- [psutil](https://github.com/giampaolo/psutil) -- system metrics

Keep all dependencies updated. The `scripts/update.sh` script handles this automatically.
