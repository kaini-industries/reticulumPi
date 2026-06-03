"""Tests for the NOAA APT decoder plugin."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any
from unittest.mock import MagicMock, patch

from reticulumpi import events
from reticulumpi.builtin_plugins.noaa_apt_decoder import NOAAAPTDecoder


def _make_app() -> MagicMock:
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.event_bus = MagicMock()
    return app


def _make_plugin(config: dict | None = None) -> NOAAAPTDecoder:
    cfg = config or {}
    cfg.setdefault("image_dir", "/tmp/test_noaa_images")
    cfg.setdefault("recording_dir", "/tmp/test_noaa_recordings")
    with patch("os.makedirs"):
        plugin = NOAAAPTDecoder(_make_app(), cfg)
    plugin._active = True
    plugin._current_pass = None
    plugin._recent_images = deque(maxlen=20)
    plugin._next_passes = []
    plugin._passes_lock = threading.Lock()
    plugin._stats = {
        "total_captures": 0,
        "successful_decodes": 0,
        "failed_decodes": 0,
        "last_capture_at": None,
        "success_rate_pct": 0.0,
        "best_pass": None,
    }
    plugin._status = "idle"
    plugin._last_error = None
    plugin._recording_file = None
    plugin._rtl_process = None
    plugin._process = None
    plugin._pid = None
    return plugin


def _pass_prediction(
    name: str = "NOAA 19",
    max_el: float = 45.0,
    aos_offset: float = 600,
    duration: float = 900,
) -> dict[str, Any]:
    now = time.time()
    return {
        "passes": [
            {
                "name": name,
                "max_el": max_el,
                "aos_ts": now + aos_offset,
                "los_ts": now + aos_offset + duration,
                "duration_s": duration,
            }
        ]
    }


class TestValidateConfig:
    def test_defaults(self):
        with patch("os.makedirs"):
            p = NOAAAPTDecoder(_make_app(), {})
        assert p._gain == 40.0
        assert p._ppm == 0
        assert p._decoder_bin == "noaa-apt"
        assert p._min_elevation == 15.0
        assert p._pre_pass_seconds == 30
        assert p._post_pass_seconds == 30
        assert p._retention_days == 7
        assert p._max_images == 50
        assert "NOAA 15" in p._satellites
        assert "NOAA 18" in p._satellites
        assert "NOAA 19" in p._satellites

    def test_custom_satellites(self):
        with patch("os.makedirs"):
            p = NOAAAPTDecoder(
                _make_app(),
                {"satellites": {"NOAA 15": 137.620, "NOAA 18": 137.9125}},
            )
        assert len(p._satellites) == 2
        assert "NOAA 19" not in p._satellites


class TestOnPassPrediction:
    def test_filters_noaa_satellites(self):
        p = _make_plugin()
        data = {
            "passes": [
                {"name": "NOAA 19", "max_el": 50, "aos_ts": time.time() + 600, "los_ts": time.time() + 1500, "duration_s": 900},
                {"name": "ISS", "max_el": 80, "aos_ts": time.time() + 300, "los_ts": time.time() + 900, "duration_s": 600},
            ]
        }
        p._on_pass_prediction(events.SPACE_PASS_UPCOMING, data)
        with p._passes_lock:
            assert len(p._next_passes) == 1
            assert p._next_passes[0]["satellite"] == "NOAA 19"

    def test_filters_low_elevation(self):
        p = _make_plugin()
        data = _pass_prediction(max_el=5.0)
        p._on_pass_prediction(events.SPACE_PASS_UPCOMING, data)
        with p._passes_lock:
            assert len(p._next_passes) == 0

    def test_high_elevation_passes_through(self):
        p = _make_plugin()
        data = _pass_prediction(max_el=60.0)
        p._on_pass_prediction(events.SPACE_PASS_UPCOMING, data)
        with p._passes_lock:
            assert len(p._next_passes) == 1
            assert p._next_passes[0]["freq_mhz"] == 137.100

    def test_caps_at_6_passes(self):
        p = _make_plugin()
        passes = []
        for i in range(10):
            passes.append({
                "name": "NOAA 19",
                "max_el": 50,
                "aos_ts": time.time() + 600 * (i + 1),
                "los_ts": time.time() + 600 * (i + 1) + 900,
                "duration_s": 900,
            })
        p._on_pass_prediction(events.SPACE_PASS_UPCOMING, {"passes": passes})
        with p._passes_lock:
            assert len(p._next_passes) == 6


class TestDecodeRecording:
    def test_missing_file_skips(self):
        p = _make_plugin()
        p._decode_recording({"recording_path": "/nonexistent/file.wav", "satellite": "NOAA 19"})
        assert p._status == "idle"

    @patch("shutil.which", return_value=None)
    def test_missing_decoder_skips(self, _mock_which):
        p = _make_plugin()
        with patch("os.path.exists", return_value=True):
            p._decode_recording({"recording_path": "/tmp/test.wav", "satellite": "NOAA 19"})
        assert p._status == "idle"

    @patch("shutil.which", return_value="/usr/bin/noaa-apt")
    @patch("subprocess.run")
    @patch("os.path.exists", return_value=True)
    @patch("os.path.getsize", return_value=600000)
    @patch("os.remove")
    def test_successful_decode(self, _rm, _size, _exists, mock_run, _which):
        mock_run.return_value = MagicMock(returncode=0)
        p = _make_plugin()
        pass_info = {
            "recording_path": "/tmp/test.wav",
            "satellite": "NOAA 19",
            "started_at": time.time(),
            "max_el": 65,
            "aos_ts": time.time() - 900,
            "los_ts": time.time(),
            "duration_s": 900,
        }
        p._decode_recording(pass_info)
        assert p._stats["successful_decodes"] == 1
        assert len(p._recent_images) == 1
        img = p._recent_images[0]
        assert img["quality"] == "excellent"
        assert img["quality_score"] >= 0.8

    @patch("shutil.which", return_value="/usr/bin/noaa-apt")
    @patch("subprocess.run")
    @patch("os.path.exists", return_value=True)
    @patch("os.path.getsize", return_value=100)
    @patch("os.remove")
    def test_failed_decode(self, _rm, _size, _exists, mock_run, _which):
        mock_run.return_value = MagicMock(returncode=1, stderr="decode error")
        p = _make_plugin()
        p._decode_recording({
            "recording_path": "/tmp/test.wav",
            "satellite": "NOAA 19",
            "started_at": time.time(),
            "max_el": 30,
        })
        assert p._stats["failed_decodes"] == 1


class TestSnapshotAndStatus:
    def test_snapshot_empty(self):
        p = _make_plugin()
        p._update_snapshot_cache()
        snap = p.get_snapshot()
        assert snap["status"] == "idle"
        assert snap["recent_images"] == []
        assert snap["next_passes"] == []

    def test_get_status(self):
        p = _make_plugin()
        s = p.get_status()
        assert s["active"] is True
        assert s["status"] == "idle"
        assert s["total_captures"] == 0
        assert s["next_pass"] is None

    def test_get_status_with_pass(self):
        p = _make_plugin()
        data = _pass_prediction()
        p._on_pass_prediction(events.SPACE_PASS_UPCOMING, data)
        s = p.get_status()
        assert s["next_pass"] == "NOAA 19"
