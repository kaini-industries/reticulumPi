"""WebSocket handler for real-time metrics streaming."""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import functools
import json
import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Any

import aiohttp
import aiohttp.web


from reticulumpi.builtin_plugins.web_dashboard.broadcast_registry import (
    BroadcastRegistry,
)

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


def _check_ws_origin(request: aiohttp.web.Request) -> bool:
    """Reject cross-origin WebSocket upgrades (CSWSH mitigation)."""
    origin = request.headers.get("Origin")
    if not origin:
        return True
    try:
        from urllib.parse import urlparse

        origin_host = urlparse(origin).netloc
    except Exception:
        return False
    return origin_host == request.host


_SWEEP_COUNT_KEYS = frozenset({"spectrum", "lora_scanner", "link_tester"})


def _diff_payload(data: dict[str, Any], prev: dict[str, Any]) -> dict[str, Any]:
    """Return only keys whose value changed since last broadcast."""
    result = {}
    for key, value in data.items():
        if key in _SWEEP_COUNT_KEYS:
            prev_val = prev.get(key)
            if (
                isinstance(value, dict)
                and isinstance(prev_val, dict)
                and value.get("sweep_count") == prev_val.get("sweep_count")
                and value.get("bins_version") == prev_val.get("bins_version")
                and value.get("status") == prev_val.get("status")
            ):
                continue
            result[key] = value
        elif value != prev.get(key):
            result[key] = value
    return result


# ── Per-WS preset switch rate limiting ──────────────────────────────
_ws_preset_rate: dict[int, collections.deque] = {}
_WS_PRESET_MAX = 3
_WS_PRESET_WINDOW = 30.0

# ── Off-grid toggle rate limiting ─────────────────────────────────
_offgrid_last_toggle: float = 0.0
_OFFGRID_RATE_LIMIT: float = 2.0


def _ws_preset_rate_ok(ws_id: int) -> bool:
    now = time.monotonic()
    times = _ws_preset_rate.setdefault(ws_id, collections.deque())
    cutoff = now - _WS_PRESET_WINDOW
    while times and times[0] < cutoff:
        times.popleft()
    if len(times) >= _WS_PRESET_MAX:
        return False
    times.append(now)
    return True


_WS_SEND_TIMEOUT = 5.0


