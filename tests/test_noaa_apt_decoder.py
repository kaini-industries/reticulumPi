"""Tests for the NOAA APT decoder plugin."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi import events
from reticulumpi.builtin_plugins.noaa_apt_decoder import NOAAAPTDecoder
from reticulumpi.process_supervisor import ProcessFailure


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
    plugin._capture_failed = threading.Event()
    plugin._capture_state_lock = threading.Lock()
    plugin._capture_lease_released = True
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
                {
                    "name": "NOAA 19",
                    "max_el": 50,
                    "aos_ts": time.time() + 600,
                    "los_ts": time.time() + 1500,
                    "duration_s": 900,
                },
                {
                    "name": "ISS",
                    "max_el": 80,
                    "aos_ts": time.time() + 300,
                    "los_ts": time.time() + 900,
                    "duration_s": 600,
                },
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
            passes.append(
                {
                    "name": "NOAA 19",
                    "max_el": 50,
                    "aos_ts": time.time() + 600 * (i + 1),
                    "los_ts": time.time() + 600 * (i + 1) + 900,
                    "duration_s": 900,
                }
            )
        p._on_pass_prediction(events.SPACE_PASS_UPCOMING, {"passes": passes})
        with p._passes_lock:
            assert len(p._next_passes) == 6


class TestManagedCaptureLifecycle:
    def test_launch_is_transactional_and_restart_is_disabled(self):
        plugin = _make_plugin()
        now = time.time()
        plugin._next_passes = [
            {
                "satellite": "NOAA 19",
                "freq_mhz": 137.1,
                "aos_ts": now - 10,
                "los_ts": now + 100,
                "max_el": 55,
                "duration_s": 110,
            }
        ]
        rtl_process = MagicMock(pid=1601)
        recorder_process = MagicMock(pid=1602)
        managed = MagicMock()

        def construct(specs, **kwargs):
            managed.specs = specs
            managed.options = kwargs
            managed.start.side_effect = lambda: kwargs["on_started"](
                (rtl_process, recorder_process), False
            )
            return managed

        with (
            patch(
                "reticulumpi.builtin_plugins.noaa_apt_decoder.shutil.which",
                side_effect=["/usr/bin/rtl_fm", "/usr/bin/sox"],
            ),
            patch(
                "reticulumpi.builtin_plugins.noaa_apt_decoder.ManagedProcessGroup",
                side_effect=construct,
            ),
            patch.object(plugin, "_start_stderr_reader"),
            patch.object(plugin, "_start_thread"),
        ):
            plugin._launch_subprocess(7)

        assert [spec.name for spec in managed.specs] == ["rtl_fm", "sox"]
        assert managed.options["restart_policy"].enabled is False
        assert plugin._process_group is managed
        assert plugin._rtl_process is rtl_process
        assert plugin._process is recorder_process
        assert plugin._status == "recording"

    def test_missing_recorder_fails_acquisition_instead_of_claiming_sdr(self):
        plugin = _make_plugin()
        now = time.time()
        plugin._next_passes = [
            {
                "satellite": "NOAA 19",
                "freq_mhz": 137.1,
                "aos_ts": now - 10,
                "los_ts": now + 100,
                "max_el": 55,
            }
        ]
        with patch(
            "reticulumpi.builtin_plugins.noaa_apt_decoder.shutil.which",
            side_effect=["/usr/bin/rtl_fm", None],
        ):
            with pytest.raises(RuntimeError, match="sox"):
                plugin._on_acquire("APT-SDR", 0)

        assert plugin._dongle_active is False
        assert plugin._dongle_index is None

    def test_monitor_thread_failure_stops_started_pipeline(self):
        plugin = _make_plugin()
        now = time.time()
        plugin._next_passes = [
            {
                "satellite": "NOAA 19",
                "freq_mhz": 137.1,
                "aos_ts": now - 10,
                "los_ts": now + 100,
                "max_el": 55,
            }
        ]
        processes = (MagicMock(pid=1651), MagicMock(pid=1652))
        managed = MagicMock()

        def construct(_specs, **kwargs):
            managed.start.side_effect = lambda: kwargs["on_started"](processes, False)
            return managed

        with (
            patch(
                "reticulumpi.builtin_plugins.noaa_apt_decoder.shutil.which",
                side_effect=["/usr/bin/rtl_fm", "/usr/bin/sox"],
            ),
            patch(
                "reticulumpi.builtin_plugins.noaa_apt_decoder.ManagedProcessGroup",
                side_effect=construct,
            ),
            patch.object(plugin, "_start_stderr_reader"),
            patch.object(plugin, "_start_thread", side_effect=RuntimeError("thread cap")),
        ):
            with pytest.raises(RuntimeError, match="thread cap"):
                plugin._launch_subprocess(7)

        managed.stop.assert_called_once_with()
        assert plugin._process_group is None
        assert plugin._process is None
        assert plugin._capture_lease_released is True

    def test_unexpected_exit_stops_pipeline_before_releasing_lease(self):
        plugin = _make_plugin()
        now = time.time()
        plugin._current_pass = {
            "satellite": "NOAA 19",
            "recording_file": "capture.wav",
            "recording_path": "/tmp/capture.wav",
            "aos_ts": now - 200,
            "los_ts": now - 100,
        }
        plugin._recording_file = "/tmp/capture.wav"
        plugin._dongle_serial = "APT-SDR"
        plugin._dongle_active = True
        plugin._capture_lease_released = False
        order = []
        managed = MagicMock()
        managed.stop.side_effect = lambda: order.append("stop")
        plugin._process_group = managed
        scheduler = MagicMock()
        scheduler.dongle_released.side_effect = lambda *_args: order.append("release")
        plugin.app.sdr_scheduler = scheduler
        failure = ProcessFailure(1, "sox", 1, "exited", time.monotonic())

        plugin._on_capture_failure(failure)
        assert order == ["stop", "release"]
        plugin._monitor_pass()

        assert order == ["stop", "release"]
        assert plugin._current_pass is None
        assert plugin._dongle_active is False
        assert plugin._stats["failed_decodes"] == 1


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
        p._decode_recording(
            {
                "recording_path": "/tmp/test.wav",
                "satellite": "NOAA 19",
                "started_at": time.time(),
                "max_el": 30,
            }
        )
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
