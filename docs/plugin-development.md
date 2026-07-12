# Plugin Development Guide

This guide covers everything you need to build a ReticulumPi plugin -- from a minimal "hello world" to a production-quality plugin with LXMF messaging, SQLite storage, event-driven architecture, and dashboard integration.

For a quick-reference list of all built-in plugins and their options, see [Built-in Plugins](plugins.md).

## Quick Start

### 1. Copy the Scaffold

```bash
mkdir -p ~/my_plugins
cp plugins/example_plugin.py ~/my_plugins/my_plugin.py
```

### 2. Edit the Plugin

```python
# ~/my_plugins/my_plugin.py
from reticulumpi.plugin_base import PluginBase

class MyPlugin(PluginBase):
    plugin_name = "my_plugin"
    plugin_version = "1.0.0"
    plugin_description = "Does something useful"

    def start(self):
        self._active = True
        self.log.info("My plugin started!")

    def stop(self):
        self._active = False
        self.log.info("My plugin stopped.")
```

### 3. Enable It

Add to your `config.yaml`:

```yaml
reticulumpi:
  plugin_paths:
    - ~/my_plugins

  plugins:
    my_plugin:
      enabled: true
```

### 4. Test It

```bash
reticulumpi --list-plugins      # should show my_plugin
reticulumpi --check             # validate config
reticulumpi --config config.yaml  # run it
```

---

## Plugin Lifecycle

Understanding the lifecycle is essential for writing robust plugins:

```
Discovery → Instantiation → Config Validation → start() → [running] → stop()
```

### 1. Discovery

`PluginLoader` scans directories for `.py` files and looks for classes inheriting from `PluginBase` with a valid `plugin_name` (not `"unnamed"`).

**Scan order:**
1. `src/reticulumpi/builtin_plugins/` (always)
2. `plugins/` in the project root (for development)
3. Any directories listed in `plugin_paths` config

Files starting with `_` are skipped. Each file is imported into its own namespace.

### 2. Instantiation

Only plugins with `enabled: true` in config are instantiated. The constructor receives:

```python
def __init__(self, app, plugin_config):
    super().__init__(app, plugin_config)
    # self.app      - ReticulumPiApp instance
    # self.config   - this plugin's YAML config (dict)
    # self.rns      - RNS.Reticulum instance
    # self.identity - RNS.Identity (node identity)
    # self.event_bus - EventBus for pub/sub
    # self.log      - logging.Logger
    # self._active  - lifecycle flag (starts False)
    # self._threads - tracked thread list
```

### 3. Config Validation

Override `validate_config()` to check required settings early:

```python
def validate_config(self):
    if "api_key" not in self.config:
        raise ValueError("api_key is required")
    if not isinstance(self.config.get("interval", 60), (int, float)):
        raise ValueError("interval must be a number")
```

This runs during `__init__`, before `start()`. If it raises, the plugin is marked as failed and never started.

### 4. start()

Called once after all plugins are instantiated. This is where you:
- Set `self._active = True`
- Create RNS destinations
- Register announce handlers
- Start background threads
- Subscribe to events
- Open database connections

### 5. Running

Your plugin runs until shutdown. Background work happens in threads started via `self._start_thread()`.

### 6. stop()

Called on SIGTERM/SIGINT (in reverse plugin order). This is where you:
- Set `self._active = False`
- Close database connections
- Cancel timers
- Call `self._join_threads()` to wait for threads to finish

Resources registered with the managed-resource helpers are cleaned automatically, exactly
once and in reverse registration order, after normal stop, failed start, or timeout.

---

## Base Class Helpers

`PluginBase` provides several helpers to simplify common patterns:

### Managed resource ownership

Register acquired resources immediately, before publishing readiness:

```python
def start(self):
    self._active = True
    self._executor = self.manage_executor(ThreadPoolExecutor(max_workers=2))
    self._task = self.manage_async_task(loop.create_task(self._poll()))
    self._destination = self.manage_destination(
        RNS.Destination(self.identity, RNS.Destination.IN, RNS.Destination.SINGLE,
                        "reticulumpi", "example")
    )
    self._destination.register_request_handler("/status", response_generator=self._status)
    self.manage_request_handler(self._destination, "/status")
    self.manage_subscription(self.event_bus.subscribe("example", self._on_event))
    self.mark_ready()  # lifecycle API v2 only, after every resource is usable
```

