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

from reticulumpi.builtin_plugins.web_dashboard.broadcast_registry import (
    BroadcastRegistry,
)

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

def _diff_payload(data: dict[str, Any], prev: dict[str, Any]) -> dict[str, Any]:
    """Return only keys whose value object changed since last broadcast."""
    result = {}
    for key, value in data.items():
        if value is not prev.get(key):
            result[key] = value
    return result


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

async def websocket_metrics(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    """Handle WebSocket connections for live metrics streaming."""
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

    lora_scanner = plugin.app.get_plugin("lora_scanner")
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
                resp = _handle_ws_command(msg.data, plugin)
                if resp is not None:
                    await ws.send_str(json.dumps(resp))
    finally:
        _ws_clients.discard(ws)
        _ws_last_activity.pop(ws, None)
        log.info(
            "WebSocket disconnected (code=%s, %d remaining)",
            ws.close_code,
            len(_ws_clients),
        )

    return ws


_last_mesh_announce_ts: float = 0
_mesh_version: int = 0


def _collect_broadcast_data(
    plugin: Any,
    cycle_count: int,
    last_mesh_announce_ts: float,
    mesh_version: int,
) -> tuple[dict[str, Any], float, int]:
    """Collect all plugin data synchronously.  Runs in a thread executor.

    Uses :class:`BroadcastRegistry` to iterate plugins by tier with a
    time budget, then post-processes to preserve the frontend contract.
    """
    interval = plugin.config.get("metrics_interval", 5)
    registry = BroadcastRegistry(metrics_interval=interval)

    plugin_statuses: dict[str, Any] = {}
    for name, p in plugin.app.plugins.items():
        try:
            plugin_statuses[name] = {"active": p.get_status().get("active", False)}
        except Exception:
            plugin_statuses[name] = {"active": False}

    interfaces = _collect_interfaces(plugin.app.reticulum)

    data = registry.collect(plugin.app.plugins, cycle_count)

    data["plugins"] = plugin_statuses
    data["interfaces"] = interfaces
    probe = getattr(plugin.app, "internet_probe", None)
    data["internet"] = probe.get_status() if probe else {"online": True, "wan_ip": None, "lan_ip": None}

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
                    objects, key=lambda o: o.get("el", -999), reverse=True,
                )
            space["positions"] = {
                "fetched_at": positions.get("fetched_at"),
                "count": positions.get("count", len(objects)),
                "objects": objects[:60],
            }

    return data, last_mesh_announce_ts, mesh_version


