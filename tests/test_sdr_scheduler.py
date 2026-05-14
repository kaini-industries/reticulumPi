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

SERIAL = "00000001"


def _make_scheduler(bus=None, **config_kw) -> SdrScheduler:
    config = {"managed_dongles": [{"serial": SERIAL}], **config_kw}
    return SdrScheduler(bus or MagicMock(), config)


def _cb_pair() -> tuple[MagicMock, MagicMock]:
    """Return (acquire_cb, yield_cb) mocks."""
    return MagicMock(), MagicMock(return_value=True)


def _patch_hw():
    """Stub out rtlsdr hardware calls."""
    return patch.multiple(
        "reticulumpi.rtlsdr",
        resolve_device=MagicMock(return_value=0),
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
    def test_register_creates_slot(self, sched):
        acq, yld = _cb_pair()
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, acq, yld,
                       label="AIS", continuous=True)
        slot = sched.get_status()[SERIAL]["slots"]["ais"]
        assert slot["priority"] == PRIORITY_BACKGROUND
        assert slot["label"] == "AIS"

    def test_register_unknown_serial_creates_dongle(self, sched):
        sched.register("NEW", "fm", PRIORITY_BACKGROUND, *_cb_pair(), continuous=True)
        assert "NEW" in sched.get_status()

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


class TestPriorityPreemption:
    def test_p0_preempts_p2(self, sched):
        acq_bg, yld_bg = _cb_pair()
        acq_p0, _ = _cb_pair()
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, acq_bg, yld_bg,
                       continuous=True)
        sched.register(SERIAL, "wx", PRIORITY_CRITICAL, acq_p0, _cb_pair()[1],
                       continuous=True)
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
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, acq_bg, yld_bg,
                       continuous=True)
        sched.register(SERIAL, "sat", PRIORITY_SCHEDULED, acq_p1, _cb_pair()[1],
                       windows=[TimeWindow(now - 10, now + 300, "sat")])
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
        sched.register(SERIAL, "sat", PRIORITY_SCHEDULED, _cb_pair()[0], yld_p1,
                       windows=[TimeWindow(now - 10, now + 300, "sat")])
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, acq_bg, _cb_pair()[1],
                       continuous=True)
        dongle = sched._dongles[SERIAL]
        dongle.current_holder = "sat"
        dongle.slots["sat"].is_active = True
        with sched._condition:
            sched._evaluate(SERIAL)
        yld_p1.assert_not_called()
        acq_bg.assert_not_called()


class TestLocking:
    def test_lock_prevents_p1_preemption(self, sched):
        _, yld_bg = _cb_pair()
        now = time.time()
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, _cb_pair()[0], yld_bg,
                       continuous=True)
        sched.register(SERIAL, "sat", PRIORITY_SCHEDULED, *_cb_pair(),
                       windows=[TimeWindow(now - 10, now + 300, "sat")])
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
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, _cb_pair()[0], yld_bg,
                       continuous=True)
        sched.register(SERIAL, "wx", PRIORITY_CRITICAL, acq_p0, _cb_pair()[1],
                       continuous=True)
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
            s.register(SERIAL, "ais", PRIORITY_BACKGROUND, _cb_pair()[0], yld_bg,
                       continuous=True)
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
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, acq_bg, yld_bg,
                       continuous=True)
        sched.register(SERIAL, "wx", PRIORITY_CRITICAL, acq_p0, yld_p0,
                       continuous=True)
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
        sched.register(SERIAL, "sat", PRIORITY_SCHEDULED, *_cb_pair(),
                       windows=[TimeWindow(past - 200, past, "sat")])
        sched._expire_windows(sched._dongles[SERIAL], time.time())
        assert len(sched._dongles[SERIAL].slots["sat"].windows) == 0

    def test_p1_ignored_outside_window(self, sched):
        _, yld_bg = _cb_pair()
        future = time.time() + 3600
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, _cb_pair()[0], yld_bg,
                       continuous=True)
        sched.register(SERIAL, "sat", PRIORITY_SCHEDULED, *_cb_pair(),
                       windows=[TimeWindow(future, future + 300, "sat")])
        dongle = sched._dongles[SERIAL]
        dongle.current_holder = "ais"
        dongle.slots["ais"].is_active = True
        with sched._condition:
            sched._evaluate(SERIAL)
        yld_bg.assert_not_called()


