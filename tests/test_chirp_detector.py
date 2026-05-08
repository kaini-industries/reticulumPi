"""Tests for the chirp_detector plugin — standalone LoRa chirp detection."""

from __future__ import annotations

import threading
from collections import deque
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from reticulumpi.builtin_plugins.chirp_detector import (
    ChirpDetector,
    _compute_sf_slopes,
    _process_chunk,
)
from reticulumpi.plugin_base import PluginBase


def _make_app() -> MagicMock:
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.event_bus = MagicMock()
    return app


def _make_detector(config: dict | None = None) -> ChirpDetector:
    """Build a ChirpDetector without starting the stream thread."""
    with patch.object(ChirpDetector, "start"):
        det = ChirpDetector(_make_app(), config or {})

    det._active = True
    det._state_lock = threading.Lock()
    det._stream_lock = threading.Lock()
    det._resolved_index = None
    det._rtl_sdr_path = "/usr/bin/rtl_sdr"
    det._stream_process = None
    det._stream_active = False
    det._stream_thread = None
    det._status = "idle"
    det._waterfall = deque(maxlen=det._waterfall_depth)
    det._sweep_count = 0
    det._db_lo = None
    det._db_hi = None
    fft_size = det._fft_size_for_sr(det._sample_rate)
    det._window = np.hanning(fft_size).astype(np.float32)
    det._trackers = []
    det._channel_filters = {}
    det._detection_history = deque(maxlen=det._detection_history_depth)
    det._packet_history = deque(maxlen=det._packet_history_depth)
    det._last_detection_ts = {}
    det._iq_ring_buffer = None
    det._pending_extractions = deque(maxlen=32)
    det._snapshot_cache = None

    if det._detection_enabled:
        det._init_detection()
    return det


# ---------------------------------------------------------------------------
# Class identity
# ---------------------------------------------------------------------------
class TestClassAttributes:
    def test_plugin_name(self):
        assert ChirpDetector.plugin_name == "chirp_detector"

    def test_inherits_from_plugin_base(self):
        assert issubclass(ChirpDetector, PluginBase)

    def test_does_not_inherit_spectrum_scanner(self):
        from reticulumpi.builtin_plugins.spectrum_scanner import SpectrumScanner
        assert not issubclass(ChirpDetector, SpectrumScanner)


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------
class TestConfigDefaults:
    def test_default_freq(self):
        d = _make_detector()
        assert d._freq_mhz == 903.9

    def test_default_sample_rate(self):
        d = _make_detector()
        assert d._sample_rate == 250_000

    def test_waterfall_off_by_default(self):
        d = _make_detector()
        assert d._waterfall_enabled is False

    def test_detection_on_by_default(self):
        d = _make_detector()
        assert d._detection_enabled is True

    def test_default_waterfall_depth(self):
        d = _make_detector()
        assert d._waterfall_depth == 1024

    def test_config_overrides(self):
        d = _make_detector({
            "freq_mhz": 905.0,
            "sample_rate": 1_024_000,
            "waterfall_enabled": True,
            "detection_enabled": False,
            "gain_db": 30.0,
        })
        assert d._freq_mhz == 905.0
        assert d._sample_rate == 1_024_000
        assert d._waterfall_enabled is True
        assert d._detection_enabled is False
        assert d._gain_db == 30.0


