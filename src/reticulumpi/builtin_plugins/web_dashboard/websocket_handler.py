"""WebSocket handler for real-time metrics streaming."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Any

import aiohttp
import aiohttp.web

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

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
    "frequency", "bandwidth", "txpower", "spreadingfactor", "codingrate",
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
                        "airtime_short", "airtime_long",
                        "channel_load_short", "channel_load_long",
                        "noise_floor", "interference",
                        "battery_state", "battery_percent",
                        "announce_queue", "held_announces",
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
                for k in ("frequency", "bandwidth", "txpower",
                          "spreadingfactor", "codingrate"):
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
    app.on_startup.append(_start_broadcast_task)
    app.on_shutdown.append(_stop_broadcast_task)


_ws_clients: set[aiohttp.web.WebSocketResponse] = set()
_ws_last_activity: dict[aiohttp.web.WebSocketResponse, float] = {}
_broadcast_task: asyncio.Task | None = None
# Loop + plugin refs captured at startup so cross-thread event-bus
# callbacks can push straight into the WS broadcast path without polling.
_ws_loop: asyncio.AbstractEventLoop | None = None
_ws_plugin: Any | None = None

# Dedicated single-worker executor for broadcast data collection.
# Prevents overlapping collections from saturating the default pool.
_broadcast_executor: concurrent.futures.ThreadPoolExecutor | None = None
_collection_running = threading.Event()

_BROADCAST_PLUGIN_NAMES: tuple[str, ...] = (
    "system_monitor", "network_map", "mesh_telemetry", "alert_system",
    "sensor_framework", "emergency_broadcast", "transport_monitor",
    "connectivity_monitor", "path_warmer", "transport_health",
    "meshtastic_gateway", "meshcore_gateway", "meshcore_observer",
    "mesh_bridge", "space_tracker", "spectrum_scanner", "lora_scanner",
    "lora_chirp_viewer", "lora_diagnostics", "gps_telemetry", "messaging_hub",
    "ntp_server", "adsb_radar", "lora_link_tester",
)

_plugin_refs: dict[str, Any] = {}


def _rebuild_plugin_refs(app_get_plugin: Any) -> None:
    """Resolve all broadcast plugin references and cache them."""
    global _plugin_refs
    refs: dict[str, Any] = {}
    for name in _BROADCAST_PLUGIN_NAMES:
        try:
            refs[name] = app_get_plugin(name)
        except Exception:
            refs[name] = None
    _plugin_refs = refs


def _on_plugin_lifecycle(_event_type: str, _data: dict[str, Any]) -> None:
    """Invalidate plugin ref cache when a plugin starts/stops/crashes."""
    if _ws_plugin is not None:
        _rebuild_plugin_refs(_ws_plugin.app.get_plugin)


async def websocket_metrics(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    """Handle WebSocket connections for live metrics streaming."""
    plugin = request.app["plugin"]
    max_clients = plugin.config.get("max_websocket_clients", 10)

    # Authenticate via cookie or Authorization header
    token = request.cookies.get("session")

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

    ws = aiohttp.web.WebSocketResponse(heartbeat=60.0)
    await ws.prepare(request)

    # Send the spectrum history buffer BEFORE joining the broadcast pool so
    # a live update tick can't race ahead of the initial backfill.  The
    # frontend's shared spectrumCommon.historyStore absorbs this frame and
    # bumps its generation, which triggers a one-shot bulk paint in each
    # spectrum panel.
    scanner = plugin.app.get_plugin("spectrum_scanner")
    if scanner and hasattr(scanner, "get_history"):
        try:
            history_payload = scanner.get_history()
        except Exception:
            history_payload = {"available": False, "rows": []}
        try:
            await ws.send_str(json.dumps({
                "type": "spectrum_history",
                "data": history_payload,
            }))
        except Exception:
            log.debug("Failed to send spectrum history hello", exc_info=True)

    lora_scanner = (
        plugin.app.get_plugin("lora_scanner")
        or plugin.app.get_plugin("lora_chirp_viewer")
    )
    if lora_scanner and hasattr(lora_scanner, "get_history"):
        try:
            lora_hist = lora_scanner.get_history()
        except Exception:
            lora_hist = {"available": False, "rows": []}
        try:
            await ws.send_str(json.dumps({
                "type": "lora_scanner_history",
                "data": lora_hist,
            }))
        except Exception:
            log.debug("Failed to send lora_scanner history hello", exc_info=True)

    chirp_viewer = plugin.app.get_plugin("lora_chirp_viewer")
    if chirp_viewer and hasattr(chirp_viewer, "get_chirp_waterfall_history"):
        try:
            chirp_hist = chirp_viewer.get_chirp_waterfall_history()
        except Exception:
            chirp_hist = {"available": False}
        try:
            await ws.send_str(json.dumps({
                "type": "chirp_waterfall_history",
                "data": chirp_hist,
            }))
        except Exception:
            log.debug("Failed to send chirp waterfall history hello", exc_info=True)

    if chirp_viewer and hasattr(chirp_viewer, "get_detection_history"):
        try:
            det_hist = chirp_viewer.get_detection_history()
        except Exception:
            det_hist = []
        if det_hist:
            try:
                await ws.send_str(json.dumps({
                    "type": "chirp_detection_history",
                    "data": det_hist,
                }))
            except Exception:
                log.debug("Failed to send chirp detection history hello", exc_info=True)

    link_tester = plugin.app.get_plugin("lora_link_tester")
    if link_tester and hasattr(link_tester, "get_history"):
        try:
            lt_hist = link_tester.get_history()
        except Exception:
            lt_hist = {"available": False}
        try:
            await ws.send_str(json.dumps({
                "type": "link_tester_history",
                "data": lt_hist,
            }))
        except Exception:
            log.debug("Failed to send link_tester history hello", exc_info=True)

    # Send a full data snapshot so every panel populates immediately
    # instead of waiting up to 5s for the next broadcast cycle.
    loop = asyncio.get_running_loop()
    try:
        data, _, _ = await loop.run_in_executor(
            None,
            _collect_broadcast_data,
            plugin,
            0,
            _last_mesh_announce_ts,
            _mesh_version,
            _plugin_refs,
        )
        await ws.send_str(json.dumps({
            "type": "update",
            "data": data,
            "timestamp": time.time(),
        }))
    except Exception:
        log.debug("Failed to send initial data snapshot", exc_info=True)

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
                _handle_ws_command(msg.data, plugin)
    finally:
        _ws_clients.discard(ws)
        _ws_last_activity.pop(ws, None)
        log.info(
            "WebSocket disconnected (code=%s, %d remaining)",
            ws.close_code,
            len(_ws_clients),
        )

    return ws


_last_mesh_announce_ts: float = 0  # track last announce time for delta broadcasts
_mesh_version: int = 0  # increments when mesh data changes

# Slow-plugin threshold for per-plugin timing warnings (seconds).
_SLOW_PLUGIN_THRESHOLD = 0.2


def _collect_broadcast_data(
    plugin: Any,
    cycle_count: int,
    last_mesh_announce_ts: float,
    mesh_version: int,
    plugin_refs: dict[str, Any],
) -> tuple[dict[str, Any], float, int]:
    """Collect all plugin data synchronously.  Runs in a thread executor.

    Plugins are collected in priority order.  A time budget (75% of the
    configured metrics_interval) ensures that expensive plugins at the
    tail don't stall the broadcast loop and starve the event-loop of
    CPU time for heartbeat processing.
    """
    refs = plugin_refs
    budget = plugin.config.get("metrics_interval", 5) * 0.75
    t0 = time.monotonic()
    skipped: list[str] = []

    def _over_budget() -> bool:
        return (time.monotonic() - t0) >= budget

    def _timed(label: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Call *fn* and warn if it exceeds the slow-plugin threshold."""
        t = time.monotonic()
        try:
            return fn(*args, **kwargs)
        except Exception:
            return None
        finally:
            elapsed = time.monotonic() - t
            if elapsed > _SLOW_PLUGIN_THRESHOLD:
                log.warning("Slow broadcast plugin %s: %.0fms", label, elapsed * 1000)

    # ── Critical tier (always collected) ─────────────────────────────

    monitor = refs.get("system_monitor")
    metrics = {}
    if monitor and hasattr(monitor, "latest_metrics"):
        try:
            metrics = monitor.latest_metrics
        except Exception:
            pass

    plugin_statuses = {}
    for name, p in plugin.app.plugins.items():
        try:
            plugin_statuses[name] = {"active": p.get_status().get("active", False)}
        except Exception:
            plugin_statuses[name] = {"active": False}

    interfaces = _collect_interfaces(plugin.app.reticulum)

    connectivity_data: dict = {}
    conn_mon = refs.get("connectivity_monitor")
    if conn_mon and hasattr(conn_mon, "get_health"):
        connectivity_data = _timed("connectivity_monitor", conn_mon.get_health) or {}

    routing_data: dict = {}
    if connectivity_data:
        routing_data = connectivity_data.get("routing", {})

    transport_data: dict = {}
    transport_mon = refs.get("transport_monitor")
    if transport_mon and hasattr(transport_mon, "get_hub_health"):
        transport_data = _timed("transport_monitor", transport_mon.get_hub_health) or {}
        if transport_data:
            try:
                _enrich_transport_traffic(transport_data, interfaces)
            except Exception:
                pass

    # ── Standard tier (collected if time remains) ────────────────────

    mesh_data: dict = {}
    if not _over_budget():
        network_map = refs.get("network_map")
        if network_map:
            try:
                if hasattr(network_map, "get_node_count"):
                    mesh_data["known_nodes"] = network_map.get_node_count()
                elif hasattr(network_map, "get_known_nodes"):
                    mesh_data["known_nodes"] = len(network_map.get_known_nodes())

                if hasattr(network_map, "get_recent_announces"):
                    recent = _timed(
                        "network_map.recent",
                        network_map.get_recent_announces,
                        since=last_mesh_announce_ts,
                        limit=20,
                    )
                    if recent:
                        mesh_data["recent_announces"] = recent
                        mesh_version += 1
                        last_mesh_announce_ts = max(
                            n.get("last_seen", 0) for n in recent
                        )
                mesh_data["version"] = mesh_version

                if cycle_count % 3 == 0 and hasattr(network_map, "get_mesh_summary"):
                    mesh_data["summary"] = _timed(
                        "network_map.summary", network_map.get_mesh_summary,
                    )
            except Exception:
                pass
    else:
        skipped.append("network_map")

    if not _over_budget():
        telemetry = refs.get("mesh_telemetry")
        if telemetry and hasattr(telemetry, "get_peer_metrics"):
            peers = _timed("mesh_telemetry", telemetry.get_peer_metrics)
            if peers is not None:
                mesh_data["peers"] = peers
                mesh_data["peer_count"] = len(peers)
    else:
        skipped.append("mesh_telemetry")

    alert_sys = refs.get("alert_system")
    if alert_sys:
        try:
            alert_status = alert_sys.get_status()
            mesh_data["alerts_sent"] = alert_status.get("alerts_sent", 0)
            mesh_data["last_alert"] = alert_status.get("last_alert")
        except Exception:
            pass

    emergency_data: dict = {}
    emergency = refs.get("emergency_broadcast")
    if emergency and hasattr(emergency, "get_status"):
        try:
            emergency_data = emergency.get_status()
        except Exception:
            pass

    path_warming_data: dict = {}
    warmer = refs.get("path_warmer")
    if warmer and hasattr(warmer, "get_warming_stats"):
        try:
            path_warming_data = warmer.get_warming_stats()
        except Exception:
            pass

    transport_health_data: dict = {}
    th = refs.get("transport_health")
    if th and hasattr(th, "get_transport_summary"):
        try:
            transport_health_data = th.get_transport_summary()
        except Exception:
            pass

    meshtastic_device_data: dict = {}
    meshtastic_status: dict = {}
    meshtastic_nodes: list = []
    meshtastic_lora_neighbors: list = []
    if not _over_budget():
        meshtastic_gw = refs.get("meshtastic_gateway")
        if meshtastic_gw:
            if hasattr(meshtastic_gw, "get_device_info"):
                meshtastic_device_data = _timed(
                    "meshtastic.device", meshtastic_gw.get_device_info,
                ) or {}
            if hasattr(meshtastic_gw, "get_status"):
                meshtastic_status = _timed(
                    "meshtastic.status", meshtastic_gw.get_status,
                ) or {}
            if hasattr(meshtastic_gw, "get_meshtastic_nodes"):
                meshtastic_nodes = _timed(
                    "meshtastic.nodes", meshtastic_gw.get_meshtastic_nodes,
                ) or []
            if hasattr(meshtastic_gw, "get_lora_neighbors"):
                meshtastic_lora_neighbors = _timed(
                    "meshtastic.neighbors", meshtastic_gw.get_lora_neighbors,
                ) or []
    else:
        skipped.append("meshtastic_gateway")

    meshcore_device_data: dict = {}
    meshcore_contacts: list = []
    meshcore_status: dict = {}
    if not _over_budget():
        meshcore_gw = refs.get("meshcore_gateway")
        if meshcore_gw:
            if hasattr(meshcore_gw, "get_device_info"):
                meshcore_device_data = _timed(
                    "meshcore.device", meshcore_gw.get_device_info,
                ) or {}
            if hasattr(meshcore_gw, "get_contacts"):
                meshcore_contacts = _timed(
                    "meshcore.contacts", meshcore_gw.get_contacts,
                ) or []
            if hasattr(meshcore_gw, "get_status"):
                meshcore_status = _timed(
                    "meshcore.status", meshcore_gw.get_status,
                ) or {}
    else:
        skipped.append("meshcore_gateway")

    meshcore_obs_status: dict = {}
    meshcore_obs = refs.get("meshcore_observer")
    if meshcore_obs and hasattr(meshcore_obs, "get_status"):
        try:
            meshcore_obs_status = {"available": True, **meshcore_obs.get_status()}
        except Exception:
            pass

    mesh_bridge_data: dict = {}
    mesh_bridge = refs.get("mesh_bridge")
    if mesh_bridge and hasattr(mesh_bridge, "get_status"):
        try:
            mesh_bridge_data = mesh_bridge.get_status()
        except Exception:
            pass

    messaging_data: dict = {}
    if not _over_budget():
        msg_hub = refs.get("messaging_hub")
        if msg_hub and hasattr(msg_hub, "get_transports"):
            try:
                transports = msg_hub.get_transports()
                if transports:
                    messaging_data["transports"] = transports
                if hasattr(msg_hub, "get_unread_counts_grouped"):
                    messaging_data["unread"] = msg_hub.get_unread_counts_grouped()
                elif hasattr(msg_hub, "get_unread_counts"):
                    messaging_data["unread"] = msg_hub.get_unread_counts()
            except Exception:
                pass
    else:
        skipped.append("messaging_hub")

    # ── Expensive tier (collected last, skippable) ───────────────────

    space_data: dict = {}
    if not _over_budget():
        space_tracker = refs.get("space_tracker")
        if space_tracker and hasattr(space_tracker, "get_snapshot"):
            snap = _timed("space_tracker", space_tracker.get_snapshot)
            if snap:
                space_data = {
                    "tle_groups": snap.get("tle_groups", {}),
                    "launches": (snap.get("launches") or [])[:5],
                    "weather": snap.get("weather"),
                    "observer": snap.get("observer"),
                    "passes": (snap.get("passes") or [])[:10],
                    "passes_computed_at": snap.get("passes_computed_at"),
                }
                positions = snap.get("positions") or {}
                objects = positions.get("objects") or []
                if objects:
                    if snap.get("observer"):
                        objects = sorted(
                            objects,
                            key=lambda o: o.get("el", -999),
                            reverse=True,
                        )
                    space_data["positions"] = {
                        "fetched_at": positions.get("fetched_at"),
                        "count": positions.get("count", len(objects)),
                        "objects": objects[:60],
                    }
    else:
        skipped.append("space_tracker")

    spectrum_data: dict = {}
    if not _over_budget():
        scanner = refs.get("spectrum_scanner")
        if scanner and hasattr(scanner, "get_snapshot"):
            spectrum_data = _timed("spectrum_scanner", scanner.get_snapshot) or {}
    else:
        skipped.append("spectrum_scanner")

    lora_scanner_data: dict = {}
    lora_chirp_viewer_data: dict = {}
    if not _over_budget():
        lora_scanner = refs.get("lora_scanner")
        if lora_scanner and hasattr(lora_scanner, "get_snapshot"):
            lora_scanner_data = _timed("lora_scanner", lora_scanner.get_snapshot) or {}
        lora_chirp = refs.get("lora_chirp_viewer")
        if lora_chirp and hasattr(lora_chirp, "get_snapshot"):
            lora_chirp_viewer_data = _timed("lora_chirp_viewer", lora_chirp.get_snapshot) or {}
    else:
        skipped.append("lora_scanner")

    lora_diag_data: dict = {}
    lora_diag = refs.get("lora_diagnostics")
    if lora_diag and hasattr(lora_diag, "get_diagnostics"):
        try:
            d = lora_diag.get_diagnostics()
            li = d.get("lora_interface", {}) or {}
            lora_diag_data = {
                "channel_load_short": li.get("channel_load_short"),
                "channel_load_long":  li.get("channel_load_long"),
                "airtime_short":      li.get("airtime_short"),
                "airtime_long":       li.get("airtime_long"),
                "announce_queue":     li.get("announce_queue"),
                "online":             li.get("online"),
            }
        except Exception:
            pass

    gps_data: dict = {}
    if not _over_budget():
        gps = refs.get("gps_telemetry")
        if gps and hasattr(gps, "get_snapshot"):
            gps_data = _timed("gps_telemetry", gps.get_snapshot) or {}
    else:
        skipped.append("gps_telemetry")

    sensor_data: dict = {}
    if not _over_budget():
        sensor_fw = refs.get("sensor_framework")
        if sensor_fw and hasattr(sensor_fw, "get_latest_readings"):
            sensor_data = _timed("sensor_framework", sensor_fw.get_latest_readings) or {}
    else:
        skipped.append("sensor_framework")

    # ── Assemble response ────────────────────────────────────────────

    data: dict[str, Any] = {
        "metrics": metrics,
        "plugins": plugin_statuses,
        "interfaces": interfaces,
        "sensors": sensor_data,
        "emergency": emergency_data,
        "transport": transport_data,
        "connectivity": connectivity_data,
        "routing": routing_data,
        "path_warming": path_warming_data,
        "transport_health": transport_health_data,
    }
    if meshtastic_device_data:
        data["meshtastic_device"] = meshtastic_device_data
    if meshtastic_status:
        data["meshtastic_status"] = meshtastic_status
    if meshtastic_nodes:
        data["meshtastic_nodes"] = meshtastic_nodes
    if meshtastic_lora_neighbors:
        data["meshtastic_lora_neighbors"] = meshtastic_lora_neighbors
    if meshcore_status:
        data["meshcore_status"] = meshcore_status
    if meshcore_device_data:
        data["meshcore_device"] = meshcore_device_data
    if meshcore_contacts:
        data["meshcore_contacts"] = meshcore_contacts
    if meshcore_obs_status:
        data["meshcore_observer"] = meshcore_obs_status
    if messaging_data:
        data["messaging"] = messaging_data
    if mesh_data:
        data["mesh"] = mesh_data
    if space_data:
        data["space"] = space_data
    if spectrum_data:
        data["spectrum"] = spectrum_data
    if lora_scanner_data:
        data["lora_scanner"] = lora_scanner_data
    if lora_chirp_viewer_data:
        data["lora_chirp_viewer"] = lora_chirp_viewer_data
    if lora_diag_data:
        data["lora_diagnostics"] = lora_diag_data
    if gps_data:
        data["gps"] = gps_data
    if mesh_bridge_data:
        data["mesh_bridge"] = mesh_bridge_data

    if not _over_budget():
        ntp_srv = refs.get("ntp_server")
        if ntp_srv and hasattr(ntp_srv, "get_snapshot"):
            ntp_data = _timed("ntp_server", ntp_srv.get_snapshot)
            if ntp_data:
                data["ntp"] = ntp_data
    else:
        skipped.append("ntp_server")

    if not _over_budget():
        adsb_plugin = refs.get("adsb_radar")
        if adsb_plugin and hasattr(adsb_plugin, "get_snapshot"):
            adsb_data = _timed("adsb_radar", adsb_plugin.get_snapshot)
            if adsb_data:
                data["adsb"] = adsb_data
    else:
        skipped.append("adsb_radar")

    if not _over_budget():
        lt_plugin = refs.get("lora_link_tester")
        if lt_plugin and hasattr(lt_plugin, "get_snapshot"):
            lt_data = _timed("lora_link_tester", lt_plugin.get_snapshot)
            if lt_data:
                data["link_tester"] = lt_data
    else:
        skipped.append("lora_link_tester")

    if skipped:
        log.info("Broadcast budget exceeded — skipped: %s", ", ".join(skipped))

    return data, last_mesh_announce_ts, mesh_version