async def _send_with_timeout(
    ws: aiohttp.web.WebSocketResponse,
    message: str,
    timeout: float = _WS_SEND_TIMEOUT,
) -> bool:
    try:
        await asyncio.wait_for(ws.send_str(message), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        log.warning("WS send timed out after %.1fs, evicting client", timeout)
        return False
    except Exception:
        return False


# ── RNode config cache ──────────────────────────────────────────────
_rnode_config_cache: dict[str, dict] | None = None
_rnode_config_mtime: float = 0

# All _ws_* globals are only mutated from the asyncio event loop that
# owns the aiohttp server.  Cross-thread callers (event-bus callbacks)
# schedule work via loop.call_soon_threadsafe(), never touching these
# directly.  No additional locking is needed.

_RETICULUM_CONFIG_PATHS = [
    os.path.expanduser("~reticulumpi/.reticulum/config"),
    os.path.expanduser("~/.reticulum/config"),
]

_RNODE_RADIO_KEYS = {
    "frequency",
    "bandwidth",
    "txpower",
    "spreadingfactor",
    "codingrate",
}


def _parse_rnode_config() -> dict[str, dict]:
    """Parse Reticulum config to extract RNode radio settings.

    Returns a dict mapping section name → {frequency, bandwidth, ...}.
    Results are cached and re-read only when the file changes.
    """
    global _rnode_config_cache, _rnode_config_mtime

    config_path = None
    for p in _RETICULUM_CONFIG_PATHS:
        if os.path.isfile(p):
            config_path = p
            break
    if not config_path:
        return _rnode_config_cache or {}

    try:
        mtime = os.path.getmtime(config_path)
        if _rnode_config_cache is not None and mtime == _rnode_config_mtime:
            return _rnode_config_cache

        result: dict[str, dict] = {}
        current_section: str | None = None
        current_data: dict[str, str] = {}

        with open(config_path, "r") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("[[") and stripped.endswith("]]"):
                    # Save previous section if it was an RNode
                    if current_section and current_data.get("type") == "RNodeInterface":
                        result[current_section] = _extract_radio(current_data)
                    current_section = stripped[2:-2].strip()
                    current_data = {}
                elif stripped.startswith("[") and stripped.endswith("]"):
                    if current_section and current_data.get("type") == "RNodeInterface":
                        result[current_section] = _extract_radio(current_data)
                    current_section = None
                    current_data = {}
                elif "=" in stripped and current_section:
                    key, _, value = stripped.partition("=")
                    current_data[key.strip()] = value.strip()

        # Handle last section
        if current_section and current_data.get("type") == "RNodeInterface":
            result[current_section] = _extract_radio(current_data)

        _rnode_config_cache = result
        _rnode_config_mtime = mtime
        return result
    except Exception:
        return _rnode_config_cache or {}


def _extract_radio(data: dict[str, str]) -> dict[str, Any]:
    """Extract and type-cast radio config fields."""
    radio: dict[str, Any] = {}
    for key in _RNODE_RADIO_KEYS:
        val = data.get(key)
        if val is not None:
            try:
                radio[key] = int(val)
            except ValueError:
                try:
                    radio[key] = float(val)
                except ValueError:
                    radio[key] = val
    return radio


def _collect_interfaces(reticulum_instance: Any = None) -> list[dict]:
    """Collect current RNS interface data for broadcast.

    In shared-instance mode, ``RNS.Transport.interfaces`` only contains the
    local client connection to rnsd.  ``Reticulum.get_interface_stats()``
    returns the full list of interfaces from the shared instance, including
    TCP, I2P, LoRa, etc.

    For RNodeInterface entries, radio config from the Reticulum config file
    is merged in (frequency, bandwidth, SF, CR, txpower) along with runtime
    metrics (airtime, channel load, noise floor, etc.) from the stats API.
    """
    try:
        import RNS

        # Load RNode radio config from the Reticulum config file (cached)
        rnode_configs = _parse_rnode_config()

        # Prefer the Reticulum stats API (works across shared instance boundary)
        if reticulum_instance and hasattr(reticulum_instance, "get_interface_stats"):
            stats = reticulum_instance.get_interface_stats()
            interfaces = []
            for entry in stats.get("interfaces", []):
                itype = entry.get("type", "")
                # Skip internal local-client/local-server interfaces
                if itype in ("LocalClientInterface", "LocalServerInterface"):
                    continue
                info: dict[str, Any] = {
                    "name": entry.get("name", "?"),
                    "type": itype,
                    "online": entry.get("status", False),
                    "bitrate": entry.get("bitrate"),
                    "rxb": entry.get("rxb", 0),
                    "txb": entry.get("txb", 0),
                }

                # For RNode interfaces, include radio metrics and config
                if itype == "RNodeInterface":
                    # Runtime metrics from rnsd stats
                    for key in (
                        "airtime_short",
                        "airtime_long",
                        "channel_load_short",
                        "channel_load_long",
                        "noise_floor",
                        "interference",
                        "battery_state",
                        "battery_percent",
                        "announce_queue",
                        "held_announces",
                    ):
                        if key in entry:
                            info[key] = entry[key]

                    # Radio config from parsed Reticulum config file
                    # Match by checking if the config section name appears
                    # in the interface name string
                    iface_name = entry.get("name", "")
                    for section_name, radio_cfg in rnode_configs.items():
                        if section_name in iface_name:
                            info["radio"] = radio_cfg
                            break

                interfaces.append(info)
            return interfaces

        # Fallback: direct interface iteration (standalone mode)
        interfaces = []
        for iface in RNS.Transport.interfaces:
            info = {
                "name": str(iface),
                "type": iface.__class__.__name__,
                "online": getattr(iface, "online", None),
            }
            if hasattr(iface, "bitrate"):
                info["bitrate"] = iface.bitrate
            if hasattr(iface, "rxb"):
                info["rxb"] = iface.rxb
            if hasattr(iface, "txb"):
                info["txb"] = iface.txb

            # For RNode: pull radio stats directly from the interface object
            if iface.__class__.__name__ == "RNodeInterface":
                for attr, key in [
                    ("r_airtime_short", "airtime_short"),
                    ("r_airtime_long", "airtime_long"),
                    ("r_channel_load_short", "channel_load_short"),
                    ("r_channel_load_long", "channel_load_long"),
                    ("r_noise_floor", "noise_floor"),
                    ("r_interference", "interference"),
                    ("r_frequency", "frequency"),
                    ("r_bandwidth", "bandwidth"),
                    ("r_txpower", "txpower"),
                    ("r_sf", "spreadingfactor"),
                    ("r_cr", "codingrate"),
                ]:
                    val = getattr(iface, attr, None)
                    if val is not None:
                        info[key] = val
                # Standalone mode: embed radio config directly
                radio = {}
                for k in ("frequency", "bandwidth", "txpower", "spreadingfactor", "codingrate"):
                    if k in info:
                        radio[k] = info[k]
                if radio:
                    info["radio"] = radio

            interfaces.append(info)
        return interfaces
    except Exception:
        return []


def _enrich_transport_traffic(transport_data: dict, interfaces: list[dict]) -> None:
    """Add rxb/txb traffic stats to transport hub entries from interface data."""
    traffic_map: dict[str, dict] = {}
    for iface in interfaces:
        if "TCPClient" not in iface.get("type", ""):
            continue
        name = iface.get("name", "")
        traffic = {"rxb": iface.get("rxb", 0), "txb": iface.get("txb", 0)}
        # Names: "TCPInterface[TCP Client label/host:port]" -> "host:port"
        if "/" in name:
            addr = name.split("/", 1)[1].rstrip("]")
            traffic_map[addr] = traffic

    def _enrich(hub: dict) -> None:
        host = hub.get("target_host", "")
        port = hub.get("target_port", 0)
        t = traffic_map.get(f"{host}:{port}")
        if t:
            hub["rxb"] = t["rxb"]
            hub["txb"] = t["txb"]

    for h in transport_data.get("primaries", []):
        _enrich(h)
    for h in transport_data.get("active_fallbacks", []):
        _enrich(h)
    for h in transport_data.get("auto_discovery", {}).get("connected", []):
        _enrich(h)


def setup_websocket_routes(app: aiohttp.web.Application) -> None:
    """Register WebSocket routes."""
    app.router.add_get("/ws/metrics", websocket_metrics)
    app.router.add_get("/ws/spectrum", websocket_spectrum)
    app.on_startup.append(_start_broadcast_task)
    app.on_shutdown.append(_stop_broadcast_task)


_ws_clients: set[aiohttp.web.WebSocketResponse] = set()
_ws_last_activity: dict[aiohttp.web.WebSocketResponse, float] = {}
_ws_last_snapshot: dict[int, float] = {}
_last_global_snapshot: float = 0.0
_broadcast_task: asyncio.Task | None = None


# Loop + plugin refs captured at startup so cross-thread event-bus
# callbacks can push straight into the WS broadcast path without polling.
_ws_loop: asyncio.AbstractEventLoop | None = None
_ws_plugin: Any | None = None

# Dedicated single-worker executor for broadcast data collection.
# Prevents overlapping collections from saturating the default pool.
_broadcast_executor: concurrent.futures.ThreadPoolExecutor | None = None
_command_executor: concurrent.futures.ThreadPoolExecutor | None = None
_collection_running = threading.Event()

# ── Warm cache heartbeat ──────────────────────────────────────────
_warm_cache_data: dict[str, Any] = {}
_warm_cache_ts: float = 0.0
_last_heartbeat_ts: float = 0.0
_hb_count: int = 0
_hb_fail: int = 0
_cache_hits: int = 0
_last_hb_summary_ts: float = 0.0
_HB_SUMMARY_INTERVAL = 3600.0

_HEARTBEAT_MIN_INTERVAL = 30.0
_HEARTBEAT_MAX_INTERVAL = 120.0
_HEARTBEAT_LOAD_LOW = 1.2
_HEARTBEAT_LOAD_HIGH = 3.2
_WARM_CACHE_MAX_AGE = 90.0


def _heartbeat_interval() -> float | None:
    """Adaptive heartbeat interval based on 1-minute load average.

    Returns seconds between heartbeats, or None to skip entirely.
    """
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        return _HEARTBEAT_MIN_INTERVAL
    if load1 < _HEARTBEAT_LOAD_LOW:
        return _HEARTBEAT_MIN_INTERVAL
    if load1 >= _HEARTBEAT_LOAD_HIGH:
        return None
    ratio = (load1 - _HEARTBEAT_LOAD_LOW) / (_HEARTBEAT_LOAD_HIGH - _HEARTBEAT_LOAD_LOW)
    return _HEARTBEAT_MIN_INTERVAL + ratio * (_HEARTBEAT_MAX_INTERVAL - _HEARTBEAT_MIN_INTERVAL)


# ── Spectrum-dedicated WS ───────────────────────────────────────────

_spectrum_clients: set[aiohttp.web.WebSocketResponse] = set()
_spectrum_task: asyncio.Task | None = None


async def websocket_spectrum(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    """Dedicated WebSocket for spectrum/waterfall panels."""
    if not _check_ws_origin(request):
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        await ws.close(code=4003, message=b"Origin not allowed")
        return ws

    plugin = request.app["plugin"]
    max_clients = plugin.config.get("max_websocket_clients", 10)

    _auth_hdr = request.headers.get("Authorization", "")
    token = _auth_hdr[7:] if _auth_hdr.startswith("Bearer ") else request.cookies.get("session")
    if not token or not plugin._auth.validate_token(token):
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        await ws.close(code=4001, message=b"Authentication required")
        return ws

    if len(_spectrum_clients) >= max_clients:
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        await ws.close(code=4002, message=b"Too many connections")
        return ws

    compress = 15 if request.app.get("ws_compress", True) else 0
    ws = aiohttp.web.WebSocketResponse(heartbeat=60.0, compress=compress)
    await ws.prepare(request)

    for name, msg_type in [
        ("spectrum_scanner", "spectrum_history"),
        ("lora_scanner", "lora_scanner_history"),
        ("lora_link_tester", "link_tester_history"),
    ]:
        p = plugin.app.get_plugin(name)
        if p and hasattr(p, "get_history"):
            try:
                hist = p.get_history()
            except Exception:
                hist = {"available": False}
            if not await _send_with_timeout(ws, json.dumps({"type": msg_type, "data": hist})):
                log.debug("Failed to send %s hello", msg_type)

    snap_data: dict[str, Any] = {}
    for name, key in [
        ("spectrum_scanner", "spectrum"),
        ("lora_scanner", "lora_scanner"),
        ("lora_link_tester", "link_tester"),
    ]:
        p = plugin.app.get_plugin(name)
        if p and hasattr(p, "get_snapshot"):
            try:
                snap = p.get_snapshot()
                if snap is not None:
                    snap_data[key] = snap
            except Exception:
                pass
    if snap_data:
        if not await _send_with_timeout(ws, json.dumps({"type": "update", "data": snap_data})):
            log.debug("Failed to send initial snapshot")

    _spectrum_clients.add(ws)
    log.debug("Spectrum WS client connected (%d total)", len(_spectrum_clients))

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.ERROR:
                break
            if msg.type == aiohttp.WSMsgType.TEXT:
                loop = asyncio.get_running_loop()
                resp = await loop.run_in_executor(
                    _command_executor,
                    _handle_ws_command,
                    msg.data,
                    plugin,
                    ws,
                )
                if resp is not None:
                    if not await _send_with_timeout(ws, json.dumps(resp)):
                        break
    finally:
        _spectrum_clients.discard(ws)
        _ws_preset_rate.pop(id(ws), None)
        log.info(
            "Spectrum WS disconnected (code=%s, %d remaining)",
            ws.close_code,
            len(_spectrum_clients),
        )
    return ws


def _collect_spectrum_data(plugin: Any) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for name, key in [
        ("spectrum_scanner", "spectrum"),
        ("lora_scanner", "lora_scanner"),
        ("lora_link_tester", "link_tester"),
    ]:
        p = plugin.app.get_plugin(name)
        if p and hasattr(p, "get_snapshot"):
            try:
                snap = p.get_snapshot()
                if snap is not None:
                    data[key] = snap
            except Exception:
                pass
        elif p and hasattr(p, "broadcast_snapshot"):
            try:
                snap = p.broadcast_snapshot(cycle_count=0)
                if snap is not None:
                    data[key] = snap
            except Exception:
                pass
    return data


async def _spectrum_broadcast_loop(app: aiohttp.web.Application) -> None:
    plugin = app["plugin"]
    interval = plugin.config.get("spectrum_broadcast_interval", 2)
    resync_every = int(plugin.config.get("spectrum_resync_cycles", 12))
    prev_data: dict[str, Any] = {}
    cycle_count = 0
    loop = asyncio.get_running_loop()

    while True:
        try:
            await asyncio.sleep(interval)
            if not _spectrum_clients:
                continue

            data = await loop.run_in_executor(
                _broadcast_executor,
                _collect_spectrum_data,
                plugin,
            )
            if not data:
                continue

            cycle_count += 1
            if resync_every > 0 and cycle_count % resync_every == 0:
                payload = data
            else:
                payload = _diff_payload(data, prev_data)
            prev_data = data
            if not payload:
                continue

            message = json.dumps(
                {
                    "type": "update",
                    "data": payload,
                    "timestamp": time.time(),
                }
            )

            clients = list(_spectrum_clients)

            results = await asyncio.gather(*(_send_with_timeout(ws, message) for ws in clients))
            for ws, ok in zip(clients, results):
                if not ok:
                    _spectrum_clients.discard(ws)

        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("Error in spectrum broadcast")
            await asyncio.sleep(1)


async def _handle_snapshot_request(ws: aiohttp.web.WebSocketResponse, plugin: Any) -> None:
    """Send a full data snapshot in response to a client request."""
    global _last_global_snapshot, _cache_hits
    now = time.time()
    if now - _last_global_snapshot < 2.0:
        return
    if now - _ws_last_snapshot.get(id(ws), 0) < 10:
        return
    _ws_last_snapshot[id(ws)] = now
    _last_global_snapshot = now
    loop = asyncio.get_running_loop()
    try:
        if _warm_cache_data and (time.monotonic() - _warm_cache_ts) < _WARM_CACHE_MAX_AGE:
            data = _warm_cache_data.copy()
            _cache_hits += 1
        else:
            if _collection_running.is_set():
                await asyncio.sleep(0.15)
            data, _, _ = await loop.run_in_executor(
                _broadcast_executor,
                functools.partial(
                    _collect_broadcast_data,
                    plugin,
                    0,
                    _last_mesh_announce_ts,
                    _mesh_version,
                    initial=True,
                ),
            )
        data["ws_stats"] = {
            "clients": len(_ws_clients),
            "max_clients": plugin.config.get("max_websocket_clients", 10),
            "collect_ms": 0,
            "prev_payload_bytes": 0,
        }
        if not await _send_with_timeout(
            ws,
            json.dumps(
                {
                    "type": "update",
                    "data": data,
                    "timestamp": time.time(),
                }
            ),
        ):
            log.debug("Failed to send snapshot on request")
    except Exception:
        log.debug("Failed to collect snapshot data", exc_info=True)


async def websocket_metrics(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    """Handle WebSocket connections for live metrics streaming."""
    if not _check_ws_origin(request):
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        await ws.close(code=4003, message=b"Origin not allowed")
        return ws

    global _cache_hits
    plugin = request.app["plugin"]
    max_clients = plugin.config.get("max_websocket_clients", 10)

    # Authenticate via cookie or Authorization header
    _auth_hdr = request.headers.get("Authorization", "")
    token = _auth_hdr[7:] if _auth_hdr.startswith("Bearer ") else request.cookies.get("session")

    if not token or not plugin._auth.validate_token(token):
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        await ws.close(code=4001, message=b"Authentication required")
        return ws

    if len(_ws_clients) >= max_clients:
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        await ws.close(code=4002, message=b"Too many connections")
        return ws

    compress = 15 if request.app.get("ws_compress", True) else 0
    ws = aiohttp.web.WebSocketResponse(heartbeat=60.0, compress=compress)
    await ws.prepare(request)

    # Send a full data snapshot so every panel populates immediately
    # instead of waiting up to 5s for the next broadcast cycle.
    loop = asyncio.get_running_loop()
    try:
        if _warm_cache_data and (time.monotonic() - _warm_cache_ts) < _WARM_CACHE_MAX_AGE:
            data = _warm_cache_data.copy()
            _cache_hits += 1
            log.debug(
                "Serving warm cache to new client (age=%.1fs)", time.monotonic() - _warm_cache_ts
            )
        else:
            if _collection_running.is_set():
                await asyncio.sleep(0.15)
            data, _, _ = await loop.run_in_executor(
                _broadcast_executor,
                functools.partial(
                    _collect_broadcast_data,
                    plugin,
                    0,
                    _last_mesh_announce_ts,
                    _mesh_version,
                    initial=True,
                ),
            )
        data["ws_stats"] = {
            "clients": len(_ws_clients) + 1,
            "max_clients": plugin.config.get("max_websocket_clients", 10),
            "collect_ms": 0,
            "prev_payload_bytes": 0,
        }
        if not await _send_with_timeout(
            ws,
            json.dumps(
                {
                    "type": "update",
                    "data": data,
                    "timestamp": time.time(),
                }
            ),
        ):
            log.debug("Failed to send initial data snapshot")
    except Exception:
        log.debug("Failed to collect initial data snapshot", exc_info=True)

    _ws_clients.add(ws)
    _ws_last_activity[ws] = time.time()
    log.debug("WebSocket client connected (%d total)", len(_ws_clients))

    try:
        async for msg in ws:
            _ws_last_activity[ws] = time.time()
            if msg.type == aiohttp.WSMsgType.ERROR:
                log.debug("WebSocket error: %s", ws.exception())
                break
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    parsed = json.loads(msg.data)
                except (json.JSONDecodeError, TypeError):
                    parsed = None
                if isinstance(parsed, dict) and parsed.get("action") == "request_snapshot":
                    await _handle_snapshot_request(ws, plugin)
                    continue
                loop = asyncio.get_running_loop()
                resp = await loop.run_in_executor(
                    _command_executor,
                    _handle_ws_command,
                    parsed or msg.data,
                    plugin,
                    ws,
                )
                if resp is not None:
                    if not await _send_with_timeout(ws, json.dumps(resp)):
                        break
    finally:
        _ws_clients.discard(ws)
        _ws_last_activity.pop(ws, None)
        _ws_last_snapshot.pop(id(ws), None)
        _ws_preset_rate.pop(id(ws), None)
        log.info(
            "WebSocket disconnected (code=%s, %d remaining)",
            ws.close_code,
            len(_ws_clients),
        )

    return ws


_last_mesh_announce_ts: float = 0
_mesh_version: int = 0
_broadcast_registry: BroadcastRegistry | None = None


def _get_registry(interval: float) -> BroadcastRegistry:
    global _broadcast_registry
    if _broadcast_registry is None:
        _broadcast_registry = BroadcastRegistry(metrics_interval=interval)
    return _broadcast_registry


def _collect_broadcast_data(
    plugin: Any,
    cycle_count: int,
    last_mesh_announce_ts: float,
    mesh_version: int,
    initial: bool = False,
) -> tuple[dict[str, Any], float, int]:
    """Collect all plugin data synchronously.  Runs in a thread executor.

    Uses :class:`BroadcastRegistry` to iterate plugins by tier with a
    time budget, then post-processes to preserve the frontend contract.
    """
    interval = plugin.config.get("metrics_interval", 5)
    registry = _get_registry(interval)

    plugin_statuses: dict[str, Any] = {}
    for name, p in plugin.app.plugins.items():
        try:
            plugin_statuses[name] = {"active": p.get_status().get("active", False)}
        except Exception:
            plugin_statuses[name] = {"active": False}

    interfaces = _collect_interfaces(plugin.app.reticulum)

    data = registry.collect(
        plugin.app.plugins,
        cycle_count,
        budget_multiplier=3.0 if initial else 1.0,
    )

    data["plugins"] = plugin_statuses
    data["interfaces"] = interfaces
    probe = getattr(plugin.app, "internet_probe", None)
    data["internet"] = (
        probe.get_status() if probe else {"online": True, "wan_ip": None, "lan_ip": None}
    )

    if "transport" in data:
        try:
            _enrich_transport_traffic(data["transport"], interfaces)
        except Exception:
            pass

    mesh = data.get("mesh") or {}
    mesh_peers = data.pop("mesh_peers", None)
    if mesh_peers is not None:
        mesh["peers"] = mesh_peers
        mesh["peer_count"] = len(mesh_peers)
    alerts = data.pop("alerts", None)
    if alerts:
        mesh["alerts_sent"] = alerts.get("alerts_sent", 0)
        mesh["last_alert"] = alerts.get("last_alert")

    recent = mesh.get("recent_announces")
    if recent:
        newest_ts = max(n.get("last_seen", 0) for n in recent)
        if newest_ts > last_mesh_announce_ts:
            mesh_version += 1
            last_mesh_announce_ts = newest_ts
    mesh["version"] = mesh_version
    if mesh:
        data["mesh"] = mesh

    space = data.get("space")
    if space:
        if "launches" in space:
            space["launches"] = (space["launches"] or [])[:5]
        if "passes" in space:
            space["passes"] = (space["passes"] or [])[:10]
        positions = space.get("positions") or {}
        objects = positions.get("objects") or []
        if objects:
            if space.get("observer"):
                objects = sorted(
                    objects,
                    key=lambda o: o.get("el", -999),
                    reverse=True,
                )
            space["positions"] = {
                "fetched_at": positions.get("fetched_at"),
                "count": positions.get("count", len(objects)),
                "objects": objects[:60],
            }

    return data, last_mesh_announce_ts, mesh_version


async def _broadcast_metrics(app: aiohttp.web.Application) -> None:
    """Periodically broadcast system metrics to all connected WebSocket clients."""
    global \
        _last_mesh_announce_ts, \
        _mesh_version, \
        _warm_cache_data, \
        _warm_cache_ts, \
        _last_heartbeat_ts
    global _hb_count, _hb_fail, _cache_hits, _last_hb_summary_ts

    plugin = app["plugin"]
    interval = plugin.config.get("metrics_interval", 5)
    _cycle_count = 0
    _prev_data: dict[str, Any] = {}
    _prev_payload_len = 0
    loop = asyncio.get_running_loop()

    while True:
        try:
            await asyncio.sleep(interval)

            _cycle_count += 1

            if not _ws_clients:
                hb_interval = _heartbeat_interval()
                if hb_interval is not None:
                    now_mono = time.monotonic()
                    if (
                        now_mono - _last_heartbeat_ts >= hb_interval
                        and not _collection_running.is_set()
                    ):
                        _collection_running.set()
                        try:
                            (
                                data,
                                _last_mesh_announce_ts,
                                _mesh_version,
                            ) = await loop.run_in_executor(
                                _broadcast_executor,
                                _collect_broadcast_data,
                                plugin,
                                _cycle_count,
                                _last_mesh_announce_ts,
                                _mesh_version,
                            )
                            _warm_cache_data = data
                            _warm_cache_ts = time.monotonic()
                            _last_heartbeat_ts = _warm_cache_ts
                            _hb_count += 1
                            log.debug(
                                "Warm cache heartbeat: collected %d keys (interval=%.0fs)",
                                len(data),
                                hb_interval,
                            )
                        except Exception:
                            _hb_fail += 1
                            log.debug("Warm cache heartbeat failed", exc_info=True)
                        finally:
                            _collection_running.clear()
                    # Periodic INFO summary (~hourly)
                    if (now_mono - _last_hb_summary_ts) >= _HB_SUMMARY_INTERVAL:
                        log.info(
                            "Warm cache: %d heartbeats, %d failures,"
                            " %d cache hits (interval=%.0fs)",
                            _hb_count,
                            _hb_fail,
                            _cache_hits,
                            hb_interval,
                        )
                        _last_hb_summary_ts = now_mono
                continue

            # Skip this cycle if the previous collection hasn't finished.
            if _collection_running.is_set():
                log.debug("Skipping broadcast cycle — previous collection still running")
                continue

            _collection_running.set()
            t_collect = time.monotonic()
            try:
                data, _last_mesh_announce_ts, _mesh_version = await loop.run_in_executor(
                    _broadcast_executor,
                    _collect_broadcast_data,
                    plugin,
                    _cycle_count,
                    _last_mesh_announce_ts,
                    _mesh_version,
                )
            finally:
                _collection_running.clear()

            _warm_cache_data = data
            _warm_cache_ts = time.monotonic()
            _last_heartbeat_ts = _warm_cache_ts

            collect_elapsed = time.monotonic() - t_collect

            data["ws_stats"] = {
                "clients": len(_ws_clients),
                "max_clients": plugin.config.get("max_websocket_clients", 10),
                "collect_ms": round(collect_elapsed * 1000, 1),
                "prev_payload_bytes": _prev_payload_len,
            }

            diff_data = _diff_payload(data, _prev_data)
            _prev_data = data
            if not diff_data:
                continue

            _encode_payload = {
                "type": "update",
                "data": diff_data,
                "timestamp": time.time(),
            }
            message = await loop.run_in_executor(
                _broadcast_executor,
                json.dumps,
                _encode_payload,
            )
            _prev_payload_len = len(message)

            # Fan out to all clients concurrently (mirrors _push_to_clients).
            clients = list(_ws_clients)

            results = await asyncio.gather(*(_send_with_timeout(ws, message) for ws in clients))
            now = time.time()
            for ws, ok in zip(clients, results):
                if not ok:
                    _ws_clients.discard(ws)
                    _ws_last_activity.pop(ws, None)
                else:
                    _ws_last_activity[ws] = now

            # Reap stale connections every ~60s (12 cycles at 5s default)
            if _cycle_count % 12 == 0:
                stale_timeout = plugin.config.get("ws_stale_timeout", 180)
                stale = [ws for ws, last in _ws_last_activity.items() if now - last > stale_timeout]
                for ws in stale:
                    _ws_clients.discard(ws)
                    _ws_last_activity.pop(ws, None)
                    try:
                        await ws.close(
                            code=aiohttp.WSCloseCode.GOING_AWAY,
                            message=b"Connection stale",
                        )
                    except Exception:
                        pass
                if stale:
                    log.info("Reaped %d stale WebSocket client(s)", len(stale))

            # Periodic health log (~every 60s at default interval).
            if _cycle_count % 30 == 0:
                log.info(
                    "WS broadcast: collect=%.0fms payload=%dB diff=%d/%d clients=%d",
                    collect_elapsed * 1000,
                    len(message),
                    len(diff_data),
                    len(data),
                    len(clients),
                )

        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("Error in metrics broadcast")
            await asyncio.sleep(1)


_push_sem: asyncio.Semaphore | None = None


async def _start_broadcast_task(app: aiohttp.web.Application) -> None:
    global \
        _broadcast_task, \
        _spectrum_task, \
        _ws_loop, \
        _ws_plugin, \
        _broadcast_executor, \
        _command_executor, \
        _push_sem, \
        _warm_cache_data, \
        _warm_cache_ts, \
        _last_heartbeat_ts, \
        _hb_count, \
        _hb_fail, \
        _cache_hits, \
        _last_hb_summary_ts
    _ws_loop = asyncio.get_running_loop()
    _ws_plugin = app["plugin"]
    _push_sem = asyncio.Semaphore(8)
    _broadcast_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="ws-broadcast",
    )
    _command_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="ws-command",
    )

    # Warm plugin caches and seed the warm cache for instant first-client response
    try:
        data, _, _ = await asyncio.get_running_loop().run_in_executor(
            _broadcast_executor,
            functools.partial(_collect_broadcast_data, app["plugin"], 0, 0, 0),
        )
        _warm_cache_data = data
        _warm_cache_ts = time.monotonic()
        _last_heartbeat_ts = _warm_cache_ts
        log.info("Broadcast cache warmed on startup (%d keys)", len(data))
    except Exception:
        log.debug("Cache warm-up failed (non-fatal)", exc_info=True)

    _broadcast_task = asyncio.create_task(_broadcast_metrics(app))
    _spectrum_task = asyncio.create_task(_spectrum_broadcast_loop(app))
    try:
        from reticulumpi import events as _events

        event_bus = _ws_plugin.app.event_bus
        event_bus.subscribe(_events.MESSAGE_RECEIVED, _on_message_event)
        event_bus.subscribe(_events.MESSAGE_SENT, _on_message_event)
        event_bus.subscribe(_events.MESSAGE_STATUS_CHANGED, _on_status_event)
        event_bus.subscribe(_events.MESSAGE_REACTION_RECEIVED, _on_reaction_event)
        event_bus.subscribe(_events.ALERT_TRIGGERED, _on_alert_event)
        event_bus.subscribe(_events.INTERNET_ONLINE, _on_internet_event)
        event_bus.subscribe(_events.INTERNET_OFFLINE, _on_internet_event)
        event_bus.subscribe(_events.OFFGRID_MODE_CHANGED, _on_offgrid_event)
        event_bus.subscribe(_events.MESHTASTIC_FIRMWARE_HANG, _on_firmware_event)
        event_bus.subscribe(
            _events.MESHTASTIC_FIRMWARE_RECOVERED, _on_firmware_event
        )
    except Exception:
        log.exception("Failed to subscribe WS handler to events")


async def _stop_broadcast_task(app: aiohttp.web.Application) -> None:
    global \
        _broadcast_task, \
        _spectrum_task, \
        _ws_loop, \
        _ws_plugin, \
        _broadcast_executor, \
        _command_executor, \
        _broadcast_registry, \
        _warm_cache_data, \
        _warm_cache_ts, \
        _last_heartbeat_ts, \
        _hb_count, \
        _hb_fail, \
        _cache_hits, \
        _last_hb_summary_ts
    try:
        if _ws_plugin is not None:
            event_bus = _ws_plugin.app.event_bus
            event_bus.unsubscribe_all(_on_message_event)
            event_bus.unsubscribe_all(_on_status_event)
            event_bus.unsubscribe_all(_on_reaction_event)
            event_bus.unsubscribe_all(_on_alert_event)
            event_bus.unsubscribe_all(_on_internet_event)
            event_bus.unsubscribe_all(_on_offgrid_event)
            event_bus.unsubscribe_all(_on_firmware_event)
    except Exception:
        log.debug("Error unsubscribing WS handler", exc_info=True)
    if _spectrum_task:
        _spectrum_task.cancel()
        try:
            await _spectrum_task
        except asyncio.CancelledError:
            pass
        _spectrum_task = None
    if _broadcast_task:
        _broadcast_task.cancel()
        try:
            await _broadcast_task
        except asyncio.CancelledError:
            pass
        _broadcast_task = None
    _ws_loop = None
    _ws_plugin = None
    _broadcast_registry = None
    _warm_cache_data = {}
    _warm_cache_ts = 0.0
    _last_heartbeat_ts = 0.0
    _hb_count = 0
    _hb_fail = 0
    _cache_hits = 0
    _last_hb_summary_ts = 0.0
    if _broadcast_executor is not None:
        _broadcast_executor.shutdown(wait=True, cancel_futures=True)
        _broadcast_executor = None
    if _command_executor is not None:
        _command_executor.shutdown(wait=True, cancel_futures=True)
        _command_executor = None
    # Close all WebSocket connections
    for ws in list(_ws_clients):
        await ws.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b"Server shutting down")
    _ws_clients.clear()
    _ws_last_activity.clear()
    for ws in list(_spectrum_clients):
        await ws.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b"Server shutting down")
    _spectrum_clients.clear()


