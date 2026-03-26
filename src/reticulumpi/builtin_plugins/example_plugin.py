"""Example plugin — a starting point for writing your own reticulumPi plugin.

This plugin creates a Reticulum destination, announces it periodically,
and handles incoming data packets with a simple echo response. It
demonstrates all the key PluginBase features:

- Config access and validation
- Destination creation and announcing
- Packet handling with request/response
- Background threads with graceful shutdown
- Inter-plugin communication
- Status reporting
- Logging

Copy this file, rename the class, and change plugin_name to get started.
"""

import threading

import RNS

from reticulumpi.plugin_base import PluginBase


class ExamplePlugin(PluginBase):
    """Announces a destination and echoes back any received data packets."""

    plugin_name = "example_plugin"
    plugin_version = "1.0.0"
    plugin_description = "Example scaffold — announces a destination and echoes packets"

    def validate_config(self) -> None:
        """Validate plugin-specific config at construction time."""
        interval = self.config.get("announce_interval", 300)
        if not isinstance(interval, (int, float)) or interval < 1:
            raise ValueError(f"announce_interval must be >= 1, got: {interval}")

    def start(self) -> None:
        self._packets_handled = 0
        self._lock = threading.Lock()

        # Create a Reticulum destination for this plugin
        app_name = self.config.get("app_name", "reticulumpi")
        aspect = self.config.get("aspect", "example")
        self.destination = RNS.Destination(
            self.identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            app_name,
            aspect,
        )

        # Register a callback for incoming packets
        self.destination.set_packet_callback(self._on_packet)

        self._active = True

        # Start a background thread for periodic announces
        self._start_thread(self._announce_loop, "example-announcer")

        self.log.info(
            "Example plugin active at %s",
            RNS.prettyhexrep(self.destination.hash),
        )

    def stop(self) -> None:
        self._active = False
        self._join_threads()
        self.destination = None

    def get_status(self) -> dict:
        """Expose plugin-specific metrics for monitoring."""
        return {
            "active": self._active,
            "packets_handled": self._packets_handled,
        }

    def _announce_loop(self) -> None:
        """Periodically announce our destination so other nodes can find us."""
        interval = self.config.get("announce_interval", 300)
        while self._active:
            try:
                # Optionally include app_data with the announce
                display_name = self.config.get("display_name", "ReticulumPi Example")
                self.destination.announce(
                    app_data=display_name.encode("utf-8"),
                )
                self.log.debug("Announced example destination")
            except Exception:
                self.log.exception("Error during announce")

            # Interruptible sleep — exits early if stop() is called
            self._sleep_while_active(interval)

    def _on_packet(self, data: bytes, packet: RNS.Packet) -> None:
        """Handle an incoming data packet by sending a proof (acknowledgement)."""
        with self._lock:
            if not self._active:
                return
            try:
                self._packets_handled += 1
                content = data.decode("utf-8")
                sender = RNS.prettyhexrep(packet.destination_hash)
                self.log.info("Received from %s: %s", sender, content[:100])

                # Example: read metrics from another plugin
                monitor = self.app.get_plugin("system_monitor")
                if monitor and hasattr(monitor, "latest_metrics"):
                    cpu = monitor.latest_metrics.get("cpu_percent", "?")
                    self.log.debug("Current CPU usage: %s%%", cpu)

                # Send a proof back to acknowledge the packet.
                # For a full bidirectional echo, set up an RNS.Link or use LXMF
                # (see the message_echo plugin for an LXMF example).
                if packet.destination.type == RNS.Destination.SINGLE:
                    packet.prove()
                    self.log.debug("Sent proof to sender")
            except Exception:
                self.log.exception("Error handling packet")
