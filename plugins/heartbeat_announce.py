"""Heartbeat Announce plugin - periodically announces node presence on the network."""

import logging
import socket
import threading
import time

import RNS

from reticulumpi.plugin_base import PluginBase

log = logging.getLogger(__name__)


class HeartbeatAnnounce(PluginBase):
    """Periodically announces node presence with optional system telemetry as app_data."""

    plugin_name = "heartbeat_announce"
    plugin_version = "1.0.0"

    def start(self) -> None:
        app_name = self.config.get("app_name", "reticulumpi")
        aspects = self.config.get("aspects", ["node", "heartbeat"])

        self.destination = RNS.Destination(
            self.identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            app_name,
            *aspects,
        )

        self._active = True
        self._thread = threading.Thread(target=self._announce_loop, daemon=True, name="heartbeat")
        self._thread.start()
        log.info(
            "Heartbeat destination: %s (interval: %ds)",
            RNS.prettyhexrep(self.destination.hash),
            self.config.get("interval_seconds", 300),
        )

    def stop(self) -> None:
        self._active = False

    def _announce_loop(self) -> None:
        interval = self.config.get("interval_seconds", 300)
        while self._active:
            try:
                app_data = self._build_app_data()
                self.destination.announce(
                    app_data=app_data.encode("utf-8") if app_data else None,
                )
                log.debug("Heartbeat announced")
            except Exception:
                log.exception("Error during heartbeat announce")
            # Sleep in small increments so we can stop quickly
            for _ in range(int(interval)):
                if not self._active:
                    return
                time.sleep(1)

    def _build_app_data(self) -> str | None:
        if not self.config.get("include_telemetry", False):
            return None
        try:
            import psutil

            hostname = socket.gethostname()
            cpu = psutil.cpu_percent(interval=0)
            mem = psutil.virtual_memory().percent
            return f"{hostname}|cpu:{cpu:.0f}%|mem:{mem:.0f}%"
        except ImportError:
            return socket.gethostname()
