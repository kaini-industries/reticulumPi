"""Tests for the lora_chirp_viewer plugin — continuous chirp streaming waterfall."""

from __future__ import annotations

import threading
from collections import deque
from unittest.mock import MagicMock

import numpy as np
import pytest

from reticulumpi import events
from reticulumpi.builtin_plugins.lora_chirp_viewer import (
    LoraChirpViewer,
    _compute_sf_slopes,
)
from tests.test_lora_dechirp import make_preamble


def _make_app() -> MagicMock:
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.event_bus = MagicMock()
    return app


def _make_plugin(config: dict | None = None) -> LoraChirpViewer:
    plugin = LoraChirpViewer(_make_app(), config or {})
    plugin._state_lock = threading.Lock()
    plugin._process = None
    plugin._pid = None
    plugin._restart_count = 0
    plugin._sweep_count = 0
    plugin._last_sweep_at = None
    plugin._rtl_power_path = "/usr/bin/rtl_power"
    plugin._rtl_sdr_path = "/usr/bin/rtl_sdr"
    plugin._last_error = None
    plugin._status = "starting"
    plugin._event_sweep_topic = events.LORA_SCANNER_SWEEP
    plugin._event_status_topic = events.LORA_SCANNER_STATUS
    plugin._bins_hz = []
    plugin._latest_powers_db = []
    plugin._waterfall = deque(maxlen=plugin._waterfall_rows)
    plugin._segments = {}
    plugin._current_ts = None
    plugin._device_released = False
    plugin._supervisor_alive = False
    plugin._active = True

    # Continuous streaming state
    plugin._stream_process = None
    plugin._stream_active = False
    plugin._stream_lock = threading.Lock()
    plugin._chirp_status = "idle"
    plugin._chirp_waterfall = deque(maxlen=plugin._chirp_wf_depth)
    plugin._chirp_sweep_count = 0
    plugin._db_lo = None
    plugin._db_hi = None
    fft_size = plugin._fft_size_for_sr(plugin._chirp_sr)
    plugin._window = np.hanning(fft_size).astype(np.float32)

    # Detection state
    plugin._preamble_tracker = None
    plugin._detection_history = deque(maxlen=plugin._detection_history_depth)
    plugin._packet_history = deque(maxlen=plugin._packet_history_depth)
    plugin._last_detection_ts = {}
    if plugin._detection_enabled:
        plugin._init_detection()
    return plugin


class TestClassAttributes:
    def test_plugin_name(self):
        assert LoraChirpViewer.plugin_name == "lora_chirp_viewer"

    def test_extends_lora_scanner(self):
        from reticulumpi.builtin_plugins.lora_scanner import LoraScanner
        assert issubclass(LoraChirpViewer, LoraScanner)


class TestConfigDefaults:
    def test_continuous_enabled_by_default(self):
        plugin = _make_plugin()
        assert plugin._continuous_enabled is True

    def test_continuous_disabled_via_config(self):
        plugin = _make_plugin({"chirp_continuous": False})
        assert plugin._continuous_enabled is False

    def test_default_freq(self):
        plugin = _make_plugin()
        assert plugin._chirp_freq_mhz == 903.9

    def test_default_sample_rate(self):
        plugin = _make_plugin()
        assert plugin._chirp_sr == 250_000

    def test_default_display_rows(self):
        plugin = _make_plugin()
        assert plugin._display_rows_per_s == 32

    def test_default_batch_interval(self):
        plugin = _make_plugin()
        assert plugin._batch_interval == 0.5

    def test_default_waterfall_depth(self):
        plugin = _make_plugin()
        assert plugin._chirp_wf_depth == 1024

    def test_config_overrides(self):
        plugin = _make_plugin({
            "chirp_default_freq_mhz": 915.0,
            "chirp_default_sample_rate": 1_024_000,
            "chirp_display_rows_per_s": 16,
            "chirp_batch_interval_s": 1.0,
            "chirp_waterfall_depth": 512,
        })
        assert plugin._chirp_freq_mhz == 915.0
        assert plugin._chirp_sr == 1_024_000
        assert plugin._display_rows_per_s == 16
        assert plugin._batch_interval == 1.0
        assert plugin._chirp_wf_depth == 512


