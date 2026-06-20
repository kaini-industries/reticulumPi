"""API route handlers for mesh network, routing, transport, and reachability."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp.web

from reticulumpi.builtin_plugins.web_dashboard.api import (
    _build_traffic_map,
    _collect_local_services,
    _error,
    _get_plugin,
    _ok,
    _run_sync,
)
from reticulumpi.builtin_plugins.web_dashboard.api_cache import api_cache


@api_cache(ttl=10, stale=30, max_entries=20)
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
        return _ok(
            {
                "nodes": [],
                "total": 0,
                "page": 1,
                "pages": 1,
                "local_services": local_services,
                "message": "network_map plugin not available",
            }
        )

    # Check if paginated method is available (new) — fall back to full list
    if not hasattr(network_map, "get_known_nodes_paginated"):
        nodes = await _run_sync(network_map.get_known_nodes)
        return _ok({"nodes": nodes, "local_services": local_services})

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
        nodes = await _run_sync(network_map.get_known_nodes)
        return _ok({"nodes": nodes, "local_services": local_services})

    sort = request.query.get("sort", "last_seen")
    order = request.query.get("order", "desc")
    if order not in ("asc", "desc"):
        order = "desc"
    search = request.query.get("search", "")
    app_filter = request.query.get("app", "")
    view = request.query.get("view", "")

    result = await _run_sync(
        network_map.get_known_nodes_paginated,
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        search=search,
        app_filter=app_filter,
        view=view,
    )
    result["local_services"] = local_services
    return _ok(result)


@api_cache(ttl=30, stale=120)
async def handle_mesh_summary(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/mesh/summary — aggregate mesh stats for the summary strip."""
    plugin = _get_plugin(request)
    network_map = plugin.app.get_plugin("network_map")
    if not network_map or not hasattr(network_map, "get_mesh_summary"):
        return _ok(
            {
                "message": "network_map plugin not available",
                "total_nodes": 0,
                "app_breakdown": {},
                "hop_distribution": {},
                "activity_stats": {},
                "growth": {},
                "nearby": 0,
            }
        )
    return _ok(await _run_sync(network_map.get_mesh_summary))


