"""Shared RTL-SDR device enumeration and serial → index resolution."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading

log = logging.getLogger("reticulumpi.rtlsdr")

_DEVICE_RE = re.compile(r"^\s*(\d+):\s+.*SN:\s*(\S+)")

_cache: list[tuple[int, str]] | None = None
_cache_lock = threading.Lock()
_claimed: dict[str, str] = {}


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
            # No -t flag: just list devices without opening/testing tuners.
            # The -t flag runs a tuner sensitivity test that exclusively
            # claims every device for several seconds, racing with plugins
            # that are starting their subprocesses concurrently.
            result = subprocess.run(
                [rtl_test_path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            output = result.stdout + result.stderr
        except Exception as exc:
            log.warning("rtl_test failed: %s", exc)
            _cache = []
            return []

        devices: list[tuple[int, str]] = []
        for line in output.splitlines():
            m = _DEVICE_RE.match(line)
            if m:
                devices.append((int(m.group(1)), m.group(2)))

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
