# Troubleshooting

Common problems and solutions for ReticulumPi operators. For plugin-specific issues, see also [Built-in Plugins](plugins.md).

## Quick Diagnostics

Run these first to understand the system state:

```bash
# Service status
sudo systemctl status rnsd reticulumpi

# Recent logs (last 100 lines)
sudo journalctl -u reticulumpi --no-pager -n 100

# Live log stream
sudo journalctl -u reticulumpi -f

# Network interfaces and peer count
sudo -u reticulumpi /srv/reticulumpi/current/.venv/bin/rnstatus

# Validate config without starting
sudo -u reticulumpi /srv/reticulumpi/current/.venv/bin/reticulumpi --check \
  --config /etc/reticulumpi/config.yaml

# List discovered plugins
sudo -u reticulumpi /srv/reticulumpi/current/.venv/bin/reticulumpi --list-plugins
```

---

## Service Won't Start

### Exit Code 226 (Namespace Mount Failure)

**Symptom:** `systemctl status reticulumpi` shows exit code 226.

**Cause:** The systemd service uses `ProtectSystem=strict` with `ReadWritePaths` for sandboxing. The entire filesystem becomes read-only except for paths explicitly listed in `ReadWritePaths`. If any listed directory **doesn't exist**, the namespace mount fails and the service won't start at all.

**Fix:**
```bash
# Create the two service-owned writable roots
sudo install -d -o reticulumpi -g reticulumpi -m 0750 \
  /var/lib/reticulumpi \
  /var/cache/reticulumpi

# Create conventional durable-state subdirectories
sudo -u reticulumpi mkdir -p \
  /var/lib/reticulumpi/.config/reticulumpi \
  /var/lib/reticulumpi/.local/share/reticulumpi \
  /var/lib/reticulumpi/.local/state \
  /var/lib/reticulumpi/.reticulum \
  /var/lib/reticulumpi/.nomadnet \
  /var/lib/reticulumpi/.nomadnet-tui

# If a separately managed MeshChat checkout is configured under durable state:
sudo -u reticulumpi mkdir -p /var/lib/reticulumpi/meshchat/storage

# Restart
sudo systemctl restart reticulumpi
```

The bootstrap script creates these automatically. This only happens with manual installs.

**Understanding `ReadWritePaths`:**

The current service file lists these writable paths:

| Path | Purpose |
|------|---------|
| `/var/lib/reticulumpi` | HOME, Reticulum state, identities, databases, and NomadNet data |
| `/var/cache/reticulumpi` | Disposable tiles, TLEs, and other caches |

If a plugin needs to write to a path not in this list, it will get `sqlite3.OperationalError: attempt to write a readonly database` or `PermissionError: [Errno 13] Permission denied`. The fix is to add the path to `ReadWritePaths` in `/etc/systemd/system/reticulumpi.service` and run `sudo systemctl daemon-reload && sudo systemctl restart reticulumpi`.

> **Important:** Every path in `ReadWritePaths` must exist before the service starts. If you add a new path, create the directory first.

### "No module named reticulumpi"

**Cause:** The venv isn't activated or the package isn't installed.

**Fix:**
```bash
sudo reticulumpi-admin doctor
sudo -u reticulumpi /srv/reticulumpi/current/.venv/bin/python -c \
  "import reticulumpi; print(reticulumpi.__version__)"
sudo systemctl restart reticulumpi
```

### Config Validation Errors

**Symptom:** Logs show "Configuration error" or "Invalid config".

An explicitly selected configuration file is mandatory. If `--config PATH` is supplied (as it
is by the production systemd unit), a missing file aborts startup instead of silently starting
with defaults. Restore `/etc/reticulumpi/config.yaml` from a verified backup and confirm it is a
regular `root:reticulumpi` `0640` file before restarting.

**Fix:**
```bash
# Validate and see the exact error
sudo -u reticulumpi /srv/reticulumpi/current/.venv/bin/reticulumpi --check \
  --config /etc/reticulumpi/config.yaml
```

Common config mistakes:
- Missing `reticulumpi:` top-level key
- Plugin name typo (must match `plugin_name` attribute exactly)
- YAML indentation errors (use spaces, not tabs)
- Boolean values must be `true`/`false` (not `yes`/`no` in strict YAML)