class TestFftSizeSelection:
    def test_narrow_for_250k(self):
        plugin = _make_plugin()
        assert plugin._fft_size_for_sr(250_000) == 256

    def test_wide_for_2m(self):
        plugin = _make_plugin()
        assert plugin._fft_size_for_sr(2_048_000) == 2048

    def test_wide_for_1m(self):
        plugin = _make_plugin()
        assert plugin._fft_size_for_sr(1_024_000) == 2048


class TestSetContinuousParams:
    def test_updates_freq(self):
        plugin = _make_plugin()
        plugin.set_continuous_params(freq_mhz=915.0)
        assert plugin._chirp_freq_mhz == 915.0

    def test_updates_sample_rate(self):
        plugin = _make_plugin()
        plugin.set_continuous_params(sample_rate=1_024_000)
        assert plugin._chirp_sr == 1_024_000

    def test_rejects_invalid_sample_rate(self):
        plugin = _make_plugin()
        with pytest.raises(ValueError, match="sample_rate"):
            plugin.set_continuous_params(sample_rate=300_000)

    def test_updates_window_on_sr_change(self):
        plugin = _make_plugin()
        old_len = len(plugin._window)
        plugin.set_continuous_params(sample_rate=2_048_000)
        assert len(plugin._window) == 2048
        assert len(plugin._window) != old_len


class TestProcessChunk:
    def _make_iq_bytes(self, sample_rate: int, duration_s: float,
                       tone_hz: float = 0) -> bytes:
        n_samples = int(sample_rate * duration_s)
        t = np.arange(n_samples) / sample_rate
        if tone_hz:
            phase = 2 * np.pi * tone_hz * t
            iq = np.exp(1j * phase).astype(np.complex64)
        else:
            iq = np.zeros(n_samples, dtype=np.complex64)

        i_ch = np.clip(iq.real * 127 + 127.5, 0, 255).astype(np.uint8)
        q_ch = np.clip(iq.imag * 127 + 127.5, 0, 255).astype(np.uint8)
        raw = np.empty(n_samples * 2, dtype=np.uint8)
        raw[0::2] = i_ch
        raw[1::2] = q_ch
        return raw.tobytes()

    def test_basic_shape(self):
        plugin = _make_plugin()
        sr = 250_000
        raw = self._make_iq_bytes(sr, 0.5)
        fft_size = 256
        hop = 64
        pool = 4
        window = np.hanning(fft_size).astype(np.float32)

        result = plugin._process_chunk(raw, fft_size, hop, window, pool)
        assert result is not None
        assert result.ndim == 2
        assert result.shape[1] == fft_size

    def test_pool_reduces_rows(self):
        plugin = _make_plugin()
        sr = 250_000
        raw = self._make_iq_bytes(sr, 0.5)
        fft_size = 256
        hop = 64
        window = np.hanning(fft_size).astype(np.float32)

        unpooled = plugin._process_chunk(raw, fft_size, hop, window, 1)
        pooled = plugin._process_chunk(raw, fft_size, hop, window, 8)
        assert pooled.shape[0] < unpooled.shape[0]

    def test_tone_creates_spectral_peak(self):
        plugin = _make_plugin()
        sr = 250_000
        tone_hz = 50_000
        raw = self._make_iq_bytes(sr, 0.5, tone_hz=tone_hz)
        fft_size = 256
        hop = 64
        window = np.hanning(fft_size).astype(np.float32)

        result = plugin._process_chunk(raw, fft_size, hop, window, 1)
        center = fft_size // 2
        right_mean = float(result[:, center:].mean())
        left_mean = float(result[:, :center].mean())
        assert right_mean > left_mean

    def test_returns_none_for_short_chunk(self):
        plugin = _make_plugin()
        raw = b"\x80" * 100  # too few samples
        result = plugin._process_chunk(raw, 256, 64, np.hanning(256).astype(np.float32), 1)
        assert result is None