def _lookup_message_row(msg_id: Any) -> dict | None:
    """Fetch a full message row by id from the messaging hub.

    Used to enrich event-bus payloads (which only carry {id, transport,
    ...}) into the shape the dashboard expects so clients can append
    incrementally without an additional HTTP round-trip.
    """
    if _ws_plugin is None or msg_id is None:
        return None
    try:
        msg_hub = _ws_plugin.app.get_plugin("messaging_hub")
    except Exception:
        return None
    if not msg_hub or not hasattr(msg_hub, "get_message"):
        return None
    try:
        return msg_hub.get_message(msg_id)
    except Exception:
        return None


def _on_message_event(event_type: str, data: dict) -> None:
    """Event-bus callback (runs in any thread). Push to WS asynchronously."""
    loop = _ws_loop
    if loop is None or not _ws_clients:
        return
    try:
        loop.call_soon_threadsafe(
            _schedule_enriched_push,
            "message",
            event_type,
            data,
        )
    except (RuntimeError, AttributeError):
        pass


def _on_status_event(event_type: str, data: dict) -> None:
    """Event-bus callback for MESSAGE_STATUS_CHANGED — same pattern."""
    loop = _ws_loop
    if loop is None or not _ws_clients:
        return
    try:
        loop.call_soon_threadsafe(
            _schedule_enriched_push,
            "message_status",
            event_type,
            data,
        )
    except (RuntimeError, AttributeError):
        pass


