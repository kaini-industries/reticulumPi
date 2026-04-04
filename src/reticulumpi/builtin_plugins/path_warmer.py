"""Path Warmer plugin — proactively refreshes paths to known/important nodes."""

from __future__ import annotations

import threading
import time
from typing import Any

import RNS

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

# Defaults
_DEFAULT_WARM_INTERVAL = 120
_DEFAULT_MAX_PER_CYCLE = 10
_DEFAULT_AGE_THRESHOLD = 1200  # 20 minutes
_DEFAULT_PRE_SEND_TIMEOUT = 8
_DEFAULT_REQUEST_TIMEOUT = 10
_DEFAULT_RECENT_HOURS = 24


class PathWarmerPlugin(PluginBase):
    """Proactively requests paths for nodes whose paths are stale or missing.

    Periodically warms paths for priority nodes and recently-seen nodes
    (from network_map).  Also exposes ``ensure_path()`` for LXMF-sending
    plugins to call before transmitting a message.
    """

    plugin_name = "path_warmer"
    plugin_version = "1.0.0"
    plugin_description = "Proactively refreshes paths to known/important nodes"

    def validate_config(self) -> None:
        interval = self.config.get("warm_interval", _DEFAULT_WARM_INTERVAL)
        if not isinstance(interval, (int, float)) or interval < 10:
            raise ValueError("warm_interval must be >= 10 seconds")
        max_req = self.config.get("max_requests_per_cycle", _DEFAULT_MAX_PER_CYCLE)
        if not isinstance(max_req, int) or max_req < 1:
            raise ValueError("max_requests_per_cycle must be >= 1")

    def start(self) -> None:
        self._active = True
        self._lock = threading.Lock()

        # Stats
        self._paths_warmed = 0
        self._paths_failed = 0
        self._last_cycle_warmed = 0
        self._last_cycle_time: float | None = None

        # Rate limiting: dest_hash_hex -> last attempt timestamp
        self._last_warm_attempt: dict[str, float] = {}

        # Parse priority nodes from config
        self._priority_hashes: list[bytes] = []
        for hex_hash in self.config.get("priority_nodes", []):
            try:
                self._priority_hashes.append(bytes.fromhex(hex_hash))
            except (ValueError, TypeError):
                self.log.warning("Invalid priority_nodes hash: %s", hex_hash)

        self._start_thread(self._warm_loop, "path-warmer")

        self.log.info(
            "Path warmer active (interval=%ds, max_per_cycle=%d, "
            "priority_nodes=%d, age_threshold=%ds)",
            self.config.get("warm_interval", _DEFAULT_WARM_INTERVAL),
            self.config.get("max_requests_per_cycle", _DEFAULT_MAX_PER_CYCLE),
            len(self._priority_hashes),
            self.config.get("path_age_threshold", _DEFAULT_AGE_THRESHOLD),
        )

    def stop(self) -> None:
        self._active = False
        self._join_threads()

    # --- Public API ---

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._active,
                "paths_warmed": self._paths_warmed,
                "paths_failed": self._paths_failed,
                "last_cycle_warmed": self._last_cycle_warmed,
                "last_cycle_time": self._last_cycle_time,
                "priority_nodes": len(self._priority_hashes),
            }

    def get_warming_stats(self) -> dict[str, Any]:
        """Full stats for dashboard API."""
        with self._lock:
            return {
                "paths_warmed": self._paths_warmed,
                "paths_failed": self._paths_failed,
                "last_cycle_warmed": self._last_cycle_warmed,
                "last_cycle_time": self._last_cycle_time,
                "priority_nodes": len(self._priority_hashes),
                "warm_interval": self.config.get(
                    "warm_interval", _DEFAULT_WARM_INTERVAL
                ),
                "max_requests_per_cycle": self.config.get(
                    "max_requests_per_cycle", _DEFAULT_MAX_PER_CYCLE
                ),
            }

    def ensure_path(
        self, dest_hash: bytes, timeout: float | None = None
    ) -> bool:
        """Ensure a path exists to *dest_hash*, blocking up to *timeout* seconds.

        Returns ``True`` if a path is available, ``False`` on timeout.
        Intended for LXMF-sending plugins to call before transmitting.
        """
        if timeout is None:
            timeout = self.config.get("pre_send_timeout", _DEFAULT_PRE_SEND_TIMEOUT)

        if RNS.Transport.has_path(dest_hash):
            return True

        self.log.debug(
            "Requesting path for %s (pre-send)", RNS.prettyhexrep(dest_hash)
        )
        RNS.Transport.request_path(dest_hash)

        deadline = time.time() + timeout
        while time.time() < deadline:
            if RNS.Transport.has_path(dest_hash):
                with self._lock:
                    self._paths_warmed += 1
                self.event_bus.publish(
                    events.PATH_WARMED,
                    {"destination_hash": dest_hash.hex(), "source": "ensure_path"},
                )
                return True
            time.sleep(0.5)

        with self._lock:
            self._paths_failed += 1
        self.event_bus.publish(
            events.PATH_WARM_FAILED,
            {"destination_hash": dest_hash.hex(), "source": "ensure_path"},
        )
        return False

    # --- Internal ---

    def _warm_loop(self) -> None:
        """Background thread: periodically warm stale/missing paths."""
        interval = self.config.get("warm_interval", _DEFAULT_WARM_INTERVAL)

        # Let the system settle before first cycle
        self._sleep_while_active(min(interval, 30))

        while self._active:
            try:
                self._run_warm_cycle()
            except Exception:
                self.log.warning("Error in warming cycle", exc_info=True)

            self._sleep_while_active(interval)

    def _run_warm_cycle(self) -> None:
        """Execute one warming cycle."""
        max_requests = self.config.get(
            "max_requests_per_cycle", _DEFAULT_MAX_PER_CYCLE
        )
        age_threshold = self.config.get(
            "path_age_threshold", _DEFAULT_AGE_THRESHOLD
        )

        # Build path age lookup from connectivity_monitor if available
        path_ages = self._get_path_ages()

        # Build candidate list in priority order
        candidates = self._build_candidates(path_ages, age_threshold)

        warmed = 0
        failed = 0
        now = time.time()
        interval = self.config.get("warm_interval", _DEFAULT_WARM_INTERVAL)

        # Prune stale rate-limit entries to prevent unbounded growth
        stale_cutoff = now - interval * 2
        self._last_warm_attempt = {
            k: v for k, v in self._last_warm_attempt.items() if v > stale_cutoff
        }

        for dest_hash in candidates:
            if warmed + failed >= max_requests:
                break
            if not self._active:
                break

            # Rate limit: don't re-attempt within warm_interval
            hex_hash = dest_hash.hex()
            last = self._last_warm_attempt.get(hex_hash, 0)
            if now - last < interval:
                continue

            self._last_warm_attempt[hex_hash] = now
            success = self._warm_node(dest_hash)
            if success:
                warmed += 1
            else:
                failed += 1

        with self._lock:
            self._last_cycle_warmed = warmed
            self._last_cycle_time = time.time()

        if warmed or failed:
            self.log.info(
                "Warming cycle: %d warmed, %d failed (of %d candidates)",
                warmed,
                failed,
                len(candidates),
            )

        self.event_bus.publish(
            events.PATH_WARMING_CYCLE,
            {"warmed": warmed, "failed": failed, "candidates": len(candidates)},
        )

    def _warm_node(self, dest_hash: bytes) -> bool:
        """Request path for a single node. Returns True if path available."""
        timeout = self.config.get("request_timeout", _DEFAULT_REQUEST_TIMEOUT)

        # If path already exists and is fresh, skip
        if RNS.Transport.has_path(dest_hash):
            return False  # already reachable, not counted as "warmed"

        self.log.debug(
            "Warming path for %s", RNS.prettyhexrep(dest_hash)
        )
        RNS.Transport.request_path(dest_hash)

        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._active:
                return False
            if RNS.Transport.has_path(dest_hash):
                with self._lock:
                    self._paths_warmed += 1
                self.event_bus.publish(
                    events.PATH_WARMED,
                    {"destination_hash": dest_hash.hex(), "source": "cycle"},
                )
                return True
            time.sleep(0.5)

        with self._lock:
            self._paths_failed += 1
        self.event_bus.publish(
            events.PATH_WARM_FAILED,
            {"destination_hash": dest_hash.hex(), "source": "cycle"},
        )
        return False

    def _get_path_ages(self) -> dict[str, float]:
        """Get path ages from connectivity_monitor's cached routing data.

        Returns a dict mapping hex destination hash -> age in seconds.
        """
        ages: dict[str, float] = {}
        try:
            conn_mon = self.app.get_plugin("connectivity_monitor")
            if not conn_mon or not hasattr(conn_mon, "get_routing_data"):
                return ages

            data = conn_mon.get_routing_data(per_page=500)
            for entry in data.get("paths", []):
                h = entry.get("hash", "")
                age = entry.get("age_s", 0)
                if h:
                    ages[h] = age
        except Exception:
            self.log.debug("Error reading path ages from connectivity_monitor", exc_info=True)
        return ages

    def _build_candidates(
        self, path_ages: dict[str, float], age_threshold: float
    ) -> list[bytes]:
        """Build an ordered list of destination hashes to warm.

        Priority order:
        1. Priority nodes (from config) that need warming
        2. Recently-seen nodes (from network_map) with stale/missing paths
        """
        candidates: list[bytes] = []
        seen_hashes: set[str] = set()

        # 1. Priority nodes
        for dest_hash in self._priority_hashes:
            hex_hash = dest_hash.hex()
            if hex_hash not in seen_hashes:
                # Always include priority nodes if path is missing or stale
                age = path_ages.get(hex_hash)
                if age is None or age > age_threshold:
                    candidates.append(dest_hash)
                    seen_hashes.add(hex_hash)
                elif not RNS.Transport.has_path(dest_hash):
                    candidates.append(dest_hash)
                    seen_hashes.add(hex_hash)

        # 2. Recently-seen nodes from network_map
        if self.config.get("warm_recently_seen", True):
            recent_hours = self.config.get("warm_recent_hours", _DEFAULT_RECENT_HOURS)
            cutoff = time.time() - (recent_hours * 3600)

            try:
                net_map = self.app.get_plugin("network_map")
                if net_map and hasattr(net_map, "get_known_nodes"):
                    nodes = net_map.get_known_nodes()
                    # Filter to recently seen and sort by staleness
                    recent = [
                        n
                        for n in nodes
                        if n.get("last_seen", 0) > cutoff
                    ]
                    # Sort by path age descending (stalest first)
                    for node in recent:
                        h = node.get("destination_hash", "")
                        # Strip angle brackets and spaces from prettyhexrep
                        clean = h.replace("<", "").replace(">", "").replace(" ", "")
                        age = path_ages.get(clean)
                        node["_path_age"] = age if age is not None else float("inf")

                    recent.sort(key=lambda n: n["_path_age"], reverse=True)

                    for node in recent:
                        h = node.get("destination_hash", "")
                        clean = h.replace("<", "").replace(">", "").replace(" ", "")
                        if clean in seen_hashes:
                            continue
                        age = node.get("_path_age", float("inf"))
                        if age is None or age > age_threshold or age == float("inf"):
                            try:
                                dest_hash = bytes.fromhex(clean)
                                candidates.append(dest_hash)
                                seen_hashes.add(clean)
                            except (ValueError, TypeError):
                                pass
            except Exception:
                self.log.debug(
                    "Error reading nodes from network_map", exc_info=True
                )

        return candidates