class TestIngestRows:
    def test_appends_to_waterfall(self):
        plugin = _make_plugin()
        rows = np.random.randn(8, 256).astype(np.float32)
        plugin._ingest_rows(rows, 1000.0)
        assert len(plugin._chirp_waterfall) == 8
        assert plugin._chirp_sweep_count == 8

    def test_updates_db_scale(self):
        plugin = _make_plugin()
        rows = np.random.randn(8, 256).astype(np.float32) * 10 - 60
        plugin._ingest_rows(rows, 1000.0)
        assert plugin._db_lo is not None
        assert plugin._db_hi is not None
        assert plugin._db_hi > plugin._db_lo

    def test_waterfall_depth_limit(self):
        plugin = _make_plugin({"chirp_waterfall_depth": 16})
        plugin._chirp_waterfall = deque(maxlen=16)
        for i in range(20):
            rows = np.random.randn(1, 256).astype(np.float32)
            plugin._ingest_rows(rows, 1000.0 + i)
        assert len(plugin._chirp_waterfall) == 16


class TestGetCaptureStatus:
    def test_includes_streaming_flag(self):
        plugin = _make_plugin()
        status = plugin.get_capture_status()
        assert "streaming" in status
        assert status["streaming"] is False

    def test_includes_freq_and_sr(self):
        plugin = _make_plugin()
        status = plugin.get_capture_status()
        assert status["freq_mhz"] == 903.9
        assert status["sample_rate"] == 250_000

    def test_includes_waterfall_count(self):
        plugin = _make_plugin()
        rows = np.random.randn(5, 256).astype(np.float32)
        plugin._ingest_rows(rows, 1000.0)
        status = plugin.get_capture_status()
        assert status["waterfall_count"] == 5


class TestGetSnapshot:
    def test_includes_chirp_waterfall_tail(self):
        plugin = _make_plugin()
        rows = np.random.randn(5, 256).astype(np.float32)
        plugin._ingest_rows(rows, 1000.0)
        snap = plugin.get_snapshot()
        assert "chirp_waterfall_tail" in snap
        tail = snap["chirp_waterfall_tail"]
        assert tail["cols"] == 256
        assert tail["sweep_count"] == 5
        assert len(tail["rows"]) == 5

    def test_chirp_status_in_snapshot(self):
        plugin = _make_plugin()
        snap = plugin.get_snapshot()
        assert "chirp_status" in snap


class TestGetChirpWaterfallHistory:
    def test_empty_history(self):
        plugin = _make_plugin()
        hist = plugin.get_chirp_waterfall_history()
        assert hist["available"] is True
        assert len(hist["rows"]) == 0
        assert hist["sweep_count"] == 0

    def test_populated_history(self):
        plugin = _make_plugin()
        rows = np.random.randn(10, 256).astype(np.float32)
        plugin._ingest_rows(rows, 1000.0)
        hist = plugin.get_chirp_waterfall_history()
        assert len(hist["rows"]) == 10
        assert len(hist["row_timestamps"]) == 10
        assert hist["cols"] == 256
        assert hist["freq_center_hz"] == int(903.9 * 1e6)
        assert "sf_slopes" in hist
        assert len(hist["sf_slopes"]) == 6


