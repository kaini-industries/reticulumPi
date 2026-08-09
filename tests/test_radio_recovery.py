"""Tests for durable hardware-radio recovery safety controls."""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from io import StringIO

import pytest

from reticulumpi import radio_recovery
from reticulumpi.radio_recovery import PersistentResetLimiter


class Clock:
    def __init__(self, wall: float = 1_800_000_000.0, mono: float = 10_000.0):
        self.wall = wall
        self.mono = mono

    def wall_time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono


def make_limiter(tmp_path, clock, *, maximum=3, boot_id="boot-a"):
    return PersistentResetLimiter(
        str(tmp_path / "watchdog.json"),
        maximum,
        wall_clock=clock.wall_time,
        monotonic_clock=clock.monotonic,
        boot_id=boot_id,
    )


def test_reservation_is_persisted_before_return(tmp_path):
    clock = Clock()
    limiter = make_limiter(tmp_path, clock)

    result = limiter.reserve("soft_reboot")

    assert result.allowed is True
    payload = json.loads((tmp_path / "watchdog.json").read_text())
    assert payload["records"][0]["id"] == result.reservation_id
    assert payload["records"][0]["method"] == "soft_reboot"
    assert os.stat(tmp_path / "watchdog.json").st_mode & 0o777 == 0o600
    assert os.stat(tmp_path / "watchdog.json.lock").st_mode & 0o777 == 0o600


@pytest.mark.parametrize("maximum", [False, True, -1, 257, 1.5])
def test_max_attempts_rejects_bools_and_unenforceable_values(tmp_path, maximum):
    with pytest.raises(ValueError, match="integer between 0 and 256"):
        make_limiter(tmp_path, Clock(), maximum=maximum)


def test_rate_limit_survives_service_restart_same_boot(tmp_path):
    clock = Clock()
    limiter = make_limiter(tmp_path, clock, maximum=2)
    assert limiter.reserve("soft_reboot").allowed
    clock.mono += 10
    assert limiter.reserve("usb_reset").allowed

    restarted = make_limiter(tmp_path, clock, maximum=2)

    blocked = restarted.reserve("soft_reboot")
    assert blocked.allowed is False
    assert blocked.reason == "rate_limited"


def test_rate_limit_survives_host_reboot_with_wall_time(tmp_path):
    clock = Clock()
    limiter = make_limiter(tmp_path, clock, maximum=1, boot_id="boot-a")
    assert limiter.reserve("soft_reboot").allowed
    clock.wall += 30
    clock.mono = 5

    rebooted = make_limiter(tmp_path, clock, maximum=1, boot_id="boot-b")

    assert rebooted.reserve("usb_reset").reason == "rate_limited"


def test_stale_attempts_expire(tmp_path):
    clock = Clock()
    limiter = make_limiter(tmp_path, clock, maximum=1)
    assert limiter.reserve("soft_reboot").allowed
    clock.mono += 3601
    clock.wall += 3601

    assert limiter.reserve("soft_reboot").allowed
    assert limiter.recent_attempts() == 1


def test_cross_boot_clock_rollback_fails_closed_for_one_window(tmp_path):
    clock = Clock()
    limiter = make_limiter(tmp_path, clock, maximum=3, boot_id="boot-a")
    assert limiter.reserve("soft_reboot").allowed
    clock.wall -= 86_400
    clock.mono = 1

    rebooted = make_limiter(tmp_path, clock, maximum=3, boot_id="boot-b")

    blocked = rebooted.reserve("usb_reset")
    assert blocked.allowed is False
    assert blocked.reason == "clock_uncertain"
    clock.mono += 3601
    assert rebooted.reserve("usb_reset").allowed is True


def test_clock_uncertainty_discovered_at_reservation_is_persisted(tmp_path, monkeypatch):
    limiter = make_limiter(tmp_path, Clock())
    monkeypatch.setattr(limiter, "_reload_for_transaction_locked", lambda: False)
    monkeypatch.setattr(limiter, "_recent_locked", lambda **_kwargs: ([], True))

    blocked = limiter.reserve("usb_reset")

    assert blocked.allowed is False
    assert blocked.reason == "clock_uncertain"
    payload = json.loads((tmp_path / "watchdog.json").read_text())
    assert payload["quarantine"]["reason"] == "clock_uncertain"


def test_corrupt_state_fails_closed_then_recovers(tmp_path):
    path = tmp_path / "watchdog.json"
    path.write_text("not-json")
    clock = Clock()
    limiter = make_limiter(tmp_path, clock)

    assert limiter.reserve("soft_reboot").reason == "invalid_state"
    clock.mono += 3601
    assert limiter.reserve("soft_reboot").allowed is True


