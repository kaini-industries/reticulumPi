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

    def validate_config(self) -> None:
        interval = self.config.get("announce_interval", 300)
        if not isinstance(interval, (int, float)) or interval < 10:
            raise ValueError("announce_interval must be >= 10 seconds")

    def start(self) -> None:
        self._active = True
        self._peer_metrics: dict[bytes, dict[str, Any]] = {}
        self._peers_lock = threading.Lock()

        app_name = self.config.get("app_name", "reticulumpi")
        aspects = self.config.get("aspects", ["node", "telemetry"])

        self.destination = RNS.Destination(
            self.identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            app_name,
            *aspects,
        )

        # Register announce handler to receive peer telemetry
        self._handler = _TelemetryHandler(self, app_name, aspects)
        RNS.Transport.register_announce_handler(self._handler)

        self._start_thread(self._announce_loop, "mesh-telemetry")

        self.log.info(
            "Mesh telemetry active at %s (interval: %ds)",
            RNS.prettyhexrep(self.destination.hash),
            self.config.get("announce_interval", 300),
        )

    def stop(self) -> None:
        self._active = False
        try:
            RNS.Transport.deregister_announce_handler(self._handler)
        except Exception:
            pass
        self._join_threads()
        self.destination = None

    def get_status(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "peer_count": len(self._peer_metrics),
        }

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
            pass
        metrics["hops"] = hops

        with self._peers_lock:
            self._peer_metrics[destination_hash] = metrics

        self.event_bus.publish(events.NODE_METRICS_RECEIVED, {
            "destination_hash": destination_hash,
            "metrics": metrics,
        })

        self.log.debug(
            "Received telemetry from %s: %s",
            RNS.prettyhexrep(destination_hash),
            {k: v for k, v in metrics.items() if k != "last_seen"},
        )

    def _announce_loop(self) -> None:
        interval = self.config.get("announce_interval", 300)
        while self._active:
            try:
                app_data = self._build_telemetry_payload()
                self.destination.announce(app_data=app_data)
                self.log.debug("Telemetry announced")
            except Exception:
                self.log.exception("Error during telemetry announce")
            self._sleep_while_active(interval)

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

        return umsgpack.packb(payload)

    def _get_app_start_time(self) -> float:
        """Approximate the app start time from uptime context."""
        # Use the system_monitor's first metric timestamp as proxy
        monitor = self.app.get_plugin("system_monitor")
        if monitor and hasattr(monitor, "latest_metrics"):
            ts = monitor.latest_metrics.get("timestamp")
            if ts:
                interval = monitor.config.get("collect_interval_seconds", 60)
                return ts - interval
        return time.time()


# Short key mapping to minimize announce payload size
_SHORT_KEYS = {
    "cpu_percent": "cpu",
    "cpu_temp": "temp",
    "memory_percent": "mem",
    "disk_percent": "disk",
}


class _TelemetryHandler:
    """Registered with RNS.Transport to receive telemetry announces."""

    def __init__(self, plugin: MeshTelemetryPlugin, app_name: str, aspects: list[str]):
        self._plugin = plugin
        # Build aspect filter from the same config used to create the destination
        self.aspect_filter = ".".join([app_name] + aspects)

    def received_announce(
        self,
        destination_hash: bytes,
        announced_identity: Any,
        app_data: bytes | None,
    ) -> None:
        try:
            self._plugin.record_peer_metrics(destination_hash, app_data)
        except Exception:
            self._plugin.log.debug("Error handling telemetry announce", exc_info=True)
