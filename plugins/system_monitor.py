"""System Monitor plugin - collects system metrics for other plugins to query."""

import logging
import threading
import time
from typing import Any

from reticulumpi.plugin_base import PluginBase

log = logging.getLogger(__name__)


class SystemMonitor(PluginBase):
    """Collects CPU, temperature, memory, and disk metrics on a timer.

    Other plugins can access metrics via app.get_plugin("system_monitor").latest_metrics.
    """

    plugin_name = "system_monitor"
    plugin_version = "1.0.0"

    def start(self) -> None:
        self._active = True
        self.latest_metrics: dict[str, Any] = {}
        self._thread = threading.Thread(target=self._collect_loop, daemon=True, name="sysmon")
        self._thread.start()
        log.info(
            "System monitor active (interval: %ds)",
            self.config.get("collect_interval_seconds", 60),
        )

    def stop(self) -> None:
        self._active = False

    def get_status(self) -> dict[str, Any]:
        return {"active": self._active, "metrics": self.latest_metrics}

    def _collect_loop(self) -> None:
        interval = self.config.get("collect_interval_seconds", 60)
        while self._active:
            try:
                self.latest_metrics = self._collect_metrics()
                log.debug("Metrics: %s", self.latest_metrics)
            except Exception:
                log.exception("Error collecting system metrics")
            for _ in range(int(interval)):
                if not self._active:
                    return
                time.sleep(1)

    def _collect_metrics(self) -> dict[str, Any]:
        import psutil

        metrics: dict[str, Any] = {
            "timestamp": time.time(),
        }

        enabled = self.config.get("metrics", ["cpu_percent", "cpu_temp", "memory_percent", "disk_percent"])

        if "cpu_percent" in enabled:
            metrics["cpu_percent"] = psutil.cpu_percent(interval=0)

        if "cpu_temp" in enabled:
            metrics["cpu_temp"] = self._read_cpu_temp()

        if "memory_percent" in enabled:
            metrics["memory_percent"] = psutil.virtual_memory().percent

        if "disk_percent" in enabled:
            metrics["disk_percent"] = psutil.disk_usage("/").percent

        return metrics

    @staticmethod
    def _read_cpu_temp() -> float | None:
        try:
            import psutil

            temps = psutil.sensors_temperatures()
            if "cpu_thermal" in temps:
                return temps["cpu_thermal"][0].current
            # Fallback for some Pi OS versions
            if "cpu-thermal" in temps:
                return temps["cpu-thermal"][0].current
        except Exception:
            pass
        return None