async def _broadcast_metrics(app: aiohttp.web.Application) -> None:
    """Periodically broadcast system metrics to all connected WebSocket clients."""
    global _last_mesh_announce_ts, _mesh_version

    plugin = app["plugin"]
    interval = plugin.config.get("metrics_interval", 5)
    _cycle_count = 0
    loop = asyncio.get_running_loop()

    while True:
        try:
            await asyncio.sleep(interval)

            _cycle_count += 1

            if not _ws_clients:
                continue

            # Skip this cycle if the previous collection hasn't finished.
            if _collection_running.is_set():
                log.debug("Skipping broadcast cycle — previous collection still running")
                continue

            _collection_running.set()
            t_collect = time.monotonic()
            try:
                data, _last_mesh_announce_ts, _mesh_version = (
                    await loop.run_in_executor(
                        _broadcast_executor,
                        _collect_broadcast_data,
                        plugin,
                        _cycle_count,
                        _last_mesh_announce_ts,
                        _mesh_version,
                        _plugin_refs,
                    )
                )
            finally:
                _collection_running.clear()

            collect_elapsed = time.monotonic() - t_collect

            message = json.dumps({
                "type": "update",
                "data": data,
                "timestamp": time.time(),
            })

            # Fan out to all clients concurrently (mirrors _push_to_clients).
            clients = list(_ws_clients)

            async def _send(ws: aiohttp.web.WebSocketResponse) -> bool:
                try:
                    await ws.send_str(message)
                    return True
                except Exception:
                    return False

            results = await asyncio.gather(*(_send(ws) for ws in clients))
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
                stale = [
                    ws for ws, last in _ws_last_activity.items()
                    if now - last > stale_timeout
                ]
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
                    "WS broadcast: collect=%.0fms payload=%dB clients=%d",
                    collect_elapsed * 1000,
                    len(message),
                    len(clients),
                )

        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("Error in metrics broadcast")
            await asyncio.sleep(1)


