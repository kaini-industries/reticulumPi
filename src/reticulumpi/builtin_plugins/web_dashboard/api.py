"""JSON API route handlers — core utilities, auth, and system endpoints.

Domain-specific handlers are in:
  - api_interfaces.py  (interface config management)
  - api_mesh.py        (mesh network, routing, transport, reachability)
  - api_services.py    (LoRa, messaging, NomadNet, Meshtastic, sensors, etc.)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import logging
import re
import secrets
import time
from typing import TYPE_CHECKING, Any

import aiohttp.web

from reticulumpi.builtin_plugins.web_dashboard.keys import (
    get_app_plugin,
    get_request_token,
)
from reticulumpi.builtin_plugins.web_dashboard.operational_metrics import (
    record_auth_admission,
    record_auth_completion,
    record_auth_release_failure,
)
from reticulumpi.plugin_base import resolve_ready_plugin

from reticulumpi.builtin_plugins.web_dashboard.shared_state import (
    client_error_rate_limiter as _client_error_rl,
)
from reticulumpi.builtin_plugins.web_dashboard.shared_state import (
    offgrid_rate_limiter as _offgrid_rl,
)

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    pass

SENSITIVE_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "current_password",
        "new_password",
        "token",
        "secret",
        "api_key",
        "private_key",
        "credentials",
        "auth_token",
        "channel_key",
        "passphrase",
        "passwd",
        "psk",
    }
)
_SENSITIVE_KEY_WORDS = frozenset(
    {
        "credential",
        "credentials",
        "key",
        "passphrase",
        "passwd",
        "password",
        "psk",
        "secret",
        "token",
    }
)

# API version — bump when making breaking changes to response schemas.
# Included in all API responses via the Api-Version header so clients
# can detect incompatibilities before they parse the response body.
API_VERSION = "1.1"


def _scrub_sensitive(obj: Any) -> Any:
    """Recursively redact conservative, case-insensitive secret-like key names."""
    if isinstance(obj, dict):
        scrubbed = {}
        for key, value in obj.items():
            canonical = (
                re.sub(
                    r"[^a-z0-9]+",
                    "_",
                    re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).casefold(),
                ).strip("_")
                if isinstance(key, str)
                else ""
            )
            words = frozenset(canonical.split("_")) if canonical else frozenset()
            sensitive = canonical in SENSITIVE_KEYS or bool(words & _SENSITIVE_KEY_WORDS)
            scrubbed[key] = "***" if sensitive else _scrub_sensitive(value)
        return scrubbed
    if isinstance(obj, list):
        return [_scrub_sensitive(item) for item in obj]
    return obj


# ── Shared utilities (imported by sub-modules) ───────────────────────


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
        readiness_getter = getattr(type(app), "get_ready_plugin", None)
        if callable(readiness_getter) and readiness_getter(app, name) is not p:
            continue
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
        except (ValueError, IndexError, TypeError):
            pass

        services.append(
            {
                "destination_hash": dest_hash,
                "plugin_name": getattr(p, "plugin_name", name),
                "app_name": app_name,
                "aspects": aspects,
                "is_local": True,
            }
        )
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
        log.debug("Interface traffic collection failed", exc_info=True)
    return traffic_map


def _ok(data: Any, status: int = 200) -> aiohttp.web.Response:
    """Return a success JSON response."""
    import json

    body = json.dumps({"ok": True, "data": data, "timestamp": time.time()})
    return aiohttp.web.Response(text=body, status=status, content_type="application/json")


def _error(message: str, status: int = 400) -> aiohttp.web.Response:
    """Return an error JSON response."""
    import json

    body = json.dumps({"ok": False, "error": message, "code": status})
    return aiohttp.web.Response(text=body, status=status, content_type="application/json")


def _get_plugin(request: aiohttp.web.Request):
    """Get the WebDashboardPlugin from the request's app."""
    return get_app_plugin(request.app)