def _on_reaction_event(event_type: str, data: dict) -> None:
    """Event-bus callback for MESSAGE_REACTION_RECEIVED — push to WS clients."""
    loop = _ws_loop
    if loop is None or not _ws_clients:
        return
    try:
        loop.call_soon_threadsafe(_schedule_push, "reaction", data)
    except (RuntimeError, AttributeError):
        pass


def _handle_ws_command(raw: dict | str, plugin: Any, ws: Any = None) -> dict | None:
    """Process a JSON command from a WebSocket client.

    Returns an optional response dict to send back to the caller.
    """
    if isinstance(raw, dict):
        cmd = raw
    else:
        try:
            cmd = json.loads(raw)
        except Exception:
            return
    action = cmd.get("action")
    if not action:
        return None
    if action == "ping":
        return {"type": "pong", "ts": cmd.get("ts", 0)}
    if action == "spectrum_switch_preset":
        if ws is not None and not _ws_preset_rate_ok(id(ws)):
            return {"type": "spectrum_preset_error", "error": "Rate limited — try again shortly"}
        scanner = plugin.app.plugins.get("spectrum_scanner")
        if scanner and hasattr(scanner, "switch_preset"):
            preset_name = cmd.get("preset", "")
            try:
                result = scanner.switch_preset(preset_name)
                return {"type": "spectrum_preset_switched", **result}
            except ValueError as exc:
                return {"type": "spectrum_preset_error", "error": str(exc)}
            except Exception as exc:
                log.warning("Spectrum preset switch failed", exc_info=True)
                return {
                    "type": "spectrum_preset_error",
                    "error": str(exc) or "Internal error during preset switch",
                }
    elif action == "spectrum_list_presets":
        scanner = plugin.app.plugins.get("spectrum_scanner")
        if scanner and hasattr(scanner, "get_presets"):
            return {"type": "spectrum_presets", **scanner.get_presets()}
    elif action.startswith("radio_"):
        fm = plugin.app.plugins.get("fm_receiver")
        if not fm:
            return None

        if action == "radio_tune" and hasattr(fm, "tune"):
            freq_mhz = cmd.get("frequency_mhz")
            if freq_mhz is None:
                return {"type": "radio_error", "error": "frequency_mhz required"}
            try:
                freq_hz = int(float(freq_mhz) * 1_000_000)
            except (ValueError, OverflowError, TypeError):
                return {"type": "radio_error", "error": "invalid frequency_mhz"}
            mode = cmd.get("mode")
            if mode is not None and not isinstance(mode, str):
                return {"type": "radio_error", "error": "mode must be a string"}
            try:
                result = fm.tune(freq_hz, mode=mode)
                return {"type": "radio_tuned", **result}
            except (ValueError, TypeError) as exc:
                return {"type": "radio_error", "error": str(exc)}
        elif action == "radio_play" and hasattr(fm, "play"):
            try:
                result = fm.play()
                return {"type": "radio_play", **result}
            except Exception as exc:
                return {"type": "radio_error", "error": str(exc)}
        elif action == "radio_stop" and hasattr(fm, "stop_playback"):
            try:
                result = fm.stop_playback()
                return {"type": "radio_stop", **result}
            except Exception as exc:
                return {"type": "radio_error", "error": str(exc)}
        elif action == "radio_gain" and hasattr(fm, "set_gain"):
            try:
                gain = cmd.get("gain_db")
                if gain is not None:
                    gain = float(gain)
                result = fm.set_gain(gain)
                return {"type": "radio_gain", **result}
            except (ValueError, TypeError) as exc:
                return {"type": "radio_error", "error": str(exc)}
        elif action == "radio_squelch" and hasattr(fm, "set_squelch"):
            try:
                result = fm.set_squelch(int(cmd.get("level", 0)))
                return {"type": "radio_squelch", **result}
            except (ValueError, TypeError) as exc:
                return {"type": "radio_error", "error": str(exc)}
        elif action == "radio_volume" and hasattr(fm, "set_volume"):
            try:
                vol = float(cmd.get("volume", 0.75))
                if not 0.0 <= vol <= 1.0:
                    return {"type": "radio_error", "error": "volume must be 0.0-1.0"}
                result = fm.set_volume(vol)
                return {"type": "radio_volume", **result}
            except (ValueError, TypeError) as exc:
                return {"type": "radio_error", "error": str(exc)}
        elif action == "radio_lock" and hasattr(fm, "lock_dongle"):
            try:
                result = fm.lock_dongle()
                return {"type": "radio_lock", **result}
            except Exception as exc:
                return {"type": "radio_error", "error": str(exc)}
        elif action == "radio_unlock" and hasattr(fm, "unlock_dongle"):
            try:
                result = fm.unlock_dongle()
                return {"type": "radio_unlock", **result}
            except Exception as exc:
                return {"type": "radio_error", "error": str(exc)}
        elif action == "radio_add_favorite" and hasattr(fm, "add_favorite"):
            try:
                fav = fm.add_favorite(
                    label=cmd.get("label", ""),
                    frequency_mhz=float(cmd.get("frequency_mhz", 0)),
                    mode=cmd.get("mode", "wbfm"),
                    gain_db=cmd.get("gain_db"),
                )
                return {"type": "radio_favorite_added", **fav}
            except (ValueError, TypeError) as exc:
                return {"type": "radio_error", "error": str(exc)}
        elif action == "radio_remove_favorite" and hasattr(fm, "remove_favorite"):
            fav_id = cmd.get("favorite_id", "")
            if fm.remove_favorite(fav_id):
                return {"type": "radio_favorite_removed", "id": fav_id}
            return {"type": "radio_error", "error": "Favorite not found"}
        elif action == "radio_tune_favorite" and hasattr(fm, "tune_favorite"):
            try:
                result = fm.tune_favorite(cmd.get("favorite_id", ""))
                return {"type": "radio_tuned", **result}
            except ValueError as exc:
                return {"type": "radio_error", "error": str(exc)}
        elif action == "radio_record_start" and hasattr(fm, "start_recording"):
            result = fm.start_recording(label=cmd.get("label"))
            if result.get("error"):
                return {"type": "radio_error", "error": result["error"]}
            return {"type": "radio_record_started", **result}
        elif action == "radio_record_stop" and hasattr(fm, "stop_recording"):
            result = fm.stop_recording()
            return {"type": "radio_record_stopped", **result}
    elif action == "set_offgrid_mode":
        global _offgrid_last_toggle
        now = time.monotonic()
        if now - _offgrid_last_toggle < _OFFGRID_RATE_LIMIT:
            return {"type": "offgrid_error", "error": "Rate limited, try again shortly"}
        enabled = cmd.get("enabled")
        if enabled is None:
            return {"type": "offgrid_error", "error": "'enabled' field required"}
        if not isinstance(enabled, bool):
            return {"type": "offgrid_error", "error": "'enabled' must be a boolean"}
        _offgrid_last_toggle = now
        result = plugin.app.set_offgrid_mode(enabled)
        return {"type": "offgrid_mode_set", **result}


