"""Transport Monitor plugin — monitors TCP hub health, activates fallback
connections, and auto-discovers community hubs to maintain connectivity.

Hub exchange: reticulumPi nodes announce a ``reticulumpi.hubexchange``
destination.  When two nodes discover each other via these announces they
can establish an RNS Link and exchange their lists of known-working hubs,
organically growing the pool beyond the bundled YAML list.
"""

from __future__ import annotations

import importlib.resources
import random
import socket
import threading
import time
from typing import Any

import RNS
import RNS.vendor.umsgpack as umsgpack
import yaml

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

# Timeout for TCP connectivity probes (seconds)
_PROBE_TIMEOUT = 5

# Hub exchange constants
_HUB_EXCHANGE_APP = "reticulumpi"
_HUB_EXCHANGE_ASPECT = "hubexchange"
_EXCHANGE_INTERVAL_DEFAULT = 900  # 15 minutes between exchange rounds
_EXCHANGE_LINK_TIMEOUT = 30  # seconds to wait for link establishment
_MAX_EXCHANGE_PEERS = 20  # max peers to track from announces
_DEFAULT_HUB_STALE_HOURS = 24
_HUB_SWEEP_INTERVAL_TICKS = 10


class TransportMonitorPlugin(PluginBase):
    """Monitors TCP transport hub reachability and connects to fallback hubs
    when all primary hubs are down.

    Works in both shared-instance mode (where TCP interfaces live in rnsd)
    and standalone mode.  Health checks use direct TCP socket probes so
    they are independent of the RNS interface layer.

    Publishes events on hub state transitions so the alert system can
    notify operators.  Surfaces health data on the web dashboard.

    When auto_discovery is enabled, also maintains a pool of connections
    to community hubs drawn from a bundled list, automatically replacing
    unhealthy connections to keep target_connections active at all times.
    """

    plugin_name = "transport_monitor"
    plugin_version = "1.1.0"
    plugin_description = (
        "Monitors TCP hub health, activates fallback connections, "
        "and auto-discovers community hubs"
    )
    broadcast_tier = 0
    broadcast_keys = "transport"

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

        # Auto-discovery sub-config validation
        auto = self.config.get("auto_discovery", {})
        if not isinstance(auto, dict):
            raise ValueError("auto_discovery must be a dict")

        if auto.get("enabled", False):
            tc = auto.get("target_connections", 3)
            if not isinstance(tc, int) or tc < 1:
                raise ValueError("auto_discovery.target_connections must be an integer >= 1")

            pi = auto.get("probe_interval", 120)
            if not isinstance(pi, (int, float)) or pi < 10:
                raise ValueError("auto_discovery.probe_interval must be >= 10 seconds")

            cd = auto.get("cooldown_seconds", 300)
            if not isinstance(cd, (int, float)) or cd < 30:
                raise ValueError("auto_discovery.cooldown_seconds must be >= 30 seconds")

            mcd = auto.get("max_cooldown_seconds", 3600)
            if not isinstance(mcd, (int, float)) or mcd < cd:
                raise ValueError(
                    "auto_discovery.max_cooldown_seconds must be >= cooldown_seconds"
                )

            extra = auto.get("extra_hubs", [])
            if not isinstance(extra, list):
                raise ValueError("auto_discovery.extra_hubs must be a list")
            for i, hub in enumerate(extra):
                if not isinstance(hub, dict):
                    raise ValueError(f"auto_discovery.extra_hubs[{i}] must be a dict")
                if "target_host" not in hub:
                    raise ValueError(
                        f"auto_discovery.extra_hubs[{i}] missing 'target_host'"
                    )
                if "target_port" not in hub:
                    raise ValueError(
                        f"auto_discovery.extra_hubs[{i}] missing 'target_port'"
                    )

            hlp = auto.get("hub_list_path")
            if hlp is not None and not isinstance(hlp, str):
                raise ValueError("auto_discovery.hub_list_path must be a string or null")

            ei = auto.get("exchange_interval", _EXCHANGE_INTERVAL_DEFAULT)
            if not isinstance(ei, (int, float)) or ei < 60:
                raise ValueError("auto_discovery.exchange_interval must be >= 60 seconds")

    def start(self) -> None:
        self._active = True
        self._lock = threading.Lock()
        self._check_interval = self.config.get("check_interval", 15)
        self._down_threshold = self.config.get("down_threshold", 60)
        self._auto_teardown = self.config.get("auto_teardown_fallback", True)
        self._primary_hubs = self.config.get("primary_hubs", [])
        self._fallback_hubs = self.config.get("fallback_hubs", [])
        self._tcp_disabled = False  # set dynamically in monitor loop

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

        # --- Auto-discovery ---
        auto = self.config.get("auto_discovery", {})
        self._auto_enabled = auto.get("enabled", False)
        self._target_connections = auto.get("target_connections", 3)
        self._auto_probe_interval = auto.get("probe_interval", 120)
        self._cooldown_seconds = auto.get("cooldown_seconds", 300)
        self._max_cooldown_seconds = auto.get("max_cooldown_seconds", 3600)
        self._prefer_diverse = auto.get("prefer_diverse_regions", True)
        self._auto_extra_hubs: list[dict] = auto.get("extra_hubs", [])
        self._auto_hub_list_path: str | None = auto.get("hub_list_path")

        self._hub_pool: list[dict[str, Any]] = []
        self._auto_interfaces: dict[str, Any] = {}
        self._hub_cooldowns: dict[str, dict[str, Any]] = {}
        self._pinned_hubs: set[str] = set()
        self._stale_hours: float = auto.get("stale_hours", _DEFAULT_HUB_STALE_HOURS)
        self._sweep_tick_counter: int = 0

        # Hub exchange state
        self._exchange_interval = auto.get("exchange_interval", _EXCHANGE_INTERVAL_DEFAULT)
        self._exchange_peers: dict[bytes, float] = {}  # dest_hash -> last_seen monotonic
        self._exchange_destination: Any = None
        self._announce_sub: str | None = None

        if self._auto_enabled:
            self._start_thread(self._auto_discovery_loop, "hub-pool-manager")
            self._setup_hub_exchange()

        self.log.info(
            "Transport monitor active (%d primary hubs, %d fallback hubs, "
            "auto_discovery=%s)",
            len(self._primary_hubs),
            len(self._fallback_hubs),
            "on" if self._auto_enabled else "off",
        )

    def stop(self) -> None:
        self._active = False
        self._teardown_hub_exchange()
        self._deactivate_fallbacks()
        self._teardown_auto_interfaces()
        self._join_threads()

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            online = sum(1 for s in self._hub_status.values() if s["online"])
            now = time.monotonic()
            return {
                "active": self._active,
                "primary_count": len(self._primary_hubs),
                "primaries_online": online,
                "fallback_active": self._fallback_active,
                "active_fallbacks": len(self._active_fallbacks),
                "auto_discovery_enabled": self._auto_enabled,
                "auto_target": self._target_connections if self._auto_enabled else 0,
                "auto_connected": len(self._auto_interfaces),
                "pool_size": len(self._hub_pool),
                "in_cooldown": sum(
                    1 for c in self._hub_cooldowns.values() if c["until"] > now
                ),
                "exchange_peers": len(self._exchange_peers),
            }

    def get_hub_health(self) -> dict[str, Any]:
        """Return structured health data for the dashboard."""
        # Build a lookup of live interface traffic stats keyed by target_host:target_port.
        # In shared-instance mode RNS.Transport.interfaces only shows the
        # LocalClientInterface, but pool/fallback interfaces created by this
        # plugin are tracked in _auto_interfaces and _active_fallbacks.
        iface_stats: dict[str, dict[str, int]] = {}
        try:
            for iface in RNS.Transport.interfaces:
                host = getattr(iface, "target_ip", None)
                port = getattr(iface, "target_port", None)
                if host and port:
                    key = f"{host}:{port}"
                    iface_stats[key] = {
                        "rxb": getattr(iface, "rxb", 0),
                        "txb": getattr(iface, "txb", 0),
                    }
        except Exception:
            pass

        with self._lock:
            primaries = []
            for status in self._hub_status.values():
                entry = dict(status)
                # Try to attach traffic stats from matching live interface
                key = f"{entry.get('target_host', '')}:{entry.get('target_port', '')}"
                stats = iface_stats.get(key)
                if stats:
                    entry["rxb"] = stats["rxb"]
                    entry["txb"] = stats["txb"]
                primaries.append(entry)

            fallbacks = []
            for iface in self._active_fallbacks:
                fallbacks.append({
                    "name": getattr(iface, "name", "unknown"),
                    "online": getattr(iface, "online", False),
                    "target_host": getattr(iface, "target_ip", ""),
                    "target_port": getattr(iface, "target_port", 0),
                    "rxb": getattr(iface, "rxb", 0),
                    "txb": getattr(iface, "txb", 0),
                })

            now = time.monotonic()
            pool_hubs = []
            for key, iface in self._auto_interfaces.items():
                pool_hubs.append({
                    "key": key,
                    "name": getattr(iface, "name", key),
                    "online": True,
                    "target_host": getattr(iface, "target_ip", ""),
                    "target_port": getattr(iface, "target_port", 0),
                    "rxb": getattr(iface, "rxb", 0),
                    "txb": getattr(iface, "txb", 0),
                })

            return {
                "primaries": primaries,
                "fallback_active": self._fallback_active,
                "active_fallbacks": fallbacks,
                "all_down_since": self._all_down_since,
                "down_threshold": self._down_threshold,
                "tcp_disabled": self._tcp_disabled,
                "auto_discovery": {
                    "enabled": self._auto_enabled,
                    "target_connections": self._target_connections,
                    "connected": pool_hubs,
                    "pool_size": len(self._hub_pool),
                    "cooldowns": {
                        k: {"until": v["until"], "failures": v["failures"]}
                        for k, v in self._hub_cooldowns.items()
                        if v["until"] > now
                    },
                    "exchange_peers": len(self._exchange_peers),
                },
            }

    def broadcast_snapshot(self, cycle_count: int = 0) -> dict | None:
        return self.get_hub_health()

    # --- Primary/fallback internals (unchanged) ---

    @staticmethod
    def _probe_tcp(host: str, port: int, timeout: float = _PROBE_TIMEOUT) -> bool:
        """Test TCP connectivity to a host:port. Returns True if reachable."""
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
            return True
        except (OSError, socket.timeout):
            return False

    def _has_user_tcp_interfaces(self) -> bool:
        """Check if any user-configured TCP client interfaces are enabled.

        Returns False when the user has disabled all TCP client interfaces,
        which means we should not create auto-discovery or fallback TCP
        interfaces either.

        In shared-instance mode, ``RNS.Transport.interfaces`` only contains
        a ``LocalClientInterface`` (socket to rnsd) — the real TCP
        interfaces are owned by rnsd and invisible here.  So we check the
        Reticulum config file directly, which is always accurate.
        """
        try:
            from reticulumpi.rns_config import parse_rns_config
            import os

            config_dir = getattr(self.app, "_reticulum_config_dir", None)
            if not config_dir:
                config_dir = os.path.expanduser("~/.reticulum")
            config_path = os.path.join(config_dir, "config")

            _, interfaces = parse_rns_config(config_path)
            for entry in interfaces:
                if entry.iface_type == "TCPClientInterface" and entry.enabled:
                    return True
            return False
        except Exception:
            # Fallback: check live interfaces (works when not in shared mode)
            from RNS.Interfaces.TCPInterface import TCPClientInterface

            our_prefixes = ("Pool-", "Fallback-")
            for iface in RNS.Transport.interfaces:
                if isinstance(iface, TCPClientInterface):
                    name = getattr(iface, "name", "")
                    if not any(name.startswith(p) for p in our_prefixes):
                        return True
            return False

    def _monitor_loop(self) -> None:
        """Periodically check hub health and manage failover."""
        while self._active:
            self._jittered_sleep(self._check_interval)
            if not self._active:
                break

            # Update TCP-disabled flag each cycle
            self._tcp_disabled = not self._has_user_tcp_interfaces()

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
                    and not self._tcp_disabled
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
                # Set attributes normally applied by Reticulum._add_interface()
                iface.announce_rate_target = None
                iface.announce_rate_grace = None
                iface.announce_rate_penalty = None
                if not hasattr(iface, "announce_cap"):
                    iface.announce_cap = RNS.Reticulum.ANNOUNCE_CAP / 100.0
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

    # --- Auto-discovery internals ---

    def _load_hub_pool(self) -> None:
        """Load the community hub list from YAML (bundled or custom path)."""
        pool = []
        path = self._auto_hub_list_path

        try:
            if path:
                with open(path) as f:
                    data = yaml.safe_load(f)
            else:
                ref = importlib.resources.files("reticulumpi").joinpath(
                    "data/community_hubs.yaml"
                )
                data = yaml.safe_load(ref.read_text(encoding="utf-8"))

            if isinstance(data, dict):
                pool = data.get("hubs", [])
        except Exception:
            self.log.exception("Failed to load community hub list")

        # Merge extra_hubs from config
        for hub in self._auto_extra_hubs:
            pool.append(hub)

        # Stamp all entries with last_seen for stale sweep
        now = time.monotonic()
        for hub in pool:
            hub.setdefault("last_seen", now)

        with self._lock:
            self._hub_pool = pool

        self.log.info("Hub pool loaded: %d community hubs", len(pool))

    def _build_pinned_set(self) -> None:
        """Identify hubs already configured in Reticulum so we don't duplicate them."""
        pinned: set[str] = set()

        # From primary_hubs and fallback_hubs config
        for hub in self._primary_hubs + self._fallback_hubs:
            pinned.add(f"{hub['target_host']}:{hub['target_port']}")

        # From Reticulum's active interfaces (catches hubs in the .reticulum/config)
        try:
            stats = self.app.reticulum.get_interface_stats()
            for iface_stat in stats.get("interfaces", []):
                if iface_stat.get("type", "") == "TCPClientInterface":
                    ip = iface_stat.get("target_ip", "")
                    port = iface_stat.get("target_port", 0)
                    if ip and port:
                        pinned.add(f"{ip}:{port}")
                    name = iface_stat.get("name", "")
                    # Also extract hostname from interface name pattern
                    # "TCP Client foo/hostname:port"
                    if "/" in name:
                        addr_part = name.split("/", 1)[1]
                        pinned.add(addr_part)
        except Exception:
            self.log.debug("Could not query interface stats for pinned set", exc_info=True)

        # Resolve hub pool hostnames and check against pinned IPs
        for hub in self._hub_pool:
            host = hub["target_host"]
            port = hub["target_port"]
            key = f"{host}:{port}"
            if key in pinned:
                continue
            try:
                resolved = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
                if resolved:
                    ip = resolved[0][4][0]
                    ip_key = f"{ip}:{port}"
                    if ip_key in pinned:
                        # The hostname resolves to a pinned IP — mark the hostname as pinned too
                        pinned.add(key)
            except OSError:
                pass

        with self._lock:
            self._pinned_hubs = pinned

        self.log.debug("Pinned hub set: %s", pinned)

    def _auto_discovery_loop(self) -> None:
        """Thread entry point for auto-discovery hub pool management."""
        # Initial delay to let Reticulum interfaces stabilize
        self._sleep_while_active(10)
        if not self._active:
            return

        # Check TCP state before loading pool
        self._tcp_disabled = not self._has_user_tcp_interfaces()

        self._build_pinned_set()
        self._load_hub_pool()

        while self._active:
            try:
                self._auto_discovery_tick()
            except Exception:
                self.log.debug("Error in auto-discovery tick", exc_info=True)

            self._jittered_sleep(self._auto_probe_interval)

    def _auto_discovery_tick(self) -> None:
        """One cycle: probe existing pool connections, replace unhealthy ones."""
        self._sweep_tick_counter += 1
        if self._sweep_tick_counter >= _HUB_SWEEP_INTERVAL_TICKS:
            self._sweep_tick_counter = 0
            self._sweep_stale_hubs()

        # Don't create TCP interfaces when user has disabled all TCP
        if self._tcp_disabled:
            # Tear down any pool interfaces we already created
            with self._lock:
                keys = list(self._auto_interfaces.keys())
            for key in keys:
                self._disconnect_auto_hub(key, "tcp_disabled")
            return

        now = time.monotonic()

        # 1. Probe existing auto-discovered connections
        to_remove = []
        with self._lock:
            current_keys = list(self._auto_interfaces.keys())

        for key in current_keys:
            host, port_str = key.rsplit(":", 1)
            if not self._probe_tcp(host, int(port_str)):
                to_remove.append(key)

        for key in to_remove:
            self._disconnect_auto_hub(key, "probe_failed")

        # 2. Count how many more connections we need
        with self._lock:
            needed = self._target_connections - len(self._auto_interfaces)

        if needed <= 0:
            return

        # 3. Select candidates
        candidates = self._select_candidates(needed)

        # 4. Connect to candidates
        connected = 0
        for hub in candidates:
            if connected >= needed:
                break
            if self._connect_auto_hub(hub):
                connected += 1

        # 5. Check exhaustion
        with self._lock:
            still_needed = self._target_connections - len(self._auto_interfaces)
            pool_size = len(self._hub_pool)
            in_cooldown = sum(
                1 for c in self._hub_cooldowns.values() if c["until"] > now
            )

        if still_needed > 0 and connected == 0:
            self.log.warning(
                "Hub pool exhausted: need %d more connections, "
                "%d hubs in cooldown out of %d in pool",
                still_needed, in_cooldown, pool_size,
            )
            self.event_bus.publish(events.HUB_POOL_EXHAUSTED, {
                "target": self._target_connections,
                "connected": self._target_connections - still_needed,
                "in_cooldown": in_cooldown,
                "pool_size": pool_size,
            })

    def _select_candidates(self, needed: int) -> list[dict[str, Any]]:
        """Pick the best candidates from the hub pool for connection."""
        now = time.monotonic()

        with self._lock:
            pinned = set(self._pinned_hubs)
            connected_keys = set(self._auto_interfaces.keys())
            cooldowns = dict(self._hub_cooldowns)
            pool = list(self._hub_pool)

        # Filter out unavailable hubs
        available = []
        for hub in pool:
            key = f"{hub['target_host']}:{hub['target_port']}"
            if key in pinned:
                continue
            if key in connected_keys:
                continue
            cd = cooldowns.get(key)
            if cd and cd["until"] > now:
                continue
            available.append(hub)

        if not available:
            return []

        if self._prefer_diverse and len(available) > needed:
            # Count regions of currently connected auto-hubs
            connected_regions: dict[str, int] = {}
            for hub in pool:
                key = f"{hub['target_host']}:{hub['target_port']}"
                if key in connected_keys:
                    region = hub.get("region", "unknown")
                    connected_regions[region] = connected_regions.get(region, 0) + 1

            # Score: fewer connections in same region = better (lower score)
            # Random tiebreaker to avoid always picking the same hub
            def sort_key(h: dict) -> tuple[int, float]:
                region = h.get("region", "unknown")
                return (connected_regions.get(region, 0), random.random())

            available.sort(key=sort_key)

        return available[:needed * 2]  # Return extra candidates in case some fail probes

    def _connect_auto_hub(self, hub: dict[str, Any]) -> bool:
        """Probe and connect to a single community hub. Returns True on success."""
        from RNS.Interfaces.TCPInterface import TCPClientInterface

        host = hub["target_host"]
        port = int(hub["target_port"])
        key = f"{host}:{port}"
        name = hub.get("name", key)

        if not self._probe_tcp(host, port):
            self._update_cooldown(key)
            self.log.debug("Hub probe failed (cooldown): %s", key)
            return False

        config = {
            "name": f"Pool-{name}",
            "target_host": host,
            "target_port": str(port),
        }
        try:
            iface = TCPClientInterface(RNS.Transport, config)
            # Set attributes normally applied by Reticulum._add_interface()
            # Without these, RNS will raise AttributeError when processing
            # announces through this interface.
            iface.announce_rate_target = None
            iface.announce_rate_grace = None
            iface.announce_rate_penalty = None
            if not hasattr(iface, "announce_cap"):
                iface.announce_cap = RNS.Reticulum.ANNOUNCE_CAP / 100.0
            RNS.Transport.interfaces.append(iface)
            with self._lock:
                self._auto_interfaces[key] = iface
                # Clear cooldown and refresh last_seen on success
                self._hub_cooldowns.pop(key, None)
                for h in self._hub_pool:
                    if f"{h['target_host']}:{h['target_port']}" == key:
                        h["last_seen"] = time.monotonic()
                        break
            self.log.info(
                "Pool hub connected: %s (%s:%d, region=%s)",
                name, host, port, hub.get("region", "unknown"),
            )
            self.event_bus.publish(events.HUB_POOL_CONNECTED, {
                "name": name,
                "target_host": host,
                "target_port": port,
                "region": hub.get("region", "unknown"),
                "pool_count": len(self._auto_interfaces),
            })
            return True
        except Exception:
            self.log.exception("Failed to create pool interface: %s", key)
            self._update_cooldown(key)
            return False

    def _disconnect_auto_hub(self, key: str, reason: str) -> None:
        """Detach an auto-discovered hub interface and update cooldown."""
        with self._lock:
            iface = self._auto_interfaces.pop(key, None)

        if iface is None:
            return

        name = getattr(iface, "name", key)
        try:
            iface.detach()
            if iface in RNS.Transport.interfaces:
                RNS.Transport.interfaces.remove(iface)
        except Exception:
            self.log.exception("Error detaching pool interface: %s", name)

        self._update_cooldown(key)

        host, port_str = key.rsplit(":", 1)
        self.log.info("Pool hub disconnected: %s (%s)", name, reason)
        self.event_bus.publish(events.HUB_POOL_DISCONNECTED, {
            "name": name,
            "target_host": host,
            "target_port": int(port_str),
            "reason": reason,
        })

    def _teardown_auto_interfaces(self) -> None:
        """Tear down all auto-discovered pool interfaces (called from stop)."""
        with self._lock:
            interfaces = dict(self._auto_interfaces)
            self._auto_interfaces.clear()

        for key, iface in interfaces.items():
            name = getattr(iface, "name", key)
            try:
                iface.detach()
                if iface in RNS.Transport.interfaces:
                    RNS.Transport.interfaces.remove(iface)
                self.log.info("Pool hub torn down: %s", name)
            except Exception:
                self.log.exception("Error tearing down pool interface: %s", name)

    def _update_cooldown(self, key: str) -> None:
        """Record or escalate cooldown for a failed hub (exponential backoff)."""
        with self._lock:
            entry = self._hub_cooldowns.get(key, {"until": 0.0, "failures": 0})
            failures = entry["failures"] + 1
            backoff = min(
                self._cooldown_seconds * (2 ** (failures - 1)),
                self._max_cooldown_seconds,
            )
            self._hub_cooldowns[key] = {
                "until": time.monotonic() + backoff,
                "failures": failures,
            }

    # --- Hub exchange ---

    def _setup_hub_exchange(self) -> None:
        """Create the hub exchange destination, register announce handler, and
        start the exchange thread."""
        try:
            self._exchange_destination = RNS.Destination(
                self.identity,
                RNS.Destination.IN,
                RNS.Destination.SINGLE,
                _HUB_EXCHANGE_APP,
                _HUB_EXCHANGE_ASPECT,
            )

            # Serve our hub list to peers that link to us
            self._exchange_destination.set_link_established_callback(
                self._exchange_link_established
            )
            self._exchange_destination.register_request_handler(
                "/hubs",
                self._handle_hub_request,
                allow=RNS.Destination.ALLOW_ALL,
            )

            # Listen for other nodes announcing hub exchange
            _aspect = f"{_HUB_EXCHANGE_APP}.{_HUB_EXCHANGE_ASPECT}"
            self._announce_sub = self.announce_dispatcher.subscribe(
                _aspect, self._on_hub_announce,
            )

            # Announce ourselves so other nodes can find us
            self._exchange_destination.announce()

            # Start exchange loop
            self._start_thread(self._hub_exchange_loop, "hub-exchange")

            self.log.info(
                "Hub exchange active at %s",
                RNS.prettyhexrep(self._exchange_destination.hash),
            )
        except Exception:
            self.log.exception("Failed to set up hub exchange")

    def _teardown_hub_exchange(self) -> None:
        """Clean up hub exchange resources."""
        if getattr(self, "_announce_sub", None):
            self.announce_dispatcher.unsubscribe(self._announce_sub)
            self._announce_sub = None
        self._exchange_destination = None

    def _exchange_link_established(self, link: Any) -> None:
        """Called when a peer links to our hub exchange destination."""
        self.log.debug("Hub exchange: incoming link from %s", link)

    def _handle_hub_request(
        self, path: str, data: Any, request_id: Any,
        link_id: Any, remote_identity: Any, requested_at: Any,
    ) -> Any:
        """Serve our known-working hub list to a requesting peer."""
        with self._lock:
            working_hubs = []
            # Include hubs from our pool that we've successfully connected to
            # (either currently connected or recently seen online)
            for hub in self._hub_pool:
                key = f"{hub['target_host']}:{hub['target_port']}"
                cd = self._hub_cooldowns.get(key)
                # Skip hubs in cooldown (they're known-broken)
                if cd and cd["until"] > time.monotonic():
                    continue
                working_hubs.append({
                    "h": hub["target_host"],
                    "p": int(hub["target_port"]),
                    "n": hub.get("name", ""),
                    "r": hub.get("region", ""),
                })

        self.log.debug("Hub exchange: serving %d hubs to peer", len(working_hubs))
        return umsgpack.packb({"hubs": working_hubs, "v": 1})

    def _hub_exchange_loop(self) -> None:
        """Periodically query discovered peers for their hub lists."""
        # Wait for initial pool to stabilize
        self._sleep_while_active(30)

        while self._active:
            try:
                self._exchange_tick()
            except Exception:
                self.log.debug("Error in hub exchange tick", exc_info=True)

            # Re-announce periodically so new nodes discover us
            if self._exchange_destination and self._active:
                try:
                    self._exchange_destination.announce()
                except Exception:
                    pass

            self._jittered_sleep(self._exchange_interval)

    def _exchange_tick(self) -> None:
        """Query one exchange peer for their hub list."""
        with self._lock:
            if not self._exchange_peers:
                return
            # Pick the least-recently queried peer
            peers = sorted(self._exchange_peers.items(), key=lambda x: x[1])

        for dest_hash, _last_seen in peers:
            if not self._active:
                return
            if self._query_peer_hubs(dest_hash):
                return  # One successful exchange per tick is enough

    def _query_peer_hubs(self, dest_hash: bytes) -> bool:
        """Link to a peer and request their hub list. Returns True on success."""
        dest_hex = dest_hash.hex()[:12]

        if not RNS.Transport.has_path(dest_hash):
            RNS.Transport.request_path(dest_hash)
            # Brief wait for path
            deadline = time.time() + 10
            while not RNS.Transport.has_path(dest_hash) and time.time() < deadline:
                time.sleep(0.5)
            if not RNS.Transport.has_path(dest_hash):
                self.log.debug("Hub exchange: no path to <%s>", dest_hex)
                return False

        identity = RNS.Identity.recall(dest_hash)
        if identity is None:
            self.log.debug("Hub exchange: cannot recall identity for <%s>", dest_hex)
            return False

        destination = RNS.Destination(
            identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            _HUB_EXCHANGE_APP,
            _HUB_EXCHANGE_ASPECT,
        )

        result = {"done": False, "data": None}

        def on_established(link: Any) -> None:
            link.request(
                "/hubs",
                data=None,
                response_callback=lambda receipt: _on_response(receipt, result),
                failed_callback=lambda receipt: _on_failed(result),
                timeout=_EXCHANGE_LINK_TIMEOUT,
            )

        def _on_response(receipt: Any, res: dict) -> None:
            res["data"] = receipt.response
            res["done"] = True

        def _on_failed(res: dict) -> None:
            res["done"] = True

        try:
            link = RNS.Link(destination, established_callback=on_established)
        except Exception:
            self.log.debug("Hub exchange: link creation failed for <%s>", dest_hex)
            return False

        # Wait for response
        deadline = time.time() + _EXCHANGE_LINK_TIMEOUT + 10
        while not result["done"] and time.time() < deadline:
            time.sleep(0.5)

        try:
            if link.status == RNS.Link.ACTIVE:
                link.teardown()
        except Exception:
            pass

        if result["data"] is None:
            self.log.debug("Hub exchange: no response from <%s>", dest_hex)
            return False

        # Parse and merge the hub list
        try:
            payload = umsgpack.unpackb(result["data"])
            received_hubs = payload.get("hubs", [])
            added = self._merge_exchanged_hubs(received_hubs)
            self.log.info(
                "Hub exchange: received %d hubs from <%s>, %d new",
                len(received_hubs), dest_hex, added,
            )
            if added > 0:
                self.event_bus.publish(events.HUB_POOL_DISCOVERED, {
                    "source": dest_hex,
                    "received": len(received_hubs),
                    "new": added,
                })
            # Update last-queried time
            with self._lock:
                self._exchange_peers[dest_hash] = time.monotonic()
            return True
        except Exception:
            self.log.debug("Hub exchange: failed to parse response from <%s>", dest_hex)
            return False

    # Hard cap on hub pool size to prevent memory exhaustion via malicious peers
    _MAX_HUB_POOL_SIZE = 500

    def _sweep_stale_hubs(self) -> None:
        """Remove exchange-sourced hubs that have been unreachable too long."""
        threshold = self._stale_hours * 3600
        now = time.monotonic()
        cutoff = now - threshold

        with self._lock:
            before = len(self._hub_pool)
            connected_keys = set(self._auto_interfaces.keys())
            self._hub_pool = [
                hub for hub in self._hub_pool
                if hub.get("last_seen", now) > cutoff
                or f"{hub['target_host']}:{hub['target_port']}" in connected_keys
                or hub.get("source") != "exchange"
            ]
            removed = before - len(self._hub_pool)

            pool_keys = {
                f"{h['target_host']}:{h['target_port']}" for h in self._hub_pool
            }
            stale_cooldowns = [
                k for k in self._hub_cooldowns if k not in pool_keys
            ]
            for k in stale_cooldowns:
                del self._hub_cooldowns[k]

        if removed > 0:
            self.log.info(
                "Hub pool sweep: removed %d stale hubs (threshold=%dh, pool=%d)",
                removed, int(self._stale_hours), len(self._hub_pool),
            )

    def _merge_exchanged_hubs(self, received: list[dict]) -> int:
        """Merge hubs received from a peer into our pool. Returns count of new hubs."""
        added = 0
        with self._lock:
            existing_keys = {
                f"{h['target_host']}:{h['target_port']}" for h in self._hub_pool
            }
            pinned = set(self._pinned_hubs)

        for entry in received:
            host = entry.get("h", "")
            port = entry.get("p", 0)
            if not host or not port:
                continue
            key = f"{host}:{port}"
            if key in existing_keys or key in pinned:
                continue
            new_hub = {
                "target_host": host,
                "target_port": int(port),
                "name": entry.get("n", key),
                "region": entry.get("r", "unknown"),
                "source": "exchange",
                "last_seen": time.monotonic(),
            }
            with self._lock:
                if len(self._hub_pool) >= self._MAX_HUB_POOL_SIZE:
                    self.log.debug(
                        "Hub pool cap reached (%d), ignoring remaining exchanged hubs",
                        self._MAX_HUB_POOL_SIZE,
                    )
                    break
                self._hub_pool.append(new_hub)
            existing_keys.add(key)
            added += 1

        return added

    def _on_hub_announce(
        self,
        destination_hash: bytes,
        announced_identity: Any,
        app_data: bytes | None,
    ) -> None:
        if self._exchange_destination and \
                destination_hash == self._exchange_destination.hash:
            return
        self._on_peer_announced(destination_hash)

    def _on_peer_announced(self, dest_hash: bytes) -> None:
        """Called by the announce handler when a hub exchange peer is discovered."""
        with self._lock:
            if dest_hash in self._exchange_peers:
                return  # Already known
            if len(self._exchange_peers) >= _MAX_EXCHANGE_PEERS:
                # Evict oldest peer
                oldest = min(self._exchange_peers, key=self._exchange_peers.get)
                del self._exchange_peers[oldest]
            self._exchange_peers[dest_hash] = 0.0  # never queried

        self.log.info(
            "Hub exchange: discovered peer <%s>",
            dest_hash.hex()[:12],
        )