async def _run_sync(fn, *args, **kwargs):
    """Run a blocking function in the default executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


async def _run_auth_work(plugin: Any, fn, *args) -> tuple[bool, Any]:
    """Run expensive password work in the dashboard's bounded auth pool.

    The boolean is false when the four-request admission limit is saturated.
    Tests and embedding code that have not called ``plugin.start()`` retain a
    safe default-executor fallback.
    """
    executor = getattr(plugin, "_auth_executor", None)
    slots = getattr(plugin, "_auth_slots", None)
    if not isinstance(executor, concurrent.futures.Executor):
        record_auth_admission("bypassed")
        return True, await _run_sync(fn, *args)
    acquire = getattr(slots, "acquire", None)
    release = getattr(slots, "release", None)
    if not callable(acquire) or not callable(release):
        record_auth_admission("rejected")
        log.error("Authentication executor is configured without a valid admission semaphore")
        return False, None
    try:
        admitted = acquire(blocking=False)
    except Exception:
        record_auth_admission("rejected")
        log.exception("Authentication admission semaphore failed")
        return False, None
    if not admitted:
        record_auth_admission("saturated")
        return False, None
    record_auth_admission("admitted")
    work_succeeded = False
    try:
        loop = asyncio.get_running_loop()
        try:
            pending = loop.run_in_executor(executor, functools.partial(fn, *args))
        except RuntimeError:
            log.exception("Authentication executor is unavailable")
            return False, None
        try:
            result = await pending
        except concurrent.futures.BrokenExecutor:
            log.exception("Authentication executor failed while processing work")
            return False, None
        work_succeeded = True
        return True, result
    finally:
        record_auth_completion(succeeded=work_succeeded)
        try:
            release()
        except Exception:
            # Work may already have created or rotated a credential.  Do not
            # misreport that completed operation as failed; future admission
            # attempts will continue to fail closed if the semaphore is bad.
            record_auth_release_failure()
            log.exception("Authentication admission semaphore release failed")


# ── Route registration hub ───────────────────────────────────────────


def setup_api_routes(app: aiohttp.web.Application) -> None:
    """Register all API routes on the aiohttp application."""
    from reticulumpi.builtin_plugins.web_dashboard.api_interfaces import (
        setup_interface_routes,
    )
    from reticulumpi.builtin_plugins.web_dashboard.api_mesh import setup_mesh_routes
    from reticulumpi.builtin_plugins.web_dashboard.api_radio import (
        setup_radio_routes,
    )
    from reticulumpi.builtin_plugins.web_dashboard.api_services import (
        setup_service_routes,
    )

    # Version
    app.router.add_get("/api/version", handle_version)
    # Auth
    app.router.add_post("/api/auth/login", handle_login)
    app.router.add_post("/auth/login", handle_form_login)
    app.router.add_post("/api/auth/logout", handle_logout)
    app.router.add_post("/api/auth/password", handle_change_password)
    # System
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/node", handle_node)
    app.router.add_get("/api/metrics", handle_metrics)
    app.router.add_get("/api/plugins", handle_plugins)
    app.router.add_get("/api/plugins/{name}", handle_plugin_detail)
    app.router.add_post("/api/services/restart", handle_services_restart)
    app.router.add_get("/api/services/restart/{operation_id}", handle_services_restart_status)
    app.router.add_get("/api/config", handle_config)
    app.router.add_get("/api/offgrid", handle_offgrid_get)
    app.router.add_post("/api/offgrid", handle_offgrid_set)
    # Client-side JS error reporting (auth + CSRF protected; NOT public)
    app.router.add_post("/api/client_error", handle_client_error)
    # Spectrum presets
    app.router.add_get("/api/spectrum/presets", handle_spectrum_presets)
    app.router.add_post("/api/spectrum/preset", handle_spectrum_switch_preset)
    # Domain sub-modules
    setup_interface_routes(app)
    setup_mesh_routes(app)
    setup_service_routes(app)
    setup_radio_routes(app)


# ── Version endpoint ─────────────────────────────────────────────────


async def handle_version(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/version — API version and compatibility info."""
    plugin = _get_plugin(request)
    return _ok(
        {
            "api_version": API_VERSION,
            "app_version": plugin.app._get_version(),
        }
    )


