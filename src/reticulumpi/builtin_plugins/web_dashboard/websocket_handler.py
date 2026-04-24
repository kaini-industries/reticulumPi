"""WebSocket handler for real-time metrics streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import os
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
_broadcast_task: asyncio.Task | None = None
# Loop + plugin refs captured at startup so cross-thread event-bus
# callbacks can push straight into the WS broadcast path without polling.
_ws_loop: asyncio.AbstractEventLoop | None = None
_ws_plugin: Any | None = None


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

    _ws_clients.add(ws)
    log.debug("WebSocket client connected (%d total)", len(_ws_clients))

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.ERROR:
                log.debug("WebSocket error: %s", ws.exception())
                break
            # We don't expect client messages, but handle ping/pong gracefully
    finally:
        _ws_clients.discard(ws)
        log.debug("WebSocket client disconnected (%d remaining)", len(_ws_clients))

    return ws


_last_mesh_announce_ts: float = 0  # track last announce time for delta broadcasts
_mesh_version: int = 0  # increments when mesh data changes


async def _broadcast_metrics(app: aiohttp.web.Application) -> None:
    """Periodically broadcast system metrics to all connected WebSocket clients."""
    global _last_mesh_announce_ts, _mesh_version

    plugin = app["plugin"]
    interval = plugin.config.get("metrics_interval", 5)
    _cycle_count = 0

    while True:
        try:
            await asyncio.sleep(interval)

            _cycle_count += 1

            if not _ws_clients:
                continue

            # Collect metrics
            monitor = plugin.app.get_plugin("system_monitor")
            metrics = {}
            if monitor and hasattr(monitor, "latest_metrics"):
                try:
                    metrics = monitor.latest_metrics
                except Exception:
                    pass

            # Collect plugin statuses
            plugin_statuses = {}
            for name, p in plugin.app.plugins.items():
                try:
                    plugin_statuses[name] = {"active": p.get_status().get("active", False)}
                except Exception:
                    plugin_statuses[name] = {"active": False}

            # Collect interface traffic data
            interfaces = _collect_interfaces(plugin.app.reticulum)

            # Collect mesh data as SUMMARY + DELTAS (not full node list)
            # This reduces broadcast size from ~100KB to ~1KB per cycle
            mesh_data: dict = {}
            network_map = plugin.app.get_plugin("network_map")
            if network_map:
                try:
                    if hasattr(network_map, "get_node_count"):
                        mesh_data["known_nodes"] = network_map.get_node_count()
                    elif hasattr(network_map, "get_known_nodes"):
                        mesh_data["known_nodes"] = len(network_map.get_known_nodes())

                    # Send only recent announces (delta) instead of full list
                    if hasattr(network_map, "get_recent_announces"):
                        recent = network_map.get_recent_announces(
                            since=_last_mesh_announce_ts, limit=20,
                        )
                        if recent:
                            mesh_data["recent_announces"] = recent
                            _mesh_version += 1
                            _last_mesh_announce_ts = max(
                                n.get("last_seen", 0) for n in recent
                            )
                    mesh_data["version"] = _mesh_version

                    # Include summary stats every 3rd cycle (~15s) for live dashboard
                    if _cycle_count % 3 == 0 and hasattr(network_map, "get_mesh_summary"):
                        mesh_data["summary"] = network_map.get_mesh_summary()
                except Exception:
                    pass

            telemetry = plugin.app.get_plugin("mesh_telemetry")
            if telemetry and hasattr(telemetry, "get_peer_metrics"):
                try:
                    peers = telemetry.get_peer_metrics()
                    mesh_data["peers"] = peers
                    mesh_data["peer_count"] = len(peers)
                except Exception:
                    pass

            alert_sys = plugin.app.get_plugin("alert_system")
            if alert_sys:
                try:
                    alert_status = alert_sys.get_status()
                    mesh_data["alerts_sent"] = alert_status.get("alerts_sent", 0)
                    mesh_data["last_alert"] = alert_status.get("last_alert")
                except Exception:
                    pass

            # Collect sensor data (if plugin available)
            sensor_data: dict = {}
            sensor_fw = plugin.app.get_plugin("sensor_framework")
            if sensor_fw and hasattr(sensor_fw, "get_latest_readings"):
                try:
                    sensor_data = sensor_fw.get_latest_readings()
                except Exception:
                    pass

            # Collect emergency data (if plugin available)
            emergency_data: dict = {}
            emergency = plugin.app.get_plugin("emergency_broadcast")
            if emergency and hasattr(emergency, "get_status"):
                try:
                    emergency_data = emergency.get_status()
                except Exception:
                    pass

            transport_data: dict = {}
            transport_mon = plugin.app.get_plugin("transport_monitor")
            if transport_mon and hasattr(transport_mon, "get_hub_health"):
                try:
                    transport_data = transport_mon.get_hub_health()
                    _enrich_transport_traffic(transport_data, interfaces)
                except Exception:
                    pass

            # Collect connectivity diagnostics (if plugin available)
            connectivity_data: dict = {}
            conn_mon = plugin.app.get_plugin("connectivity_monitor")
            if conn_mon and hasattr(conn_mon, "get_health"):
                try:
                    connectivity_data = conn_mon.get_health()
                except Exception:
                    pass

            # Extract routing summary from connectivity data (no extra RPC)
            routing_data: dict = {}
            if connectivity_data:
                routing_data = connectivity_data.get("routing", {})

            # Collect path warming stats (if plugin available)
            path_warming_data: dict = {}
            warmer = plugin.app.get_plugin("path_warmer")
            if warmer and hasattr(warmer, "get_warming_stats"):
                try:
                    path_warming_data = warmer.get_warming_stats()
                except Exception:
                    pass

            # Collect transport health summary (if plugin available)
            transport_health_data: dict = {}
            th = plugin.app.get_plugin("transport_health")
            if th and hasattr(th, "get_transport_summary"):
                try:
                    transport_health_data = th.get_transport_summary()
                except Exception:
                    pass

            # Collect Meshtastic device info (if gateway plugin available)
            meshtastic_device_data: dict = {}
            meshtastic_gw = plugin.app.get_plugin("meshtastic_gateway")
            if meshtastic_gw and hasattr(meshtastic_gw, "get_device_info"):
                try:
                    meshtastic_device_data = meshtastic_gw.get_device_info()
                except Exception:
                    pass

            # Collect LoRa neighbors (if gateway plugin available)
            meshtastic_lora_neighbors: list = []
            if meshtastic_gw and hasattr(meshtastic_gw, "get_lora_neighbors"):
                try:
                    meshtastic_lora_neighbors = meshtastic_gw.get_lora_neighbors()
                except Exception:
                    pass

            # Collect MeshCore device info (if gateway plugin available)
            meshcore_device_data: dict = {}
            meshcore_gw = plugin.app.get_plugin("meshcore_gateway")
            if meshcore_gw and hasattr(meshcore_gw, "get_device_info"):
                try:
                    meshcore_device_data = meshcore_gw.get_device_info()
                except Exception:
                    pass

            # Collect MeshCore contacts (if gateway plugin available)
            meshcore_contacts: list = []
            if meshcore_gw and hasattr(meshcore_gw, "get_contacts"):
                try:
                    meshcore_contacts = meshcore_gw.get_contacts()
                except Exception:
                    pass

            # Collect MeshCore status (if gateway plugin available)
            meshcore_status: dict = {}
            if meshcore_gw and hasattr(meshcore_gw, "get_status"):
                try:
                    meshcore_status = meshcore_gw.get_status()
                except Exception:
                    pass

            meshcore_obs_status: dict = {}
            meshcore_obs = plugin.app.get_plugin("meshcore_observer")
            if meshcore_obs and hasattr(meshcore_obs, "get_status"):
                try:
                    meshcore_obs_status = {"available": True, **meshcore_obs.get_status()}
                except Exception:
                    pass

            mesh_bridge_data: dict = {}
            mesh_bridge = plugin.app.get_plugin("mesh_bridge")
            if mesh_bridge and hasattr(mesh_bridge, "get_status"):
                try:
                    mesh_bridge_data = mesh_bridge.get_status()
                except Exception:
                    pass

            # Collect space tracker snapshot (if plugin enabled).  Live
            # position deltas are published via the plugin's own event bus
            # message; here we only surface the lightweight snapshot.
            space_data: dict = {}
            space_tracker = plugin.app.get_plugin("space_tracker")
            if space_tracker and hasattr(space_tracker, "get_snapshot"):
                try:
                    snap = space_tracker.get_snapshot()
                    space_data = {
                        "tle_groups": snap.get("tle_groups", {}),
                        "launches": (snap.get("launches") or [])[:5],
                        "weather": snap.get("weather"),
                        "observer": snap.get("observer"),
                        "passes": (snap.get("passes") or [])[:10],
                        "passes_computed_at": snap.get("passes_computed_at"),
                    }
                    # Propagated positions: trim to top N (by elevation if
                    # observer, otherwise as-is) to keep broadcast size bounded.
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
                except Exception:
                    pass

            # Collect spectrum scanner snapshot (if plugin enabled).  The
            # plugin hands us a pruned, wire-ready dict; we attach it
            # verbatim.  Snapshot includes a rolling waterfall buffer
            # (capped by the plugin's config), so size is bounded.
            spectrum_data: dict = {}
            scanner = plugin.app.get_plugin("spectrum_scanner")
            if scanner and hasattr(scanner, "get_snapshot"):
                try:
                    spectrum_data = scanner.get_snapshot()
                except Exception:
                    pass

            # Collect RNode PHY channel-load / airtime from lora_diagnostics.
            # Surfaces RNode's own view of how busy the channel is, so the
            # LoRa Spectrum panel can show it next to the SDR-derived bars.
            # channel_load_* / announce_queue / online populate at runtime
            # after the first monitor tick; before that they're absent and
            # .get(...) returns None, which the frontend handles.
            lora_diag_data: dict = {}
            lora_diag = plugin.app.get_plugin("lora_diagnostics")
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

            # Collect GPS telemetry snapshot (if plugin enabled).
            gps_data: dict = {}
            gps = plugin.app.get_plugin("gps_telemetry")
            if gps and hasattr(gps, "get_snapshot"):
                try:
                    gps_data = gps.get_snapshot()
                except Exception:
                    pass

            # Messaging state snapshot for the tick.  New messages and
            # status changes are delivered via the per-event `message` /
            # `message_status` WS envelopes (see `_on_message_event`),
            # so the tick only carries slow-moving state (transports +
            # unread counts) as a reconnect-safe backstop.
            messaging_data: dict = {}
            msg_hub = plugin.app.get_plugin("messaging_hub")
            if msg_hub and hasattr(msg_hub, "get_transports"):
                try:
                    transports = msg_hub.get_transports()
                    if transports:
                        messaging_data["transports"] = transports
                    # Always send the unread map (even empty). If we skip
                    # empty maps, clients that marked a conversation read
                    # never observe the transition and keep a stale local
                    # count until a new message arrives.
                    if hasattr(msg_hub, "get_unread_counts_grouped"):
                        messaging_data["unread"] = msg_hub.get_unread_counts_grouped()
                    elif hasattr(msg_hub, "get_unread_counts"):
                        messaging_data["unread"] = msg_hub.get_unread_counts()
                except Exception:
                    pass

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
            if lora_diag_data:
                data["lora_diagnostics"] = lora_diag_data
            if gps_data:
                data["gps"] = gps_data
            if mesh_bridge_data:
                data["mesh_bridge"] = mesh_bridge_data

            adsb_plugin = plugin.app.get_plugin("adsb_radar")
            if adsb_plugin and hasattr(adsb_plugin, "get_snapshot"):
                try:
                    adsb_data = adsb_plugin.get_snapshot()
                    if adsb_data:
                        data["adsb"] = adsb_data
                except Exception:
                    log.debug("adsb_radar snapshot failed", exc_info=True)

            message = json.dumps({
                "type": "update",
                "data": data,
                "timestamp": time.time(),
            })

            # Broadcast to all clients, remove dead ones
            dead: list[aiohttp.web.WebSocketResponse] = []
            for ws in list(_ws_clients):
                try:
                    await ws.send_str(message)
                except Exception:
                    dead.append(ws)

            for ws in dead:
                _ws_clients.discard(ws)

        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("Error in metrics broadcast")
            await asyncio.sleep(1)


async def _start_broadcast_task(app: aiohttp.web.Application) -> None:
    global _broadcast_task, _ws_loop, _ws_plugin
    _ws_loop = asyncio.get_running_loop()
    _ws_plugin = app["plugin"]
    _broadcast_task = asyncio.create_task(_broadcast_metrics(app))
    # Subscribe to messaging events so inbound messages and delivery status
    # changes reach the dashboard in <100 ms instead of waiting for the
    # next metrics poll (up to 5 s).
    try:
        from reticulumpi import events as _events
        event_bus = _ws_plugin.app.event_bus
        event_bus.subscribe(_events.MESSAGE_RECEIVED, _on_message_event)
        event_bus.subscribe(_events.MESSAGE_SENT, _on_message_event)
        event_bus.subscribe(_events.MESSAGE_STATUS_CHANGED, _on_status_event)
        event_bus.subscribe(_events.ALERT_TRIGGERED, _on_alert_event)
    except Exception:
        log.exception("Failed to subscribe WS handler to messaging events")


async def _stop_broadcast_task(app: aiohttp.web.Application) -> None:
    global _broadcast_task, _ws_loop, _ws_plugin
    try:
        if _ws_plugin is not None:
            event_bus = _ws_plugin.app.event_bus
            event_bus.unsubscribe_all(_on_message_event)
            event_bus.unsubscribe_all(_on_status_event)
            event_bus.unsubscribe_all(_on_alert_event)
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
    # Close all WebSocket connections
    for ws in list(_ws_clients):
        await ws.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b"Server shutting down")
    _ws_clients.clear()


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
