"""Alert System plugin — sends LXMF alerts when thresholds are breached."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import RNS

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

# Default alert rules
_DEFAULT_RULES = [
    {"metric": "cpu_temp", "operator": ">", "threshold": 80, "message": "CPU temperature critical: {value}C"},
    {"metric": "disk_percent", "operator": ">", "threshold": 90, "message": "Disk usage critical: {value}%"},
    {"metric": "memory_percent", "operator": ">", "threshold": 90, "message": "Memory usage high: {value}%"},
]


class AlertSystemPlugin(PluginBase):
    """Monitors system conditions and sends LXMF alerts to configured recipients.

    Supports threshold-based metric alerts, plugin crash notifications,
    and node reboot detection.
    """

    plugin_name = "alert_system"
    plugin_version = "1.0.0"
    plugin_description = "LXMF alert notifications for threshold breaches and failures"

    def validate_config(self) -> None:
        recipients = self.config.get("recipients", [])
        if not isinstance(recipients, list):
            raise ValueError("recipients must be a list of LXMF address hashes")

        cooldown = self.config.get("cooldown_seconds", 300)
        if not isinstance(cooldown, (int, float)) or cooldown < 0:
            raise ValueError("cooldown_seconds must be a non-negative number")

        rules = self.config.get("rules", _DEFAULT_RULES)
        if not isinstance(rules, list):
            raise ValueError("rules must be a list")
        for rule in rules:
            if not isinstance(rule, dict):
                raise ValueError("each rule must be a dict")
            if "metric" not in rule or "operator" not in rule or "threshold" not in rule:
                raise ValueError("each rule must have metric, operator, and threshold")

    def start(self) -> None:
        self._active = True
        self._lock = threading.Lock()
        self._alerts_sent = 0
        self._last_alert: dict[str, Any] | None = None
        # Cooldown tracking: (rule_key, recipient_hex) -> last_alert_time
        self._cooldowns: dict[tuple[str, str], float] = {}

        # Parse recipients
        self._recipient_hashes: list[bytes] = []
        for hex_hash in self.config.get("recipients", []):
            try:
                cleaned = hex_hash.replace("<", "").replace(">", "").replace(" ", "")
                self._recipient_hashes.append(bytes.fromhex(cleaned))
            except ValueError:
                self.log.warning("Invalid recipient hash: %s", hex_hash)

        if not self._recipient_hashes:
            self.log.warning("No valid recipients configured — alerts will only be logged")

        # Initialize LXMF if we have recipients
        self._lxmf_router = None
        self._lxmf_destination = None
        if self._recipient_hashes:
            self._setup_lxmf()

        # Subscribe to event bus
        self.event_bus.subscribe(events.PLUGIN_CRASHED, self._on_plugin_crashed)

        # Start check loop
        self._start_thread(self._check_loop, "alert-system")

        # Reboot detection
        self._detect_reboot()

        self.log.info(
            "Alert system active (%d recipients, %d rules)",
            len(self._recipient_hashes),
            len(self.config.get("rules", _DEFAULT_RULES)),
        )

    def stop(self) -> None:
        self._active = False
        self.event_bus.unsubscribe(events.PLUGIN_CRASHED, self._on_plugin_crashed)

        # Write shutdown marker for reboot detection
        self._write_shutdown_marker()
        self._join_threads()

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._active,
                "alerts_sent": self._alerts_sent,
                "last_alert": self._last_alert,
                "recipients": len(self._recipient_hashes),
                "active_cooldowns": sum(
                    1 for t in self._cooldowns.values()
                    if time.time() - t < self.config.get("cooldown_seconds", 300)
                ),
            }

    # --- LXMF setup ---

    def _setup_lxmf(self) -> None:
        try:
            import LXMF

            storage_path = os.path.expanduser(
                self.config.get(
                    "storage_path", "~/.local/share/reticulumpi/alert_lxmf"
                )
            )
            os.makedirs(storage_path, exist_ok=True)

            # Create a separate identity for the alert system
            identity = RNS.Identity()
            self._lxmf_router = LXMF.LXMRouter(
                identity=identity,
                storagepath=storage_path,
            )
            display_name = self.config.get("display_name") or f"{self.app.node_name} Alerts"
            self._lxmf_destination = self._lxmf_router.register_delivery_identity(
                identity, display_name=display_name
            )
            self.log.info(
                "Alert LXMF destination: %s",
                RNS.prettyhexrep(self._lxmf_destination.hash),
            )
        except ImportError:
            self.log.warning("LXMF not available — alerts will only be logged")
        except Exception:
            self.log.exception("Failed to initialize LXMF for alerts")

    # --- Alert sending ---

    def _send_alert(self, message: str, rule_key: str = "") -> None:
        """Send an alert message to all configured recipients."""
        now = time.time()
        cooldown = self.config.get("cooldown_seconds", 300)

        self.log.warning("ALERT: %s", message)
        with self._lock:
            self._last_alert = {"message": message, "time": now}

        self.event_bus.publish(events.ALERT_TRIGGERED, {
            "message": message,
            "rule_key": rule_key,
            "time": now,
        })

        if not self._lxmf_router or not self._lxmf_destination:
            return

        import LXMF

        for recipient_hash in self._recipient_hashes:
            # Check cooldown
            cooldown_key = (rule_key, recipient_hash.hex())
            with self._lock:
                last_sent = self._cooldowns.get(cooldown_key, 0)
                if now - last_sent < cooldown:
                    self.log.debug(
                        "Alert suppressed (cooldown) for %s: %s",
                        RNS.prettyhexrep(recipient_hash),
                        rule_key,
                    )
                    continue

            try:
                # Create destination for recipient
                dest_identity = RNS.Identity.recall(recipient_hash)
                if dest_identity is None:
                    self.log.debug(
                        "Cannot recall identity for %s — queuing via propagation",
                        RNS.prettyhexrep(recipient_hash),
                    )

                dest = RNS.Destination(
                    dest_identity,
                    RNS.Destination.OUT,
                    RNS.Destination.SINGLE,
                    "lxmf",
                    "delivery",
                )
                dest.hash = recipient_hash

                full_message = f"[{self.app.node_name}] {message}"
                lxm = LXMF.LXMessage(
                    dest,
                    self._lxmf_destination,
                    full_message,
                    desired_method=LXMF.LXMessage.DIRECT,
                )
                self._lxmf_router.handle_outbound(lxm)
                with self._lock:
                    self._cooldowns[cooldown_key] = now
                    self._alerts_sent += 1
                self.log.info(
                    "Alert sent to %s", RNS.prettyhexrep(recipient_hash)
                )
            except Exception:
                self.log.exception(
                    "Failed to send alert to %s",
                    RNS.prettyhexrep(recipient_hash),
                )

    # --- Event handlers ---

    def _on_plugin_crashed(self, event_type: str, data: dict[str, Any]) -> None:
        if not self.config.get("alert_on_plugin_crash", True):
            return
        name = data.get("name", "unknown")
        error = data.get("error", "unknown error")
        self._send_alert(
            f"Plugin crashed: {name} — {error}",
            rule_key=f"plugin_crash:{name}",
        )

    # --- Check loop ---

    def _check_loop(self) -> None:
        """Periodically check metric thresholds."""
        check_interval = self.config.get("check_interval", 60)
        rules = self.config.get("rules", _DEFAULT_RULES)

        while self._active:
            self._sleep_while_active(check_interval)
            if not self._active:
                break

            monitor = self.app.get_plugin("system_monitor")
            if not monitor or not hasattr(monitor, "latest_metrics"):
                continue

            metrics = monitor.latest_metrics
            for rule in rules:
                metric_name = rule.get("metric", "")
                value = metrics.get(metric_name)
                if value is None:
                    continue

                threshold = rule.get("threshold", 0)
                op = rule.get("operator", ">")

                triggered = False
                if op == ">" and value > threshold:
                    triggered = True
                elif op == ">=" and value >= threshold:
                    triggered = True
                elif op == "<" and value < threshold:
                    triggered = True
                elif op == "<=" and value <= threshold:
                    triggered = True
                elif op == "==" and value == threshold:
                    triggered = True

                if triggered:
                    msg_template = rule.get("message", f"{metric_name} = {{value}}")
                    message = msg_template.format(value=value, metric=metric_name, threshold=threshold)
                    self._send_alert(message, rule_key=f"rule:{metric_name}:{op}:{threshold}")

    # --- Reboot detection ---

    def _get_shutdown_marker_path(self) -> str:
        return os.path.expanduser("~/.local/share/reticulumpi/last_shutdown")

    def _write_shutdown_marker(self) -> None:
        try:
            path = self._get_shutdown_marker_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(str(time.time()))
        except Exception:
            self.log.debug("Error writing shutdown marker", exc_info=True)

    def _detect_reboot(self) -> None:
        if not self.config.get("alert_on_reboot", True):
            return
        marker_path = self._get_shutdown_marker_path()
        if not os.path.isfile(marker_path):
            # No marker = first run or unclean shutdown
            self._send_alert("Node started (possible reboot detected)", rule_key="reboot")
            return
        try:
            with open(marker_path) as f:
                last_shutdown = float(f.read().strip())
            # If more than 5 minutes since shutdown, it's a reboot
            if time.time() - last_shutdown > 300:
                self._send_alert("Node rebooted after extended downtime", rule_key="reboot")
        except Exception:
            self._send_alert("Node started (shutdown marker unreadable)", rule_key="reboot")
