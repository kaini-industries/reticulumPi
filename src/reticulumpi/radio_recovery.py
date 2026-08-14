"""Durable safety controls for hardware-radio recovery actions.

Radio reset limits must survive both service restarts and host reboots.  This
module deliberately records *attempts* before reset-capable I/O: a failing
device must not be able to evade its circuit breaker by crashing or restarting
the supervising process.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

_SCHEMA_VERSION = 1
_MAX_STATE_BYTES = 64 * 1024
_MAX_RECORDS = 256
_CLOCK_SKEW_TOLERANCE = 300.0
_QUARANTINE_REASONS = frozenset({"clock_uncertain", "invalid_state", "persistence_error"})


def _read_boot_id() -> str:
    """Return the Linux boot ID, with a conservative portable fallback."""

    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as handle:
            value = handle.read(128).strip()
        if value:
            return value
    except OSError:
        pass
    return "unknown-boot"


@dataclass(frozen=True)
class ResetReservation:
    """Result of atomically reserving one reset-rate slot."""

    allowed: bool
    reason: str
    reservation_id: str | None = None
    recent_attempts: int = 0


class PersistentResetLimiter:
    """Persist and enforce a rolling reset-attempt limit.

    Same-boot age is calculated from ``CLOCK_MONOTONIC`` so NTP corrections do
    not clear the limiter.  Across boots, wall time is used.  If a rebooted
    system's wall clock is clearly behind stored records, reset actions fail
    closed for one limiter window instead of silently forgetting history.
    """

    def __init__(
        self,
        path: str,
        max_attempts: int,
        *,
        window_seconds: float = 3600.0,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        boot_id: str | None = None,
    ) -> None:
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or not 0 <= max_attempts <= _MAX_RECORDS
        ):
            raise ValueError(f"max_attempts must be an integer between 0 and {_MAX_RECORDS}")
        if (
            not isinstance(window_seconds, (int, float))
            or isinstance(window_seconds, bool)
            or not math.isfinite(float(window_seconds))
            or window_seconds <= 0
        ):
            raise ValueError("window_seconds must be positive")
        self.path = os.path.abspath(os.path.expanduser(path))
        self._lock_path = f"{self.path}.lock"
        self.max_attempts = max_attempts
        self.window_seconds = float(window_seconds)
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._boot_id = boot_id or _read_boot_id()
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []
        self._metadata: dict[str, Any] = {}
        self._total_attempts = 0
        self._state_error: str | None = None
        self._blocked_until_monotonic = 0.0
        self._future_records_may_be_discarded = False
        self._quarantine: dict[str, Any] | None = None
        self._load()

    @property
    def state_error(self) -> str | None:
        with self._lock:
            return self._state_error

    @property
    def total_attempts(self) -> int:
        with self._lock:
            return self._total_attempts

    def metadata(self) -> dict[str, Any]:
        """Return a detached copy of persisted limiter metadata."""

        with self._lock:
            return json.loads(json.dumps(self._metadata))

    def set_metadata(self, key: str, value: Mapping[str, Any] | None) -> bool:
        """Atomically persist small JSON metadata alongside reset history."""

        if not key or len(key) > 64:
            raise ValueError("metadata key must contain 1-64 characters")
        detached_value: dict[str, Any] | None = None
        if value is not None:
            # JSON round-trip both validates and detaches caller-owned data.
            encoded = json.dumps(dict(value), sort_keys=True)
            if len(encoded.encode("utf-8")) > 8192:
                raise ValueError("metadata value is too large")
            detached_value = json.loads(encoded)

        with self._lock:
            try:
                with self._state_file_lock():
                    self._reload_for_transaction_locked()
                    self._expire_quarantine_locked()
                    before = dict(self._metadata)
                    if detached_value is None:
                        self._metadata.pop(key, None)
                    else:
                        self._metadata[key] = detached_value
                    try:
                        self._save_locked()
                    except (OSError, OverflowError, TypeError, ValueError):
                        self._metadata = before
                        self._fail_closed_locked("persistence_error")
                        return False
            except (OSError, OverflowError, TypeError, ValueError):
                self._fail_closed_locked("persistence_error")
                return False
            return True

    def recent_attempts(self) -> int:
        """Return reset attempts inside the active rolling window."""

        with self._lock:
            recent, _ = self._recent_locked()
            return len(recent)

    def reserve(self, method: str) -> ResetReservation:
        """Reserve and durably persist one reset attempt before hardware I/O."""

        if not isinstance(method, str) or not method or len(method) > 64:
            raise ValueError("method must contain 1-64 characters")

        with self._lock:
            try:
                with self._state_file_lock():
                    self._reload_for_transaction_locked()
                    now_mono = self._safe_clock(self._monotonic_clock, "monotonic")
                    self._expire_quarantine_locked(now_mono)
                    if now_mono < self._blocked_until_monotonic:
                        self._persist_quarantine_locked()
                        recent, _ = self._recent_locked(ignore_clock_uncertainty=True)
                        return ResetReservation(
                            False,
                            self._state_error or "clock_uncertain",
                            recent_attempts=len(recent),
                        )

                    recent, clock_uncertain = self._recent_locked()
                    if clock_uncertain:
                        self._fail_closed_locked("clock_uncertain")
                        self._persist_quarantine_locked()
                        return ResetReservation(
                            False,
                            "clock_uncertain",
                            recent_attempts=len(recent),
                        )
                    if self.max_attempts > 0 and len(recent) >= self.max_attempts:
                        return ResetReservation(
                            False,
                            "rate_limited",
                            recent_attempts=len(recent),
                        )

                    wall = self._safe_clock(self._wall_clock, "wall")
                    mono = self._safe_clock(self._monotonic_clock, "monotonic")
                    reservation_id = uuid.uuid4().hex
                    record = {
                        "id": reservation_id,
                        "wall_time": wall,
                        "monotonic_time": mono,
                        "boot_id": self._boot_id,
                        "method": method,
                    }
                    previous_records = self._records
                    previous_total = self._total_attempts
                    self._records = (recent + [record])[-_MAX_RECORDS:]
                    self._total_attempts += 1
                    self._clear_quarantine_locked()
                    try:
                        self._save_locked()
                    except (OSError, OverflowError, TypeError, ValueError):
                        self._records = previous_records
                        self._total_attempts = previous_total
                        self._fail_closed_locked("persistence_error")
                        return ResetReservation(
                            False,
                            "persistence_error",
                            recent_attempts=len(recent),
                        )
            except (OSError, OverflowError, TypeError, ValueError):
                self._fail_closed_locked("persistence_error")
                return ResetReservation(
                    False,
                    "persistence_error",
                    recent_attempts=0,
                )

            return ResetReservation(
                True,
                "reserved",
                reservation_id=reservation_id,
                recent_attempts=len(recent) + 1,
            )

    def status(self) -> dict[str, Any]:
        """Return JSON-compatible limiter status for monitoring."""

        with self._lock:
            recent, uncertain = self._recent_locked(ignore_clock_uncertainty=True)
            now_mono = self._safe_clock(self._monotonic_clock, "monotonic")
            return {
                "max_attempts": self.max_attempts,
                "window_seconds": int(self.window_seconds),
                "recent_attempts": len(recent),
                "total_attempts": self._total_attempts,
                "state_error": self._state_error,
                "clock_uncertain": uncertain or now_mono < self._blocked_until_monotonic,
                "blocked_seconds": max(0, int(self._blocked_until_monotonic - now_mono)),
            }

    @staticmethod
    def _safe_clock(clock: Callable[[], float], label: str) -> float:
        value = float(clock())
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{label} clock returned an invalid value")
        return value

    def _fail_closed_locked(self, reason: str) -> None:
        if reason not in _QUARANTINE_REASONS:
            raise ValueError(f"unsupported quarantine reason: {reason}")
        wall = self._safe_clock(self._wall_clock, "wall")
        self._state_error = reason
        now = self._safe_clock(self._monotonic_clock, "monotonic")
        blocked_until = now + self.window_seconds
        if blocked_until >= self._blocked_until_monotonic:
            self._blocked_until_monotonic = blocked_until
            self._quarantine = {
                "reason": reason,
                "wall_time": wall,
                "monotonic_time": now,
                "boot_id": self._boot_id,
            }

    def _clear_quarantine_locked(self) -> None:
        self._state_error = None
        self._blocked_until_monotonic = 0.0
        self._quarantine = None

    def _quarantine_remaining_locked(self, quarantine: Mapping[str, Any]) -> float:
        now_wall = self._safe_clock(self._wall_clock, "wall")
        now_mono = self._safe_clock(self._monotonic_clock, "monotonic")
        if quarantine["boot_id"] == self._boot_id:
            age = now_mono - float(quarantine["monotonic_time"])
        else:
            age = now_wall - float(quarantine["wall_time"])
        if age < -_CLOCK_SKEW_TOLERANCE:
            return self.window_seconds
        return max(0.0, self.window_seconds - max(0.0, age))

    def _apply_quarantine_locked(self, quarantine: Mapping[str, Any]) -> float:
        remaining = self._quarantine_remaining_locked(quarantine)
        if remaining <= 0:
            if quarantine["reason"] == "clock_uncertain":
                self._future_records_may_be_discarded = True
            return 0.0
        now_mono = self._safe_clock(self._monotonic_clock, "monotonic")
        self._quarantine = dict(quarantine)
        self._state_error = str(quarantine["reason"])
        self._blocked_until_monotonic = now_mono + remaining
        return remaining

    def _expire_quarantine_locked(self, now_mono: float | None = None) -> None:
        current = (
            self._safe_clock(self._monotonic_clock, "monotonic") if now_mono is None else now_mono
        )
        if self._quarantine is None or current < self._blocked_until_monotonic:
            return
        if self._state_error == "clock_uncertain":
            self._future_records_may_be_discarded = True
        self._clear_quarantine_locked()

    def _recent_locked(
        self,
        *,
        ignore_clock_uncertainty: bool = False,
    ) -> tuple[list[dict[str, Any]], bool]:
        now_wall = self._safe_clock(self._wall_clock, "wall")
        now_mono = self._safe_clock(self._monotonic_clock, "monotonic")
        self._expire_quarantine_locked(now_mono)
        recent: list[dict[str, Any]] = []
        uncertain = False

        for record in self._records:
            same_boot = record["boot_id"] == self._boot_id
            if same_boot:
                age = now_mono - record["monotonic_time"]
            else:
                age = now_wall - record["wall_time"]

            if age < -_CLOCK_SKEW_TOLERANCE:
                if self._future_records_may_be_discarded:
                    continue
                uncertain = True
                if not ignore_clock_uncertainty:
                    continue
            if -_CLOCK_SKEW_TOLERANCE <= age < self.window_seconds:
                recent.append(record)

        return recent, uncertain

    def _load(self) -> None:
        try:
            with self._state_file_lock():
                if self._reload_for_transaction_locked():
                    self._persist_quarantine_locked()
        except (OSError, OverflowError, TypeError, ValueError):
            self._reset_loaded_state_locked()
            self._fail_closed_locked("persistence_error")

    @contextmanager
    def _state_file_lock(self) -> Iterator[None]:
        """Hold the stable per-state lock inode across read-modify-replace."""

        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(self._lock_path, flags, 0o600)
        locked = False
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
            yield
        finally:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _reset_loaded_state_locked(self) -> None:
        self._records = []
        self._metadata = {}
        self._total_attempts = 0
        self._state_error = None
        self._blocked_until_monotonic = 0.0
        self._future_records_may_be_discarded = False
        self._quarantine = None

    def _reload_for_transaction_locked(self) -> bool:
        """Reload the latest state while preserving stricter local quarantine."""

        local_quarantine = dict(self._quarantine) if self._quarantine is not None else None
        local_remaining = (
            self._quarantine_remaining_locked(local_quarantine)
            if local_quarantine is not None
            else 0.0
        )
        local_future_discard = self._future_records_may_be_discarded
        self._reset_loaded_state_locked()

        if not os.path.exists(self.path):
            if local_quarantine is not None and local_remaining > 0:
                self._apply_quarantine_locked(local_quarantine)
                return True
            self._future_records_may_be_discarded = local_future_discard
            return False

        try:
            if os.path.getsize(self.path) > _MAX_STATE_BYTES:
                raise ValueError("state file exceeds size limit")
            with open(self.path, encoding="utf-8") as handle:
                payload = json.load(handle)
            self._validate_payload(payload)
        except (
            OSError,
            json.JSONDecodeError,
            KeyError,
            OverflowError,
            TypeError,
            ValueError,
        ):
            self._reset_loaded_state_locked()
            self._fail_closed_locked("invalid_state")
            return True

        self._records = payload["records"][-_MAX_RECORDS:]
        self._total_attempts = payload["total_attempts"]
        self._metadata = payload.get("metadata", {})
        quarantine = payload.get("quarantine")
        disk_remaining = 0.0
        if quarantine is not None:
            disk_remaining = self._apply_quarantine_locked(quarantine)
        self._future_records_may_be_discarded |= local_future_discard

        dirty = False
        if local_quarantine is not None and local_remaining > disk_remaining:
            self._apply_quarantine_locked(local_quarantine)
            dirty = True

        # Detect cross-boot wall-clock rollback immediately unless an existing
        # quarantine already provides an equal or stronger fail-closed gate.
        if self._quarantine is None:
            _, uncertain = self._recent_locked()
            if uncertain:
                self._fail_closed_locked("clock_uncertain")
                dirty = True
        return dirty

    def _persist_quarantine_locked(self) -> bool:
        """Persist current quarantine without authorizing an actuator."""

        try:
            self._save_locked()
        except (OSError, OverflowError, TypeError, ValueError):
            self._fail_closed_locked("persistence_error")
            return False
        return True

    @staticmethod
    def _validate_payload(payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("schema") != _SCHEMA_VERSION:
            raise ValueError("unsupported reset-state schema")
        records = payload.get("records")
        if not isinstance(records, list) or len(records) > _MAX_RECORDS:
            raise ValueError("invalid reset-state records")
        total = payload.get("total_attempts")
        if not isinstance(total, int) or isinstance(total, bool) or total < len(records):
            raise ValueError("invalid reset-state total")
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("invalid reset-state metadata")
        quarantine = payload.get("quarantine")
        if quarantine is not None:
            if not isinstance(quarantine, dict):
                raise ValueError("invalid reset-state quarantine")
            reason = quarantine.get("reason")
            if reason not in _QUARANTINE_REASONS:
                raise ValueError("invalid reset-state quarantine reason")
            boot_id = quarantine.get("boot_id")
            if not isinstance(boot_id, str) or not boot_id or len(boot_id) > 128:
                raise ValueError("invalid reset-state quarantine boot id")
            for key in ("wall_time", "monotonic_time"):
                PersistentResetLimiter._validate_timestamp(
                    quarantine.get(key),
                    f"quarantine {key}",
                )
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("invalid reset record")
            if not isinstance(record.get("id"), str) or not record["id"]:
                raise ValueError("invalid reset record id")
            if not isinstance(record.get("method"), str) or not record["method"]:
                raise ValueError("invalid reset record method")
            boot_id = record.get("boot_id")
            if not isinstance(boot_id, str) or not boot_id or len(boot_id) > 128:
                raise ValueError("invalid reset record boot id")
            for key in ("wall_time", "monotonic_time"):
                PersistentResetLimiter._validate_timestamp(
                    record.get(key),
                    f"reset record {key}",
                )

    @staticmethod
    def _validate_timestamp(value: Any, label: str) -> None:
        try:
            numeric = float(value)
        except (OverflowError, TypeError, ValueError):
            raise ValueError(f"invalid {label}") from None
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(numeric)
            or numeric < 0
        ):
            raise ValueError(f"invalid {label}")

    def _save_locked(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        payload = {
            "schema": _SCHEMA_VERSION,
            "records": self._records[-_MAX_RECORDS:],
            "total_attempts": self._total_attempts,
            "metadata": self._metadata,
            "quarantine": self._quarantine,
        }
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        if len(encoded) > _MAX_STATE_BYTES:
            raise ValueError("reset-state payload exceeds size limit")

        fd, temporary = tempfile.mkstemp(
            dir=directory,
            prefix=".firmware-watchdog-",
            suffix=".tmp",
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(directory, directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