async def _start_broadcast_task(app: aiohttp.web.Application) -> None:
    global _broadcast_task, _ws_loop, _ws_plugin, _broadcast_executor
    _ws_loop = asyncio.get_running_loop()
    _ws_plugin = app["plugin"]
    _broadcast_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="ws-broadcast",
    )
    _rebuild_plugin_refs(_ws_plugin.app.get_plugin)
    _broadcast_task = asyncio.create_task(_broadcast_metrics(app))
    try:
        from reticulumpi import events as _events
        event_bus = _ws_plugin.app.event_bus
        event_bus.subscribe(_events.MESSAGE_RECEIVED, _on_message_event)
        event_bus.subscribe(_events.MESSAGE_SENT, _on_message_event)
        event_bus.subscribe(_events.MESSAGE_STATUS_CHANGED, _on_status_event)
        event_bus.subscribe(_events.MESSAGE_REACTION_RECEIVED, _on_reaction_event)
        event_bus.subscribe(_events.ALERT_TRIGGERED, _on_alert_event)
        event_bus.subscribe(_events.PLUGIN_STARTED, _on_plugin_lifecycle)
        event_bus.subscribe(_events.PLUGIN_STOPPED, _on_plugin_lifecycle)
        event_bus.subscribe(_events.PLUGIN_CRASHED, _on_plugin_lifecycle)
        event_bus.subscribe(_events.CHIRP_CAPTURE_DONE, _on_chirp_capture_done)
        event_bus.subscribe(_events.CHIRP_WATERFALL_ROWS, _on_chirp_waterfall_rows)
        event_bus.subscribe(_events.CHIRP_DETECTION, _on_chirp_detection)
        event_bus.subscribe(_events.CHIRP_PACKET_DECODED, _on_chirp_packet_decoded)
    except Exception:
        log.exception("Failed to subscribe WS handler to events")