class TestSfSlopes:
    def test_sf_range(self):
        slopes = _compute_sf_slopes(250_000, 256, 64)
        assert len(slopes) == 6
        sfs = [s["sf"] for s in slopes]
        assert sfs == [7, 8, 9, 10, 11, 12]

    def test_sf7_faster_than_sf12(self):
        slopes = _compute_sf_slopes(250_000, 256, 64)
        sf7 = next(s for s in slopes if s["sf"] == 7)
        sf12 = next(s for s in slopes if s["sf"] == 12)
        assert sf7["t_symbol_ms"] < sf12["t_symbol_ms"]

    def test_pool_factor_affects_slope(self):
        slopes_1 = _compute_sf_slopes(250_000, 256, 64, pool_factor=1)
        slopes_4 = _compute_sf_slopes(250_000, 256, 64, pool_factor=4)
        for s1, s4 in zip(slopes_1, slopes_4):
            assert s1["t_symbol_ms"] == s4["t_symbol_ms"]
            assert abs(s4["bins_per_frame"]) > abs(s1["bins_per_frame"])


# ---------------------------------------------------------------------------
# Helpers for detection tests
# ---------------------------------------------------------------------------

def _iq_to_raw_bytes(iq: np.ndarray) -> bytes:
    """Convert complex64 IQ to uint8 interleaved I/Q bytes (rtl_sdr format)."""
    i_ch = np.clip(iq.real * 127 + 127.5, 0, 255).astype(np.uint8)
    q_ch = np.clip(iq.imag * 127 + 127.5, 0, 255).astype(np.uint8)
    raw = np.empty(len(iq) * 2, dtype=np.uint8)
    raw[0::2] = i_ch
    raw[1::2] = q_ch
    return raw.tobytes()


# ---------------------------------------------------------------------------
# Detection config tests
# ---------------------------------------------------------------------------


class TestDetectionConfig:
    def test_detection_enabled_by_default(self):
        plugin = _make_plugin()
        assert plugin._detection_enabled is True

    def test_detection_disabled_via_config(self):
        plugin = _make_plugin({"chirp_detection_enabled": False})
        assert plugin._detection_enabled is False
        assert plugin._preamble_tracker is None

    def test_detection_sfs_default(self):
        plugin = _make_plugin()
        assert plugin._detection_sfs == [7, 8, 9, 10, 11, 12]

    def test_detection_sfs_override(self):
        plugin = _make_plugin({"chirp_detection_sfs": [7, 8]})
        assert plugin._detection_sfs == [7, 8]

    def test_detection_snr_threshold(self):
        plugin = _make_plugin({"chirp_detection_snr_threshold_db": 10.0})
        assert plugin._detection_snr_threshold_db == 10.0

    def test_tracker_initialized_when_enabled(self):
        plugin = _make_plugin()
        assert plugin._preamble_tracker is not None


# ---------------------------------------------------------------------------
# Detection via _run_detection
# ---------------------------------------------------------------------------


class TestRunDetection:
    def test_detection_from_synthetic_preamble(self):
        plugin = _make_plugin({
            "chirp_detection_sfs": [7],
            "chirp_detection_snr_threshold_db": 0.0,
        })
        iq = make_preamble(7, n_symbols=10)
        raw = _iq_to_raw_bytes(iq)
        plugin._run_detection(raw, 1000.0)
        assert len(plugin._detection_history) >= 1
        d = plugin._detection_history[0]
        assert d["sf"] == 7
        assert d["snr_db"] > 0

    def test_no_detection_when_disabled(self):
        plugin = _make_plugin({"chirp_detection_enabled": False})
        iq = make_preamble(7, n_symbols=10)
        raw = _iq_to_raw_bytes(iq)
        plugin._run_detection(raw, 1000.0)
        assert len(plugin._detection_history) == 0

    def test_publishes_event(self):
        plugin = _make_plugin({
            "chirp_detection_sfs": [7],
            "chirp_detection_snr_threshold_db": 0.0,
        })
        iq = make_preamble(7, n_symbols=10)
        raw = _iq_to_raw_bytes(iq)
        plugin._run_detection(raw, 1000.0)
        plugin.event_bus.publish.assert_any_call(
            events.CHIRP_DETECTION,
            plugin._detection_history[0],
        )

    def test_payload_fields(self):
        plugin = _make_plugin({
            "chirp_detection_sfs": [7],
            "chirp_detection_snr_threshold_db": 0.0,
        })
        iq = make_preamble(7, n_symbols=10)
        raw = _iq_to_raw_bytes(iq)
        plugin._run_detection(raw, 1234.5)
        d = plugin._detection_history[0]
        assert d["timestamp"] == 1234.5
        assert "freq_offset_hz" in d
        assert "freq_offset_bin" in d
        assert "freq_center_hz" in d
        assert "sample_rate" in d
        assert d["sample_rate"] == 250_000