---

## Reticulum Interface Issues

### No Peers Found (rnstatus Shows 0 Peers)

**Possible causes:**

1. **No interfaces configured** -- check `/var/lib/reticulumpi/.reticulum/config` has at least one interface enabled
2. **AutoInterface not discovering** -- ensure you're on the same LAN/subnet, IPv6 multicast must be allowed
3. **TCP hub unreachable** -- verify the hub hostname resolves and port is open:
   ```bash
   nc -zv rns.stoppedcold.com 4242
   ```
4. **Firewall blocking** -- AutoInterface uses IPv6 link-local multicast (port 29716 UDP)

### "Interface [Name] is offline"

**Cause:** A configured interface failed to initialize or lost connection.

**For TCP Client:** The remote hub may be down. Enable transport_monitor with auto_discovery for automatic failover:
```yaml
transport_monitor:
  enabled: true
  auto_discovery:
    enabled: true
    target_connections: 3
```

**For RNode/Serial:** Check the device is connected and the port exists:
```bash
ls /dev/ttyUSB* /dev/ttyACM*
dmesg | tail -20    # check for USB disconnect messages
```

### Reticulum Config Format

**Critical:** Interface sections MUST use double brackets `[[Name]]` under an `[interfaces]` parent section. Single brackets are silently ignored.

**Wrong:**
```ini
[interfaces]

[My Interface]     # WRONG - single brackets
  type = TCPClientInterface
```

**Correct:**
```ini
[interfaces]

[[My Interface]]   # CORRECT - double brackets
  type = TCPClientInterface
```

---

## Web Dashboard Issues

### Can't Access Dashboard

1. **Check it's running:** `systemctl status reticulumpi` and `ss -tlnp | grep 8080`
   (`/api/node` requires a dashboard session or configured local-service Bearer token)
2. **Check bind address:** Default is `127.0.0.1` (loopback only). Set `host: "0.0.0.0"` to access from other devices
3. **Check port:** Default 8080. Verify with `ss -tlnp | grep 8080`
4. **Check firewall:** `sudo ufw allow 8080` (if using ufw)

### Forgot Dashboard Password

There are three ways to reset the dashboard password:

**Option 1: Delete and auto-regenerate (simplest)**

```bash
# Delete the auto-generated secret
sudo -u reticulumpi rm /var/lib/reticulumpi/.config/reticulumpi/dashboard_secret

# Restart -- a new password is written to a protected bootstrap file
sudo systemctl restart reticulumpi

# Read it as root; plaintext passwords are never logged
sudo cat /var/lib/reticulumpi/.config/reticulumpi/dashboard_password.txt
```

**Option 2: Set a specific password via hash**

```bash
# Generate a hash interactively (prompts twice for confirmation)
/srv/reticulumpi/current/.venv/bin/reticulumpi --hash-password

# Add to config
sudo nano /etc/reticulumpi/config.yaml
# Under web_dashboard, add:
#   password_hash: "scrypt:..."

sudo systemctl restart reticulumpi
```

**Password storage details:**
- Hash file: `/var/lib/reticulumpi/.config/reticulumpi/dashboard_secret` (mode 0600)
- Algorithm: scrypt with n=16384, r=8, p=2 (32-byte derived key)
- Format: `scrypt:<salt_hex>:<n>:<r>:<p>:<hash_hex>`
- Preferred source: root-owned `password_hash` in `/etc/reticulumpi/config.yaml`, then the
  auto-generated hash/bootstrap flow. Legacy plaintext environment/config inputs are deprecated
  and emit a critical warning; do not use them for new deployments. During an upgrade, every
  credential-bearing systemd drop-in—including hash overrides and opaque `EnvironmentFile=`
  references—is backed up and removed. Rollback restores it; continued environment-based
  automation requires an explicit post-upgrade review and reprovisioning step.

### "Too Many Login Attempts" (429)

The dashboard rate-limits to 5 failed attempts per IP per 60 seconds. Wait 60 seconds and try again.

### WebSocket Disconnects

**Symptom:** Dashboard shows "disconnected" status, metrics stop updating.

**Causes:**
- Network instability between browser and Pi
- Server restarted
- Max WebSocket clients reached (default: 10)

