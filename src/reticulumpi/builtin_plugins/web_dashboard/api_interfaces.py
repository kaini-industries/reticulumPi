"""API route handlers for Reticulum network interface management."""

from __future__ import annotations

import aiohttp.web

from reticulumpi.builtin_plugins.web_dashboard.api import _error, _get_plugin, _ok


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


def _rns_config_path(plugin) -> str:
    """Resolve the path to the Reticulum config file."""
    import os

    config_dir = getattr(plugin.app, "_reticulum_config_dir", None)
    if not config_dir:
        config_dir = os.path.expanduser("~/.reticulum")
    return os.path.join(config_dir, "config")


# ── Route handlers ──────────────────────────────────────────────────


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
    import re
    if not re.match(r'^[A-Za-z0-9 _-]{1,100}$', iface_name):
        return _error("Interface name contains invalid characters", 400)
    for k, v in properties.items():
        if '\n' in str(k) or '\n' in str(v) or '\r' in str(k) or '\r' in str(v):
            return _error("Properties must not contain newlines", 400)

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


def setup_interface_routes(app: aiohttp.web.Application) -> None:
    """Register interface management API routes."""
    app.router.add_get("/api/interfaces", handle_interfaces)
    app.router.add_get("/api/interfaces/config", handle_interfaces_config)
    app.router.add_post("/api/interfaces/{name:.+}/toggle", handle_interface_toggle)
    app.router.add_post("/api/interfaces/add", handle_interface_add)