# ---------------------------------------------------------------------------
# Detection cooldown
# ---------------------------------------------------------------------------


class TestDetectionCooldown:
    def test_cooldown_suppresses_duplicate(self):
        plugin = _make_plugin({
            "chirp_detection_sfs": [7],
            "chirp_detection_snr_threshold_db": 0.0,
            "chirp_detection_cooldown_s": 10.0,
        })
        iq = make_preamble(7, n_symbols=10)
        raw = _iq_to_raw_bytes(iq)
        plugin._run_detection(raw, 1000.0)
        assert len(plugin._detection_history) == 1
        plugin._run_detection(raw, 1001.0)
        assert len(plugin._detection_history) == 1

    def test_cooldown_allows_after_expiry(self):
        plugin = _make_plugin({
            "chirp_detection_sfs": [7],
            "chirp_detection_snr_threshold_db": 0.0,
            "chirp_detection_cooldown_s": 0.1,
        })
        iq = make_preamble(7, n_symbols=10)
        raw = _iq_to_raw_bytes(iq)
        plugin._run_detection(raw, 1000.0)
        assert len(plugin._detection_history) == 1
        plugin._preamble_tracker.reset()
        plugin._run_detection(raw, 1001.0)
        assert len(plugin._detection_history) == 2

    def test_cooldown_per_sf(self):
        plugin = _make_plugin({
            "chirp_detection_sfs": [7, 8],
            "chirp_detection_snr_threshold_db": 0.0,
            "chirp_detection_cooldown_s": 10.0,
        })
        iq7 = make_preamble(7, n_symbols=10)
        iq8 = make_preamble(8, n_symbols=10)
        plugin._run_detection(_iq_to_raw_bytes(iq7), 1000.0)
        plugin._run_detection(_iq_to_raw_bytes(iq8), 1001.0)
        sfs = {d["sf"] for d in plugin._detection_history}
        assert 7 in sfs
        assert 8 in sfs


# ---------------------------------------------------------------------------
# Detection history & stats
# ---------------------------------------------------------------------------


class TestDetectionHistoryAndStats:
    def test_get_detection_history_empty(self):
        plugin = _make_plugin()
        assert plugin.get_detection_history() == []

    def test_get_detection_history_populated(self):
        plugin = _make_plugin({
            "chirp_detection_sfs": [7],
            "chirp_detection_snr_threshold_db": 0.0,
        })
        iq = make_preamble(7, n_symbols=10)
        plugin._run_detection(_iq_to_raw_bytes(iq), 1000.0)
        hist = plugin.get_detection_history()
        assert len(hist) >= 1
        assert hist[0]["sf"] == 7

    def test_get_detection_stats(self):
        plugin = _make_plugin({
            "chirp_detection_sfs": [7],
            "chirp_detection_snr_threshold_db": 0.0,
        })
        iq = make_preamble(7, n_symbols=10)
        plugin._run_detection(_iq_to_raw_bytes(iq), 1000.0)
        stats = plugin.get_detection_stats()
        assert stats["enabled"] is True
        assert stats["total"] >= 1
        assert 7 in stats["by_sf"]

    def test_stats_disabled(self):
        plugin = _make_plugin({"chirp_detection_enabled": False})
        stats = plugin.get_detection_stats()
        assert stats["enabled"] is False
        assert stats["total"] == 0

    def test_history_depth_limit(self):
        plugin = _make_plugin({
            "chirp_detection_sfs": [7],
            "chirp_detection_snr_threshold_db": 0.0,
            "chirp_detection_cooldown_s": 0.0,
            "chirp_detection_history_depth": 4,
        })
        plugin._detection_history = deque(maxlen=4)
        iq = make_preamble(7, n_symbols=10)
        raw = _iq_to_raw_bytes(iq)
        for i in range(10):
            plugin._preamble_tracker.reset()
            plugin._run_detection(raw, 1000.0 + i * 10)
        assert len(plugin._detection_history) <= 4