async def _stop_broadcast_task(app: aiohttp.web.Application) -> None:
    global _broadcast_task, _ws_loop, _ws_plugin, _broadcast_executor
    try:
        if _ws_plugin is not None:
            event_bus = _ws_plugin.app.event_bus
            event_bus.unsubscribe_all(_on_message_event)
            event_bus.unsubscribe_all(_on_status_event)
            event_bus.unsubscribe_all(_on_reaction_event)
            event_bus.unsubscribe_all(_on_alert_event)
            event_bus.unsubscribe_all(_on_plugin_lifecycle)
            event_bus.unsubscribe_all(_on_chirp_capture_done)
            event_bus.unsubscribe_all(_on_chirp_waterfall_rows)
            event_bus.unsubscribe_all(_on_chirp_detection)
            event_bus.unsubscribe_all(_on_chirp_packet_decoded)
    except Exception:
        log.debug("Error unsubscribing WS handler", exc_info=True)
    if _broadcast_task:
        _broadcast_task.cancel()
        try:
            await _broadcast_task
        except asyncio.CancelledError:
            pass
        _broadcast_task = None
    _ws_loop = None
    _ws_plugin = None
    if _broadcast_executor is not None:
        _broadcast_executor.shutdown(wait=False)
        _broadcast_executor = None
    # Close all WebSocket connections
    for ws in list(_ws_clients):
        await ws.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b"Server shutting down")
    _ws_clients.clear()
    _ws_last_activity.clear()


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
    if _ws_loop is None or not _ws_clients:
        return
    row = _lookup_message_row(data.get("id"))
    # Fall back to the skeletal event payload if the row isn't yet
    # readable — avoids dropping events during races, and the client
    # dedupes by id regardless.
    payload = row if row else {"event": event_type, **data}
    try:
        _ws_loop.call_soon_threadsafe(_schedule_push, "message", payload)
    except RuntimeError:
        # Loop already closed — happens during shutdown; safe to ignore.
        pass


