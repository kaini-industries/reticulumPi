"""Transport Monitor plugin — monitors TCP hub health and activates fallback connections."""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import RNS

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

# Timeout for TCP connectivity probes (seconds)
_PROBE_TIMEOUT = 5


class TransportMonitorPlugin(PluginBase):
    """Monitors TCP transport hub reachability and connects to fallback hubs
    when all primary hubs are down.

    Works in both shared-instance mode (where TCP interfaces live in rnsd)
    and standalone mode.  Health checks use direct TCP socket probes so
    they are independent of the RNS interface layer.

    Publishes events on hub state transitions so the alert system can
    notify operators.  Surfaces health data on the web dashboard.
    """

    plugin_name = "transport_monitor"
    plugin_version = "1.0.0"
    plugin_description = "Monitors TCP hub health and activates fallback connections"

    def validate_config(self) -> None:
        interval = self.config.get("check_interval", 15)
        if not isinstance(interval, (int, float)) or interval < 5:
            raise ValueError("check_interval must be >= 5 seconds")

        threshold = self.config.get("down_threshold", 60)
        if not isinstance(threshold, (int, float)) or threshold < 10:
            raise ValueError("down_threshold must be >= 10 seconds")

        for label in ("primary_hubs", "fallback_hubs"):
            hubs = self.config.get(label, [])
            if not isinstance(hubs, list):
                raise ValueError(f"{label} must be a list")
            for i, hub in enumerate(hubs):
                if not isinstance(hub, dict):
                    raise ValueError(f"{label}[{i}] must be a dict")
                if "target_host" not in hub:
                    raise ValueError(f"{label}[{i}] missing 'target_host'")
                if "target_port" not in hub:
                    raise ValueError(f"{label}[{i}] missing 'target_port'")

    def start(self) -> None:
        self._active = True
        self._lock = threading.Lock()
        self._check_interval = self.config.get("check_interval", 15)
        self._down_threshold = self.config.get("down_threshold", 60)
        self._auto_teardown = self.config.get("auto_teardown_fallback", True)
        self._primary_hubs = self.config.get("primary_hubs", [])
        self._fallback_hubs = self.config.get("fallback_hubs", [])

        # Per-hub status: keyed by "host:port"
        self._hub_status: dict[str, dict[str, Any]] = {}
        for hub in self._primary_hubs:
            key = f"{hub['target_host']}:{hub['target_port']}"
            self._hub_status[key] = {
                "name": hub.get("name", key),
                "target_host": hub["target_host"],
                "target_port": hub["target_port"],
                "online": False,
                "last_check": 0.0,
            }

        self._all_down_since: float | None = None
        self._active_fallbacks: list[Any] = []
        self._fallback_active = False

        self._start_thread(self._monitor_loop, "transport-monitor")

        self.log.info(
            "Transport monitor active (%d primary hubs, %d fallback hubs)",
            len(self._primary_hubs),
            len(self._fallback_hubs),
        )

    def stop(self) -> None:
        self._active = False
        self._deactivate_fallbacks()
        self._join_threads()

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            online = sum(1 for s in self._hub_status.values() if s["online"])
            return {
                "active": self._active,
                "primary_count": len(self._primary_hubs),
                "primaries_online": online,
                "fallback_active": self._fallback_active,
                "active_fallbacks": len(self._active_fallbacks),
            }

    def get_hub_health(self) -> dict[str, Any]:
        """Return structured health data for the dashboard."""
        with self._lock:
            primaries = []
            for status in self._hub_status.values():
                primaries.append(dict(status))

            fallbacks = []
            for iface in self._active_fallbacks:
                fallbacks.append({
                    "name": getattr(iface, "name", "unknown"),
                    "online": getattr(iface, "online", False),
                    "target_host": getattr(iface, "target_ip", ""),
                    "target_port": getattr(iface, "target_port", 0),
                })

            return {
                "primaries": primaries,
                "fallback_active": self._fallback_active,
                "active_fallbacks": fallbacks,
                "all_down_since": self._all_down_since,
                "down_threshold": self._down_threshold,
            }

    # --- Internal ---

    @staticmethod
    def _probe_tcp(host: str, port: int, timeout: float = _PROBE_TIMEOUT) -> bool:
        """Test TCP connectivity to a host:port. Returns True if reachable."""
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
            return True
        except (OSError, socket.timeout):
            return False

    def _monitor_loop(self) -> None:
        """Periodically check hub health and manage failover."""
        while self._active:
            self._sleep_while_active(self._check_interval)
            if not self._active:
                break

            try:
                self._check_health()
            except Exception:
                self.log.debug("Error in transport monitor loop", exc_info=True)

    def _check_health(self) -> None:
        """Probe all primary hubs and evaluate failover."""
        any_online = False
        now = time.monotonic()

        for hub in self._primary_hubs:
            key = f"{hub['target_host']}:{hub['target_port']}"
            online = self._probe_tcp(hub["target_host"], int(hub["target_port"]))

            with self._lock:
                prev = self._hub_status.get(key, {})
                was_online = prev.get("online", False)

                self._hub_status[key] = {
                    "name": hub.get("name", key),
                    "target_host": hub["target_host"],
                    "target_port": hub["target_port"],
                    "online": online,
                    "last_check": now,
                }

            # Detect transitions (outside lock for event publishing)
            if was_online and not online:
                self.log.warning(
                    "Transport hub OFFLINE: %s (%s:%s)",
                    hub.get("name", key), hub["target_host"], hub["target_port"],
                )
                self.event_bus.publish(events.HUB_OFFLINE, {
                    "name": hub.get("name", key),
                    "target_host": hub["target_host"],
                    "target_port": hub["target_port"],
                })

            elif not was_online and online:
                self.log.info(
                    "Transport hub ONLINE: %s (%s:%s)",
                    hub.get("name", key), hub["target_host"], hub["target_port"],
                )
                self.event_bus.publish(events.HUB_ONLINE, {
                    "name": hub.get("name", key),
                    "target_host": hub["target_host"],
                    "target_port": hub["target_port"],
                })

            if online:
                any_online = True

        # Evaluate failover
        if any_online:
            with self._lock:
                self._all_down_since = None
            if self._fallback_active and self._auto_teardown:
                self.log.info("Primary hub recovered — deactivating fallback")
                self._deactivate_fallbacks()
        elif self._primary_hubs:
            with self._lock:
                if self._all_down_since is None:
                    self._all_down_since = now
                elapsed = now - self._all_down_since
                should_failover = (
                    elapsed >= self._down_threshold
                    and not self._fallback_active
                    and len(self._fallback_hubs) > 0
                )

            if should_failover:
                self.log.warning(
                    "All primary hubs down for %.0f seconds — activating fallback",
                    elapsed,
                )
                self._activate_fallback()

    def _activate_fallback(self) -> None:
        """Connect to the first reachable fallback hub."""
        from RNS.Interfaces.TCPInterface import TCPClientInterface

        for hub in self._fallback_hubs:
            name = hub.get("name", f"Fallback-{hub['target_host']}:{hub['target_port']}")
            config = {
                "name": name,
                "target_host": hub["target_host"],
                "target_port": str(hub["target_port"]),
            }
            try:
                iface = TCPClientInterface(RNS.Transport, config)
                RNS.Transport.interfaces.append(iface)
                with self._lock:
                    self._active_fallbacks.append(iface)
                    self._fallback_active = True
                self.log.info(
                    "Fallback hub activated: %s (%s:%s)",
                    name, hub["target_host"], hub["target_port"],
                )
                self.event_bus.publish(events.FALLBACK_ACTIVATED, {
                    "fallback_name": name,
                    "target_host": hub["target_host"],
                    "target_port": hub["target_port"],
                })
                return  # Stop after first successful creation
            except Exception:
                self.log.exception("Failed to create fallback interface: %s", name)

        self.log.error("All fallback hubs failed to connect")

    def _deactivate_fallbacks(self) -> None:
        """Tear down all active fallback interfaces."""
        with self._lock:
            fallbacks = list(self._active_fallbacks)
            self._active_fallbacks.clear()
            self._fallback_active = False

        for iface in fallbacks:
            name = getattr(iface, "name", "unknown")
            try:
                iface.detach()
                if iface in RNS.Transport.interfaces:
                    RNS.Transport.interfaces.remove(iface)
                self.log.info("Fallback hub deactivated: %s", name)
            except Exception:
                self.log.exception("Error deactivating fallback: %s", name)

        if fallbacks:
            self.event_bus.publish(events.FALLBACK_DEACTIVATED, {
                "reason": "primary_recovered",
                "count": len(fallbacks),
            })
