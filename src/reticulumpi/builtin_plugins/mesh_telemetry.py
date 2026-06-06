"""Mesh Telemetry plugin — broadcasts and receives node metrics over Reticulum."""

from __future__ import annotations

import threading
import time
from typing import Any

import RNS

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase


class MeshTelemetryPlugin(PluginBase):
    """Announces structured system metrics and receives peer metrics.

    Creates a distributed monitoring network where each node can see the
    health of all reachable nodes on the mesh.
    """

    plugin_name = "mesh_telemetry"
    plugin_version = "1.0.0"
    plugin_description = "Distributed mesh telemetry — broadcast and receive node metrics"
    broadcast_tier = 1
    broadcast_keys = "mesh_peers"

    def validate_config(self) -> None:
        interval = self.config.get("announce_interval", 300)
        if not isinstance(interval, (int, float)) or interval < 10:
            raise ValueError("announce_interval must be >= 10 seconds")

    def start(self) -> None:
        self._active = True
        self._start_time = time.time()
        self._peer_metrics: dict[bytes, dict[str, Any]] = {}
        self._peers_lock = threading.Lock()
        self._broadcast_cache: tuple[float, list] | None = None
        self._broadcast_cache_ttl = 5.0

        app_name = self.config.get("app_name", "reticulumpi")
        aspects = self.config.get("aspects", ["node", "telemetry"])

        self.destination = RNS.Destination(
            self.identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            app_name,
            *aspects,
        )

        aspect_str = ".".join([app_name] + aspects)
        self._announce_sub = self.announce_dispatcher.subscribe(
            aspect_str,
            lambda dest, _identity, app_data: self.record_peer_metrics(dest, app_data),
        )

        self._start_thread(self._announce_loop, "mesh-telemetry")

        self.log.info(
            "Mesh telemetry active at %s (interval: %ds)",
            RNS.prettyhexrep(self.destination.hash),
            self.config.get("announce_interval", 300),
        )

    def stop(self) -> None:
        self._active = False
        self.announce_dispatcher.unsubscribe(self._announce_sub)
        self._join_threads()
        self.destination = None

    def get_status(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "peer_count": len(self._peer_metrics),
        }

    def broadcast_snapshot(self, cycle_count: int = 0) -> dict | None:
        now = time.monotonic()
        cached = self._broadcast_cache
        if cached is not None and (now - cached[0]) < self._broadcast_cache_ttl:
            return cached[1]
        peers = self.get_peer_metrics()
        result = {"peers": peers, "peer_count": len(peers)}
        self._broadcast_cache = (now, result)
        return result

    def get_peer_metrics(self) -> list[dict[str, Any]]:
        """Return all known peer metrics for API/dashboard consumption."""
        result = []
        with self._peers_lock:
            items = list(self._peer_metrics.items())
        for dest_hash, data in items:
            entry = {"destination_hash": RNS.prettyhexrep(dest_hash)}
            entry.update(data)
            result.append(entry)
        return sorted(result, key=lambda x: x.get("last_seen", 0), reverse=True)

    def record_peer_metrics(
        self,
        destination_hash: bytes,
        app_data: bytes | None,
    ) -> None:
        """Parse and store metrics received via announce."""
        if not app_data:
            return

        try:
            import RNS.vendor.umsgpack as umsgpack

            metrics = umsgpack.unpackb(app_data)
        except Exception:
            # Fall back to UTF-8 string (heartbeat-style announces)
            try:
                metrics = {"raw": app_data.decode("utf-8", errors="replace")}
            except Exception:
                return

        if not isinstance(metrics, dict):
            metrics = {"raw": str(metrics)}

        metrics["last_seen"] = time.time()

        hops = None
        try:
            hops = RNS.Transport.hops_to(destination_hash)
        except Exception:
            self.log.debug("hops_to lookup failed", exc_info=True)
        metrics["hops"] = hops

        with self._peers_lock:
            self._peer_metrics[destination_hash] = metrics

        self.event_bus.publish(
            events.NODE_METRICS_RECEIVED,
            {
                "destination_hash": destination_hash,
                "metrics": metrics,
            },
        )

        self.log.debug(
            "Received telemetry from %s: %s",
            RNS.prettyhexrep(destination_hash),
            {k: v for k, v in metrics.items() if k != "last_seen"},
        )

    def _announce_loop(self) -> None:
        interval = self.config.get("announce_interval", 300)
        peer_ttl = self.config.get("peer_ttl_seconds", 3600)
        while self._active:
            try:
                app_data = self._build_telemetry_payload()
                self.destination.announce(app_data=app_data)
                self.log.debug("Telemetry announced")
            except Exception:
                self.log.exception("Error during telemetry announce")

            # Evict stale peers whose last_seen exceeds the TTL
            self._evict_stale_peers(peer_ttl)

            self._jittered_sleep(interval)

    def _evict_stale_peers(self, ttl: float) -> None:
        """Remove peer entries not seen within *ttl* seconds."""
        now = time.time()
        stale_keys: list[bytes] = []
        with self._peers_lock:
            for key, data in self._peer_metrics.items():
                if now - data.get("last_seen", 0) > ttl:
                    stale_keys.append(key)
            for key in stale_keys:
                del self._peer_metrics[key]
        if stale_keys:
            self.log.debug("Evicted %d stale peer(s) from telemetry", len(stale_keys))

    def _build_telemetry_payload(self) -> bytes:
        """Build a compact umsgpack payload with system metrics."""
        import RNS.vendor.umsgpack as umsgpack

        include = self.config.get(
            "include_metrics",
            ["cpu_percent", "cpu_temp", "memory_percent", "disk_percent"],
        )

        payload: dict[str, Any] = {
            "name": self.app.node_name,
            "v": self.plugin_version,
            "uptime": int(time.time() - self._get_app_start_time()),
            "plugins": len(self.app.plugins),
        }

        # Read from system_monitor if available
        monitor = self.app.get_plugin("system_monitor")
        if monitor and hasattr(monitor, "latest_metrics"):
            m = monitor.latest_metrics
            for key in include:
                if key in m:
                    # Use short keys to minimize announce size
                    short = _SHORT_KEYS.get(key, key)
                    payload[short] = m[key]

        gps = self.app.get_plugin("gps_telemetry")
        if gps and hasattr(gps, "last_fix") and gps.last_fix:
            fix = gps.last_fix
            if fix.get("lat") is not None and fix.get("lon") is not None:
                payload["lat"] = round(fix["lat"], 5)
                payload["lon"] = round(fix["lon"], 5)

        return umsgpack.packb(payload)

    def _get_app_start_time(self) -> float:
        """Return the recorded start time of this plugin."""
        return getattr(self, "_start_time", time.time())


# Short key mapping to minimize announce payload size
_SHORT_KEYS = {
    "cpu_percent": "cpu",
    "cpu_temp": "temp",
    "memory_percent": "mem",
    "disk_percent": "disk",
}
