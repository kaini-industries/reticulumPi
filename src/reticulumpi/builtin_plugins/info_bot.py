"""Info Bot plugin - responds to LXMF commands with internet-sourced information."""

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request

import LXMF
import RNS
import RNS.vendor.umsgpack as umsgpack

from reticulumpi.plugin_base import PluginBase

# WMO Weather Interpretation Codes (WW)
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
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}

_HTTP_TIMEOUT = 10  # seconds


class _PropagationAnnounceHandler:
    """RNS announce handler that auto-selects the nearest LXMF propagation node."""

    def __init__(self, plugin: "InfoBot"):
        self.aspect_filter = "lxmf.propagation"
        self._plugin = plugin

    def received_announce(self, destination_hash, announced_identity, app_data):
        self._plugin._handle_propagation_announce(
            destination_hash, announced_identity, app_data
        )


class InfoBot(PluginBase):
    """Responds to LXMF command messages with information from the internet."""

    plugin_name = "info_bot"
    plugin_description = "Responds to LXMF commands with internet-sourced information"
    plugin_version = "1.0.0"

    # Command prefix
    PREFIX = "!"

    def start(self) -> None:
        self._lock = threading.Lock()
        default_storage = "~/.local/share/reticulumpi/info_bot_lxmf"
        storage_path = os.path.expanduser(
            self.config.get("storage_path", default_storage)
        )
        os.makedirs(storage_path, exist_ok=True)

        # Info Bot needs its own identity so it gets a unique LXMF address
        # (the shared node identity is already registered by message_echo).
        identity_path = os.path.join(storage_path, "identity")
        if os.path.isfile(identity_path):
            self._bot_identity = RNS.Identity.from_file(identity_path)
            self.log.debug("Loaded Info Bot identity from %s", identity_path)
        else:
            self._bot_identity = RNS.Identity()
            self._bot_identity.to_file(identity_path)
            self.log.info("Created new Info Bot identity at %s", identity_path)

        self.lxmf_router = LXMF.LXMRouter(storagepath=storage_path)
        self.local_lxmf_destination = self.lxmf_router.register_delivery_identity(
            self._bot_identity,
            display_name=self.config.get("display_name")
            or f"{self.app.node_name} Info",
        )
        self.lxmf_router.register_delivery_callback(self._handle_message)

        # Auto-select the nearest LXMF propagation node for store-and-forward
        self._best_propagation_hops = RNS.Transport.PATHFINDER_M + 1
        self._propagation_handler = _PropagationAnnounceHandler(self)
        RNS.Transport.register_announce_handler(self._propagation_handler)

        # Command registry — maps command name to (handler, description)
        self._commands = {
            "weather": (self._cmd_weather, "Get current weather for a location"),
            "help": (self._cmd_help, "Show available commands"),
        }

        self._active = True
        self.log.info(
            "LXMF Info Bot active at %s",
            RNS.prettyhexrep(self.local_lxmf_destination.hash),
        )

    def stop(self) -> None:
        self._active = False
        RNS.Transport.deregister_announce_handler(self._propagation_handler)
        self.lxmf_router.register_delivery_callback(None)
        self._join_threads()

    # ── Message handling ─────────────────────────────────────────────

    def _handle_message(self, message: LXMF.LXMessage) -> None:
        with self._lock:
            if not self._active:
                return
            try:
                sender = RNS.prettyhexrep(message.source_hash)
                content = message.content_as_string().strip()
                self.log.info("Received message from %s: %s", sender, content[:100])

                response = self._route_command(content)

                reply = LXMF.LXMessage(
                    message.source,
                    self.local_lxmf_destination,
                    response,
                    desired_method=LXMF.LXMessage.DIRECT,
                )
                self.lxmf_router.handle_outbound(reply)
                self.log.debug("Sent reply to %s", sender)
            except Exception:
                self.log.exception("Error handling LXMF message")

    def _route_command(self, content: str) -> str:
        """Parse and route a command, returning the response text."""
        if not content.startswith(self.PREFIX):
            return self._cmd_help()

        # Split "!weather Austin, TX" into ("weather", "Austin, TX")
        without_prefix = content[len(self.PREFIX) :]
        parts = without_prefix.split(None, 1)
        if not parts:
            return self._cmd_help()

        cmd_name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        handler_entry = self._commands.get(cmd_name)
        if handler_entry is None:
            return f"Unknown command: !{cmd_name}\n\n{self._cmd_help()}"

        handler, _description = handler_entry
        return handler(args)

    # ── Commands ─────────────────────────────────────────────────────

    def _cmd_help(self, _args: str = "") -> str:
        """Return a help message listing all available commands."""
        lines = [f"{self.app.node_name} Info Bot", ""]
        lines.append("Available commands:")
        for name, (_handler, description) in sorted(self._commands.items()):
            lines.append(f"  !{name} — {description}")
        lines.append("")
        lines.append("Example: !weather Austin, TX")
        return "\n".join(lines)

    def _cmd_weather(self, args: str) -> str:
        """Fetch current weather for a location using the Open-Meteo API."""
        location = args.strip()
        if not location:
            return "Usage: !weather <city name>\nExample: !weather London"

        try:
            # Step 1: Geocode the location name to coordinates.
            # Open-Meteo's "name" param works best with just the city name,
            # so split "Madison, WI" into city="Madison" + filter="WI".
            parts = [p.strip() for p in location.split(",")]
            city_name = parts[0]
            filter_terms = [p.lower() for p in parts[1:] if p]

            geo_url = (
                "https://geocoding-api.open-meteo.com/v1/search?"
                + urllib.parse.urlencode({"name": city_name, "count": 10})
            )
            geo_data = self._fetch_json(geo_url)

            results = geo_data.get("results")
            if not results:
                return f"Location not found: {location}"

            # If the user provided extra terms (state, country), filter results
            place = results[0]  # default to top result
            if filter_terms:
                for r in results:
                    # Check if any filter term matches admin1, admin2, or country
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
            admin1 = place.get("admin1", "")  # state/region

            # Step 2: Fetch current weather
            weather_url = (
                "https://api.open-meteo.com/v1/forecast?"
                + urllib.parse.urlencode(
                    {
                        "latitude": lat,
                        "longitude": lon,
                        "current": "temperature_2m,relative_humidity_2m,"
                        "wind_speed_10m,weather_code",
                        "temperature_unit": "fahrenheit",
                        "wind_speed_unit": "mph",
                    }
                )
            )
            weather_data = self._fetch_json(weather_url)

            current = weather_data.get("current", {})
            temp_f = current.get("temperature_2m")
            humidity = current.get("relative_humidity_2m")
            wind_mph = current.get("wind_speed_10m")
            weather_code = current.get("weather_code", -1)

            # Convert F to C for dual display
            temp_c = round((temp_f - 32) * 5 / 9, 1) if temp_f is not None else None

            conditions = _WMO_CODES.get(weather_code, "Unknown")

            # Build location label
            location_parts = [place_name]
            if admin1:
                location_parts.append(admin1)
            if country:
                location_parts.append(country)
            location_label = ", ".join(location_parts)

            lines = [
                f"Weather for {location_label}",
                f"  Conditions: {conditions}",
            ]
            if temp_f is not None:
                lines.append(f"  Temperature: {temp_f}°F ({temp_c}°C)")
            if humidity is not None:
                lines.append(f"  Humidity: {humidity}%")
            if wind_mph is not None:
                lines.append(f"  Wind: {wind_mph} mph")

            return "\n".join(lines)

        except urllib.error.URLError as exc:
            self.log.warning("Weather fetch failed for %r: %s", location, exc)
            return f"Could not fetch weather (network error). Try again later."
        except (KeyError, TypeError, ValueError) as exc:
            self.log.warning("Weather parse error for %r: %s", location, exc)
            return f"Could not parse weather data for: {location}"

    # ── Helpers ───────────────────────────────────────────────────────

    def _fetch_json(self, url: str) -> dict:
        """Fetch a URL and parse the JSON response."""
        req = urllib.request.Request(
            url, headers={"User-Agent": "ReticulumPi-InfoBot/1.0"}
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ── Propagation node handling (same pattern as message_echo) ─────

    def _handle_propagation_announce(
        self, destination_hash, announced_identity, app_data
    ):
        """Auto-select the nearest active propagation node."""
        try:
            if not app_data:
                return

            from LXMF import pn_announce_data_is_valid

            if not pn_announce_data_is_valid(app_data):
                return

            data = umsgpack.unpackb(app_data)
            if not (len(data) >= 3 and data[2] is True):
                return

            hops = RNS.Transport.hops_to(destination_hash)
            with self._lock:
                if hops < self._best_propagation_hops:
                    self._best_propagation_hops = hops
                    self.lxmf_router.set_outbound_propagation_node(destination_hash)
                    self.log.info(
                        "Auto-selected propagation node %s (%d hops)",
                        RNS.prettyhexrep(destination_hash),
                        hops,
                    )
        except Exception:
            self.log.exception("Error handling propagation node announce")