def _on_status_event(event_type: str, data: dict) -> None:
    """Event-bus callback for MESSAGE_STATUS_CHANGED — same pattern."""
    if _ws_loop is None or not _ws_clients:
        return
    row = _lookup_message_row(data.get("id"))
    payload = {
        "id": data.get("id"),
        "status": data.get("status"),
        "timestamp": data.get("timestamp"),
        "transport": data.get("transport"),
    }
    # Prefer the live row — it reflects the post-update state. If the
    # lookup failed (plugin teardown, rare race), fall back to the
    # contact_id/sub_transport that messaging_hub included on the event
    # payload, so the client can still route the status update.
    if row:
        payload["contact_id"] = row.get("contact_id")
        payload["sub_transport"] = row.get("sub_transport", "")
    else:
        if "contact_id" in data:
            payload["contact_id"] = data.get("contact_id")
        if "sub_transport" in data:
            payload["sub_transport"] = data.get("sub_transport") or ""
    try:
        _ws_loop.call_soon_threadsafe(_schedule_push, "message_status", payload)
    except RuntimeError:
        pass


def _on_reaction_event(event_type: str, data: dict) -> None:
    """Event-bus callback for MESSAGE_REACTION_RECEIVED — push to WS clients."""
    if _ws_loop is None or not _ws_clients:
        return
    try:
        _ws_loop.call_soon_threadsafe(_schedule_push, "reaction", data)
    except RuntimeError:
        pass