def test_transaction_preserves_stricter_local_quarantine(tmp_path):
    path = tmp_path / "watchdog.json"
    path.write_text("not-json", encoding="utf-8")
    limiter = make_limiter(tmp_path, Clock())
    assert limiter.state_error == "invalid_state"

    # Simulate a stale peer replacing the shared file with valid state that has
    # no quarantine. The in-process fail-closed window must remain authoritative.
    path.write_text(
        json.dumps({"schema": 1, "records": [], "total_attempts": 0}),
        encoding="utf-8",
    )

    assert limiter.set_metadata("device_identity", {"serial": "radio-a"}) is True
    assert limiter.state_error == "invalid_state"
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["quarantine"]["reason"] == "invalid_state"


def test_oversized_state_fails_closed(tmp_path):
    (tmp_path / "watchdog.json").write_bytes(b"x" * (65 * 1024))
    limiter = make_limiter(tmp_path, Clock())

    assert limiter.reserve("soft_reboot").reason == "invalid_state"


def test_persistence_failure_does_not_authorize_reset(tmp_path, monkeypatch):
    limiter = make_limiter(tmp_path, Clock())

    def fail_save():
        raise OSError("disk full")

    monkeypatch.setattr(limiter, "_save_locked", fail_save)

    result = limiter.reserve("usb_reset")
    assert result.allowed is False
    assert result.reason == "persistence_error"
    assert limiter.total_attempts == 0


def test_concurrent_reservations_cannot_exceed_limit(tmp_path):
    limiter = make_limiter(tmp_path, Clock(), maximum=1)
    barrier = threading.Barrier(8)
    decisions = []

    def reserve():
        barrier.wait()
        decisions.append(limiter.reserve("soft_reboot").allowed)

    workers = [threading.Thread(target=reserve) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert decisions.count(True) == 1
    assert decisions.count(False) == 7


def test_separate_instances_transactionally_share_one_limit(tmp_path):
    clock = Clock()
    limiters = [make_limiter(tmp_path, clock, maximum=1) for _ in range(8)]
    barrier = threading.Barrier(len(limiters))
    decisions = []

    def reserve(limiter):
        barrier.wait()
        decisions.append(limiter.reserve("soft_reboot").allowed)

    workers = [threading.Thread(target=reserve, args=(limiter,)) for limiter in limiters]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert decisions.count(True) == 1
    assert decisions.count(False) == 7
    restarted = make_limiter(tmp_path, clock, maximum=1)
    assert restarted.recent_attempts() == 1
    assert restarted.total_attempts == 1


def test_unlimited_mode_records_every_attempt(tmp_path):
    limiter = make_limiter(tmp_path, Clock(), maximum=0)

    for _ in range(10):
        assert limiter.reserve("soft_reboot").allowed

    assert limiter.total_attempts == 10


def test_metadata_round_trips_atomically(tmp_path):
    clock = Clock()
    limiter = make_limiter(tmp_path, clock)
    identity = {"usb_serial": "E83ABC", "vendor_id": "239a", "product_id": "0029"}

    assert limiter.set_metadata("device_identity", identity)

    restarted = make_limiter(tmp_path, clock)
    assert restarted.metadata()["device_identity"] == identity


def test_stale_instance_metadata_update_preserves_newer_reset_history(tmp_path):
    clock = Clock()
    writer = make_limiter(tmp_path, clock, maximum=2)
    stale_metadata_writer = make_limiter(tmp_path, clock, maximum=2)

    assert writer.reserve("soft_reboot").allowed is True
    assert stale_metadata_writer.set_metadata(
        "device_identity",
        {"serial": "radio-a"},
    )

    restarted = make_limiter(tmp_path, clock, maximum=2)
    assert restarted.recent_attempts() == 1
    assert restarted.total_attempts == 1
    assert restarted.metadata()["device_identity"] == {"serial": "radio-a"}


def test_invalid_record_schema_fails_closed(tmp_path):
    (tmp_path / "watchdog.json").write_text(
        json.dumps({"schema": 99, "records": [], "total_attempts": 0})
    )

    limiter = make_limiter(tmp_path, Clock())

    assert limiter.status()["state_error"] == "invalid_state"


def test_metadata_binding_cannot_clear_active_fail_closed_window(tmp_path):
    (tmp_path / "watchdog.json").write_text("corrupt")
    clock = Clock()
    limiter = make_limiter(tmp_path, clock)

    assert limiter.set_metadata("device_identity", {"serial": "radio-a"}) is True
    assert limiter.status()["state_error"] == "invalid_state"
    assert limiter.reserve("soft_reboot").reason == "invalid_state"


def test_metadata_repair_cannot_clear_quarantine_after_process_restart(tmp_path):
    path = tmp_path / "watchdog.json"
    path.write_text("corrupt")
    clock = Clock()
    limiter = make_limiter(tmp_path, clock)

    assert limiter.set_metadata("device_identity", {"serial": "radio-a"}) is True
    repaired = json.loads(path.read_text())
    assert repaired["quarantine"]["reason"] == "invalid_state"

    restarted = make_limiter(tmp_path, clock)
    blocked = restarted.reserve("soft_reboot")
    assert blocked.allowed is False
    assert blocked.reason == "invalid_state"
    assert restarted.metadata()["device_identity"] == {"serial": "radio-a"}

    clock.mono += 3601
    assert restarted.reserve("soft_reboot").allowed is True


def test_huge_timestamp_is_repaired_as_quarantined_invalid_state(tmp_path):
    path = tmp_path / "watchdog.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "records": [
                    {
                        "id": "huge-time",
                        "method": "soft_reboot",
                        "boot_id": "boot-a",
                        "wall_time": 10**1000,
                        "monotonic_time": 1,
                    }
                ],
                "total_attempts": 1,
            }
        )
    )

    limiter = make_limiter(tmp_path, Clock())

    assert limiter.status()["state_error"] == "invalid_state"
    assert limiter.reserve("soft_reboot").reason == "invalid_state"
    repaired = json.loads(path.read_text())
    assert repaired["records"] == []
    assert repaired["quarantine"]["reason"] == "invalid_state"


