"""Shared RTL-SDR device enumeration and serial → index resolution."""

from __future__ import annotations

import atexit
import logging
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

log = logging.getLogger("reticulumpi.rtlsdr")

_DEVICE_RE = re.compile(r"^\s*(\d+):\s+.*SN:\s*(\S+)")
_FOUND_RE = re.compile(r"^Found\s+(\d+)\s+device")

_cache: list[tuple[int, str]] | None = None
_cache_time: float = 0.0
_CACHE_TTL: float = 300.0
_cache_lock = threading.Lock()
_claim_lock = threading.Lock()
_claimed: dict[int, _ClaimRecord] = {}
_claim_selections: dict[tuple[str, str, str], int] = {}
_next_claim_id = 0

# rtl_test parses the leading ``-d`` text as an index without checking where
# numeric parsing stopped, then also tries exact/prefix/suffix serial matching.
# Start out of index range and exceed its 256-byte USB serial buffer so none of
# those modes can select production hardware.
_INVENTORY_DEVICE_SELECTOR = "2147483647-" + ("x" * 256)

DeviceSelector = Literal["auto", "serial", "index"]
ConfigDeviceSelector = Literal["serial", "index"]


@dataclass(frozen=True)
class _ClaimRecord:
    """One exact selection claim, versioned for stale-lease-safe release."""

    claim_id: int
    caller: str
    selector: DeviceSelector
    configured: str
    canonical_id: str
    index: int

    @property
    def selection(self) -> tuple[str, str, str]:
        return (self.caller, self.selector, self.configured)


@dataclass(frozen=True)
class DeviceLease:
    """Canonical ownership token for one physical RTL-SDR device."""

    configured: str
    canonical_id: str
    index: int
    caller: str
    selector: DeviceSelector = "auto"
    _claim_id: int = field(default=0, repr=False, compare=False)

    def release(self) -> None:
        if self._claim_id:
            _release_claim_id(self._claim_id, self.configured, self.caller)
        else:
            _release_legacy_lease(self)


@dataclass(frozen=True)
class ResolvedDevice:
    """One device resolution derived from one enumeration snapshot."""

    index: int
    canonical_id: str


class DeviceBusyError(RuntimeError):
    """A claim conflict that retains the authoritative immutable resolution."""

    __slots__ = ("_resolved",)

    def __init__(self, configured: str, previous: str, resolved: ResolvedDevice) -> None:
        super().__init__(
            f"RTL-SDR device '{configured}' is already claimed by '{previous}' — "
            "only one plugin can use a device at a time"
        )
        self._resolved = resolved

    @property
    def resolved(self) -> ResolvedDevice:
        return self._resolved

    @property
    def canonical_id(self) -> str:
        return self._resolved.canonical_id

    @property
    def index(self) -> int:
        return self._resolved.index


def _cleanup_claims() -> None:
    with _claim_lock:
        _claimed.clear()
        _claim_selections.clear()


def get_lease_metrics() -> dict[str, int]:
    """Return a secret-free snapshot of canonical device claims."""

    with _claim_lock:
        return {"canonical_claims": len(_claimed)}


atexit.register(_cleanup_claims)