class TestConfigValidation:
    def test_reject_invalid_sample_rate(self):
        with pytest.raises(ValueError, match="sample_rate"):
            _make_detector({"sample_rate": 500_000})

    def test_reject_invalid_sf_in_config(self):
        with pytest.raises(ValueError, match="SF"):
            _make_detector({"detection_sfs": [5]})

    def test_reject_invalid_bw_in_config(self):
        with pytest.raises(ValueError, match="BW"):
            _make_detector({"detection_bws": [100_000]})

    def test_reject_high_gain(self):
        with pytest.raises(ValueError, match="gain_db"):
            _make_detector({"gain_db": 60.0})

    def test_reject_freq_below_range(self):
        with pytest.raises(ValueError, match="freq_mhz"):
            _make_detector({"freq_mhz": 10.0})

    def test_reject_freq_above_range(self):
        with pytest.raises(ValueError, match="freq_mhz"):
            _make_detector({"freq_mhz": 2000.0})

    def test_accept_boundary_freq_low(self):
        d = _make_detector({"freq_mhz": 24.0})
        assert d._freq_mhz == 24.0

    def test_accept_boundary_freq_high(self):
        d = _make_detector({"freq_mhz": 1800.0})
        assert d._freq_mhz == 1800.0


# ---------------------------------------------------------------------------
# FFT size selection
# ---------------------------------------------------------------------------
class TestFftSizeSelection:
    def test_narrow_for_250k(self):
        d = _make_detector({"sample_rate": 250_000})
        assert d._fft_size_for_sr(250_000) == 256

    def test_wide_for_1m(self):
        d = _make_detector({"sample_rate": 1_024_000, "detection_bws": [125_000]})
        assert d._fft_size_for_sr(1_024_000) == 2048

    def test_wide_for_2m(self):
        d = _make_detector({"sample_rate": 2_048_000, "detection_bws": [125_000]})
        assert d._fft_size_for_sr(2_048_000) == 2048


# ---------------------------------------------------------------------------
# Waterfall toggle
# ---------------------------------------------------------------------------
class TestWaterfallToggle:
    def test_enable_waterfall(self):
        d = _make_detector()
        assert d._waterfall_enabled is False
        d.set_waterfall_enabled(True)
        assert d._waterfall_enabled is True

    def test_disable_clears_state(self):
        d = _make_detector({"waterfall_enabled": True})
        d._waterfall.append((1.0, [0.0] * 256))
        d._sweep_count = 10
        d._db_lo = -80.0
        d._db_hi = -30.0

        d.set_waterfall_enabled(False)

        assert d._waterfall_enabled is False
        assert len(d._waterfall) == 0
        assert d._sweep_count == 0
        assert d._db_lo is None

    def test_invalidates_snapshot_cache(self):
        d = _make_detector()
        d._snapshot_cache = (42, {"cached": True})
        d.set_waterfall_enabled(True)
        assert d._snapshot_cache is None


# ---------------------------------------------------------------------------
# STFT chunk processing (module-level function)
# ---------------------------------------------------------------------------
class TestProcessChunk:
    FFT = 256
    HOP = 64
    SR = 250_000

    @property
    def window(self):
        return np.hanning(self.FFT).astype(np.float32)

    def _make_tone_chunk(self, freq_hz=1000, n_samples=2048):
        t = np.arange(n_samples) / self.SR
        iq = np.cos(2 * np.pi * freq_hz * t) + 1j * np.sin(2 * np.pi * freq_hz * t)
        raw = np.empty(n_samples * 2, dtype=np.uint8)
        raw[0::2] = np.clip(np.real(iq) * 100 + 127.5, 0, 255).astype(np.uint8)
        raw[1::2] = np.clip(np.imag(iq) * 100 + 127.5, 0, 255).astype(np.uint8)
        return raw.tobytes()

    def test_basic_shape(self):
        chunk = self._make_tone_chunk(n_samples=2048)
        result = _process_chunk(chunk, self.FFT, self.HOP, self.window, 1)
        assert result is not None
        assert result.ndim == 2
        assert result.shape[1] == self.FFT

    def test_pool_reduces_rows(self):
        chunk = self._make_tone_chunk(n_samples=4096)
        unpooled = _process_chunk(chunk, self.FFT, self.HOP, self.window, 1)
        pooled = _process_chunk(chunk, self.FFT, self.HOP, self.window, 4)
        assert pooled.shape[0] < unpooled.shape[0]

    def test_returns_none_for_short_chunk(self):
        short = b"\x80" * (self.FFT - 1)
        result = _process_chunk(short, self.FFT, self.HOP, self.window, 1)
        assert result is None


