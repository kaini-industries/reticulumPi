"""Tests for the fm_receiver plugin.

Focuses on config validation, command construction, state management,
signal level computation, and snapshot format — without touching real
RTL-SDR hardware or spawning rtl_fm.
"""

from __future__ import annotations

import json
import math
import os
import struct
import threading
from collections import deque
from unittest.mock import MagicMock

import pytest

from reticulumpi.builtin_plugins.fm_receiver import (
    _CHUNK_BYTES,
    _FAVORITES_FILENAME,
    _MODE_DEFAULTS,
    _RECORDINGS_DIR,
    _STATE_FILENAME,
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
    plugin._signal_history = deque(maxlen=300)
    plugin._squelch_break_count = 0
    plugin._squelch_was_open = False
    plugin._last_signal_history_ts = 0.0
    plugin._favorites = []
    plugin._recording = False
    plugin._recording_file = None
    plugin._recording_path = None
    plugin._recording_start_ts = None
    plugin._recording_bytes = 0
    plugin._recording_label = None
    plugin._rec_lock = threading.Lock()
    cfg = config or {}
    plugin._max_recording_seconds = int(cfg.get("max_recording_seconds", 3600))
    plugin._max_recording_size_bytes = (
        int(cfg.get("max_recording_size_mb", 500)) * 1024 * 1024
    )
    plugin._max_recordings = int(cfg.get("max_recordings", 50))
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


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
class TestStatePersistence:
    def test_persist_creates_state_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin({"default_frequency_mhz": 101.1, "default_mode": "wbfm"})
        p._persist_state()
        path = tmp_path / "reticulumpi" / "fm_receiver" / _STATE_FILENAME
        assert path.exists()
        state = json.loads(path.read_text())
        assert state["frequency_mhz"] == 101.1
        assert state["mode"] == "wbfm"
        assert state["gain_db"] is None
        assert state["squelch_level"] == 0
        assert state["volume"] == 0.75
        assert "timestamp" in state

    def test_load_restores_frequency_and_mode(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        state_dir = tmp_path / "reticulumpi" / "fm_receiver"
        state_dir.mkdir(parents=True)
        (state_dir / _STATE_FILENAME).write_text(json.dumps({
            "frequency_mhz": 121.5,
            "mode": "am",
            "gain_db": 34.0,
            "squelch_level": 50,
            "volume": 0.6,
        }))
        p = _make_plugin()
        p._load_state()
        assert p._frequency_hz == 121_500_000
        assert p._mode == "am"
        assert p._gain_db == 34.0
        assert p._squelch_level == 50
        assert p._volume == 0.6
        assert p._sample_rate_hz == _MODE_DEFAULTS["am"]["sample_rate_hz"]
        assert p._output_rate_hz == _MODE_DEFAULTS["am"]["output_rate_hz"]

    def test_load_skips_invalid_frequency(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        state_dir = tmp_path / "reticulumpi" / "fm_receiver"
        state_dir.mkdir(parents=True)
        (state_dir / _STATE_FILENAME).write_text(json.dumps({
            "frequency_mhz": 9999.0,
            "mode": "wbfm",
        }))
        p = _make_plugin()
        orig_freq = p._frequency_hz
        p._load_state()
        assert p._frequency_hz == orig_freq

    def test_load_skips_invalid_mode(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        state_dir = tmp_path / "reticulumpi" / "fm_receiver"
        state_dir.mkdir(parents=True)
        (state_dir / _STATE_FILENAME).write_text(json.dumps({
            "mode": "cw",
        }))
        p = _make_plugin()
        p._load_state()
        assert p._mode == "wbfm"

    def test_load_handles_corrupted_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        state_dir = tmp_path / "reticulumpi" / "fm_receiver"
        state_dir.mkdir(parents=True)
        (state_dir / _STATE_FILENAME).write_text("{broken json")
        p = _make_plugin()
        p._load_state()
        assert p._frequency_hz == 95_500_000

    def test_load_handles_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        p._load_state()
        assert p._frequency_hz == 95_500_000

    def test_load_null_gain_restores_auto(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        state_dir = tmp_path / "reticulumpi" / "fm_receiver"
        state_dir.mkdir(parents=True)
        (state_dir / _STATE_FILENAME).write_text(json.dumps({
            "gain_db": None,
        }))
        p = _make_plugin({"gain_db": 20.0})
        p._load_state()
        assert p._gain_db is None

    def test_load_skips_out_of_range_gain(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        state_dir = tmp_path / "reticulumpi" / "fm_receiver"
        state_dir.mkdir(parents=True)
        (state_dir / _STATE_FILENAME).write_text(json.dumps({
            "gain_db": 100.0,
        }))
        p = _make_plugin()
        p._load_state()
        assert p._gain_db is None

    def test_tune_persists_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        p.tune(100_000_000, mode="am")
        path = tmp_path / "reticulumpi" / "fm_receiver" / _STATE_FILENAME
        assert path.exists()
        state = json.loads(path.read_text())
        assert state["frequency_mhz"] == 100.0
        assert state["mode"] == "am"

    def test_set_volume_persists_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        p.set_volume(0.4)
        path = tmp_path / "reticulumpi" / "fm_receiver" / _STATE_FILENAME
        state = json.loads(path.read_text())
        assert state["volume"] == 0.4

    def test_set_gain_persists_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        p.set_gain(20.0)
        path = tmp_path / "reticulumpi" / "fm_receiver" / _STATE_FILENAME
        state = json.loads(path.read_text())
        assert state["gain_db"] == 20.0

    def test_set_squelch_persists_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        p.set_squelch(75)
        path = tmp_path / "reticulumpi" / "fm_receiver" / _STATE_FILENAME
        state = json.loads(path.read_text())
        assert state["squelch_level"] == 75

    def test_roundtrip_persist_and_load(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p1 = _make_plugin()
        p1.tune(146_520_000, mode="fm")
        p1.set_gain(24.0)
        p1.set_squelch(100)
        p1.set_volume(0.3)
        p2 = _make_plugin()
        p2._load_state()
        assert p2._frequency_hz == 146_520_000
        assert p2._mode == "fm"
        assert p2._gain_db == 24.0
        assert p2._squelch_level == 100
        assert p2._volume == 0.3


# ---------------------------------------------------------------------------
# Favorites
# ---------------------------------------------------------------------------
class TestFavorites:
    def test_add_favorite(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        fav = p.add_favorite("My Station", 95.5, "wbfm")
        assert fav["label"] == "My Station"
        assert fav["frequency_mhz"] == 95.5
        assert fav["mode"] == "wbfm"
        assert "id" in fav
        assert len(p.get_favorites()) == 1

    def test_add_favorite_persists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        p.add_favorite("Test", 100.0, "fm")
        path = tmp_path / "reticulumpi" / "fm_receiver" / _FAVORITES_FILENAME
        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data) == 1
        assert data[0]["label"] == "Test"

    def test_remove_favorite(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        fav = p.add_favorite("Remove Me", 88.0, "wbfm")
        assert p.remove_favorite(fav["id"])
        assert len(p.get_favorites()) == 0

    def test_remove_nonexistent_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        assert p.remove_favorite("nonexistent-id") is False

    def test_update_favorite(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        fav = p.add_favorite("Old Name", 95.5, "wbfm")
        updated = p.update_favorite(fav["id"], label="New Name")
        assert updated["label"] == "New Name"
        assert p.get_favorites()[0]["label"] == "New Name"

    def test_update_nonexistent_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        assert p.update_favorite("no-such-id", label="X") is None

    def test_add_favorite_invalid_mode(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        with pytest.raises(ValueError, match="Invalid mode"):
            p.add_favorite("Bad", 95.5, "cw")

    def test_add_favorite_invalid_freq(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        with pytest.raises(ValueError, match="outside tuner range"):
            p.add_favorite("Bad", 9999.0, "wbfm")

    def test_max_favorites_enforced(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin({"max_favorites": 2})
        p.add_favorite("A", 88.0, "wbfm")
        p.add_favorite("B", 89.0, "wbfm")
        with pytest.raises(ValueError, match="Maximum favorites"):
            p.add_favorite("C", 90.0, "wbfm")

    def test_tune_favorite(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        fav = p.add_favorite("Aviation Guard", 121.5, "am")
        result = p.tune_favorite(fav["id"])
        assert p._frequency_hz == 121_500_000
        assert p._mode == "am"
        assert result["frequency_mhz"] == 121.5

    def test_tune_favorite_updates_last_used(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        fav = p.add_favorite("Test", 95.5, "wbfm")
        assert fav["last_used_at"] is None
        p.tune_favorite(fav["id"])
        updated = p.get_favorites()[0]
        assert updated["last_used_at"] is not None

    def test_tune_nonexistent_favorite(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        with pytest.raises(ValueError, match="not found"):
            p.tune_favorite("no-such-id")

    def test_load_favorites_from_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        fav_dir = tmp_path / "reticulumpi" / "fm_receiver"
        fav_dir.mkdir(parents=True)
        (fav_dir / _FAVORITES_FILENAME).write_text(json.dumps([
            {"id": "abc", "label": "Saved", "frequency_mhz": 101.1, "mode": "wbfm"},
        ]))
        p = _make_plugin()
        p._load_favorites()
        assert len(p.get_favorites()) == 1
        assert p.get_favorites()[0]["label"] == "Saved"

    def test_load_favorites_handles_corrupt_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        fav_dir = tmp_path / "reticulumpi" / "fm_receiver"
        fav_dir.mkdir(parents=True)
        (fav_dir / _FAVORITES_FILENAME).write_text("{bad json")
        p = _make_plugin()
        p._load_favorites()
        assert p.get_favorites() == []


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------
class TestRecording:
    def test_start_recording_requires_playing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        result = p.start_recording()
        assert result["recording"] is False
        assert "Not playing" in result["error"]

    def test_start_recording_creates_wav(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        p._playing = True
        result = p.start_recording()
        assert result["recording"] is True
        assert result["filename"].endswith(".wav")
        assert p._recording is True
        rec_dir = tmp_path / "reticulumpi" / "fm_receiver" / _RECORDINGS_DIR
        wav_files = list(rec_dir.glob("*.wav"))
        assert len(wav_files) == 1
        # Header should be 44 bytes
        assert wav_files[0].stat().st_size == 44
        p.stop_recording()

    def test_stop_recording_patches_header(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        p._playing = True
        p.start_recording()
        # Write some fake audio data
        with p._rec_lock:
            p._recording_file.write(b"\x00" * 1000)
            p._recording_bytes = 1000
        result = p.stop_recording()
        assert result["recording"] is False
        assert result["size_bytes"] == 1000
        # Check WAV header was patched
        rec_dir = tmp_path / "reticulumpi" / "fm_receiver" / _RECORDINGS_DIR
        wav_file = list(rec_dir.glob("*.wav"))[0]
        data = wav_file.read_bytes()
        riff_size = struct.unpack("<I", data[4:8])[0]
        assert riff_size == 36 + 1000
        data_size = struct.unpack("<I", data[40:44])[0]
        assert data_size == 1000

    def test_recording_already_active(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        p._playing = True
        p.start_recording()
        result = p.start_recording()
        assert "Already recording" in result["error"]
        p.stop_recording()

    def test_stop_when_not_recording(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        result = p.stop_recording()
        assert result["recording"] is False

    def test_tune_auto_stops_recording(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        p._playing = True
        p.start_recording()
        assert p._recording is True
        p.tune(100_000_000)
        assert p._recording is False

    def test_stop_playback_auto_stops_recording(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        p._playing = True
        p.start_recording()
        assert p._recording is True
        p.stop_playback()
        assert p._recording is False

    def test_get_recordings_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        assert p.get_recordings() == []

    def test_get_recordings_lists_files(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        p._playing = True
        p.start_recording()
        with p._rec_lock:
            p._recording_file.write(b"\x00" * 500)
            p._recording_bytes = 500
        p.stop_recording()
        recs = p.get_recordings()
        assert len(recs) == 1
        assert recs[0]["filename"].endswith(".wav")
        assert recs[0]["size_bytes"] == 44 + 500

    def test_delete_recording(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        p._playing = True
        p.start_recording()
        p.stop_recording()
        recs = p.get_recordings()
        assert len(recs) == 1
        assert p.delete_recording(recs[0]["filename"]) is True
        assert p.get_recordings() == []

    def test_delete_recording_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        assert p.delete_recording("nonexistent.wav") is False

    def test_delete_recording_path_traversal(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        with pytest.raises(ValueError, match="Invalid filename"):
            p.delete_recording("../../../etc/passwd")
        with pytest.raises(ValueError, match="Invalid filename"):
            p.delete_recording("test.txt")

    def test_max_recordings_enforced(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin({"max_recordings": 2})
        p._playing = True
        p.start_recording()
        p.stop_recording()
        import time
        time.sleep(0.01)
        p.start_recording()
        p.stop_recording()
        time.sleep(0.01)
        result = p.start_recording()
        assert result["recording"] is False
        assert "Maximum recordings" in result["error"]

    def test_snapshot_includes_recording(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        snap = p.get_snapshot()
        assert snap["recording"] is None
        p._playing = True
        p.start_recording()
        snap = p.get_snapshot()
        assert snap["recording"]["active"] is True
        assert snap["recording"]["filename"].endswith(".wav")
        p.stop_recording()

    def test_recording_filename_format(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin({"default_frequency_mhz": 101.1, "default_mode": "wbfm"})
        p._playing = True
        result = p.start_recording()
        fn = result["filename"]
        assert "101.100MHz" in fn
        assert "wbfm" in fn
        assert fn.endswith(".wav")
        p.stop_recording()

    def test_get_recording_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        p._playing = True
        p.start_recording()
        p.stop_recording()
        recs = p.get_recordings()
        path = p.get_recording_path(recs[0]["filename"])
        assert path is not None
        assert os.path.isfile(path)

    def test_get_recording_path_traversal_blocked(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        assert p.get_recording_path("../../etc/passwd") is None
        assert p.get_recording_path("test.txt") is None

    def test_write_recording_chunk(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        p._playing = True
        p.start_recording()
        chunk = b"\x01\x00" * 100
        p._write_recording_chunk(chunk)
        assert p._recording_bytes == 200
        p.stop_recording()
        recs = p.get_recordings()
        assert recs[0]["size_bytes"] == 44 + 200

    def test_write_recording_chunk_auto_stops_on_size_limit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        p._max_recording_size_bytes = 100
        p._playing = True
        p.start_recording()
        p._write_recording_chunk(b"\x00" * 50)
        assert p._recording is True
        p._write_recording_chunk(b"\x00" * 60)
        assert p._recording is False

    def test_write_recording_chunk_auto_stops_on_time_limit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        p = _make_plugin()
        p._max_recording_seconds = 0
        p._playing = True
        p.start_recording()
        import time
        time.sleep(0.01)
        p._write_recording_chunk(b"\x00" * 10)
        assert p._recording is False
