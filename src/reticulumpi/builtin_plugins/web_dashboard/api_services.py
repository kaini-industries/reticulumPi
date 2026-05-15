"""API route handlers for plugin-provided services.

Covers: LoRa diagnostics, messaging hub, NomadNet auth, Meshtastic gateway,
MeshCore gateway, sensors, alerts, emergency broadcasts, and file transfers.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import threading
import time
from collections import deque

import aiohttp.web

from reticulumpi.builtin_plugins.web_dashboard.api import _error, _get_plugin, _ok


# ── Send-endpoint rate limiter ─────────────────────────────────────
# LoRa transmit is a limited, regulated resource. A runaway caller could
# saturate the channel or trigger duty-cycle violations. Gate every call
# to /api/messages/send through a sliding-window limiter.
#
# State is stashed on the plugin instance so hot-reload reinitializes it
# cleanly (module-level globals survive `importlib.reload` and would leak
# stale buckets across reloads).


def _get_rate_state(plugin) -> tuple[threading.Lock, dict[str, deque]]:
    """Return the (lock, buckets) pair for *plugin*, creating on first use."""
    state = getattr(plugin, "_send_rate_state", None)
    # Mock-based tests have auto-vivified attributes, so don't trust a
    # truthy value alone — require the shape we wrote.
    if not isinstance(state, tuple) or len(state) != 2:
        state = (threading.Lock(), {})
        plugin._send_rate_state = state
    return state


def _check_send_rate_limit(
    plugin, key: str, max_per_window: int, window_seconds: float,
) -> tuple[bool, float]:
    """Return ``(allowed, retry_after)``. Uses a per-key sliding window."""
    now = time.monotonic()
    cutoff = now - window_seconds
    lock, buckets = _get_rate_state(plugin)
    with lock:
        bucket = buckets.setdefault(key, deque())
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= max_per_window:
            retry_after = max(0.0, window_seconds - (now - bucket[0]))
            return False, retry_after
        bucket.append(now)
        # Bound memory: drop buckets that have no recent activity.
        if len(buckets) > 256:
            stale = [
                k for k, b in buckets.items() if not b or b[-1] < cutoff
            ]
            for k in stale:
                buckets.pop(k, None)
    return True, 0.0


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
        return _error("Invalid JSON body", 400)

    mode = body.get("mode", "")
    if not mode:
        return _error("Missing 'mode' field", 400)

    try:
        result = lora.set_announce_mode(mode)
        return _ok(result)
    except ValueError as exc:
        return _error(str(exc), 400)
    except RuntimeError as exc:
        return _error(str(exc), 500)


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


async def handle_meshtastic_device_reset(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/meshtastic/device/reset — Reboot or USB-reset the Meshtastic device."""
    plugin = _get_plugin(request)
    gw = plugin.app.get_plugin("meshtastic_gateway")
    if not gw or not hasattr(gw, "reset_device"):
        return _error("meshtastic_gateway plugin not enabled", 503)

    result = gw.reset_device()
    if result.get("ok"):
        return _ok(result)
    return _error(result.get("reason", "Reset failed"), 400)


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


# ── MeshCore Observer ────────────────────────────────────────────────


