"""Shared RTL-SDR device enumeration and serial → index resolution."""

from __future__ import annotations

import atexit
import logging
import re
import shutil
import subprocess
import threading
import time

log = logging.getLogger("reticulumpi.rtlsdr")

_DEVICE_RE = re.compile(r"^\s*(\d+):\s+.*SN:\s*(\S+)")
_FOUND_RE = re.compile(r"^Found\s+(\d+)\s+device")

_cache: list[tuple[int, str]] | None = None
_cache_time: float = 0.0
_CACHE_TTL: float = 300.0
_cache_lock = threading.Lock()
_claim_lock = threading.Lock()
_claimed: dict[str, str] = {}


def _cleanup_claims() -> None:
    with _claim_lock:
        _claimed.clear()


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


def resolve_device(configured: str, caller: str = "") -> int:
    """Resolve a config value (serial string or numeric index) to a device index.

    Tries serial match first against ``rtl_test`` output, then falls
    back to interpreting *configured* as a literal integer index.
    Raises ``RuntimeError`` if neither works and devices were enumerated.
    """
    devices = enumerate_devices()

    for idx, serial in devices:
        if serial == configured:
            with _claim_lock:
                prev = _claimed.get(serial)
                if prev and prev != caller and caller:
                    raise RuntimeError(
                        f"RTL-SDR serial '{serial}' is already claimed by '{prev}' — "
                        f"only one plugin can use a device at a time"
                    )
                if caller:
                    _claimed[serial] = caller
            if str(idx) != configured:
                log.info(
                    "Resolved RTL-SDR serial '%s' → index %d (caller: %s)",
                    serial,
                    idx,
                    caller or "?",
                )
            return idx

    # Only fall back to numeric index if the value doesn't look like
    # a serial number.  RTL-SDR serials are always 8 decimal digits;
    # any other length is treated as a device index.  This prevents
    # "00000001" from silently resolving to index 1 when the intended
    # dongle is absent.
    known_serials = {s for _, s in devices}
    try:
        idx = int(configured)
    except ValueError:
        idx = None

    if (
        idx is not None
        and idx >= 0
        and configured not in known_serials
        and (len(configured) != 8 or not devices)
    ):
        return idx

    available = ", ".join(f"{i}: SN {s}" for i, s in devices)
    raise RuntimeError(f"RTL-SDR device '{configured}' not found. Available: [{available}]")


def release_device(configured: str, caller: str = "") -> None:
    """Release a previously claimed device serial.

    Called during plugin stop() to allow another plugin to claim the device.
    """
    with _claim_lock:
        current = _claimed.get(configured)
        if current == caller or not caller:
            _claimed.pop(configured, None)
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
