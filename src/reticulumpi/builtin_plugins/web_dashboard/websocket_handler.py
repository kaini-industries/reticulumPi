"""WebSocket handler for real-time metrics streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

import aiohttp
import aiohttp.web

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


def _collect_interfaces() -> list[dict]:
    """Collect current RNS interface data for broadcast."""
    try:
        import RNS

        interfaces = []
        for iface in RNS.Transport.interfaces:
            info: dict = {
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
            interfaces.append(info)
        return interfaces
    except Exception:
        return []


def setup_websocket_routes(app: aiohttp.web.Application) -> None:
    """Register WebSocket routes."""
    app.router.add_get("/ws/metrics", websocket_metrics)
    app.on_startup.append(_start_broadcast_task)
    app.on_shutdown.append(_stop_broadcast_task)


_ws_clients: set[aiohttp.web.WebSocketResponse] = set()
_broadcast_task: asyncio.Task | None = None


async def websocket_metrics(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    """Handle WebSocket connections for live metrics streaming."""
    plugin = request.app["plugin"]
    max_clients = plugin.config.get("max_websocket_clients", 10)

    # Authenticate via query param or expect token in first message
    token = request.query.get("token")
    if not token:
        # Check cookie as fallback
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

    ws = aiohttp.web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)
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


async def _broadcast_metrics(app: aiohttp.web.Application) -> None:
    """Periodically broadcast system metrics to all connected WebSocket clients."""
    plugin = app["plugin"]
    interval = plugin.config.get("metrics_interval", 5)

    while True:
        try:
            await asyncio.sleep(interval)

            if not _ws_clients:
                continue

            # Collect metrics
            monitor = plugin.app.get_plugin("system_monitor")
            metrics = {}
            if monitor and hasattr(monitor, "latest_metrics"):
                metrics = monitor.latest_metrics

            # Collect plugin statuses
            plugin_statuses = {}
            for name, p in plugin.app.plugins.items():
                try:
                    plugin_statuses[name] = {"active": p.get_status().get("active", False)}
                except Exception:
                    plugin_statuses[name] = {"active": False}

            # Collect interface traffic data
            interfaces = _collect_interfaces()

            # Collect mesh data (if plugins available)
            mesh_data: dict = {}
            network_map = plugin.app.get_plugin("network_map")
            if network_map and hasattr(network_map, "get_known_nodes"):
                nodes = network_map.get_known_nodes()
                mesh_data["nodes"] = nodes
                mesh_data["known_nodes"] = len(nodes)

            telemetry = plugin.app.get_plugin("mesh_telemetry")
            if telemetry and hasattr(telemetry, "get_peer_metrics"):
                peers = telemetry.get_peer_metrics()
                mesh_data["peers"] = peers
                mesh_data["peer_count"] = len(peers)

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
                sensor_data = sensor_fw.get_latest_readings()

            # Collect emergency data (if plugin available)
            emergency_data: dict = {}
            emergency = plugin.app.get_plugin("emergency_broadcast")
            if emergency and hasattr(emergency, "get_status"):
                try:
                    emergency_data = emergency.get_status()
                except Exception:
                    pass

            message = json.dumps({
                "type": "update",
                "data": {
                    "metrics": metrics,
                    "plugins": plugin_statuses,
                    "interfaces": interfaces,
                    "mesh": mesh_data,
                    "sensors": sensor_data,
                    "emergency": emergency_data,
                },
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
    global _broadcast_task
    _broadcast_task = asyncio.create_task(_broadcast_metrics(app))


async def _stop_broadcast_task(app: aiohttp.web.Application) -> None:
    global _broadcast_task
    if _broadcast_task:
        _broadcast_task.cancel()
        try:
            await _broadcast_task
        except asyncio.CancelledError:
            pass
        _broadcast_task = None
    # Close all WebSocket connections
    for ws in list(_ws_clients):
        await ws.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b"Server shutting down")
    _ws_clients.clear()
