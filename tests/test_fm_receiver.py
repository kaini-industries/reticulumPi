"""Tests for the fm_receiver plugin.

Focuses on config validation, command construction, state management,
signal level computation, and snapshot format — without touching real
RTL-SDR hardware or spawning rtl_fm.
"""

from __future__ import annotations

import math
import struct
import threading
from unittest.mock import MagicMock

import pytest

from reticulumpi.builtin_plugins.fm_receiver import (
    _CHUNK_BYTES,
    _MODE_DEFAULTS,
    FMReceiver,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def _make_app() -> MagicMock:
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.event_bus = MagicMock()
    return app


def _make_plugin(config: dict | None = None) -> FMReceiver:
    """Construct an FMReceiver without calling start() (no threads)."""
    plugin = FMReceiver(_make_app(), config or {})
    plugin._state_lock = threading.Lock()
    plugin._stream_lock = threading.Lock()
    plugin._process = None
    plugin._pid = None
    plugin._restart_count = 0
    plugin._rtl_fm_path = "/usr/bin/rtl_fm"
    plugin._last_error = None
    plugin._status = "stopped"
    plugin._playing = False
    plugin._resolved_index = 0
    plugin._supervisor_alive = False
    plugin._signal_rms = 0.0
    plugin._signal_db = -90.0
    plugin._dead_zone_warning = None
    plugin._stream_queues = []
    plugin._event_loop = None
    return plugin


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------
class TestValidateConfig:
    def test_defaults(self):
        p = _make_plugin()
        assert p._frequency_hz == 95_500_000
        assert p._mode == "wbfm"
        assert p._gain_db is None
        assert p._squelch_level == 0
        assert p._volume == 0.75
        assert p._ppm == 0
        assert p._enable_bias_tee is False
        assert p._max_restarts == 5
        assert p._auto_play is False
        assert p._freq_min_mhz == 52.0
        assert p._freq_max_mhz == 2200.0

    def test_custom_config(self):
        p = _make_plugin({
            "default_frequency_mhz": 121.5,
            "default_mode": "am",
            "gain_db": 34.0,
            "squelch_level": 50,
            "default_volume": 50,
            "ppm": 3,
            "enable_bias_tee": True,
            "max_restarts": 10,
            "freq_min_mhz": 24.0,
            "freq_max_mhz": 1766.0,
        })
        assert p._frequency_hz == 121_500_000
        assert p._mode == "am"
        assert p._gain_db == 34.0
        assert p._squelch_level == 50
        assert p._volume == 0.5
        assert p._ppm == 3
        assert p._enable_bias_tee is True
        assert p._max_restarts == 10
        assert p._freq_min_mhz == 24.0
        assert p._freq_max_mhz == 1766.0

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError, match="default_mode"):
            _make_plugin({"default_mode": "cw"})

    def test_frequency_below_range_rejected(self):
        with pytest.raises(ValueError, match="outside tuner range"):
            _make_plugin({"default_frequency_mhz": 10.0})

    def test_frequency_above_range_rejected(self):
        with pytest.raises(ValueError, match="outside tuner range"):
            _make_plugin({"default_frequency_mhz": 3000.0})

    def test_gain_out_of_range_rejected(self):
        with pytest.raises(ValueError, match="gain_db"):
            _make_plugin({"gain_db": 100.0})

    def test_null_gain_means_auto(self):
        p = _make_plugin({"gain_db": None})
        assert p._gain_db is None

    def test_mode_sets_sample_rates(self):
        for mode, defaults in _MODE_DEFAULTS.items():
            p = _make_plugin({"default_mode": mode})
            assert p._sample_rate_hz == defaults["sample_rate_hz"]
            assert p._output_rate_hz == defaults["output_rate_hz"]

    def test_user_presets_merge(self):
        p = _make_plugin({
            "presets": {
                "my_band": {
                    "label": "Custom",
                    "mode": "am",
                    "frequencies": [{"freq_mhz": 100.0, "label": "Test"}],
                }
            }
        })
        assert "my_band" in p._presets
        assert p._presets["my_band"]["label"] == "Custom"
        assert "aviation" in p._presets

    def test_volume_clamped(self):
        p = _make_plugin({"default_volume": 200})
        assert p._volume == 1.0
        p = _make_plugin({"default_volume": -10})
        assert p._volume == 0.0


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------
class TestBuildCmd:
    def test_wbfm_command(self):
        p = _make_plugin({"default_mode": "wbfm"})
        cmd = p._build_cmd()
        assert cmd[0] == "/usr/bin/rtl_fm"
        assert "-f" in cmd
        assert "95500000" in cmd
        assert "-M" in cmd
        assert "wbfm" in cmd
        assert "-E" in cmd
        assert "deemp" in cmd
        assert cmd[-1] == "-"

    def test_am_command_no_deemp(self):
        p = _make_plugin({"default_mode": "am"})
        cmd = p._build_cmd()
        assert "am" in cmd
        assert "deemp" not in cmd

    def test_gain_included_when_set(self):
        p = _make_plugin({"gain_db": 34.0})
        cmd = p._build_cmd()
        assert "-g" in cmd
        idx = cmd.index("-g")
        assert cmd[idx + 1] == "34.0"

    def test_gain_omitted_when_auto(self):
        p = _make_plugin({"gain_db": None})
        cmd = p._build_cmd()
        assert "-g" not in cmd

    def test_bias_tee_flag(self):
        p = _make_plugin({"enable_bias_tee": True})
        cmd = p._build_cmd()
        assert "-T" in cmd

    def test_no_bias_tee_by_default(self):
        p = _make_plugin()
        cmd = p._build_cmd()
        assert "-T" not in cmd

    def test_device_index_included(self):
        p = _make_plugin()
        cmd = p._build_cmd()
        assert "-d" in cmd
        idx = cmd.index("-d")
        assert cmd[idx + 1] == "0"

    def test_output_rate_included_when_different(self):
        p = _make_plugin({"default_mode": "wbfm"})
        cmd = p._build_cmd()
        assert "-r" in cmd

    def test_output_rate_omitted_when_same(self):
        p = _make_plugin({"default_mode": "am"})
        cmd = p._build_cmd()
        assert "-r" not in cmd


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------
class TestStateManagement:
    def test_tune_updates_frequency(self):
        p = _make_plugin()
        result = p.tune(100_000_000)
        assert p._frequency_hz == 100_000_000
        assert result["frequency_mhz"] == 100.0

    def test_tune_updates_mode(self):
        p = _make_plugin()
        p.tune(121_500_000, mode="am")
        assert p._mode == "am"
        assert p._sample_rate_hz == _MODE_DEFAULTS["am"]["sample_rate_hz"]

    def test_tune_invalid_mode_rejected(self):
        p = _make_plugin()
        with pytest.raises(ValueError, match="Invalid mode"):
            p.tune(100_000_000, mode="cw")

    def test_tune_out_of_range_rejected(self):
        p = _make_plugin()
        with pytest.raises(ValueError, match="outside tuner range"):
            p.tune(5_000_000)

    def test_set_volume_clamps(self):
        p = _make_plugin()
        p.set_volume(1.5)
        assert p._volume == 1.0
        p.set_volume(-0.5)
        assert p._volume == 0.0
        p.set_volume(0.5)
        assert p._volume == 0.5

    def test_set_gain_validates(self):
        p = _make_plugin()
        with pytest.raises(ValueError, match="gain_db"):
            p.set_gain(100.0)

    def test_set_gain_null(self):
        p = _make_plugin()
        result = p.set_gain(None)
        assert p._gain_db is None
        assert result["gain_db"] is None

    def test_set_squelch(self):
        p = _make_plugin()
        result = p.set_squelch(100)
        assert p._squelch_level == 100
        assert result["squelch_level"] == 100

    def test_set_squelch_negative_clamped(self):
        p = _make_plugin()
        p.set_squelch(-10)
        assert p._squelch_level == 0