# ---------------------------------------------------------------------------
# Detection in capture status & snapshot
# ---------------------------------------------------------------------------


class TestDetectionInStatus:
    def test_capture_status_includes_detection(self):
        plugin = _make_plugin()
        status = plugin.get_capture_status()
        assert "detection_enabled" in status
        assert status["detection_enabled"] is True
        assert "detection_count" in status
        assert status["detection_count"] == 0

    def test_snapshot_includes_detection_stats(self):
        plugin = _make_plugin()
        snap = plugin.get_snapshot()
        assert "chirp_detection" in snap
        assert snap["chirp_detection"]["enabled"] is True


# ---------------------------------------------------------------------------
# Tracker reset on param change
# ---------------------------------------------------------------------------


class TestDetectionTrackerReset:
    def test_reinit_on_freq_change(self):
        plugin = _make_plugin({
            "chirp_detection_sfs": [7],
            "chirp_detection_snr_threshold_db": 0.0,
        })
        old_tracker = plugin._preamble_tracker
        plugin.set_continuous_params(freq_mhz=915.0)
        assert plugin._preamble_tracker is not old_tracker

    def test_reinit_on_sr_change(self):
        plugin = _make_plugin({
            "chirp_detection_sfs": [7],
            "chirp_detection_snr_threshold_db": 0.0,
        })
        old_tracker = plugin._preamble_tracker
        plugin.set_continuous_params(sample_rate=1_024_000)
        assert plugin._preamble_tracker is not old_tracker


class TestPendingExtractionRetry:
    def test_retry_when_iq_not_yet_available(self):
        from tests.test_lora_dechirp import make_lora_packet
        plugin = _make_plugin({
            "chirp_detection_sfs": [7],
            "chirp_detection_snr_threshold_db": 0.0,
        })
        sf = 7
        payload = b"\xAA\xBB"
        pkt_iq = make_lora_packet(sf, payload, cr=1, sync_byte=0x34)
        cref = plugin._preamble_tracker.chirp_ref
        sym_len = cref.symbol_length(sf)

        preamble_end = 8 * sym_len
        chunk1_raw = _iq_to_raw_bytes(pkt_iq[:preamble_end])
        plugin._run_detection(chunk1_raw, 1000.0)
        assert len(plugin._detection_history) >= 1
        assert len(plugin._pending_extractions) > 0

        chunk2_raw = _iq_to_raw_bytes(pkt_iq[preamble_end:])
        plugin._run_detection(chunk2_raw, 1001.0)
        calls = [c for c in plugin.event_bus.publish.call_args_list
                 if c[0][0] == events.CHIRP_PACKET_DECODED]
        assert len(calls) >= 1


class TestExpiredDetectionPruning:
    def test_expired_detection_pruned(self):
        from reticulumpi.builtin_plugins.lora_dechirp import Detection
        plugin = _make_plugin({
            "chirp_detection_sfs": [7],
            "chirp_detection_snr_threshold_db": 0.0,
        })
        stale_det = Detection(
            timestamp=500.0, sf=7, freq_offset_hz=0.0,
            freq_offset_bin=0, snr_db=20.0, sample_offset=0,
        )
        plugin._pending_extractions.append(stale_det)

        big_iq = np.zeros(plugin._iq_ring_buffer.capacity + 1000, dtype=np.complex64)
        plugin._iq_ring_buffer.write(big_iq)

        plugin._process_pending_extractions()
        stale_offsets = [d.sample_offset for d in plugin._pending_extractions
                         if d.sample_offset == 0]
        assert len(stale_offsets) == 0