async def _broadcast_metrics(app: aiohttp.web.Application) -> None:
    """Periodically broadcast system metrics to all connected WebSocket clients."""
    global _last_mesh_announce_ts, _mesh_version

    plugin = app["plugin"]
    interval = plugin.config.get("metrics_interval", 5)
    _cycle_count = 0
    _prev_data: dict[str, Any] = {}
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
                    )
                )
            finally:
                _collection_running.clear()

            collect_elapsed = time.monotonic() - t_collect

            diff_data = _diff_payload(data, _prev_data)
            _prev_data = data
            if not diff_data:
                continue

            message = json.dumps({
                "type": "update",
                "data": diff_data,
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
                    "WS broadcast: collect=%.0fms payload=%dB"
                    " diff=%d/%d clients=%d",
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
    global _broadcast_task, _ws_loop, _ws_plugin, _broadcast_executor, _push_sem
    _ws_loop = asyncio.get_running_loop()
    _ws_plugin = app["plugin"]
    _push_sem = asyncio.Semaphore(8)
    _broadcast_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="ws-broadcast",
    )
    _broadcast_task = asyncio.create_task(_broadcast_metrics(app))
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
            event_bus.unsubscribe_all(_on_internet_event)
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
        _broadcast_executor.shutdown(wait=True, cancel_futures=True)
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


def _handle_ws_command(raw: str, plugin: Any) -> dict | None:
    """Process a JSON command from a WebSocket client.

    Returns an optional response dict to send back to the caller.
    """
    try:
        cmd = json.loads(raw)
    except Exception:
        return
    action = cmd.get("action")
    if action == "spectrum_switch_preset":
        scanner = plugin.app.plugins.get("spectrum_scanner")
        if scanner and hasattr(scanner, "switch_preset"):
            preset_name = cmd.get("preset", "")
            try:
                result = scanner.switch_preset(preset_name)
                return {"type": "spectrum_preset_switched", **result}
            except ValueError as exc:
                return {"type": "spectrum_preset_error", "error": str(exc)}
            except Exception:
                log.debug("Spectrum preset switch failed", exc_info=True)
    elif action == "spectrum_list_presets":
        scanner = plugin.app.plugins.get("spectrum_scanner")
        if scanner and hasattr(scanner, "get_presets"):
            return {"type": "spectrum_presets", **scanner.get_presets()}
    elif action == "radio_tune":
        fm = plugin.app.plugins.get("fm_receiver")
        if fm and hasattr(fm, "tune"):
            freq_mhz = cmd.get("frequency_mhz")
            if freq_mhz is None:
                return {"type": "radio_error", "error": "frequency_mhz required"}
            try:
                result = fm.tune(int(float(freq_mhz) * 1_000_000), mode=cmd.get("mode"))
                return {"type": "radio_tuned", **result}
            except ValueError as exc:
                return {"type": "radio_error", "error": str(exc)}
    elif action == "radio_play":
        fm = plugin.app.plugins.get("fm_receiver")
        if fm and hasattr(fm, "play"):
            try:
                result = fm.play()
                return {"type": "radio_play", **result}
            except Exception as exc:
                return {"type": "radio_error", "error": str(exc)}
    elif action == "radio_stop":
        fm = plugin.app.plugins.get("fm_receiver")
        if fm and hasattr(fm, "stop_playback"):
            try:
                result = fm.stop_playback()
                return {"type": "radio_stop", **result}
            except Exception as exc:
                return {"type": "radio_error", "error": str(exc)}
    elif action == "radio_gain":
        fm = plugin.app.plugins.get("fm_receiver")
        if fm and hasattr(fm, "set_gain"):
            try:
                gain = cmd.get("gain_db")
                if gain is not None:
                    gain = float(gain)
                result = fm.set_gain(gain)
                return {"type": "radio_gain", **result}
            except (ValueError, TypeError) as exc:
                return {"type": "radio_error", "error": str(exc)}
    elif action == "radio_squelch":
        fm = plugin.app.plugins.get("fm_receiver")
        if fm and hasattr(fm, "set_squelch"):
            try:
                result = fm.set_squelch(int(cmd.get("level", 0)))
                return {"type": "radio_squelch", **result}
            except (ValueError, TypeError) as exc:
                return {"type": "radio_error", "error": str(exc)}
    elif action == "radio_volume":
        fm = plugin.app.plugins.get("fm_receiver")
        if fm and hasattr(fm, "set_volume"):
            try:
                vol = float(cmd.get("volume", 0.75))
                if not 0.0 <= vol <= 1.0:
                    return {"type": "radio_error", "error": "volume must be 0.0-1.0"}
                result = fm.set_volume(vol)
                return {"type": "radio_volume", **result}
            except (ValueError, TypeError) as exc:
                return {"type": "radio_error", "error": str(exc)}


def _on_alert_event(event_type: str, data: dict) -> None:
    """Event-bus callback for ALERT_TRIGGERED — push to WS clients."""
    if _ws_loop is None or not _ws_clients:
        return
    try:
        _ws_loop.call_soon_threadsafe(_schedule_push, "alert", data)
    except RuntimeError:
        pass


def _on_internet_event(event_type: str, data: dict) -> None:
    """Event-bus callback for INTERNET_ONLINE/OFFLINE — push to WS clients."""
    if _ws_loop is None or not _ws_clients:
        return
    payload = {
        "online": event_type == "internet.online",
        "wan_ip": data.get("wan_ip"),
        "lan_ip": data.get("lan_ip"),
    }
    try:
        _ws_loop.call_soon_threadsafe(_schedule_push, "internet_status", payload)
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

    Bounded by ``_push_sem`` so message storms can't accumulate
    unbounded tasks on the event loop.
    """
    sem = _push_sem
    if sem is not None:
        await sem.acquire()
    try:
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
                _ws_last_activity.pop(ws, None)
    finally:
        if sem is not None:
            sem.release()