def _on_alert_event(event_type: str, data: dict) -> None:
    """Event-bus callback for ALERT_TRIGGERED — push to WS clients."""
    loop = _ws_loop
    if loop is None or not _ws_clients:
        return
    try:
        loop.call_soon_threadsafe(_schedule_push, "alert", data)
    except (RuntimeError, AttributeError):
        pass


def _on_internet_event(event_type: str, data: dict) -> None:
    """Event-bus callback for INTERNET_ONLINE/OFFLINE — push to WS clients."""
    loop = _ws_loop
    plugin = _ws_plugin
    if loop is None or not _ws_clients:
        return
    force_offline = False
    if plugin is not None:
        probe = getattr(plugin.app, "internet_probe", None)
        if probe is not None:
            force_offline = probe.force_offline
    payload = {
        "online": event_type == "internet.online",
        "wan_ip": data.get("wan_ip"),
        "lan_ip": data.get("lan_ip"),
        "force_offline": force_offline,
    }
    try:
        loop.call_soon_threadsafe(_schedule_push, "internet_status", payload)
    except (RuntimeError, AttributeError):
        pass


def _on_firmware_event(event_type: str, data: dict) -> None:
    """Event-bus callback for MESHTASTIC_FIRMWARE_HANG/RECOVERED."""
    loop = _ws_loop
    if loop is None or not _ws_clients:
        return
    from reticulumpi import events as _ev

    payload = {
        "hang": event_type == _ev.MESHTASTIC_FIRMWARE_HANG,
        "reason": data.get("reason"),
        "silence_seconds": data.get("silence_seconds"),
        "total_hangs": data.get("total_hangs"),
        "total_resets": data.get("total_resets"),
        "consecutive_failures": data.get("consecutive_failures"),
        "duration_seconds": data.get("duration_seconds"),
    }
    try:
        loop.call_soon_threadsafe(_schedule_push, "firmware_status", payload)
    except (RuntimeError, AttributeError):
        pass