def _on_chirp_capture_done(event_type: str, data: dict) -> None:
    """Event-bus callback for CHIRP_CAPTURE_DONE — push spectrogram to WS clients."""
    if _ws_loop is None or not _ws_clients:
        return
    try:
        _ws_loop.call_soon_threadsafe(_schedule_push, "chirp_result", data)
    except RuntimeError:
        pass


def _on_chirp_waterfall_rows(event_type: str, data: dict) -> None:
    """Event-bus callback for CHIRP_WATERFALL_ROWS — push streaming batch."""
    if _ws_loop is None or not _ws_clients:
        return
    try:
        _ws_loop.call_soon_threadsafe(_schedule_push, "chirp_waterfall_rows", data)
    except RuntimeError:
        pass


def _on_chirp_detection(event_type: str, data: dict) -> None:
    """Event-bus callback for CHIRP_DETECTION — push detection to WS clients."""
    if _ws_loop is None or not _ws_clients:
        return
    try:
        _ws_loop.call_soon_threadsafe(_schedule_push, "chirp_detection", data)
    except RuntimeError:
        pass


def _on_chirp_packet_decoded(event_type: str, data: dict) -> None:
    """Event-bus callback for CHIRP_PACKET_DECODED — push decoded packet to WS."""
    if _ws_loop is None or not _ws_clients:
        return
    try:
        _ws_loop.call_soon_threadsafe(_schedule_push, "chirp_packet_decoded", data)
    except RuntimeError:
        pass


