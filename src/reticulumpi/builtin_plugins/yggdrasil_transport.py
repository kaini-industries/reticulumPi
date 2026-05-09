"""Yggdrasil Transport plugin — monitors the Yggdrasil encrypted IPv6 overlay
and optionally auto-configures a Reticulum TCP interface for global reachability.

Yggdrasil provides an encrypted IPv6 mesh where every node derives a persistent
``200::/7`` address from its ed25519 public key.  Running a Reticulum
TCPServerInterface on this address makes the node globally reachable to any
other Reticulum node on the Yggdrasil network — no public IP, port forwarding,
or DNS required.

The plugin:

- Monitors the Yggdrasil daemon via its admin API (Unix socket or yggdrasilctl)
- Collects metrics: address, subnet, peers, uptime, traffic, build version
- Optionally auto-adds a ``[[Yggdrasil TCP Interface]]`` to the Reticulum config
- Publishes events on Yggdrasil state transitions (online/offline/peer changes)
- Exposes health data for the web dashboard and connectivity_monitor

Requires: Yggdrasil installed and running as a systemd service.
Install with: ``sudo apt-get install yggdrasil && sudo systemctl enable --now yggdrasil``
Or use: ``sudo bash scripts/bootstrap.sh --with-yggdrasil``
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from typing import Any

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

# Default Yggdrasil admin socket paths (varies by distro)
_DEFAULT_ADMIN_SOCKET = "/var/run/yggdrasil/yggdrasil.sock"
_ALT_ADMIN_SOCKETS = [
    "/var/run/yggdrasil.sock",
    "/run/yggdrasil/yggdrasil.sock",
    "/run/yggdrasil.sock",
]

# Default monitoring interval (seconds)
_DEFAULT_CHECK_INTERVAL = 30

# Grace period before warning about missing peers (seconds)
_BOOTSTRAP_GRACE = 120

# Default RNS listen port on Yggdrasil address
_DEFAULT_RNS_PORT = 4242

# Interface name used in Reticulum config
_RNS_INTERFACE_NAME = "Yggdrasil TCP Interface"

# Admin API query timeout (seconds)
_ADMIN_TIMEOUT = 5


class YggdrasilTransportPlugin(PluginBase):
    """Monitors Yggdrasil daemon health and optionally auto-configures
    a Reticulum TCP interface for global mesh connectivity over the
    Yggdrasil encrypted IPv6 overlay network.

    Yggdrasil gives every node a globally-unique ``200::/7`` IPv6 address
    derived from its ed25519 key pair.  By binding a Reticulum
    TCPServerInterface to that address, other ReticulumPi nodes anywhere
    on the Yggdrasil network can connect directly — no public IPs, port
    forwarding, or DNS required.

    Config options::

        yggdrasil_transport:
          enabled: true
          check_interval: 30          # health-check period (seconds)
          admin_socket: null           # auto-detect if null
          auto_configure_rns: false    # add RNS interface to reticulum config
          rns_listen_port: 4242        # port for auto-configured interface
    """

    plugin_name = "yggdrasil_transport"
    plugin_version = "1.0.0"
    plugin_description = (
        "Yggdrasil encrypted IPv6 overlay — global mesh connectivity"
    )

    # ── Lifecycle ────────────────────────────────────────────────────

    def validate_config(self) -> None:
        interval = self.config.get("check_interval", _DEFAULT_CHECK_INTERVAL)
        if not isinstance(interval, (int, float)) or interval < 10:
            raise ValueError("check_interval must be >= 10 seconds")

        port = self.config.get("rns_listen_port", _DEFAULT_RNS_PORT)
        if not isinstance(port, int) or port < 1 or port > 65535:
            raise ValueError("rns_listen_port must be 1-65535")

    def start(self) -> None:
        self._active = True
        self._lock = threading.Lock()
        self._check_interval = self.config.get(
            "check_interval", _DEFAULT_CHECK_INTERVAL
        )
        self._auto_configure_rns = self.config.get("auto_configure_rns", False)
        self._rns_listen_port = self.config.get(
            "rns_listen_port", _DEFAULT_RNS_PORT
        )
        self._admin_socket = self.config.get("admin_socket")  # None = auto

        # Resolved socket path (discovered on first check)
        self._resolved_socket: str | None = None

        # State tracking for event transitions
        self._start_time = time.monotonic()
        self._was_online = False
        self._rns_configured = False
        self._last_peer_count = -1  # -1 = unknown

        # Health snapshot (exposed via get_health / dashboard)
        self._health: dict[str, Any] = {
            "installed": False,
            "running": False,
            "address": None,
            "subnet": None,
            "public_key": None,
            "build_version": None,
            "peers": [],
            "peer_count": 0,
            "coords": [],
            "traffic": {"bytes_sent": 0, "bytes_recvd": 0},
            "rns_interface_configured": False,
            "rns_interface_name": _RNS_INTERFACE_NAME,
            "issues": [],
            "last_check": 0.0,
        }

        self._start_thread(self._monitor_loop, "yggdrasil-monitor")
        self.log.info(
            "Yggdrasil transport monitor started "
            "(interval=%ds, auto_configure_rns=%s)",
            self._check_interval,
            self._auto_configure_rns,
        )

    def stop(self) -> None:
        self._active = False
        self._join_threads()

    # ── Public API (for other plugins / dashboard) ───────────────────

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._active,
                "installed": self._health["installed"],
                "running": self._health["running"],
                "address": self._health["address"],
                "peer_count": self._health["peer_count"],
                "rns_interface_configured": self._health[
                    "rns_interface_configured"
                ],
                "issues": len(self._health["issues"]),
            }

    def get_health(self) -> dict[str, Any]:
        """Full health snapshot for the dashboard API."""
        with self._lock:
            return dict(self._health)

    def get_address(self) -> str | None:
        """Return the Yggdrasil IPv6 address, or None."""
        with self._lock:
            return self._health.get("address")

    def get_peers(self) -> list[dict[str, Any]]:
        """Return the list of connected Yggdrasil peers."""
        with self._lock:
            return list(self._health.get("peers", []))

    def get_peering_uri(self) -> str | None:
        """Return a ``tcp://[address]:port`` URI other nodes can peer with.

        This is the Reticulum TCP interface address, not a Yggdrasil peering
        URI.  Share this with other ReticulumPi operators so they can add
        a TCPClientInterface pointing at your Yggdrasil address.
        """
        with self._lock:
            addr = self._health.get("address")
            if addr and self._health.get("rns_interface_configured"):
                return f"tcp://[{addr}]:{self._rns_listen_port}"
        return None

    # ── Monitor loop ─────────────────────────────────────────────────

    def _monitor_loop(self) -> None:
        """Periodically check Yggdrasil status and update health."""
        self._sleep_while_active(3)  # brief startup delay

        while self._active:
            try:
                self._run_check()
            except Exception:
                self.log.debug(
                    "Error in Yggdrasil monitor loop", exc_info=True
                )
            self._sleep_while_active(self._check_interval)

    def _run_check(self) -> None:
        """Run a full Yggdrasil health check cycle."""
        issues: list[str] = []

        # 1. Check if Yggdrasil is installed
        installed = shutil.which("yggdrasil") is not None
        with self._lock:
            self._health["installed"] = installed

        if not installed:
            issues.append(
                "Yggdrasil not installed — "
                "install with: sudo apt-get install yggdrasil"
            )
            self._update_offline_state(issues)
            return

        # 2. Query self info from admin API
        self_info = self._admin_request("getself")
        if self_info is None:
            elapsed = time.monotonic() - self._start_time
            msg = "Yggdrasil daemon not responding"
            if elapsed < _BOOTSTRAP_GRACE:
                msg += f" (starting up, {elapsed:.0f}/{_BOOTSTRAP_GRACE}s)"
            issues.append(msg)
            self._update_offline_state(issues)
            return

        # Daemon is running — extract self info
        with self._lock:
            self._health["running"] = True
            self._health["address"] = self_info.get("address")
            self._health["subnet"] = self_info.get("subnet")
            self._health["public_key"] = self_info.get("key")
            self._health["build_version"] = self_info.get("build_version")
            self._health["coords"] = self_info.get("coords", [])

        # 3. Query peers
        peers_data = self._admin_request("getpeers")
        peers = peers_data if isinstance(peers_data, list) else []

        total_sent = sum(p.get("bytes_sent", 0) for p in peers)
        total_recvd = sum(p.get("bytes_recvd", 0) for p in peers)

        with self._lock:
            self._health["peers"] = peers
            self._health["peer_count"] = len(peers)
            self._health["traffic"] = {
                "bytes_sent": total_sent,
                "bytes_recvd": total_recvd,
            }

        if len(peers) == 0:
            elapsed = time.monotonic() - self._start_time
            if elapsed > _BOOTSTRAP_GRACE:
                if not self.internet_available:
                    issues.append(
                        "Yggdrasil has 0 peers — "
                        "internet is currently unavailable"
                    )
                else:
                    issues.append(
                        "Yggdrasil has 0 peers — "
                        "add public peers in /etc/yggdrasil.conf"
                    )

        # 4. State transition events
        if not self._was_online:
            self._was_online = True
            addr = self._health.get("address", "?")
            self.log.info("Yggdrasil ONLINE — address %s", addr)
            self._publish_event(
                events.YGGDRASIL_ONLINE, {"address": addr}
            )

        # Peer count change event
        if len(peers) != self._last_peer_count and self._last_peer_count >= 0:
            self._publish_event(
                events.YGGDRASIL_PEERS_CHANGED,
                {"count": len(peers), "previous": self._last_peer_count},
            )
        self._last_peer_count = len(peers)

        # 5. Auto-configure Reticulum interface (one-shot)
        if self._auto_configure_rns and not self._rns_configured:
            self._maybe_configure_rns_interface()

        # 6. Check if RNS interface exists
        self._check_rns_interface_exists()

        # Finalize
        with self._lock:
            self._health["issues"] = issues
            self._health["last_check"] = time.time()

    def _update_offline_state(self, issues: list[str]) -> None:
        """Update health for when Yggdrasil is not responding."""
        was_online = self._was_online
        with self._lock:
            self._health["running"] = False
            self._health["peers"] = []
            self._health["peer_count"] = 0
            self._health["issues"] = issues
            self._health["last_check"] = time.time()

        if was_online:
            self._was_online = False
            self._last_peer_count = -1
            self.log.warning("Yggdrasil OFFLINE")
            self._publish_event(events.YGGDRASIL_OFFLINE)

    # ── Admin API ────────────────────────────────────────────────────

    def _admin_request(self, request: str) -> dict | list | None:
        """Query the Yggdrasil admin API.

        Tries the Unix admin socket first, then falls back to the
        ``yggdrasilctl`` CLI tool.  Returns the parsed response data,
        or ``None`` on failure.
        """
        result = self._query_socket(request)
        if result is not None:
            return result
        return self._query_ctl(request)

    def _find_admin_socket(self) -> str | None:
        """Locate the Yggdrasil admin Unix socket."""
        if self._admin_socket:
            return self._admin_socket if os.path.exists(self._admin_socket) else None

        # Auto-detect: check default and alternative paths
        for path in [_DEFAULT_ADMIN_SOCKET] + _ALT_ADMIN_SOCKETS:
            if os.path.exists(path):
                return path
        return None

    def _query_socket(self, request: str) -> dict | list | None:
        """Query via Yggdrasil Unix admin socket."""
        sock_path = self._resolved_socket or self._find_admin_socket()
        if not sock_path:
            return None

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.settimeout(_ADMIN_TIMEOUT)
                sock.connect(sock_path)

                payload = json.dumps({"request": request}) + "\n"
                sock.sendall(payload.encode("utf-8"))

                # Read response (accumulate chunks until valid JSON)
                data = b""
                while True:
                    try:
                        chunk = sock.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                        try:
                            parsed = json.loads(data.decode("utf-8"))
                            self._resolved_socket = sock_path
                            return self._extract_response(parsed)
                        except json.JSONDecodeError:
                            continue
                    except socket.timeout:
                        break

                # Final parse attempt with all data received
                if data:
                    parsed = json.loads(data.decode("utf-8"))
                    self._resolved_socket = sock_path
                    return self._extract_response(parsed)
            finally:
                sock.close()

        except (OSError, json.JSONDecodeError, KeyError):
            # Socket failed — clear cached path so we retry discovery
            self._resolved_socket = None

        return None

    def _query_ctl(self, request: str) -> dict | list | None:
        """Query via yggdrasilctl subprocess (fallback)."""
        ctl = shutil.which("yggdrasilctl")
        if not ctl:
            return None

        try:
            result = subprocess.run(
                [ctl, "-json", request],
                capture_output=True,
                timeout=10,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                parsed = json.loads(result.stdout)
                # yggdrasilctl -json returns the response directly
                # (no wrapper), or a full envelope depending on version
                if isinstance(parsed, dict) and "response" in parsed:
                    return parsed["response"]
                return parsed
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            pass

        return None

    @staticmethod
    def _extract_response(parsed: dict) -> dict | list | None:
        """Extract the response payload from an admin API envelope."""
        if isinstance(parsed, dict):
            if parsed.get("status") == "success":
                return parsed.get("response", parsed)
            # Some versions return the data directly
            if "address" in parsed or "key" in parsed:
                return parsed
        return None

    # ── Reticulum interface management ───────────────────────────────

    def _get_rns_config_path(self) -> str | None:
        """Locate the Reticulum config file."""
        config_dir = getattr(self.app, "reticulum_config_dir", None)
        if not config_dir:
            rns_instance = getattr(self.app, "reticulum", None)
            if rns_instance:
                config_dir = getattr(rns_instance, "configdir", None)
        if not config_dir:
            return None

        path = os.path.join(config_dir, "config")
        return path if os.path.isfile(path) else None

    def _maybe_configure_rns_interface(self) -> None:
        """Add a Yggdrasil TCPServerInterface to the Reticulum config.

        Only runs once (guarded by ``self._rns_configured``).  Checks for
        an existing interface bound to the Yggdrasil address before adding
        a new one.
        """
        address = self.get_address()
        if not address:
            return

        try:
            from reticulumpi.rns_config import (
                add_interface_section,
                parse_rns_config,
                write_rns_config,
            )

            config_path = self._get_rns_config_path()
            if not config_path:
                self.log.debug(
                    "Cannot auto-configure: Reticulum config not found"
                )
                return

            lines, interfaces = parse_rns_config(config_path)

            # Check if already configured
            for iface in interfaces:
                name_match = (
                    _RNS_INTERFACE_NAME.lower() in iface.name.lower()
                )
                addr_match = address in (
                    iface.properties.get("listen_ip", "")
                    or iface.properties.get("listen_host", "")
                )
                if name_match or addr_match:
                    self._rns_configured = True
                    with self._lock:
                        self._health["rns_interface_configured"] = True
                    self.log.info(
                        "Yggdrasil RNS interface already present: '%s'",
                        iface.name,
                    )
                    return

            # Add new interface section
            lines = add_interface_section(
                lines,
                _RNS_INTERFACE_NAME,
                "TCPServerInterface",
                {
                    "listen_ip": address,
                    "listen_port": str(self._rns_listen_port),
                },
            )
            write_rns_config(config_path, lines)
            self._rns_configured = True

            with self._lock:
                self._health["rns_interface_configured"] = True

            self.log.info(
                "Added '%s' listening on [%s]:%d — restart rnsd to activate",
                _RNS_INTERFACE_NAME,
                address,
                self._rns_listen_port,
            )
            self._publish_event(
                events.YGGDRASIL_RNS_CONFIGURED,
                {
                    "interface_name": _RNS_INTERFACE_NAME,
                    "address": address,
                    "port": self._rns_listen_port,
                },
            )

        except Exception:
            self.log.warning(
                "Failed to auto-configure RNS interface", exc_info=True
            )

    def _check_rns_interface_exists(self) -> None:
        """Check whether a Yggdrasil-bound RNS interface exists in the config."""
        try:
            from reticulumpi.rns_config import parse_rns_config

            config_path = self._get_rns_config_path()
            if not config_path:
                return

            _, interfaces = parse_rns_config(config_path)
            address = self.get_address()

            found = False
            for iface in interfaces:
                if _RNS_INTERFACE_NAME.lower() in iface.name.lower():
                    found = True
                    break
                if address and address in (
                    iface.properties.get("listen_ip", "")
                    or iface.properties.get("listen_host", "")
                ):
                    found = True
                    break

            self._rns_configured = found
            with self._lock:
                self._health["rns_interface_configured"] = found

        except Exception:
            pass

    # ── Helpers ───────────────────────────────────────────────────────

    def _publish_event(
        self, event_type: str, data: dict[str, Any] | None = None
    ) -> None:
        """Publish an event, swallowing errors."""
        try:
            if hasattr(self, "event_bus") and self.event_bus:
                self.event_bus.publish(event_type, data or {})
        except Exception:
            pass
