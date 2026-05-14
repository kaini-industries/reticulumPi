"""Tests for the shared RTL-SDR device resolver."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from reticulumpi.rtlsdr import enumerate_devices, release_device, reset_cache, resolve_device

_RTL_TEST_OUTPUT = """\
Found 3 device(s):
  0:  RTLSDRBlog, Blog V4, SN: 00000001
  1:  Nooelec, SMArTee XTR v5ee, SN: 07143901
  2:  Nooelec, SMArTee XTR v5ee, SN: 14342860

Using device 0: Generic RTL2832U OEM
"""


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_cache()
    yield
    reset_cache()


def _mock_rtl_test(output: str = _RTL_TEST_OUTPUT, which: str = "/usr/bin/rtl_test"):
    """Patch shutil.which and _run_rtl_test to return canned device list."""
    from reticulumpi.rtlsdr import _DEVICE_RE
    devices = []
    for line in output.splitlines():
        m = _DEVICE_RE.match(line)
        if m:
            devices.append((int(m.group(1)), m.group(2)))
    return [
        patch("reticulumpi.rtlsdr.shutil.which", return_value=which),
        patch("reticulumpi.rtlsdr._run_rtl_test", return_value=devices),
    ]


class TestEnumerateDevices:
    def test_parses_three_devices(self):
        with _mock_rtl_test()[0], _mock_rtl_test()[1]:
            devices = enumerate_devices()
        assert devices == [(0, "00000001"), (1, "07143901"), (2, "14342860")]

    def test_empty_when_rtl_test_missing(self):
        with patch("reticulumpi.rtlsdr.shutil.which", return_value=None):
            assert enumerate_devices() == []

    def test_cache_reuses_result(self):
        patches = _mock_rtl_test()
        with patches[0], patches[1] as mock_run:
            enumerate_devices()
            enumerate_devices()
            assert mock_run.call_count == 1

    def test_empty_when_rtl_test_fails(self):
        with (
            patch("reticulumpi.rtlsdr.shutil.which", return_value="/usr/bin/rtl_test"),
            patch("reticulumpi.rtlsdr._run_rtl_test", side_effect=OSError("fail")),
        ):
            assert enumerate_devices() == []


class TestResolveDevice:
    def test_serial_match_returns_index(self):
        with _mock_rtl_test()[0], _mock_rtl_test()[1]:
            assert resolve_device("00000001") == 0
            reset_cache()
        with _mock_rtl_test()[0], _mock_rtl_test()[1]:
            assert resolve_device("07143901") == 1
            reset_cache()
        with _mock_rtl_test()[0], _mock_rtl_test()[1]:
            assert resolve_device("14342860") == 2

    def test_numeric_fallback(self):
        with _mock_rtl_test()[0], _mock_rtl_test()[1]:
            assert resolve_device("1") == 1

    def test_three_digit_numeric_index(self):
        with _mock_rtl_test()[0], _mock_rtl_test()[1]:
            assert resolve_device("100") == 100

    def test_eight_digit_unknown_serial_raises(self):
        with _mock_rtl_test()[0], _mock_rtl_test()[1]:
            with pytest.raises(RuntimeError, match="not found"):
                resolve_device("12345678")

    def test_eight_digit_fallback_no_devices(self):
        with patch("reticulumpi.rtlsdr.shutil.which", return_value=None):
            assert resolve_device("12345678") == 12345678

    def test_negative_index_raises(self):
        with _mock_rtl_test()[0], _mock_rtl_test()[1]:
            with pytest.raises(RuntimeError, match="not found"):
                resolve_device("-1")

    def test_numeric_fallback_no_devices(self):
        with patch("reticulumpi.rtlsdr.shutil.which", return_value=None):
            assert resolve_device("0") == 0

    def test_unknown_serial_raises(self):
        with _mock_rtl_test()[0], _mock_rtl_test()[1]:
            with pytest.raises(RuntimeError, match="not found"):
                resolve_device("NOSUCH")

    def test_non_numeric_non_serial_raises_no_devices(self):
        with patch("reticulumpi.rtlsdr.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="not found"):
                resolve_device("abc")

    def test_duplicate_serial_raises(self):
        patches = _mock_rtl_test()
        with patches[0], patches[1]:
            resolve_device("00000001", caller="adsb_radar")
            with pytest.raises(RuntimeError, match="already claimed by 'adsb_radar'"):
                resolve_device("00000001", caller="spectrum_scanner")

    def test_same_caller_no_error(self):
        patches = _mock_rtl_test()
        with patches[0], patches[1]:
            resolve_device("00000001", caller="adsb_radar")
            idx = resolve_device("00000001", caller="adsb_radar")
        assert idx == 0

    def test_release_then_reclaim(self):
        patches = _mock_rtl_test()
        with patches[0], patches[1]:
            resolve_device("00000001", caller="adsb_radar")
            release_device("00000001", caller="adsb_radar")
            idx = resolve_device("00000001", caller="spectrum_scanner")
        assert idx == 0

    def test_release_wrong_caller_is_noop(self):
        patches = _mock_rtl_test()
        with patches[0], patches[1]:
            resolve_device("00000001", caller="adsb_radar")
            release_device("00000001", caller="other_plugin")
            with pytest.raises(RuntimeError, match="already claimed"):
                resolve_device("00000001", caller="spectrum_scanner")