def _handle_ws_command(raw: str, plugin: Any) -> None:
    """Process a JSON command from a WebSocket client."""
    try:
        cmd = json.loads(raw)
    except Exception:
        return
    action = cmd.get("action")
    if action == "chirp_capture":
        viewer = plugin.app.plugins.get("lora_chirp_viewer")
        if viewer and hasattr(viewer, "capture_chirps"):
            try:
                viewer.capture_chirps(
                    freq_hz=int(cmd["freq_hz"]) if "freq_hz" in cmd else None,
                    sample_rate=int(cmd["sample_rate"]) if "sample_rate" in cmd else None,
                    duration_s=float(cmd["duration_s"]) if "duration_s" in cmd else None,
                )
            except Exception:
                log.debug("Chirp capture command failed", exc_info=True)
    elif action == "chirp_set_params":
        viewer = plugin.app.plugins.get("lora_chirp_viewer")
        if viewer and hasattr(viewer, "set_continuous_params"):
            try:
                viewer.set_continuous_params(
                    freq_mhz=float(cmd["freq_mhz"]) if "freq_mhz" in cmd else None,
                    sample_rate=int(cmd["sample_rate"]) if "sample_rate" in cmd else None,
                )
            except Exception:
                log.debug("Chirp set_params command failed", exc_info=True)