# ---------------------------------------------------------------------------
# Ingest rows
# ---------------------------------------------------------------------------
class TestIngestRows:
    def test_appends_to_waterfall(self):
        d = _make_detector({"waterfall_enabled": True})
        rows = np.random.uniform(-80, -30, (4, 256)).astype(np.float32)
        d._ingest_rows(rows, 1000.0)
        assert len(d._waterfall) == 4
        assert d._sweep_count == 4

    def test_updates_db_scale(self):
        d = _make_detector({"waterfall_enabled": True})
        rows = np.random.uniform(-80, -30, (4, 256)).astype(np.float32)
        d._ingest_rows(rows, 1000.0)
        assert d._db_lo is not None
        assert d._db_hi is not None
        assert d._db_lo < d._db_hi

    def test_waterfall_depth_limit(self):
        d = _make_detector({"waterfall_enabled": True, "waterfall_depth": 8})
        d._waterfall = deque(maxlen=8)
        for i in range(12):
            rows = np.random.uniform(-80, -30, (1, 256)).astype(np.float32)
            d._ingest_rows(rows, 1000.0 + i)
        assert len(d._waterfall) == 8


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------
class TestGetSnapshot:
    def test_snapshot_shape(self):
        d = _make_detector()
        snap = d.get_snapshot()
        assert "status" in snap
        assert "streaming" in snap
        assert "waterfall_enabled" in snap
        assert "detection_enabled" in snap
        assert "detection" in snap
        assert snap["waterfall_enabled"] is False

    def test_snapshot_includes_waterfall_tail_when_enabled(self):
        d = _make_detector({"waterfall_enabled": True})
        rows = np.random.uniform(-80, -30, (4, 256)).astype(np.float32)
        d._ingest_rows(rows, 1000.0)
        snap = d.get_snapshot()
        assert "waterfall_tail" in snap
        assert snap["waterfall_tail"]["sweep_count"] == 4

    def test_snapshot_no_waterfall_tail_when_disabled(self):
        d = _make_detector()
        snap = d.get_snapshot()
        assert "waterfall_tail" not in snap

    def test_snapshot_caching(self):
        d = _make_detector()
        s1 = d.get_snapshot()
        s2 = d.get_snapshot()
        assert s1 is s2  # same cache entry


# ---------------------------------------------------------------------------
# Waterfall history
# ---------------------------------------------------------------------------
class TestGetWaterfallHistory:
    def test_unavailable_when_disabled(self):
        d = _make_detector()
        hist = d.get_waterfall_history()
        assert hist["available"] is False
        assert hist["waterfall_enabled"] is False

    def test_available_when_enabled(self):
        d = _make_detector({"waterfall_enabled": True})
        hist = d.get_waterfall_history()
        assert hist["available"] is True
        assert "cols" in hist
        assert "sf_slopes" in hist

    def test_includes_rows_when_populated(self):
        d = _make_detector({"waterfall_enabled": True})
        rows = np.random.uniform(-80, -30, (4, 256)).astype(np.float32)
        d._ingest_rows(rows, 1000.0)
        hist = d.get_waterfall_history()
        assert len(hist["rows"]) == 4
        assert len(hist["row_timestamps"]) == 4


# ---------------------------------------------------------------------------
# Detection stats
# ---------------------------------------------------------------------------
class TestDetectionStats:
    def test_empty_stats(self):
        d = _make_detector()
        stats = d.get_detection_stats()
        assert stats["enabled"] is True
        assert stats["total"] == 0
        assert stats["by_sf"] == {}

    def test_stats_disabled(self):
        d = _make_detector({"detection_enabled": False})
        stats = d.get_detection_stats()
        assert stats["enabled"] is False

    def test_history_depth_limit(self):
        d = _make_detector({"detection_history_depth": 4})
        d._detection_history = deque(maxlen=4)
        for i in range(8):
            d._detection_history.append({"sf": 7, "timestamp": i})
        assert len(d._detection_history) == 4


