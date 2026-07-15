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
from reticulumpi.rtlsdr import (
    DeviceBusyError,
    DeviceSelector,
    ResolvedDevice,
    resolve_device_identity,
)

log = logging.getLogger(__name__)

PRIORITY_CRITICAL = 0
PRIORITY_SCHEDULED = 1
PRIORITY_BACKGROUND = 2

_USB_SETTLE_DELAY = 0.5
_SDR_PROBE_RETRY_INTERVAL = 10.0


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
    device_selector: DeviceSelector = "auto"
    canonical_id: str = ""
    label: str = ""
    continuous: bool = False
    windows: list[TimeWindow] = field(default_factory=list)
    is_active: bool = False
    last_acquired: float = 0.0
    last_yielded: float = 0.0
    device_lease: Any | None = None
    release_requested: bool = False
    suspended: bool = False
    registration_id: int = 0
    allocation_generation: int = 0
    identity_preflight_at: float = 0.0
    identity_preflight_signature: tuple[int, str | None, str | None, int, bool] | None = None
    identity_preflight_deferred: bool = False
    acquire_retry_at: float = 0.0
    acquire_retry_signature: tuple[int, int, str] | None = None


@dataclass
class DongleState:
    """Per-dongle scheduler state."""

    serial: str
    canonical_id: str = ""
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
        self._canonical_dongles: dict[str, str] = {}
        self._registrations: dict[tuple[str, str], str] = {}
        self._managed_aliases: dict[str, str] = {}
        self._thread: threading.Thread | None = None
        self._running = False
        self._next_registration_id = 0
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
            canonical_id = f"serial:{serial}"
            state = DongleState(
                serial=serial,
                canonical_id=canonical_id,
                default_signal=d.get("default_signal", ""),
                bg_slice_seconds=float(d.get("background_slice_seconds", 120)),
            )
            self._dongles[serial] = state
            self._canonical_dongles[canonical_id] = serial
            self._managed_aliases[serial] = canonical_id

    def add_dongle(self, serial: str, **kwargs: Any) -> None:
        with self._lock:
            if serial not in self._dongles:
                canonical_id = f"serial:{serial}"
                self._dongles[serial] = DongleState(
                    serial=serial,
                    canonical_id=canonical_id,
                    **kwargs,
                )
                self._canonical_dongles[canonical_id] = serial
                self._managed_aliases[serial] = canonical_id

    def _registration_canonical_id(
        self,
        configured: str,
        selector: DeviceSelector,
    ) -> str:
        """Resolve one registration without claiming the physical device."""

        managed = self._managed_aliases.get(configured)
        if selector == "auto" and managed is not None:
            return managed
        try:
            return resolve_device_identity(configured, selector=selector).canonical_id
        except RuntimeError:
            if selector == "index":
                try:
                    return f"index:{int(configured)}"
                except ValueError:
                    return f"index:{configured}"
            return f"serial:{configured}"

    def _ensure_dongle_locked(self, configured: str, canonical_id: str) -> str:
        storage_key = self._canonical_dongles.get(canonical_id)
        if storage_key is not None:
            return storage_key

        storage_key = configured
        existing = self._dongles.get(storage_key)
        if existing is not None and existing.canonical_id != canonical_id:
            storage_key = canonical_id
            suffix = 2
            while storage_key in self._dongles:
                storage_key = f"{canonical_id}#{suffix}"
                suffix += 1

        self._dongles[storage_key] = DongleState(
            serial=configured,
            canonical_id=canonical_id,
        )
        self._canonical_dongles[canonical_id] = storage_key
        return storage_key

    def _registration_dongle_locked(
        self,
        configured: str,
        caller: str,
    ) -> tuple[str | None, DongleState | None]:
        storage_key = self._registrations.get((configured, caller))
        if storage_key is None and configured in self._dongles:
            storage_key = configured
        return storage_key, self._dongles.get(storage_key) if storage_key is not None else None

    def _prune_empty_provisional_locked(
        self,
        storage_key: str,
        dongle: DongleState,
    ) -> None:
        """Remove only a fully idle, unmanaged state with no registration routes."""

        if (
            dongle.slots
            or dongle.bg_order
            or dongle.current_holder is not None
            or dongle.locked_by is not None
            or dongle.relock_after is not None
            or dongle.canonical_id in self._managed_aliases.values()
            or any(key == storage_key for key in self._registrations.values())
        ):
            return
        if self._dongles.get(storage_key) is dongle:
            self._dongles.pop(storage_key, None)
        if self._canonical_dongles.get(dongle.canonical_id) == storage_key:
            self._canonical_dongles.pop(dongle.canonical_id, None)

    def _reconcile_idle_slot_locked(
        self,
        source: DongleState,
        slot: SignalSlot,
        caller: str,
        resolved: ResolvedDevice,
    ) -> bool:
        """Move one idle slot to its authoritative physical-device state."""

        registration = (slot.serial, caller)
        source_key = self._registrations.get(registration)
        if (
            source_key is None
            or self._dongles.get(source_key) is not source
            or source.slots.get(caller) is not slot
            or slot.is_active
            or slot.device_lease is not None
            or source.current_holder == caller
            or source.locked_by == caller
        ):
            return False

        canonical_id = resolved.canonical_id
        if source.canonical_id == canonical_id:
            slot.canonical_id = canonical_id
            slot.allocation_generation = 0
            return False

        target_key = self._canonical_dongles.get(canonical_id)
        if target_key is None:
            target_key = self._ensure_dongle_locked(slot.serial, canonical_id)
        target = self._dongles[target_key]
        if target is source or caller in target.slots:
            return False

        source.slots.pop(caller)
        if caller in source.bg_order:
            source.bg_order.remove(caller)
        if source.relock_after == caller:
            # A relock belongs to the old physical device.  Once an idle
            # index-selected slot resolves elsewhere it must not be carried
            # to the new state or keep the old state pinned to that caller.
            source.relock_after = None

        slot.canonical_id = canonical_id
        slot.allocation_generation = 0
        target.slots[caller] = slot
        if slot.priority == PRIORITY_BACKGROUND and slot.continuous:
            if caller not in target.bg_order:
                target.bg_order.append(caller)
        self._registrations[registration] = target_key

        # Each state advances independently; holder, lock, lease, index, and
        # generation values are never copied or merged across states.
        source.generation += 1
        target.generation += 1
        self._prune_empty_provisional_locked(source_key, source)
        return True

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
        device_selector: DeviceSelector = "auto",
    ) -> None:
        if priority == PRIORITY_CRITICAL and not continuous:
            raise ValueError("P0/critical slots must be continuous")
        if device_selector not in ("auto", "serial", "index"):
            raise ValueError(f"Unknown RTL-SDR device selector: {device_selector!r}")

        canonical_id = self._registration_canonical_id(serial, device_selector)

        with self._condition:
            storage_key = self._ensure_dongle_locked(serial, canonical_id)
            previous_key = self._registrations.get((serial, caller))
            if previous_key is not None and previous_key != storage_key:
                previous = self._dongles.get(previous_key)
                if previous is not None:
                    previous_slot = previous.slots.get(caller)
                    if previous_slot is not None and previous_slot.is_active:
                        self._do_yield(previous, caller, "", "", None)
                    previous.slots.pop(caller, None)
                    if caller in previous.bg_order:
                        previous.bg_order.remove(caller)
                    if previous.locked_by == caller:
                        previous.locked_by = None
                    previous.generation += 1

            dongle = self._dongles[storage_key]
            self._registrations[(serial, caller)] = storage_key

            dongle.generation += 1
            self._next_registration_id += 1

            slot = SignalSlot(
                caller=caller,
                serial=serial,
                device_selector=device_selector,
                canonical_id=canonical_id,
                priority=priority,
                acquire_cb=acquire_cb,
                yield_cb=yield_cb,
                label=label,
                continuous=continuous,
                windows=list(windows) if windows else [],
                registration_id=self._next_registration_id,
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
            _storage_key, dongle = self._registration_dongle_locked(serial, caller)
            if dongle is None:
                return

            slot = dongle.slots.get(caller)
            if slot and slot.is_active:
                self._do_yield(dongle, caller, "", "", None)
            if slot is not None:
                dongle.generation += 1
            dongle.slots.pop(caller, None)

            if caller in dongle.bg_order:
                dongle.bg_order.remove(caller)

            if dongle.locked_by == caller:
                dongle.locked_by = None
            self._registrations.pop((serial, caller), None)

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
            _storage_key, dongle = self._registration_dongle_locked(serial, caller)
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
            _storage_key, dongle = self._registration_dongle_locked(serial, caller)
            if dongle is None:
                return
            slot = dongle.slots.get(caller)
            if slot is not None:
                slot.windows.clear()
            self._condition.notify_all()

    # ── lock support ─────────────────────────────────────────────────

    def lock(self, serial: str, caller: str) -> bool:
        with self._condition:
            _storage_key, dongle = self._registration_dongle_locked(serial, caller)
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
            _storage_key, dongle = self._registration_dongle_locked(serial, caller)
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
        wall_now = time.time()
        monotonic_now = time.monotonic()

        self._expire_windows(dongle, wall_now)

        winner = self._pick_winner(dongle, wall_now, monotonic_now)
        if winner is None and dongle.current_holder is None:
            return
        if winner == dongle.current_holder:
            return

        if winner is None and dongle.current_holder is not None:
            current_slot = dongle.slots.get(dongle.current_holder)
            if current_slot and current_slot.priority == PRIORITY_SCHEDULED:
                self._do_yield(dongle, dongle.current_holder, "", "", None)
                bg = self._pick_background(dongle, monotonic_now)
                if bg:
                    self._do_acquire(dongle, bg)
            return

        current = dongle.current_holder
        if current is not None:
            can_preempt = self._can_preempt(dongle, current, winner)
            if not can_preempt:
                winner_slot = dongle.slots.get(winner)
                if winner_slot is not None and winner_slot.device_selector != "serial":
                    self._preflight_winner_identity_locked(
                        dongle,
                        winner,
                        can_preempt=False,
                    )
                return
            if self._preflight_winner_identity_locked(
                dongle,
                winner,
                can_preempt=True,
            ):
                return
            winner_slot = dongle.slots.get(winner)
            winner_label = winner_slot.label if winner_slot else winner
            winner_end = self._window_end(dongle, winner, wall_now)
            self._do_yield(dongle, current, winner, winner_label, winner_end)

        self._do_acquire(dongle, winner)

    def _preflight_winner_identity_locked(
        self,
        dongle: DongleState,
        caller: str,
        *,
        can_preempt: bool,
    ) -> bool:
        """Resolve a proposed handoff before touching the current holder.

        Returns ``True`` when this arbitration pass must stop.  No plugin
        callback is invoked here, and a successful preflight lease is always
        released before normal arbitration may continue.
        """

        slot = dongle.slots.get(caller)
        if (
            slot is None
            or slot.suspended
            or slot.is_active
            or slot.device_lease is not None
            or dongle.current_holder == caller
        ):
            return True
        registration = (slot.serial, caller)
        storage_key = self._registrations.get(registration)
        if storage_key is None or self._dongles.get(storage_key) is not dongle:
            return True

        generation = dongle.generation
        current_holder = dongle.current_holder
        locked_by = dongle.locked_by
        preflight_signature = (
            generation,
            current_holder,
            locked_by,
            slot.registration_id,
            can_preempt,
        )
        now = time.monotonic()
        if (
            slot.identity_preflight_signature == preflight_signature
            and now - slot.identity_preflight_at < _SDR_PROBE_RETRY_INTERVAL
            and (not can_preempt or slot.identity_preflight_deferred)
        ):
            return True

        lease = None
        busy_error: DeviceBusyError | None = None
        claim_error: RuntimeError | None = None

        self._condition.release()
        try:
            from reticulumpi.rtlsdr import claim_device

            try:
                lease = claim_device(
                    slot.serial,
                    caller=caller,
                    selector=slot.device_selector,
                )
            except DeviceBusyError as exc:
                busy_error = exc
            except RuntimeError as exc:
                claim_error = exc
        finally:
            self._condition.acquire()

        still_current = (
            dongle.generation == generation
            and dongle.slots.get(caller) is slot
            and self._registrations.get(registration) == storage_key
            and self._dongles.get(storage_key) is dongle
            and dongle.current_holder == current_holder
            and dongle.locked_by == locked_by
        )
        if still_current:
            still_current = (
                self._pick_winner(
                    dongle,
                    time.time(),
                    time.monotonic(),
                    mutate_background=False,
                )
                == caller
            )
        if not still_current:
            if lease is not None:
                try:
                    lease.release()
                except Exception:
                    log.debug("SDR preflight lease release failed for %s", caller, exc_info=True)
            return True

        slot.identity_preflight_at = time.monotonic()
        slot.identity_preflight_signature = preflight_signature
        slot.identity_preflight_deferred = False

        resolved: ResolvedDevice | None = None
        if busy_error is not None:
            resolved = busy_error.resolved
        elif lease is not None and isinstance(getattr(lease, "canonical_id", None), str):
            resolved = ResolvedDevice(lease.index, lease.canonical_id)

        if resolved is None:
            slot.identity_preflight_deferred = True
            if lease is not None:
                try:
                    lease.release()
                except Exception:
                    log.debug("SDR preflight lease release failed for %s", caller, exc_info=True)
            if claim_error is not None:
                slot.identity_preflight_deferred = True
                log.error(
                    "Deferred handoff to %s on %s: %s",
                    caller,
                    slot.serial,
                    claim_error,
                )
            return True

        if resolved.canonical_id != slot.canonical_id:
            reconciled = self._reconcile_idle_slot_locked(dongle, slot, caller, resolved)
            slot.identity_preflight_deferred = not reconciled
            if lease is not None:
                try:
                    lease.release()
                except Exception:
                    log.debug("SDR preflight lease release failed for %s", caller, exc_info=True)
            self._condition.notify_all()
            log.info(
                "Deferred handoff to %s after canonical identity %s%s",
                caller,
                resolved.canonical_id,
                " was reconciled" if reconciled else " could not be reconciled safely",
            )
            return True

        if lease is not None:
            try:
                lease.release()
            except Exception:
                slot.identity_preflight_deferred = True
                log.debug("SDR preflight lease release failed for %s", caller, exc_info=True)
                return True
        return False

    def _pick_winner(
        self,
        dongle: DongleState,
        wall_now: float,
        monotonic_now: float,
        *,
        mutate_background: bool = True,
    ) -> str | None:
        best: str | None = None
        best_priority = 999

        for caller, slot in dongle.slots.items():
            if slot.suspended:
                continue
            if slot.priority == PRIORITY_CRITICAL and slot.continuous:
                if slot.priority < best_priority:
                    best = caller
                    best_priority = slot.priority

            elif slot.priority == PRIORITY_SCHEDULED:
                for w in slot.windows:
                    if w.start_ts <= wall_now <= w.end_ts:
                        if slot.priority < best_priority:
                            best = caller
                            best_priority = slot.priority
                        break

        if best is not None:
            return best

        if mutate_background:
            return self._pick_background(dongle, monotonic_now)
        return self._peek_background(dongle, monotonic_now)

    def _peek_background(self, dongle: DongleState, now: float) -> str | None:
        """Read the already-selected background position without rotating it."""

        bg = dongle.bg_order
        if not bg:
            return None
        if dongle.current_holder in bg:
            elapsed = now - dongle.bg_last_rotation
            if elapsed < dongle.bg_slice_seconds:
                return dongle.current_holder

        index = dongle.bg_index
        for _ in range(len(bg)):
            candidate = bg[index % len(bg)]
            slot = dongle.slots.get(candidate)
            if slot is not None and not slot.suspended:
                return candidate
            index += 1
        return None

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
            if slot is not None and not slot.suspended:
                return candidate
            if slot is not None:
                dongle.bg_index += 1
                continue
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
        slot.last_yielded = time.monotonic()
        slot.allocation_generation = 0
        dongle.current_holder = None
        dongle.generation += 1

        was_locked = dongle.locked_by == caller
        if was_locked and preempted_by:
            dongle.locked_by = None
            dongle.relock_after = caller
        elif dongle.relock_after == caller:
            dongle.relock_after = None

        yield_cb = slot.yield_cb
        serial = slot.serial
        lease = slot.device_lease
        slot.device_lease = None

        self._condition.release()
        try:
            try:
                yield_cb(preempted_by, preempted_by_label, preempted_until_ts)
            except Exception:
                log.exception("yield_cb failed for %s", caller)

            try:
                if lease is not None:
                    lease.release()
                else:
                    from reticulumpi.rtlsdr import release_device

                    release_device(
                        serial,
                        caller=caller,
                        selector=slot.device_selector,
                    )
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
        if slot is None or slot.suspended:
            return

        retry_signature = (
            dongle.generation,
            slot.registration_id,
            slot.canonical_id,
        )
        if (
            slot.acquire_retry_signature == retry_signature
            and time.monotonic() - slot.acquire_retry_at < _SDR_PROBE_RETRY_INTERVAL
        ):
            return
        slot.acquire_retry_signature = None

        acquire_cb = slot.acquire_cb
        priority = slot.priority
        serial = slot.serial
        slot.release_requested = False
        dongle.generation += 1
        acquire_generation = dongle.generation
        acquire_slot = slot
        acquire_slot.allocation_generation = acquire_generation

        idx = None
        lease = None
        busy_error: DeviceBusyError | None = None
        claim_error: RuntimeError | None = None
        acquire_ok = False

        self._condition.release()
        try:
            time.sleep(_USB_SETTLE_DELAY)

            from reticulumpi.rtlsdr import claim_device

            try:
                lease = claim_device(
                    serial,
                    caller=caller,
                    selector=slot.device_selector,
                )
                idx = lease.index
            except DeviceBusyError as exc:
                busy_error = exc
            except RuntimeError as exc:
                claim_error = exc
        finally:
            self._condition.acquire()

        registration_key = self._registrations.get((serial, caller))
        still_current = (
            registration_key is not None
            and self._dongles.get(registration_key) is dongle
            and dongle.slots.get(caller) is acquire_slot
            and acquire_slot.allocation_generation == acquire_generation
        )
        if not still_current:
            if acquire_slot.allocation_generation == acquire_generation:
                acquire_slot.allocation_generation = 0
            if lease is not None:
                self._condition.release()
                try:
                    lease.release()
                except Exception:
                    log.debug("SDR device release failed for %s", caller, exc_info=True)
                finally:
                    self._condition.acquire()
            return

        resolved: ResolvedDevice | None = None
        if busy_error is not None:
            resolved = busy_error.resolved
        elif lease is not None and isinstance(getattr(lease, "canonical_id", None), str):
            resolved = ResolvedDevice(lease.index, lease.canonical_id)

        if resolved is not None and resolved.canonical_id != acquire_slot.canonical_id:
            reconciled = self._reconcile_idle_slot_locked(
                dongle,
                acquire_slot,
                caller,
                resolved,
            )
            if reconciled:
                acquire_slot.acquire_retry_signature = None
            else:
                self._defer_acquire_retry(dongle, acquire_slot)
            if acquire_slot.allocation_generation == acquire_generation:
                acquire_slot.allocation_generation = 0
            if lease is not None:
                try:
                    lease.release()
                except Exception:
                    log.debug("SDR device release failed for %s", caller, exc_info=True)
            self._condition.notify_all()
            log.info(
                "Deferred %s on %s after canonical identity %s%s",
                caller,
                serial,
                resolved.canonical_id,
                " was reconciled" if reconciled else " could not be reconciled safely",
            )
            return

        if busy_error is not None:
            acquire_slot.allocation_generation = 0
            self._defer_acquire_retry(dongle, acquire_slot)
            log.error("Failed to claim dongle %s for %s: %s", serial, caller, busy_error)
            return
        if claim_error is not None:
            acquire_slot.allocation_generation = 0
            self._defer_acquire_retry(dongle, acquire_slot)
            log.error("Failed to claim dongle %s for %s: %s", serial, caller, claim_error)
            return

        log.info("Granting dongle %s to %s (index %s)", serial, caller, idx)

        self._condition.release()
        try:
            try:
                acquire_cb(serial, idx)
                acquire_ok = True
            except Exception:
                log.exception("acquire_cb failed for %s", caller)
                try:
                    lease.release()
                except Exception:
                    log.debug("SDR device release failed for %s", caller, exc_info=True)
        finally:
            self._condition.acquire()

        if not acquire_ok:
            if acquire_slot.allocation_generation == acquire_generation:
                acquire_slot.allocation_generation = 0
            self._defer_acquire_retry(dongle, acquire_slot)
            return

        if not self._running:
            self._discard_successful_acquisition(acquire_slot, caller, lease)
            return

        slot = dongle.slots.get(caller)
        if slot is None:
            if dongle.relock_after == caller:
                dongle.relock_after = None
            self._discard_successful_acquisition(acquire_slot, caller, lease)
            return

        if slot is not acquire_slot or dongle.generation != acquire_generation:
            self._discard_successful_acquisition(acquire_slot, caller, lease)
            return

        if slot.release_requested:
            slot.release_requested = False
            self._discard_successful_acquisition(acquire_slot, caller, lease)
            return

        dongle.device_index = idx
        slot.is_active = True
        slot.device_lease = lease
        slot.last_acquired = time.monotonic()
        dongle.current_holder = caller

        if priority == PRIORITY_BACKGROUND:
            dongle.bg_last_rotation = time.monotonic()
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

    @staticmethod
    def _defer_acquire_retry(dongle: DongleState, slot: SignalSlot) -> None:
        """Rate-limit repeated fresh inventory after a failed exact claim."""

        slot.acquire_retry_at = time.monotonic()
        slot.acquire_retry_signature = (
            dongle.generation,
            slot.registration_id,
            slot.canonical_id,
        )

    def _discard_successful_acquisition(
        self,
        slot: SignalSlot,
        caller: str,
        lease: Any | None,
    ) -> None:
        """Undo a callback that completed after its scheduler grant went stale.

        ``acquire_cb`` may have launched a complete decoder pipeline.  Its
        paired cleanup callback therefore has to run before the physical
        lease is returned, otherwise another owner can open the same dongle
        while the stale pipeline is still shutting down.

        The scheduler condition is held on entry and restored on return.
        """

        slot.allocation_generation = 0
        self._condition.release()
        try:
            try:
                slot.yield_cb("", "", None)
            except Exception:
                log.exception("yield_cb failed while discarding stale acquire for %s", caller)
            try:
                if lease is not None:
                    lease.release()
            except Exception:
                log.debug("SDR device release failed for %s", caller, exc_info=True)
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
                        "suspended": slot.suspended,
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
            storage_keys = {
                storage_key
                for (configured, _caller), storage_key in self._registrations.items()
                if configured == serial
            }
            if not storage_keys and serial in self._dongles:
                storage_keys.add(serial)
            if not storage_keys:
                return []
            windows: list[dict[str, Any]] = []
            for storage_key in storage_keys:
                dongle = self._dongles.get(storage_key)
                if dongle is None:
                    continue
                for caller, slot in dongle.slots.items():
                    if slot.serial != serial:
                        continue
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
            if dongle is None:
                storage_keys = {
                    storage_key
                    for (configured, _caller), storage_key in self._registrations.items()
                    if configured == serial
                }
                if len(storage_keys) == 1:
                    dongle = self._dongles.get(next(iter(storage_keys)))
            return dongle.generation if dongle is not None else 0

    def get_allocation_generation(self, serial: str, caller: str) -> int:
        """Return the current lease generation for one registered slot."""

        with self._lock:
            _storage_key, dongle = self._registration_dongle_locked(serial, caller)
            slot = dongle.slots.get(caller) if dongle is not None else None
            return slot.allocation_generation if slot is not None else 0

    def get_metrics(self) -> dict[str, int]:
        """Return aggregate lease state without serials or caller names."""

        with self._lock:
            slots = [slot for dongle in self._dongles.values() for slot in dongle.slots.values()]
            return {
                "dongles": len(self._dongles),
                "active_leases": sum(slot.device_lease is not None for slot in slots),
                "active_slots": sum(slot.is_active for slot in slots),
                "suspended_slots": sum(slot.suspended for slot in slots),
            }

    def dongle_released(
        self,
        serial: str,
        caller: str,
        *,
        generation: int | None = None,
    ) -> None:
        self._release_slot(serial, caller, suspend=False, generation=generation)

    def suspend(
        self,
        serial: str,
        caller: str,
        *,
        generation: int | None = None,
    ) -> int | None:
        """Release and suppress a failed slot until it is registered again."""

        return self._release_slot(serial, caller, suspend=True, generation=generation)

    def resume(
        self,
        serial: str,
        caller: str,
        *,
        registration_id: int,
    ) -> bool:
        """Make a retrying slot eligible after its process backoff expires.

        The registration token prevents a delayed retry worker from reviving a
        slot that was unregistered and recreated while it slept.  Resuming only
        restores eligibility; normal arbitration must grant ownership and run
        ``acquire_cb`` before a decoder can restart.
        """

        with self._condition:
            _storage_key, dongle = self._registration_dongle_locked(serial, caller)
            if dongle is None or not self._running:
                return False
            slot = dongle.slots.get(caller)
            if slot is None or slot.registration_id != registration_id or not slot.suspended:
                return False
            slot.suspended = False
            slot.release_requested = False
            dongle.generation += 1
            self._condition.notify_all()
            return True

    def _release_slot(
        self,
        serial: str,
        caller: str,
        *,
        suspend: bool,
        generation: int | None,
    ) -> int | None:
        lease = None
        registration_id = None
        with self._condition:
            _storage_key, dongle = self._registration_dongle_locked(serial, caller)
            if dongle is None:
                return None
            slot = dongle.slots.get(caller)
            if generation is not None and (
                slot is None or generation != slot.allocation_generation
            ):
                log.debug(
                    "Ignoring stale release for %s on %s (generation %d != %d)",
                    caller,
                    serial,
                    generation,
                    slot.allocation_generation if slot is not None else 0,
                )
                return None
            if slot is not None:
                slot.is_active = False
                slot.suspended = slot.suspended or suspend
                slot.release_requested = True
                lease = slot.device_lease
                slot.device_lease = None
                slot.allocation_generation = 0
                registration_id = slot.registration_id
                dongle.generation += 1
            if dongle.current_holder == caller:
                dongle.current_holder = None
                self._advance_bg_index(dongle)
            if dongle.locked_by == caller:
                dongle.locked_by = None
            if dongle.relock_after == caller:
                dongle.relock_after = None
            self._condition.notify_all()
        if lease is not None:
            try:
                lease.release()
            except Exception:
                log.debug("SDR device release failed for %s", caller, exc_info=True)
        return registration_id