The dashboard auto-reconnects with exponential backoff. If persistent, check:
```bash
# Are there too many connections?
ss -tn | grep :8080 | wc -l
```

### Content-Security-Policy Warnings in Browser Console

The dashboard intentionally disallows inline scripts, inline event handlers, and inline style
attributes. Record the violated directive, blocked URL, page, browser version, and dashboard
version. If the console identifies a ReticulumPi page or asset, report it as a dashboard bug.
If it identifies a browser-extension URL, reproduce in an incognito/private window with
extensions disabled before reporting it.

---

## Plugin Issues

### Plugin Shows as "Failed"

**Symptom:** Dashboard shows plugin in failed list, or logs show "Plugin X failed to start".

**Debug:**
```bash
# Check for the specific error
sudo journalctl -u reticulumpi -g "plugin_name" --no-pager -n 50
```

**Common causes:**
- Missing optional dependency (`pip install reticulumpi[dashboard]`, `[meshtastic]`, etc.)
- Config validation failure (check required fields)
- Port already in use (for web-serving plugins)
- File permission errors (identity files, database paths)

### NomadNet Won't Start

1. **Check it's installed:** `sudo -u reticulumpi /srv/reticulumpi/current/.venv/bin/python -c "import nomadnet"`
2. **Check shared instance:** `use_shared_instance: true` MUST be set when NomadNet is enabled
3. **Check rnsd is running:** `sudo systemctl status rnsd`
4. **PATH issue:** The plugin falls back to the venv, but check:
   ```bash
   sudo -u reticulumpi /srv/reticulumpi/current/.venv/bin/which nomadnet
   ```

### MeshChat Won't Start

1. **Check the configured external checkout:** `ls /var/lib/reticulumpi/meshchat/`
2. **Check shared instance:** Same as NomadNet -- `use_shared_instance: true` required
3. **Port conflict:** Default port 8000. Check: `ss -tlnp | grep 8000`
4. **Node version:** MeshChat requires Node.js for frontend build

### Meshtastic Gateway Won't Connect

**Serial mode:**
```bash
# Check device is present
ls /dev/ttyUSB* /dev/ttyACM*

# Check permissions
groups reticulumpi    # should include 'dialout'

# Test with meshtastic CLI
sudo -u reticulumpi /srv/reticulumpi/current/.venv/bin/meshtastic --info
```

**MQTT mode:**
```bash
# Test broker connectivity
nc -zv mqtt.meshtastic.org 1883
```