# ---------------------------------------------------------------------------
# Dead zone detection
# ---------------------------------------------------------------------------
class TestDeadZone:
    def test_frequency_in_dead_zone(self):
        p = _make_plugin()
        warning = p._check_dead_zone(1150.0)
        assert warning is not None
        assert "E4000" in warning

    def test_frequency_outside_dead_zone(self):
        p = _make_plugin()
        assert p._check_dead_zone(95.5) is None
        assert p._check_dead_zone(1100.0) is None
        assert p._check_dead_zone(1235.0) is None

    def test_frequency_at_dead_zone_boundary(self):
        p = _make_plugin()
        assert p._check_dead_zone(1101.0) is not None
        assert p._check_dead_zone(1234.0) is not None

    def test_tune_sets_dead_zone_warning(self):
        p = _make_plugin({"freq_min_mhz": 52.0, "freq_max_mhz": 2200.0})
        p.tune(1_150_000_000)
        assert p._dead_zone_warning is not None
        p.tune(95_500_000)
        assert p._dead_zone_warning is None


# ---------------------------------------------------------------------------
# Signal level computation
# ---------------------------------------------------------------------------
class TestSignalLevel:
    def test_zero_signal(self):
        p = _make_plugin()
        chunk = struct.pack(f"<{_CHUNK_BYTES // 2}h", *([0] * (_CHUNK_BYTES // 2)))
        p._update_signal_level(chunk)
        assert p._signal_rms == 0.0
        assert p._signal_db == -90.0

    def test_full_scale_signal(self):
        p = _make_plugin()
        n = _CHUNK_BYTES // 2
        samples = [32767] * n
        chunk = struct.pack(f"<{n}h", *samples)
        p._update_signal_level(chunk)
        assert p._signal_rms > 32000
        assert p._signal_db > 0

    def test_moderate_signal(self):
        p = _make_plugin()
        n = _CHUNK_BYTES // 2
        samples = [1000] * n
        chunk = struct.pack(f"<{n}h", *samples)
        p._update_signal_level(chunk)
        expected_rms = 1000.0
        assert abs(p._signal_rms - expected_rms) < 1.0
        expected_db = 20.0 * math.log10(1000.0) - 90.0
        assert abs(p._signal_db - expected_db) < 0.1

    def test_empty_chunk_no_crash(self):
        p = _make_plugin()
        p._update_signal_level(b"")
        assert p._signal_rms == 0.0


# ---------------------------------------------------------------------------
# Volume application
# ---------------------------------------------------------------------------
class TestApplyVolume:
    def test_volume_zero_silences(self):
        samples = [1000, -1000, 500]
        chunk = struct.pack("<3h", *samples)
        result = FMReceiver._apply_volume(chunk, 0.0)
        out = struct.unpack("<3h", result)
        assert all(s == 0 for s in out)

    def test_volume_half(self):
        samples = [1000, -1000]
        chunk = struct.pack("<2h", *samples)
        result = FMReceiver._apply_volume(chunk, 0.5)
        out = struct.unpack("<2h", result)
        assert out[0] == 500
        assert out[1] == -500

    def test_volume_one_passthrough(self):
        samples = [12345, -6789]
        chunk = struct.pack("<2h", *samples)
        result = FMReceiver._apply_volume(chunk, 1.0)
        out = struct.unpack("<2h", result)
        assert out == (12345, -6789)

    def test_volume_clamps_overflow(self):
        samples = [32767]
        chunk = struct.pack("<1h", *samples)
        result = FMReceiver._apply_volume(chunk, 1.5)
        out = struct.unpack("<1h", result)
        assert out[0] == 32767


# ---------------------------------------------------------------------------
# Snapshot format
# ---------------------------------------------------------------------------
class TestSnapshot:
    def test_snapshot_shape(self):
        p = _make_plugin()
        snap = p.get_snapshot()
        expected_keys = {
            "status", "playing", "frequency_hz", "frequency_mhz",
            "mode", "gain_db", "squelch_level", "volume",
            "signal_rms", "signal_db", "output_rate_hz",
            "freq_min_mhz", "freq_max_mhz",
            "restart_count", "error", "dead_zone_warning",
            "audio_clients",
        }
        assert expected_keys.issubset(set(snap.keys()))

    def test_snapshot_reflects_state(self):
        p = _make_plugin({"default_frequency_mhz": 121.5, "default_mode": "am"})
        snap = p.get_snapshot()
        assert snap["frequency_hz"] == 121_500_000
        assert snap["frequency_mhz"] == 121.5
        assert snap["mode"] == "am"
        assert snap["playing"] is False
        assert snap["status"] == "stopped"


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------
class TestPresets:
    def test_builtin_presets_available(self):
        p = _make_plugin()
        presets = p.get_presets()
        assert "aviation" in presets
        assert "weather" in presets
        assert "marine" in presets
        assert "two_meter_ham" in presets
        assert "seventy_cm_ham" in presets

    def test_preset_structure(self):
        p = _make_plugin()
        presets = p.get_presets()
        fm = presets["aviation"]
        assert "label" in fm
        assert "mode" in fm
        assert "frequencies" in fm
        assert isinstance(fm["frequencies"], list)

    def test_user_preset_merged(self):
        p = _make_plugin({
            "presets": {
                "custom": {
                    "label": "Custom Band",
                    "mode": "am",
                    "frequencies": [{"freq_mhz": 200.0, "label": "Test"}],
                }
            }
        })
        presets = p.get_presets()
        assert "custom" in presets
        assert presets["custom"]["label"] == "Custom Band"
        assert "aviation" in presets

    def test_weather_preset_frequencies(self):
        p = _make_plugin()
        presets = p.get_presets()
        wx = presets["weather"]
        freqs = [f["freq_mhz"] for f in wx["frequencies"]]
        assert 162.4 in freqs
        assert 162.55 in freqs
        assert len(freqs) == 7


# ---------------------------------------------------------------------------
# Play / stop (mocked, no subprocess)
# ---------------------------------------------------------------------------
class TestPlayStop:
    def test_play_without_device_returns_error(self):
        p = _make_plugin()
        p._resolved_index = None
        result = p.play()
        assert result["status"] == "error"

    def test_play_already_playing(self):
        p = _make_plugin()
        p._playing = True
        result = p.play()
        assert result["status"] == "already_playing"

    def test_stop_when_stopped(self):
        p = _make_plugin()
        result = p.stop_playback()
        assert result["status"] == "already_stopped"
