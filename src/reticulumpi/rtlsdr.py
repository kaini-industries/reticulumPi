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
from dataclasses import dataclass
from typing import Any, Literal

log = logging.getLogger("reticulumpi.rtlsdr")

_DEVICE_RE = re.compile(r"^\s*(\d+):\s+.*SN:\s*(\S+)")
_FOUND_RE = re.compile(r"^Found\s+(\d+)\s+device")

_cache: list[tuple[int, str]] | None = None
_cache_time: float = 0.0
_CACHE_TTL: float = 300.0
_cache_lock = threading.Lock()
_claim_lock = threading.Lock()
_claimed: dict[str, str] = {}

DeviceSelector = Literal["auto", "serial", "index"]
ConfigDeviceSelector = Literal["serial", "index"]


@dataclass(frozen=True)
class DeviceLease:
    """Canonical ownership token for one physical RTL-SDR device."""

    configured: str
    canonical_id: str
    index: int
    caller: str

    def release(self) -> None:
        _release_canonical(self.canonical_id, self.configured, self.caller)


def _cleanup_claims() -> None:
    with _claim_lock:
        _claimed.clear()


def get_lease_metrics() -> dict[str, int]:
    """Return a secret-free snapshot of canonical device claims."""

    with _claim_lock:
        return {"canonical_claims": len(_claimed)}


atexit.register(_cleanup_claims)


def _run_rtl_test(rtl_test_path: str) -> list[tuple[int, str]]:
    """Run rtl_test, capture the device listing, kill before it opens a device.

    rtl_test prints the device listing to stderr immediately, then tries
    to open device 0 for testing (which hangs if the device is in use).
    We stream stderr line-by-line, capture the device entries, and kill
    the process once we've seen all expected devices or a blank line
    after the listing.
    """
    proc = subprocess.Popen(
        [rtl_test_path],
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


def enumerate_devices() -> list[tuple[int, str]]:
    """Run ``rtl_test`` and return ``[(index, serial), ...]``.

    Results are cached for up to ``_CACHE_TTL`` seconds (default 5 min).
    Returns an empty list if ``rtl_test`` is not installed or fails.
    """
    global _cache, _cache_time
    with _cache_lock:
        if _cache is not None and (time.monotonic() - _cache_time) < _CACHE_TTL:
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
    if selector not in ("auto", "serial", "index"):
        raise ValueError(f"Unknown RTL-SDR device selector: {selector!r}")

    devices = enumerate_devices()

    if selector != "index":
        for idx, serial in devices:
            if serial == configured:
                _claim_device(f"serial:{serial}", configured, caller)
                if str(idx) != configured:
                    log.info(
                        "Resolved RTL-SDR serial '%s' → index %d (caller: %s)",
                        serial,
                        idx,
                        caller or "?",
                    )
                return idx

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
        _claim_device(canonical, configured, caller)
        return idx

    available = ", ".join(f"{i}: SN {s}" for i, s in devices)
    raise RuntimeError(f"RTL-SDR device '{configured}' not found. Available: [{available}]")


def _claim_device(canonical: str, configured: str, caller: str) -> None:
    if not caller:
        return
    with _claim_lock:
        previous = _claimed.get(canonical)
        if previous and previous != caller:
            raise RuntimeError(
                f"RTL-SDR device '{configured}' is already claimed by '{previous}' — "
                "only one plugin can use a device at a time"
            )
        _claimed[canonical] = caller


def _canonical_device(configured: str, selector: DeviceSelector = "auto") -> str:
    devices = enumerate_devices()
    if selector != "index":
        for _index, serial in devices:
            if configured == serial:
                return f"serial:{serial}"

    if selector != "serial":
        try:
            configured_index = int(configured)
        except ValueError:
            configured_index = None
        if configured_index is not None:
            for index, serial in devices:
                if configured_index == index and (selector == "index" or configured == str(index)):
                    return f"serial:{serial}"
            return f"index:{configured_index}"

    if selector == "serial":
        return f"serial:{configured}"

    try:
        return f"index:{int(configured)}"
    except ValueError:
        return f"serial:{configured}"


def claim_device(
    configured: str,
    caller: str,
    *,
    selector: DeviceSelector = "auto",
) -> DeviceLease:
    """Resolve and claim a device, returning an explicit release token."""

    index = resolve_device(configured, caller=caller, selector=selector)
    return DeviceLease(configured, _canonical_device(configured, selector), index, caller)


def refresh_device_lease(
    lease: DeviceLease | None,
    configured: str,
    caller: str,
    *,
    selector: DeviceSelector = "auto",
) -> DeviceLease:
    """Resolve a possibly re-enumerated device and retain one exact claim."""

    index = resolve_device(configured, caller=caller, selector=selector)
    canonical = _canonical_device(configured, selector)
    refreshed = DeviceLease(configured, canonical, index, caller)
    if lease is not None and lease.canonical_id != canonical:
        _release_canonical(lease.canonical_id, lease.configured, lease.caller)
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
    canonical = _canonical_device(configured, selector)
    _release_canonical(canonical, configured, caller)


def _release_canonical(canonical: str, configured: str, caller: str = "") -> None:
    """Release an exact canonical claim even after USB enumeration changes."""

    with _claim_lock:
        current = _claimed.get(canonical)
        if current == caller or not caller:
            _claimed.pop(canonical, None)
            log.debug("Released RTL-SDR device '%s' (caller: %s)", configured, caller or "?")


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