# ---------------------------------------------------------------------------
# Detection params API
# ---------------------------------------------------------------------------
class TestGetDetectionParams:
    def test_returns_expected_keys(self):
        d = _make_detector()
        params = d.get_detection_params()
        assert "detection_enabled" in params
        assert "detection_sfs" in params
        assert "detection_bws" in params
        assert "detection_snr_threshold_db" in params
        assert "sample_rate" in params


class TestSetDetectionParams:
    def test_update_sfs(self):
        d = _make_detector()
        d.set_detection_params(sfs=[7, 8])
        assert d._detection_sfs == [7, 8]

    def test_update_bws(self):
        d = _make_detector()
        d.set_detection_params(bws=[125_000])
        assert d._detection_bws == [125_000]

    def test_update_snr_threshold(self):
        d = _make_detector()
        d.set_detection_params(snr_threshold_db=15.0)
        assert d._snr_threshold_db == 15.0

    def test_snr_clamped_low(self):
        d = _make_detector()
        d.set_detection_params(snr_threshold_db=-5.0)
        assert d._snr_threshold_db == 0.0

    def test_snr_clamped_high(self):
        d = _make_detector()
        d.set_detection_params(snr_threshold_db=50.0)
        assert d._snr_threshold_db == 40.0

    def test_disable_clears_trackers(self):
        d = _make_detector()
        assert len(d._trackers) > 0
        d.set_detection_params(enabled=False)
        assert len(d._trackers) == 0

    def test_enable_reinits_trackers(self):
        d = _make_detector({"detection_enabled": False})
        d.set_detection_params(enabled=True, sfs=[7, 8], bws=[125_000])
        assert len(d._trackers) == 1

    def test_reject_invalid_sf(self):
        d = _make_detector()
        with pytest.raises(ValueError, match="SF"):
            d.set_detection_params(sfs=[5])

    def test_reject_unknown_bw(self):
        d = _make_detector()
        with pytest.raises(ValueError, match="BW"):
            d.set_detection_params(bws=[100_000])

    def test_reject_bw_ge_sample_rate(self):
        d = _make_detector()
        with pytest.raises(ValueError, match="must be <"):
            d.set_detection_params(bws=[250_000])

    def test_reject_enable_with_empty_sfs(self):
        d = _make_detector()
        with pytest.raises(ValueError, match="empty"):
            d.set_detection_params(enabled=True, sfs=[])

    def test_invalidates_snapshot_cache(self):
        d = _make_detector()
        d._snapshot_cache = (42, {"cached": True})
        d.set_detection_params(sfs=[7, 8])
        assert d._snapshot_cache is None

    def test_preamble_len_bounds(self):
        d = _make_detector()
        with pytest.raises(ValueError, match="preamble_len"):
            d.set_detection_params(preamble_len=2)
        with pytest.raises(ValueError, match="preamble_len"):
            d.set_detection_params(preamble_len=40)


# ---------------------------------------------------------------------------
# Continuous params
# ---------------------------------------------------------------------------
class TestSetContinuousParams:
    def test_updates_freq(self):
        d = _make_detector()
        d.set_continuous_params(freq_mhz=910.0)
        assert d._freq_mhz == 910.0

    def test_updates_sample_rate(self):
        d = _make_detector()
        d.set_continuous_params(sample_rate=1_024_000)
        assert d._sample_rate == 1_024_000

    def test_prunes_bws_on_sample_rate_downgrade(self):
        d = _make_detector({"sample_rate": 2_048_000, "detection_bws": [125_000, 250_000, 500_000]})
        d.set_continuous_params(sample_rate=250_000)
        assert d._sample_rate == 250_000
        assert d._detection_bws == [125_000]

    def test_rejects_invalid_sample_rate(self):
        d = _make_detector()
        with pytest.raises(ValueError, match="sample_rate"):
            d.set_continuous_params(sample_rate=500_000)

    def test_rejects_freq_below_range(self):
        d = _make_detector()
        with pytest.raises(ValueError, match="freq_mhz"):
            d.set_continuous_params(freq_mhz=10.0)

    def test_rejects_freq_above_range(self):
        d = _make_detector()
        with pytest.raises(ValueError, match="freq_mhz"):
            d.set_continuous_params(freq_mhz=2000.0)


