"""Shared RTL-SDR device enumeration and serial → index resolution."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading

log = logging.getLogger("reticulumpi.rtlsdr")

_DEVICE_RE = re.compile(r"^\s*(\d+):\s+.*SN:\s*(\S+)")
_FOUND_RE = re.compile(r"^Found\s+(\d+)\s+device")

_cache: list[tuple[int, str]] | None = None
_cache_lock = threading.Lock()
_claimed: dict[str, str] = {}


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

    Results are cached for the lifetime of the process (USB devices
    don't change at runtime on this embedded system).  Returns an empty
    list if ``rtl_test`` is not installed or fails.
    """
    global _cache
    with _cache_lock:
        if _cache is not None:
            return list(_cache)

        rtl_test_path = shutil.which("rtl_test")
        if not rtl_test_path:
            log.warning("rtl_test not found; serial-based device resolution unavailable")
            _cache = []
            return []

        try:
            devices = _run_rtl_test(rtl_test_path)
        except Exception as exc:
            log.warning("rtl_test failed: %s", exc)
            _cache = []
            return []

        _cache = list(devices)
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
            prev = _claimed.get(serial)
            if prev and prev != caller and caller:
                log.warning(
                    "RTL-SDR serial '%s' claimed by both '%s' and '%s' — "
                    "only one can use it at a time",
                    serial, prev, caller,
                )
            if caller:
                _claimed[serial] = caller
            if str(idx) != configured:
                log.info("Resolved RTL-SDR serial '%s' → index %d (caller: %s)", serial, idx, caller or "?")
            return idx

    try:
        return int(configured)
    except ValueError:
        available = ", ".join(f"{i}: SN {s}" for i, s in devices)
        raise RuntimeError(
            f"RTL-SDR device '{configured}' not found. "
            f"Available: [{available}]"
        ) from None


def reset_cache() -> None:
    """Clear cached enumeration (for testing)."""
    global _cache
    with _cache_lock:
        _cache = None
    _claimed.clear()