def test_boot_id_reader_uses_value_and_portable_fallback(monkeypatch):
    monkeypatch.setattr("builtins.open", lambda *_args, **_kwargs: StringIO("boot-value\n"))
    assert radio_recovery._read_boot_id() == "boot-value"

    def unreadable(*_args, **_kwargs):
        raise OSError("procfs unavailable")

    monkeypatch.setattr("builtins.open", unreadable)
    assert radio_recovery._read_boot_id() == "unknown-boot"


@pytest.mark.parametrize("window", [False, 0, -1, float("nan"), float("inf"), "3600"])
def test_window_rejects_nonpositive_nonfinite_and_non_numeric_values(tmp_path, window):
    with pytest.raises(ValueError, match="window_seconds must be positive"):
        PersistentResetLimiter(str(tmp_path / "state.json"), 1, window_seconds=window)


def test_metadata_validation_removal_and_detached_status(tmp_path):
    limiter = make_limiter(tmp_path, Clock())

    for key in ("", "x" * 65):
        with pytest.raises(ValueError, match="metadata key"):
            limiter.set_metadata(key, {})
    with pytest.raises(ValueError, match="too large"):
        limiter.set_metadata("oversized", {"value": "x" * 9000})

    source = {"serial": "radio-a"}
    assert limiter.set_metadata("device_identity", source)
    source["serial"] = "mutated"
    detached = limiter.metadata()
    detached["device_identity"]["serial"] = "also-mutated"
    assert limiter.metadata()["device_identity"] == {"serial": "radio-a"}
    assert limiter.set_metadata("device_identity", None)
    assert limiter.metadata() == {}
    assert limiter.state_error is None


def test_metadata_persistence_failures_restore_value_and_fail_closed(tmp_path, monkeypatch):
    limiter = make_limiter(tmp_path, Clock())
    assert limiter.set_metadata("device_identity", {"serial": "radio-a"})

    def fail_save():
        raise OSError("disk full")

    monkeypatch.setattr(limiter, "_save_locked", fail_save)
    assert limiter.set_metadata("device_identity", {"serial": "radio-b"}) is False
    assert limiter.metadata()["device_identity"] == {"serial": "radio-a"}
    assert limiter.state_error == "persistence_error"


def test_state_lock_failures_never_authorize_metadata_or_reset(tmp_path, monkeypatch):
    limiter = make_limiter(tmp_path, Clock())

    @contextmanager
    def fail_lock():
        raise OSError("lock unavailable")
        yield

    monkeypatch.setattr(limiter, "_state_file_lock", fail_lock)
    assert limiter.set_metadata("device_identity", {"serial": "radio-a"}) is False
    reservation = limiter.reserve("usb_reset")
    assert reservation.allowed is False
    assert reservation.reason == "persistence_error"
    assert limiter.total_attempts == 0


@pytest.mark.parametrize("method", [None, False, "", "x" * 65])
def test_reservation_method_validation(tmp_path, method):
    limiter = make_limiter(tmp_path, Clock())
    with pytest.raises(ValueError, match="method must contain"):
        limiter.reserve(method)


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_safe_clock_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="wall clock"):
        PersistentResetLimiter._safe_clock(lambda: value, "wall")