- `register_cleanup(callback, *args, **kwargs)` owns any idempotent custom cleanup.
- `manage_subscription`, `manage_link`, and `manage_destination` detach RNS/event resources.
- `manage_process` and `manage_process_group` terminate complete external workloads.
- `manage_executor` cancels queued futures with `wait=False`; it never blocks an event loop.
- `manage_async_task` cancels on the task's owning loop, using a thread-safe wakeup when
  cleanup runs elsewhere.
- `manage_request_handler(destination, path)` deregisters the exact RNS handler path.

Do not also tear down the same resource concurrently in `stop()`. Use `request_stop()` or
`on_stop_requested()` to interrupt blocking work, then let reverse-order managed cleanup
dispose of ownership. Managed callbacks must be idempotent because timeout/failure paths may
converge on cleanup. A resource registered after cleanup has already completed is immediately
cleaned on a bounded daemon worker and registration raises `RuntimeError`; plugin code must not
catch that error and continue serving.

### Thread Management

```python
def start(self):
    self._active = True
    # Start a tracked daemon thread
    self._start_thread(self._background_loop, name="my-loop")

def _background_loop(self):
    while self._active:
        self._do_work()
        # Sleep interruptibly -- wakes early if _active becomes False
        self._sleep_while_active(60)

def stop(self):
    self._active = False
    # Wait for all tracked threads to finish (5s timeout)
    self._join_threads(timeout=5.0)
```

