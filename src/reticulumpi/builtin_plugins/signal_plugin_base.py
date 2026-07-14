"""Base class for signal plugins that share an RTL-SDR dongle via the scheduler."""

from __future__ import annotations

import subprocess
import threading
import time
from abc import abstractmethod
from collections import deque
from typing import TYPE_CHECKING, Any

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase
from reticulumpi.rtlsdr import configured_device
from reticulumpi.sdr_scheduler import (
    PRIORITY_BACKGROUND,
)

if TYPE_CHECKING:
    from reticulumpi.app import ReticulumPiApp

_SDR_RESTART_DELAYS = (1.0, 2.0, 4.0, 8.0, 30.0)
_SDR_RESTART_WINDOW_SECONDS = 600.0


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
        self._dongle_selector = "index"
        self._dongle_index: int | None = None
        self._dongle_generation: int | None = None
        self._dongle_active = False
        self._process: subprocess.Popen | None = None
        self._process_group: Any | None = None
        self._snapshot_cache: dict[str, Any] = {}
        self._cache_lock = threading.Lock()
        self._preempted_by: str = ""
        self._preempted_by_label: str = ""
        self._preempted_until_ts: float | None = None
        self._locked = False
        self._receiver_lat: float | None = None
        self._receiver_lon: float | None = None
        self._sdr_retry_lock = threading.Lock()
        self._sdr_retry_times: deque[float] = deque()
        self._sdr_retry_epoch = 0
        self._sdr_retry_pending = False

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        self._dongle_serial, self._dongle_selector = configured_device(
            self.config,
            default_index="",
        )
        self._active = True
        with self._sdr_retry_lock:
            self._sdr_retry_epoch += 1
            self._sdr_retry_times.clear()
            self._sdr_retry_pending = False

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
            self.log.debug("GPS event subscription failed", exc_info=True)

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
                device_selector=self._dongle_selector,
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
        with self._sdr_retry_lock:
            self._sdr_retry_epoch += 1
            self._sdr_retry_pending = False
        try:
            self.event_bus.unsubscribe(events.GPS_FIX_RECEIVED, self._on_gps_fix)
            self.event_bus.unsubscribe(events.GPS_FIX_UPDATED, self._on_gps_fix)
        except Exception:
            self.log.debug("GPS event unsubscription failed", exc_info=True)
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
        scheduler = getattr(self.app, "sdr_scheduler", None)
        allocation_getter = getattr(type(scheduler), "get_allocation_generation", None)
        if callable(allocation_getter):
            self._dongle_generation = allocation_getter(
                scheduler,
                serial,
                self.plugin_name,
            )
        else:
            generation_getter = getattr(type(scheduler), "get_generation", None)
            self._dongle_generation = (
                generation_getter(scheduler, serial) if callable(generation_getter) else None
            )
        self._dongle_active = True
        self._preempted_by = ""
        self._preempted_by_label = ""
        self._preempted_until_ts = None
        self.log.info("Acquired dongle %s (index %d)", serial, device_index)
        try:
            self._launch_subprocess(device_index)
        except BaseException as exc:
            # The scheduler releases its canonical DeviceLease when the
            # acquire callback fails.  Keep plugin state in sync so a failed
            # or cancelled transactional launch is never reported as active.
            self._dongle_active = False
            self._dongle_index = None
            if self._active:
                self.mark_degraded(f"decoder launch failed: {exc}")
                self._schedule_sdr_retry(getattr(self, "_max_restarts", 5))
            else:
                self._dongle_generation = None
            raise
        else:
            with self._sdr_retry_lock:
                self._sdr_retry_pending = False

    def _on_yield(
        self,
        preempted_by: str,
        preempted_by_label: str,
        preempted_until_ts: float | None,
    ) -> bool:
        self._dongle_active = False
        self._dongle_generation = None
        self._locked = False
        self._preempted_by = preempted_by
        self._preempted_by_label = preempted_by_label
        self._preempted_until_ts = preempted_until_ts
        self.log.info(
            "Yielding dongle to %s (%s)",
            preempted_by,
            preempted_by_label,
        )
        self._kill_subprocess()
        self._update_snapshot_cache()
        return True

    # ── subprocess management ────────────────────────────────────────

    @abstractmethod
    def _launch_subprocess(self, device_index: int) -> None:
        """Start the signal-specific decoder subprocess."""

    def _kill_subprocess(self) -> None:
        process_group = self._process_group
        self._process_group = None
        if process_group is not None:
            self._process = None
            try:
                process_group.stop()
            except Exception:
                self.log.exception("Error stopping managed process group")
            return
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
                    except OSError:
                        pass

    def _release_dongle(self, *, suspend: bool = False) -> int | None:
        """Return the current lease, optionally suppressing reacquisition."""

        scheduler = getattr(self.app, "sdr_scheduler", None)
        if scheduler is None or not self._dongle_serial:
            self._dongle_generation = None
            return None
        registration_id: int | None = None
        suspend_method = getattr(type(scheduler), "suspend", None)
        if suspend and callable(suspend_method):
            result = suspend_method(
                scheduler,
                self._dongle_serial,
                self.plugin_name,
                generation=self._dongle_generation,
            )
            if isinstance(result, int):
                registration_id = result
        else:
            release_method = getattr(type(scheduler), "dongle_released", None)
            if callable(release_method):
                release_method(
                    scheduler,
                    self._dongle_serial,
                    self.plugin_name,
                    generation=self._dongle_generation,
                )
            else:
                scheduler.dongle_released(self._dongle_serial, self.plugin_name)
        self._dongle_generation = None
        return registration_id

    def _schedule_sdr_retry(self, configured_max_restarts: int) -> bool:
        """Release scheduler ownership and make a bounded reacquisition attempt.

        Managed process groups used by scheduler-backed decoders do not restart
        themselves: doing so would keep (or reopen) hardware during backoff
        without arbitration.  Instead, the failed group is detached, the slot
        is suspended and its lease returned, and a daemon worker merely makes
        that same registration eligible after the standard delay.  The normal
        scheduler acquisition callback is the only path that can launch the
        replacement process group.
        """

        self._process = None
        self._process_group = None
        self._dongle_active = False
        registration_id = self._release_dongle(suspend=True)
        scheduler = getattr(self.app, "sdr_scheduler", None)
        resume_method = getattr(type(scheduler), "resume", None)
        if registration_id is None or not callable(resume_method):
            with self._sdr_retry_lock:
                self._sdr_retry_epoch += 1
                self._sdr_retry_pending = False
            return False

        limit = min(5, max(0, int(configured_max_restarts)))
        now = time.monotonic()
        with self._sdr_retry_lock:
            cutoff = now - _SDR_RESTART_WINDOW_SECONDS
            while self._sdr_retry_times and self._sdr_retry_times[0] < cutoff:
                self._sdr_retry_times.popleft()
            if len(self._sdr_retry_times) >= limit:
                self._sdr_retry_epoch += 1
                self._sdr_retry_pending = False
                return False
            self._sdr_retry_times.append(now)
            attempt = len(self._sdr_retry_times)
            delay = _SDR_RESTART_DELAYS[min(attempt - 1, len(_SDR_RESTART_DELAYS) - 1)]
            self._sdr_retry_epoch += 1
            retry_epoch = self._sdr_retry_epoch
            self._sdr_retry_pending = True
            self._restart_count = getattr(self, "_restart_count", 0) + 1

        serial = self._dongle_serial
        caller = self.plugin_name

        def _resume_after_backoff() -> None:
            if self._stop_event.wait(delay) or not self._active:
                return
            with self._sdr_retry_lock:
                if retry_epoch != self._sdr_retry_epoch or not self._sdr_retry_pending:
                    return
            try:
                resumed = resume_method(
                    scheduler,
                    serial,
                    caller,
                    registration_id=registration_id,
                )
            except Exception:
                self.log.exception("SDR scheduler resume failed")
                resumed = False
            if not resumed:
                with self._sdr_retry_lock:
                    if retry_epoch == self._sdr_retry_epoch:
                        self._sdr_retry_pending = False

        self.log.warning(
            "Decoder stopped; released SDR lease and will request reacquisition "
            "in %.0fs (attempt %d/%d)",
            delay,
            attempt,
            limit,
        )
        try:
            self._start_thread(
                _resume_after_backoff,
                name=f"{self.plugin_name}-sdr-retry",
            )
        except BaseException:
            with self._sdr_retry_lock:
                if retry_epoch == self._sdr_retry_epoch:
                    self._sdr_retry_pending = False
            self.log.exception("Failed to start SDR retry worker")
            return False
        return True

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
