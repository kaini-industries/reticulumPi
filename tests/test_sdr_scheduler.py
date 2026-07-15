"""Tests for the SDR dongle time-sharing scheduler."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi.sdr_scheduler import (
    PRIORITY_BACKGROUND,
    PRIORITY_CRITICAL,
    PRIORITY_SCHEDULED,
    SdrScheduler,
    TimeWindow,
)
from reticulumpi.rtlsdr import DeviceBusyError, ResolvedDevice

SERIAL = "00000001"
DEVICES = [(0, "00000001"), (1, "07143901")]


def _make_scheduler(bus=None, **config_kw) -> SdrScheduler:
    config = {"managed_dongles": [{"serial": SERIAL}], **config_kw}
    return SdrScheduler(bus or MagicMock(), config)


def _cb_pair() -> tuple[MagicMock, MagicMock]:
    """Return (acquire_cb, yield_cb) mocks."""
    return MagicMock(), MagicMock(return_value=True)


def _patch_hw():
    """Stub out rtlsdr hardware calls."""
    lease = MagicMock()
    lease.index = 0
    lease.canonical_id = f"serial:{SERIAL}"
    return patch.multiple(
        "reticulumpi.rtlsdr",
        claim_device=MagicMock(return_value=lease),
        release_device=MagicMock(),
    )


@pytest.fixture
def sched():
    """Scheduler with one managed dongle; hardware access patched out."""
    with _patch_hw(), patch("reticulumpi.sdr_scheduler._USB_SETTLE_DELAY", 0):
        s = _make_scheduler()
        s._running = True
        yield s
        s.stop()


class TestRegistration:
    def test_add_dongle_records_canonical_alias_once(self):
        scheduler = SdrScheduler(MagicMock())

        scheduler.add_dongle("NEW", default_signal="ais")
        scheduler.add_dongle("NEW", default_signal="ignored")

        assert scheduler._dongles["NEW"].canonical_id == "serial:NEW"
        assert scheduler._dongles["NEW"].default_signal == "ais"
        assert scheduler._canonical_dongles["serial:NEW"] == "NEW"
        assert scheduler._managed_aliases["NEW"] == "serial:NEW"

    def test_unresolved_non_numeric_index_keeps_explicit_index_identity(self):
        scheduler = SdrScheduler(MagicMock())

        with patch(
            "reticulumpi.sdr_scheduler.resolve_device_identity",
            side_effect=RuntimeError("inventory unavailable"),
        ):
            canonical_id = scheduler._registration_canonical_id(
                "not-a-number",
                "index",
            )

        assert canonical_id == "index:not-a-number"

    def test_register_creates_slot(self, sched):
        acq, yld = _cb_pair()
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, acq, yld, label="AIS", continuous=True)
        slot = sched.get_status()[SERIAL]["slots"]["ais"]
        assert slot["priority"] == PRIORITY_BACKGROUND
        assert slot["label"] == "AIS"

    def test_register_unknown_serial_creates_dongle(self, sched):
        sched.register("NEW", "fm", PRIORITY_BACKGROUND, *_cb_pair(), continuous=True)
        assert "NEW" in sched.get_status()

    def test_explicit_device_selector_is_forwarded_to_claim(self):
        scheduler = _make_scheduler()
        scheduler._running = True
        lease = MagicMock(index=1)
        scheduler.register(
            "00000001",
            "spectrum",
            PRIORITY_BACKGROUND,
            *_cb_pair(),
            continuous=True,
            device_selector="index",
        )
        with (
            patch("reticulumpi.rtlsdr.claim_device", return_value=lease) as claim,
            patch("reticulumpi.sdr_scheduler._USB_SETTLE_DELAY", 0),
            scheduler._condition,
        ):
            storage_key = scheduler._registrations[("00000001", "spectrum")]
            scheduler._do_acquire(scheduler._dongles[storage_key], "spectrum")

        claim.assert_called_once_with(
            "00000001",
            caller="spectrum",
            selector="index",
        )

    def test_serial_and_index_aliases_share_one_arbitration_state(self):
        scheduler = SdrScheduler(MagicMock())
        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=DEVICES):
            scheduler.register(
                "07143901",
                "by_serial",
                PRIORITY_BACKGROUND,
                *_cb_pair(),
                continuous=True,
                device_selector="serial",
            )
            scheduler.register(
                "00000001",
                "by_index",
                PRIORITY_BACKGROUND,
                *_cb_pair(),
                continuous=True,
                device_selector="index",
            )

        serial_key = scheduler._registrations[("07143901", "by_serial")]
        index_key = scheduler._registrations[("00000001", "by_index")]
        assert serial_key == index_key
        assert set(scheduler._dongles[serial_key].slots) == {"by_serial", "by_index"}

    def test_identical_text_with_distinct_selectors_stays_separate(self):
        scheduler = SdrScheduler(MagicMock())
        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=DEVICES):
            scheduler.register(
                "00000001",
                "by_serial",
                PRIORITY_BACKGROUND,
                *_cb_pair(),
                continuous=True,
                device_selector="serial",
            )
            scheduler.register(
                "00000001",
                "by_index",
                PRIORITY_BACKGROUND,
                *_cb_pair(),
                continuous=True,
                device_selector="index",
            )

        serial_key = scheduler._registrations[("00000001", "by_serial")]
        index_key = scheduler._registrations[("00000001", "by_index")]
        assert serial_key != index_key
        assert scheduler._dongles[serial_key].canonical_id == "serial:00000001"
        assert scheduler._dongles[index_key].canonical_id == "serial:07143901"

        scheduler.unregister("00000001", "by_index")
        assert "by_serial" in scheduler._dongles[serial_key].slots
        assert "by_index" not in scheduler._dongles[index_key].slots

    def test_reregister_after_reenumeration_cleans_up_previous_state(self):
        scheduler = SdrScheduler(MagicMock())
        scheduler._running = True
        old_acquire, old_yield = _cb_pair()

        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=DEVICES):
            scheduler.register(
                "1",
                "decoder",
                PRIORITY_BACKGROUND,
                old_acquire,
                old_yield,
                continuous=True,
                device_selector="index",
            )

        old_key = scheduler._registrations[("1", "decoder")]
        old_state = scheduler._dongles[old_key]
        old_lease = MagicMock(index=1, canonical_id="serial:07143901")
        old_state.current_holder = "decoder"
        old_state.locked_by = "decoder"
        old_state.slots["decoder"].is_active = True
        old_state.slots["decoder"].device_lease = old_lease

        with patch(
            "reticulumpi.rtlsdr.enumerate_devices",
            return_value=[(1, "99999999")],
        ):
            scheduler.register(
                "1",
                "decoder",
                PRIORITY_BACKGROUND,
                *_cb_pair(),
                continuous=True,
                device_selector="index",
            )

        new_key = scheduler._registrations[("1", "decoder")]
        assert new_key != old_key
        assert scheduler._dongles[new_key].canonical_id == "serial:99999999"
        assert "decoder" not in old_state.slots
        assert "decoder" not in old_state.bg_order
        assert old_state.current_holder is None
        assert old_state.locked_by is None
        old_yield.assert_called_once_with("", "", None)
        old_lease.release.assert_called_once_with()

    def test_p0_preempts_p2_across_serial_and_index_aliases(self):
        scheduler = SdrScheduler(MagicMock())
        scheduler._running = True
        _acquire_bg, yield_bg = _cb_pair()
        acquire_p0, yield_p0 = _cb_pair()
        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=DEVICES):
            scheduler.register(
                "00000001",
                "background",
                PRIORITY_BACKGROUND,
                _acquire_bg,
                yield_bg,
                continuous=True,
                device_selector="index",
            )
            scheduler.register(
                "07143901",
                "critical",
                PRIORITY_CRITICAL,
                acquire_p0,
                yield_p0,
                continuous=True,
                device_selector="serial",
            )

        storage_key = scheduler._registrations[("00000001", "background")]
        dongle = scheduler._dongles[storage_key]
        background_slot = dongle.slots["background"]
        background_slot.is_active = True
        background_lease = MagicMock(index=1, canonical_id="serial:07143901")
        background_slot.device_lease = background_lease
        dongle.current_holder = "background"
        same_device_busy = DeviceBusyError(
            "07143901",
            "background",
            ResolvedDevice(1, "serial:07143901"),
        )
        critical_lease = MagicMock(index=1, canonical_id="serial:07143901")
        with (
            patch(
                "reticulumpi.rtlsdr.claim_device",
                side_effect=[same_device_busy, critical_lease],
            ),
            patch("reticulumpi.sdr_scheduler._USB_SETTLE_DELAY", 0),
            scheduler._condition,
        ):
            scheduler._evaluate(storage_key)

        yield_bg.assert_called_once_with("critical", "", None)
        background_lease.release.assert_called_once_with()
        acquire_p0.assert_called_once_with("07143901", 1)
        assert dongle.current_holder == "critical"


class TestCanonicalReconciliation:
    def test_absent_index_reconciles_before_serial_p0_preemption(self):
        scheduler = SdrScheduler(MagicMock())
        scheduler._running = True
        acquire_index, yield_index = _cb_pair()
        acquire_serial_p0, yield_serial_p0 = _cb_pair()

        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=[]):
            scheduler.register(
                "1",
                "index_background",
                PRIORITY_BACKGROUND,
                acquire_index,
                yield_index,
                continuous=True,
                device_selector="index",
            )
        provisional_key = scheduler._registrations[("1", "index_background")]

        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=DEVICES):
            scheduler.register(
                "07143901",
                "serial_critical",
                PRIORITY_CRITICAL,
                acquire_serial_p0,
                yield_serial_p0,
                continuous=True,
                device_selector="serial",
            )
        canonical_key = scheduler._registrations[("07143901", "serial_critical")]
        assert provisional_key != canonical_key

        reconciled_lease = MagicMock(index=1, canonical_id="serial:07143901")
        with (
            patch("reticulumpi.rtlsdr.claim_device", return_value=reconciled_lease),
            patch("reticulumpi.sdr_scheduler._USB_SETTLE_DELAY", 0),
            scheduler._condition,
        ):
            scheduler._do_acquire(
                scheduler._dongles[provisional_key],
                "index_background",
            )

        acquire_index.assert_not_called()
        yield_index.assert_not_called()
        reconciled_lease.release.assert_called_once_with()
        assert scheduler._registrations[("1", "index_background")] == canonical_key
        assert provisional_key not in scheduler._dongles

        critical_lease = MagicMock(index=1, canonical_id="serial:07143901")
        with (
            patch("reticulumpi.rtlsdr.claim_device", return_value=critical_lease),
            patch("reticulumpi.sdr_scheduler._USB_SETTLE_DELAY", 0),
            scheduler._condition,
        ):
            scheduler._evaluate(canonical_key)

        acquire_serial_p0.assert_called_once_with("07143901", 1)
        acquire_index.assert_not_called()
        assert scheduler._dongles[canonical_key].current_holder == "serial_critical"
        scheduler.stop()

    def test_busy_provisional_p0_routes_then_preempts_active_serial_p2(self):
        scheduler = SdrScheduler(MagicMock())
        scheduler._running = True
        acquire_serial, yield_serial = _cb_pair()
        acquire_index_p0, yield_index_p0 = _cb_pair()

        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=DEVICES):
            scheduler.register(
                "07143901",
                "serial_background",
                PRIORITY_BACKGROUND,
                acquire_serial,
                yield_serial,
                continuous=True,
                device_selector="serial",
            )
        canonical_key = scheduler._registrations[("07143901", "serial_background")]
        canonical = scheduler._dongles[canonical_key]
        serial_lease = MagicMock(index=1, canonical_id="serial:07143901")
        canonical.current_holder = "serial_background"
        canonical.slots["serial_background"].is_active = True
        canonical.slots["serial_background"].device_lease = serial_lease

        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=[]):
            scheduler.register(
                "1",
                "index_critical",
                PRIORITY_CRITICAL,
                acquire_index_p0,
                yield_index_p0,
                continuous=True,
                device_selector="index",
            )
        provisional_key = scheduler._registrations[("1", "index_critical")]
        busy = DeviceBusyError(
            "1",
            "serial_background",
            ResolvedDevice(1, "serial:07143901"),
        )
        with (
            patch("reticulumpi.rtlsdr.claim_device", side_effect=busy),
            patch("reticulumpi.sdr_scheduler._USB_SETTLE_DELAY", 0),
            scheduler._condition,
        ):
            scheduler._do_acquire(
                scheduler._dongles[provisional_key],
                "index_critical",
            )

        acquire_index_p0.assert_not_called()
        yield_index_p0.assert_not_called()
        assert scheduler._registrations[("1", "index_critical")] == canonical_key
        assert provisional_key not in scheduler._dongles
        assert canonical.current_holder == "serial_background"
        assert canonical.slots["serial_background"].device_lease is serial_lease
        serial_lease.release.assert_not_called()

        critical_lease = MagicMock(index=1, canonical_id="serial:07143901")
        with (
            patch("reticulumpi.rtlsdr.claim_device", return_value=critical_lease),
            patch("reticulumpi.sdr_scheduler._USB_SETTLE_DELAY", 0),
            scheduler._condition,
        ):
            scheduler._evaluate(canonical_key)

        yield_serial.assert_called_once_with("index_critical", "", None)
        serial_lease.release.assert_called_once_with()
        acquire_index_p0.assert_called_once_with("1", 1)
        assert canonical.current_holder == "index_critical"
        scheduler.stop()

    def test_index_reenumeration_moves_only_idle_slot_and_lifecycle_route(self):
        scheduler = SdrScheduler(MagicMock())
        scheduler._running = True
        acquire_serial, yield_serial = _cb_pair()
        acquire_index, yield_index = _cb_pair()

        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=DEVICES):
            scheduler.register(
                "07143901",
                "serial_background",
                PRIORITY_BACKGROUND,
                acquire_serial,
                yield_serial,
                continuous=True,
                device_selector="serial",
            )
            scheduler.register(
                "1",
                "index_critical",
                PRIORITY_CRITICAL,
                acquire_index,
                yield_index,
                continuous=True,
                device_selector="index",
            )

        source_key = scheduler._registrations[("1", "index_critical")]
        source = scheduler._dongles[source_key]
        serial_lease = MagicMock(index=1, canonical_id="serial:07143901")
        source.current_holder = "serial_background"
        source.device_index = 1
        source.slots["serial_background"].is_active = True
        source.slots["serial_background"].device_lease = serial_lease
        source.slots["serial_background"].allocation_generation = 41

        changed_lease = MagicMock(index=1, canonical_id="serial:99999999")
        with (
            patch("reticulumpi.rtlsdr.claim_device", return_value=changed_lease),
            patch("reticulumpi.sdr_scheduler._USB_SETTLE_DELAY", 0),
            scheduler._condition,
        ):
            scheduler._evaluate(source_key)

        acquire_serial.assert_not_called()
        yield_serial.assert_not_called()
        acquire_index.assert_not_called()
        yield_index.assert_not_called()
        changed_lease.release.assert_called_once_with()
        target_key = scheduler._registrations[("1", "index_critical")]
        target = scheduler._dongles[target_key]
        assert target is not source
        assert target.canonical_id == "serial:99999999"
        assert target.current_holder is None
        assert target.locked_by is None
        assert target.relock_after is None
        assert target.device_index is None
        assert target.slots["index_critical"].device_lease is None
        assert target.slots["index_critical"].allocation_generation == 0

        assert source.current_holder == "serial_background"
        assert source.locked_by is None
        assert source.device_index == 1
        assert source.slots["serial_background"].device_lease is serial_lease
        assert source.slots["serial_background"].allocation_generation == 41
        assert "index_critical" not in source.slots

        token = scheduler.suspend("1", "index_critical")
        assert token is not None
        assert target.slots["index_critical"].suspended is True
        assert scheduler.get_allocation_generation("1", "index_critical") == 0
        assert scheduler.resume("1", "index_critical", registration_id=token) is True
        scheduler.unregister("1", "index_critical")
        assert "index_critical" not in target.slots
        assert "serial_background" in source.slots
        assert source.current_holder == "serial_background"
        scheduler.stop()

    def test_locked_source_does_not_block_reenumerated_winner_migration(self):
        scheduler = SdrScheduler(
            MagicMock(),
            {"weather_alerts_override_lock": False},
        )
        scheduler._running = True
        acquire_serial, yield_serial = _cb_pair()
        acquire_index, yield_index = _cb_pair()

        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=DEVICES):
            scheduler.register(
                "07143901",
                "serial_background",
                PRIORITY_BACKGROUND,
                acquire_serial,
                yield_serial,
                continuous=True,
                device_selector="serial",
            )
            scheduler.register(
                "1",
                "index_critical",
                PRIORITY_CRITICAL,
                acquire_index,
                yield_index,
                continuous=True,
                device_selector="index",
            )

        source_key = scheduler._registrations[("1", "index_critical")]
        source = scheduler._dongles[source_key]
        serial_lease = MagicMock(index=1, canonical_id="serial:07143901")
        source.current_holder = "serial_background"
        source.locked_by = "serial_background"
        source.relock_after = "index_critical"
        source.device_index = 1
        source.slots["serial_background"].is_active = True
        source.slots["serial_background"].device_lease = serial_lease
        source.slots["serial_background"].allocation_generation = 57

        changed_lease = MagicMock(index=1, canonical_id="serial:99999999")
        with (
            patch("reticulumpi.rtlsdr.claim_device", return_value=changed_lease) as claim,
            patch("reticulumpi.sdr_scheduler._USB_SETTLE_DELAY", 0),
            scheduler._condition,
        ):
            scheduler._evaluate(source_key)

        claim.assert_called_once_with(
            "1",
            caller="index_critical",
            selector="index",
        )
        changed_lease.release.assert_called_once_with()
        acquire_serial.assert_not_called()
        yield_serial.assert_not_called()
        acquire_index.assert_not_called()
        yield_index.assert_not_called()

        target_key = scheduler._registrations[("1", "index_critical")]
        target = scheduler._dongles[target_key]
        assert target is not source
        assert target.canonical_id == "serial:99999999"
        assert target.current_holder is None
        assert target.locked_by is None
        assert target.relock_after is None
        assert target.device_index is None
        assert target.slots["index_critical"].device_lease is None
        assert target.slots["index_critical"].allocation_generation == 0

        assert source.current_holder == "serial_background"
        assert source.locked_by == "serial_background"
        assert source.relock_after is None
        assert source.device_index == 1
        assert source.slots["serial_background"].device_lease is serial_lease
        assert source.slots["serial_background"].allocation_generation == 57
        assert "index_critical" not in source.slots
        serial_lease.release.assert_not_called()
        scheduler.stop()

    def test_unregister_removes_slot_and_bg_order(self, sched):
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, *_cb_pair(), continuous=True)
        sched.unregister(SERIAL, "ais")
        assert "ais" not in sched.get_status()[SERIAL]["slots"]
        assert "ais" not in sched.get_status()[SERIAL]["bg_order"]

    def test_unregister_active_slot_calls_yield(self, sched):
        acq, yld = _cb_pair()
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, acq, yld, continuous=True)
        dongle = sched._dongles[SERIAL]
        dongle.slots["ais"].is_active = True
        dongle.current_holder = "ais"
        sched.unregister(SERIAL, "ais")
        yld.assert_called_once()


class TestProbeRateLimiting:
    @staticmethod
    def _locked_contenders(winner_selector="index"):
        scheduler = SdrScheduler(
            MagicMock(),
            {"weather_alerts_override_lock": False},
        )
        scheduler._running = True
        acquire_background, yield_background = _cb_pair()
        acquire_critical, yield_critical = _cb_pair()
        winner_config = "1" if winner_selector == "index" else "07143901"

        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=DEVICES):
            scheduler.register(
                "07143901",
                "background",
                PRIORITY_BACKGROUND,
                acquire_background,
                yield_background,
                continuous=True,
                device_selector="serial",
            )
            scheduler.register(
                winner_config,
                "critical",
                PRIORITY_CRITICAL,
                acquire_critical,
                yield_critical,
                continuous=True,
                device_selector=winner_selector,
            )

        storage_key = scheduler._registrations[(winner_config, "critical")]
        dongle = scheduler._dongles[storage_key]
        holder_lease = MagicMock(index=1, canonical_id="serial:07143901")
        dongle.current_holder = "background"
        dongle.device_index = 1
        dongle.slots["background"].is_active = True
        dongle.slots["background"].device_lease = holder_lease
        assert scheduler.lock("07143901", "background") is True
        return (
            scheduler,
            storage_key,
            dongle,
            acquire_background,
            yield_background,
            acquire_critical,
            yield_critical,
            holder_lease,
        )

    def test_locked_index_winner_probes_once_while_state_is_unchanged(self):
        (
            scheduler,
            storage_key,
            dongle,
            acquire_background,
            yield_background,
            acquire_critical,
            yield_critical,
            holder_lease,
        ) = self._locked_contenders()
        busy = DeviceBusyError(
            "1",
            "background",
            ResolvedDevice(1, "serial:07143901"),
        )

        with (
            patch("reticulumpi.rtlsdr.claim_device", side_effect=busy) as claim,
            scheduler._condition,
        ):
            for _ in range(10):
                scheduler._evaluate(storage_key)

        assert claim.call_count == 1
        acquire_background.assert_not_called()
        yield_background.assert_not_called()
        acquire_critical.assert_not_called()
        yield_critical.assert_not_called()
        assert dongle.current_holder == "background"
        assert dongle.locked_by == "background"
        assert dongle.slots["background"].device_lease is holder_lease
        holder_lease.release.assert_not_called()

    def test_blocked_strict_serial_winner_never_probes_identity(self):
        (
            scheduler,
            storage_key,
            dongle,
            _acquire_background,
            yield_background,
            acquire_critical,
            _yield_critical,
            holder_lease,
        ) = self._locked_contenders(winner_selector="serial")

        with (
            patch("reticulumpi.rtlsdr.claim_device") as claim,
            scheduler._condition,
        ):
            for _ in range(10):
                scheduler._evaluate(storage_key)

        claim.assert_not_called()
        yield_background.assert_not_called()
        acquire_critical.assert_not_called()
        assert dongle.current_holder == "background"
        assert dongle.slots["background"].device_lease is holder_lease

    def test_unlock_forces_fresh_probe_before_preemption(self):
        (
            scheduler,
            storage_key,
            dongle,
            _acquire_background,
            yield_background,
            acquire_critical,
            _yield_critical,
            holder_lease,
        ) = self._locked_contenders()
        busy = DeviceBusyError(
            "1",
            "background",
            ResolvedDevice(1, "serial:07143901"),
        )
        critical_lease = MagicMock(index=1, canonical_id="serial:07143901")

        with (
            patch(
                "reticulumpi.rtlsdr.claim_device",
                side_effect=[busy, busy, critical_lease],
            ) as claim,
            patch("reticulumpi.sdr_scheduler._USB_SETTLE_DELAY", 0),
        ):
            with scheduler._condition:
                for _ in range(10):
                    scheduler._evaluate(storage_key)
            scheduler.unlock("07143901", "background")
            with scheduler._condition:
                scheduler._evaluate(storage_key)

        assert claim.call_count == 3
        yield_background.assert_called_once_with("critical", "", None)
        holder_lease.release.assert_called_once_with()
        acquire_critical.assert_called_once_with("1", 1)
        assert dongle.current_holder == "critical"

    def test_allowed_preflight_claim_error_is_rate_limited(self):
        (
            scheduler,
            storage_key,
            dongle,
            _acquire_background,
            yield_background,
            acquire_critical,
            _yield_critical,
            holder_lease,
        ) = self._locked_contenders()
        scheduler.unlock("07143901", "background")

        with (
            patch(
                "reticulumpi.rtlsdr.claim_device",
                side_effect=RuntimeError("inventory unavailable"),
            ) as claim,
            scheduler._condition,
        ):
            for _ in range(10):
                scheduler._evaluate(storage_key)

        assert claim.call_count == 1
        yield_background.assert_not_called()
        acquire_critical.assert_not_called()
        assert dongle.current_holder == "background"
        assert dongle.slots["background"].device_lease is holder_lease
        holder_lease.release.assert_not_called()

    def test_no_holder_claim_error_is_rate_limited(self):
        scheduler = SdrScheduler(MagicMock())
        scheduler._running = True
        acquire, yield_cb = _cb_pair()
        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=DEVICES):
            scheduler.register(
                "1",
                "critical",
                PRIORITY_CRITICAL,
                acquire,
                yield_cb,
                continuous=True,
                device_selector="index",
            )
        storage_key = scheduler._registrations[("1", "critical")]

        with (
            patch(
                "reticulumpi.rtlsdr.claim_device",
                side_effect=RuntimeError("device missing"),
            ) as claim,
            patch("reticulumpi.sdr_scheduler._USB_SETTLE_DELAY", 0),
            scheduler._condition,
        ):
            for _ in range(10):
                scheduler._evaluate(storage_key)

        assert claim.call_count == 1
        acquire.assert_not_called()
        yield_cb.assert_not_called()
        assert scheduler._dongles[storage_key].current_holder is None

    def test_no_holder_callback_failure_is_rate_limited(self):
        scheduler = SdrScheduler(MagicMock())
        scheduler._running = True
        acquire = MagicMock(side_effect=RuntimeError("decoder failed"))
        yield_cb = MagicMock(return_value=True)
        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=DEVICES):
            scheduler.register(
                "1",
                "critical",
                PRIORITY_CRITICAL,
                acquire,
                yield_cb,
                continuous=True,
                device_selector="index",
            )
        storage_key = scheduler._registrations[("1", "critical")]
        lease = MagicMock(index=1, canonical_id="serial:07143901")

        with (
            patch("reticulumpi.rtlsdr.claim_device", return_value=lease) as claim,
            patch("reticulumpi.sdr_scheduler._USB_SETTLE_DELAY", 0),
            scheduler._condition,
        ):
            for _ in range(10):
                scheduler._evaluate(storage_key)

        claim.assert_called_once_with("1", caller="critical", selector="index")
        acquire.assert_called_once_with("1", 1)
        lease.release.assert_called_once_with()
        yield_cb.assert_not_called()
        assert scheduler._dongles[storage_key].current_holder is None

    def test_same_identity_busy_claim_is_deferred_without_callback(self):
        scheduler = SdrScheduler(MagicMock())
        scheduler._running = True
        acquire, yield_cb = _cb_pair()
        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=DEVICES):
            scheduler.register(
                "1",
                "critical",
                PRIORITY_CRITICAL,
                acquire,
                yield_cb,
                continuous=True,
                device_selector="index",
            )
        storage_key = scheduler._registrations[("1", "critical")]
        dongle = scheduler._dongles[storage_key]
        busy = DeviceBusyError(
            "1",
            "other",
            ResolvedDevice(1, "serial:07143901"),
        )

        with (
            patch("reticulumpi.rtlsdr.claim_device", side_effect=busy),
            patch("reticulumpi.sdr_scheduler._USB_SETTLE_DELAY", 0),
            scheduler._condition,
        ):
            scheduler._do_acquire(dongle, "critical")

        slot = dongle.slots["critical"]
        acquire.assert_not_called()
        yield_cb.assert_not_called()
        assert slot.allocation_generation == 0
        assert slot.acquire_retry_signature is not None

    def test_preflight_release_failure_defers_handoff(self):
        scheduler = SdrScheduler(MagicMock())
        scheduler._running = True
        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=DEVICES):
            scheduler.register(
                "1",
                "critical",
                PRIORITY_CRITICAL,
                *_cb_pair(),
                continuous=True,
                device_selector="index",
            )
        storage_key = scheduler._registrations[("1", "critical")]
        dongle = scheduler._dongles[storage_key]
        lease = MagicMock(index=1, canonical_id="serial:07143901")
        lease.release.side_effect = RuntimeError("release failed")

        with (
            patch("reticulumpi.rtlsdr.claim_device", return_value=lease),
            scheduler._condition,
        ):
            deferred = scheduler._preflight_winner_identity_locked(
                dongle,
                "critical",
                can_preempt=True,
            )

        assert deferred is True
        assert dongle.slots["critical"].identity_preflight_deferred is True
        lease.release.assert_called_once_with()

    def test_registration_removed_during_claim_releases_stale_lease(self):
        scheduler = SdrScheduler(MagicMock())
        scheduler._running = True
        acquire, yield_cb = _cb_pair()
        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=DEVICES):
            scheduler.register(
                "1",
                "critical",
                PRIORITY_CRITICAL,
                acquire,
                yield_cb,
                continuous=True,
                device_selector="index",
            )
        storage_key = scheduler._registrations[("1", "critical")]
        dongle = scheduler._dongles[storage_key]
        lease = MagicMock(index=1, canonical_id="serial:07143901")
        lease.release.side_effect = RuntimeError("release failed")

        def unregister_during_claim(*_args, **_kwargs):
            scheduler.unregister("1", "critical")
            return lease

        with (
            patch(
                "reticulumpi.rtlsdr.claim_device",
                side_effect=unregister_during_claim,
            ),
            patch("reticulumpi.sdr_scheduler._USB_SETTLE_DELAY", 0),
            scheduler._condition,
        ):
            scheduler._do_acquire(dongle, "critical")

        acquire.assert_not_called()
        yield_cb.assert_not_called()
        lease.release.assert_called_once_with()
        assert ("1", "critical") not in scheduler._registrations


class TestPriorityPreemption:
    def test_p0_preempts_p2(self, sched):
        acq_bg, yld_bg = _cb_pair()
        acq_p0, _ = _cb_pair()
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, acq_bg, yld_bg, continuous=True)
        sched.register(SERIAL, "wx", PRIORITY_CRITICAL, acq_p0, _cb_pair()[1], continuous=True)
        dongle = sched._dongles[SERIAL]
        dongle.current_holder = "ais"
        dongle.slots["ais"].is_active = True
        with sched._condition:
            sched._evaluate(SERIAL)
        yld_bg.assert_called_once()
        acq_p0.assert_called_once()

    def test_p1_preempts_p2_during_window(self, sched):
        acq_bg, yld_bg = _cb_pair()
        acq_p1, _ = _cb_pair()
        now = time.time()
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, acq_bg, yld_bg, continuous=True)
        sched.register(
            SERIAL,
            "sat",
            PRIORITY_SCHEDULED,
            acq_p1,
            _cb_pair()[1],
            windows=[TimeWindow(now - 10, now + 300, "sat")],
        )
        dongle = sched._dongles[SERIAL]
        dongle.current_holder = "ais"
        dongle.slots["ais"].is_active = True
        with sched._condition:
            sched._evaluate(SERIAL)
        yld_bg.assert_called_once()
        acq_p1.assert_called_once()

    def test_p2_cannot_preempt_p1(self, sched):
        _, yld_p1 = _cb_pair()
        acq_bg, _ = _cb_pair()
        now = time.time()
        sched.register(
            SERIAL,
            "sat",
            PRIORITY_SCHEDULED,
            _cb_pair()[0],
            yld_p1,
            windows=[TimeWindow(now - 10, now + 300, "sat")],
        )
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, acq_bg, _cb_pair()[1], continuous=True)
        dongle = sched._dongles[SERIAL]
        dongle.current_holder = "sat"
        dongle.slots["sat"].is_active = True
        with sched._condition:
            sched._evaluate(SERIAL)
        yld_p1.assert_not_called()
        acq_bg.assert_not_called()

    def test_removed_window_during_preflight_does_not_yield_holder(self):
        scheduler = _make_scheduler()
        scheduler._running = True
        acquire_bg, yield_bg = _cb_pair()
        acquire_scheduled, yield_scheduled = _cb_pair()
        now = time.time()
        scheduler.register(
            SERIAL,
            "background",
            PRIORITY_BACKGROUND,
            acquire_bg,
            yield_bg,
            continuous=True,
        )
        scheduler.register(
            SERIAL,
            "scheduled",
            PRIORITY_SCHEDULED,
            acquire_scheduled,
            yield_scheduled,
            windows=[TimeWindow(now - 10, now + 300, "scheduled")],
        )
        dongle = scheduler._dongles[SERIAL]
        holder_lease = MagicMock(index=0, canonical_id=f"serial:{SERIAL}")
        dongle.current_holder = "background"
        dongle.device_index = 0
        dongle.bg_last_rotation = time.monotonic()
        dongle.slots["background"].is_active = True
        dongle.slots["background"].device_lease = holder_lease

        preflight_lease = MagicMock(index=0, canonical_id=f"serial:{SERIAL}")

        def remove_window_during_claim(*_args, **_kwargs):
            scheduler.remove_windows(SERIAL, "scheduled")
            return preflight_lease

        with (
            patch(
                "reticulumpi.rtlsdr.claim_device",
                side_effect=remove_window_during_claim,
            ) as claim,
            scheduler._condition,
        ):
            scheduler._evaluate(SERIAL)

        claim.assert_called_once_with(
            SERIAL,
            caller="scheduled",
            selector="auto",
        )
        preflight_lease.release.assert_called_once_with()
        yield_bg.assert_not_called()
        acquire_scheduled.assert_not_called()
        yield_scheduled.assert_not_called()
        assert dongle.current_holder == "background"
        assert dongle.device_index == 0
        assert dongle.slots["background"].device_lease is holder_lease
        holder_lease.release.assert_not_called()


class TestLocking:
    def test_lock_prevents_p1_preemption(self, sched):
        _, yld_bg = _cb_pair()
        now = time.time()
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, _cb_pair()[0], yld_bg, continuous=True)
        sched.register(
            SERIAL,
            "sat",
            PRIORITY_SCHEDULED,
            *_cb_pair(),
            windows=[TimeWindow(now - 10, now + 300, "sat")],
        )
        dongle = sched._dongles[SERIAL]
        dongle.current_holder = "ais"
        dongle.slots["ais"].is_active = True
        sched.lock(SERIAL, "ais")
        with sched._condition:
            sched._evaluate(SERIAL)
        yld_bg.assert_not_called()

    def test_p0_overrides_lock_when_enabled(self, sched):
        _, yld_bg = _cb_pair()
        acq_p0, _ = _cb_pair()
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, _cb_pair()[0], yld_bg, continuous=True)
        sched.register(SERIAL, "wx", PRIORITY_CRITICAL, acq_p0, _cb_pair()[1], continuous=True)
        dongle = sched._dongles[SERIAL]
        dongle.current_holder = "ais"
        dongle.slots["ais"].is_active = True
        sched.lock(SERIAL, "ais")
        with sched._condition:
            sched._evaluate(SERIAL)
        yld_bg.assert_called_once()
        acq_p0.assert_called_once()

    def test_p0_blocked_by_lock_when_override_disabled(self):
        with _patch_hw(), patch("reticulumpi.sdr_scheduler._USB_SETTLE_DELAY", 0):
            s = _make_scheduler(weather_alerts_override_lock=False)
            _, yld_bg = _cb_pair()
            s.register(SERIAL, "ais", PRIORITY_BACKGROUND, _cb_pair()[0], yld_bg, continuous=True)
            s.register(SERIAL, "wx", PRIORITY_CRITICAL, *_cb_pair(), continuous=True)
            d = s._dongles[SERIAL]
            d.current_holder = "ais"
            d.slots["ais"].is_active = True
            s.lock(SERIAL, "ais")
            with s._condition:
                s._evaluate(SERIAL)
            yld_bg.assert_not_called()

    def test_lock_requires_holder_and_unlock_clears(self, sched):
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, *_cb_pair(), continuous=True)
        dongle = sched._dongles[SERIAL]
        assert sched.lock(SERIAL, "ais") is False  # rejected: not current holder
        assert dongle.locked_by is None
        dongle.current_holder = "ais"
        assert sched.lock(SERIAL, "ais") is True
        assert dongle.locked_by == "ais"
        sched.unlock(SERIAL, "ais")
        assert dongle.locked_by is None

    def test_relock_after_p0_preemption(self, sched):
        acq_bg, yld_bg = _cb_pair()
        acq_p0, yld_p0 = _cb_pair()
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, acq_bg, yld_bg, continuous=True)
        sched.register(SERIAL, "wx", PRIORITY_CRITICAL, acq_p0, yld_p0, continuous=True)
        dongle = sched._dongles[SERIAL]

        # Give ais the dongle and lock it
        dongle.current_holder = "ais"
        dongle.slots["ais"].is_active = True
        sched.lock(SERIAL, "ais")
        assert dongle.locked_by == "ais"

        # P0 preempts — lock broken, relock_after saved
        with sched._condition:
            sched._evaluate(SERIAL)
        assert dongle.relock_after == "ais"
        assert dongle.locked_by is None

        # Now yield P0 (unregister) and let ais re-acquire
        sched.unregister(SERIAL, "wx")
        assert dongle.relock_after == "ais"  # not clobbered by P0 yield
        with sched._condition:
            sched._evaluate(SERIAL)
        assert dongle.current_holder == "ais"
        assert dongle.locked_by == "ais"  # lock auto-restored
        assert dongle.relock_after is None


class TestTimeWindows:
    def test_add_and_remove_windows(self, sched):
        now = time.time()
        sched.register(SERIAL, "sat", PRIORITY_SCHEDULED, *_cb_pair())
        sched.add_window(SERIAL, "sat", now + 100, now + 200, label="ISS")
        assert sched.get_schedule(SERIAL)[0]["label"] == "ISS"
        sched.remove_windows(SERIAL, "sat")
        assert sched.get_schedule(SERIAL) == []

    def test_expired_windows_pruned(self, sched):
        past = time.time() - 100
        sched.register(
            SERIAL,
            "sat",
            PRIORITY_SCHEDULED,
            *_cb_pair(),
            windows=[TimeWindow(past - 200, past, "sat")],
        )
        sched._expire_windows(sched._dongles[SERIAL], time.time())
        assert len(sched._dongles[SERIAL].slots["sat"].windows) == 0

    def test_p1_ignored_outside_window(self, sched):
        _, yld_bg = _cb_pair()
        future = time.time() + 3600
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, _cb_pair()[0], yld_bg, continuous=True)
        sched.register(
            SERIAL,
            "sat",
            PRIORITY_SCHEDULED,
            *_cb_pair(),
            windows=[TimeWindow(future, future + 300, "sat")],
        )
        dongle = sched._dongles[SERIAL]
        dongle.current_holder = "ais"
        dongle.slots["ais"].is_active = True
        with sched._condition:
            sched._evaluate(SERIAL)
        yld_bg.assert_not_called()


class TestBackgroundRoundRobin:
    def test_peek_background_is_non_mutating_when_no_slot_is_eligible(self):
        scheduler = _make_scheduler()
        assert scheduler._peek_background(scheduler._dongles[SERIAL], 0.0) is None

        scheduler.register(
            SERIAL,
            "ais",
            PRIORITY_BACKGROUND,
            *_cb_pair(),
            continuous=True,
        )
        scheduler.register(
            SERIAL,
            "acars",
            PRIORITY_BACKGROUND,
            *_cb_pair(),
            continuous=True,
        )
        dongle = scheduler._dongles[SERIAL]
        dongle.slots["ais"].suspended = True
        dongle.slots["acars"].suspended = True
        original_index = dongle.bg_index

        assert scheduler._peek_background(dongle, 0.0) is None
        assert dongle.bg_index == original_index

    def test_single_bg_acquires(self, sched):
        acq, _ = _cb_pair()
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, acq, _cb_pair()[1], continuous=True)
        with sched._condition:
            sched._evaluate(SERIAL)
        acq.assert_called_once_with(SERIAL, 0)

    def test_rotation_after_slice_expires(self, sched):
        _, yld1 = _cb_pair()
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, _cb_pair()[0], yld1, continuous=True)
        sched.register(SERIAL, "acars", PRIORITY_BACKGROUND, *_cb_pair(), continuous=True)
        dongle = sched._dongles[SERIAL]
        dongle.current_holder = "ais"
        dongle.slots["ais"].is_active = True
        dongle.bg_last_rotation = time.monotonic() - dongle.bg_slice_seconds - 1
        dongle.bg_index = 0
        with sched._condition:
            sched._evaluate(SERIAL)
        yld1.assert_called_once()

    def test_bg_stays_within_slice(self, sched):
        _, yld1 = _cb_pair()
        acq2, _ = _cb_pair()
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, _cb_pair()[0], yld1, continuous=True)
        sched.register(SERIAL, "acars", PRIORITY_BACKGROUND, acq2, _cb_pair()[1], continuous=True)
        dongle = sched._dongles[SERIAL]
        dongle.current_holder = "ais"
        dongle.slots["ais"].is_active = True
        dongle.bg_last_rotation = time.monotonic()
        with sched._condition:
            sched._evaluate(SERIAL)
        yld1.assert_not_called()
        acq2.assert_not_called()


class TestHandoffProtocol:
    def test_yield_cb_receives_preemptor_info(self, sched):
        _, yld_bg = _cb_pair()
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, _cb_pair()[0], yld_bg, continuous=True)
        sched.register(
            SERIAL, "wx", PRIORITY_CRITICAL, *_cb_pair(), label="SAME Alert", continuous=True
        )
        dongle = sched._dongles[SERIAL]
        dongle.current_holder = "ais"
        dongle.slots["ais"].is_active = True
        with sched._condition:
            sched._evaluate(SERIAL)
        preempted_by, label, _ = yld_bg.call_args[0]
        assert preempted_by == "wx"
        assert label == "SAME Alert"

    def test_acquisition_completed_after_stop_releases_exact_lease(self):
        scheduler = _make_scheduler()
        order: list[str] = []
        acquire = MagicMock(side_effect=lambda *_args: order.append("acquire"))
        yield_cb = MagicMock(side_effect=lambda *_args: order.append("cleanup"), return_value=True)
        scheduler.register(
            SERIAL,
            "ais",
            PRIORITY_BACKGROUND,
            acquire,
            yield_cb,
            continuous=True,
        )
        lease = MagicMock()
        lease.index = 2
        lease.release.side_effect = lambda: order.append("release")
        with (
            patch("reticulumpi.rtlsdr.claim_device", return_value=lease),
            patch("reticulumpi.sdr_scheduler._USB_SETTLE_DELAY", 0),
            scheduler._condition,
        ):
            scheduler._do_acquire(scheduler._dongles[SERIAL], "ais")

        acquire.assert_called_once_with(SERIAL, 2)
        yield_cb.assert_called_once_with("", "", None)
        lease.release.assert_called_once()
        assert order == ["acquire", "cleanup", "release"]
        assert scheduler._dongles[SERIAL].current_holder is None

    def test_release_requested_inside_acquire_callback_cancels_stale_grant(self):
        scheduler = _make_scheduler()
        scheduler._running = True
        lease = MagicMock(index=2)

        def acquire(serial, _index):
            scheduler.dongle_released(serial, "ais")

        yield_cb = MagicMock(return_value=True)
        scheduler.register(
            SERIAL,
            "ais",
            PRIORITY_BACKGROUND,
            acquire,
            yield_cb,
            continuous=True,
        )
        with (
            patch("reticulumpi.rtlsdr.claim_device", return_value=lease),
            patch("reticulumpi.sdr_scheduler._USB_SETTLE_DELAY", 0),
            scheduler._condition,
        ):
            scheduler._do_acquire(scheduler._dongles[SERIAL], "ais")

        slot = scheduler._dongles[SERIAL].slots["ais"]
        lease.release.assert_called_once_with()
        yield_cb.assert_called_once_with("", "", None)
        assert scheduler._dongles[SERIAL].current_holder is None
        assert slot.device_lease is None
        assert slot.is_active is False

    def test_slot_replaced_during_acquire_cannot_receive_stale_lease(self):
        scheduler = _make_scheduler()
        scheduler._running = True
        lease = MagicMock(index=2)
        replacement_acquire = MagicMock()

        def acquire(serial, _index):
            scheduler.register(
                serial,
                "ais",
                PRIORITY_BACKGROUND,
                replacement_acquire,
                MagicMock(return_value=True),
                continuous=True,
            )

        stale_yield = MagicMock(return_value=True)
        scheduler.register(
            SERIAL,
            "ais",
            PRIORITY_BACKGROUND,
            acquire,
            stale_yield,
            continuous=True,
        )
        with (
            patch("reticulumpi.rtlsdr.claim_device", return_value=lease),
            patch("reticulumpi.sdr_scheduler._USB_SETTLE_DELAY", 0),
            scheduler._condition,
        ):
            scheduler._do_acquire(scheduler._dongles[SERIAL], "ais")

        slot = scheduler._dongles[SERIAL].slots["ais"]
        lease.release.assert_called_once_with()
        stale_yield.assert_called_once_with("", "", None)
        assert slot.acquire_cb is replacement_acquire
        assert slot.device_lease is None
        assert scheduler._dongles[SERIAL].current_holder is None

    def test_wall_clock_jump_does_not_rotate_background_slot(self):
        acquire, yield_cb = _cb_pair()
        scheduler = _make_scheduler()
        scheduler._running = True
        scheduler.register(
            SERIAL,
            "ais",
            PRIORITY_BACKGROUND,
            acquire,
            yield_cb,
            continuous=True,
        )
        scheduler.register(
            SERIAL,
            "acars",
            PRIORITY_BACKGROUND,
            *_cb_pair(),
            continuous=True,
        )
        dongle = scheduler._dongles[SERIAL]
        dongle.current_holder = "ais"
        dongle.slots["ais"].is_active = True
        dongle.bg_last_rotation = 100.0

        with (
            patch("reticulumpi.sdr_scheduler.time.time", return_value=9_999_999.0),
            patch("reticulumpi.sdr_scheduler.time.monotonic", return_value=101.0),
            scheduler._condition,
        ):
            scheduler._evaluate(SERIAL)

        yield_cb.assert_not_called()
        assert dongle.current_holder == "ais"


class TestIntrospection:
    def test_get_status_structure(self, sched):
        sched.register(
            SERIAL, "ais", PRIORITY_BACKGROUND, *_cb_pair(), label="AIS Decoder", continuous=True
        )
        info = sched.get_status()[SERIAL]
        assert info["current_holder"] is None
        assert info["locked_by"] is None
        assert info["bg_order"] == ["ais"]
        assert info["slots"]["ais"]["label"] == "AIS Decoder"
        assert info["slots"]["ais"]["active"] is False

    def test_get_schedule_sorted_and_unknown_empty(self, sched):
        now = time.time()
        windows = [
            TimeWindow(now + 200, now + 300, "sat", "B"),
            TimeWindow(now + 100, now + 150, "sat", "A"),
        ]
        sched.register(SERIAL, "sat", PRIORITY_SCHEDULED, *_cb_pair(), windows=windows)
        assert [w["label"] for w in sched.get_schedule(SERIAL)] == ["A", "B"]
        assert sched.get_schedule("NOSUCH") == []

    def test_get_generation_follows_configured_index_route(self):
        scheduler = SdrScheduler(MagicMock())
        with patch("reticulumpi.rtlsdr.enumerate_devices", return_value=[]):
            scheduler.register(
                "1",
                "decoder",
                PRIORITY_BACKGROUND,
                *_cb_pair(),
                continuous=True,
                device_selector="index",
            )

        provisional_key = scheduler._registrations[("1", "decoder")]
        provisional = scheduler._dongles[provisional_key]
        with scheduler._condition:
            assert scheduler._reconcile_idle_slot_locked(
                provisional,
                provisional.slots["decoder"],
                "decoder",
                ResolvedDevice(1, "serial:07143901"),
            )

        storage_key = scheduler._registrations[("1", "decoder")]
        assert storage_key != "1"
        assert scheduler.get_generation("1") == scheduler._dongles[storage_key].generation


class TestLifecycle:
    def test_start_and_stop(self):
        with _patch_hw(), patch("reticulumpi.sdr_scheduler._USB_SETTLE_DELAY", 0):
            s = _make_scheduler()
            s.start()
            assert s._thread is not None and s._thread.is_alive()
            s.stop()
            assert s._thread is None

    def test_dongle_released_clears_holder(self, sched):
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, *_cb_pair(), continuous=True)
        dongle = sched._dongles[SERIAL]
        lease = MagicMock()
        dongle.current_holder = "ais"
        dongle.slots["ais"].is_active = True
        dongle.slots["ais"].device_lease = lease
        sched.dongle_released(SERIAL, "ais")
        assert dongle.current_holder is None
        assert dongle.slots["ais"].is_active is False
        assert dongle.slots["ais"].device_lease is None
        lease.release.assert_called_once_with()

    def test_suspend_releases_lease_and_blocks_reacquisition(self, sched):
        acquire, yield_cb = _cb_pair()
        sched.register(
            SERIAL,
            "ais",
            PRIORITY_BACKGROUND,
            acquire,
            yield_cb,
            continuous=True,
        )
        dongle = sched._dongles[SERIAL]
        lease = MagicMock()
        dongle.current_holder = "ais"
        dongle.slots["ais"].is_active = True
        dongle.slots["ais"].device_lease = lease

        sched.suspend(SERIAL, "ais")
        with sched._condition:
            sched._evaluate(SERIAL)

        assert dongle.current_holder is None
        assert dongle.slots["ais"].suspended is True
        lease.release.assert_called_once_with()
        acquire.assert_not_called()

    def test_resume_requires_current_registration_and_only_restores_eligibility(self, sched):
        acquire, yield_cb = _cb_pair()
        sched.register(
            SERIAL,
            "ais",
            PRIORITY_BACKGROUND,
            acquire,
            yield_cb,
            continuous=True,
        )
        token = sched.suspend(SERIAL, "ais")
        assert token is not None
        assert sched.resume(SERIAL, "ais", registration_id=token) is True
        assert acquire.call_count == 0

        sched.unregister(SERIAL, "ais")
        sched.register(
            SERIAL,
            "ais",
            PRIORITY_BACKGROUND,
            acquire,
            yield_cb,
            continuous=True,
        )
        sched.suspend(SERIAL, "ais")
        assert sched.resume(SERIAL, "ais", registration_id=token) is False

    def test_stale_generation_cannot_release_new_holder(self, sched):
        sched.register(
            SERIAL,
            "ais",
            PRIORITY_BACKGROUND,
            *_cb_pair(),
            continuous=True,
        )
        dongle = sched._dongles[SERIAL]
        slot = dongle.slots["ais"]
        lease = MagicMock()
        dongle.current_holder = "ais"
        slot.is_active = True
        slot.device_lease = lease
        current_generation = dongle.generation
        slot.allocation_generation = current_generation

        sched.dongle_released(
            SERIAL,
            "ais",
            generation=current_generation - 1,
        )

        assert dongle.current_holder == "ais"
        assert slot.device_lease is lease
        lease.release.assert_not_called()

        sched.dongle_released(
            SERIAL,
            "ais",
            generation=current_generation,
        )
        assert dongle.current_holder is None
        lease.release.assert_called_once_with()

    def test_other_registration_does_not_invalidate_active_allocation_release(self, sched):
        sched.register(
            SERIAL,
            "ais",
            PRIORITY_BACKGROUND,
            *_cb_pair(),
            continuous=True,
        )
        dongle = sched._dongles[SERIAL]
        slot = dongle.slots["ais"]
        lease = MagicMock()
        dongle.current_holder = "ais"
        slot.is_active = True
        slot.device_lease = lease
        slot.allocation_generation = dongle.generation
        allocation_generation = slot.allocation_generation

        sched.register(
            SERIAL,
            "acars",
            PRIORITY_BACKGROUND,
            *_cb_pair(),
            continuous=True,
        )
        sched.dongle_released(
            SERIAL,
            "ais",
            generation=allocation_generation,
        )

        assert dongle.current_holder is None
        lease.release.assert_called_once_with()
