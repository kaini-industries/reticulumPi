"""Tests for the spectrum_scanner plugin.

Focuses on the pure-Python surface — config validation, CSV parsing, and
command-line construction — without touching any real RTL-SDR hardware
or spawning ``rtl_power``.  Thread and subprocess behaviour is
exercised indirectly through deterministic method calls.
"""

from __future__ import annotations

import threading
from collections import deque
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi.builtin_plugins.spectrum_scanner import (
    _COMMON_GAIN_STEPS_DB,
    _E4000_LO_GAP_MHZ,
    SpectrumScanner,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def _make_app() -> MagicMock:
    """Build a mock ReticulumPiApp just rich enough for PluginBase.__init__."""
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.event_bus = MagicMock()
    return app


def _make_plugin(config: dict | None = None) -> SpectrumScanner:
    """Construct a SpectrumScanner without calling start() (no threads spun up)."""
    plugin = SpectrumScanner(_make_app(), config or {})
    # Manually set up the attributes start() normally initialises so we
    # can exercise parser + snapshot methods in isolation.
    plugin._state_lock = threading.Lock()
    plugin._process = None
    plugin._pid = None
    plugin._restart_count = 0
    plugin._sweep_count = 0
    plugin._last_sweep_at = None
    plugin._rtl_power_path = "/usr/bin/rtl_power"  # pretend installed
    plugin._last_error = None
    plugin._status = "starting"
    plugin._bins_hz = []
    plugin._latest_powers_db = []
    plugin._waterfall = deque(maxlen=plugin._waterfall_rows)
    plugin._segments = {}
    plugin._current_ts = None
    return plugin


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------
class TestValidateConfig:
    def test_defaults(self):
        p = _make_plugin()
        assert p._freq_start_mhz == 88.0
        assert p._freq_stop_mhz == 108.0
        assert p._bin_khz == 25.0
        assert p._sweep_seconds == 2
        assert p._gain_db == 40.0
        assert p._ppm == 0
        assert p._waterfall_rows == 128
        assert p._device_index == 0

    def test_custom_config_overrides_defaults(self):
        p = _make_plugin({
            "freq_start_mhz": 144.0,
            "freq_stop_mhz": 148.0,
            "bin_khz": 10.0,
            "sweep_seconds": 5,
            "gain_db": 29.0,
            "ppm": -3,
            "waterfall_rows": 256,
            "device_index": 1,
        })
        assert p._freq_start_mhz == 144.0
        assert p._freq_stop_mhz == 148.0
        assert p._bin_khz == 10.0
        assert p._sweep_seconds == 5
        assert p._gain_db == 29.0
        assert p._ppm == -3
        assert p._waterfall_rows == 256
        assert p._device_index == 1

    def test_null_gain_means_auto(self):
        p = _make_plugin({"gain_db": None})
        assert p._gain_db is None

    def test_freq_stop_must_exceed_freq_start(self):
        with pytest.raises(ValueError, match="freq_stop_mhz"):
            _make_plugin({"freq_start_mhz": 108.0, "freq_stop_mhz": 88.0})

    def test_freq_stop_equal_to_start_rejected(self):
        with pytest.raises(ValueError, match="freq_stop_mhz"):
            _make_plugin({"freq_start_mhz": 100.0, "freq_stop_mhz": 100.0})

    def test_bin_khz_out_of_range(self):
        with pytest.raises(ValueError, match="bin_khz"):
            _make_plugin({"bin_khz": 0.5})
        with pytest.raises(ValueError, match="bin_khz"):
            _make_plugin({"bin_khz": 5000})

    def test_sweep_seconds_out_of_range(self):
        with pytest.raises(ValueError, match="sweep_seconds"):
            _make_plugin({"sweep_seconds": 0})
        with pytest.raises(ValueError, match="sweep_seconds"):
            _make_plugin({"sweep_seconds": 120})

    def test_gain_out_of_range(self):
        with pytest.raises(ValueError, match="gain_db"):
            _make_plugin({"gain_db": 200.0})

    def test_waterfall_rows_out_of_range(self):
        with pytest.raises(ValueError, match="waterfall_rows"):
            _make_plugin({"waterfall_rows": 4})
        with pytest.raises(ValueError, match="waterfall_rows"):
            _make_plugin({"waterfall_rows": 100_000})


# ---------------------------------------------------------------------------
# _build_cmd — rtl_power command-line assembly
# ---------------------------------------------------------------------------
class TestBuildCmd:
    def test_basic_command_shape(self):
        p = _make_plugin()
        cmd = p._build_cmd()
        # Binary first, CSV sink last, run-forever in between.
        assert cmd[0] == "/usr/bin/rtl_power"
        assert cmd[-1] == "-"
        assert "-f" in cmd
        freq_idx = cmd.index("-f")
        assert cmd[freq_idx + 1] == "88.000M:108.000M:25.000k"
        # Default sweep interval 2s
        interval_idx = cmd.index("-i")
        assert cmd[interval_idx + 1] == "2s"
        # Run forever
        e_idx = cmd.index("-e")
        assert cmd[e_idx + 1] == "0"

    def test_gain_included_when_set(self):
        p = _make_plugin({"gain_db": 34.0})
        cmd = p._build_cmd()
        g_idx = cmd.index("-g")
        assert cmd[g_idx + 1] == "34.0"

    def test_gain_omitted_when_null(self):
        p = _make_plugin({"gain_db": None})
        cmd = p._build_cmd()
        assert "-g" not in cmd

    def test_ppm_and_device_index_always_included(self):
        p = _make_plugin({"ppm": -12, "device_index": 2})
        cmd = p._build_cmd()
        p_idx = cmd.index("-p")
        assert cmd[p_idx + 1] == "-12"
        d_idx = cmd.index("-d")
        assert cmd[d_idx + 1] == "2"


# ---------------------------------------------------------------------------
# CSV parser — single-segment and multi-segment sweeps
# ---------------------------------------------------------------------------
class TestCsvParser:
    def test_single_segment_sweep(self):
        """Narrow span fits in one rtl_power CSV line per sweep."""
        p = _make_plugin({"freq_start_mhz": 88.0, "freq_stop_mhz": 90.0})
        # One CSV segment; timestamp changes to flush.
        p._handle_csv_line(
            "2025-01-01, 12:00:00, 88000000, 90000000, 500000, 1, "
            "-55.0, -54.0, -53.0, -52.0"
        )
        p._handle_csv_line(
            "2025-01-01, 12:00:02, 88000000, 90000000, 500000, 1, "
            "-45.0, -44.0, -43.0, -42.0"
        )

        # First sweep got flushed when ts changed.
        assert p._sweep_count == 1
        assert p._bins_hz == [88_000_000, 88_500_000, 89_000_000, 89_500_000]
        assert p._latest_powers_db == [-55.0, -54.0, -53.0, -52.0]
        assert len(p._waterfall) == 1

        # Flush the still-accumulating second sweep.
        p._flush_current_sweep()
        assert p._sweep_count == 2
        assert p._latest_powers_db == [-45.0, -44.0, -43.0, -42.0]
        assert len(p._waterfall) == 2

    def test_multi_segment_sweep_is_assembled_in_freq_order(self):
        """Wide span → multiple CSV lines per sweep (same timestamp), sorted by freq."""
        p = _make_plugin({"freq_start_mhz": 88.0, "freq_stop_mhz": 94.0})
        # Deliberately feed segments out-of-order to exercise the sort.
        p._handle_csv_line(
            "2025-01-01, 12:00:00, 92000000, 94000000, 500000, 1, -70.0, -69.0, -68.0, -67.0"
        )
        p._handle_csv_line(
            "2025-01-01, 12:00:00, 88000000, 90000000, 500000, 1, -55.0, -54.0, -53.0, -52.0"
        )
        p._handle_csv_line(
            "2025-01-01, 12:00:00, 90000000, 92000000, 500000, 1, -60.0, -59.0, -58.0, -57.0"
        )
        # New timestamp → flush the accumulated 3-segment sweep.
        p._handle_csv_line(
            "2025-01-01, 12:00:02, 88000000, 90000000, 500000, 1, 0, 0, 0, 0"
        )

        assert p._sweep_count == 1
        # Bins should be contiguous from 88 to 94 MHz at 500 kHz spacing.
        assert p._bins_hz[0] == 88_000_000
        assert p._bins_hz[-1] == 93_500_000
        assert len(p._bins_hz) == 12
        # Power values should follow the sorted-by-freq order:
        # first segment 88-90, then 90-92, then 92-94.
        assert p._latest_powers_db == [
            -55.0, -54.0, -53.0, -52.0,   # 88-90 MHz
            -60.0, -59.0, -58.0, -57.0,   # 90-92 MHz
            -70.0, -69.0, -68.0, -67.0,   # 92-94 MHz
        ]

    def test_ignores_comments_and_blank_lines(self):
        p = _make_plugin()
        p._handle_csv_line("")
        p._handle_csv_line("# this is a comment")
        p._handle_csv_line("   ")
        assert p._sweep_count == 0
        assert not p._segments

    def test_ignores_malformed_rows(self):
        p = _make_plugin()
        # Too few columns
        p._handle_csv_line("2025-01-01, 12:00:00, 88000000")
        # Non-numeric dB values
        p._handle_csv_line(
            "2025-01-01, 12:00:00, 88000000, 90000000, 500000, 1, hello, world"
        )
        assert p._sweep_count == 0
        # Non-parseable dBs → no segment stored
        assert not p._segments

    def test_waterfall_respects_max_rows(self):
        p = _make_plugin({"waterfall_rows": 8})
        for i in range(20):
            p._handle_csv_line(
                f"2025-01-01, 12:00:{i:02d}, 88000000, 90000000, 500000, 1, -50, -50, -50, -50"
            )
        # Last sweep is still accumulating; force flush.
        p._flush_current_sweep()
        # 20 sweeps total, but deque is capped at 8.
        assert p._sweep_count == 20
        assert len(p._waterfall) == 8

    def test_flush_with_no_segments_is_noop(self):
        p = _make_plugin()
        p._flush_current_sweep()
        assert p._sweep_count == 0
        assert not p._waterfall


# ---------------------------------------------------------------------------
# get_snapshot
# ---------------------------------------------------------------------------
class TestSnapshot:
    def test_empty_snapshot_shape(self):
        p = _make_plugin()
        snap = p.get_snapshot()
        for key in (
            "status", "error", "freq_start_hz", "freq_stop_hz",
            "bin_hz_requested", "sweep_seconds", "gain_db", "ppm",
            "sweep_count", "last_sweep_at", "waterfall_rows",
            "bins_hz", "latest_powers_db", "waterfall_tail",
        ):
            assert key in snap, f"missing key {key!r}"
        assert snap["sweep_count"] == 0
        assert snap["bins_hz"] == []
        assert snap["waterfall_tail"] == []

    def test_snapshot_after_sweep(self):
        p = _make_plugin()
        p._handle_csv_line(
            "2025-01-01, 12:00:00, 88000000, 90000000, 500000, 1, "
            "-55.5, -54.4, -53.3, -52.2"
        )
        p._flush_current_sweep()

        snap = p.get_snapshot()
        assert snap["sweep_count"] == 1
        assert snap["bins_hz"] == [88_000_000, 88_500_000, 89_000_000, 89_500_000]
        # Snapshot rounds power values to 1 decimal for wire efficiency.
        assert snap["latest_powers_db"] == [-55.5, -54.4, -53.3, -52.2]
        assert len(snap["waterfall_tail"]) == 1
        assert snap["freq_start_hz"] == 88_000_000
        assert snap["freq_stop_hz"] == 108_000_000

    def test_snapshot_tail_caps_at_configured_length(self):
        """Plugin may retain hundreds of sweeps internally, but the snapshot
        only emits the last _SNAPSHOT_TAIL_ROWS to keep wire payload small."""
        p = _make_plugin({"waterfall_rows": 256})
        for i in range(30):
            p._handle_csv_line(
                f"2025-01-01, 12:00:{i:02d}, 88000000, 90000000, 500000, 1, -50, -50, -50, -50"
            )
        p._flush_current_sweep()

        assert p._sweep_count == 30
        assert len(p._waterfall) == 30  # internal buffer retained all
        snap = p.get_snapshot()
        # Snapshot tail is capped regardless of internal buffer depth.
        assert len(snap["waterfall_tail"]) == SpectrumScanner._SNAPSHOT_TAIL_ROWS


# ---------------------------------------------------------------------------
# get_history — full backfill for the dashboard's one-shot REST fetch
# ---------------------------------------------------------------------------
class TestHistory:
    def test_empty_history_shape(self):
        p = _make_plugin()
        h = p.get_history()
        for key in ("available", "sweep_count", "bin_count", "waterfall_rows", "rows"):
            assert key in h
        assert h["available"] is True
        assert h["sweep_count"] == 0
        assert h["bin_count"] == 0
        assert h["rows"] == []

    def test_history_returns_full_buffer_unlike_snapshot(self):
        """Unlike get_snapshot() (which caps at _SNAPSHOT_TAIL_ROWS), the
        history endpoint ships the entire rolling buffer."""
        p = _make_plugin({"waterfall_rows": 64})
        for i in range(40):
            p._handle_csv_line(
                f"2025-01-01, 12:00:{i:02d}, 88000000, 90000000, 500000, 1, -50, -50, -50, -50"
            )
        p._flush_current_sweep()

        h = p.get_history()
        # All 40 sweeps fit under waterfall_rows=64 and should all be returned.
        assert len(h["rows"]) == 40
        assert h["sweep_count"] == 40
        assert h["bin_count"] == 4
        # Power values are rounded to 1 decimal for wire efficiency.
        assert h["rows"][0] == [-50.0, -50.0, -50.0, -50.0]

    def test_history_capped_by_waterfall_rows(self):
        """If the internal buffer is at its cap, history reflects that —
        older sweeps have already aged out of the deque."""
        p = _make_plugin({"waterfall_rows": 16})
        for i in range(50):
            p._handle_csv_line(
                f"2025-01-01, 12:00:{i:02d}, 88000000, 90000000, 500000, 1, -50, -50, -50, -50"
            )
        p._flush_current_sweep()

        h = p.get_history()
        assert h["sweep_count"] == 50  # monotonic counter unaffected
        assert h["waterfall_rows"] == 16
        assert len(h["rows"]) == 16  # only the most recent 16 retained


# ---------------------------------------------------------------------------
# Supervisor: missing binary → graceful 'unavailable' status
# ---------------------------------------------------------------------------
class TestSupervisorMissingBinary:
    def test_status_unavailable_when_rtl_power_missing(self):
        p = _make_plugin()
        # Re-enter supervisor expectations: _active must be True for the
        # loop, but with which() returning None it bails on the first
        # iteration without ever trying to Popen.
        p._active = True
        with patch(
            "reticulumpi.builtin_plugins.spectrum_scanner.shutil.which",
            return_value=None,
        ):
            p._supervisor_loop()
        assert p._status == "unavailable"
        assert p._last_error is not None
        assert "rtl_power" in p._last_error


# ---------------------------------------------------------------------------
# Module-level constants are exposed for documentation / UI hints
# ---------------------------------------------------------------------------
class TestModuleConstants:
    def test_gain_steps_cover_expected_e4000_values(self):
        assert 42.0 in _COMMON_GAIN_STEPS_DB
        assert -1.0 in _COMMON_GAIN_STEPS_DB

    def test_e4000_gap_roughly_matches_hardware(self):
        lo, hi = _E4000_LO_GAP_MHZ
        # Hardware reports 1101-1234 MHz (see `rtl_test -t` output).
        assert 1090 <= lo <= 1120
        assert 1220 <= hi <= 1260