def _run_rtl_test(rtl_test_path: str) -> list[tuple[int, str]]:
    """Run rtl_test with an invalid selector and capture the device listing.

    rtl_test prints inventory before trying to open its selected device.  A
    collision-proof overlength selector prevents that probe from ever opening
    production hardware.  We still terminate promptly after reading the
    complete listing.
    """
    proc = subprocess.Popen(
        [rtl_test_path, "-d", _INVENTORY_DEVICE_SELECTOR],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    watchdog = threading.Timer(5.0, lambda: proc.kill())
    watchdog.daemon = True
    watchdog.start()
    try:
        devices: list[tuple[int, str]] = []
        expected = -1
        for line in proc.stderr:
            line = line.rstrip()
            if expected < 0:
                fm = _FOUND_RE.match(line)
                if fm:
                    expected = int(fm.group(1))
                    continue
            m = _DEVICE_RE.match(line)
            if m:
                devices.append((int(m.group(1)), m.group(2)))
                if len(devices) >= expected > 0:
                    break
                continue
            if expected >= 0 and not line.strip():
                break
        return devices
    finally:
        watchdog.cancel()
        proc.kill()
        proc.wait(timeout=5)


def enumerate_devices(*, force_refresh: bool = False) -> list[tuple[int, str]]:
    """Run ``rtl_test`` and return ``[(index, serial), ...]``.

    Results are cached for up to ``_CACHE_TTL`` seconds (default 5 min).
    ``force_refresh=True`` atomically bypasses that cache for ownership-changing
    paths while still publishing the fresh result to ordinary readers.
    Returns an empty list if ``rtl_test`` is not installed or fails.
    """
    global _cache, _cache_time
    with _cache_lock:
        if (
            not force_refresh
            and _cache is not None
            and (time.monotonic() - _cache_time) < _CACHE_TTL
        ):
            return list(_cache)

        rtl_test_path = shutil.which("rtl_test")
        if not rtl_test_path:
            log.warning("rtl_test not found; serial-based device resolution unavailable")
            _cache = []
            _cache_time = time.monotonic()
            return []

        try:
            devices = _run_rtl_test(rtl_test_path)
        except Exception as exc:
            log.warning("rtl_test failed: %s", exc)
            _cache = []
            _cache_time = time.monotonic()
            return []

        _cache = list(devices)
        _cache_time = time.monotonic()
        return devices


def configured_device(
    config: Mapping[str, Any],
    *,
    default_index: str = "0",
) -> tuple[str, ConfigDeviceSelector]:
    """Return a configured device value together with its explicit selector type.

    ``device_serial`` retains precedence when it is non-empty.  Values read from
    ``device_index`` remain indexes even when their string representation happens
    to look like an eight-digit RTL-SDR serial (for example ``"00000001"``).
    """

    serial = config.get("device_serial")
    if serial:
        return str(serial), "serial"
    return str(config.get("device_index", default_index)), "index"


def _resolve_device_snapshot(
    configured: str,
    selector: DeviceSelector,
    devices: list[tuple[int, str]],
) -> ResolvedDevice:
    """Resolve against an immutable caller-provided enumeration snapshot."""

    if selector not in ("auto", "serial", "index"):
        raise ValueError(f"Unknown RTL-SDR device selector: {selector!r}")

    if selector != "index":
        for idx, serial in devices:
            if serial == configured:
                return ResolvedDevice(idx, f"serial:{serial}")

        if selector == "serial":
            available = ", ".join(f"{i}: SN {s}" for i, s in devices)
            raise RuntimeError(f"RTL-SDR serial '{configured}' not found. Available: [{available}]")

    # Auto mode only falls back to a numeric index when the value does not look
    # like a serial number.  Explicit index mode intentionally bypasses both
    # serial matching and this ambiguity guard.
    known_serials = {s for _, s in devices}
    try:
        idx = int(configured)
    except ValueError:
        idx = None

    if (
        idx is not None
        and idx >= 0
        and (selector == "index" or configured not in known_serials)
        and (selector == "index" or len(configured) != 8 or not devices)
    ):
        serial_for_index = next((serial for index, serial in devices if index == idx), None)
        canonical = f"serial:{serial_for_index}" if serial_for_index else f"index:{idx}"
        return ResolvedDevice(idx, canonical)

    available = ", ".join(f"{i}: SN {s}" for i, s in devices)
    raise RuntimeError(f"RTL-SDR device '{configured}' not found. Available: [{available}]")


def resolve_device_identity(
    configured: str,
    *,
    selector: DeviceSelector = "auto",
    force_refresh: bool = False,
) -> ResolvedDevice:
    """Resolve a device index and canonical identity from one enumeration."""

    return _resolve_device_snapshot(
        configured,
        selector,
        enumerate_devices(force_refresh=force_refresh),
    )


def resolve_device(
    configured: str,
    caller: str = "",
    *,
    selector: DeviceSelector = "auto",
) -> int:
    """Resolve a serial string or numeric index to a device index.

    ``selector="serial"`` requires an exact serial match and never falls back to
    an integer index.  ``selector="index"`` always interprets *configured* as an
    integer index, even when it contains eight decimal digits.  The default
    ``"auto"`` mode preserves the legacy serial-first API behavior.
    """

    if caller:
        resolved = resolve_device_identity(
            configured,
            selector=selector,
            force_refresh=True,
        )
    else:
        resolved = resolve_device_identity(configured, selector=selector)
    _claim_device(resolved, configured, caller, selector)
    if (
        selector != "index"
        and resolved.canonical_id == f"serial:{configured}"
        and str(resolved.index) != configured
    ):
        log.info(
            "Resolved RTL-SDR serial '%s' → index %d (caller: %s)",
            configured,
            resolved.index,
            caller or "?",
        )
    return resolved.index


def _claim_device(
    resolved: ResolvedDevice,
    configured: str,
    caller: str,
    selector: DeviceSelector,
    *,
    replace_claim_id: int | None = None,
) -> int | None:
    """Atomically establish or refresh one exact selection claim."""

    if not caller:
        return None

    global _next_claim_id
    selection = (caller, selector, configured)
    with _claim_lock:
        own_claim_id = _claim_selections.get(selection)
        if own_claim_id is not None and own_claim_id not in _claimed:
            _claim_selections.pop(selection, None)
            own_claim_id = None

        if replace_claim_id is not None:
            replacement = _claimed.get(replace_claim_id)
            if replacement is None:
                raise RuntimeError("Cannot refresh a stale RTL-SDR device lease")
            if replacement.caller != caller:
                raise RuntimeError("Cannot refresh an RTL-SDR device lease owned by another caller")
            if own_claim_id is not None and own_claim_id != replace_claim_id:
                raise RuntimeError("Target RTL-SDR selection already has a different active lease")

        replaced_claim_ids = {
            claim_id for claim_id in (own_claim_id, replace_claim_id) if claim_id is not None
        }
        for claim_id, record in _claimed.items():
            if claim_id in replaced_claim_ids:
                continue
            if record.canonical_id == resolved.canonical_id or record.index == resolved.index:
                raise DeviceBusyError(configured, record.caller, resolved)

        _next_claim_id += 1
        claim_id = _next_claim_id
        record = _ClaimRecord(
            claim_id=claim_id,
            caller=caller,
            selector=selector,
            configured=configured,
            canonical_id=resolved.canonical_id,
            index=resolved.index,
        )
        _claimed[claim_id] = record
        _claim_selections[selection] = claim_id
        for replaced_claim_id in replaced_claim_ids:
            _pop_claim_locked(replaced_claim_id)
        return claim_id


def claim_device(
    configured: str,
    caller: str,
    *,
    selector: DeviceSelector = "auto",
) -> DeviceLease:
    """Freshly resolve and claim a device, returning an exact release token."""

    resolved = resolve_device_identity(
        configured,
        selector=selector,
        force_refresh=True,
    )
    claim_id = _claim_device(resolved, configured, caller, selector)
    return DeviceLease(
        configured,
        resolved.canonical_id,
        resolved.index,
        caller,
        selector,
        claim_id or 0,
    )


def refresh_device_lease(
    lease: DeviceLease | None,
    configured: str,
    caller: str,
    *,
    selector: DeviceSelector = "auto",
) -> DeviceLease:
    """Freshly resolve a possibly re-enumerated exact selection claim."""

    resolved = resolve_device_identity(
        configured,
        selector=selector,
        force_refresh=True,
    )
    claim_id = _claim_device(
        resolved,
        configured,
        caller,
        selector,
        replace_claim_id=lease._claim_id if lease is not None and lease._claim_id else None,
    )
    refreshed = DeviceLease(
        configured,
        resolved.canonical_id,
        resolved.index,
        caller,
        selector,
        claim_id or 0,
    )
    return refreshed


def release_device(
    configured: str,
    caller: str = "",
    *,
    selector: DeviceSelector = "auto",
) -> None:
    """Release a previously claimed device serial.

    Called during plugin stop() to allow another plugin to claim the device.
    """
    selection_claim_ids: list[int] = []
    if caller:
        with _claim_lock:
            claim_id = _claim_selections.get((caller, selector, configured))
            if claim_id is not None:
                selection_claim_ids.append(claim_id)
    else:
        # Callerless release is the public force-release API.  Prefer recorded
        # selections so a changed inventory cannot make the original claim
        # unresolvable (notably auto-mode eight-digit numeric indexes).
        with _claim_lock:
            selection_claim_ids = [
                claim_id
                for (record_caller, record_selector, record_configured), claim_id in (
                    _claim_selections.items()
                )
                if record_selector == selector and record_configured == configured
            ]

    if selection_claim_ids:
        for claim_id in selection_claim_ids:
            _release_claim_id(claim_id, configured, caller)
        return
    if caller:
        return

    try:
        resolved = resolve_device_identity(configured, selector=selector)
    except RuntimeError:
        if selector == "index":
            try:
                resolved = ResolvedDevice(int(configured), f"index:{int(configured)}")
            except ValueError:
                return
        else:
            return
    _force_release_resolved(resolved, configured)


def _pop_claim_locked(claim_id: int) -> _ClaimRecord | None:
    record = _claimed.pop(claim_id, None)
    if record is not None and _claim_selections.get(record.selection) == claim_id:
        _claim_selections.pop(record.selection, None)
    return record


def _release_claim_id(claim_id: int, configured: str, caller: str = "") -> None:
    """Release one versioned claim; stale lease tokens are harmless."""

    released = None
    with _claim_lock:
        record = _claimed.get(claim_id)
        if record is not None and (not caller or record.caller == caller):
            released = _pop_claim_locked(claim_id)
    if released is not None:
        log.debug("Released RTL-SDR device '%s' (caller: %s)", configured, caller or "?")


def _release_legacy_lease(lease: DeviceLease) -> None:
    """Best-effort compatibility for manually constructed, unversioned leases."""

    released = None
    with _claim_lock:
        claim_id = _claim_selections.get((lease.caller, lease.selector, lease.configured))
        record = _claimed.get(claim_id) if claim_id is not None else None
        if (
            record is not None
            and record.canonical_id == lease.canonical_id
            and record.index == lease.index
        ):
            released = _pop_claim_locked(record.claim_id)
    if released is not None:
        log.debug("Released RTL-SDR device '%s' (caller: %s)", lease.configured, lease.caller)


def _force_release_resolved(resolved: ResolvedDevice, configured: str) -> None:
    """Force-release claims colliding with one currently resolved identity."""

    with _claim_lock:
        claim_ids = [
            claim_id
            for claim_id, record in _claimed.items()
            if record.canonical_id == resolved.canonical_id or record.index == resolved.index
        ]
        for claim_id in claim_ids:
            _pop_claim_locked(claim_id)
    if claim_ids:
        log.debug("Force-released RTL-SDR device '%s'", configured)


def invalidate_cache() -> None:
    """Clear cached device enumeration so the next resolve re-enumerates USB.

    Unlike ``reset_cache``, this preserves device claims.  Use when a
    dongle may have dropped off USB and re-enumerated at a different
    index.
    """
    global _cache, _cache_time
    with _cache_lock:
        _cache = None
        _cache_time = 0.0


def reset_cache() -> None:
    """Clear cached enumeration and claims (for testing)."""
    global _cache, _cache_time
    with _cache_lock:
        _cache = None
        _cache_time = 0.0
    with _claim_lock:
        _claimed.clear()
        _claim_selections.clear()