def _on_alert_event(event_type: str, data: dict) -> None:
    """Event-bus callback for ALERT_TRIGGERED — push to WS clients."""
    if _ws_loop is None or not _ws_clients:
        return
    try:
        _ws_loop.call_soon_threadsafe(_schedule_push, "alert", data)
    except RuntimeError:
        pass


def _schedule_push(push_type: str, payload: dict) -> None:
    """Schedule an async broadcast on the WS event loop."""
    try:
        asyncio.create_task(_push_to_clients(push_type, payload))
    except RuntimeError:
        # No running loop — shutting down.
        pass


async def _push_to_clients(push_type: str, payload: dict) -> None:
    """Send a targeted update to all connected clients.

    Fans out sends concurrently so a single slow peer (congested link,
    large TCP send-queue) can't block other subscribers from getting
    status updates during the same event.
    """
    if not _ws_clients:
        return
    message = json.dumps({
        "type": push_type,
        "data": payload,
        "timestamp": time.time(),
    })
    clients = list(_ws_clients)

    async def _send_one(ws: aiohttp.web.WebSocketResponse) -> bool:
        try:
            await ws.send_str(message)
            return True
        except Exception:
            return False

    results = await asyncio.gather(
        *(_send_one(ws) for ws in clients), return_exceptions=False,
    )
    for ws, ok in zip(clients, results):
        if not ok:
            _ws_clients.discard(ws)
