"""JSON API route handlers."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import aiohttp.web

if TYPE_CHECKING:
    pass

SENSITIVE_KEYS = frozenset({"password", "password_hash"})


def _get_plugin_address(p) -> str | None:
    """Extract the RNS/LXMF address hash from a plugin, if it has one."""
    import RNS

    # LXMF plugins (message_echo, info_bot) store their destination here
    dest = getattr(p, "local_lxmf_destination", None)
    if dest is not None and hasattr(dest, "hash"):
        return RNS.prettyhexrep(dest.hash)

    # RNS destination plugins (heartbeat_announce, example_plugin)
    dest = getattr(p, "destination", None)
    if dest is not None and hasattr(dest, "hash"):
        return RNS.prettyhexrep(dest.hash)

    return None


def _collect_local_services(app) -> list[dict]:
    """Collect RNS/LXMF destinations from active plugins (local 0-hop services)."""
    import RNS

    services: list[dict] = []
    for name, p in app.plugins.items():
        dest = getattr(p, "local_lxmf_destination", None)
        if dest is None or not hasattr(dest, "hash"):
            dest = getattr(p, "destination", None)
        if dest is None or not hasattr(dest, "hash"):
            continue
        try:
            dest_hash = RNS.prettyhexrep(dest.hash)
        except Exception:
            continue

        # Parse app_name/aspects from dest.name ("app.aspect1.aspect2.hexhash")
        app_name, aspects = "", ""
        try:
            raw_name = getattr(dest, "name", "") or ""
            parts = raw_name.split(".") if raw_name else []
            if len(parts) >= 2:
                app_name = parts[0]
                # Last segment is identity hex hash; middle segments are aspects
                aspects = ".".join(parts[1:-1]) if len(parts) > 2 else parts[1]
        except Exception:
            pass

        services.append({
            "destination_hash": dest_hash,
            "plugin_name": getattr(p, "plugin_name", name),
            "app_name": app_name,
            "aspects": aspects,
            "is_local": True,
        })
    return services


def _build_traffic_map(plugin: Any) -> dict[str, dict]:
    """Build a host:port -> {rxb, txb} map from Reticulum interface stats.

    Interface names in shared-instance mode look like:
        TCPInterface[TCP Client label/host:port]
    We parse the host:port from after the '/' and strip the trailing ']'.
    """
    traffic_map: dict[str, dict] = {}
    try:
        rns_instance = getattr(plugin.app, "reticulum", None)
        if not rns_instance or not hasattr(rns_instance, "get_interface_stats"):
            return traffic_map
        stats = rns_instance.get_interface_stats()
        for entry in stats.get("interfaces", []):
            if "TCPClient" not in entry.get("type", ""):
                continue
            traffic = {"rxb": entry.get("rxb", 0), "txb": entry.get("txb", 0)}
            name = entry.get("name", "")
            if "/" in name:
                # "TCPInterface[TCP Client label/host:port]" -> "host:port"
                addr = name.split("/", 1)[1].rstrip("]")
                traffic_map[addr] = traffic
            ip = entry.get("target_ip")
            port = entry.get("target_port")
            if ip and port:
                traffic_map[f"{ip}:{port}"] = traffic
    except Exception:
        pass
    return traffic_map


def setup_api_routes(app: aiohttp.web.Application) -> None:
    """Register all API routes on the aiohttp application."""
    app.router.add_post("/api/auth/login", handle_login)
    app.router.add_post("/auth/login", handle_form_login)
    app.router.add_post("/api/auth/logout", handle_logout)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/node", handle_node)
    app.router.add_get("/api/metrics", handle_metrics)
    app.router.add_get("/api/plugins", handle_plugins)
    app.router.add_get("/api/plugins/{name}", handle_plugin_detail)
    app.router.add_get("/api/interfaces", handle_interfaces)
    app.router.add_get("/api/interfaces/config", handle_interfaces_config)
    app.router.add_post("/api/interfaces/{name:.+}/toggle", handle_interface_toggle)
    app.router.add_post("/api/interfaces/add", handle_interface_add)
    app.router.add_post("/api/services/restart", handle_services_restart)
    app.router.add_get("/api/config", handle_config)
    # Mesh awareness endpoints
    app.router.add_get("/api/mesh/nodes", handle_mesh_nodes)
    app.router.add_get("/api/mesh/summary", handle_mesh_summary)
    app.router.add_get("/api/mesh/telemetry", handle_mesh_telemetry)
    app.router.add_get("/api/alerts", handle_alerts)
    app.router.add_get("/api/files", handle_files)
    app.router.add_get("/api/sensors", handle_sensors)
    app.router.add_get("/api/sensors/history", handle_sensor_history)
    app.router.add_get("/api/emergency", handle_emergency)
    app.router.add_get("/api/transport", handle_transport)
    app.router.add_get("/api/connectivity", handle_connectivity)
    app.router.add_get("/api/routing", handle_routing)
    app.router.add_get("/api/path_warming", handle_path_warming)
    app.router.add_get("/api/transport_health", handle_transport_health)
    app.router.add_get("/api/lora", handle_lora_diagnostics)
    app.router.add_get("/api/reachability", handle_reachability)
    app.router.add_get("/api/paths", handle_paths)
    app.router.add_get("/api/nomadnet/auth", handle_nomadnet_auth)
    app.router.add_post("/api/nomadnet/auth/add", handle_nomadnet_auth_add)
    app.router.add_post("/api/nomadnet/auth/remove", handle_nomadnet_auth_remove)
    # Meshtastic gateway
    app.router.add_get("/api/meshtastic/status", handle_meshtastic_status)
    app.router.add_get("/api/meshtastic/nodes", handle_meshtastic_nodes)
    # Messaging hub
    app.router.add_get("/api/messages", handle_messages)
    app.router.add_post("/api/messages/send", handle_send_message)
    app.router.add_get("/api/messages/transports", handle_transports)
    app.router.add_get("/api/messages/contacts", handle_contacts)
    app.router.add_get("/api/messages/stats", handle_message_stats)


def _ok(data: Any) -> aiohttp.web.Response:
    """Return a success JSON response."""
    import json

    body = json.dumps({"ok": True, "data": data, "timestamp": time.time()})
    return aiohttp.web.Response(text=body, content_type="application/json")


def _error(message: str, status: int = 400) -> aiohttp.web.Response:
    """Return an error JSON response."""
    import json

    body = json.dumps({"ok": False, "error": message, "code": status})
    return aiohttp.web.Response(
        text=body, status=status, content_type="application/json"
    )


def _get_plugin(request: aiohttp.web.Request):
    """Get the WebDashboardPlugin from the request's app."""
    return request.app["plugin"]


