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
    app.router.add_get("/api/config", handle_config)
    # Mesh awareness endpoints
    app.router.add_get("/api/mesh/nodes", handle_mesh_nodes)
    app.router.add_get("/api/mesh/telemetry", handle_mesh_telemetry)
    app.router.add_get("/api/alerts", handle_alerts)
    app.router.add_get("/api/files", handle_files)
    app.router.add_get("/api/sensors", handle_sensors)
    app.router.add_get("/api/emergency", handle_emergency)
    app.router.add_get("/api/transport", handle_transport)
    app.router.add_get("/api/connectivity", handle_connectivity)
    app.router.add_get("/api/routing", handle_routing)
    app.router.add_get("/api/path_warming", handle_path_warming)
    app.router.add_get("/api/transport_health", handle_transport_health)
    app.router.add_get("/api/reachability", handle_reachability)


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
    """
    import RNS

    plugin = _get_plugin(request)
    rns_instance = getattr(plugin.app, "reticulum", None)

    interfaces = []
    try:
        # Prefer the Reticulum stats API (works across shared instance)
        if rns_instance and hasattr(rns_instance, "get_interface_stats"):
            stats = rns_instance.get_interface_stats()
            for entry in stats.get("interfaces", []):
                itype = entry.get("type", "")
                if itype in ("LocalClientInterface", "LocalServerInterface"):
                    continue
                interfaces.append({
                    "name": entry.get("name", "?"),
                    "type": itype,
                    "online": entry.get("status", False),
                    "bitrate": entry.get("bitrate"),
                    "rxb": entry.get("rxb", 0),
                    "txb": entry.get("txb", 0),
                })
        else:
            # Fallback: direct iteration (standalone mode)
            for iface in RNS.Transport.interfaces:
                info: dict[str, Any] = {
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
    except Exception as exc:
        return _ok({"interfaces": interfaces, "error": f"Partial collection: {exc}"})

    return _ok({"interfaces": interfaces})


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
    """GET /api/mesh/nodes — known nodes from network_map plugin."""
    plugin = _get_plugin(request)
    network_map = plugin.app.get_plugin("network_map")
    if not network_map or not hasattr(network_map, "get_known_nodes"):
        return _ok({"nodes": [], "message": "network_map plugin not available"})
    return _ok({"nodes": network_map.get_known_nodes()})


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
    min_hops = int(min_hops_raw) if min_hops_raw is not None else None
    max_hops = int(max_hops_raw) if max_hops_raw is not None else None

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
        limit: Max nodes to return (default 50, 0 = all)
        search: Hex prefix filter on destination hash
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

    nodes = network_map.get_known_nodes()

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

    # Score all nodes
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

    # Build summary from ALL scored nodes (before limit)
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

    # Apply limit
    try:
        limit = int(request.query.get("limit", 50))
    except (ValueError, TypeError):
        limit = 50
    if limit > 0:
        scored = scored[:limit]

    summary["returned"] = len(scored)
    return _ok({"nodes": scored, "summary": summary})