# ── Auth endpoints ───────────────────────────────────────────────────


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

    admitted, token = await _run_auth_work(plugin, auth.login, password, remote_ip)
    if not admitted:
        return _error("Authentication service is busy; retry shortly", 503)
    if not token:
        return _error("Invalid password", 401)

    resp = _ok(
        {
            "message": "Login successful",
            "password_change_required": auth.password_change_required,
        }
    )

    # Set session cookie — Secure flag when SSL, behind HTTPS proxy,
    # or force_secure_cookie is configured.
    from reticulumpi.builtin_plugins.web_dashboard.server import _request_is_secure

    ssl_config = plugin.config.get("ssl", {})
    secure = (
        ssl_config.get("enabled", False)
        or _request_is_secure(request, plugin)
        or auth.force_secure_cookie
    )
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
    if len(password) > 256:
        raise aiohttp.web.HTTPFound("/login.html?error=too_long")

    admitted, token = await _run_auth_work(plugin, auth.login, password, remote_ip)
    if not admitted:
        raise aiohttp.web.HTTPFound("/login.html?error=busy")
    if not token:
        raise aiohttp.web.HTTPFound("/login.html?error=invalid")

    from reticulumpi.builtin_plugins.web_dashboard.server import _request_is_secure

    ssl_config = plugin.config.get("ssl", {})
    secure = (
        ssl_config.get("enabled", False)
        or _request_is_secure(request, plugin)
        or auth.force_secure_cookie
    )

    destination = "/?password_change=required" if auth.password_change_required else "/"
    resp = aiohttp.web.HTTPFound(destination)
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
        try:
            from reticulumpi.builtin_plugins.web_dashboard.websocket_handler import (
                close_websockets_for_token,
            )

            await close_websockets_for_token(token)
        except Exception:
            log.debug("Failed to close logged-out WebSocket sessions", exc_info=True)

    resp = _ok({"message": "Logged out"})
    resp.del_cookie("session", path="/")
    return resp