def _on_offgrid_event(event_type: str, data: dict) -> None:
    """Event-bus callback for OFFGRID_MODE_CHANGED — push to WS clients."""
    loop = _ws_loop
    if loop is None or not _ws_clients:
        return
    try:
        loop.call_soon_threadsafe(
            _schedule_push,
            "offgrid_mode_changed",
            {"enabled": data.get("enabled", False)},
        )
    except (RuntimeError, AttributeError):
        pass


def _schedule_push(push_type: str, payload: dict) -> None:
    """Schedule an async broadcast on the WS event loop."""
    try:
        asyncio.create_task(_push_to_clients(push_type, payload))
    except RuntimeError:
        pass


def _schedule_enriched_push(
    push_type: str,
    event_type: str,
    data: dict,
) -> None:
    """Schedule an async broadcast that enriches the payload before sending."""
    try:
        asyncio.create_task(_enrich_and_push(push_type, event_type, data))
    except RuntimeError:
        pass


async def _enrich_and_push(
    push_type: str,
    event_type: str,
    data: dict,
) -> None:
    """Look up the full message row in the executor, then fan out to clients."""
    loop = asyncio.get_running_loop()
    msg_id = data.get("id")
    row = None
    if msg_id is not None:
        try:
            row = await loop.run_in_executor(None, _lookup_message_row, msg_id)
        except Exception:
            pass

    if push_type == "message":
        payload = row if row else {"event": event_type, **data}
    else:
        payload = {
            "id": data.get("id"),
            "status": data.get("status"),
            "timestamp": data.get("timestamp"),
            "transport": data.get("transport"),
        }
        if row:
            payload["contact_id"] = row.get("contact_id")
            payload["sub_transport"] = row.get("sub_transport", "")
        else:
            if "contact_id" in data:
                payload["contact_id"] = data.get("contact_id")
            if "sub_transport" in data:
                payload["sub_transport"] = data.get("sub_transport") or ""

    await _push_to_clients(push_type, payload)


async def _push_to_clients(push_type: str, payload: dict) -> None:
    """Send a targeted update to all connected clients.

    Fans out sends concurrently so a single slow peer (congested link,
    large TCP send-queue) can't block other subscribers from getting
    status updates during the same event.

    Bounded by ``_push_sem`` so message storms can't accumulate
    unbounded tasks on the event loop.
    """
    sem = _push_sem
    if sem is not None:
        await sem.acquire()
    try:
        if not _ws_clients:
            return
        message = json.dumps(
            {
                "type": push_type,
                "data": payload,
                "timestamp": time.time(),
            },
            default=str,
        )
        clients = list(_ws_clients)

        results = await asyncio.gather(
            *(_send_with_timeout(ws, message) for ws in clients),
            return_exceptions=False,
        )
        now = time.time()
        for ws, ok in zip(clients, results):
            if not ok:
                _ws_clients.discard(ws)
                _ws_last_activity.pop(ws, None)
            else:
                _ws_last_activity[ws] = now
    finally:
        if sem is not None:
            sem.release()