class TestBackgroundRoundRobin:
    def test_single_bg_acquires(self, sched):
        acq, _ = _cb_pair()
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, acq, _cb_pair()[1],
                       continuous=True)
        with sched._condition:
            sched._evaluate(SERIAL)
        acq.assert_called_once_with(SERIAL, 0)

    def test_rotation_after_slice_expires(self, sched):
        _, yld1 = _cb_pair()
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, _cb_pair()[0], yld1,
                       continuous=True)
        sched.register(SERIAL, "acars", PRIORITY_BACKGROUND, *_cb_pair(),
                       continuous=True)
        dongle = sched._dongles[SERIAL]
        dongle.current_holder = "ais"
        dongle.slots["ais"].is_active = True
        dongle.bg_last_rotation = time.time() - dongle.bg_slice_seconds - 1
        dongle.bg_index = 0
        with sched._condition:
            sched._evaluate(SERIAL)
        yld1.assert_called_once()

    def test_bg_stays_within_slice(self, sched):
        _, yld1 = _cb_pair()
        acq2, _ = _cb_pair()
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, _cb_pair()[0], yld1,
                       continuous=True)
        sched.register(SERIAL, "acars", PRIORITY_BACKGROUND, acq2, _cb_pair()[1],
                       continuous=True)
        dongle = sched._dongles[SERIAL]
        dongle.current_holder = "ais"
        dongle.slots["ais"].is_active = True
        dongle.bg_last_rotation = time.time()
        with sched._condition:
            sched._evaluate(SERIAL)
        yld1.assert_not_called()
        acq2.assert_not_called()


class TestHandoffProtocol:
    def test_yield_cb_receives_preemptor_info(self, sched):
        _, yld_bg = _cb_pair()
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, _cb_pair()[0], yld_bg,
                       continuous=True)
        sched.register(SERIAL, "wx", PRIORITY_CRITICAL, *_cb_pair(),
                       label="SAME Alert", continuous=True)
        dongle = sched._dongles[SERIAL]
        dongle.current_holder = "ais"
        dongle.slots["ais"].is_active = True
        with sched._condition:
            sched._evaluate(SERIAL)
        preempted_by, label, _ = yld_bg.call_args[0]
        assert preempted_by == "wx"
        assert label == "SAME Alert"


class TestIntrospection:
    def test_get_status_structure(self, sched):
        sched.register(SERIAL, "ais", PRIORITY_BACKGROUND, *_cb_pair(),
                       label="AIS Decoder", continuous=True)
        info = sched.get_status()[SERIAL]
        assert info["current_holder"] is None
        assert info["locked_by"] is None
        assert info["bg_order"] == ["ais"]
        assert info["slots"]["ais"]["label"] == "AIS Decoder"
        assert info["slots"]["ais"]["active"] is False

    def test_get_schedule_sorted_and_unknown_empty(self, sched):
        now = time.time()
        windows = [TimeWindow(now + 200, now + 300, "sat", "B"),
                   TimeWindow(now + 100, now + 150, "sat", "A")]
        sched.register(SERIAL, "sat", PRIORITY_SCHEDULED, *_cb_pair(), windows=windows)
        assert [w["label"] for w in sched.get_schedule(SERIAL)] == ["A", "B"]
        assert sched.get_schedule("NOSUCH") == []


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
        dongle.current_holder = "ais"
        dongle.slots["ais"].is_active = True
        sched.dongle_released(SERIAL, "ais")
        assert dongle.current_holder is None
        assert dongle.slots["ais"].is_active is False
