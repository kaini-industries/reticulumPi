"""Heartbeat Announce plugin - periodically announces node presence on the network."""

import socket

import RNS

from reticulumpi.plugin_base import PluginBase


class HeartbeatAnnounce(PluginBase):
    """Periodically announces node presence with optional system telemetry as app_data."""

    plugin_name = "heartbeat_announce"
    plugin_version = "1.0.0"
    plugin_description = "Periodically announces node presence on the Reticulum network"

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
        self._thread = self._start_thread(self._announce_loop, "heartbeat")
        self.log.info(
            "Heartbeat destination: %s (interval: %ds)",
            RNS.prettyhexrep(self.destination.hash),
            self.config.get("interval_seconds", 300),
        )

    def stop(self) -> None:
        self._active = False
        self._join_threads()
        self.destination = None

    def _announce_loop(self) -> None:
        interval = self.config.get("interval_seconds", 300)
        while self._active:
            try:
                app_data = self._build_app_data()
                self.destination.announce(
                    app_data=app_data.encode("utf-8") if app_data else None,
                )
                self.log.debug("Heartbeat announced")
            except Exception:
                self.log.exception("Error during heartbeat announce")
            self._sleep_while_active(interval)

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