**Key points:**
- Always use `self._sleep_while_active()` instead of `time.sleep()` in loops
- Always call `self._join_threads()` in `stop()`
- Threads started via `_start_thread()` are daemon threads (won't prevent shutdown)

### Subprocess Log Reader

For plugins that manage subprocesses (like NomadNet, MeshChat):

```python
self._process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
self._start_log_reader(self._process, prefix="my-daemon")
```

### Status Reporting

```python
def get_status(self):
    return {
        "active": self._active,
        "messages_processed": self._count,
        "last_run": self._last_run_time,
    }
```

This data appears in the dashboard plugin list and the `/api/plugins/<name>` endpoint.

**Warning:** Never include secrets, passwords, or sensitive data in status output.

---

## Event Bus

The complete constant-to-event-name mapping is generated directly from `reticulumpi.events`
in the [event bus inventory](generated-code-reference.md#event-bus-constants). Documentation
CI rejects the snapshot when a constant is added, removed, renamed, or duplicated.

The event bus enables decoupled communication between plugins.

### Subscribing

```python
from reticulumpi import events

def start(self):
    self._active = True
    self.event_bus.subscribe(events.METRICS_UPDATED, self._on_metrics)
    self.event_bus.subscribe(events.NODE_DISCOVERED, self._on_node)

def _on_metrics(self, event_type, data):
    cpu = data.get("cpu_percent", 0)
    if cpu > 90:
        self.log.warning(f"CPU at {cpu}%!")

def stop(self):
    self._active = False
    # Always unsubscribe to prevent callbacks after shutdown
    self.event_bus.unsubscribe(events.METRICS_UPDATED, self._on_metrics)
    self.event_bus.unsubscribe(events.NODE_DISCOVERED, self._on_node)
```

### Publishing

```python
self.event_bus.publish(events.ALERT_TRIGGERED, {
    "level": "warning",
    "message": "Disk usage above 90%",
    "plugin": self.plugin_name,
    "timestamp": time.time(),
})
```

### Available Events

| Event | Data Fields | Published By |
|-------|-------------|-------------|
| `PLUGIN_STARTED` | `name` | app.py |
| `PLUGIN_STOPPED` | `name` | app.py |
| `PLUGIN_CRASHED` | `name`, `error` | app.py |
| `METRICS_UPDATED` | `cpu_percent`, `cpu_temp`, etc. | system_monitor |
| `NODE_DISCOVERED` | `destination_hash`, `app_name`, etc. | network_map |
| `NODE_METRICS_RECEIVED` | `source_hash`, metrics dict | mesh_telemetry |
| `ALERT_TRIGGERED` | `level`, `message` | alert_system |
| `FILE_RECEIVED` | `filename`, `size`, `sender` | file_transfer |
| `LINK_ESTABLISHED` | `link`, `identity` | remote_control |
| `LINK_CLOSED` | `link` | remote_control |
| `SENSOR_READING` | `sensor_name`, readings dict | sensor_framework |
| `EMERGENCY_RECEIVED` | `message`, `origin`, `ttl` | emergency_broadcast |
| `HUB_ONLINE` | `name`, `host`, `port` | transport_monitor |
| `HUB_OFFLINE` | `name`, `host`, `port` | transport_monitor |
| `FALLBACK_ACTIVATED` | `hub` | transport_monitor |
| `RNSD_DOWN` | -- | connectivity_monitor |
| `RNSD_RECOVERED` | -- | connectivity_monitor |
| `INTERFACE_OFFLINE` | `name` | connectivity_monitor |
| `PATH_TABLE_EMPTY` | -- | connectivity_monitor |
| `PATHS_STALE` | `count`, `threshold` | connectivity_monitor |
| `MESHTASTIC_CONNECTED` | `mode` | meshtastic_gateway |
| `MESHTASTIC_DISCONNECTED` | `reason` | meshtastic_gateway |
| `MESHTASTIC_MESSAGE_RECEIVED` | `text`, `from_id`, `from_name` | meshtastic_gateway |
| `MESSAGE_RECEIVED` | `transport`, `text`, `from_id` | messaging_hub |
| `MESSAGE_SENT` | `transport`, `text`, `to_id` | messaging_hub |

### Event Callback Rules

- Callbacks run **synchronously** in the publisher's thread
- Exceptions in callbacks are caught and logged (other subscribers still execute)
- Keep callbacks fast -- heavy work should be dispatched to a background thread
- Always unsubscribe in `stop()` to prevent callbacks on dead plugins

---

## Inter-Plugin Communication

### Direct Access

```python
# Read metrics from system_monitor
monitor = self.app.get_plugin("system_monitor")
if monitor and hasattr(monitor, "latest_metrics"):
    cpu = monitor.latest_metrics.get("cpu_percent", 0)

# Use path_warmer before sending LXMF
warmer = self.app.get_plugin("path_warmer")
if warmer and hasattr(warmer, "ensure_path"):
    warmer.ensure_path(destination_hash)

# Get mesh nodes from network_map
netmap = self.app.get_plugin("network_map")
if netmap and hasattr(netmap, "get_known_nodes"):
    nodes = netmap.get_known_nodes()
```

**Best practices:**
- Always check for `None` (plugin might not be enabled)
- Use `hasattr()` checks for defensive programming
- `get_plugin()` is thread-safe
- Access the plugin's public API only -- don't reach into private attributes

### Registering with Other Plugins

Some plugins accept registrations from others. Example -- the messaging hub:

```python
def start(self):
    self._active = True
    hub = self.app.get_plugin("messaging_hub")
    if hub:
        hub.register_adapter(MyCustomAdapter(self))
```

---

## Common Patterns

### LXMF Messaging Plugin

Many plugins need their own LXMF identity for sending/receiving messages:

```python
import os
import RNS
import LXMF

from reticulumpi.lxmf_compat import create_lxm_router

class MyLXMFPlugin(PluginBase):
    plugin_name = "my_lxmf_plugin"
    plugin_version = "1.0.0"

    def start(self):
        # Initialize cleanup-visible fields before any fallible operation.
        self._active = False
        self._router = None
        self._dest = None

        # Create a separate identity for this plugin
        storage = os.path.expanduser(
            self.config.get(
                "storage_path",
                "/var/lib/reticulumpi/.local/share/reticulumpi/my_plugin_lxmf",
            )
        )
        os.makedirs(storage, exist_ok=True)
        id_path = os.path.join(storage, "identity")

        if os.path.exists(id_path):
            self._lxmf_identity = RNS.Identity.from_file(id_path)
        else:
            self._lxmf_identity = RNS.Identity()
            self._lxmf_identity.to_file(id_path)

        # Lifecycle calls run on managed daemon workers. This helper preserves
        # the application's process-level signal handlers while constructing
        # LXMF safely from either the main thread or a lifecycle worker.
        self._router = create_lxm_router(
            identity=self._lxmf_identity,
            storagepath=storage,
        )
        self._dest = self._router.register_delivery_identity(
            self._lxmf_identity,
            display_name=self.config.get("display_name", f"{self.app.node_name} MyPlugin"),
        )
        self._router.register_delivery_callback(self._on_message)
        self._active = True

        self.log.info(f"LXMF active at {RNS.prettyhexrep(self._dest.hash)}")

    def _on_message(self, message):
        sender = RNS.prettyhexrep(message.source_hash)
        content = message.content.decode("utf-8") if message.content else ""
        self.log.info(f"Message from {sender}: {content}")

        # Reply
        reply = LXMF.LXMessage(
            RNS.Destination(
                RNS.Identity.recall(message.source_hash),
                RNS.Destination.OUT, RNS.Destination.SINGLE,
                "lxmf", "delivery",
            ),
            self._dest,
            f"Got your message: {content}",
        )
        self._router.handle_outbound(reply)

    def stop(self):
        self._active = False
        if self._router is not None:
            self._router.register_delivery_callback(None)
```

### SQLite Storage

Follow the patterns from `transport_health.py` and `network_map.py`:

```python
import sqlite3
import threading

class MyStoragePlugin(PluginBase):
    plugin_name = "my_storage"
    plugin_version = "1.0.0"

    def start(self):
        self._active = True
        db_path = os.path.expanduser(
            self.config.get(
                "db_path",
                "/var/lib/reticulumpi/.local/share/reticulumpi/my_data.db",
            )
        )
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db_lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._db_lock:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    value REAL NOT NULL
                )
            """)
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ts ON readings(timestamp)"
            )
            self._db.commit()

    def store_reading(self, value):
        with self._db_lock:
            self._db.execute(
                "INSERT INTO readings (timestamp, value) VALUES (?, ?)",
                (time.time(), value),
            )
            self._db.commit()

    def get_readings(self, limit=100):
        with self._db_lock:
            rows = self._db.execute(
                "SELECT timestamp, value FROM readings ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"timestamp": r[0], "value": r[1]} for r in rows]

    def stop(self):
        self._active = False
        if hasattr(self, "_db"):
            self._db.close()
```

### Background Worker with Periodic Task

```python
class MyWorker(PluginBase):
    plugin_name = "my_worker"
    plugin_version = "1.0.0"

    def validate_config(self):
        interval = self.config.get("interval", 60)
        if not isinstance(interval, (int, float)) or interval < 1:
            raise ValueError("interval must be a positive number")

    def start(self):
        self._active = True
        self._interval = self.config.get("interval", 60)
        self._start_thread(self._worker_loop, name="my-worker")

    def _worker_loop(self):
        while self._active:
            try:
                self._do_periodic_work()
            except Exception:
                self.log.exception("Error in worker loop")
            self._sleep_while_active(self._interval)

    def _do_periodic_work(self):
        # Your periodic task here
        self.log.debug("Doing work...")

    def stop(self):
        self._active = False
        self._join_threads(timeout=5.0)
```

### RNS Destination with Packet Handler

```python
import RNS

class MyDestPlugin(PluginBase):
    plugin_name = "my_dest"
    plugin_version = "1.0.0"

    def start(self):
        self._active = True

        # Create a destination
        self._dest = RNS.Destination(
            self.identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            self.config.get("app_name", "reticulumpi"),
            self.config.get("aspect", "myservice"),
        )

        # Register packet callback
        self._dest.set_packet_callback(self._on_packet)

        # Announce presence
        self._dest.announce()
        self.log.info(f"Listening at {RNS.prettyhexrep(self._dest.hash)}")

    def _on_packet(self, data, packet):
        self.log.info(f"Received {len(data)} bytes from {packet.source_hash}")

        # Send proof (acknowledgement)
        if packet.receipt:
            packet.receipt.prove()

    def get_status(self):
        return {
            "active": self._active,
            "address": RNS.prettyhexrep(self._dest.hash) if self._dest else None,
        }

    def stop(self):
        self._active = False
```

---

## Package Plugins (Multi-File)

For complex plugins, use a package directory with a shim:

```
src/reticulumpi/builtin_plugins/
  my_complex_plugin/
    __init__.py
    plugin.py          # PluginBase subclass
    database.py        # Database layer
    handlers.py        # Request handlers
  my_complex_plugin.py  # Shim for loader discovery
```

The shim file (`my_complex_plugin.py`):

```python
# This shim allows the plugin loader to discover the package plugin.
from reticulumpi.builtin_plugins.my_complex_plugin.plugin import MyComplexPlugin

__all__ = ["MyComplexPlugin"]
```

The web dashboard uses this pattern -- see `web_dashboard.py` (shim) and `web_dashboard/plugin.py` (real class).

---

## Testing Your Plugin

### Test Structure

```python
# tests/test_my_plugin.py
import pytest
from unittest.mock import MagicMock, patch

class TestMyPlugin:
    def setup_method(self):
        self.mock_app = MagicMock()
        self.mock_app.node_name = "TestNode"
        self.mock_app.get_plugin.return_value = None
        self.config = {"enabled": True, "interval": 30}

    def test_start_sets_active(self):
        from reticulumpi.builtin_plugins.my_plugin import MyPlugin
        plugin = MyPlugin(self.mock_app, self.config)
        plugin.start()
        assert plugin._active is True
        plugin.stop()

    def test_validate_config_rejects_bad_interval(self):
        from reticulumpi.builtin_plugins.my_plugin import MyPlugin
        with pytest.raises(ValueError, match="interval"):
            MyPlugin(self.mock_app, {"enabled": True, "interval": -1})

    def test_get_status(self):
        from reticulumpi.builtin_plugins.my_plugin import MyPlugin
        plugin = MyPlugin(self.mock_app, self.config)
        plugin.start()
        status = plugin.get_status()
        assert status["active"] is True
        plugin.stop()
```

### Mocking RNS/LXMF

Tests must run without a real Reticulum instance:

```python
@patch("reticulumpi.builtin_plugins.my_plugin.RNS")
@patch("reticulumpi.builtin_plugins.my_plugin.create_lxm_router")
def test_lxmf_setup(self, mock_router_factory, mock_rns):
    mock_router = MagicMock()
    mock_router_factory.return_value = mock_router

    plugin = MyPlugin(self.mock_app, self.config)
    plugin.start()

    mock_router_factory.assert_called_once()
    mock_router.register_delivery_callback.assert_called_once()
    plugin.stop()
```

### Running Tests

```bash
# Run your plugin's tests
python -m pytest tests/test_my_plugin.py -v

# Run with coverage
python -m pytest tests/test_my_plugin.py --cov=reticulumpi.builtin_plugins.my_plugin

# Full suite (ensure you didn't break anything)
make test
```

---

## Dashboard Integration

If you want your plugin's data to appear in the web dashboard, you have two options:

### Option 1: Status API (Automatic)

Any data returned by `get_status()` automatically appears in:
- Dashboard plugin list
- `/api/plugins/<name>` endpoint
- WebSocket broadcast

```python
def get_status(self):
    return {
        "active": self._active,
        "my_metric": self._current_value,
        "last_update": self._last_update_time,
    }
```

### Option 2: Custom API Endpoints

For richer integration, add routes to the web dashboard's API. This requires modifying `web_dashboard/api.py` -- see the existing endpoints as examples.

---

## Distribution

### For Personal Use

1. Place plugin file(s) in any directory
2. Add the directory to `plugin_paths` in config
3. Enable the plugin

### Contributing Upstream

1. Place in `src/reticulumpi/builtin_plugins/`
2. Add config documentation to `config/reticulumpi/config.example.yaml`
3. Add a section to `docs/plugins.md`
4. Write tests in `tests/test_<name>.py`
5. Update plugin count in `README.md`
6. Open a pull request

### Third-Party Distribution

Publish as a Python package that users install alongside reticulumpi:

```bash
pip install reticulumpi-my-plugin
```

Users add the package's plugin directory to `plugin_paths`. The plugin loader will discover it like any other directory.

---

## Reference

### PluginBase API

| Attribute/Method | Type | Description |
|-----------------|------|-------------|
| `self.app` | `ReticulumPiApp` | Application instance |
| `self.rns` | `RNS.Reticulum` | Reticulum instance |
| `self.identity` | `RNS.Identity` | Node identity |
| `self.config` | `dict` | Plugin config from YAML |
| `self.log` | `logging.Logger` | Named logger |
| `self.event_bus` | `EventBus` | Pub/sub bus |
| `self._active` | `bool` | Lifecycle flag |
| `self._threads` | `list` | Tracked threads |
| `start()` | abstract | Called at startup |
| `stop()` | abstract | Called at shutdown |
| `validate_config()` | optional | Validate config (raise on error) |
| `get_status()` | optional | Return monitoring dict |
| `_start_thread(target, name)` | helper | Start and track a daemon thread |
| `_join_threads(timeout)` | helper | Wait for all tracked threads |
| `_sleep_while_active(seconds)` | helper | Interruptible sleep |
| `_start_log_reader(process, prefix)` | helper | Pipe subprocess output to logger |

### ReticulumPiApp API

| Method | Returns | Description |
|--------|---------|-------------|
| `app.get_plugin(name)` | `PluginBase | None` | Get a running plugin by name |
| `app.enable_plugin(name)` | `bool` | Hot-load a plugin at runtime |
| `app.disable_plugin(name)` | `bool` | Hot-unload a plugin at runtime |
| `app.node_name` | `str` | Configured node name |
| `app.reticulum` | `RNS.Reticulum` | Reticulum instance |
| `app.identity` | `RNS.Identity` | Node identity |
| `app.event_bus` | `EventBus` | Event bus instance |

### EventBus API

| Method | Description |
|--------|-------------|
| `subscribe(event_type, callback)` | Register callback for event type |
| `unsubscribe(event_type, callback)` | Remove callback |
| `publish(event_type, data)` | Fire event to all subscribers |
