"""Base class for signal plugins that share an RTL-SDR dongle via the scheduler."""

from __future__ import annotations

import subprocess
import threading
from abc import abstractmethod
from typing import TYPE_CHECKING, Any

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase
from reticulumpi.sdr_scheduler import (
    PRIORITY_BACKGROUND,
)

if TYPE_CHECKING:
    from reticulumpi.app import ReticulumPiApp


class SignalPluginBase(PluginBase):
    """Mixin for plugins sharing a scheduled RTL-SDR dongle.

    Subclasses implement ``_launch_subprocess`` and ``_parse_output``
    instead of managing the dongle lifecycle directly.
    """

    signal_priority: int = PRIORITY_BACKGROUND
    signal_continuous: bool = True
    signal_label: str = ""

    broadcast_tier = 2

    def __init__(self, app: "ReticulumPiApp", plugin_config: dict[str, Any]):
        super().__init__(app, plugin_config)
        self._dongle_serial: str = ""
        self._dongle_index: int | None = None
        self._dongle_active = False
        self._process: subprocess.Popen | None = None
        self._snapshot_cache: dict[str, Any] = {}
        self._cache_lock = threading.Lock()
        self._preempted_by: str = ""
        self._preempted_by_label: str = ""
        self._preempted_until_ts: float | None = None
        self._locked = False
        self._receiver_lat: float | None = None
        self._receiver_lon: float | None = None

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        self._dongle_serial = str(
            self.config.get("device_serial")
            or self.config.get("device_index", ""),
        )
        self._active = True

        self._receiver_lat = self.config.get("receiver_lat")
        self._receiver_lon = self.config.get("receiver_lon")
        if self._receiver_lat is not None:
            self._receiver_lat = float(self._receiver_lat)
        if self._receiver_lon is not None:
            self._receiver_lon = float(self._receiver_lon)

        try:
            self.event_bus.subscribe(events.GPS_FIX_RECEIVED, self._on_gps_fix)
            self.event_bus.subscribe(events.GPS_FIX_UPDATED, self._on_gps_fix)
        except Exception:
            pass

        sched = getattr(self.app, "sdr_scheduler", None)
        if sched is not None and self._dongle_serial:
            sched.register(
                serial=self._dongle_serial,
                caller=self.plugin_name,
                priority=self.signal_priority,
                acquire_cb=self._on_acquire,
                yield_cb=self._on_yield,
                label=self.signal_label or self.plugin_description,
                continuous=self.signal_continuous,
            )

        self._on_start()

    def _on_gps_fix(self, _event_type: str, data: dict) -> None:
        lat = data.get("lat")
        lon = data.get("lon")
        if lat is not None and lon is not None:
            self._receiver_lat = float(lat)
            self._receiver_lon = float(lon)

    def stop(self) -> None:
        self._active = False
        try:
            self.event_bus.unsubscribe(events.GPS_FIX_RECEIVED, self._on_gps_fix)
            self.event_bus.unsubscribe(events.GPS_FIX_UPDATED, self._on_gps_fix)
        except Exception:
            pass
        sched = getattr(self.app, "sdr_scheduler", None)
        if sched is not None and self._dongle_serial:
            sched.unregister(self._dongle_serial, self.plugin_name)
        self._kill_subprocess()
        self._join_threads(timeout=5.0)
        self._on_stop()

    def _on_start(self) -> None:
        """Hook for subclass-specific start logic (after scheduler registration)."""

    def _on_stop(self) -> None:
        """Hook for subclass-specific stop logic (after unregistration)."""

    # ── scheduler callbacks ──────────────────────────────────────────

    def _on_acquire(self, serial: str, device_index: int) -> None:
        if not self._active:
            return
        self._dongle_index = device_index
        self._dongle_active = True
        self._preempted_by = ""
        self._preempted_by_label = ""
        self._preempted_until_ts = None
        self.log.info("Acquired dongle %s (index %d)", serial, device_index)
        self._launch_subprocess(device_index)

    def _on_yield(
        self,
        preempted_by: str,
        preempted_by_label: str,
        preempted_until_ts: float | None,
    ) -> bool:
        self._dongle_active = False
        self._locked = False
        self._preempted_by = preempted_by
        self._preempted_by_label = preempted_by_label
        self._preempted_until_ts = preempted_until_ts
        self.log.info(
            "Yielding dongle to %s (%s)",
            preempted_by, preempted_by_label,
        )
        self._kill_subprocess()
        self._update_snapshot_cache()
        return True

    # ── subprocess management ────────────────────────────────────────

    @abstractmethod
    def _launch_subprocess(self, device_index: int) -> None:
        """Start the signal-specific decoder subprocess."""

    def _kill_subprocess(self) -> None:
        proc = self._process
        self._process = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.log.warning("Subprocess did not stop; sending SIGKILL")
                    proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
        except Exception:
            self.log.exception("Error stopping subprocess")
        finally:
            for f in (proc.stdout, proc.stderr):
                if f:
                    try:
                        f.close()
                    except Exception:
                        pass

    # ── snapshot ─────────────────────────────────────────────────────

    def _update_snapshot_cache(self) -> None:
        """Rebuild the pre-computed snapshot dict.

        Called by the parser thread when new data arrives, and after yield.
        Subclasses should override to populate ``_snapshot_cache``.
        """

    def get_snapshot(self) -> dict[str, Any]:
        with self._cache_lock:
            snap = dict(self._snapshot_cache)
        snap["dongle_active"] = self._dongle_active
        if not self._dongle_active and self._preempted_by:
            snap["preempted_by"] = self._preempted_by
            snap["preempted_by_label"] = self._preempted_by_label
            snap["preempted_until_ts"] = self._preempted_until_ts
        snap["locked"] = self._locked
        return snap

    def broadcast_snapshot(self, cycle_count: int = 0) -> dict[str, Any] | None:
        with self._cache_lock:
            has_data = bool(self._snapshot_cache)
        if not self._dongle_active and not has_data:
            return None
        return self.get_snapshot()

    # ── lock ─────────────────────────────────────────────────────────

    def lock_dongle(self) -> bool:
        if not self._dongle_active:
            return False
        sched = getattr(self.app, "sdr_scheduler", None)
        if sched is None:
            return False
        if not sched.lock(self._dongle_serial, self.plugin_name):
            return False
        self._locked = True
        return True

    def unlock_dongle(self) -> bool:
        sched = getattr(self.app, "sdr_scheduler", None)
        if sched is None:
            return False
        sched.unlock(self._dongle_serial, self.plugin_name)
        self._locked = False
        return True
