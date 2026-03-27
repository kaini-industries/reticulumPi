"""Info Bot plugin - responds to LXMF commands with internet-sourced information."""

import ast
import json
import math
import operator
import os
import random
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import LXMF
import RNS
import RNS.vendor.umsgpack as umsgpack

from reticulumpi.plugin_base import PluginBase

# ── Fortunes ─────────────────────────────────────────────────────────
_FORTUNES = [
    "The best time to plant a tree was 20 years ago. The second best time is now.",
    "A journey of a thousand miles begins with a single packet.",
    "In the mesh we trust.",
    "Fortune favors the connected.",
    "The only way to do great work is to love what you do. — Steve Jobs",
    "Not all who wander are lost — some are just looking for better signal.",
    "There are 10 types of people: those who understand binary and those who don't.",
    "Any sufficiently advanced technology is indistinguishable from magic. — Arthur C. Clarke",
    "Talk is cheap. Show me the code. — Linus Torvalds",
    "The network is the computer. — John Gage",
    "Packets speak louder than words.",
    "First, solve the problem. Then, write the code. — John Johnson",
    "Simplicity is the ultimate sophistication. — Leonardo da Vinci",
    "It works on my mesh.",
    "Have you tried turning it off and on again?",
    "The mesh is dark and full of packets.",
    "May your signal be strong and your latency low.",
    "A good node is a reachable node.",
    "Propagation waits for no one.",
    "Keep calm and mesh on.",
    "Bandwidth is temporary, but uptime is forever.",
    "Every great mesh begins with a single link.",
    "In RF we trust; all others bring coax.",
    "73 de InfoBot — may your mesh be wide and your hops be few.",
]

