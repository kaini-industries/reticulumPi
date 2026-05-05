"""Tests for the shared RTL-SDR device resolver."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from reticulumpi.rtlsdr import enumerate_devices, reset_cache, resolve_device

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
    """Patch shutil.which and subprocess.run to return canned rtl_test output."""
    return [
        patch("reticulumpi.rtlsdr.shutil.which", return_value=which),
        patch(
            "reticulumpi.rtlsdr.subprocess.run",
            return_value=type("R", (), {"stdout": output, "stderr": ""})(),
        ),
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
            patch("reticulumpi.rtlsdr.subprocess.run", side_effect=OSError("fail")),
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

    def test_duplicate_serial_warning(self, caplog):
        patches = _mock_rtl_test()
        with patches[0], patches[1], caplog.at_level(logging.WARNING, logger="reticulumpi.rtlsdr"):
            resolve_device("00000001", caller="adsb_radar")
            resolve_device("00000001", caller="spectrum_scanner")
        assert "claimed by both" in caplog.text

    def test_same_caller_no_warning(self, caplog):
        patches = _mock_rtl_test()
        with patches[0], patches[1], caplog.at_level(logging.WARNING, logger="reticulumpi.rtlsdr"):
            resolve_device("00000001", caller="adsb_radar")
            resolve_device("00000001", caller="adsb_radar")
        assert "claimed by both" not in caplog.text