**Common issues:**
- Device running RNode firmware instead of Meshtastic firmware (they're different!)
- Serial port claimed by another process
- MQTT credentials wrong or broker changed

### Messaging Hub: No Messages Appearing

1. **Check plugin is enabled:** Look for `messaging_hub` in dashboard plugin list
2. **Check transports are available:** `curl http://127.0.0.1:8080/api/messages/transports`
3. **LXMF adapter:** The hub's LXMF address is separate from message_echo. Find it:
   ```bash
   sudo journalctl -u reticulumpi -g "messaging_hub.*active at" --no-pager
   ```
4. **Meshtastic adapter:** Requires `meshtastic_gateway` plugin to also be running

---

## Performance Issues

### High CPU Usage

1. **Check which plugin:** Look at logs for tight loops or high-frequency operations
2. **Reduce polling intervals:**
   ```yaml
   system_monitor:
     collect_interval_seconds: 120    # default 60
   transport_monitor:
     check_interval: 30               # default 15
   connectivity_monitor:
     check_interval: 60               # default 30
   ```
3. **Reduce WebSocket frequency:**
   ```yaml
   web_dashboard:
     metrics_interval: 10    # default 5 seconds
   ```

### High Memory Usage

1. **SQLite databases growing:** Check sizes:
   ```bash
   du -sh /var/lib/reticulumpi/.local/share/reticulumpi/*.db
   ```
2. **Reduce retention:**
   ```yaml
   network_map:
     max_history_days: 7        # default 30
   transport_health:
     history_retention_hours: 48  # default 168
   sensor_framework:
     storage:
       retention_days: 7         # default 30
   ```
3. **Limit message history:**
   ```yaml
   messaging_hub:
     message_history_limit: 200   # default 500
   ```

### Disk Space

```bash
# Check overall disk usage
df -h /

# Check ReticulumPi data
du -sh /var/lib/reticulumpi/.local/share/reticulumpi/
du -sh /var/lib/reticulumpi/.nomadnet/
du -sh /var/lib/reticulumpi/.reticulum/
du -sh /var/cache/reticulumpi/
```

SQLite databases can be compacted:
```bash
sudo -u reticulumpi sqlite3 \
  /var/lib/reticulumpi/.local/share/reticulumpi/network_map.db "VACUUM;"
```

---

## Sensor Issues

### Sensors Not Reading

1. **Check driver prerequisites:**
   - DS18B20: `dtoverlay=w1-gpio` in `/boot/config.txt`, reboot required
   - BME280/I2C: `dtparam=i2c_arm=on` in `/boot/config.txt`, `smbus2` package installed
   - ADC: sysfs path must exist

2. **Check permissions:**
   ```bash
   # I2C access
   groups reticulumpi    # should include 'i2c'

   # 1-Wire devices
   ls /sys/bus/w1/devices/
   ```

3. **Test manually:**
   ```bash
   # DS18B20
   cat /sys/bus/w1/devices/28-*/temperature

   # I2C devices
   sudo -u reticulumpi i2cdetect -y 1
   ```

### Sensor History Not Appearing in Dashboard

1. **Check storage is enabled:**
   ```yaml
   sensor_framework:
     storage:
       type: sqlite     # not 'none'
   ```
2. **Check the API:**
   ```bash
   curl http://127.0.0.1:8080/api/sensors
   curl "http://127.0.0.1:8080/api/sensors/history?sensor=cpu_temp"
   ```

---

## Update Issues

### Update Script Fails

If the launcher reports that no trusted system administrator is installed, obtain and install
the independently signed ReticulumPi recovery-administrator package. Do not set `PYTHONPATH`, run
`python -m reticulumpi.admin_cli` from the candidate, or copy an administrator out of the bundle;
those paths execute code before the bundle has been authenticated.

```bash
# Inspect a trusted bundle without changing the system
bash scripts/update.sh --bundle /path/to/release --dry-run

# Check installed state and any interrupted transaction
reticulumpi-admin status --json
reticulumpi-admin doctor
sudo cat /var/backups/reticulumpi/admin/transaction.json
```

The updater does not run `git pull` or resolve local checkout conflicts. Update the source
checkout separately, review it, then pass it as a bundle. If activation failed, run
`reticulumpi-admin rollback --dry-run` before applying rollback.

### Service Won't Restart After Update

```bash
# Check for new systemd file changes
sudo systemctl daemon-reload
sudo systemctl restart reticulumpi

# Check logs for new errors
sudo journalctl -u reticulumpi -f
```

---

## Network Debugging

### Identify Path to a Destination

```bash
# Check if a destination is known
sudo -u reticulumpi /srv/reticulumpi/current/.venv/bin/rnpath <destination_hash>

# Request a path
sudo -u reticulumpi /srv/reticulumpi/current/.venv/bin/rnpath -r <destination_hash>
```

### Check Transport Status

The web dashboard's Routing section shows:
- Full path table with search and filters
- Hop distribution chart
- Path freshness statistics
- Rate-limited and blackholed destinations

Or via API:
```bash
curl http://127.0.0.1:8080/api/routing | python3 -m json.tool
```

### DNS Issues with Transport Hubs

Some community hub hostnames may not resolve. Known working hubs:
- `rns.stoppedcold.com:4242`

Non-resolving hostnames (as of 2026-04):
- `amsterdam.connect.reticulum.network`
- `dublin.connect.reticulum.network`

Use `auto_discovery` in transport_monitor to automatically find working hubs.

---

## Getting Help

1. Check this troubleshooting guide
2. Search existing [GitHub Issues](https://github.com/kaini-industries/reticulumPi/issues)
3. Check the [Reticulum documentation](https://reticulum.network/manual/)
4. Open a new issue with:
   - Your config (sanitize passwords!)
   - Relevant log output
   - Steps to reproduce
   - System info: `uname -a`, `python3 --version`, `pip show rns`