# ── Safe math evaluator ──────────────────────────────────────────────
_SAFE_MATH_OPS = {
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

_SAFE_MATH_FUNCS = {
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


def _safe_eval(node):
    """Recursively evaluate an AST node using only safe math operations."""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    elif isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    elif isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_MATH_OPS:
        return _SAFE_MATH_OPS[type(node.op)](_safe_eval(node.operand))
    elif isinstance(node, ast.BinOp) and type(node.op) in _SAFE_MATH_OPS:
        left = _safe_eval(node.left)
        right = _safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and right > 1000:
            raise ValueError("Exponent too large")
        return _SAFE_MATH_OPS[type(node.op)](left, right)
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        fname = node.func.id
        if fname in _SAFE_MATH_FUNCS and callable(_SAFE_MATH_FUNCS[fname]):
            args = [_safe_eval(a) for a in node.args]
            return _SAFE_MATH_FUNCS[fname](*args)
        raise ValueError(f"Unknown function: {fname}")
    elif isinstance(node, ast.Name) and node.id in _SAFE_MATH_FUNCS:
        val = _SAFE_MATH_FUNCS[node.id]
        if not callable(val):
            return val
    raise ValueError("Unsupported expression")

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
    plugin_version = "2.0.0"

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

        # Record start time for uptime tracking
        self._start_time = time.monotonic()

        # Command registry — maps command name to (handler, description)
        self._commands = {
            "help": (self._cmd_help, "Show available commands"),
            "ping": (self._cmd_ping, "Check if the bot is alive"),
            "time": (self._cmd_time, "Current time (!time [timezone])"),
            "uptime": (self._cmd_uptime, "Node uptime and system stats"),
            "peers": (self._cmd_peers, "Show Reticulum network peers"),
            "nodes": (self._cmd_nodes, "Show known transport nodes"),
            "weather": (self._cmd_weather, "Get current weather for a location"),
            "fortune": (self._cmd_fortune, "Random fortune cookie quote"),
            "dice": (self._cmd_dice, "Roll dice (!dice 2d6)"),
            "flip": (self._cmd_flip, "Flip a coin"),
            "calc": (self._cmd_calc, "Evaluate math (!calc 2+2)"),
            "define": (self._cmd_define, "Dictionary lookup (!define word)"),
            "news": (self._cmd_news, "Latest headlines (!news [topic])"),
            "iss": (self._cmd_iss, "Current ISS position"),
            "crypto": (self._cmd_crypto, "Crypto price (!crypto BTC)"),
            "joke": (self._cmd_joke, "Random joke"),
            "solar": (self._cmd_solar, "Solar/geomagnetic conditions"),
            "grid": (self._cmd_grid, "Maidenhead grid converter"),
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
        lines = [f"{self.app.node_name} Info Bot v{self.plugin_version}", ""]
        lines.append("Available commands:")
        for name, (_handler, description) in sorted(self._commands.items()):
            lines.append(f"  !{name} — {description}")
        lines.append("")
        lines.append("Examples:")
        lines.append("  !weather Austin, TX")
        lines.append("  !dice 2d6")
        lines.append("  !define serendipity")
        lines.append("  !grid EM10")
        return "\n".join(lines)

    def _cmd_ping(self, _args: str = "") -> str:
        """Simple alive check."""
        uptime_s = int(time.monotonic() - self._start_time)
        h, rem = divmod(uptime_s, 3600)
        m, s = divmod(rem, 60)
        return f"Pong! Bot uptime: {h}h {m}m {s}s"

    def _cmd_time(self, args: str = "") -> str:
        """Return current time, optionally in a given timezone."""
        import zoneinfo

        tz_name = args.strip()
        if not tz_name:
            now = datetime.now(timezone.utc)
            return f"UTC: {now.strftime('%Y-%m-%d %H:%M:%S')}"

        # Try common abbreviations
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
        except (zoneinfo.ZoneInfoNotFoundError, KeyError):
            return f"Unknown timezone: {tz_name}\nExamples: UTC, US/Eastern, PST, Europe/London"

    def _cmd_uptime(self, _args: str = "") -> str:
        """Return node uptime and system stats."""
        lines = []

        # Bot uptime
        uptime_s = int(time.monotonic() - self._start_time)
        h, rem = divmod(uptime_s, 3600)
        m, s = divmod(rem, 60)
        lines.append(f"Bot uptime: {h}h {m}m {s}s")

        # System uptime from /proc/uptime
        try:
            with open("/proc/uptime") as f:
                sys_up = float(f.read().split()[0])
            days, remainder = divmod(int(sys_up), 86400)
            hours, remainder = divmod(remainder, 3600)
            mins, _ = divmod(remainder, 60)
            lines.append(f"System uptime: {days}d {hours}h {mins}m")
        except (OSError, ValueError):
            pass

        # Load average
        try:
            with open("/proc/loadavg") as f:
                parts = f.read().split()
            lines.append(f"Load: {parts[0]} {parts[1]} {parts[2]}")
        except (OSError, IndexError):
            pass

        # Memory
        try:
            with open("/proc/meminfo") as f:
                meminfo = {}
                for line in f:
                    k, v = line.split(":", 1)
                    meminfo[k.strip()] = int(v.strip().split()[0])
            total_mb = meminfo["MemTotal"] // 1024
            avail_mb = meminfo.get("MemAvailable", meminfo.get("MemFree", 0)) // 1024
            used_mb = total_mb - avail_mb
            lines.append(f"Memory: {used_mb}/{total_mb} MB used")
        except (OSError, KeyError, ValueError):
            pass

        # Disk usage for root
        try:
            st = os.statvfs("/")
            total_gb = (st.f_blocks * st.f_frsize) / (1024 ** 3)
            free_gb = (st.f_bavail * st.f_frsize) / (1024 ** 3)
            used_gb = total_gb - free_gb
            lines.append(f"Disk: {used_gb:.1f}/{total_gb:.1f} GB used")
        except OSError:
            pass

        # CPU temperature (Raspberry Pi)
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                temp_c = int(f.read().strip()) / 1000
            lines.append(f"CPU temp: {temp_c:.1f}°C ({temp_c * 9/5 + 32:.1f}°F)")
        except (OSError, ValueError):
            pass

        return "\n".join(lines) if lines else "No system stats available."

    def _cmd_peers(self, _args: str = "") -> str:
        """Show Reticulum network peers via rnstatus."""
        try:
            result = subprocess.run(
                ["/opt/reticulumpi/.venv/bin/rnstatus"],
                capture_output=True, text=True, timeout=10,
                env={**os.environ, "HOME": "/home/reticulumpi"},
            )
            output = result.stdout.strip()
            if not output:
                return "No peer information available."
            # Truncate if too long for LXMF
            if len(output) > 1500:
                output = output[:1500] + "\n... (truncated)"
            return output
        except FileNotFoundError:
            return "rnstatus not found."
        except subprocess.TimeoutExpired:
            return "rnstatus timed out."
        except Exception as exc:
            return f"Error running rnstatus: {exc}"

    def _cmd_nodes(self, _args: str = "") -> str:
        """Show known transport nodes via rnpath."""
        try:
            result = subprocess.run(
                ["/opt/reticulumpi/.venv/bin/rnstatus", "-A"],
                capture_output=True, text=True, timeout=10,
                env={**os.environ, "HOME": "/home/reticulumpi"},
            )
            output = result.stdout.strip()
            if not output:
                return "No known transport nodes."
            if len(output) > 1500:
                output = output[:1500] + "\n... (truncated)"
            return output
        except FileNotFoundError:
            return "rnstatus not found."
        except subprocess.TimeoutExpired:
            return "rnstatus timed out."
        except Exception as exc:
            return f"Error querying nodes: {exc}"

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
                return "Usage: !dice NdM (e.g., !dice 2d6, !dice 1d20)"
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
            return "Usage: !dice NdM (e.g., !dice 2d6)"

    def _cmd_flip(self, _args: str = "") -> str:
        """Flip a coin."""
        return f"Coin flip: {'Heads' if random.random() < 0.5 else 'Tails'}"

    def _cmd_calc(self, args: str = "") -> str:
        """Safely evaluate a math expression."""
        expr = args.strip()
        if not expr:
            return "Usage: !calc <expression>\nExample: !calc 2**10 + sqrt(144)"

        try:
            tree = ast.parse(expr, mode="eval")
            result = _safe_eval(tree)
            # Format nicely
            if isinstance(result, float) and result == int(result) and abs(result) < 1e15:
                result = int(result)
            return f"{expr} = {result}"
        except (ValueError, TypeError, SyntaxError, ZeroDivisionError) as exc:
            return f"Error: {exc}\nSupported: +, -, *, /, //, **, %, sqrt, sin, cos, tan, log, pi, e"

    def _cmd_define(self, args: str = "") -> str:
        """Look up a word definition using the Free Dictionary API."""
        word = args.strip().split()[0] if args.strip() else ""
        if not word:
            return "Usage: !define <word>\nExample: !define serendipity"

        try:
            url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(word)}"
            data = self._fetch_json(url)

            if not isinstance(data, list) or not data:
                return f"No definition found for: {word}"

            entry = data[0]
            lines = [f"{entry.get('word', word)}"]

            # Phonetic
            phonetic = entry.get("phonetic", "")
            if phonetic:
                lines[0] += f"  {phonetic}"

            for meaning in entry.get("meanings", [])[:3]:
                pos = meaning.get("partOfSpeech", "")
                lines.append(f"\n({pos})")
                for defn in meaning.get("definitions", [])[:2]:
                    lines.append(f"  - {defn['definition']}")
                    example = defn.get("example")
                    if example:
                        lines.append(f"    \"{example}\"")

            return "\n".join(lines)

        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return f"No definition found for: {word}"
            return f"Dictionary lookup failed (HTTP {exc.code})."
        except urllib.error.URLError:
            return "Dictionary lookup failed (network error)."
        except (KeyError, TypeError, ValueError):
            return f"Could not parse definition for: {word}"

    def _cmd_news(self, args: str = "") -> str:
        """Fetch latest headlines from Wikinews RSS."""
        try:
            # Use Wikinews Atom feed — no API key needed
            url = "https://en.wikinews.org/w/api.php?" + urllib.parse.urlencode({
                "action": "feedrecentchanges",
                "feedformat": "atom",
                "namespace": "0",
                "limit": "5",
            })
            req = urllib.request.Request(
                url, headers={"User-Agent": "ReticulumPi-InfoBot/2.0"}
            )
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                feed_text = resp.read().decode("utf-8")

            # Simple XML parsing without external deps
            import re
            titles = re.findall(r"<title[^>]*>([^<]+)</title>", feed_text)
            # Skip the feed-level title
            titles = [t for t in titles[1:] if t.strip()][:5]

            if not titles:
                return "No headlines available right now."

            lines = ["Latest Wikinews:"]
            for i, title in enumerate(titles, 1):
                # Unescape basic HTML entities
                title = (title.replace("&amp;", "&").replace("&lt;", "<")
                         .replace("&gt;", ">").replace("&quot;", '"'))
                lines.append(f"  {i}. {title}")
            return "\n".join(lines)

        except urllib.error.URLError:
            return "Could not fetch news (network error)."
        except Exception:
            return "Could not parse news feed."

    def _cmd_iss(self, _args: str = "") -> str:
        """Get the current position of the International Space Station."""
        try:
            data = self._fetch_json("http://api.open-notify.org/iss-now.json")
            pos = data["iss_position"]
            lat = float(pos["latitude"])
            lon = float(pos["longitude"])

            # Convert to Maidenhead grid for ham operators
            grid = self._latlon_to_grid(lat, lon)

            lines = [
                "International Space Station",
                f"  Latitude:  {lat:.4f}°",
                f"  Longitude: {lon:.4f}°",
                f"  Grid: {grid}",
            ]

            # Get crew count
            try:
                crew_data = self._fetch_json("http://api.open-notify.org/astros.json")
                iss_crew = [p for p in crew_data.get("people", [])
                            if p.get("craft") == "ISS"]
                if iss_crew:
                    lines.append(f"  Crew aboard: {len(iss_crew)}")
            except Exception:
                pass

            return "\n".join(lines)

        except urllib.error.URLError:
            return "Could not fetch ISS position (network error)."
        except (KeyError, TypeError, ValueError):
            return "Could not parse ISS data."

    def _cmd_crypto(self, args: str = "") -> str:
        """Get cryptocurrency price from CoinGecko."""
        symbol = args.strip().upper()
        if not symbol:
            return "Usage: !crypto <symbol>\nExample: !crypto BTC"

        # Map common symbols to CoinGecko IDs
        symbol_map = {
            "BTC": "bitcoin", "ETH": "ethereum", "LTC": "litecoin",
            "XRP": "ripple", "DOGE": "dogecoin", "ADA": "cardano",
            "SOL": "solana", "DOT": "polkadot", "AVAX": "avalanche-2",
            "MATIC": "matic-network", "LINK": "chainlink", "XMR": "monero",
            "ATOM": "cosmos", "UNI": "uniswap", "SHIB": "shiba-inu",
        }

        coin_id = symbol_map.get(symbol, symbol.lower())

        try:
            url = (
                "https://api.coingecko.com/api/v3/simple/price?"
                + urllib.parse.urlencode({
                    "ids": coin_id,
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                    "include_market_cap": "true",
                })
            )
            data = self._fetch_json(url)

            if coin_id not in data:
                return f"Unknown cryptocurrency: {symbol}"

            info = data[coin_id]
            price = info.get("usd", 0)
            change = info.get("usd_24h_change")
            mcap = info.get("usd_market_cap")

            lines = [f"{symbol} (USD)"]
            lines.append(f"  Price: ${price:,.2f}")
            if change is not None:
                direction = "+" if change >= 0 else ""
                lines.append(f"  24h change: {direction}{change:.2f}%")
            if mcap and mcap > 0:
                if mcap >= 1e12:
                    lines.append(f"  Market cap: ${mcap/1e12:.2f}T")
                elif mcap >= 1e9:
                    lines.append(f"  Market cap: ${mcap/1e9:.2f}B")
                elif mcap >= 1e6:
                    lines.append(f"  Market cap: ${mcap/1e6:.2f}M")

            return "\n".join(lines)

        except urllib.error.URLError:
            return "Could not fetch crypto price (network error)."
        except (KeyError, TypeError, ValueError):
            return f"Could not parse price data for: {symbol}"

    def _cmd_joke(self, _args: str = "") -> str:
        """Fetch a random joke."""
        try:
            req = urllib.request.Request(
                "https://official-joke-api.appspot.com/random_joke",
                headers={"User-Agent": "ReticulumPi-InfoBot/2.0"},
            )
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            setup = data.get("setup", "")
            punchline = data.get("punchline", "")
            return f"{setup}\n\n{punchline}"
        except Exception:
            # Fallback to a local joke
            jokes = [
                ("Why do programmers prefer dark mode?", "Because light attracts bugs."),
                ("Why did the packet cross the network?", "To get to the other site."),
                ("What's a mesh network's favorite dance?", "The hop."),
                ("How do routers greet each other?", "With a SYN!"),
            ]
            setup, punchline = random.choice(jokes)
            return f"{setup}\n\n{punchline}"

    def _cmd_solar(self, _args: str = "") -> str:
        """Fetch solar and geomagnetic conditions from NOAA."""
        try:
            # NOAA SWPC planetary K-index
            url = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
            data = self._fetch_json(url)

            if not data or len(data) < 2:
                return "No solar data available."

            # data[0] is header, last row is most recent
            latest = data[-1]
            # Columns: time_tag, Kp, Kp_fraction, a_running, station_count
            kp_time = latest[0]
            kp_value = latest[1]

            lines = ["Solar/Geomagnetic Conditions"]
            lines.append(f"  Time: {kp_time} UTC")
            lines.append(f"  Planetary Kp index: {kp_value}")

            kp_float = float(kp_value)
            if kp_float < 2:
                lines.append("  Status: Quiet — good HF propagation")
            elif kp_float < 4:
                lines.append("  Status: Unsettled — minor disturbance")
            elif kp_float < 5:
                lines.append("  Status: Active — possible HF degradation")
            elif kp_float < 6:
                lines.append("  Status: Minor storm (G1)")
            elif kp_float < 7:
                lines.append("  Status: Moderate storm (G2)")
            elif kp_float < 8:
                lines.append("  Status: Strong storm (G3)")
            else:
                lines.append("  Status: Severe storm (G4+)")

            # Try to get solar flux (F10.7)
            try:
                flux_url = "https://services.swpc.noaa.gov/products/summary/solar-wind-mag-field.json"
                flux_data = self._fetch_json(flux_url)
                bt = flux_data.get("Bt", "N/A")
                bz = flux_data.get("Bz", "N/A")
                lines.append(f"  Solar wind Bt: {bt} nT, Bz: {bz} nT")
            except Exception:
                pass

            return "\n".join(lines)

        except urllib.error.URLError:
            return "Could not fetch solar data (network error)."
        except (KeyError, TypeError, ValueError, IndexError):
            return "Could not parse solar data."

    def _cmd_grid(self, args: str = "") -> str:
        """Convert between Maidenhead grid squares and lat/lon."""
        args = args.strip()
        if not args:
            return ("Usage:\n"
                    "  !grid EM10 — grid to lat/lon\n"
                    "  !grid 30.27 -97.74 — lat/lon to grid")

        # Check if input looks like lat/lon
        parts = args.replace(",", " ").split()
        try:
            if len(parts) >= 2:
                lat = float(parts[0])
                lon = float(parts[1])
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    grid = self._latlon_to_grid(lat, lon)
                    return f"{lat:.4f}, {lon:.4f} -> {grid}"
        except ValueError:
            pass

        # Try as Maidenhead grid square
        grid = args.split()[0].strip()
        try:
            lat, lon = self._grid_to_latlon(grid)
            return f"{grid.upper()} -> {lat:.4f}°N, {lon:.4f}°E"
        except ValueError as exc:
            return str(exc)

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

    @staticmethod
    def _latlon_to_grid(lat: float, lon: float) -> str:
        """Convert latitude/longitude to 6-character Maidenhead grid locator."""
        lon += 180
        lat += 90
        field_lon = int(lon / 20)
        field_lat = int(lat / 10)
        square_lon = int((lon % 20) / 2)
        square_lat = int(lat % 10)
        sub_lon = int((lon - field_lon * 20 - square_lon * 2) * 12)
        sub_lat = int((lat - field_lat * 10 - square_lat) * 24)
        return (chr(65 + field_lon) + chr(65 + field_lat)
                + str(square_lon) + str(square_lat)
                + chr(97 + sub_lon) + chr(97 + sub_lat))

    @staticmethod
    def _grid_to_latlon(grid: str) -> tuple:
        """Convert Maidenhead grid locator to lat/lon (center of square)."""
        grid = grid.strip().upper()
        if len(grid) < 2 or len(grid) % 2 != 0:
            raise ValueError(f"Invalid grid square: {grid}")
        if not (grid[0].isalpha() and grid[1].isalpha()):
            raise ValueError(f"Invalid grid square: {grid}")

        lon = (ord(grid[0]) - 65) * 20 - 180
        lat = (ord(grid[1]) - 65) * 10 - 90

        if len(grid) >= 4:
            lon += int(grid[2]) * 2
            lat += int(grid[3])

        if len(grid) >= 6:
            lon += (ord(grid[4]) - 65) / 12
            lat += (ord(grid[5]) - 65) / 24

        # Center of the grid square
        if len(grid) == 2:
            lon += 10
            lat += 5
        elif len(grid) == 4:
            lon += 1
            lat += 0.5
        elif len(grid) >= 6:
            lon += 1 / 24
            lat += 1 / 48

        return lat, lon

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
