"""Auto-reply to Meshtastic direct messages with configurable commands.

Subscribes to ``MESHTASTIC_MESSAGE_RECEIVED`` events on the event bus and
sends replies through the gateway's ``send_message()`` API.  Supports
prefix-based commands (``!ping``, ``!weather Austin``, etc.) and custom
keyword-to-response mappings.

Requires the ``meshtastic_gateway`` plugin to be enabled.
"""

from __future__ import annotations

import ast
import json
import math
import operator
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

# ── Constants ────────────────────────────────────────────────────────

_MESHTASTIC_MTU = 237  # max payload bytes per Meshtastic text message
_HTTP_TIMEOUT = 10  # seconds for external API calls
_COOLDOWN_PRUNE_THRESHOLD = 256  # prune cooldown dict when it exceeds this

# WMO Weather Interpretation Codes
# https://open-meteo.com/en/docs
_WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Foggy",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm w/ slight hail",
    99: "Thunderstorm w/ heavy hail",
}

_FORTUNES = [
    "A journey of a thousand miles begins with a single packet.",
    "In the mesh we trust.",
    "Fortune favors the connected.",
    "Not all who wander are lost — some are just looking for better signal.",
    "Any sufficiently advanced technology is indistinguishable from magic.",
    "Talk is cheap. Show me the code. — Linus Torvalds",
    "The network is the computer. — John Gage",
    "Packets speak louder than words.",
    "It works on my mesh.",
    "Have you tried turning it off and on again?",
    "May your signal be strong and your latency low.",
    "Keep calm and mesh on.",
    "Every great mesh begins with a single link.",
    "In RF we trust; all others bring coax.",
    "73 — may your mesh be wide and your hops be few.",
]

# ── Safe math evaluator ──────────────────────────────────────────────

_SAFE_MATH_OPS: dict[type, Callable] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_SAFE_MATH_FUNCS: dict[str, Any] = {
    "sqrt": math.sqrt,
    "abs": abs,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "pi": math.pi,
    "e": math.e,
}


