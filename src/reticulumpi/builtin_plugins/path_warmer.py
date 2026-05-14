"""Path Warmer plugin — proactively refreshes paths to known/important nodes."""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from typing import Any

import RNS

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

# Defaults
_DEFAULT_WARM_INTERVAL = 120
_DEFAULT_MAX_PER_CYCLE = 10
_DEFAULT_AGE_THRESHOLD = 1200  # 20 minutes
_DEFAULT_PRE_SEND_TIMEOUT = 8
_DEFAULT_PRE_SEND_TIMEOUT_INTERACTIVE = 4
_DEFAULT_REQUEST_TIMEOUT = 10
_DEFAULT_RECENT_HOURS = 24
_DEFAULT_MAX_BACKOFF = 3600  # 1 hour cap
_DEFAULT_EXPIRY_PREDICT_MINUTES = 5
_DEFAULT_ADAPTIVE_SAFETY_FACTOR = 2.0
_RTT_RING_SIZE = 20


class PathWarmerPlugin(PluginBase):
    """Proactively requests paths for nodes whose paths are stale or missing.

    Periodically warms paths for priority nodes and recently-seen nodes
    (from network_map).  Also exposes ``ensure_path()`` for LXMF-sending
    plugins to call before transmitting a message.
    """

    plugin_name = "path_warmer"
    plugin_version = "1.0.0"
    plugin_description = "Proactively refreshes paths to known/important nodes"
    broadcast_tier = 1
    broadcast_keys = "path_warming"

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
        # Exponential backoff: dest_hash_hex -> consecutive failure count
        self._failure_count: dict[str, int] = {}
        # RTT tracking: hop_count -> ring buffer of durations
        self._rtt_samples: dict[int, deque[float]] = {}

        # Parse priority nodes from config
        self._priority_hashes: list[bytes] = []
        for hex_hash in self.config.get("priority_nodes", []):
            try:
                self._priority_hashes.append(bytes.fromhex(hex_hash))
            except (ValueError, TypeError):
                self.log.warning("Invalid priority_nodes hash: %s", hex_hash)
        self._priority_set: set[str] = {h.hex() for h in self._priority_hashes}

        # Announce-triggered warming
        self._announce_warm_queue: queue.Queue[bytes] = queue.Queue(maxsize=20)
        self._announce_sub_id: str | None = None
        self._recent_node_hashes: set[str] = set()
        if self.config.get("announce_triggered_warming", True):
            self._announce_sub_id = self.announce_dispatcher.subscribe(
                None, self._on_announce_received,
            )

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
        if self._announce_sub_id:
            self.announce_dispatcher.unsubscribe(self._announce_sub_id)
            self._announce_sub_id = None
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

    def broadcast_snapshot(self, cycle_count: int = 0) -> dict | None:
        return self.get_warming_stats()

    def get_warming_stats(self) -> dict[str, Any]:
        """Full stats for dashboard API."""
        with self._lock:
            backed_off = sum(1 for v in self._failure_count.values() if v > 0)
            rtt_buckets = {
                hops: {
                    "count": len(samples),
                    "median_s": sorted(samples)[len(samples) // 2] if samples else None,
                }
                for hops, samples in self._rtt_samples.items()
            }
            return {
                "paths_warmed": self._paths_warmed,
                "paths_failed": self._paths_failed,
                "last_cycle_warmed": self._last_cycle_warmed,
                "last_cycle_time": self._last_cycle_time,
                "priority_nodes": len(self._priority_hashes),
                "backed_off_nodes": backed_off,
                "rtt_buckets": rtt_buckets,
                "warm_interval": self.config.get(
                    "warm_interval", _DEFAULT_WARM_INTERVAL
                ),
                "max_requests_per_cycle": self.config.get(
                    "max_requests_per_cycle", _DEFAULT_MAX_PER_CYCLE
                ),
            }

    def ensure_path(
        self, dest_hash: bytes, timeout: float | None = None,
        *, interactive: bool = False,
    ) -> bool:
        """Ensure a path exists to *dest_hash*, blocking up to *timeout* seconds.

        Returns ``True`` if a path is available, ``False`` on timeout.
        Intended for LXMF-sending plugins to call before transmitting.

        When *interactive* is True and no explicit *timeout* is given, a
        shorter default is used so user-initiated sends don't stall the
        dashboard for too long.
        """
        if timeout is None:
            if interactive:
                timeout = self.config.get(
                    "pre_send_timeout_interactive",
                    _DEFAULT_PRE_SEND_TIMEOUT_INTERACTIVE,
                )
            else:
                timeout = self._adaptive_timeout(dest_hash)

        if RNS.Transport.has_path(dest_hash):
            return True

        self.log.debug(
            "Requesting path for %s (pre-send, timeout=%.1fs)",
            RNS.prettyhexrep(dest_hash), timeout,
        )
        t0 = time.time()
        RNS.Transport.request_path(dest_hash)

        deadline = t0 + timeout
        while time.time() < deadline:
            if RNS.Transport.has_path(dest_hash):
                self._record_rtt(dest_hash, time.time() - t0)
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

            self._drain_announce_queue()
            self._jittered_sleep(interval)

    def _run_warm_cycle(self) -> None:
        """Execute one warming cycle."""
        max_requests = self.config.get(
            "max_requests_per_cycle", _DEFAULT_MAX_PER_CYCLE
        )
        age_threshold = self.config.get(
            "path_age_threshold", _DEFAULT_AGE_THRESHOLD
        )
        max_backoff = self.config.get("max_backoff", _DEFAULT_MAX_BACKOFF)

        # Build path data lookup from connectivity_monitor if available
        path_data = self._get_path_data()

        # Build candidate list in priority order
        candidates = self._build_candidates(path_data, age_threshold)

        # Refresh recently-seen hash cache for announce-triggered warming
        self._recent_node_hashes = {
            c.hex() for c in candidates
        }

        warmed = 0
        failed = 0
        now = time.time()
        interval = self.config.get("warm_interval", _DEFAULT_WARM_INTERVAL)

        # Prune stale rate-limit and backoff entries
        stale_cutoff = now - interval * 2
        self._last_warm_attempt = {
            k: v for k, v in self._last_warm_attempt.items() if v > stale_cutoff
        }
        self._failure_count = {
            k: v for k, v in self._failure_count.items()
            if k in self._last_warm_attempt
        }

        for dest_hash in candidates:
            if warmed + failed >= max_requests:
                break
            if not self._active:
                break

            hex_hash = dest_hash.hex()
            last = self._last_warm_attempt.get(hex_hash, 0)
            failures = self._failure_count.get(hex_hash, 0)
            if failures > 0:
                backoff = min(interval * (2 ** failures), max_backoff)
                if now - last < backoff:
                    continue
            elif now - last < interval:
                continue

            self._last_warm_attempt[hex_hash] = now
            success = self._warm_node(dest_hash)
            if success:
                self._failure_count.pop(hex_hash, None)
                warmed += 1
            else:
                self._failure_count[hex_hash] = failures + 1
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
        t0 = time.time()
        RNS.Transport.request_path(dest_hash)

        deadline = t0 + timeout
        while time.time() < deadline:
            if not self._active:
                return False
            if RNS.Transport.has_path(dest_hash):
                self._record_rtt(dest_hash, time.time() - t0)
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

    def _get_path_data(self) -> dict[str, dict[str, float]]:
        """Get path ages and expiry times from connectivity_monitor.

        Returns dict: hex_hash -> {"age_s": float, "expires_in_s": float}.
        """
        data_map: dict[str, dict[str, float]] = {}
        try:
            conn_mon = self.app.get_plugin("connectivity_monitor")
            if not conn_mon or not hasattr(conn_mon, "get_routing_data"):
                return data_map

            data = conn_mon.get_routing_data(per_page=500)
            for entry in data.get("paths", []):
                h = entry.get("hash", "")
                if h:
                    data_map[h] = {
                        "age_s": entry.get("age_s", 0),
                        "expires_in_s": entry.get("expires_in_s", 0),
                    }
        except Exception:
            self.log.debug("Error reading path data from connectivity_monitor", exc_info=True)
        return data_map

    def _build_candidates(
        self, path_data: dict[str, dict[str, float]], age_threshold: float
    ) -> list[bytes]:
        """Build an ordered list of destination hashes to warm.

        Priority order:
        1. Priority nodes (from config) that need warming
        2. Recently-seen nodes (from network_map) with stale/missing paths
        3. Paths expiring soon (within expiry_predict_minutes)
        """
        candidates: list[bytes] = []
        seen_hashes: set[str] = set()

        # 1. Priority nodes
        for dest_hash in self._priority_hashes:
            hex_hash = dest_hash.hex()
            if hex_hash not in seen_hashes:
                age = path_data.get(hex_hash, {}).get("age_s")
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
                    recent = [
                        n
                        for n in nodes
                        if n.get("last_seen", 0) > cutoff
                    ]
                    for node in recent:
                        h = node.get("destination_hash", "")
                        clean = h.replace("<", "").replace(">", "").replace(" ", "")
                        age = path_data.get(clean, {}).get("age_s")
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

        # 3. Paths expiring soon
        expiry_minutes = self.config.get(
            "expiry_predict_minutes", _DEFAULT_EXPIRY_PREDICT_MINUTES
        )
        if expiry_minutes > 0:
            expiry_threshold_s = expiry_minutes * 60
            for hex_hash, pdata in path_data.items():
                if hex_hash in seen_hashes:
                    continue
                expires_in = pdata.get("expires_in_s", 0)
                if 0 < expires_in <= expiry_threshold_s:
                    try:
                        candidates.append(bytes.fromhex(hex_hash))
                        seen_hashes.add(hex_hash)
                    except (ValueError, TypeError):
                        pass

        return candidates

    # --- RTT tracking & adaptive timeout ---

    def _record_rtt(self, dest_hash: bytes, duration: float) -> None:
        """Record a successful path request duration, bucketed by hop count."""
        try:
            hops = RNS.Transport.hops_to(dest_hash)
            if not isinstance(hops, (int, float)) or hops < 0:
                return
        except Exception:
            return
        with self._lock:
            bucket = self._rtt_samples.get(hops)
            if bucket is None:
                bucket = deque(maxlen=_RTT_RING_SIZE)
                self._rtt_samples[hops] = bucket
            bucket.append(duration)

    def _adaptive_timeout(self, dest_hash: bytes) -> float:
        """Compute adaptive timeout from hop-count RTT history."""
        fallback = self.config.get("pre_send_timeout", _DEFAULT_PRE_SEND_TIMEOUT)
        safety = self.config.get(
            "adaptive_safety_factor", _DEFAULT_ADAPTIVE_SAFETY_FACTOR
        )
        try:
            hops = RNS.Transport.hops_to(dest_hash)
            if not isinstance(hops, (int, float)) or hops < 0:
                return fallback
        except Exception:
            return fallback
        with self._lock:
            bucket = self._rtt_samples.get(hops)
            if not bucket:
                return fallback
            samples = sorted(bucket)
            median = samples[len(samples) // 2]
        return max(median * safety, 2.0)

    # --- Announce-triggered warming ---

    def _on_announce_received(
        self, destination_hash: bytes, _announced_identity: Any, _app_data: Any,
    ) -> None:
        """Lightweight callback from announce dispatcher — queues qualifying hashes."""
        hex_hash = destination_hash.hex()
        if hex_hash not in self._priority_set and hex_hash not in self._recent_node_hashes:
            return
        # Cooldown: don't re-queue within 30s
        now = time.time()
        last = self._last_warm_attempt.get(hex_hash, 0)
        interval = self.config.get("warm_interval", _DEFAULT_WARM_INTERVAL)
        if now - last < min(interval, 30):
            return
        try:
            self._announce_warm_queue.put_nowait(destination_hash)
        except queue.Full:
            pass

    def _drain_announce_queue(self) -> None:
        """Warm nodes that were queued by announce callbacks."""
        interval = self.config.get("warm_interval", _DEFAULT_WARM_INTERVAL)
        max_backoff = self.config.get("max_backoff", _DEFAULT_MAX_BACKOFF)
        now = time.time()
        drained = 0
        while drained < 5:
            try:
                dest_hash = self._announce_warm_queue.get_nowait()
            except queue.Empty:
                break
            hex_hash = dest_hash.hex()
            last = self._last_warm_attempt.get(hex_hash, 0)
            failures = self._failure_count.get(hex_hash, 0)
            if failures > 0:
                backoff = min(interval * (2 ** failures), max_backoff)
                if now - last < backoff:
                    continue
            elif now - last < min(interval, 30):
                continue
            self._last_warm_attempt[hex_hash] = now
            if self._warm_node(dest_hash):
                self._failure_count.pop(hex_hash, None)
            else:
                self._failure_count[hex_hash] = failures + 1
            drained += 1