# ---------------------------------------------------------------------------
# SF slopes helper
# ---------------------------------------------------------------------------
class TestComputeSfSlopes:
    def test_returns_six_entries(self):
        slopes = _compute_sf_slopes(250_000, 256, 64, 1, bw=125_000)
        assert len(slopes) == 6

    def test_slope_keys(self):
        slopes = _compute_sf_slopes(250_000, 256, 64, 1, bw=125_000)
        for s in slopes:
            assert "sf" in s
            assert "t_symbol_ms" in s
            assert "bins_per_frame" in s

    def test_higher_sf_means_longer_symbol(self):
        slopes = _compute_sf_slopes(250_000, 256, 64, 1, bw=125_000)
        for i in range(len(slopes) - 1):
            assert slopes[i + 1]["t_symbol_ms"] > slopes[i]["t_symbol_ms"]


# ---------------------------------------------------------------------------
# Detection pipeline (_run_detection)
# ---------------------------------------------------------------------------
class TestRunDetection:
    def test_feeds_trackers_and_publishes_detection(self):
        d = _make_detector()
        assert len(d._trackers) > 0

        mock_det = MagicMock()
        mock_det.sf = 7
        mock_det.bw = 125_000
        mock_det.timestamp = 1000.0
        mock_det.freq_offset_hz = 0.0
        mock_det.freq_offset_bin = 0
        mock_det.snr_db = 15.0
        mock_det.confidence = 0.9
        mock_det.noise_floor_db = -100.0
        mock_det.threshold_db = -88.0
        mock_det.sample_offset = 0

        for bw, tracker, _ext in d._trackers:
            tracker.feed_chunk = MagicMock(return_value=[mock_det])

        sr = d._sample_rate
        n_samples = 1024
        raw = np.random.randint(0, 256, size=n_samples * 2, dtype=np.uint8)
        d._run_detection(raw.tobytes(), 1000.0)

        assert len(d._detection_history) >= 1
        det_entry = d._detection_history[0]
        assert det_entry["sf"] == 7
        assert det_entry["snr_db"] == 15.0

        publish_calls = d.app.event_bus.publish.call_args_list
        chirp_calls = [c for c in publish_calls if c[0][0] == "chirp.detection"]
        assert len(chirp_calls) >= 1

    def test_cooldown_prevents_duplicate_detections(self):
        d = _make_detector()

        mock_det = MagicMock()
        mock_det.sf = 7
        mock_det.bw = 125_000
        mock_det.timestamp = 1000.0
        mock_det.freq_offset_hz = 0.0
        mock_det.freq_offset_bin = 0
        mock_det.snr_db = 15.0
        mock_det.confidence = 0.9
        mock_det.noise_floor_db = -100.0
        mock_det.threshold_db = -88.0
        mock_det.sample_offset = 0

        for bw, tracker, _ext in d._trackers:
            tracker.feed_chunk = MagicMock(return_value=[mock_det])

        raw = np.random.randint(0, 256, size=2048, dtype=np.uint8).tobytes()

        d._run_detection(raw, 1000.0)
        d._run_detection(raw, 1000.1)

        assert len(d._detection_history) == 1

    def test_detection_with_trackers_empty_is_noop(self):
        d = _make_detector({"detection_enabled": False})
        assert d._trackers == []
        raw = np.random.randint(0, 256, size=2048, dtype=np.uint8).tobytes()
        d._run_detection(raw, 1000.0)
        assert len(d._detection_history) == 0
