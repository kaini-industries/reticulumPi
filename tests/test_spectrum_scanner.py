"""Tests for the spectrum_scanner plugin.

Focuses on the pure-Python surface — config validation, CSV parsing, and
command-line construction — without touching any real RTL-SDR hardware
or spawning ``rtl_power``.  Thread and subprocess behaviour is
exercised indirectly through deterministic method calls.
"""

from __future__ import annotations

import threading
from collections import deque
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi.builtin_plugins.spectrum_scanner import (
    _COMMON_GAIN_STEPS_DB,
    _E4000_LO_GAP_MHZ,
    SpectrumScanner,
)
from reticulumpi.plugin_base import PluginState
from reticulumpi.process_supervisor import ProcessFailure


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
    plugin._process_group = None
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
    plugin._supervisor_alive = False
    plugin._resolved_index = None
    plugin._device_lease = None
    plugin._detected_peaks = []
    plugin._sweep_intervals = deque(maxlen=60)
    plugin._last_sweep_mono = None
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
        assert p._waterfall_rows == 512
        assert p._device_id == "0"
        assert p._device_selector == "index"

    def test_custom_config_overrides_defaults(self):
        p = _make_plugin(
            {
                "freq_start_mhz": 144.0,
                "freq_stop_mhz": 148.0,
                "bin_khz": 10.0,
                "sweep_seconds": 5,
                "gain_db": 29.0,
                "ppm": -3,
                "waterfall_rows": 256,
                "device_index": 1,
            }
        )
        assert p._freq_start_mhz == 144.0
        assert p._freq_stop_mhz == 148.0
        assert p._bin_khz == 10.0
        assert p._sweep_seconds == 5
        assert p._gain_db == 29.0
        assert p._ppm == -3
        assert p._waterfall_rows == 256
        assert p._device_id == "1"
        assert p._device_selector == "index"

    def test_device_serial_and_index_keep_distinct_semantics(self):
        by_index = _make_plugin({"device_index": "00000001"})
        by_serial = _make_plugin(
            {"device_serial": "00000001", "device_index": "00000002"}
        )

        assert (by_index._device_id, by_index._device_selector) == ("00000001", "index")
        assert (by_serial._device_id, by_serial._device_selector) == (
            "00000001",
            "serial",
        )

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
        assert cmd[d_idx + 1] == "2"  # str passed through to -d flag