@api_cache(ttl=10, stale=30)
async def handle_mesh_telemetry(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/mesh/telemetry — peer metrics from mesh_telemetry plugin."""
    plugin = _get_plugin(request)
    telemetry = plugin.app.get_plugin("mesh_telemetry")
    if not telemetry or not hasattr(telemetry, "get_peer_metrics"):
        return _ok({"peers": [], "message": "mesh_telemetry plugin not available"})
    return _ok({"peers": await _run_sync(telemetry.get_peer_metrics)})


async def handle_transport(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/transport — transport hub health and fallback status."""
    plugin = _get_plugin(request)
    mon = plugin.app.get_plugin("transport_monitor")
    if not mon or not hasattr(mon, "get_hub_health"):
        return _ok(
            {
                "primaries": [],
                "fallback_active": False,
                "message": "transport_monitor plugin not available",
            }
        )

    data = await _run_sync(mon.get_hub_health)

    # Enrich hub entries with traffic stats from interface stats
    traffic_map: dict[str, dict] = await _run_sync(_build_traffic_map, plugin)

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
    return _ok(await _run_sync(conn_mon.get_health))


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
        return _ok(
            {
                "summary": {},
                "paths": [],
                "total_paths": 0,
                "page": 1,
                "per_page": 0,
                "pages": 0,
                "rate_table": [],
                "blackholed": {},
                "message": "connectivity_monitor plugin not available",
            }
        )

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

    data = await _run_sync(
        conn_mon.get_routing_data,
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
    return _ok(await _run_sync(warmer.get_warming_stats))


async def handle_transport_health(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/transport_health — transport node health data."""
    plugin = _get_plugin(request)
    th = plugin.app.get_plugin("transport_health")
    if not th or not hasattr(th, "get_transport_nodes"):
        return _ok({"nodes": [], "summary": {}, "message": "transport_health plugin not available"})
    nodes = await _run_sync(th.get_transport_nodes)
    summary = await _run_sync(th.get_transport_summary)
    return _ok({"nodes": nodes, "summary": summary})


@api_cache(ttl=15, stale=60, max_entries=20)
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
        return _ok(
            {
                "nodes": [],
                "summary": {"total_scored": 0},
                "message": "network_map plugin not available",
            }
        )

    # Path table from connectivity_monitor
    conn_mon = plugin.app.get_plugin("connectivity_monitor")
    path_table: list = []
    if conn_mon and hasattr(conn_mon, "get_routing_data"):
        routing = await _run_sync(conn_mon.get_routing_data, per_page=500)
        path_table = routing.get("paths", [])

    # Transport node health
    th = plugin.app.get_plugin("transport_health")
    transport_nodes: list = []
    if th and hasattr(th, "get_transport_nodes"):
        transport_nodes = await _run_sync(th.get_transport_nodes)

    # Check if caller requested specific hashes (efficient path)
    specific_hashes = request.query.get("hashes", "")
    if specific_hashes:
        raw_hashes: list[bytes] = []
        for h in specific_hashes.split(","):
            h = h.strip().lower().strip("<>")
            if h:
                try:
                    raw_hashes.append(bytes.fromhex(h))
                except ValueError:
                    pass
        if hasattr(network_map, "get_nodes_by_hashes"):
            nodes = await _run_sync(
                network_map.get_nodes_by_hashes, raw_hashes
            )
        else:
            all_nodes = await _run_sync(network_map.get_known_nodes)
            hash_set = {h.hex() for h in raw_hashes}
            nodes = [
                n
                for n in all_nodes
                if n.get("destination_hash", "").lower().strip("<>")
                in hash_set
            ]
        scored = score_all_nodes(nodes, path_table, transport_nodes)
        return _ok(
            {
                "nodes": scored,
                "summary": {
                    "total_scored": len(scored),
                    "returned": len(scored),
                },
            }
        )

    # Full scoring with pagination
    nodes = await _run_sync(network_map.get_known_nodes)
    scored = score_all_nodes(nodes, path_table, transport_nodes)

    # Apply search filter
    search = request.query.get("search", "").lower()
    if search:
        scored = [
            n
            for n in scored
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
        offset = per_page * (page - 1)
        scored = scored[offset : offset + per_page]
    else:
        pages = 1

    summary["returned"] = len(scored)
    summary["page"] = page
    summary["pages"] = pages
    return _ok({"nodes": scored, "summary": summary})


# ── Path table with real interface names ──────────────────────────────

# Cache to avoid running rnpath too often
_paths_cache: dict[str, Any] = {"data": None, "time": 0.0}
_PATHS_CACHE_TTL = 15.0  # seconds
_PATHS_STALE_TTL = 120.0  # seconds — serve stale rather than spawn parallel probes
_paths_lock: Any = None  # lazily initialized asyncio.Lock (bound to running loop)


async def handle_paths(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/paths — path table from rnsd with real interface names.

    In shared-instance mode, ``RNS.Transport.path_table`` only shows
    ``LocalClientInterface``.  This endpoint runs ``rnpath -t -j`` to get
    the real interface names (RNodeInterface, TCPInterface, etc.).

    Query parameters:
        interface: substring filter (e.g. ``RNode`` or ``TCP``)
    """
    import json as _json
    import os

    global _paths_lock
    if _paths_lock is None:
        _paths_lock = asyncio.Lock()

    now = time.time()
    if _paths_cache["data"] is not None and now - _paths_cache["time"] < _PATHS_CACHE_TTL:
        paths = _paths_cache["data"]
    else:
        # Serialize probes. If another request already refreshed while we
        # waited, use the new cache value instead of launching a duplicate
        # subprocess. This prevents the rnpath-subprocess stampede that
        # starved the event loop when the RNode interface was slow.
        async with _paths_lock:
            now = time.time()
            if _paths_cache["data"] is not None and now - _paths_cache["time"] < _PATHS_CACHE_TTL:
                paths = _paths_cache["data"]
            else:
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

                proc = None
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
                except (asyncio.TimeoutError, Exception) as exc:
                    # Reap the child so it doesn't linger and pile up. Bound
                    # the wait so a stuck process can't hold _paths_lock and
                    # deadlock every subsequent /api/paths request.
                    if proc is not None and proc.returncode is None:
                        try:
                            proc.kill()
                            await asyncio.wait_for(proc.wait(), timeout=2)
                        except (OSError, ProcessLookupError):
                            pass
                    # Serve stale data (up to STALE_TTL) instead of 504 —
                    # the UI stays responsive during transient rnpath stalls.
                    if (
                        _paths_cache["data"] is not None
                        and now - _paths_cache["time"] < _PATHS_STALE_TTL
                    ):
                        paths = _paths_cache["data"]
                    elif isinstance(exc, asyncio.TimeoutError):
                        return _error("rnpath timed out", 504)
                    else:
                        return _error(f"rnpath failed: {exc}", 500)

    # Optional interface filter
    iface_filter = request.query.get("interface", "")
    if iface_filter:
        paths = [p for p in paths if iface_filter in p.get("interface", "")]

    # Cross-reference with network_map and score reachability.
    # All of this acquires threading locks, so offload to the
    # default executor to avoid blocking the event loop.
    plugin = _get_plugin(request)

    def _enrich_and_score() -> dict:
        network_map = plugin.app.get_plugin("network_map")
        if network_map and hasattr(network_map, "get_node_by_hash"):
            for p in paths:
                h = p.get("hash", "")
                try:
                    node = network_map.get_node_by_hash(bytes.fromhex(h))
                    if node:
                        p["app_name"] = node.get("app_name", "")
                        p["app_data"] = node.get("app_data_str", "")
                        p["aspects"] = node.get("aspects", "")
                        p["announce_count"] = node.get(
                            "announce_count", 0
                        )
                        p["first_seen"] = node.get("first_seen")
                except (ValueError, TypeError):
                    pass

        # Score reachability for filtered paths
        if paths:
            try:
                from reticulumpi.reachability import (
                    score_all_nodes,
                )

                conn_mon = plugin.app.get_plugin("connectivity_monitor")
                path_table: list = []
                if conn_mon and hasattr(conn_mon, "get_routing_data"):
                    routing = conn_mon.get_routing_data(per_page=500)
                    path_table = routing.get("paths", [])
                th = plugin.app.get_plugin("transport_health")
                transport_nodes = (
                    th.get_transport_nodes() if th and hasattr(th, "get_transport_nodes") else []
                )
                # Build mini node list for scoring
                score_nodes = []
                for p in paths:
                    score_nodes.append(
                        {
                            "destination_hash": ("<" + p.get("hash", "") + ">"),
                            "app_name": p.get("app_name", ""),
                            "app_data": p.get("app_data", ""),
                            "hops": p.get("hops"),
                            "last_seen": p.get("timestamp"),
                            "announce_count": p.get("announce_count", 0),
                        }
                    )
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
        for p in _paths_cache["data"] or []:
            iface = p.get("interface", "unknown")
            by_iface[iface] = by_iface.get(iface, 0) + 1

        return {
            "paths": paths,
            "total": len(paths),
            "by_interface": by_iface,
        }

    result = await _run_sync(_enrich_and_score)
    return _ok(result)


def setup_mesh_routes(app: aiohttp.web.Application) -> None:
    """Register mesh, routing, transport, and reachability API routes."""
    app.router.add_get("/api/mesh/nodes", handle_mesh_nodes)
    app.router.add_get("/api/mesh/summary", handle_mesh_summary)
    app.router.add_get("/api/mesh/telemetry", handle_mesh_telemetry)
    app.router.add_get("/api/transport", handle_transport)
    app.router.add_get("/api/connectivity", handle_connectivity)
    app.router.add_get("/api/routing", handle_routing)
    app.router.add_get("/api/path_warming", handle_path_warming)
    app.router.add_get("/api/transport_health", handle_transport_health)
    app.router.add_get("/api/reachability", handle_reachability)
    app.router.add_get("/api/paths", handle_paths)