def test_quarantine_helpers_fail_closed_and_expire_clock_uncertainty(tmp_path):
    clock = Clock()
    limiter = make_limiter(tmp_path, clock)
    with pytest.raises(ValueError, match="unsupported quarantine"):
        limiter._fail_closed_locked("invented")

    future_other_boot = {
        "reason": "clock_uncertain",
        "wall_time": clock.wall + 1000,
        "monotonic_time": 1,
        "boot_id": "boot-b",
    }
    assert limiter._quarantine_remaining_locked(future_other_boot) == limiter.window_seconds

    limiter._quarantine = {
        "reason": "clock_uncertain",
        "wall_time": clock.wall,
        "monotonic_time": clock.mono,
        "boot_id": "boot-a",
    }
    limiter._state_error = "clock_uncertain"
    limiter._blocked_until_monotonic = clock.mono
    limiter._expire_quarantine_locked(clock.mono + 1)
    assert limiter._quarantine is None
    assert limiter._future_records_may_be_discarded is True


def test_constructor_lock_failure_enters_persistence_quarantine(tmp_path, monkeypatch):
    @contextmanager
    def fail_lock(_self):
        raise OSError("lock unavailable")
        yield

    monkeypatch.setattr(PersistentResetLimiter, "_state_file_lock", fail_lock)
    limiter = make_limiter(tmp_path, Clock())
    assert limiter.state_error == "persistence_error"
    assert limiter.reserve("soft_reboot").reason == "persistence_error"


def test_missing_disk_state_preserves_stricter_local_quarantine(tmp_path):
    limiter = make_limiter(tmp_path, Clock())
    limiter._fail_closed_locked("invalid_state")

    with limiter._state_file_lock():
        assert limiter._reload_for_transaction_locked() is True
    assert limiter.state_error == "invalid_state"


def test_persist_quarantine_failure_remains_fail_closed(tmp_path, monkeypatch):
    limiter = make_limiter(tmp_path, Clock())

    def fail_save():
        raise OSError("disk full")

    monkeypatch.setattr(limiter, "_save_locked", fail_save)
    assert limiter._persist_quarantine_locked() is False
    assert limiter.state_error == "persistence_error"


def _valid_payload():
    return {
        "schema": 1,
        "records": [
            {
                "id": "attempt-1",
                "method": "soft_reboot",
                "boot_id": "boot-a",
                "wall_time": 1.0,
                "monotonic_time": 2.0,
            }
        ],
        "total_attempts": 1,
        "metadata": {},
        "quarantine": {
            "reason": "clock_uncertain",
            "boot_id": "boot-a",
            "wall_time": 1.0,
            "monotonic_time": 2.0,
        },
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(schema=2),
        lambda payload: payload.update(records="bad"),
        lambda payload: payload.update(total_attempts=False),
        lambda payload: payload.update(metadata=[]),
        lambda payload: payload.update(quarantine=[]),
        lambda payload: payload["quarantine"].update(reason="invented"),
        lambda payload: payload["quarantine"].update(boot_id=""),
        lambda payload: payload.update(records=["bad"]),
        lambda payload: payload["records"][0].update(id=""),
        lambda payload: payload["records"][0].update(method=""),
        lambda payload: payload["records"][0].update(boot_id=""),
    ],
)
def test_payload_validator_rejects_each_malformed_structure(mutate):
    payload = _valid_payload()
    mutate(payload)
    with pytest.raises(ValueError, match="reset|quarantine"):
        PersistentResetLimiter._validate_payload(payload)


@pytest.mark.parametrize("value", [None, "1", True, -1, float("nan"), float("inf")])
def test_timestamp_validator_rejects_non_numeric_or_unsafe_values(value):
    with pytest.raises(ValueError, match="invalid test time"):
        PersistentResetLimiter._validate_timestamp(value, "test time")


def test_save_rejects_oversized_payload_and_cleans_temporary_file(tmp_path, monkeypatch):
    limiter = make_limiter(tmp_path, Clock())
    limiter._metadata = {"oversized": "x" * (65 * 1024)}
    with pytest.raises(ValueError, match="payload exceeds"):
        limiter._save_locked()

    limiter._metadata = {}

    def fail_replace(_source, _destination):
        raise OSError("replace failed")

    monkeypatch.setattr(radio_recovery.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        limiter._save_locked()
    assert list(tmp_path.glob(".firmware-watchdog-*.tmp")) == []


def test_save_preserves_original_error_when_temporary_cleanup_fails(tmp_path, monkeypatch):
    limiter = make_limiter(tmp_path, Clock())

    monkeypatch.setattr(
        radio_recovery.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )
    monkeypatch.setattr(
        radio_recovery.os,
        "unlink",
        lambda *_args: (_ for _ in ()).throw(OSError("unlink failed")),
    )
    with pytest.raises(OSError, match="replace failed"):
        limiter._save_locked()