# ---------------------------------------------------------------------------
# CSV parser — single-segment and multi-segment sweeps
# ---------------------------------------------------------------------------
class TestCsvParser:
    def test_single_segment_sweep(self):
        """Narrow span fits in one rtl_power CSV line per sweep."""
        p = _make_plugin({"freq_start_mhz": 88.0, "freq_stop_mhz": 90.0})
        # One CSV segment; timestamp changes to flush.
        p._handle_csv_line(
            "2025-01-01, 12:00:00, 88000000, 90000000, 500000, 1, -55.0, -54.0, -53.0, -52.0"
        )
        p._handle_csv_line(
            "2025-01-01, 12:00:02, 88000000, 90000000, 500000, 1, -45.0, -44.0, -43.0, -42.0"
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
        p._handle_csv_line("2025-01-01, 12:00:02, 88000000, 90000000, 500000, 1, 0, 0, 0, 0")

        assert p._sweep_count == 1
        # Bins should be contiguous from 88 to 94 MHz at 500 kHz spacing.
        assert p._bins_hz[0] == 88_000_000
        assert p._bins_hz[-1] == 93_500_000
        assert len(p._bins_hz) == 12
        # Power values should follow the sorted-by-freq order:
        # first segment 88-90, then 90-92, then 92-94.
        assert p._latest_powers_db == [
            -55.0,
            -54.0,
            -53.0,
            -52.0,  # 88-90 MHz
            -60.0,
            -59.0,
            -58.0,
            -57.0,  # 90-92 MHz
            -70.0,
            -69.0,
            -68.0,
            -67.0,  # 92-94 MHz
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
        p._handle_csv_line("2025-01-01, 12:00:00, 88000000, 90000000, 500000, 1, hello, world")
        assert p._sweep_count == 0
        # Non-parseable dBs → no segment stored
        assert not p._segments

    def test_nan_and_inf_become_none_preserving_positions(self):
        """rtl_power emits 'nan'/'-inf' for bins it couldn't sample;
        filter them to None so bin positions — and therefore the frequency
        map built from them in _flush_current_sweep — stay aligned, and
        strict JSON serialization of the snapshot doesn't trip over
        non-finite floats."""
        p = _make_plugin({"freq_start_mhz": 88.0, "freq_stop_mhz": 90.0})
        p._handle_csv_line(
            "2025-01-01, 12:00:00, 88000000, 90000000, 500000, 1, -55.0, nan, -inf, -52.0"
        )
        p._flush_current_sweep()
        assert p._sweep_count == 1
        # Positions preserved: 4 bins → 4 frequencies → 4 power values.
        assert p._bins_hz == [88_000_000, 88_500_000, 89_000_000, 89_500_000]
        assert p._latest_powers_db == [-55.0, None, None, -52.0]

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
            "status",
            "error",
            "freq_start_hz",
            "freq_stop_hz",
            "bin_hz_requested",
            "sweep_seconds",
            "gain_db",
            "ppm",
            "sweep_count",
            "last_sweep_at",
            "waterfall_rows",
            "bins_hz",
            "latest_powers_db",
            "waterfall_tail",
            "waterfall_tail_times",
        ):
            assert key in snap, f"missing key {key!r}"
        assert snap["sweep_count"] == 0
        assert snap["bins_hz"] == []
        assert snap["waterfall_tail"] == []
        # Sibling timestamp array stays in lock-step with waterfall_tail.
        assert snap["waterfall_tail_times"] == []

    def test_snapshot_after_sweep(self):
        import time as _t

        before = _t.time()
        p = _make_plugin()
        p._handle_csv_line(
            "2025-01-01, 12:00:00, 88000000, 90000000, 500000, 1, -55.5, -54.4, -53.3, -52.2"
        )
        p._flush_current_sweep()
        after = _t.time()

        snap = p.get_snapshot()
        assert snap["sweep_count"] == 1
        assert snap["bins_hz"] == [88_000_000, 88_500_000, 89_000_000, 89_500_000]
        # Snapshot rounds power values to 1 decimal for wire efficiency.
        assert snap["latest_powers_db"] == [-55.5, -54.4, -53.3, -52.2]
        assert len(snap["waterfall_tail"]) == 1
        assert snap["freq_start_hz"] == 88_000_000
        assert snap["freq_stop_hz"] == 108_000_000
        # The raw timestamp is captured during the flush.  The wire timestamp
        # is rounded to milliseconds, so validate timing before rounding and
        # validate the serialization precision separately.
        assert len(snap["waterfall_tail_times"]) == 1
        ts = snap["waterfall_tail_times"][0]
        assert p._last_sweep_at is not None
        assert before <= p._last_sweep_at <= after
        assert ts == round(p._last_sweep_at, 3)

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
        # Timestamp sibling stays same length, and sweeps were flushed in
        # order so the captured times must be monotonically non-decreasing.
        times = snap["waterfall_tail_times"]
        assert len(times) == SpectrumScanner._SNAPSHOT_TAIL_ROWS
        for a, b in zip(times, times[1:]):
            assert a <= b


# ---------------------------------------------------------------------------
# get_history — full backfill for the dashboard's one-shot REST fetch
# ---------------------------------------------------------------------------
class TestHistory:
    def test_empty_history_shape(self):
        p = _make_plugin()
        h = p.get_history()
        for key in (
            "available",
            "sweep_count",
            "waterfall_rows",
            "rows",
            "row_timestamps",
        ):
            assert key in h
        assert h["available"] is True
        assert h["sweep_count"] == 0
        assert "bin_count" not in h
        assert "bins_hz" not in h
        assert h["rows"] == []
        # Sibling array stays in lock-step with rows.
        assert h["row_timestamps"] == []

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
        # Parallel timestamp array: same length, same ordering, monotonic.
        assert len(h["row_timestamps"]) == 40
        for a, b in zip(h["row_timestamps"], h["row_timestamps"][1:]):
            assert a <= b

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

    def test_row_timestamps_match_flush_wallclock(self):
        """row_timestamps must carry the wall-clock time each sweep flushed,
        in the same ordering as rows (oldest first → newest last).  Patch
        time.time() to a controlled sequence so we can assert exact values
        and confirm the ordering contract the frontend relies on."""
        # Three flushes with three distinct timestamps.  The module-level
        # `time.time()` call inside _flush_current_sweep is what we patch.
        fake_times = iter([1000.0, 1002.5, 1005.0])
        with patch(
            "reticulumpi.builtin_plugins.spectrum_scanner.time.time",
            side_effect=lambda: next(fake_times),
        ):
            p = _make_plugin()
            for i in range(3):
                p._handle_csv_line(
                    f"2025-01-01, 12:00:{i:02d}, 88000000, 89000000, 500000, 1, -50, -50"
                )
            p._flush_current_sweep()

        h = p.get_history()
        assert len(h["rows"]) == 3
        # Oldest first, newest last — same ordering as rows.
        assert h["row_timestamps"] == [1000.0, 1002.5, 1005.0]

        # And in the snapshot tail (8-row cap doesn't truncate 3 rows).
        snap = p.get_snapshot()
        assert snap["waterfall_tail_times"] == [1000.0, 1002.5, 1005.0]
        assert len(snap["waterfall_tail"]) == len(snap["waterfall_tail_times"])


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


# ---------------------------------------------------------------------------
# Preset system
# ---------------------------------------------------------------------------
class TestPresets:
    def test_builtin_presets_available_by_default(self):
        p = _make_plugin()
        info = p.get_presets()
        names = [pr["name"] for pr in info["presets"]]
        assert "fm_broadcast" in names
        assert "lora_us915" in names
        assert "aviation" in names

    def test_default_preset_applies_on_construction(self):
        p = _make_plugin({"default_preset": "lora_us915"})
        assert p._active_preset == "lora_us915"
        assert p._freq_start_mhz == 902.0
        assert p._freq_stop_mhz == 928.0

    def test_no_default_preset_uses_flat_config(self):
        p = _make_plugin({"freq_start_mhz": 144.0, "freq_stop_mhz": 148.0})
        assert p._active_preset is None
        assert p._freq_start_mhz == 144.0

    def test_user_preset_merges_with_builtins(self):
        p = _make_plugin(
            {"presets": {"my_band": {"freq_start_mhz": 200.0, "freq_stop_mhz": 210.0}}}
        )
        info = p.get_presets()
        names = [pr["name"] for pr in info["presets"]]
        assert "my_band" in names
        assert "fm_broadcast" in names

    def test_user_preset_overrides_builtin(self):
        p = _make_plugin(
            {"presets": {"fm_broadcast": {"freq_start_mhz": 87.5, "freq_stop_mhz": 108.0}}}
        )
        preset = p._presets["fm_broadcast"]
        assert preset["freq_start_mhz"] == 87.5

    def test_get_presets_shape(self):
        p = _make_plugin({"default_preset": "aviation"})
        info = p.get_presets()
        assert "active_preset" in info
        assert "presets" in info
        assert info["active_preset"] == "aviation"
        for pr in info["presets"]:
            assert "name" in pr
            assert "has_analysis" in pr

    def test_lora_preset_has_analysis_flag(self):
        p = _make_plugin()
        info = p.get_presets()
        lora = [pr for pr in info["presets"] if pr["name"] == "lora_us915"][0]
        assert lora["has_analysis"] is True
        fm = [pr for pr in info["presets"] if pr["name"] == "fm_broadcast"][0]
        assert fm["has_analysis"] is False


class TestSwitchPreset:
    def test_switch_changes_frequency(self):
        p = _make_plugin({"default_preset": "fm_broadcast"})
        result = p.switch_preset("lora_us915")
        assert result["preset"] == "lora_us915"
        assert p._freq_start_mhz == 902.0
        assert p._freq_stop_mhz == 928.0
        assert result["has_analysis"] is True

    def test_switch_clears_sweep_state(self):
        p = _make_plugin({"default_preset": "fm_broadcast"})
        p._bins_hz = [88e6, 89e6, 90e6]
        p._latest_powers_db = [-80, -75, -70]
        p._sweep_count = 42
        old_version = p._bins_version

        p.switch_preset("aviation")

        assert p._bins_hz == []
        assert p._latest_powers_db == []
        assert p._sweep_count == 0
        assert p._bins_version > old_version

    def test_switch_activates_lora_analyzer(self):
        p = _make_plugin({"default_preset": "fm_broadcast"})
        assert p._analyzer is None
        p.switch_preset("lora_us915")
        assert p._analyzer is not None

    def test_switch_deactivates_analyzer_on_non_lora(self):
        p = _make_plugin({"default_preset": "lora_us915"})
        p._activate_analyzer_for_preset(p._presets["lora_us915"])
        assert p._analyzer is not None
        p.switch_preset("fm_broadcast")
        assert p._analyzer is None

    def test_switch_unknown_preset_raises(self):
        p = _make_plugin()
        with pytest.raises(ValueError, match="Unknown preset"):
            p.switch_preset("nonexistent")

    def test_switch_resets_restart_count(self):
        p = _make_plugin({"default_preset": "fm_broadcast"})
        p._restart_count = 3
        p.switch_preset("aviation")
        assert p._restart_count == 0

    def test_switch_restores_base_gain_when_preset_has_no_gain(self):
        p = _make_plugin({"gain_db": 40.0, "default_preset": "lora_us915"})
        assert p._gain_db == 34.0  # lora preset overrides
        p.switch_preset("fm_broadcast")
        assert p._gain_db == 40.0  # restored to base

    def test_switch_return_shape(self):
        p = _make_plugin()
        result = p.switch_preset("aviation")
        assert "preset" in result
        assert "freq_start_mhz" in result
        assert "freq_stop_mhz" in result
        assert "has_analysis" in result

    def test_snapshot_includes_preset_info(self):
        p = _make_plugin({"default_preset": "fm_broadcast"})
        snap = p.get_snapshot()
        assert snap["active_preset"] == "fm_broadcast"
        assert "available_presets" in snap
        assert snap["switching"] is False

    def test_snapshot_includes_channel_analysis_in_lora_mode(self):
        p = _make_plugin({"default_preset": "lora_us915"})
        p._activate_analyzer_for_preset(p._presets["lora_us915"])
        p._bins_hz = [int(902e6 + i * 12500) for i in range(2080)]
        snap = p.get_snapshot()
        assert "channel_analysis" in snap

    def test_history_includes_channel_power_in_lora_mode(self):
        p = _make_plugin({"default_preset": "lora_us915"})
        p._activate_analyzer_for_preset(p._presets["lora_us915"])
        hist = p.get_history()
        assert "channel_power_history" in hist

    def test_history_omits_channel_power_in_non_lora_mode(self):
        p = _make_plugin({"default_preset": "fm_broadcast"})
        hist = p.get_history()
        assert "channel_power_history" not in hist

    def test_switch_starts_supervisor_if_dead(self):
        p = _make_plugin({"default_preset": "fm_broadcast"})
        p._supervisor_alive = False
        with patch.object(p, "_start_thread") as mock_start:
            p.switch_preset("aviation")
            mock_start.assert_called_once()

    def test_switch_skips_supervisor_if_alive(self):
        p = _make_plugin({"default_preset": "fm_broadcast"})
        p._supervisor_alive = True
        with patch.object(p, "_start_thread") as mock_start:
            p.switch_preset("aviation")
            mock_start.assert_not_called()

    def test_switch_resets_switching_flag_on_error(self):
        p = _make_plugin({"default_preset": "fm_broadcast"})
        with patch.object(p, "_apply_preset_values", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                p.switch_preset("aviation")
        assert p._switching is False
        assert "error" in p._status

    def test_switch_resets_switching_flag_on_terminate_error(self):
        p = _make_plugin({"default_preset": "fm_broadcast"})
        with patch.object(p, "_terminate_process", side_effect=OSError("kill failed")):
            with pytest.raises(OSError, match="kill failed"):
                p.switch_preset("aviation")
        assert p._switching is False


class TestPresetValidation:
    def test_preset_rejects_invalid_freq_range(self):
        with pytest.raises(ValueError, match="freq_stop_mhz"):
            _make_plugin(
                {
                    "default_preset": "bad",
                    "presets": {"bad": {"freq_start_mhz": 200.0, "freq_stop_mhz": 100.0}},
                }
            )

    def test_preset_rejects_bin_khz_out_of_range(self):
        with pytest.raises(ValueError, match="bin_khz"):
            _make_plugin(
                {
                    "default_preset": "bad",
                    "presets": {
                        "bad": {"freq_start_mhz": 88.0, "freq_stop_mhz": 108.0, "bin_khz": 0.5}
                    },
                }
            )

    def test_preset_rejects_sweep_out_of_range(self):
        with pytest.raises(ValueError, match="sweep_seconds"):
            _make_plugin(
                {
                    "default_preset": "bad",
                    "presets": {
                        "bad": {
                            "freq_start_mhz": 88.0,
                            "freq_stop_mhz": 108.0,
                            "sweep_seconds": 120,
                        }
                    },
                }
            )

    def test_preset_rejects_gain_out_of_range(self):
        with pytest.raises(ValueError, match="gain_db"):
            _make_plugin(
                {
                    "default_preset": "bad",
                    "presets": {
                        "bad": {"freq_start_mhz": 88.0, "freq_stop_mhz": 108.0, "gain_db": 200.0}
                    },
                }
            )

    def test_switch_preset_validates_values(self):
        p = _make_plugin()
        p._presets["bad"] = {"freq_start_mhz": 200.0, "freq_stop_mhz": 100.0}
        with pytest.raises(ValueError, match="freq_stop_mhz"):
            p.switch_preset("bad")

    def test_switch_respects_cooldown(self):
        p = _make_plugin({"default_preset": "fm_broadcast"})
        p._last_preset_switch = 0.0
        p.switch_preset("aviation")
        with pytest.raises(ValueError, match="cooldown"):
            p.switch_preset("fm_broadcast")


class TestPresetAnalyzerIntegration:
    """End-to-end: switch to lora_us915, feed CSV data, verify channel_analysis."""

    def _make_lora_plugin(self):
        p = _make_plugin({"default_preset": "lora_us915"})
        # _make_plugin skips start() — activate the analyzer manually.
        if p._active_preset and p._active_preset in p._presets:
            p._activate_analyzer_for_preset(p._presets[p._active_preset])
        return p

    def _make_csv_line(self, freq_lo_hz, freq_hi_hz, bin_step_hz, powers_db):
        """Construct an rtl_power CSV line."""
        n_bins = len(powers_db)
        parts = [
            "2026-01-01",
            "00:00:00",
            str(freq_lo_hz),
            str(freq_hi_hz),
            str(bin_step_hz),
            str(n_bins),
        ]
        parts += [f"{p:.1f}" for p in powers_db]
        return ", ".join(parts)

    def test_lora_preset_produces_channel_analysis(self):
        p = self._make_lora_plugin()
        assert p._analyzer is not None

        # Feed a sweep covering 902-928 MHz at 12.5 kHz bins (same as preset)
        bin_step = 12500
        freq_lo = 902_000_000
        freq_hi = 928_000_000
        n_bins = (freq_hi - freq_lo) // bin_step
        powers = [-80.0] * n_bins
        # Elevate channel 0 bins (centered at 902.3 MHz, BW 125 kHz)
        for i in range(n_bins):
            f = freq_lo + i * bin_step
            if 902_237_500 <= f < 902_362_500:
                powers[i] = -50.0

        line = self._make_csv_line(freq_lo, freq_hi, bin_step, powers)
        p._handle_csv_line(line)
        p._flush_current_sweep()

        snap = p.get_snapshot()
        assert "channel_analysis" in snap
        ca = snap["channel_analysis"]
        assert len(ca["channels"]) == 72
        assert ca["noise_floor_db"] is not None
        ch0 = ca["channels"][0]
        assert ch0["power_db"] is not None
        assert ch0["power_db"] > -60.0

    def test_capture_triggers_published_in_lora_mode(self):
        p = self._make_lora_plugin()
        assert p._analyzer is not None

        bin_step = 12500
        freq_lo = 902_000_000
        freq_hi = 928_000_000
        n_bins = (freq_hi - freq_lo) // bin_step
        powers = [-90.0] * n_bins
        for i in range(n_bins):
            f = freq_lo + i * bin_step
            if 902_237_500 <= f < 902_362_500:
                powers[i] = -40.0

        line = self._make_csv_line(freq_lo, freq_hi, bin_step, powers)

        # Feed enough sweeps to exceed capture_trigger_consec (default 3)
        for sweep in range(5):
            p._current_ts = f"2026-01-01 00:00:0{sweep}"
            p._handle_csv_line(line)
            p._flush_current_sweep()

        publish_calls = p.app.event_bus.publish.call_args_list
        trigger_calls = [c for c in publish_calls if c[0][0] == "lora.capture_trigger"]
        assert len(trigger_calls) >= 1
        payload = trigger_calls[0][0][1]
        assert payload["channel_idx"] == 0

    def test_no_channel_analysis_in_non_lora_preset(self):
        p = _make_plugin({"default_preset": "fm_broadcast"})
        assert p._analyzer is None
        snap = p.get_snapshot()
        assert "channel_analysis" not in snap

    def test_switch_from_lora_to_fm_removes_analysis(self):
        p = self._make_lora_plugin()
        assert p._analyzer is not None
        p.switch_preset("fm_broadcast")
        assert p._analyzer is None
        snap = p.get_snapshot()
        assert "channel_analysis" not in snap


class TestSpectrumSweepEvent:
    """Verify that SPECTRUM_SWEEP events carry frequency and power data."""

    def _make_csv_line(self, freq_lo_hz, freq_hi_hz, bin_step_hz, powers_db):
        n_bins = len(powers_db)
        parts = [
            "2026-01-01",
            "00:00:00",
            str(freq_lo_hz),
            str(freq_hi_hz),
            str(bin_step_hz),
            str(n_bins),
        ]
        parts += [f"{p:.1f}" for p in powers_db]
        return ", ".join(parts)

    def test_sweep_event_includes_bins_and_powers(self):
        from reticulumpi import events

        p = _make_plugin()
        p._event_sweep_topic = events.SPECTRUM_SWEEP
        freq_lo = 88_000_000
        freq_hi = 108_000_000
        bin_step = 250_000
        n_bins = (freq_hi - freq_lo) // bin_step
        powers = [-70.0] * n_bins

        line = self._make_csv_line(freq_lo, freq_hi, bin_step, powers)
        p._handle_csv_line(line)
        p._flush_current_sweep()

        publish_calls = p.app.event_bus.publish.call_args_list
        sweep_calls = [c for c in publish_calls if c[0][0] == "spectrum.sweep"]
        assert len(sweep_calls) >= 1
        payload = sweep_calls[0][0][1]
        assert "bins_hz" in payload
        assert "powers_db" in payload
        assert len(payload["bins_hz"]) == n_bins
        assert len(payload["powers_db"]) == n_bins
        assert payload["bins_hz"][0] == freq_lo
        assert payload["powers_db"][0] == -70.0


class TestManagedSpectrumLifecycle:
    def test_zero_padded_device_index_resolves_as_index_not_matching_serial(self):
        from reticulumpi.rtlsdr import reset_cache

        reset_cache()
        p = _make_plugin({"device_index": "00000001"})
        try:
            with (
                patch(
                    "reticulumpi.rtlsdr.enumerate_devices",
                    return_value=[(0, "00000001"), (1, "07143901")],
                ),
                patch.object(p, "_start_thread"),
                patch.object(p, "_join_threads"),
            ):
                p.start()
                assert p._resolved_index == 1
                assert p._device_lease.canonical_id == "serial:07143901"
                p.stop()
        finally:
            reset_cache()

    def test_start_and_stop_own_one_device_lease(self):
        p = _make_plugin()
        lease = SimpleNamespace(index=7, release=MagicMock())
        with (
            patch("reticulumpi.rtlsdr.refresh_device_lease", return_value=lease) as refresh,
            patch.object(p, "_start_thread") as start_thread,
            patch.object(p, "_join_threads") as join_threads,
        ):
            p.start()
            assert p._device_lease is lease
            assert p._resolved_index == 7
            assert p._process_group is None
            start_thread.assert_called_once()
            p.stop()

        refresh.assert_called_once_with(
            None,
            "0",
            "spectrum_scanner",
            selector="index",
        )
        lease.release.assert_called_once_with()
        join_threads.assert_called_once_with(timeout=5.0)
        assert p._device_lease is None
        assert p._status == "stopped"

    def test_start_continues_in_degraded_state_when_device_is_missing(self):
        p = _make_plugin()
        with (
            patch(
                "reticulumpi.rtlsdr.refresh_device_lease",
                side_effect=RuntimeError("no compatible dongle"),
            ),
            patch.object(p, "_start_thread") as start_thread,
        ):
            p.start()
        start_thread.assert_called_once()
        assert p._status == "error"
        assert p._last_error == "no compatible dongle"

    def test_supervisor_launch_failure_degrades_and_releases_lease(self):
        p = _make_plugin()
        p._active = True
        lease = MagicMock()
        p._device_lease = lease
        with (
            patch(
                "reticulumpi.builtin_plugins.spectrum_scanner.shutil.which",
                return_value="/usr/bin/rtl_power",
            ),
            patch.object(p, "_launch_rtl_power", side_effect=OSError("fork failed")),
            patch.object(p, "mark_degraded") as degraded,
        ):
            p._supervisor_loop_inner()
        degraded.assert_called_once_with("fork failed")
        lease.release.assert_called_once_with()
        assert p._device_lease is None
        assert p._status == "error"

    def test_supervisor_refreshes_an_absent_lease_before_launch(self):
        p = _make_plugin()
        p._active = True
        p._device_lease = None
        with (
            patch(
                "reticulumpi.builtin_plugins.spectrum_scanner.shutil.which",
                return_value="/usr/bin/rtl_power",
            ),
            patch.object(p, "_refresh_device_lease") as refresh,
            patch.object(p, "_launch_rtl_power") as launch,
        ):
            p._supervisor_loop_inner()
        refresh.assert_called_once_with()
        launch.assert_called_once_with()

    def test_launch_constructs_managed_group_and_clears_failed_start(self):
        p = _make_plugin()
        p._active = True
        p._process_group = None
        group = MagicMock()
        group.start.side_effect = RuntimeError("monitor unavailable")
        with patch(
            "reticulumpi.builtin_plugins.spectrum_scanner.ManagedProcessGroup",
            return_value=group,
        ) as constructor:
            with pytest.raises(RuntimeError, match="monitor unavailable"):
                p._launch_rtl_power()
        assert p._process_group is None
        spec = constructor.call_args.args[0][0]
        assert spec.name == "rtl_power"
        assert spec.text is True
        assert spec.encoding == "utf-8"

    def test_process_started_registers_parser_and_preserves_ready_state(self):
        p = _make_plugin()
        p._active = True
        p._plugin_state = PluginState.READY
        group = MagicMock(restart_count=3)
        process = MagicMock(pid=2468)
        p._process_group = group
        with (
            patch.object(p, "mark_ready") as ready,
            patch.object(p, "_start_thread") as start_thread,
        ):
            p._on_process_started(group, (process,), restarted=True)
        ready.assert_called_once_with()
        start_thread.assert_called_once()
        assert p._process is process
        assert p._pid == 2468
        assert p._restart_count == 3
        assert p._status == "running"
        assert p._segments == {}

    def test_process_started_rejects_stale_launch(self):
        p = _make_plugin()
        p._active = True
        with pytest.raises(RuntimeError, match="stale"):
            p._on_process_started(MagicMock(), (MagicMock(),), restarted=False)

    def test_failure_restart_and_restart_failure_update_health(self):
        p = _make_plugin()
        p._active = True
        group = MagicMock()
        p._process_group = group
        failure = ProcessFailure(0, "rtl_power", 2, "unexpected EOF", 1.0)
        with (
            patch.object(p, "mark_degraded") as degraded,
            patch.object(p, "_release_device_lease") as release,
        ):
            p._on_process_failure(group, failure)
        degraded.assert_called_once()
        release.assert_called_once_with()
        assert "unexpected EOF" in p._last_error

        with patch.object(p, "_refresh_device_lease") as refresh:
            p._on_process_restart(group, 2, 4.0)
        refresh.assert_called_once_with()
        group.replace_specs.assert_called_once()
        assert p._restart_count == 2
        assert p._status == "restarting"

        with (
            patch.object(p, "mark_degraded") as degraded,
            patch.object(p, "_release_device_lease") as release,
        ):
            p._on_process_restart_failed(group, RuntimeError("still absent"), 3)
        degraded.assert_called_once()
        release.assert_called_once_with()
        assert p._restart_count == 3
        assert "restart 3 failed" in p._last_error

    def test_restart_rejects_a_stale_group(self):
        p = _make_plugin()
        p._active = True
        with pytest.raises(RuntimeError, match="stopped"):
            p._on_process_restart(MagicMock(), 1, 1.0)

    def test_restart_exhaustion_degrades_and_releases_device(self):
        p = _make_plugin()
        group = MagicMock(restart_count=5)
        p._process_group = group
        lease = MagicMock()
        p._device_lease = lease
        failure = ProcessFailure(0, "rtl_power", 1, "EOF", 1.0)
        with patch.object(p, "mark_degraded") as degraded:
            p._on_process_exhausted(group, failure)
        degraded.assert_called_once()
        lease.release.assert_called_once_with()
        assert p._restart_count == 5
        assert p._status == "error"
        assert p._device_lease is None

    def test_stale_callbacks_do_not_change_current_group(self):
        p = _make_plugin()
        current = MagicMock()
        p._process_group = current
        stale = MagicMock()
        failure = ProcessFailure(0, "rtl_power", 1, "EOF", 1.0)
        p._on_process_failure(stale, failure)
        p._on_process_restart_failed(stale, RuntimeError("ignored"), 2)
        p._on_process_exhausted(stale, failure)
        assert p._process_group is current
        assert p._status == "starting"

    def test_refresh_and_release_device_lease_are_generation_safe(self):
        p = _make_plugin()
        old = MagicMock()
        replacement = SimpleNamespace(index=11, release=MagicMock())
        p._device_lease = old
        with (
            patch("reticulumpi.rtlsdr.invalidate_cache") as invalidate,
            patch("reticulumpi.rtlsdr.refresh_device_lease", return_value=replacement) as refresh,
        ):
            p._refresh_device_lease()
        invalidate.assert_called_once_with()
        refresh.assert_called_once_with(
            old,
            "0",
            "spectrum_scanner",
            selector="index",
        )
        assert p._resolved_index == 11

        p._release_device_lease()
        replacement.release.assert_called_once_with()
        p._release_device_lease()

        broken = MagicMock()
        broken.release.side_effect = OSError("already unplugged")
        p._device_lease = broken
        p._release_device_lease()
        assert p._device_lease is None

    def test_terminate_stops_managed_group_and_clears_process_metadata(self):
        p = _make_plugin()
        group = MagicMock()
        p._process_group = group
        p._process = MagicMock()
        p._pid = 123
        p._terminate_process()
        group.stop.assert_called_once_with()
        assert p._process_group is None
        assert p._process is None
        assert p._pid is None

    def test_parser_eof_flushes_and_notifies_current_group(self):
        p = _make_plugin()
        p._active = True
        stream = MagicMock()
        stream.fileno.return_value = 10
        stream.readline.return_value = ""
        process = MagicMock(stdout=stream)
        process.poll.return_value = None
        group = MagicMock(running=True)
        p._process = process
        p._process_group = group
        with (
            patch(
                "reticulumpi.builtin_plugins.spectrum_scanner.select.select",
                return_value=([10], [], []),
            ),
            patch.object(p, "_flush_current_sweep") as flush,
        ):
            p._parser_loop(process)
        flush.assert_called_once_with()
        group.notify_unexpected_eof.assert_called_once_with(
            0,
            "rtl_power stdout reached EOF",
        )

    def test_parser_without_process_returns_cleanly(self):
        p = _make_plugin()
        p._process = None
        p._parser_loop()
