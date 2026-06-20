"""SDR dongle time-sharing scheduler.

Manages priority-based access to shared RTL-SDR dongles so multiple
signal plugins can take turns using the same physical device.

Priority tiers:
  P0 (critical)  — safety-of-life signals (weather alerts)
  P1 (scheduled)  — time-bounded captures (satellite passes, radiosonde windows)
  P2 (background) — continuous decoders (AIS, ACARS, FM receiver)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from reticulumpi import events

log = logging.getLogger(__name__)

PRIORITY_CRITICAL = 0
PRIORITY_SCHEDULED = 1
PRIORITY_BACKGROUND = 2

_USB_SETTLE_DELAY = 0.5


@dataclass
class TimeWindow:
    """A scheduled time range for a signal plugin."""

    start_ts: float
    end_ts: float
    caller: str
    label: str = ""


@dataclass
class SignalSlot:
    """Registration record for one signal plugin on one dongle."""

    caller: str
    serial: str
    priority: int
    acquire_cb: Callable[[str, int], None]
    yield_cb: Callable[[str, str, float | None], bool]
    label: str = ""
    continuous: bool = False
    windows: list[TimeWindow] = field(default_factory=list)
    is_active: bool = False
    last_acquired: float = 0.0
    last_yielded: float = 0.0


@dataclass
class DongleState:
    """Per-dongle scheduler state."""

    serial: str
    device_index: int | None = None
    current_holder: str | None = None
    locked_by: str | None = None
    relock_after: str | None = None
    slots: dict[str, SignalSlot] = field(default_factory=dict)
    bg_order: list[str] = field(default_factory=list)
    bg_index: int = 0
    bg_last_rotation: float = 0.0
    default_signal: str = ""
    bg_slice_seconds: float = 120.0
    generation: int = 0


class SdrScheduler:
    """Central dongle time-sharing coordinator."""

    def __init__(self, event_bus: Any, config: dict[str, Any] | None = None) -> None:
        self._event_bus = event_bus
        self._config = config or {}
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._dongles: dict[str, DongleState] = {}
        self._thread: threading.Thread | None = None
        self._running = False
        self._weather_override_lock = self._config.get(
            "weather_alerts_override_lock",
            True,
        )
        self._init_dongles()

    def _init_dongles(self) -> None:
        managed = self._config.get("managed_dongles", [])
        for d in managed:
            serial = str(d.get("serial", ""))
            if not serial:
                continue
            state = DongleState(
                serial=serial,
                default_signal=d.get("default_signal", ""),
                bg_slice_seconds=float(d.get("background_slice_seconds", 120)),
            )
            self._dongles[serial] = state

    def add_dongle(self, serial: str, **kwargs: Any) -> None:
        with self._lock:
            if serial not in self._dongles:
                self._dongles[serial] = DongleState(serial=serial, **kwargs)

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._scheduler_loop,
            name="sdr-scheduler",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "SDR scheduler started, managing %d dongle(s)",
            len(self._dongles),
        )

    def stop(self) -> None:
        with self._condition:
            self._running = False
            for dongle in self._dongles.values():
                if dongle.current_holder:
                    self._do_yield(dongle, dongle.current_holder, "", "", None)
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        log.info("SDR scheduler stopped")

    # ── registration ─────────────────────────────────────────────────

    def register(
        self,
        serial: str,
        caller: str,
        priority: int,
        acquire_cb: Callable[[str, int], None],
        yield_cb: Callable[[str, str, float | None], bool],
        label: str = "",
        windows: list[TimeWindow] | None = None,
        continuous: bool = False,
    ) -> None:
        if priority == PRIORITY_CRITICAL and not continuous:
            raise ValueError("P0/critical slots must be continuous")

        with self._condition:
            dongle = self._dongles.get(serial)
            if dongle is None:
                dongle = DongleState(serial=serial)
                self._dongles[serial] = dongle

            slot = SignalSlot(
                caller=caller,
                serial=serial,
                priority=priority,
                acquire_cb=acquire_cb,
                yield_cb=yield_cb,
                label=label,
                continuous=continuous,
                windows=list(windows) if windows else [],
            )
            dongle.slots[caller] = slot

            if priority == PRIORITY_BACKGROUND and continuous:
                if caller not in dongle.bg_order:
                    dongle.bg_order.append(caller)

            log.info(
                "Registered %s on dongle %s (P%d, %s)",
                caller,
                serial,
                priority,
                "continuous" if continuous else f"{len(slot.windows)} windows",
            )
            self._condition.notify_all()

    def unregister(self, serial: str, caller: str) -> None:
        with self._condition:
            dongle = self._dongles.get(serial)
            if dongle is None:
                return

            slot = dongle.slots.get(caller)
            if slot and slot.is_active:
                self._do_yield(dongle, caller, "", "", None)
            dongle.slots.pop(caller, None)

            if caller in dongle.bg_order:
                dongle.bg_order.remove(caller)

            if dongle.locked_by == caller:
                dongle.locked_by = None

            log.info("Unregistered %s from dongle %s", caller, serial)
            self._condition.notify_all()

    # ── time windows ─────────────────────────────────────────────────

    def add_window(
        self,
        serial: str,
        caller: str,
        start_ts: float,
        end_ts: float,
        label: str = "",
    ) -> None:
        with self._condition:
            dongle = self._dongles.get(serial)
            if dongle is None:
                return
            slot = dongle.slots.get(caller)
            if slot is None:
                return
            slot.windows.append(
                TimeWindow(
                    start_ts=start_ts,
                    end_ts=end_ts,
                    caller=caller,
                    label=label,
                )
            )
            slot.windows.sort(key=lambda w: w.start_ts)
            window_count = len(slot.windows)
            self._condition.notify_all()

        try:
            self._event_bus.publish(
                events.SDR_SCHEDULE_UPDATED,
                {
                    "serial": serial,
                    "caller": caller,
                    "window_count": window_count,
                },
            )
        except Exception:
            log.debug("event publish failed", exc_info=True)

    def remove_windows(self, serial: str, caller: str) -> None:
        with self._condition:
            dongle = self._dongles.get(serial)
            if dongle is None:
                return
            slot = dongle.slots.get(caller)
            if slot is not None:
                slot.windows.clear()
            self._condition.notify_all()

    # ── lock support ─────────────────────────────────────────────────

    def lock(self, serial: str, caller: str) -> bool:
        with self._condition:
            dongle = self._dongles.get(serial)
            if dongle is None:
                return False
            if dongle.current_holder != caller:
                log.warning(
                    "lock() rejected: %s is not current holder of %s",
                    caller,
                    serial,
                )
                return False
            dongle.locked_by = caller
            log.info("Dongle %s locked by %s", serial, caller)
            return True

    def unlock(self, serial: str, caller: str) -> None:
        with self._condition:
            dongle = self._dongles.get(serial)
            if dongle is None:
                return
            if dongle.locked_by != caller:
                return
            dongle.locked_by = None
            log.info("Dongle %s unlocked by %s", serial, caller)
            self._condition.notify_all()

    # ── scheduler loop ───────────────────────────────────────────────

    def _scheduler_loop(self) -> None:
        while self._running:
            with self._condition:
                for serial in list(self._dongles):
                    try:
                        self._evaluate(serial)
                    except Exception:
                        log.exception(
                            "Error evaluating dongle %s",
                            serial,
                        )
                self._condition.wait(timeout=1.0)

    def _evaluate(self, serial: str) -> None:
        dongle = self._dongles[serial]
        now = time.time()

        self._expire_windows(dongle, now)

        winner = self._pick_winner(dongle, now)
        if winner is None and dongle.current_holder is None:
            return
        if winner == dongle.current_holder:
            return

        if winner is None and dongle.current_holder is not None:
            current_slot = dongle.slots.get(dongle.current_holder)
            if current_slot and current_slot.priority == PRIORITY_SCHEDULED:
                self._do_yield(dongle, dongle.current_holder, "", "", None)
                bg = self._pick_background(dongle, now)
                if bg:
                    self._do_acquire(dongle, bg)
            return

        current = dongle.current_holder
        if current is not None:
            if not self._can_preempt(dongle, current, winner):
                return
            winner_slot = dongle.slots.get(winner)
            winner_label = winner_slot.label if winner_slot else winner
            winner_end = self._window_end(dongle, winner, now)
            self._do_yield(dongle, current, winner, winner_label, winner_end)

        self._do_acquire(dongle, winner)

    def _pick_winner(self, dongle: DongleState, now: float) -> str | None:
        best: str | None = None
        best_priority = 999

        for caller, slot in dongle.slots.items():
            if slot.priority == PRIORITY_CRITICAL and slot.continuous:
                if slot.priority < best_priority:
                    best = caller
                    best_priority = slot.priority

            elif slot.priority == PRIORITY_SCHEDULED:
                for w in slot.windows:
                    if w.start_ts <= now <= w.end_ts:
                        if slot.priority < best_priority:
                            best = caller
                            best_priority = slot.priority
                        break

        if best is not None:
            return best

        return self._pick_background(dongle, now)

    def _pick_background(self, dongle: DongleState, now: float) -> str | None:
        bg = dongle.bg_order
        if not bg:
            return None

        should_rotate = False
        if dongle.current_holder in bg:
            elapsed = now - dongle.bg_last_rotation
            if elapsed < dongle.bg_slice_seconds:
                return dongle.current_holder
            should_rotate = True

        if should_rotate:
            dongle.bg_index += 1

        for _ in range(len(bg)):
            if not bg:
                return None
            idx = dongle.bg_index % len(bg)
            candidate = bg[idx]
            slot = dongle.slots.get(candidate)
            if slot is not None:
                return candidate
            bg.pop(idx)
            # After removal, adjust index so we don't skip the element
            # that slid into the vacated position.
            if bg and idx < dongle.bg_index:
                dongle.bg_index -= 1

        return None

    def _can_preempt(
        self,
        dongle: DongleState,
        current: str,
        winner: str,
    ) -> bool:
        if dongle.locked_by == current:
            winner_slot = dongle.slots.get(winner)
            winner_priority = winner_slot.priority if winner_slot else 999
            if winner_priority == PRIORITY_CRITICAL and self._weather_override_lock:
                log.info(
                    "P0 signal %s overrides lock held by %s",
                    winner,
                    current,
                )
                return True
            log.debug(
                "Dongle %s locked by %s — skipping P%d signal %s",
                dongle.serial,
                current,
                winner_priority,
                winner,
            )
            return False

        current_slot = dongle.slots.get(current)
        winner_slot = dongle.slots.get(winner)
        if current_slot is None or winner_slot is None:
            return True
        if (
            winner_slot.priority == PRIORITY_BACKGROUND
            and current_slot.priority == PRIORITY_BACKGROUND
        ):
            return True
        return winner_slot.priority < current_slot.priority

    def _window_end(
        self,
        dongle: DongleState,
        caller: str,
        now: float,
    ) -> float | None:
        slot = dongle.slots.get(caller)
        if slot is None:
            return None
        for w in slot.windows:
            if w.start_ts <= now <= w.end_ts:
                return w.end_ts
        return None

    def _expire_windows(self, dongle: DongleState, now: float) -> None:
        for slot in dongle.slots.values():
            slot.windows = [w for w in slot.windows if w.end_ts > now]

    # ── handoff ──────────────────────────────────────────────────────

    def _do_yield(
        self,
        dongle: DongleState,
        caller: str,
        preempted_by: str,
        preempted_by_label: str,
        preempted_until_ts: float | None,
    ) -> None:
        slot = dongle.slots.get(caller)
        if slot is None:
            dongle.current_holder = None
            return

        log.info(
            "Yielding dongle %s from %s (preempted by %s)",
            dongle.serial,
            caller,
            preempted_by or "none",
        )

        slot.is_active = False
        slot.last_yielded = time.time()
        dongle.current_holder = None

        was_locked = dongle.locked_by == caller
        if was_locked and preempted_by:
            dongle.locked_by = None
            dongle.relock_after = caller
        elif dongle.relock_after == caller:
            dongle.relock_after = None

        yield_cb = slot.yield_cb
        serial = dongle.serial

        self._condition.release()
        try:
            try:
                yield_cb(preempted_by, preempted_by_label, preempted_until_ts)
            except Exception:
                log.exception("yield_cb failed for %s", caller)

            from reticulumpi.rtlsdr import release_device

            try:
                release_device(serial, caller=caller)
            except Exception:
                log.debug("SDR device release failed for %s", caller, exc_info=True)

            try:
                self._event_bus.publish(
                    events.SDR_DONGLE_YIELDED,
                    {
                        "serial": serial,
                        "caller": caller,
                        "preempted_by": preempted_by,
                    },
                )
            except Exception:
                log.debug("event publish failed", exc_info=True)
        finally:
            self._condition.acquire()

    def _do_acquire(self, dongle: DongleState, caller: str) -> None:
        slot = dongle.slots.get(caller)
        if slot is None:
            return

        acquire_cb = slot.acquire_cb
        priority = slot.priority
        serial = dongle.serial

        idx = None
        acquire_ok = False

        self._condition.release()
        try:
            time.sleep(_USB_SETTLE_DELAY)

            from reticulumpi.rtlsdr import resolve_device

            try:
                idx = resolve_device(serial, caller=caller)
            except RuntimeError as exc:
                log.error(
                    "Failed to claim dongle %s for %s: %s",
                    serial,
                    caller,
                    exc,
                )
                return

            log.info("Granting dongle %s to %s (index %s)", serial, caller, idx)

            try:
                acquire_cb(serial, idx)
                acquire_ok = True
            except Exception:
                log.exception("acquire_cb failed for %s", caller)
                from reticulumpi.rtlsdr import release_device

                try:
                    release_device(serial, caller=caller)
                except Exception:
                    log.debug("SDR device release failed for %s", caller, exc_info=True)
        finally:
            self._condition.acquire()

        if not acquire_ok:
            return

        if not self._running:
            self._condition.release()
            try:
                from reticulumpi.rtlsdr import release_device

                try:
                    release_device(serial, caller=caller)
                except Exception:
                    log.debug("SDR device release failed for %s", caller, exc_info=True)
            finally:
                self._condition.acquire()
            return

        slot = dongle.slots.get(caller)
        if slot is None:
            if dongle.relock_after == caller:
                dongle.relock_after = None
            self._condition.release()
            try:
                from reticulumpi.rtlsdr import release_device

                try:
                    release_device(serial, caller=caller)
                except Exception:
                    log.debug("SDR device release failed for %s", caller, exc_info=True)
            finally:
                self._condition.acquire()
            return

        dongle.device_index = idx
        dongle.generation += 1
        slot.is_active = True
        slot.last_acquired = time.time()
        dongle.current_holder = caller

        if priority == PRIORITY_BACKGROUND:
            dongle.bg_last_rotation = time.time()
            if caller in dongle.bg_order:
                dongle.bg_index = dongle.bg_order.index(caller)

        if dongle.relock_after == caller:
            dongle.locked_by = caller
            dongle.relock_after = None

        self._condition.release()
        try:
            try:
                self._event_bus.publish(
                    events.SDR_DONGLE_GRANTED,
                    {
                        "serial": serial,
                        "caller": caller,
                        "priority": priority,
                    },
                )
            except Exception:
                log.debug("event publish failed", exc_info=True)
        finally:
            self._condition.acquire()

    def _advance_bg_index(self, dongle: DongleState) -> None:
        if dongle.bg_order:
            dongle.bg_index = (dongle.bg_index + 1) % len(dongle.bg_order)

    # ── notification ─────────────────────────────────────────────────

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    # ── status / introspection ───────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            result: dict[str, Any] = {}
            for serial, dongle in self._dongles.items():
                slots_info = {}
                for caller, slot in dongle.slots.items():
                    slots_info[caller] = {
                        "priority": slot.priority,
                        "active": slot.is_active,
                        "continuous": slot.continuous,
                        "window_count": len(slot.windows),
                        "label": slot.label,
                    }
                result[serial] = {
                    "current_holder": dongle.current_holder,
                    "locked_by": dongle.locked_by,
                    "slots": slots_info,
                    "bg_order": list(dongle.bg_order),
                    "bg_slice_seconds": dongle.bg_slice_seconds,
                }
            return result

    def get_schedule(self, serial: str) -> list[dict[str, Any]]:
        with self._lock:
            dongle = self._dongles.get(serial)
            if dongle is None:
                return []
            windows: list[dict[str, Any]] = []
            for caller, slot in dongle.slots.items():
                for w in slot.windows:
                    windows.append(
                        {
                            "caller": caller,
                            "label": w.label or slot.label,
                            "start_ts": w.start_ts,
                            "end_ts": w.end_ts,
                            "priority": slot.priority,
                        }
                    )
            windows.sort(key=lambda w: w["start_ts"])
            return windows

    def get_generation(self, serial: str) -> int:
        """Return the current slot-allocation generation for a dongle.

        Callers can snapshot this value before releasing the lock and
        compare after re-acquiring to detect whether their slot was
        reallocated in the interim.
        """
        with self._lock:
            dongle = self._dongles.get(serial)
            return dongle.generation if dongle is not None else 0

    def dongle_released(self, serial: str, caller: str) -> None:
        with self._condition:
            dongle = self._dongles.get(serial)
            if dongle is None:
                return
            slot = dongle.slots.get(caller)
            if slot is not None:
                slot.is_active = False
            if dongle.current_holder == caller:
                dongle.current_holder = None
                self._advance_bg_index(dongle)
            self._condition.notify_all()
