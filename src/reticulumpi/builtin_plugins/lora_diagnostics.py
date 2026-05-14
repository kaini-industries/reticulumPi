"""LoRa Diagnostics plugin — monitoring, announce beaconing, and peer tracking.

Designed for interop with LoRa-only devices (e.g. Ratcom Cardputer running
microReticulum).  Addresses three problems:

1. **Announce starvation**: Local service announces compete with thousands of
   TCP-sourced announces for limited LoRa bandwidth (``announce_cap``).  The
   beacon thread re-announces ReticulumPi-controlled destinations at a fixed
   interval so LoRa peers can discover them reliably.

2. **Path freshness**: Tracks monitored destination hashes, publishing events
   when their paths appear or disappear.  Combined with the ``path_warmer``
   plugin's ``priority_nodes``, this ensures rnsd always has a fresh path
   ready when a LoRa peer requests one.

3. **Visibility**: Exposes LoRa interface traffic stats and monitored
   destination status via ``get_diagnostics()`` for the ``/api/lora``
   dashboard endpoint.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import RNS

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

# Defaults
_DEFAULT_MONITOR_INTERVAL = 30
_DEFAULT_BEACON_INTERVAL = 120
_MIN_MONITOR_INTERVAL = 10
_MIN_BEACON_INTERVAL = 30

# Announce mode presets
ANNOUNCE_MODES = {
    "all": {
        "announce_cap": "5",
        "interface_mode": None,  # remove interface_mode line (defaults to full)
        "description": "Full announce forwarding, ~10 announces/min on LoRa",
    },
    "local_priority": {
        "announce_cap": "1",
        "interface_mode": None,
        "description": "Local announces get priority, TCP announces barely trickle",
    },
    "silent": {
        "announce_cap": None,  # don't change
        "interface_mode": "access_point",
        "description": "Zero announces on LoRa, path requests still work",
    },
}


class LoRaDiagnosticsPlugin(PluginBase):
    """LoRa-specific diagnostics and announce beaconing for microReticulum interop."""

    plugin_name = "lora_diagnostics"
    plugin_version = "1.0.0"
    plugin_description = (
        "LoRa traffic monitoring, announce beaconing, and peer tracking"
    )
    broadcast_tier = 2
    broadcast_keys = "lora_diagnostics"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def validate_config(self) -> None:
        monitor = self.config.get("monitor_interval", _DEFAULT_MONITOR_INTERVAL)
        if not isinstance(monitor, (int, float)) or monitor < _MIN_MONITOR_INTERVAL:
            raise ValueError(
                f"monitor_interval must be >= {_MIN_MONITOR_INTERVAL} seconds"
            )
        beacon = self.config.get("beacon_interval", _DEFAULT_BEACON_INTERVAL)
        if not isinstance(beacon, (int, float)) or beacon < _MIN_BEACON_INTERVAL:
            raise ValueError(
                f"beacon_interval must be >= {_MIN_BEACON_INTERVAL} seconds"
            )
        dests = self.config.get("monitored_destinations", [])
        if not isinstance(dests, list):
            raise ValueError("monitored_destinations must be a list")
        for entry in dests:
            if not isinstance(entry, dict) or "hash" not in entry:
                raise ValueError(
                    "Each monitored_destinations entry must have a 'hash' key"
                )
            try:
                bytes.fromhex(entry["hash"])
            except (ValueError, TypeError):
                raise ValueError(
                    f"Invalid hex hash in monitored_destinations: {entry.get('hash')}"
                )

    def start(self) -> None:
        self._active = True
        self._lock = threading.Lock()

        # Parse monitored destinations
        self._monitored: dict[str, dict[str, Any]] = {}
        for entry in self.config.get("monitored_destinations", []):
            hex_hash = entry["hash"]
            self._monitored[hex_hash] = {
                "name": entry.get("name", hex_hash[:12]),
                "hash_bytes": bytes.fromhex(hex_hash),
                "has_path": False,
                "hops": None,
                "last_announce_seen": None,
                "last_path_check": None,
            }

        # LoRa interface stats
        self._lora_stats: dict[str, Any] = {
            "name": self.config.get("lora_interface_name", "RNode LoRa Interface"),
            "online": False,
            "rxb": 0,
            "txb": 0,
            "rxb_delta": 0,
            "txb_delta": 0,
            "airtime_short": 0.0,
            "airtime_long": 0.0,
        }
        self._prev_rxb = 0
        self._prev_txb = 0

        # Beacon stats
        self._beacons_sent = 0
        self._last_beacon_time: float | None = None

        # Subscribe to announces for monitored destinations
        self._announce_sub = self.announce_dispatcher.subscribe(
            "lxmf.delivery", self.on_announce_received,
        )

        # Start background threads
        self._start_thread(self._monitor_loop, "lora-monitor")
        self._start_thread(self._beacon_loop, "lora-beacon")

        self.log.info(
            "LoRa diagnostics active (monitor=%ds, beacon=%ds, tracking %d destinations)",
            self.config.get("monitor_interval", _DEFAULT_MONITOR_INTERVAL),
            self.config.get("beacon_interval", _DEFAULT_BEACON_INTERVAL),
            len(self._monitored),
        )

    def stop(self) -> None:
        self._active = False
        if hasattr(self, "_announce_sub"):
            self.announce_dispatcher.unsubscribe(self._announce_sub)
        self._join_threads()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._active,
                "monitored_destinations": len(self._monitored),
                "beacons_sent": self._beacons_sent,
                "lora_online": self._lora_stats.get("online", False),
            }

    def broadcast_snapshot(self, cycle_count: int = 0) -> dict | None:
        return self.get_diagnostics()

    def get_diagnostics(self) -> dict[str, Any]:
        """Full diagnostic data for the /api/lora endpoint."""
        with self._lock:
            monitored = []
            for hex_hash, info in self._monitored.items():
                monitored.append(
                    {
                        "hash": hex_hash,
                        "name": info["name"],
                        "has_path": info["has_path"],
                        "hops": info["hops"],
                        "last_announce_seen": info["last_announce_seen"],
                        "last_path_check": info["last_path_check"],
                    }
                )
            current_mode = self._detect_announce_mode()
            return {
                "lora_interface": dict(self._lora_stats),
                "monitored_destinations": monitored,
                "beacon": {
                    "last_beacon_time": self._last_beacon_time,
                    "beacons_sent": self._beacons_sent,
                    "interval": self.config.get(
                        "beacon_interval", _DEFAULT_BEACON_INTERVAL
                    ),
                },
                "announce_mode": {
                    "current": current_mode,
                    "available": list(ANNOUNCE_MODES.keys()),
                    "description": ANNOUNCE_MODES.get(
                        current_mode, {}
                    ).get("description", "unknown"),
                },
            }

    def set_announce_mode(self, mode: str) -> dict[str, Any]:
        """Change the LoRa announce mode by modifying rnsd config and restarting.

        Returns a dict with the result or raises ValueError for invalid modes.
        """
        import subprocess

        from reticulumpi.rns_config import (
            parse_rns_config,
            parse_rns_config_from_lines,
            remove_interface_property,
            set_interface_property,
            write_rns_config,
        )

        if mode not in ANNOUNCE_MODES:
            raise ValueError(
                f"Invalid mode '{mode}'. Must be one of: {', '.join(ANNOUNCE_MODES)}"
            )

        preset = ANNOUNCE_MODES[mode]
        rns_config_path = self._get_rns_config_path()
        iface_name = self.config.get("lora_interface_name", "RNode LoRa Interface")

        lines, interfaces = parse_rns_config(rns_config_path)
        rnode = None
        for iface in interfaces:
            if iface_name in iface.name:
                rnode = iface
                break

        if not rnode:
            raise RuntimeError(
                f"RNode interface '{iface_name}' not found in {rns_config_path}"
            )

        # Apply announce_cap
        if preset["announce_cap"] is not None:
            lines = set_interface_property(
                lines, rnode, "announce_cap", preset["announce_cap"]
            )
            # Re-parse to get updated line positions after insertion
            lines, interfaces = parse_rns_config_from_lines(lines)
            rnode = next(
                (i for i in interfaces if iface_name in i.name), rnode
            )

        # Apply interface_mode
        if preset["interface_mode"] is not None:
            lines = set_interface_property(
                lines, rnode, "interface_mode", preset["interface_mode"]
            )
        else:
            # Remove interface_mode line if present (defaults to full)
            lines = remove_interface_property(lines, rnode, "interface_mode")

        write_rns_config(rns_config_path, lines)
        self.log.info(
            "LoRa announce mode set to '%s' — restarting rnsd", mode
        )

        self.event_bus.publish(events.RNSD_RESTARTING, {
            "reason": f"announce_mode_change:{mode}",
        })

        try:
            subprocess.run(
                ["sudo", "systemctl", "restart", "rnsd"],
                timeout=15,
                check=True,
                capture_output=True,
            )
        except subprocess.TimeoutExpired:
            self.log.warning("rnsd restart timed out")
        except subprocess.CalledProcessError as exc:
            self.log.error("rnsd restart failed: %s", exc.stderr.decode())
            raise RuntimeError(f"rnsd restart failed: {exc.stderr.decode()}")

        self._wait_for_rnsd(timeout=10)
        self.event_bus.publish(events.RNSD_RECOVERED, {
            "reason": f"announce_mode_change:{mode}",
        })

        return {
            "mode": mode,
            "description": preset["description"],
            "rnsd_restarted": True,
        }

    def _wait_for_rnsd(self, timeout: float = 10) -> None:
        """Poll until rnsd is responsive or timeout."""
        import subprocess as _sp

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = _sp.run(["rnstatus"], capture_output=True, timeout=5)
                if result.returncode == 0:
                    return
            except Exception:
                pass
            time.sleep(1)
        self.log.warning("rnsd did not become responsive within %ds", timeout)

    def _detect_announce_mode(self) -> str:
        """Read current rnsd config to determine active announce mode."""
        try:
            from reticulumpi.rns_config import parse_rns_config

            rns_config_path = self._get_rns_config_path()
            _, interfaces = parse_rns_config(rns_config_path)
            iface_name = self.config.get(
                "lora_interface_name", "RNode LoRa Interface"
            )
            for iface in interfaces:
                if iface_name in iface.name:
                    imode = iface.properties.get("interface_mode", "").lower()
                    cap = iface.properties.get("announce_cap", "2")
                    if imode in ("access_point", "accesspoint", "ap"):
                        return "silent"
                    try:
                        cap_val = float(cap)
                    except (ValueError, TypeError):
                        cap_val = 2.0
                    if cap_val <= 1.0:
                        return "local_priority"
                    return "all"
        except Exception:
            pass
        return "unknown"

    def _get_rns_config_path(self) -> str:
        """Resolve the rnsd Reticulum config path."""
        # rnsd runs as the reticulumpi user with its own config dir
        return "/home/reticulumpi/.reticulum/config"

    # ------------------------------------------------------------------
    # Monitor thread — poll interface stats + check paths
    # ------------------------------------------------------------------

    def _monitor_loop(self) -> None:
        interval = self.config.get("monitor_interval", _DEFAULT_MONITOR_INTERVAL)
        iface_name = self.config.get("lora_interface_name", "RNode LoRa Interface")

        # Let system settle
        self._sleep_while_active(min(interval, 15))

        while self._active:
            try:
                self._poll_interface_stats(iface_name)
                self._check_monitored_paths()
            except Exception:
                self.log.warning("Error in LoRa monitor loop", exc_info=True)
            self._jittered_sleep(interval)

    def _poll_interface_stats(self, iface_name: str) -> None:
        """Query rnsd for interface stats and update LoRa tracking."""
        try:
            stats = self.app.reticulum.get_interface_stats()
        except Exception:
            return

        if not stats:
            return

        # get_interface_stats() returns a dict with an "interfaces" key
        # containing a list of interface dicts.  Each dict has "name"
        # like "RNodeInterface[RNode LoRa Interface]" and "short_name"
        # like "RNode LoRa Interface".
        iface_list = stats.get("interfaces", []) if isinstance(stats, dict) else stats
        if not isinstance(iface_list, list):
            return

        lora_iface = None
        for iface in iface_list:
            if not isinstance(iface, dict):
                continue
            name = iface.get("short_name", "") or iface.get("name", "")
            if iface_name in name:
                lora_iface = iface
                break

        if not lora_iface:
            return

        rxb = lora_iface.get("rxb", 0)
        txb = lora_iface.get("txb", 0)

        with self._lock:
            self._lora_stats["online"] = lora_iface.get("status", False)
            self._lora_stats["rxb"] = rxb
            self._lora_stats["txb"] = txb
            self._lora_stats["rxb_delta"] = rxb - self._prev_rxb
            self._lora_stats["txb_delta"] = txb - self._prev_txb
            self._lora_stats["airtime_short"] = lora_iface.get(
                "airtime_short", 0.0
            )
            self._lora_stats["airtime_long"] = lora_iface.get(
                "airtime_long", 0.0
            )
            self._lora_stats["announce_queue"] = lora_iface.get(
                "announce_queue", 0
            )
            self._lora_stats["channel_load_short"] = lora_iface.get(
                "channel_load_short", 0.0
            )
            self._lora_stats["channel_load_long"] = lora_iface.get(
                "channel_load_long", 0.0
            )
            rxb_delta = rxb - self._prev_rxb
            txb_delta = txb - self._prev_txb
            self._prev_rxb = rxb
            self._prev_txb = txb

        self.event_bus.publish(
            events.LORA_STATS_UPDATED,
            {"rxb_delta": rxb_delta, "txb_delta": txb_delta},
        )

    def _check_monitored_paths(self) -> None:
        """Check path status for each monitored destination."""
        now = time.time()
        for hex_hash, info in self._monitored.items():
            dest_hash = info["hash_bytes"]
            had_path = info["has_path"]

            has_path = RNS.Transport.has_path(dest_hash)
            hops = None
            if has_path:
                try:
                    hops = RNS.Transport.hops_to(dest_hash)
                except Exception:
                    pass

            with self._lock:
                info["has_path"] = has_path
                info["hops"] = hops
                info["last_path_check"] = now

            # Publish events on state transitions
            if had_path and not has_path:
                self.log.warning(
                    "Path lost to %s (%s)",
                    info["name"],
                    hex_hash[:12],
                )
                self.event_bus.publish(
                    events.LORA_PEER_PATH_LOST,
                    {"hash": hex_hash, "name": info["name"]},
                )
            elif not had_path and has_path:
                self.log.info(
                    "Path discovered to %s (%s) — %d hops",
                    info["name"],
                    hex_hash[:12],
                    hops if hops is not None else -1,
                )

    # ------------------------------------------------------------------
    # Beacon thread — periodically re-announce local destinations
    # ------------------------------------------------------------------

    def _beacon_loop(self) -> None:
        interval = self.config.get("beacon_interval", _DEFAULT_BEACON_INTERVAL)

        # Let system settle and other plugins start
        self._sleep_while_active(min(interval, 30))

        while self._active:
            try:
                self._send_beacons()
            except Exception:
                self.log.warning("Error in LoRa beacon loop", exc_info=True)
            self._jittered_sleep(interval)

    def _send_beacons(self) -> None:
        """Trigger announces on sibling plugins' destinations.

        These announces flow through the shared instance to rnsd, which
        retransmits them on all interfaces including the LoRa RNode.
        With ``announce_cap=5``, local announces (hops=1) get priority
        in the queue over higher-hop TCP-sourced announces.
        """
        announced = 0

        # 1. Heartbeat announce destination
        heartbeat = self.app.get_plugin("heartbeat_announce")
        if heartbeat and hasattr(heartbeat, "destination") and heartbeat.destination:
            try:
                app_data = None
                if hasattr(heartbeat, "_build_app_data"):
                    raw = heartbeat._build_app_data()
                    app_data = raw.encode("utf-8") if raw else None
                heartbeat.destination.announce(app_data=app_data)
                announced += 1
            except Exception:
                self.log.debug("Failed to beacon heartbeat destination", exc_info=True)

        # 2. Messaging hub LXMF destination
        messaging_hub = self.app.get_plugin("messaging_hub")
        if messaging_hub and hasattr(messaging_hub, "_adapters"):
            for adapter in getattr(messaging_hub, "_adapters", {}).values():
                dest = getattr(adapter, "_destination", None)
                if dest and hasattr(dest, "announce"):
                    try:
                        dest.announce()
                        announced += 1
                    except Exception:
                        self.log.debug(
                            "Failed to beacon messaging_hub destination",
                            exc_info=True,
                        )

        if announced:
            with self._lock:
                self._beacons_sent += announced
                self._last_beacon_time = time.time()
            self.log.debug("Beacon: announced %d destination(s)", announced)

    # ------------------------------------------------------------------
    # Announce handler — track incoming announces from monitored peers
    # ------------------------------------------------------------------

    def on_announce_received(
        self,
        destination_hash: bytes,
        announced_identity: Any,
        app_data: bytes | None,
    ) -> None:
        """Called by the announce handler when an LXMF announce arrives."""
        hex_hash = destination_hash.hex()

        with self._lock:
            info = self._monitored.get(hex_hash)
            if not info:
                return
            info["last_announce_seen"] = time.time()

        self.log.info(
            "Announce received from monitored peer %s (%s)",
            info["name"],
            hex_hash[:12],
        )
        self.event_bus.publish(
            events.LORA_PEER_ANNOUNCE_RECEIVED,
            {"hash": hex_hash, "name": info["name"]},
        )


