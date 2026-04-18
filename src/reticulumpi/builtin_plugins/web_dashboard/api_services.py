"""API route handlers for plugin-provided services.

Covers: LoRa diagnostics, messaging hub, NomadNet auth, Meshtastic gateway,
MeshCore gateway, sensors, alerts, emergency broadcasts, and file transfers.
"""

from __future__ import annotations

import aiohttp.web

from reticulumpi.builtin_plugins.web_dashboard.api import _error, _get_plugin, _ok


# ── LoRa diagnostics ────────────────────────────────────────────────


async def handle_lora_diagnostics(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/lora — LoRa diagnostics (traffic, monitored peers, beacon status)."""
    plugin = _get_plugin(request)
    lora = plugin.app.get_plugin("lora_diagnostics")
    if not lora or not hasattr(lora, "get_diagnostics"):
        return _ok({"message": "lora_diagnostics plugin not available"})
    return _ok(lora.get_diagnostics())


async def handle_lora_announce_mode(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/lora/announce_mode — toggle LoRa announce forwarding mode.

    Body: ``{"mode": "all"|"local_priority"|"silent"}``

    Modifies rnsd's Reticulum config and restarts rnsd.
    """
    plugin = _get_plugin(request)
    lora = plugin.app.get_plugin("lora_diagnostics")
    if not lora or not hasattr(lora, "set_announce_mode"):
        return _error("lora_diagnostics plugin not available", 503)

    try:
        body = await request.json()
    except Exception:
        return aiohttp.web.json_response(
            {"ok": False, "error": "Invalid JSON body"}, status=400
        )

    mode = body.get("mode", "")
    if not mode:
        return aiohttp.web.json_response(
            {"ok": False, "error": "Missing 'mode' field"}, status=400
        )

    try:
        result = lora.set_announce_mode(mode)
        return _ok(result)
    except ValueError as exc:
        return aiohttp.web.json_response(
            {"ok": False, "error": str(exc)}, status=400
        )
    except RuntimeError as exc:
        return aiohttp.web.json_response(
            {"ok": False, "error": str(exc)}, status=500
        )


# ── Sensors, alerts, emergency, files ────────────────────────────────


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


# ── NomadNet auth ────────────────────────────────────────────────────


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


# ── Meshtastic gateway ──────────────────────────────────────────────


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


async def handle_meshtastic_device(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/meshtastic/device — Meshtastic device hardware and radio info."""
    plugin = _get_plugin(request)
    gw = plugin.app.get_plugin("meshtastic_gateway")
    if not gw or not hasattr(gw, "get_device_info"):
        return _ok({"available": False, "message": "meshtastic_gateway plugin not enabled"})
    return _ok(gw.get_device_info())


async def handle_meshtastic_lora_neighbors(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/meshtastic/lora_neighbors — LoRa-only neighbors from the physical radio."""
    plugin = _get_plugin(request)
    gw = plugin.app.get_plugin("meshtastic_gateway")
    if not gw or not hasattr(gw, "get_lora_neighbors"):
        return _ok({"neighbors": [], "message": "meshtastic_gateway plugin not enabled"})
    return _ok({"neighbors": gw.get_lora_neighbors()})


async def handle_meshtastic_channels(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/meshtastic/channels — Radio channel configuration (serial mode)."""
    plugin = _get_plugin(request)
    gw = plugin.app.get_plugin("meshtastic_gateway")
    if not gw or not hasattr(gw, "get_channels"):
        return _ok({"channels": [], "message": "meshtastic_gateway plugin not enabled"})
    return _ok({
        "channels": gw.get_channels(),
        "live": getattr(gw, "channels_live", None),
        "cache_age_seconds": getattr(gw, "channels_cache_age_seconds", None),
    })


async def handle_meshtastic_channel_join(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/meshtastic/channels/join — Join a channel on the radio.

    Body: ``{"name": "...", "psk": "..."}`` or ``{"url": "https://meshtastic.org/e/#..."}``
    ``psk`` accepts: "none", "default", "random", "simple1"-"simple254", or base64 key.
    ``index`` (1-7) is optional; omit to use the first available slot.
    """
    plugin = _get_plugin(request)
    gw = plugin.app.get_plugin("meshtastic_gateway")
    if not gw:
        return _error("meshtastic_gateway plugin not enabled", 503)

    try:
        body = await request.json()
    except Exception:
        return _error("Invalid JSON body", 400)

    # URL-based join
    url = body.get("url", "").strip()
    if url:
        if not hasattr(gw, "join_channel_url"):
            return _error("Channel URL join not supported", 501)
        result = gw.join_channel_url(url)
        if result.get("ok"):
            return _ok(result)
        return _error(result.get("reason", "Join failed"), 400)

    # Name + PSK join
    name = body.get("name", "").strip()
    psk = body.get("psk", "default").strip()
    if not name:
        return _error("name field is required", 400)
    index = body.get("index")
    if index is not None:
        try:
            index = int(index)
        except (TypeError, ValueError):
            return _error("index must be an integer 1-7", 400)

    if not hasattr(gw, "join_channel"):
        return _error("Channel join not supported", 501)
    result = gw.join_channel(name, psk, index=index)
    if result.get("ok"):
        return _ok(result)
    return _error(result.get("reason", "Join failed"), 400)


async def handle_meshtastic_channel_delete(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """DELETE /api/meshtastic/channels/{index} — Remove a SECONDARY channel."""
    plugin = _get_plugin(request)
    gw = plugin.app.get_plugin("meshtastic_gateway")
    if not gw or not hasattr(gw, "delete_channel"):
        return _error("meshtastic_gateway plugin not enabled", 503)

    try:
        index = int(request.match_info["index"])
    except (KeyError, ValueError):
        return _error("Invalid channel index", 400)

    result = gw.delete_channel(index)
    if result.get("ok"):
        return _ok(result)
    return _error(result.get("reason", "Delete failed"), 400)


# ── MeshCore gateway ───────────────────────────────────────────────


async def handle_meshcore_status(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/meshcore/status — MeshCore gateway status and message stats."""
    plugin = _get_plugin(request)
    gw = plugin.app.get_plugin("meshcore_gateway")
    if not gw or not hasattr(gw, "get_status"):
        return _ok({"available": False, "message": "meshcore_gateway plugin not enabled"})
    return _ok(gw.get_status())


async def handle_meshcore_contacts(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/meshcore/contacts — Known MeshCore contacts."""
    plugin = _get_plugin(request)
    gw = plugin.app.get_plugin("meshcore_gateway")
    if not gw or not hasattr(gw, "get_contacts"):
        return _ok({"contacts": [], "message": "meshcore_gateway plugin not enabled"})
    return _ok({"contacts": gw.get_contacts()})


async def handle_meshcore_device(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/meshcore/device — MeshCore device hardware and firmware info."""
    plugin = _get_plugin(request)
    gw = plugin.app.get_plugin("meshcore_gateway")
    if not gw or not hasattr(gw, "get_device_info"):
        return _ok({"available": False, "message": "meshcore_gateway plugin not enabled"})
    return _ok(gw.get_device_info())


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
    sub_transport = request.query.get("sub_transport")
    direction = request.query.get("direction") or None
    since_str = request.query.get("since")
    try:
        since = float(since_str) if since_str else None
    except (ValueError, TypeError):
        return _error("since must be a numeric timestamp", 400)

    messages = hub.get_messages(
        limit=limit, offset=offset, transport=transport,
        direction=direction, since=since, sub_transport=sub_transport,
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

    kwargs: dict = {}
    if body.get("msg_type"):
        kwargs["msg_type"] = body["msg_type"]
    if body.get("sub_transport"):
        kwargs["sub_transport"] = body["sub_transport"]
    if body.get("channel") is not None:
        try:
            kwargs["channel"] = int(body["channel"])
        except (TypeError, ValueError):
            return _error("channel must be an integer 0-7", 400)
    result = hub.send_message(transport, text, destination, **kwargs)
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

    Query params:
        transport — optional transport filter (e.g. ``meshtastic``).
        q — optional search string (name or id substring match).
    """
    plugin = _get_plugin(request)
    hub = plugin.app.get_plugin("messaging_hub")
    if not hub or not hasattr(hub, "get_contacts"):
        return _ok({"contacts": []})
    transport = request.query.get("transport") or None
    query = request.query.get("q") or None
    sub_transport = request.query.get("sub_transport")
    contacts = hub.get_contacts(transport, query=query)
    # The hub aggregates from adapters which don't know about sub_transport
    # today, so filter client-side here on any sub_transport hint the
    # contact carries.  Adapters that expose a sub_transport on contacts
    # (e.g. Meshtastic MQTT vs serial peers) will work cleanly; the rest
    # ignore the filter because their contacts have no sub_transport key.
    if sub_transport is not None:
        contacts = [
            c for c in contacts
            if not c.get("sub_transport")
            or c.get("sub_transport") == sub_transport
        ]
    return _ok({"contacts": contacts})


async def handle_message_stats(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/messages/stats — Message counts by transport and direction."""
    plugin = _get_plugin(request)
    hub = plugin.app.get_plugin("messaging_hub")
    if not hub or not hasattr(hub, "get_stats"):
        return _ok({"stats": {}})
    return _ok({"stats": hub.get_stats()})


async def handle_conversations(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/messages/conversations — Conversation summaries."""
    plugin = _get_plugin(request)
    hub = plugin.app.get_plugin("messaging_hub")
    if not hub or not hasattr(hub, "get_conversations"):
        return _ok({"conversations": []})
    transport = request.query.get("transport") or None
    sub_transport = request.query.get("sub_transport")
    return _ok({"conversations": hub.get_conversations(
        transport=transport, sub_transport=sub_transport,
    )})


async def handle_conversation_messages(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/messages/conversation/{contact_id} — Messages for one conversation."""
    plugin = _get_plugin(request)
    hub = plugin.app.get_plugin("messaging_hub")
    if not hub or not hasattr(hub, "get_conversation_messages"):
        return _ok({"messages": []})
    contact_id = request.match_info["contact_id"]
    limit = min(int(request.query.get("limit", "50")), 200)
    before_str = request.query.get("before")
    before = float(before_str) if before_str else None
    return _ok({
        "messages": hub.get_conversation_messages(
            contact_id, limit=limit, before=before,
        ),
    })


async def handle_message_search(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/messages/search — Text search across messages."""
    plugin = _get_plugin(request)
    hub = plugin.app.get_plugin("messaging_hub")
    if not hub or not hasattr(hub, "search_messages"):
        return _ok({"messages": []})
    query = request.query.get("q", "").strip()
    if not query:
        return _ok({"messages": []})
    limit = min(int(request.query.get("limit", "50")), 200)
    transport = request.query.get("transport") or None
    sub_transport = request.query.get("sub_transport")
    return _ok({"messages": hub.search_messages(
        query, limit=limit, transport=transport, sub_transport=sub_transport,
    )})


async def handle_delete_conversation(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """DELETE /api/messages/conversation/{contact_id} — Delete all messages."""
    plugin = _get_plugin(request)
    hub = plugin.app.get_plugin("messaging_hub")
    if not hub or not hasattr(hub, "delete_conversation"):
        return _error("messaging_hub not available", 503)
    contact_id = request.match_info["contact_id"]
    if not contact_id:
        return aiohttp.web.json_response(
            {"ok": False, "error": "contact_id required"}, status=400,
        )
    deleted = hub.delete_conversation(contact_id)
    return _ok({"deleted": deleted})


async def handle_mark_read(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/messages/read — Mark conversation as read."""
    plugin = _get_plugin(request)
    hub = plugin.app.get_plugin("messaging_hub")
    if not hub or not hasattr(hub, "mark_read"):
        return _ok({"updated": 0})
    try:
        body = await request.json()
    except Exception:
        return aiohttp.web.json_response(
            {"ok": False, "error": "Invalid JSON"}, status=400,
        )
    contact_id = body.get("contact_id", "")
    if not contact_id:
        return aiohttp.web.json_response(
            {"ok": False, "error": "contact_id required"}, status=400,
        )
    updated = hub.mark_read(contact_id)
    return _ok({"updated": updated})


async def handle_unread_counts(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/messages/unread — Unread counts per contact."""
    plugin = _get_plugin(request)
    hub = plugin.app.get_plugin("messaging_hub")
    if not hub or not hasattr(hub, "get_unread_counts"):
        return _ok({"unread": {}})
    transport = request.query.get("transport") or None
    sub_transport = request.query.get("sub_transport")
    return _ok({"unread": hub.get_unread_counts(
        transport=transport, sub_transport=sub_transport,
    )})


# ── Space tracker ────────────────────────────────────────────────────


async def handle_space_snapshot(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/space — current snapshot (TLE groups, launches, weather, quotas).

    Live satellite positions are delivered via the WebSocket stream
    (event: ``space.positions.snapshot``) rather than this endpoint, so
    the REST call stays cheap regardless of how many sats are tracked.
    """
    plugin = _get_plugin(request)
    tracker = plugin.app.get_plugin("space_tracker")
    if not tracker or not hasattr(tracker, "get_snapshot"):
        return _ok({"available": False, "message": "space_tracker plugin not enabled"})
    try:
        snap = tracker.get_snapshot()
    except Exception:
        return _error("Failed to gather space_tracker snapshot", 500)
    snap["available"] = True
    return _ok(snap)


# ── Spectrum scanner ─────────────────────────────────────────────────


async def handle_spectrum_history(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/spectrum/history — full waterfall backfill (one-shot).

    The WebSocket broadcast carries only the last few sweeps to keep
    each tick compact; this endpoint returns the plugin's full rolling
    buffer (capped by ``waterfall_rows`` in config) so the dashboard
    can paint a populated waterfall on page load instead of starting
    blank and accumulating only ~16 s of pre-load history from the WS
    tail.
    """
    plugin = _get_plugin(request)
    scanner = plugin.app.get_plugin("spectrum_scanner")
    if not scanner or not hasattr(scanner, "get_history"):
        return _ok({"available": False, "rows": []})
    try:
        return _ok(scanner.get_history())
    except Exception:
        return _error("Failed to gather spectrum history", 500)


def setup_service_routes(app: aiohttp.web.Application) -> None:
    """Register plugin service API routes."""
    # LoRa
    app.router.add_get("/api/lora", handle_lora_diagnostics)
    app.router.add_post("/api/lora/announce_mode", handle_lora_announce_mode)
    # Data plugins
    app.router.add_get("/api/alerts", handle_alerts)
    app.router.add_get("/api/files", handle_files)
    app.router.add_get("/api/sensors", handle_sensors)
    app.router.add_get("/api/sensors/history", handle_sensor_history)
    app.router.add_get("/api/emergency", handle_emergency)
    # NomadNet
    app.router.add_get("/api/nomadnet/auth", handle_nomadnet_auth)
    app.router.add_post("/api/nomadnet/auth/add", handle_nomadnet_auth_add)
    app.router.add_post("/api/nomadnet/auth/remove", handle_nomadnet_auth_remove)
    # Meshtastic
    app.router.add_get("/api/meshtastic/status", handle_meshtastic_status)
    app.router.add_get("/api/meshtastic/nodes", handle_meshtastic_nodes)
    app.router.add_get("/api/meshtastic/device", handle_meshtastic_device)
    app.router.add_get("/api/meshtastic/lora_neighbors", handle_meshtastic_lora_neighbors)
    app.router.add_get("/api/meshtastic/channels", handle_meshtastic_channels)
    app.router.add_post("/api/meshtastic/channels/join", handle_meshtastic_channel_join)
    app.router.add_delete("/api/meshtastic/channels/{index}", handle_meshtastic_channel_delete)
    # MeshCore
    app.router.add_get("/api/meshcore/status", handle_meshcore_status)
    app.router.add_get("/api/meshcore/contacts", handle_meshcore_contacts)
    app.router.add_get("/api/meshcore/device", handle_meshcore_device)
    # Messaging
    app.router.add_get("/api/messages", handle_messages)
    app.router.add_post("/api/messages/send", handle_send_message)
    app.router.add_get("/api/messages/transports", handle_transports)
    app.router.add_get("/api/messages/contacts", handle_contacts)
    app.router.add_get("/api/messages/stats", handle_message_stats)
    app.router.add_get("/api/messages/conversations", handle_conversations)
    app.router.add_get(
        "/api/messages/conversation/{contact_id}", handle_conversation_messages,
    )
    app.router.add_delete(
        "/api/messages/conversation/{contact_id}", handle_delete_conversation,
    )
    app.router.add_get("/api/messages/search", handle_message_search)
    app.router.add_post("/api/messages/read", handle_mark_read)
    app.router.add_get("/api/messages/unread", handle_unread_counts)
    # Spectrum scanner
    app.router.add_get("/api/spectrum/history", handle_spectrum_history)
    # Space tracker
    app.router.add_get("/api/space", handle_space_snapshot)