# --- Auth endpoints ---


async def handle_login(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """POST /api/auth/login — authenticate and receive session token."""
    plugin = _get_plugin(request)
    auth = plugin._auth
    remote_ip = request.remote or "unknown"

    if auth.is_rate_limited(remote_ip):
        retry_after = auth.get_retry_after(remote_ip)
        resp = _error("Too many login attempts", 429)
        resp.headers["Retry-After"] = str(retry_after)
        return resp

    try:
        body = await request.json()
        password = body.get("password", "")
    except Exception:
        return _error("Invalid request body", 400)

    if not password:
        return _error("Password is required", 400)
    if len(password) > 256:
        return _error("Password too long", 400)

    token = auth.login(password, remote_ip)
    if not token:
        return _error("Invalid password", 401)

    resp = _ok({"token": token})

    # Set session cookie
    ssl_config = plugin.config.get("ssl", {})
    secure = ssl_config.get("enabled", False)
    resp.set_cookie(
        "session",
        token,
        httponly=True,
        secure=secure,
        samesite="Lax",
        max_age=int(auth.session_timeout),
        path="/",
    )
    return resp


async def handle_form_login(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """POST /auth/login — form-based login that redirects with Set-Cookie.

    Browsers reliably store cookies from form POST redirects, unlike fetch().
    """
    plugin = _get_plugin(request)
    auth = plugin._auth
    remote_ip = request.remote or "unknown"

    if auth.is_rate_limited(remote_ip):
        raise aiohttp.web.HTTPFound("/login.html?error=rate_limited")

    try:
        data = await request.post()
        password = data.get("password", "")
    except Exception:
        raise aiohttp.web.HTTPFound("/login.html?error=invalid")

    if not password:
        raise aiohttp.web.HTTPFound("/login.html?error=empty")

    token = auth.login(password, remote_ip)
    if not token:
        raise aiohttp.web.HTTPFound("/login.html?error=invalid")

    ssl_config = plugin.config.get("ssl", {})
    secure = ssl_config.get("enabled", False)

    resp = aiohttp.web.HTTPFound("/")
    resp.set_cookie(
        "session",
        token,
        httponly=True,
        secure=secure,
        samesite="Lax",
        max_age=int(auth.session_timeout),
        path="/",
    )
    raise resp


async def handle_logout(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """POST /api/auth/logout — invalidate current session."""
    plugin = _get_plugin(request)
    # Extract token from Authorization header or session cookie
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        token = request.cookies.get("session", "")
    if token:
        plugin._auth.logout(token)

    resp = _ok({"message": "Logged out"})
    resp.del_cookie("session", path="/")
    return resp


# --- Data endpoints ---


async def handle_status(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/status — full app status."""
    plugin = _get_plugin(request)
    status = plugin.app.get_status()
    return _ok(status)


async def handle_node(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/node — node identity and info."""
    import RNS

    plugin = _get_plugin(request)
    app = plugin.app

    identity_hash = ""
    if app.identity:
        identity_hash = RNS.prettyhexrep(app.identity.hash)

    data = {
        "node_name": app.node_name,
        "identity_hash": identity_hash,
        "version": app._get_version(),
        "uptime": time.time() - plugin._start_time if plugin._active else 0,
    }
    return _ok(data)


async def handle_metrics(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/metrics — latest system_monitor metrics."""
    plugin = _get_plugin(request)
    monitor = plugin.app.get_plugin("system_monitor")

    if monitor and hasattr(monitor, "latest_metrics"):
        return _ok(monitor.latest_metrics)

    return _ok({"message": "system_monitor plugin not available", "metrics": {}})


async def handle_plugins(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/plugins — all plugins with statuses."""
    plugin = _get_plugin(request)
    app = plugin.app

    plugins_data = {}
    for name, p in app.plugins.items():
        try:
            status = p.get_status()
        except Exception:
            status = {"error": "status collection failed"}
        plugins_data[name] = {
            "name": name,
            "version": p.plugin_version,
            "description": p.plugin_description,
            "status": status,
            "address": _get_plugin_address(p),
        }

    failed = [
        {"name": name, "error": reason} for name, reason in app._failed_plugins
    ]

    return _ok({"plugins": plugins_data, "failed_plugins": failed})


async def handle_plugin_detail(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/plugins/{name} — single plugin detail."""
    plugin = _get_plugin(request)
    name = request.match_info["name"]
    p = plugin.app.get_plugin(name)

    if not p:
        return _error(f"Plugin '{name}' not found", 404)

    try:
        status = p.get_status()
    except Exception:
        status = {"error": "status collection failed"}

    return _ok({
        "name": name,
        "version": p.plugin_version,
        "description": p.plugin_description,
        "status": status,
        "address": _get_plugin_address(p),
    })


async def handle_interfaces(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/interfaces — active RNS network interfaces.

    In shared-instance mode, queries rnsd for the full interface list
    (TCP, I2P, LoRa, etc.) via ``Reticulum.get_interface_stats()``.
    For RNode interfaces, radio config and runtime metrics are included.
    """
    from reticulumpi.builtin_plugins.web_dashboard.websocket_handler import (
        _collect_interfaces,
    )

    plugin = _get_plugin(request)
    rns_instance = getattr(plugin.app, "reticulum", None)

    try:
        interfaces = _collect_interfaces(rns_instance)
    except Exception as exc:
        return _ok({"interfaces": [], "error": f"Partial collection: {exc}"})

    return _ok({"interfaces": interfaces})


def _rns_config_path(plugin) -> str:
    """Resolve the path to the Reticulum config file."""
    import os

    config_dir = getattr(plugin.app, "_reticulum_config_dir", None)
    if not config_dir:
        config_dir = os.path.expanduser("~/.reticulum")
    return os.path.join(config_dir, "config")


async def handle_interfaces_config(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/interfaces/config — all interfaces from the Reticulum config file."""
    from reticulumpi.rns_config import parse_rns_config

    plugin = _get_plugin(request)
    path = _rns_config_path(plugin)
    try:
        _, interfaces = parse_rns_config(path)
    except FileNotFoundError:
        return _error(f"Reticulum config not found: {path}", 404)
    except Exception as exc:
        return _error(f"Failed to parse config: {exc}", 500)

    return _ok({
        "interfaces": [
            {
                "name": e.name,
                "type": e.iface_type,
                "enabled": e.enabled,
                "properties": {
                    k: v for k, v in e.properties.items()
                    if k not in ("type", "enabled", "password")
                },
            }
            for e in interfaces
        ],
        "config_path": path,
    })


async def handle_interface_toggle(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/interfaces/{name}/toggle — toggle enabled yes/no in config."""
    from reticulumpi.rns_config import (
        parse_rns_config,
        set_interface_enabled,
        write_rns_config,
    )

    plugin = _get_plugin(request)
    name = request.match_info["name"]
    path = _rns_config_path(plugin)

    try:
        lines, interfaces = parse_rns_config(path)
    except Exception as exc:
        return _error(f"Failed to parse config: {exc}", 500)

    entry = next((e for e in interfaces if e.name == name), None)
    if entry is None:
        return _error(f"Interface '{name}' not found in config", 404)

    new_enabled = not entry.enabled
    try:
        new_lines = set_interface_enabled(lines, entry, new_enabled)
        write_rns_config(path, new_lines)
    except Exception as exc:
        return _error(f"Failed to write config: {exc}", 500)

    return _ok({
        "name": name,
        "enabled": new_enabled,
        "restart_required": True,
    })


# ── Interface config validation ──────────────────────────────────────
_INTERFACE_SCHEMAS: dict[str, dict] = {
    "TCPClientInterface": {
        "required": {"target_host": str, "target_port": int},
        "optional": {"kiss_framing": bool, "connect_timeout": int,
                      "max_reconnect_tries": int},
    },
    "TCPServerInterface": {
        "required": {},
        "optional": {"listen_ip": str, "listen_port": int,
                      "kiss_framing": bool},
    },
    "RNodeInterface": {
        "required": {"port": str, "frequency": int, "bandwidth": int,
                      "txpower": int, "spreadingfactor": int, "codingrate": int},
        "optional": {"id_callsign": str, "id_interval": int,
                      "announce_cap": float, "airtime_limit_short": float,
                      "airtime_limit_long": float},
    },
    "UDPInterface": {
        "required": {},
        "optional": {"listen_ip": str, "listen_port": int,
                      "forward_ip": str, "forward_port": int},
    },
    "SerialInterface": {
        "required": {"port": str},
        "optional": {"speed": int, "databits": int, "parity": str,
                      "stopbits": int},
    },
    "KISSInterface": {
        "required": {"port": str},
        "optional": {"speed": int, "databits": int, "parity": str,
                      "stopbits": int, "preamble": int, "txtail": int,
                      "persistence": int, "slottime": int},
    },
    "AutoInterface": {
        "required": {},
        "optional": {"group_id": str, "discovery_scope": str,
                      "discovery_port": int, "data_port": int},
    },
    "I2PInterface": {
        "required": {},
        "optional": {"connectable": bool, "peers": str},
    },
}

# RNode-specific value ranges
_RNODE_RANGES = {
    "frequency": (100_000_000, 1_000_000_000),  # 100 MHz – 1 GHz
    "bandwidth": (7800, 500_000),                 # 7.8 kHz – 500 kHz
    "txpower": (0, 22),
    "spreadingfactor": (7, 12),
    "codingrate": (5, 8),
}


def _validate_interface_config(iface_type: str, properties: dict) -> str | None:
    """Return an error message if the interface config is invalid, or None."""
    schema = _INTERFACE_SCHEMAS.get(iface_type)
    if schema is None:
        known = ", ".join(sorted(_INTERFACE_SCHEMAS))
        return f"Unknown interface type '{iface_type}'. Valid types: {known}"

    # Check required properties
    for key, expected_type in schema.get("required", {}).items():
        if key not in properties:
            return f"Missing required property '{key}' for {iface_type}"
        if not _check_type(properties[key], expected_type):
            return f"Property '{key}' must be {expected_type.__name__}, got '{properties[key]}'"

    # Type-check optional properties if present
    all_props = {**schema.get("required", {}), **schema.get("optional", {})}
    for key, val in properties.items():
        if key.lower() in ("type", "enabled", "mode"):
            continue
        expected = all_props.get(key)
        if expected and not _check_type(val, expected):
            return f"Property '{key}' must be {expected.__name__}, got '{val}'"

    # RNode range validation
    if iface_type == "RNodeInterface":
        for key, (lo, hi) in _RNODE_RANGES.items():
            if key in properties:
                try:
                    v = int(properties[key])
                    if v < lo or v > hi:
                        return f"Property '{key}' must be between {lo} and {hi}, got {v}"
                except (ValueError, TypeError):
                    pass  # already caught by type check

    return None


def _check_type(value: str | int | float | bool, expected: type) -> bool:
    """Check if a config value can be interpreted as the expected type."""
    if expected is str:
        return True
    if expected is bool:
        return str(value).lower() in ("true", "false", "yes", "no", "1", "0", "on", "off")
    if expected is int:
        try:
            int(str(value))
            return True
        except (ValueError, TypeError):
            return False
    if expected is float:
        try:
            float(str(value))
            return True
        except (ValueError, TypeError):
            return False
    return True


async def handle_interface_add(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/interfaces/add — add a new interface section to config.

    Body: ``{"name": "...", "type": "...", "properties": {...}}``
    """
    from reticulumpi.rns_config import (
        add_interface_section,
        parse_rns_config,
        write_rns_config,
    )

    plugin = _get_plugin(request)
    path = _rns_config_path(plugin)

    try:
        body = await request.json()
    except Exception:
        return _error("Invalid JSON body", 400)

    iface_name = body.get("name", "").strip()
    iface_type = body.get("type", "").strip()
    properties = body.get("properties", {})

    if not iface_name:
        return _error("name field is required", 400)
    if not iface_type:
        return _error("type field is required", 400)
    if len(iface_name) > 100:
        return _error("name too long", 400)

    # Validate interface type and properties before writing
    validation_err = _validate_interface_config(iface_type, properties)
    if validation_err:
        return _error(validation_err, 400)

    try:
        lines, interfaces = parse_rns_config(path)
    except Exception as exc:
        return _error(f"Failed to parse config: {exc}", 500)

    if any(e.name == iface_name for e in interfaces):
        return _error(f"Interface '{iface_name}' already exists", 409)

    try:
        new_lines = add_interface_section(lines, iface_name, iface_type, properties)
        write_rns_config(path, new_lines)
    except Exception as exc:
        return _error(f"Failed to write config: {exc}", 500)

    return _ok({
        "name": iface_name,
        "type": iface_type,
        "restart_required": True,
    })


async def handle_services_restart(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/services/restart — restart rnsd + reticulumpi."""
    import asyncio

    async def _do_restart() -> None:
        await asyncio.sleep(2)  # let HTTP response flush
        proc = await asyncio.create_subprocess_exec(
            "sudo", "systemctl", "restart", "rnsd",
        )
        await proc.wait()
        await asyncio.sleep(3)  # rnsd startup time
        await asyncio.create_subprocess_exec(
            "sudo", "systemctl", "restart", "reticulumpi",
        )
        # reticulumpi kills us — no await needed

    asyncio.create_task(_do_restart())
    return _ok({"message": "Restarting services..."})


async def handle_config(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/config — read-only, sanitized config view."""
    plugin = _get_plugin(request)
    config = plugin.app.config

    # Build sanitized plugin config
    plugins_config = {}
    for name, cfg in config.plugins.items():
        plugins_config[name] = {
            k: v for k, v in cfg.items() if k not in SENSITIVE_KEYS
        }

    data = {
        "node_name": config.node_name,
        "log_level": config.log_level,
        "use_shared_instance": config.use_shared_instance,
        "plugin_paths": config.plugin_paths,
        "plugins": plugins_config,
    }
    return _ok(data)


# --- Mesh awareness endpoints ---


async def handle_mesh_nodes(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/mesh/nodes — known nodes from network_map plugin.

    Supports server-side pagination, sorting, and filtering:
        page: Page number (default 1)
        per_page: Items per page (default 25, max 200, 0 = all)
        sort: Sort field (last_seen, hops, announce_count, app_name, first_seen)
        order: asc or desc (default desc)
        search: Text search on hash, name, or app
        app: Filter by app_name
    """
    plugin = _get_plugin(request)
    network_map = plugin.app.get_plugin("network_map")
    local_services = _collect_local_services(plugin.app)

    if not network_map or not hasattr(network_map, "get_known_nodes"):
        return _ok({"nodes": [], "total": 0, "page": 1, "pages": 1,
                     "local_services": local_services,
                     "message": "network_map plugin not available"})

    # Check if paginated method is available (new) — fall back to full list
    if not hasattr(network_map, "get_known_nodes_paginated"):
        return _ok({"nodes": network_map.get_known_nodes(),
                     "local_services": local_services})

    try:
        page = max(1, int(request.query.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = int(request.query.get("per_page", 25))
    except (ValueError, TypeError):
        per_page = 25
    # Clamp: 0 means "all" (legacy compat), otherwise max 200
    if per_page < 0:
        per_page = 25
    elif per_page > 200 and per_page != 0:
        per_page = 200

    # If per_page=0, return full list for legacy callers
    if per_page == 0:
        return _ok({"nodes": network_map.get_known_nodes(),
                     "local_services": local_services})

    sort = request.query.get("sort", "last_seen")
    order = request.query.get("order", "desc")
    search = request.query.get("search", "")
    app_filter = request.query.get("app", "")
    view = request.query.get("view", "")

    result = network_map.get_known_nodes_paginated(
        page=page, per_page=per_page, sort=sort, order=order,
        search=search, app_filter=app_filter, view=view,
    )
    result["local_services"] = local_services
    return _ok(result)


async def handle_mesh_summary(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/mesh/summary — aggregate mesh stats for the summary strip."""
    plugin = _get_plugin(request)
    network_map = plugin.app.get_plugin("network_map")
    if not network_map or not hasattr(network_map, "get_mesh_summary"):
        return _ok({"message": "network_map plugin not available",
                     "total_nodes": 0, "app_breakdown": {},
                     "hop_distribution": {}, "activity_stats": {},
                     "growth": {}, "nearby": 0})
    return _ok(network_map.get_mesh_summary())


async def handle_mesh_telemetry(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/mesh/telemetry — peer metrics from mesh_telemetry plugin."""
    plugin = _get_plugin(request)
    telemetry = plugin.app.get_plugin("mesh_telemetry")
    if not telemetry or not hasattr(telemetry, "get_peer_metrics"):
        return _ok({"peers": [], "message": "mesh_telemetry plugin not available"})
    return _ok({"peers": telemetry.get_peer_metrics()})


async def handle_alerts(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/alerts — alert system status."""
    plugin = _get_plugin(request)
    alert_sys = plugin.app.get_plugin("alert_system")
    if not alert_sys:
        return _ok({"status": None, "message": "alert_system plugin not available"})
    try:
        status = alert_sys.get_status()
    except Exception:
        status = {"error": "status collection failed"}
    return _ok(status)


async def handle_files(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/files — shared files from file_transfer plugin."""
    plugin = _get_plugin(request)
    ft = plugin.app.get_plugin("file_transfer")
    if not ft or not hasattr(ft, "get_shared_files"):
        return _ok({"files": [], "message": "file_transfer plugin not available"})
    return _ok({"files": ft.get_shared_files()})


async def handle_sensors(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/sensors — latest sensor readings from sensor_framework plugin."""
    plugin = _get_plugin(request)
    sf = plugin.app.get_plugin("sensor_framework")
    if not sf or not hasattr(sf, "get_latest_readings"):
        return _ok({"sensors": {}, "message": "sensor_framework plugin not available"})
    return _ok({"sensors": sf.get_latest_readings()})


async def handle_sensor_history(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/sensors/history — recent readings for a sensor.

    Query params: sensor (required), limit (default 60).
    """
    plugin = _get_plugin(request)
    sf = plugin.app.get_plugin("sensor_framework")
    if not sf or not hasattr(sf, "get_sensor_history"):
        return _ok({"history": [], "message": "sensor_framework plugin not available"})

    sensor_name = request.query.get("sensor", "")
    if not sensor_name:
        return _error("sensor query param is required", 400)

    try:
        limit = min(int(request.query.get("limit", "60")), 500)
    except (ValueError, TypeError):
        limit = 60

    history = sf.get_sensor_history(sensor_name, limit=limit)
    return _ok({"sensor": sensor_name, "history": history})


async def handle_emergency(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/emergency — recent emergency broadcast messages."""
    plugin = _get_plugin(request)
    eb = plugin.app.get_plugin("emergency_broadcast")
    if not eb or not hasattr(eb, "get_messages"):
        return _ok({"messages": [], "message": "emergency_broadcast plugin not available"})
    return _ok({"messages": eb.get_messages(), "status": eb.get_status()})


async def handle_transport(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/transport — transport hub health and fallback status."""
    plugin = _get_plugin(request)
    mon = plugin.app.get_plugin("transport_monitor")
    if not mon or not hasattr(mon, "get_hub_health"):
        return _ok({"primaries": [], "fallback_active": False, "message": "transport_monitor plugin not available"})

    data = mon.get_hub_health()

    # Enrich hub entries with traffic stats from interface stats
    traffic_map: dict[str, dict] = _build_traffic_map(plugin)

    # Also enrich primaries from interface stats for the "Interfaces" section
    # data which is already collected

    def _enrich(hub: dict) -> None:
        host = hub.get("target_host", "")
        port = hub.get("target_port", 0)
        key = f"{host}:{port}"
        t = traffic_map.get(key)
        if t:
            hub["rxb"] = t["rxb"]
            hub["txb"] = t["txb"]

    for h in data.get("primaries", []):
        _enrich(h)
    for h in data.get("active_fallbacks", []):
        _enrich(h)
    for h in data.get("auto_discovery", {}).get("connected", []):
        _enrich(h)

    return _ok(data)


async def handle_connectivity(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/connectivity — connectivity diagnostics and health."""
    plugin = _get_plugin(request)
    conn_mon = plugin.app.get_plugin("connectivity_monitor")
    if not conn_mon or not hasattr(conn_mon, "get_health"):
        return _ok({"issues": [], "message": "connectivity_monitor plugin not available"})
    return _ok(conn_mon.get_health())


async def handle_routing(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/routing — routing table with pagination, filtering, and sorting.

    Query parameters:
        page: Page number (default 1)
        per_page: Items per page (default 100, max 500, 0=summary only)
        sort: Sort field — hops, timestamp, expires, hash, interface (default hops)
        order: Sort order — asc or desc (default asc)
        interface: Substring filter on interface name
        min_hops: Minimum hop count filter
        max_hops: Maximum hop count filter
        search: Hex prefix filter on destination hash
    """
    plugin = _get_plugin(request)
    conn_mon = plugin.app.get_plugin("connectivity_monitor")
    if not conn_mon or not hasattr(conn_mon, "get_routing_data"):
        return _ok({
            "summary": {},
            "paths": [],
            "total_paths": 0,
            "page": 1,
            "per_page": 0,
            "pages": 0,
            "rate_table": [],
            "blackholed": {},
            "message": "connectivity_monitor plugin not available",
        })

    # Parse query parameters
    def _int_param(name: str, default: int) -> int:
        try:
            return int(request.query.get(name, default))
        except (ValueError, TypeError):
            return default

    page = _int_param("page", 1)
    per_page = _int_param("per_page", 100)
    sort = request.query.get("sort", "hops")
    order = request.query.get("order", "asc")
    iface_filter = request.query.get("interface", "")
    search = request.query.get("search", "")

    min_hops_raw = request.query.get("min_hops")
    max_hops_raw = request.query.get("max_hops")
    try:
        min_hops = int(min_hops_raw) if min_hops_raw is not None else None
        max_hops = int(max_hops_raw) if max_hops_raw is not None else None
    except (ValueError, TypeError):
        min_hops = None
        max_hops = None

    data = conn_mon.get_routing_data(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        iface_filter=iface_filter,
        min_hops=min_hops,
        max_hops=max_hops,
        search=search,
    )
    return _ok(data)


async def handle_path_warming(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/path_warming — path warmer stats."""
    plugin = _get_plugin(request)
    warmer = plugin.app.get_plugin("path_warmer")
    if not warmer or not hasattr(warmer, "get_warming_stats"):
        return _ok({"message": "path_warmer plugin not available"})
    return _ok(warmer.get_warming_stats())


async def handle_lora_diagnostics(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/lora — LoRa diagnostics (traffic, monitored peers, beacon status)."""
    plugin = _get_plugin(request)
    lora = plugin.app.get_plugin("lora_diagnostics")
    if not lora or not hasattr(lora, "get_diagnostics"):
        return _ok({"message": "lora_diagnostics plugin not available"})
    return _ok(lora.get_diagnostics())


async def handle_transport_health(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/transport_health — transport node health data."""
    plugin = _get_plugin(request)
    th = plugin.app.get_plugin("transport_health")
    if not th or not hasattr(th, "get_transport_nodes"):
        return _ok({"nodes": [], "summary": {}, "message": "transport_health plugin not available"})
    return _ok({
        "nodes": th.get_transport_nodes(),
        "summary": th.get_transport_summary(),
    })


async def handle_reachability(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/reachability — scored node reachability.

    Query parameters:
        page: Page number (default 1)
        per_page: Items per page (default 50, 0 = all for legacy callers)
        limit: Legacy alias for per_page (default 50, 0 = all)
        search: Text filter on hash, name, or app
        hashes: Comma-separated list of specific hashes to score (efficient)
    """
    from reticulumpi.reachability import score_all_nodes

    plugin = _get_plugin(request)

    # Gather data from plugins
    network_map = plugin.app.get_plugin("network_map")
    if not network_map or not hasattr(network_map, "get_known_nodes"):
        return _ok({
            "nodes": [],
            "summary": {"total_scored": 0},
            "message": "network_map plugin not available",
        })

    # Path table from connectivity_monitor
    conn_mon = plugin.app.get_plugin("connectivity_monitor")
    path_table: list = []
    if conn_mon and hasattr(conn_mon, "get_routing_data"):
        routing = conn_mon.get_routing_data(per_page=500)
        path_table = routing.get("paths", [])

    # Transport node health
    th = plugin.app.get_plugin("transport_health")
    transport_nodes: list = []
    if th and hasattr(th, "get_transport_nodes"):
        transport_nodes = th.get_transport_nodes()

    # Check if caller requested specific hashes (efficient path)
    specific_hashes = request.query.get("hashes", "")
    if specific_hashes:
        # Normalize: strip <> and lowercase for comparison
        hash_set = set(
            h.strip().lower().strip("<>")
            for h in specific_hashes.split(",") if h.strip()
        )
        all_nodes = network_map.get_known_nodes()
        nodes = [
            n for n in all_nodes
            if n.get("destination_hash", "").lower().strip("<>") in hash_set
        ]
        scored = score_all_nodes(nodes, path_table, transport_nodes)
        return _ok({"nodes": scored, "summary": {"total_scored": len(scored), "returned": len(scored)}})

    # Full scoring with pagination
    nodes = network_map.get_known_nodes()
    scored = score_all_nodes(nodes, path_table, transport_nodes)

    # Apply search filter
    search = request.query.get("search", "").lower()
    if search:
        scored = [
            n for n in scored
            if search in n.get("destination_hash", "").lower()
            or search in (n.get("app_data") or "").lower()
            or search in (n.get("app_name") or "").lower()
        ]

    # Build summary from ALL scored nodes (before pagination)
    total = len(scored)
    all_scores = [n["score"] for n in scored]
    label_counts = {"high": 0, "good": 0, "fair": 0, "low": 0, "unlikely": 0}
    for n in scored:
        key = n.get("label", "unlikely").lower()
        if key in label_counts:
            label_counts[key] += 1

    summary = {
        "total_scored": total,
        "average_score": sum(all_scores) / len(all_scores) if all_scores else 0,
        **label_counts,
    }

    # Support both legacy 'limit' and new 'per_page' + 'page' params
    try:
        per_page = int(request.query.get("per_page", request.query.get("limit", 50)))
    except (ValueError, TypeError):
        per_page = 50
    try:
        page = max(1, int(request.query.get("page", 1)))
    except (ValueError, TypeError):
        page = 1

    if per_page > 0:
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)
        offset = (per_page * (page - 1))
        scored = scored[offset:offset + per_page]
    else:
        pages = 1

    summary["returned"] = len(scored)
    summary["page"] = page
    summary["pages"] = pages
    return _ok({"nodes": scored, "summary": summary})


# --- NomadNet auth endpoints ---


async def handle_nomadnet_auth(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/nomadnet/auth — NomadNet page access control status."""
    plugin = _get_plugin(request)
    nn = plugin.app.get_plugin("nomadnet_server")
    if not nn or not hasattr(nn, "get_allowed_identities"):
        return _ok({"message": "nomadnet_server plugin not available"})
    protected = nn._get_protected_pages() if hasattr(nn, "_get_protected_pages") else []
    return _ok({
        "allowed_identities": nn.get_allowed_identities(),
        "protected_pages": protected,
    })


async def handle_nomadnet_auth_add(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/nomadnet/auth/add — add an identity to the allow list."""
    plugin = _get_plugin(request)
    nn = plugin.app.get_plugin("nomadnet_server")
    if not nn or not hasattr(nn, "add_allowed_identity"):
        return _error("nomadnet_server plugin not available", 503)
    try:
        body = await request.json()
        identity = body.get("identity", "")
    except Exception:
        return _error("Invalid request body", 400)
    if not identity:
        return _error("identity field is required", 400)
    if len(identity) > 128:
        return _error("identity too long", 400)
    try:
        added = nn.add_allowed_identity(identity)
        return _ok({"added": added, "identity": identity.strip().lower()})
    except ValueError as exc:
        return _error(str(exc), 400)


async def handle_nomadnet_auth_remove(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/nomadnet/auth/remove — remove an identity from the allow list."""
    plugin = _get_plugin(request)
    nn = plugin.app.get_plugin("nomadnet_server")
    if not nn or not hasattr(nn, "remove_allowed_identity"):
        return _error("nomadnet_server plugin not available", 503)
    try:
        body = await request.json()
        identity = body.get("identity", "")
    except Exception:
        return _error("Invalid request body", 400)
    if not identity:
        return _error("identity field is required", 400)
    if len(identity) > 128:
        return _error("identity too long", 400)
    removed = nn.remove_allowed_identity(identity)
    return _ok({"removed": removed, "identity": identity.strip().lower()})


# ── Meshtastic gateway ────────────────────────────────────────────────


async def handle_meshtastic_status(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/meshtastic/status — Meshtastic gateway status and message stats."""
    plugin = _get_plugin(request)
    gw = plugin.app.get_plugin("meshtastic_gateway")
    if not gw or not hasattr(gw, "get_status"):
        return _ok({"available": False, "message": "meshtastic_gateway plugin not enabled"})
    return _ok(gw.get_status())


async def handle_meshtastic_nodes(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/meshtastic/nodes — Known Meshtastic mesh nodes."""
    plugin = _get_plugin(request)
    gw = plugin.app.get_plugin("meshtastic_gateway")
    if not gw or not hasattr(gw, "get_meshtastic_nodes"):
        return _ok({"nodes": [], "message": "meshtastic_gateway plugin not enabled"})
    return _ok({"nodes": gw.get_meshtastic_nodes()})


# ── Messaging Hub ────────────────────────────────────────────────────


async def handle_messages(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/messages — Paginated message history.

    Query params: limit, offset, transport, direction, since.
    """
    plugin = _get_plugin(request)
    hub = plugin.app.get_plugin("messaging_hub")
    if not hub or not hasattr(hub, "get_messages"):
        return _ok({"messages": [], "message": "messaging_hub not enabled"})

    try:
        limit = min(int(request.query.get("limit", "50")), 200)
        offset = max(int(request.query.get("offset", "0")), 0)
    except (ValueError, TypeError):
        return _error("limit and offset must be integers", 400)

    transport = request.query.get("transport") or None
    direction = request.query.get("direction") or None
    since_str = request.query.get("since")
    try:
        since = float(since_str) if since_str else None
    except (ValueError, TypeError):
        return _error("since must be a numeric timestamp", 400)

    messages = hub.get_messages(
        limit=limit, offset=offset, transport=transport,
        direction=direction, since=since,
    )
    return _ok({"messages": messages})


async def handle_send_message(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/messages/send — Send a message via a transport.

    Body: ``{"transport": "lxmf"|"meshtastic", "text": "...", "destination": "..."}``
    """
    plugin = _get_plugin(request)
    hub = plugin.app.get_plugin("messaging_hub")
    if not hub or not hasattr(hub, "send_message"):
        return _error("messaging_hub not enabled", 503)

    try:
        body = await request.json()
    except Exception:
        return _error("Invalid JSON body", 400)

    transport = body.get("transport", "")
    text = body.get("text", "").strip()
    destination = body.get("destination", "").strip()

    if not transport:
        return _error("transport field is required", 400)
    if not text:
        return _error("text field is required", 400)
    if len(text) > 5000:
        return _error("text exceeds maximum length (5000 chars)", 400)
    if not destination:
        return _error("destination field is required", 400)

    result = hub.send_message(transport, text, destination)
    if result.get("sent"):
        return _ok(result)
    return _error(result.get("reason", "Send failed"), 400)


async def handle_transports(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/messages/transports — Available transports."""
    plugin = _get_plugin(request)
    hub = plugin.app.get_plugin("messaging_hub")
    if not hub or not hasattr(hub, "get_transports"):
        return _ok({"transports": []})
    return _ok({"transports": hub.get_transports()})


async def handle_contacts(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/messages/contacts — Contacts across transports.

    Query param: transport (optional filter).
    """
    plugin = _get_plugin(request)
    hub = plugin.app.get_plugin("messaging_hub")
    if not hub or not hasattr(hub, "get_contacts"):
        return _ok({"contacts": []})
    transport = request.query.get("transport") or None
    return _ok({"contacts": hub.get_contacts(transport)})


async def handle_message_stats(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/messages/stats — Message counts by transport and direction."""
    plugin = _get_plugin(request)
    hub = plugin.app.get_plugin("messaging_hub")
    if not hub or not hasattr(hub, "get_stats"):
        return _ok({"stats": {}})
    return _ok({"stats": hub.get_stats()})


# ── Path table with real interface names ──────────────────────────────


# Cache to avoid running rnpath too often
_paths_cache: dict[str, Any] = {"data": None, "time": 0.0}
_PATHS_CACHE_TTL = 15.0  # seconds


async def handle_paths(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/paths — path table from rnsd with real interface names.

    In shared-instance mode, ``RNS.Transport.path_table`` only shows
    ``LocalClientInterface``.  This endpoint runs ``rnpath -t -j`` to get
    the real interface names (RNodeInterface, TCPInterface, etc.).

    Query parameters:
        interface: substring filter (e.g. ``RNode`` or ``TCP``)
    """
    import asyncio
    import json as _json
    import os

    now = time.time()
    if _paths_cache["data"] is not None and now - _paths_cache["time"] < _PATHS_CACHE_TTL:
        paths = _paths_cache["data"]
    else:
        # Find rnpath binary in the same venv
        import sys
        venv_bin = os.path.dirname(sys.executable)
        rnpath_bin = os.path.join(venv_bin, "rnpath")
        if not os.path.isfile(rnpath_bin):
            return _error("rnpath binary not found", 503)

        plugin = _get_plugin(request)
        config_dir = getattr(plugin.app, "_reticulum_config_dir", None)
        cmd = [rnpath_bin, "-t", "-j"]
        if config_dir:
            cmd.extend(["--config", config_dir])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            paths = _json.loads(stdout)
            _paths_cache["data"] = paths
            _paths_cache["time"] = now
        except asyncio.TimeoutError:
            return _error("rnpath timed out", 504)
        except Exception as exc:
            return _error(f"rnpath failed: {exc}", 500)

    # Optional interface filter
    iface_filter = request.query.get("interface", "")
    if iface_filter:
        paths = [p for p in paths if iface_filter in p.get("interface", "")]

    # Cross-reference with network_map for node names + extra fields
    plugin = _get_plugin(request)
    network_map = plugin.app.get_plugin("network_map")
    if network_map and hasattr(network_map, "_known_nodes"):
        with network_map._nodes_lock:
            known = network_map._known_nodes
            for p in paths:
                h = p.get("hash", "")
                try:
                    node = known.get(bytes.fromhex(h))
                    if node:
                        p["app_name"] = node.get("app_name", "")
                        p["app_data"] = node.get("app_data_str", "")
                        p["aspects"] = node.get("aspects", "")
                        p["announce_count"] = node.get("announce_count", 0)
                        p["first_seen"] = node.get("first_seen")
                except (ValueError, TypeError):
                    pass

    # Score reachability for filtered paths
    if paths:
        try:
            from reticulumpi.reachability import score_all_nodes
            conn_mon = plugin.app.get_plugin("connectivity_monitor")
            path_table: list = []
            if conn_mon and hasattr(conn_mon, "get_routing_data"):
                routing = conn_mon.get_routing_data(per_page=500)
                path_table = routing.get("paths", [])
            th = plugin.app.get_plugin("transport_health")
            transport_nodes = th.get_transport_nodes() if th and hasattr(th, "get_transport_nodes") else []
            # Build mini node list for scoring
            score_nodes = []
            for p in paths:
                score_nodes.append({
                    "destination_hash": "<" + p.get("hash", "") + ">",
                    "app_name": p.get("app_name", ""),
                    "app_data": p.get("app_data", ""),
                    "hops": p.get("hops"),
                    "last_seen": p.get("timestamp"),
                    "announce_count": p.get("announce_count", 0),
                })
            scored = score_all_nodes(score_nodes, path_table, transport_nodes)
            score_map = {s["destination_hash"]: s for s in scored}
            for p in paths:
                key = "<" + p.get("hash", "") + ">"
                s = score_map.get(key)
                if s:
                    p["score"] = s.get("score", 0)
                    p["label"] = s.get("label", "unlikely")
                    p["factors"] = s.get("factors")
        except Exception:
            pass  # Scoring is best-effort

    # Group counts by interface for summary
    by_iface: dict[str, int] = {}
    for p in (_paths_cache["data"] or []):
        iface = p.get("interface", "unknown")
        by_iface[iface] = by_iface.get(iface, 0) + 1

    return _ok({
        "paths": paths,
        "total": len(paths),
        "by_interface": by_iface,
    })
