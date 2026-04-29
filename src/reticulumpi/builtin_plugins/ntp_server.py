"""NTP Server plugin — GPS-disciplined time synchronization via chrony.

Monitors chrony sync status and optionally configures GPS as a reference
clock via gpsd shared memory.  Publishes sync state transitions on the
event bus so other plugins (alerts, dashboard) can react to time quality.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

# chronyc -c tracking fields (comma-separated)
_TRACKING_FIELDS = [
    "ref_id_name", "ref_id_hex", "stratum", "ref_time",
    "system_time_offset", "last_offset", "rms_offset",
    "frequency", "residual_freq", "skew", "root_delay",
    "root_dispersion", "update_interval", "leap_status",
]

# chronyc -c sources fields (comma-separated)
_SOURCE_FIELDS = [
    "mode", "state", "name", "stratum", "poll", "reach",
    "last_rx", "last_sample_offset", "last_sample_error",
]

# Source state codes from chronyc
_SOURCE_STATES = {
    "*": "synced", "+": "candidate", "-": "not_combined",
    "?": "unreachable", "x": "false_ticker", "~": "too_variable",
}


class NtpServerPlugin(PluginBase):
    """GPS-disciplined NTP time synchronization via chrony."""

    plugin_name = "ntp_server"
    plugin_version = "1.0.0"
    plugin_description = "GPS-disciplined NTP time synchronization via chrony"

    def validate_config(self) -> None:
        interval = self.config.get("check_interval", 30)
        if not isinstance(interval, (int, float)) or interval < 5:
            raise ValueError("check_interval must be >= 5")

        conf_dir = self.config.get("chrony_conf_dir", "/etc/chrony/conf.d")
        if not isinstance(conf_dir, str) or not conf_dir:
            raise ValueError("chrony_conf_dir must be a non-empty string")

        threshold = self.config.get("sync_loss_threshold", 300)
        if not isinstance(threshold, (int, float)) or threshold < 0:
            raise ValueError("sync_loss_threshold must be >= 0")

    def start(self) -> None:
        self._active = True
        self._lock = threading.Lock()
        self._start_time = time.time()
        self._check_interval = self.config.get("check_interval", 30)

        # Sync state
        self._sync_state: str = "unknown"
        self._prev_sync_state: str = "unknown"
        self._last_synced_time: float = 0.0
        self._sync_lost_alerted = False

        # Chrony data
        self._tracking: dict[str, Any] = {}
        self._sources: list[dict[str, Any]] = []
        self._last_check: float = 0.0
        self._check_errors = 0

        # GPS refclock state
        self._gps_refclock_active = False
        self._gps_refclock_configured = False
        self._manage_chrony = self.config.get("manage_chrony_config", True)
        self._conf_dir = self.config.get("chrony_conf_dir", "/etc/chrony/conf.d")
        self._conf_path = os.path.join(self._conf_dir, "reticulumpi-gps.conf")
        self._use_sudo = self.config.get("sudo_chronyc", True)

        # Subscribe to GPS events for auto-configuration
        gps_cfg = self.config.get("gps_refclock", {})
        if gps_cfg.get("enabled", True) and self._manage_chrony:
            self.event_bus.subscribe(events.GPS_FIX_RECEIVED, self._on_gps_fix)
            self.event_bus.subscribe(events.GPS_FIX_LOST, self._on_gps_lost)

        # Start monitor thread
        self._start_thread(self._monitor_loop, "ntp-monitor")
        self.log.info("NTP server monitor started (interval=%ds)", self._check_interval)

    def stop(self) -> None:
        self._active = False
        # Unsubscribe from GPS events
        try:
            self.event_bus.unsubscribe(events.GPS_FIX_RECEIVED, self._on_gps_fix)
            self.event_bus.unsubscribe(events.GPS_FIX_LOST, self._on_gps_lost)
        except Exception:
            pass
        self._join_threads(timeout=5)

    # ── Public API ─────────────────────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._active,
                "sync_state": self._sync_state,
                "gps_refclock_active": self._gps_refclock_active,
                "gps_refclock_configured": self._gps_refclock_configured,
                "stratum": self._tracking.get("stratum"),
                "ref_id": self._tracking.get("ref_id_name"),
                "offset_ms": self._tracking.get("system_time_offset_ms"),
                "last_check_age_s": (
                    round(time.time() - self._last_check, 1) if self._last_check else None
                ),
                "check_errors": self._check_errors,
                "uptime": time.time() - self._start_time,
            }

    def get_snapshot(self) -> dict[str, Any]:
        snap = self.get_status()
        with self._lock:
            snap["tracking"] = dict(self._tracking)
            snap["sources"] = [dict(s) for s in self._sources]
            snap["sources_count"] = len(self._sources)
        return snap

    # ── Monitor loop ───────────────────────────────────────────────────

    def _monitor_loop(self) -> None:
        while self._active:
            try:
                tracking = self._run_chronyc("tracking")
                sources = self._run_chronyc("sources")

                parsed_tracking = self._parse_tracking(tracking)
                parsed_sources = self._parse_sources(sources)

                with self._lock:
                    self._tracking = parsed_tracking
                    self._sources = parsed_sources
                    self._last_check = time.time()
                    self._prev_sync_state = self._sync_state
                    self._sync_state = self._determine_sync_state(
                        parsed_tracking, parsed_sources,
                    )

                self._handle_state_transitions()

                self.event_bus.publish(events.NTP_STATUS_UPDATED, {
                    "sync_state": self._sync_state,
                    "stratum": parsed_tracking.get("stratum"),
                    "offset_ms": parsed_tracking.get("system_time_offset_ms"),
                    "sources_count": len(parsed_sources),
                })

            except Exception:
                self._check_errors += 1
                self.log.debug("chronyc check failed", exc_info=True)

            self._sleep_while_active(self._check_interval)

    def _run_chronyc(self, command: str) -> str:
        cmd = ["chronyc", "-c", command]
        if self._use_sudo:
            cmd = ["sudo", "-n"] + cmd
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"chronyc {command} failed (rc={result.returncode}): "
                f"{result.stderr.strip()[:200]}"
            )
        return result.stdout.strip()

    # ── Parsing ────────────────────────────────────────────────────────

    def _parse_tracking(self, output: str) -> dict[str, Any]:
        if not output:
            return {}
        parts = output.split(",")
        result: dict[str, Any] = {}

        for i, field in enumerate(_TRACKING_FIELDS):
            if i >= len(parts):
                break
            val = parts[i].strip()
            if field == "stratum":
                try:
                    result[field] = int(val)
                except ValueError:
                    result[field] = val
            elif field in (
                "system_time_offset", "last_offset", "rms_offset",
                "root_delay", "root_dispersion", "frequency",
                "residual_freq", "skew", "update_interval",
            ):
                try:
                    result[field] = float(val)
                except ValueError:
                    result[field] = val
            else:
                result[field] = val

        # Add human-friendly offset in milliseconds
        offset = result.get("system_time_offset")
        if isinstance(offset, float):
            result["system_time_offset_ms"] = round(offset * 1000, 3)

        return result

    def _parse_sources(self, output: str) -> list[dict[str, Any]]:
        if not output:
            return []
        sources = []
        for line in output.splitlines():
            parts = line.split(",")
            if len(parts) < 2:
                continue
            source: dict[str, Any] = {}
            for i, field in enumerate(_SOURCE_FIELDS):
                if i >= len(parts):
                    break
                val = parts[i].strip()
                if field == "mode":
                    source[field] = val
                elif field == "state":
                    source[field] = val
                    source["state_label"] = _SOURCE_STATES.get(val, "unknown")
                elif field in ("stratum", "poll", "reach"):
                    try:
                        source[field] = int(val)
                    except ValueError:
                        source[field] = val
                elif field in ("last_rx", "last_sample_offset", "last_sample_error"):
                    try:
                        source[field] = float(val)
                    except ValueError:
                        source[field] = val
                else:
                    source[field] = val

            # Offset in ms for display
            offset = source.get("last_sample_offset")
            if isinstance(offset, float):
                source["offset_ms"] = round(offset * 1000, 3)

            sources.append(source)
        return sources

    # ── Sync state logic ───────────────────────────────────────────────

    def _determine_sync_state(
        self,
        tracking: dict[str, Any],
        sources: list[dict[str, Any]],
    ) -> str:
        stratum = tracking.get("stratum")
        ref_id = tracking.get("ref_id_name", "")
        leap = tracking.get("leap_status", "")

        if leap == "Not synchronised" or stratum is None or stratum == 0:
            return "unsynced"

        # Check if GPS is the active reference
        if ref_id in ("GPS", "PPS", "SHM0", "SHM1"):
            return "gps_disciplined"

        # Check if any source is synced
        for src in sources:
            if src.get("state") == "*":
                return "synced"

        if isinstance(stratum, int) and stratum > 0:
            return "synced"

        return "unsynced"

    def _handle_state_transitions(self) -> None:
        prev = self._prev_sync_state
        curr = self._sync_state

        if prev == curr:
            # Check for prolonged sync loss
            if curr == "unsynced" and not self._sync_lost_alerted:
                threshold = self.config.get("sync_loss_threshold", 300)
                if self._last_synced_time > 0:
                    lost_duration = time.time() - self._last_synced_time
                    if lost_duration >= threshold and self.config.get("alert_on_sync_loss", True):
                        self._sync_lost_alerted = True
                        self.event_bus.publish(events.NTP_SYNC_LOST, {
                            "last_sync_age_s": round(lost_duration, 1),
                        })
                        self.log.warning(
                            "NTP sync lost for %.0fs (threshold: %ds)",
                            lost_duration, threshold,
                        )
            return

        # State changed
        if curr in ("synced", "gps_disciplined"):
            self._last_synced_time = time.time()
            self._sync_lost_alerted = False
            self.event_bus.publish(events.NTP_SYNC_ACQUIRED, {
                "stratum": self._tracking.get("stratum"),
                "ref_id": self._tracking.get("ref_id_name"),
                "offset_ms": self._tracking.get("system_time_offset_ms"),
            })
            self.log.info(
                "NTP sync acquired: stratum %s, ref %s",
                self._tracking.get("stratum"),
                self._tracking.get("ref_id_name"),
            )
        elif curr == "unsynced" and prev in ("synced", "gps_disciplined"):
            self.log.warning("NTP sync state changed to unsynced")

    # ── GPS refclock management ────────────────────────────────────────

    def _on_gps_fix(self, event_type: str, data: dict[str, Any]) -> None:
        if self._gps_refclock_configured:
            return
        if not self._manage_chrony:
            return
        try:
            self._configure_gps_refclock()
        except Exception:
            self.log.debug("Failed to configure GPS refclock", exc_info=True)

    def _on_gps_lost(self, event_type: str, data: dict[str, Any]) -> None:
        # Do NOT remove refclock config — chrony handles GPS outages gracefully
        with self._lock:
            self._gps_refclock_active = False
        self.log.debug("GPS fix lost — chrony will handle source unavailability")

    def _configure_gps_refclock(self) -> None:
        gps_cfg = self.config.get("gps_refclock", {})
        if not gps_cfg.get("enabled", True):
            return

        shm_segment = gps_cfg.get("shm_segment", 0)
        precision = gps_cfg.get("precision", "1e-1")
        offset = gps_cfg.get("offset", 0.0)
        delay = gps_cfg.get("delay", 0.2)
        pps_device = gps_cfg.get("pps_device")
        pps_precision = gps_cfg.get("pps_precision", "1e-9")

        lines = [
            f"# ReticulumPi GPS refclock — managed by ntp_server plugin",
            f"refclock SHM {shm_segment} refid GPS precision {precision} "
            f"offset {offset} delay {delay}",
        ]
        if pps_device:
            lines.append(
                f"refclock PPS {pps_device} refid PPS precision {pps_precision} lock GPS"
            )
        content = "\n".join(lines) + "\n"

        # Write config snippet
        try:
            if self._use_sudo:
                subprocess.run(
                    ["sudo", "-n", "tee", self._conf_path],
                    input=content, capture_output=True, text=True,
                    timeout=5, check=True,
                )
            else:
                os.makedirs(self._conf_dir, exist_ok=True)
                with open(self._conf_path, "w") as f:
                    f.write(content)
        except Exception as exc:
            self.log.warning("Could not write chrony GPS config: %s", exc)
            return

        # Restart chrony to pick up the new config.
        # chronyc reload sources returns "501 Not authorised" when called from
        # inside systemd's ProtectSystem=strict mount namespace, so we go
        # through systemctl which talks to PID-1 via D-Bus instead.
        try:
            restart_cmd = ["systemctl", "restart", "chrony"]
            if self._use_sudo:
                restart_cmd = ["sudo", "-n"] + restart_cmd
            subprocess.run(
                restart_cmd, capture_output=True, text=True,
                timeout=30, check=True,
            )
        except Exception as exc:
            self.log.warning("Could not restart chrony: %s", exc)
            return

        with self._lock:
            self._gps_refclock_configured = True
            self._gps_refclock_active = True

        self.event_bus.publish(events.NTP_GPS_REFCLOCK_ACTIVE, {
            "shm_segment": shm_segment,
            "pps_device": pps_device,
        })
        self.log.info(
            "GPS refclock configured (SHM %d%s)",
            shm_segment,
            f" + PPS {pps_device}" if pps_device else "",
        )

    def _remove_gps_refclock(self) -> None:
        try:
            if self._use_sudo:
                subprocess.run(
                    ["sudo", "-n", "rm", "-f", self._conf_path],
                    capture_output=True, text=True, timeout=5,
                )
            elif os.path.exists(self._conf_path):
                os.remove(self._conf_path)

            restart_cmd = ["systemctl", "restart", "chrony"]
            if self._use_sudo:
                restart_cmd = ["sudo", "-n"] + restart_cmd
            subprocess.run(
                restart_cmd, capture_output=True, text=True, timeout=30,
            )
        except Exception:
            self.log.debug("Error removing GPS refclock config", exc_info=True)

        with self._lock:
            self._gps_refclock_configured = False
            self._gps_refclock_active = False