async def handle_meshcore_observer_status(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/meshcore_observer/status — MeshCore observer status and packet stats."""
    plugin = _get_plugin(request)
    obs = plugin.app.get_plugin("meshcore_observer")
    if not obs or not hasattr(obs, "get_status"):
        return _ok({"available": False, "message": "meshcore_observer plugin not enabled"})
    return _ok({"available": True, **obs.get_status()})


# ── Mesh Bridge ──────────────────────────────────────────────────────


async def handle_mesh_bridge_status(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/mesh_bridge/status — Bridge runtime state and counters."""
    plugin = _get_plugin(request)
    bridge = plugin.app.get_plugin("mesh_bridge")
    if not bridge or not hasattr(bridge, "get_status"):
        return _ok({"available": False, "message": "mesh_bridge plugin not enabled"})
    return _ok({"available": True, **bridge.get_status()})


async def handle_mesh_bridge_running(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/mesh_bridge/running — Pause or resume the bridge."""
    plugin = _get_plugin(request)
    bridge = plugin.app.get_plugin("mesh_bridge")
    if not bridge or not hasattr(bridge, "set_running"):
        return _error("mesh_bridge plugin not available", 503)
    try:
        body = await request.json()
    except Exception:
        return _error("Invalid JSON body", 400)
    running = body.get("running")
    if not isinstance(running, bool):
        return _error("'running' field must be a boolean", 400)
    return _ok(bridge.set_running(running, reason="manual" if not running else None))


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

    loop = asyncio.get_running_loop()
    messages = await loop.run_in_executor(
        None,
        functools.partial(
            hub.get_messages,
            limit=limit, offset=offset, transport=transport,
            direction=direction, since=since, sub_transport=sub_transport,
        ),
    )
    return _ok({"messages": messages})


async def handle_send_message(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/messages/send — Send a message via a transport.

    Body: ``{"transport": "lxmf"|"meshtastic", "text": "...", "destination": "..."}``

    Requires a valid session token. Unauthenticated localhost callers are
    rejected unless ``allow_localhost_send`` is explicitly enabled, so that
    a compromised local process can't spoof messages under this node's
    identity. All callers are rate-limited.
    """
    plugin = _get_plugin(request)
    hub = plugin.app.get_plugin("messaging_hub")
    if not hub or not hasattr(hub, "send_message"):
        return _error("messaging_hub not enabled", 503)

    # Explicit auth gate: the global middleware bypasses auth for localhost
    # requests so that internal tooling (NomadNet pages, scripts) can reach
    # read-only endpoints. Sending radio traffic is not read-only and needs
    # a stricter check — require a token unless the operator has opted in.
    has_token = bool(request.get("token"))
    if not has_token and not plugin.config.get("allow_localhost_send", False):
        return _error(
            "Authentication required to send messages. Log in first, or set "
            "allow_localhost_send=true in the web_dashboard config to permit "
            "unauthenticated sends from localhost.",
            401,
        )

    # Rate limit per-token (or per-remote-IP if unauth-localhost is allowed).
    # Defaults: 30 sends/min — enough for interactive use, low enough to
    # keep a misbehaving caller from saturating LoRa airtime.
    rl_cfg = plugin.config.get("send_rate_limit") or {}
    max_sends = int(rl_cfg.get("max_per_window", 30))
    window_s = float(rl_cfg.get("window_seconds", 60))
    token = request.get("token")
    if token:
        rate_key = f"tok:{token}"
    else:
        # Multiple local tools (browser, CLI scripts, NomadNet helpers) all
        # connect from 127.0.0.1 and would otherwise share a single bucket,
        # so one chatty script could starve the rest. Mix in a short
        # User-Agent fingerprint so distinct clients get distinct buckets.
        # Not a security boundary — a malicious local caller can vary UA —
        # but this is only reached when allow_localhost_send is opted in,
        # which already trusts the host.
        ua = request.headers.get("User-Agent", "")
        ua_tag = hashlib.sha1(ua.encode("utf-8", "replace")).hexdigest()[:8] if ua else "none"
        rate_key = f"local:{request.remote or 'unknown'}:{ua_tag}"
    ok, retry_after = _check_send_rate_limit(
        plugin, rate_key, max_sends, window_s,
    )
    if not ok:
        resp = _error(
            f"Send rate limit exceeded (max {max_sends} per {int(window_s)}s). "
            f"Retry in {retry_after:.1f}s.",
            429,
        )
        resp.headers["Retry-After"] = str(int(retry_after) + 1)
        return resp

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
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        functools.partial(hub.send_message, transport, text, destination, **kwargs),
    )
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
    loop = asyncio.get_running_loop()
    transports = await loop.run_in_executor(None, hub.get_transports)
    return _ok({"transports": transports})


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
    loop = asyncio.get_running_loop()
    contacts = await loop.run_in_executor(
        None, functools.partial(hub.get_contacts, transport, query=query),
    )
    # Adapters don't tag their contacts with sub_transport today, so we
    # derive it from stored DM history: include a contact on a given
    # sub_transport only if we've actually exchanged DMs with that peer
    # on that sub_transport. Peers with no DM history yet pass through
    # so users can still initiate a new chat from the panel.
    #
    # Contacts that DO carry an adapter-set ``sub_transport`` are honored
    # strictly against the query, as before.
    if sub_transport is not None:
        peer_subs: dict[str, set[str]] = {}
        if transport and hasattr(hub, "get_peer_sub_transports"):
            try:
                peer_subs = await loop.run_in_executor(
                    None, functools.partial(hub.get_peer_sub_transports, transport),
                )
            except Exception:
                peer_subs = {}

        def _keep(c: dict) -> bool:
            tagged = c.get("sub_transport")
            if tagged:
                return tagged == sub_transport
            seen = peer_subs.get(c.get("id") or "")
            if not seen:
                # No DM history for this peer — allow in both panels so
                # the user can still start a conversation.
                return True
            return sub_transport in seen

        contacts = [c for c in contacts if _keep(c)]
    return _ok({"contacts": contacts})


async def handle_message_stats(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/messages/stats — Message counts by transport and direction."""
    plugin = _get_plugin(request)
    hub = plugin.app.get_plugin("messaging_hub")
    if not hub or not hasattr(hub, "get_stats"):
        return _ok({"stats": {}})
    loop = asyncio.get_running_loop()
    stats = await loop.run_in_executor(None, hub.get_stats)
    return _ok({"stats": stats})


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
    loop = asyncio.get_running_loop()
    conversations = await loop.run_in_executor(
        None,
        functools.partial(
            hub.get_conversations,
            transport=transport, sub_transport=sub_transport,
        ),
    )
    return _ok({"conversations": conversations})


async def handle_conversation_messages(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/messages/conversation/{contact_id} — Messages for one conversation."""
    plugin = _get_plugin(request)
    hub = plugin.app.get_plugin("messaging_hub")
    if not hub or not hasattr(hub, "get_conversation_messages"):
        return _ok({"messages": []})
    contact_id = request.match_info["contact_id"]
    try:
        limit = min(int(request.query.get("limit", "50")), 200)
    except (ValueError, TypeError):
        return _error("limit must be an integer", 400)
    before_str = request.query.get("before")
    try:
        before = float(before_str) if before_str else None
    except (ValueError, TypeError):
        return _error("before must be a numeric timestamp", 400)
    loop = asyncio.get_running_loop()
    msgs = await loop.run_in_executor(
        None,
        functools.partial(
            hub.get_conversation_messages,
            contact_id, limit=limit, before=before,
        ),
    )
    return _ok({"messages": msgs})


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
    try:
        limit = min(int(request.query.get("limit", "50")), 200)
    except (ValueError, TypeError):
        return _error("limit must be an integer", 400)
    transport = request.query.get("transport") or None
    sub_transport = request.query.get("sub_transport")
    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(
        None,
        functools.partial(
            hub.search_messages,
            query, limit=limit, transport=transport, sub_transport=sub_transport,
        ),
    )
    return _ok({"messages": results})


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
        return _error("contact_id required", 400)
    loop = asyncio.get_running_loop()
    deleted = await loop.run_in_executor(
        None, functools.partial(hub.delete_conversation, contact_id),
    )
    return _ok({"deleted": deleted})


async def handle_mark_read(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/messages/read — Mark conversation as read."""
    plugin = _get_plugin(request)
    hub = plugin.app.get_plugin("messaging_hub")
    if not hub or not hasattr(hub, "mark_read"):
        return _error("messaging_hub not available", 503)
    try:
        body = await request.json()
    except Exception:
        return _error("Invalid JSON", 400)
    contact_id = body.get("contact_id", "")
    if not contact_id:
        return _error("contact_id required", 400)
    loop = asyncio.get_running_loop()
    updated = await loop.run_in_executor(
        None, functools.partial(hub.mark_read, contact_id),
    )
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
    loop = asyncio.get_running_loop()
    unread = await loop.run_in_executor(
        None,
        functools.partial(
            hub.get_unread_counts,
            transport=transport, sub_transport=sub_transport,
        ),
    )
    return _ok({"unread": unread})


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


# ── GPS telemetry ────────────────────────────────────────────────────


async def handle_gps_snapshot(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/gps — status + last fix + satellites-in-view."""
    plugin = _get_plugin(request)
    gps = plugin.app.get_plugin("gps_telemetry")
    if not gps or not hasattr(gps, "get_snapshot"):
        return _ok({"available": False, "message": "gps_telemetry plugin not enabled"})
    snap = gps.get_snapshot()
    snap["available"] = True
    return _ok(snap)


async def handle_gps_status(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/gps/status — connection + fix presence, without the fix payload."""
    plugin = _get_plugin(request)
    gps = plugin.app.get_plugin("gps_telemetry")
    if not gps or not hasattr(gps, "get_status"):
        return _ok({"available": False})
    status = gps.get_status()
    status["available"] = True
    return _ok(status)


async def handle_gps_satellites(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/gps/satellites — just the per-SV sky view."""
    plugin = _get_plugin(request)
    gps = plugin.app.get_plugin("gps_telemetry")
    if not gps or not hasattr(gps, "get_snapshot"):
        return _ok({"available": False, "satellites": []})
    snap = gps.get_snapshot()
    return _ok(
        {
            "available": True,
            "satellites": snap.get("satellites_in_view", []),
        }
    )


# ── ADS-B radar ──────────────────────────────────────────────────────


async def handle_adsb_snapshot(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/adsb — current aircraft snapshot."""
    plugin = _get_plugin(request)
    adsb = plugin.app.get_plugin("adsb_radar")
    if not adsb or not hasattr(adsb, "get_snapshot"):
        return _ok({"available": False, "message": "adsb_radar plugin not enabled"})
    try:
        snap = adsb.get_snapshot()
    except Exception:
        return _error("Failed to gather adsb_radar snapshot", 500)
    snap["available"] = True
    return _ok(snap)


async def handle_ntp_snapshot(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/ntp — full NTP/chrony status + sources."""
    plugin = _get_plugin(request)
    ntp = plugin.app.get_plugin("ntp_server")
    if not ntp or not hasattr(ntp, "get_snapshot"):
        return _ok({"available": False, "message": "ntp_server plugin not enabled"})
    try:
        snap = ntp.get_snapshot()
    except Exception:
        return _error("Failed to gather ntp_server snapshot", 500)
    snap["available"] = True
    return _ok(snap)


async def handle_ntp_sources(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/ntp/sources — chrony source list only."""
    plugin = _get_plugin(request)
    ntp = plugin.app.get_plugin("ntp_server")
    if not ntp or not hasattr(ntp, "get_snapshot"):
        return _ok({"available": False, "sources": []})
    try:
        snap = ntp.get_snapshot()
    except Exception:
        return _ok({"available": False, "sources": []})
    return _ok({"available": True, "sources": snap.get("sources", [])})


# ── LoRa link tester ────────────────────────────────────────────────────


async def handle_link_tester_snapshot(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/link_tester — status + full history."""
    plugin = _get_plugin(request)
    lt = plugin.app.get_plugin("lora_link_tester")
    if not lt or not hasattr(lt, "get_history"):
        return _ok({"available": False, "message": "lora_link_tester plugin not enabled"})
    try:
        return _ok(lt.get_history())
    except Exception:
        return _error("Failed to gather link tester data", 500)


async def handle_link_tester_start(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/link_tester/start — start a test run."""
    plugin = _get_plugin(request)
    lt = plugin.app.get_plugin("lora_link_tester")
    if not lt or not hasattr(lt, "start_test"):
        return _error("lora_link_tester plugin not available", 503)
    try:
        body = await request.json()
    except Exception:
        body = {}
    result = lt.start_test(
        target=body.get("target"),
        count=body.get("count"),
    )
    if not result.get("ok"):
        return _error(result.get("reason", "unknown error"), 400)
    return _ok(result)


async def handle_link_tester_stop(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/link_tester/stop — stop current test run."""
    plugin = _get_plugin(request)
    lt = plugin.app.get_plugin("lora_link_tester")
    if not lt or not hasattr(lt, "stop_test"):
        return _error("lora_link_tester plugin not available", 503)
    return _ok(lt.stop_test())


async def handle_link_tester_clear(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/link_tester/clear — clear history buffer."""
    plugin = _get_plugin(request)
    lt = plugin.app.get_plugin("lora_link_tester")
    if not lt or not hasattr(lt, "clear_history"):
        return _error("lora_link_tester plugin not available", 503)
    return _ok(lt.clear_history())


# ── Signal plugin endpoints ──────────────────────────────────────────


async def handle_weather_alert(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    plugin = _get_plugin(request)
    wa = plugin.app.get_plugin("weather_alert")
    if not wa:
        return _error("weather_alert plugin not available", 503)
    return _ok(wa.get_snapshot())


async def handle_weather_alert_active(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    plugin = _get_plugin(request)
    wa = plugin.app.get_plugin("weather_alert")
    if not wa:
        return _error("weather_alert plugin not available", 503)
    snap = wa.get_snapshot()
    return _ok(snap.get("active_alert"))


async def handle_ais(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    plugin = _get_plugin(request)
    ais = plugin.app.get_plugin("ais_receiver")
    if not ais:
        return _error("ais_receiver plugin not available", 503)
    return _ok(ais.get_snapshot())


async def handle_acars(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    plugin = _get_plugin(request)
    acars = plugin.app.get_plugin("acars_decoder")
    if not acars:
        return _error("acars_decoder plugin not available", 503)
    return _ok(acars.get_snapshot())


async def handle_radiosonde(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    plugin = _get_plugin(request)
    rs = plugin.app.get_plugin("radiosonde_tracker")
    if not rs:
        return _error("radiosonde_tracker plugin not available", 503)
    return _ok(rs.get_snapshot())


async def handle_noaa(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    plugin = _get_plugin(request)
    noaa = plugin.app.get_plugin("noaa_apt_decoder")
    if not noaa:
        return _error("noaa_apt_decoder plugin not available", 503)
    return _ok(noaa.get_snapshot())


async def handle_noaa_image(
    request: aiohttp.web.Request,
) -> aiohttp.web.StreamResponse:
    import os
    plugin = _get_plugin(request)
    noaa = plugin.app.get_plugin("noaa_apt_decoder")
    if not noaa:
        return _error("noaa_apt_decoder plugin not available", 503)
    filename = request.match_info.get("filename", "")
    if not filename or ".." in filename or "/" in filename:
        return _error("invalid filename", 400)
    image_dir = getattr(noaa, "_image_dir", "")
    if not image_dir:
        return _error("image directory not configured", 503)
    path = os.path.join(image_dir, filename)
    if not os.path.exists(path):
        return _error("image not found", 404)
    return aiohttp.web.FileResponse(path)


async def handle_sdr_scheduler(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    plugin = _get_plugin(request)
    sched = getattr(plugin.app, "sdr_scheduler", None)
    if not sched:
        return _error("sdr_scheduler not available", 503)
    return _ok(sched.get_status())


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
    app.router.add_post("/api/meshtastic/device/reset", handle_meshtastic_device_reset)
    app.router.add_get("/api/meshtastic/lora_neighbors", handle_meshtastic_lora_neighbors)
    app.router.add_get("/api/meshtastic/channels", handle_meshtastic_channels)
    app.router.add_post("/api/meshtastic/channels/join", handle_meshtastic_channel_join)
    app.router.add_delete("/api/meshtastic/channels/{index}", handle_meshtastic_channel_delete)
    # MeshCore
    app.router.add_get("/api/meshcore/status", handle_meshcore_status)
    app.router.add_get("/api/meshcore/contacts", handle_meshcore_contacts)
    app.router.add_get("/api/meshcore/device", handle_meshcore_device)
    # MeshCore Observer
    app.router.add_get("/api/meshcore_observer/status", handle_meshcore_observer_status)
    # Mesh Bridge
    app.router.add_get("/api/mesh_bridge/status", handle_mesh_bridge_status)
    app.router.add_post("/api/mesh_bridge/running", handle_mesh_bridge_running)
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
    # Space tracker
    app.router.add_get("/api/space", handle_space_snapshot)
    # GPS telemetry
    app.router.add_get("/api/gps", handle_gps_snapshot)
    app.router.add_get("/api/gps/status", handle_gps_status)
    app.router.add_get("/api/gps/satellites", handle_gps_satellites)
    # ADS-B radar
    app.router.add_get("/api/adsb", handle_adsb_snapshot)
    # NTP server
    app.router.add_get("/api/ntp", handle_ntp_snapshot)
    app.router.add_get("/api/ntp/sources", handle_ntp_sources)
    # LoRa link tester
    app.router.add_get("/api/link_tester", handle_link_tester_snapshot)
    app.router.add_post("/api/link_tester/start", handle_link_tester_start)
    app.router.add_post("/api/link_tester/stop", handle_link_tester_stop)
    app.router.add_post("/api/link_tester/clear", handle_link_tester_clear)
    # Signal plugins
    app.router.add_get("/api/weather_alert", handle_weather_alert)
    app.router.add_get("/api/weather_alert/active", handle_weather_alert_active)
    app.router.add_get("/api/ais", handle_ais)
    app.router.add_get("/api/acars", handle_acars)
    app.router.add_get("/api/radiosonde", handle_radiosonde)
    app.router.add_get("/api/noaa", handle_noaa)
    app.router.add_get("/api/noaa/image/{filename}", handle_noaa_image)
    app.router.add_get("/api/sdr_scheduler", handle_sdr_scheduler)