async def handle_change_password(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """POST /api/auth/password — durably replace a managed dashboard password."""
    plugin = _get_plugin(request)
    try:
        body = await request.json()
    except Exception:
        return _error("Invalid request body", 400)
    if not isinstance(body, dict):
        return _error("Invalid request body", 400)

    current_password = body.get("current_password")
    new_password = body.get("new_password")
    if not isinstance(current_password, str) or not isinstance(new_password, str):
        return _error("Current and new passwords are required", 400)
    if len(current_password) > 256 or len(new_password) > 256:
        return _error("Password too long", 400)

    admitted, result = await _run_auth_work(
        plugin,
        plugin._auth.change_password,
        current_password,
        new_password,
    )
    if not admitted:
        return _error("Authentication service is busy; retry shortly", 503)
    if not result.applied:
        errors = {
            "new_password_too_short": ("New password must contain at least 12 characters", 400),
            "new_password_too_long": ("New password is too long", 400),
            "invalid_current_password": ("Current password is incorrect", 401),
            "password_unchanged": ("New password must differ from the current password", 400),
            "password_managed_externally": (
                "Password is managed by configuration and cannot be changed here",
                409,
            ),
            "persistence_failed": ("Password could not be persisted", 500),
        }
        message, status = errors.get(result.reason, ("Password could not be changed", 500))
        return _error(message, status)

    from reticulumpi.builtin_plugins.web_dashboard.websocket_handler import (
        close_websockets_for_token,
    )

    await asyncio.gather(
        *(close_websockets_for_token(token) for token in result.revoked_tokens),
        return_exceptions=True,
    )
    response = _ok(
        {
            "message": "Password changed; sign in again",
            "password_change_required": result.password_change_required,
            "reason": result.reason,
        }
    )
    response.del_cookie("session", path="/")
    return response


# ── System endpoints ─────────────────────────────────────────────────


async def handle_status(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/status — full app status."""
    plugin = _get_plugin(request)
    status = await _run_sync(plugin.app.get_status)
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
        "uptime": max(
            0.0,
            time.monotonic() - getattr(plugin, "_start_monotonic", time.monotonic()),
        )
        if plugin._active
        else 0,
        "server_time": time.time(),
    }
    return _ok(data)


async def handle_metrics(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/metrics — latest system_monitor metrics."""
    plugin = _get_plugin(request)
    monitor = resolve_ready_plugin(plugin, "system_monitor")

    if monitor and hasattr(monitor, "latest_metrics"):
        return _ok(monitor.latest_metrics)

    return _ok({"message": "system_monitor plugin not available", "metrics": {}})


def _collect_plugin_statuses(app: Any) -> dict[str, Any]:
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
    return plugins_data


async def handle_plugins(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/plugins — all plugins with statuses."""
    plugin = _get_plugin(request)
    app = plugin.app

    loop = asyncio.get_running_loop()
    plugins_data = await loop.run_in_executor(None, _collect_plugin_statuses, app)

    failed = [{"name": name, "error": reason} for name, reason in app._failed_plugins]

    return _ok({"plugins": plugins_data, "failed_plugins": failed})


async def handle_plugin_detail(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/plugins/{name} — single plugin detail."""
    plugin = _get_plugin(request)
    name = request.match_info["name"]
    p = resolve_ready_plugin(plugin, name)

    if not p:
        return _error(f"Plugin '{name}' not found", 404)

    try:
        status = await _run_sync(p.get_status)
    except Exception:
        status = {"error": "status collection failed"}

    return _ok(
        {
            "name": name,
            "version": p.plugin_version,
            "description": p.plugin_description,
            "status": status,
            "address": _get_plugin_address(p),
        }
    )


_last_restart_time: float = 0.0
_RESTART_COOLDOWN = 60.0
_MAX_RESTART_OPERATIONS = 20


async def handle_services_restart(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/services/restart — restart rnsd + reticulumpi.

    Requires password re-entry via X-Confirm-Password header as an
    extra safeguard for this destructive operation.
    """
    import asyncio

    from reticulumpi.builtin_plugins.web_dashboard.auth import verify_password

    global _last_restart_time

    if not get_request_token(request):
        return _error("Authentication required", 401)

    # Require password confirmation for this destructive action
    confirm_pw = request.headers.get("X-Confirm-Password", "")
    if not confirm_pw:
        return _error(
            "Password confirmation required (X-Confirm-Password header)",
            403,
        )
    plugin = _get_plugin(request)
    admitted, confirmed = await _run_auth_work(
        plugin,
        verify_password,
        confirm_pw,
        plugin._auth._password_hash,
    )
    if not admitted:
        return _error("Authentication service is busy; retry shortly", 503)
    if not confirmed:
        return _error("Password confirmation failed", 403)

    now = time.monotonic()
    if now - _last_restart_time < _RESTART_COOLDOWN:
        return _error("Service restart already in progress", 429)
    _last_restart_time = now

    operation_id = secrets.token_hex(8)
    operations = getattr(plugin, "_restart_operations", None)
    if not isinstance(operations, dict):
        operations = plugin._restart_operations = {}
    operations[operation_id] = {
        "id": operation_id,
        "state": "accepted",
        "created_at": time.time(),
        "error": None,
    }
    while len(operations) > _MAX_RESTART_OPERATIONS:
        oldest = min(operations, key=lambda key: operations[key]["created_at"])
        del operations[oldest]

    async def _do_restart() -> None:
        operation = operations[operation_id]
        try:
            await asyncio.sleep(2)  # let HTTP response flush
            from reticulumpi.control_client import (
                DEFAULT_CONTROL_SOCKET,
                request_control,
            )

            control_socket = plugin.config.get("control_socket", str(DEFAULT_CONTROL_SOCKET))
            operation["state"] = "requesting_control_broker"
            result = await _run_sync(
                request_control,
                "restart_services",
                socket_path=control_socket,
                timeout=65.0,
            )
            operation["state"] = "scheduled"
            operation["broker"] = result.get("operation", "restart_services")
        except asyncio.CancelledError:
            operation["state"] = "cancelled"
            raise
        except Exception as exc:
            operation["state"] = "failed"
            operation["error"] = str(exc)[:256]
            log.exception("Service restart operation %s failed", operation_id)

    task = asyncio.create_task(_do_restart(), name=f"dashboard-restart-{operation_id}")
    tasks = getattr(plugin, "_restart_tasks", None)
    if not isinstance(tasks, set):
        tasks = plugin._restart_tasks = set()
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return _ok(
        {
            "message": "Restart accepted",
            "operation_id": operation_id,
            "state": "accepted",
        },
        status=202,
    )


async def handle_services_restart_status(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET restart operation state for an authenticated dashboard client."""
    operation_id = request.match_info.get("operation_id", "")
    plugin = _get_plugin(request)
    operations = getattr(plugin, "_restart_operations", {})
    operation = operations.get(operation_id) if isinstance(operations, dict) else None
    if not operation:
        return _error("Restart operation not found", 404)
    return _ok(dict(operation))


async def handle_spectrum_presets(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/spectrum/presets — list available frequency presets."""
    plugin = _get_plugin(request)
    scanner = resolve_ready_plugin(plugin, "spectrum_scanner")
    if not scanner or not hasattr(scanner, "get_presets"):
        return _error("spectrum_scanner plugin not enabled", 404)
    return _ok(scanner.get_presets())


async def handle_spectrum_switch_preset(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/spectrum/preset — switch the active frequency preset."""
    if not get_request_token(request):
        return _error("Authentication required", 401)

    plugin = _get_plugin(request)
    scanner = resolve_ready_plugin(plugin, "spectrum_scanner")
    if not scanner or not hasattr(scanner, "switch_preset"):
        return _error("spectrum_scanner plugin not enabled", 404)

    from .api_services import _check_send_rate_limit

    remote_ip = request.remote or "unknown"
    ok, retry_after = await _run_sync(
        _check_send_rate_limit,
        plugin,
        f"preset:{remote_ip}",
        max_per_window=3,
        window_seconds=30.0,
    )
    if not ok:
        resp = _error("Too many preset switches — try again shortly", 429)
        resp.headers["Retry-After"] = str(int(retry_after) + 1)
        return resp

    try:
        body = await request.json()
    except Exception:
        return _error("Invalid JSON body", 400)

    preset_name = body.get("preset")
    if not preset_name:
        return _error("'preset' field required", 400)

    try:
        result = scanner.switch_preset(preset_name)
        return _ok(result)
    except ValueError as exc:
        return _error(str(exc), 400)


async def handle_config(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/config — read-only, sanitized config view."""
    plugin = _get_plugin(request)
    config = plugin.app.config

    # Build sanitized plugin config
    plugins_config = {}
    for name, cfg in config.plugins.items():
        plugins_config[name] = _scrub_sensitive(cfg)

    data = {
        "node_name": config.node_name,
        "log_level": config.log_level,
        "use_shared_instance": config.use_shared_instance,
        "plugin_paths": config.plugin_paths,
        "plugins": plugins_config,
    }
    return _ok(data)


async def handle_offgrid_get(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/offgrid — current off-grid mode state."""
    plugin = _get_plugin(request)
    return _ok({"enabled": plugin.app.offgrid_mode})


async def handle_offgrid_set(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """POST /api/offgrid — toggle off-grid mode."""
    plugin = _get_plugin(request)
    try:
        body = await request.json()
    except Exception:
        return _error("Invalid JSON body")
    enabled = body.get("enabled")
    if enabled is None:
        return _error("'enabled' field required")
    if not isinstance(enabled, bool):
        return _error("'enabled' must be a boolean")
    if not _offgrid_rl.check_and_record():
        return _error("Rate limited, try again shortly")
    result = await _run_sync(plugin.app.set_offgrid_mode, enabled)
    return _ok(result)


# ── Client-side error reporting ──────────────────────────────────────


def _clean_single_line(value: Any, max_len: int) -> str:
    """Coerce to str, strip newlines/control chars, and truncate.

    Used for fields logged on a single journal line so they stay
    one-line greppable. Control chars (incl. CR/LF/TAB) are replaced
    with spaces before truncation.
    """
    text = str(value) if value is not None else ""
    cleaned = "".join(" " if ord(ch) < 0x20 or ord(ch) == 0x7F else ch for ch in text)
    return cleaned[:max_len]


async def handle_client_error(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """POST /api/client_error — receive a browser-side JS error report.

    Auth + CSRF protected (not in PUBLIC_PATHS). Per-IP rate limited to
    bound log-flood risk. Payload fields are coerced to str, truncated,
    and (for single-line fields) stripped of control chars so journal
    lines stay one-line greppable.
    """
    if not get_request_token(request):
        return _error("Authentication required", 401)

    ip = request.remote or "unknown"
    if not _client_error_rl.is_allowed(ip):
        return _error("Rate limited", 429)
    _client_error_rl.record_attempt(ip)

    try:
        body = await request.json()
    except Exception:
        return _error("Invalid JSON body", 400)
    if not isinstance(body, dict):
        return _error("Invalid JSON body", 400)

    message = _clean_single_line(body.get("message"), 512)
    source = _clean_single_line(body.get("source"), 512)
    url = _clean_single_line(body.get("url"), 512)
    ua = _clean_single_line(body.get("ua"), 512)
    line = _clean_single_line(body.get("line"), 16)
    col = _clean_single_line(body.get("col"), 16)
    # Stack may legitimately be multi-line; only truncate (control chars kept).
    stack = (str(body.get("stack")) if body.get("stack") is not None else "")[:4096]

    log.warning("Client JS error from %s: %s @ %s:%s", ip, message, source, line)
    if stack:
        log.debug(
            "Client JS error stack from %s (col=%s, url=%s, ua=%s):\n%s",
            ip,
            col,
            url,
            ua,
            stack,
        )

    return _ok({})