def _safe_eval(node: ast.AST) -> int | float:
    """Recursively evaluate an AST node using only safe math operations."""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_MATH_OPS:
        return _SAFE_MATH_OPS[type(node.op)](_safe_eval(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_MATH_OPS:
        left = _safe_eval(node.left)
        right = _safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and right > 1000:
            raise ValueError("Exponent too large")
        return _SAFE_MATH_OPS[type(node.op)](left, right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        fname = node.func.id
        if fname in _SAFE_MATH_FUNCS and callable(_SAFE_MATH_FUNCS[fname]):
            args = [_safe_eval(a) for a in node.args]
            return _SAFE_MATH_FUNCS[fname](*args)
        raise ValueError(f"Unknown function: {fname}")
    if isinstance(node, ast.Name) and node.id in _SAFE_MATH_FUNCS:
        val = _SAFE_MATH_FUNCS[node.id]
        if not callable(val):
            return val
    raise ValueError("Unsupported expression")


# ── Known built-in command names (for config validation) ─────────────

_ALL_COMMANDS = frozenset({
    "help", "ping", "time", "uptime", "nodes",
    "weather", "fortune", "dice", "flip", "calc",
})


# ── Plugin ───────────────────────────────────────────────────────────


class MeshtasticResponder(PluginBase):
    """Auto-replies to Meshtastic DMs with configurable commands."""

    plugin_name = "meshtastic_responder"
    plugin_version = "1.0.0"
    plugin_description = "Auto-replies to Meshtastic DMs with configurable commands"

    # ── Lifecycle ─────────────────────────────────────────────────

    def validate_config(self) -> None:
        prefix = self.config.get("prefix", "!")
        if not isinstance(prefix, str) or not prefix:
            raise ValueError("meshtastic_responder: 'prefix' must be a non-empty string")

        cooldown = self.config.get("cooldown_seconds", 30)
        if not isinstance(cooldown, (int, float)) or cooldown < 0:
            raise ValueError(
                "meshtastic_responder: 'cooldown_seconds' must be a non-negative number"
            )

        commands = self.config.get("commands", [])
        if commands:
            unknown = set(commands) - _ALL_COMMANDS
            if unknown:
                raise ValueError(
                    f"meshtastic_responder: unknown commands: {sorted(unknown)}"
                )

        custom = self.config.get("custom_responses", {})
        if not isinstance(custom, dict):
            raise ValueError(
                "meshtastic_responder: 'custom_responses' must be a mapping"
            )

    def start(self) -> None:
        self._prefix: str = self.config.get("prefix", "!")
        self._respond_to_broadcast: bool = self.config.get(
            "respond_to_broadcast", False
        )
        self._cooldown_seconds: float = float(
            self.config.get("cooldown_seconds", 30)
        )
        self._reply_via: str = self.config.get("reply_via", "")

        # Custom responses — lowercased keys for case-insensitive matching
        raw_custom = self.config.get("custom_responses", {})
        self._custom_responses: dict[str, str] = {
            k.lower(): v for k, v in raw_custom.items()
        }

        # Build command registry from enabled commands list
        enabled = self.config.get("commands", [])
        self._commands = self._build_command_registry(enabled)

        # Per-node cooldown tracking
        self._lock = threading.Lock()
        self._node_cooldowns: dict[str, float] = {}

        # Counters for status reporting
        self._msgs_handled = 0
        self._msgs_cooldown_skipped = 0
        self._start_time = time.monotonic()

        # Cache for the gateway's own node ID (self-reply guard)
        self._own_node_id: str | None = None

        self.event_bus.subscribe(
            events.MESHTASTIC_MESSAGE_RECEIVED, self._on_mesh_message
        )
        self._active = True
        self.log.info(
            "Meshtastic responder active — %d commands, %d custom responses",
            len(self._commands),
            len(self._custom_responses),
        )

    def stop(self) -> None:
        self._active = False
        self.event_bus.unsubscribe(
            events.MESHTASTIC_MESSAGE_RECEIVED, self._on_mesh_message
        )
        with self._lock:
            self._node_cooldowns.clear()
        self.log.info(
            "Meshtastic responder stopped — handled %d, cooldown-skipped %d",
            self._msgs_handled,
            self._msgs_cooldown_skipped,
        )

    def get_status(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "msgs_handled": self._msgs_handled,
            "msgs_cooldown_skipped": self._msgs_cooldown_skipped,
            "enabled_commands": sorted(self._commands.keys()),
            "custom_responses": len(self._custom_responses),
        }

    # ── Command registry ──────────────────────────────────────────

    def _build_command_registry(
        self, enabled: list[str]
    ) -> dict[str, tuple[Callable[..., str], str]]:
        """Build the command dict from the configured enabled list.

        If *enabled* is empty, all built-in commands are enabled.
        """
        all_handlers: dict[str, tuple[Callable[..., str], str]] = {
            "help": (self._cmd_help, "Show available commands"),
            "ping": (self._cmd_ping, "Check if the node is alive"),
            "time": (self._cmd_time, "Current time (!time [timezone])"),
            "uptime": (self._cmd_uptime, "Bot and system uptime"),
            "nodes": (self._cmd_nodes, "Known Meshtastic nodes"),
            "weather": (self._cmd_weather, "Weather (!weather <city>)"),
            "fortune": (self._cmd_fortune, "Random fortune quote"),
            "dice": (self._cmd_dice, "Roll dice (!dice 2d6)"),
            "flip": (self._cmd_flip, "Flip a coin"),
            "calc": (self._cmd_calc, "Math (!calc 2+2)"),
        }
        if not enabled:
            return all_handlers
        return {k: v for k, v in all_handlers.items() if k in enabled}

    # ── Event handler ─────────────────────────────────────────────

    def _on_mesh_message(self, event_type: str, data: dict[str, Any]) -> None:
        """Event bus callback for incoming Meshtastic messages."""
        from_id = data.get("from_id", "")
        is_broadcast = data.get("is_broadcast", True)
        text = data.get("text", "").strip()

        if not text or not from_id:
            return
        if is_broadcast and not self._respond_to_broadcast:
            return

        # Self-reply guard — don't respond to our own node
        if self._is_own_node(from_id):
            return

        # Per-node cooldown
        if not self._check_cooldown(from_id):
            self._msgs_cooldown_skipped += 1
            return

        response = self._match_and_respond(text)
        if response:
            self._msgs_handled += 1
            truncated = _truncate_response(response)
            self._send_reply(truncated, from_id)

    def _is_own_node(self, node_id: str) -> bool:
        """Check if *node_id* is our own gateway node (prevent self-reply loops)."""
        if self._own_node_id is not None:
            return node_id == self._own_node_id
        try:
            gw = self.app.get_plugin("meshtastic_gateway")
            if gw and hasattr(gw, "get_status"):
                status = gw.get_status()
                self._own_node_id = status.get("node_id", "")
                return node_id == self._own_node_id
        except Exception:
            pass
        return False

    def _check_cooldown(self, node_id: str) -> bool:
        """Return True if the node is allowed to trigger a response.

        Updates the cooldown timestamp on success.  Prunes expired entries
        when the dict grows too large.
        """
        if self._cooldown_seconds <= 0:
            return True

        now = time.time()
        with self._lock:
            last = self._node_cooldowns.get(node_id, 0.0)
            if now - last < self._cooldown_seconds:
                return False
            self._node_cooldowns[node_id] = now

            # Prune expired entries to prevent unbounded growth
            if len(self._node_cooldowns) > _COOLDOWN_PRUNE_THRESHOLD:
                cutoff = now - self._cooldown_seconds
                self._node_cooldowns = {
                    nid: ts
                    for nid, ts in self._node_cooldowns.items()
                    if ts > cutoff
                }
        return True

    def _match_and_respond(self, text: str) -> str | None:
        """Try custom responses first, then command prefix, else None."""
        # Check custom responses (case-insensitive on full stripped text)
        lower_text = text.lower().strip()
        if lower_text in self._custom_responses:
            return self._custom_responses[lower_text]

        # Check command prefix
        if text.startswith(self._prefix):
            return self._route_command(text)

        return None

    def _route_command(self, text: str) -> str:
        """Parse the prefix command and dispatch to the handler."""
        without_prefix = text[len(self._prefix):]
        parts = without_prefix.split(None, 1)
        if not parts:
            return self._cmd_help()

        cmd_name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        handler_entry = self._commands.get(cmd_name)
        if handler_entry is None:
            return f"Unknown: {self._prefix}{cmd_name}\nSend {self._prefix}help"

        handler, _description = handler_entry
        return handler(args)

    def _send_reply(self, text: str, destination_id: str) -> None:
        """Send a reply through the Meshtastic gateway."""
        try:
            gw = self.app.get_plugin("meshtastic_gateway")
            if not gw or not hasattr(gw, "send_message"):
                self.log.warning(
                    "Cannot send reply — meshtastic_gateway plugin not available"
                )
                return
            result = gw.send_message(
                text, destination_id=destination_id, via=self._reply_via
            )
            if result.get("sent"):
                self.log.debug("Reply sent to %s (%d bytes)", destination_id, len(text))
            else:
                self.log.warning(
                    "Reply to %s failed: %s",
                    destination_id,
                    result.get("reason", "unknown"),
                )
        except Exception:
            self.log.exception("Error sending reply to %s", destination_id)

    # ── Commands ──────────────────────────────────────────────────

    def _cmd_help(self, _args: str = "") -> str:
        """List enabled commands with descriptions."""
        lines = [f"{self.app.node_name} Mesh Responder"]
        for name, (_handler, desc) in sorted(self._commands.items()):
            lines.append(f" {self._prefix}{name} - {desc}")
        if self._custom_responses:
            lines.append(f" + {len(self._custom_responses)} keyword(s)")
        return "\n".join(lines)

    def _cmd_ping(self, _args: str = "") -> str:
        """Simple alive check with uptime."""
        elapsed = time.monotonic() - self._start_time
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)
        return f"Pong! Uptime: {hours}h {minutes}m"

    def _cmd_time(self, args: str = "") -> str:
        """Return current time, optionally in a named timezone."""
        tz_name = args.strip()
        if not tz_name:
            now = datetime.now(timezone.utc)
            return f"UTC: {now.strftime('%Y-%m-%d %H:%M:%S')}"

        try:
            import zoneinfo
        except ImportError:
            return f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"

        tz_aliases = {
            "EST": "US/Eastern", "EDT": "US/Eastern",
            "CST": "US/Central", "CDT": "US/Central",
            "MST": "US/Mountain", "MDT": "US/Mountain",
            "PST": "US/Pacific", "PDT": "US/Pacific",
            "GMT": "GMT", "CET": "CET", "EET": "EET",
            "JST": "Asia/Tokyo", "IST": "Asia/Kolkata",
            "AEST": "Australia/Sydney", "AEDT": "Australia/Sydney",
            "NZST": "Pacific/Auckland", "NZDT": "Pacific/Auckland",
        }
        resolved = tz_aliases.get(tz_name.upper(), tz_name)

        try:
            tz = zoneinfo.ZoneInfo(resolved)
            now = datetime.now(tz)
            return f"{resolved}: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        except (KeyError, Exception):
            return f"Unknown timezone: {tz_name}\nExamples: UTC, PST, Europe/London"

    def _cmd_uptime(self, _args: str = "") -> str:
        """Bot uptime and system uptime."""
        bot_elapsed = time.monotonic() - self._start_time
        bh = int(bot_elapsed // 3600)
        bm = int((bot_elapsed % 3600) // 60)

        try:
            with open("/proc/uptime") as f:
                sys_secs = float(f.read().split()[0])
            sd = int(sys_secs // 86400)
            sh = int((sys_secs % 86400) // 3600)
            sm = int((sys_secs % 3600) // 60)
            sys_str = f"{sd}d {sh}h {sm}m"
        except (OSError, ValueError):
            sys_str = "unavailable"

        return f"Bot: {bh}h {bm}m\nSystem: {sys_str}"

    def _cmd_nodes(self, _args: str = "") -> str:
        """Show known Meshtastic node count and top entries."""
        try:
            gw = self.app.get_plugin("meshtastic_gateway")
            if not gw or not hasattr(gw, "get_meshtastic_nodes"):
                return "Meshtastic gateway not available."
            nodes = gw.get_meshtastic_nodes()
            if not nodes:
                return "No Meshtastic nodes known."

            total = len(nodes)
            # Sort by last_heard descending (most recent first)
            with_heard = [
                n for n in nodes if n.get("last_heard")
            ]
            with_heard.sort(key=lambda n: n["last_heard"], reverse=True)

            lines = [f"Nodes: {total} known"]
            # Show top 5 most recently heard
            for n in with_heard[:5]:
                name = n.get("long_name") or n.get("short_name") or n.get("id", "?")
                lines.append(f" {name}")
            if total > 5:
                lines.append(f" ...and {total - 5} more")
            return "\n".join(lines)
        except Exception as exc:
            return f"Error querying nodes: {exc}"

    def _cmd_weather(self, args: str = "") -> str:
        """Fetch current weather for a location via Open-Meteo."""
        location = args.strip()
        if not location:
            return f"Usage: {self._prefix}weather <city>\nExample: {self._prefix}weather London"

        try:
            parts = [p.strip() for p in location.split(",")]
            city_name = parts[0]
            filter_terms = [p.lower() for p in parts[1:] if p]

            geo_url = (
                "https://geocoding-api.open-meteo.com/v1/search?"
                + urllib.parse.urlencode({"name": city_name, "count": 10})
            )
            geo_data = _fetch_json(geo_url)

            results = geo_data.get("results")
            if not results:
                return f"Location not found: {location}"

            place = results[0]
            if filter_terms:
                for r in results:
                    searchable = " ".join([
                        r.get("admin1", ""),
                        r.get("admin2", ""),
                        r.get("country", ""),
                        r.get("country_code", ""),
                    ]).lower()
                    if all(term in searchable for term in filter_terms):
                        place = r
                        break

            lat = place["latitude"]
            lon = place["longitude"]
            place_name = place.get("name", location)
            country = place.get("country", "")
            admin1 = place.get("admin1", "")

            weather_url = (
                "https://api.open-meteo.com/v1/forecast?"
                + urllib.parse.urlencode({
                    "latitude": lat,
                    "longitude": lon,
                    "current": (
                        "temperature_2m,relative_humidity_2m,"
                        "wind_speed_10m,weather_code"
                    ),
                    "temperature_unit": "fahrenheit",
                    "wind_speed_unit": "mph",
                })
            )
            weather_data = _fetch_json(weather_url)
            current = weather_data.get("current", {})

            temp_f = current.get("temperature_2m")
            humidity = current.get("relative_humidity_2m")
            wind_mph = current.get("wind_speed_10m")
            weather_code = current.get("weather_code", -1)
            temp_c = (
                round((temp_f - 32) * 5 / 9, 1) if temp_f is not None else None
            )
            conditions = _WMO_CODES.get(weather_code, "Unknown")

            loc_parts = [place_name]
            if admin1:
                loc_parts.append(admin1)
            if country:
                loc_parts.append(country)
            loc_label = ", ".join(loc_parts)

            lines = [f"Weather: {loc_label}", f" {conditions}"]
            if temp_f is not None:
                lines.append(f" {temp_f}F ({temp_c}C)")
            if humidity is not None:
                lines.append(f" Humidity: {humidity}%")
            if wind_mph is not None:
                lines.append(f" Wind: {wind_mph} mph")
            return "\n".join(lines)

        except urllib.error.URLError:
            return "Weather fetch failed (network error)."
        except (KeyError, TypeError, ValueError):
            return f"Could not parse weather for: {location}"

    def _cmd_fortune(self, _args: str = "") -> str:
        """Return a random fortune."""
        return random.choice(_FORTUNES)

    def _cmd_dice(self, args: str = "") -> str:
        """Roll dice in NdM format (e.g., 2d6, 1d20)."""
        args = args.strip().lower()
        if not args:
            args = "1d6"
        try:
            if "d" not in args:
                return f"Usage: {self._prefix}dice NdM (e.g. 2d6)"
            n_str, m_str = args.split("d", 1)
            n = int(n_str) if n_str else 1
            m = int(m_str)
            if n < 1 or n > 100 or m < 2 or m > 1000:
                return "Limits: 1-100 dice, 2-1000 sides."
            rolls = [random.randint(1, m) for _ in range(n)]
            total = sum(rolls)
            if n == 1:
                return f"Rolling 1d{m}: {rolls[0]}"
            rolls_str = ", ".join(str(r) for r in rolls)
            return f"Rolling {n}d{m}: [{rolls_str}] = {total}"
        except (ValueError, OverflowError):
            return f"Usage: {self._prefix}dice NdM (e.g. 2d6)"

    def _cmd_flip(self, _args: str = "") -> str:
        """Flip a coin."""
        return random.choice(["Heads!", "Tails!"])

    def _cmd_calc(self, args: str = "") -> str:
        """Safely evaluate a math expression."""
        expr = args.strip()
        if not expr:
            return f"Usage: {self._prefix}calc <expression>\nExample: {self._prefix}calc 2**10 + sqrt(144)"
        try:
            tree = ast.parse(expr, mode="eval")
            result = _safe_eval(tree)
            if isinstance(result, float) and result == int(result) and abs(result) < 1e15:
                result = int(result)
            return f"{expr} = {result}"
        except (ValueError, TypeError, SyntaxError, ZeroDivisionError) as exc:
            return f"Error: {exc}"


# ── Module-level helpers ─────────────────────────────────────────────


def _truncate_response(text: str, max_bytes: int = _MESHTASTIC_MTU) -> str:
    """Truncate *text* to fit within *max_bytes* of UTF-8.

    Cuts at the last word boundary before the limit and appends a
    ``...(more)`` suffix when truncation occurs.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text

    suffix = b"\n...(more)"
    limit = max_bytes - len(suffix)
    if limit <= 0:
        return text[:max_bytes]

    # Truncate encoded bytes, then decode safely
    truncated = encoded[:limit]
    # Back up to last space or newline to avoid mid-word cut
    last_break = max(truncated.rfind(b" "), truncated.rfind(b"\n"))
    if last_break > limit // 2:
        truncated = truncated[:last_break]

    return truncated.decode("utf-8", errors="ignore") + suffix.decode("utf-8")


def _fetch_json(url: str) -> dict:
    """Fetch a URL and parse the JSON response."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "ReticulumPi-MeshResponder/1.0"}
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))
