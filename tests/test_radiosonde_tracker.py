"""Tests for the radiosonde tracker plugin."""

from __future__ import annotations

import time
from collections import deque
from typing import Any
from unittest.mock import MagicMock, patch

from reticulumpi import events
from reticulumpi.builtin_plugins.radiosonde_tracker import RadiosondeTracker
from reticulumpi.process_supervisor import ProcessFailure


def _make_app() -> MagicMock:
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.event_bus = MagicMock()
    app.sdr_scheduler = None
    return app


def _make_plugin(config: dict | None = None) -> RadiosondeTracker:
    plugin = RadiosondeTracker(_make_app(), config or {})
    plugin._active = True
    plugin._active_sonde = None
    plugin._altitude_profile = deque(maxlen=plugin._max_profile_points)
    plugin._position_track = deque(maxlen=plugin._max_track_points)
    plugin._wind_profile = deque(maxlen=500)
    plugin._prev_wind_point = None
    plugin._recent_sondes = deque(maxlen=10)
    plugin._stats = {
        "sondes_tracked_total": 0,
        "frames_decoded_total": 0,
        "current_session_frames": 0,
    }
    plugin._status = "idle"
    plugin._last_error = None
    plugin._restart_count = 0
    plugin._frame_count = 0
    plugin._last_sonde_frame_ts = 0.0
    plugin._snapshot_dirty = True
    plugin._cached_next_launch = None
    plugin._cached_next_launch_ts = 0.0
    plugin._process = None
    plugin._rtl_process = None
    plugin._pid = None
    return plugin


class TestManagedDecoderLifecycle:
    def test_launch_uses_transactional_two_stage_group(self):
        plugin = _make_plugin()
        rtl_process = MagicMock(pid=1301)
        decoder_process = MagicMock(pid=1302)
        managed = MagicMock(restart_count=0)

        def construct(specs, **kwargs):
            managed.specs = specs
            managed.hooks = kwargs
            managed.start.side_effect = lambda: kwargs["on_started"](
                (rtl_process, decoder_process), False
            )
            return managed

        with (
            patch(
                "reticulumpi.builtin_plugins.radiosonde_tracker.shutil.which",
                side_effect=["/usr/bin/rtl_fm", "/usr/bin/rs41mod"],
            ),
            patch(
                "reticulumpi.builtin_plugins.radiosonde_tracker.ManagedProcessGroup",
                side_effect=construct,
            ),
            patch.object(plugin, "_start_stderr_reader"),
            patch.object(plugin, "_start_thread"),
        ):
            plugin._launch_subprocess(4)

        assert [spec.name for spec in managed.specs] == ["rtl_fm", "rs41mod"]
        assert plugin._rtl_process is rtl_process
        assert plugin._process is decoder_process
        assert plugin._process_group is managed
        assert plugin._status == "scanning"
        assert managed.hooks["restart_policy"].enabled is False

    def test_parser_eof_notifies_second_stage(self):
        plugin = _make_plugin()
        process = MagicMock(stdout=[])
        group = MagicMock(running=True)
        plugin._process = process
        plugin._process_group = group

        plugin._parser_loop(process)

        group.notify_unexpected_eof.assert_called_once_with(1, "Radiosonde decoder stdout ended")

    def test_restart_exhaustion_releases_sdr(self):
        plugin = _make_plugin()
        plugin._dongle_serial = "SONDE-SDR"
        plugin._dongle_active = True
        scheduler = MagicMock()
        plugin.app.sdr_scheduler = scheduler
        failure = ProcessFailure(0, "rtl_fm", 1, "EOF", time.monotonic())

        plugin._on_decoder_exhausted(failure)

        assert plugin._dongle_active is False
        scheduler.dongle_released.assert_called_once_with("SONDE-SDR", plugin.plugin_name)


def _frame(
    sonde_id: str = "S1234567",
    lat: float = 40.0,
    lon: float = -90.0,
    alt: float = 5000.0,
    temp: float = -20.0,
    humidity: float = 50.0,
    vel_h: float = 5.0,
    vel_v: float = 4.5,
    sonde_type: str = "RS41",
) -> dict[str, Any]:
    return {
        "id": sonde_id,
        "type": sonde_type,
        "lat": lat,
        "lon": lon,
        "alt": alt,
        "temp": temp,
        "humidity": humidity,
        "vel_h": vel_h,
        "vel_v": vel_v,
    }


class TestValidateConfig:
    def test_defaults(self):
        p = RadiosondeTracker(_make_app(), {})
        assert p._gain == 40.0
        assert p._ppm == 0
        assert p._decoder_bin == "rs41mod"
        assert p._default_freq_hz == 404800000
        assert p._launch_windows_utc == ["11:15", "23:15"]
        assert p._window_duration_min == 120
        assert p._stale_timeout == 300.0
        assert p._max_profile_points == 2000
        assert p._max_track_points == 500
        assert p._max_restarts == 5

    def test_custom_config(self):
        p = RadiosondeTracker(
            _make_app(),
            {
                "decoder_bin": "/usr/bin/rs41mod",
                "default_freq_mhz": 403.0,
                "launch_windows_utc": ["00:00", "12:00"],
                "launch_window_duration_min": 60,
                "max_profile_points": 1000,
            },
        )
        assert p._decoder_bin == "/usr/bin/rs41mod"
        assert p._default_freq_hz == 403000000
        assert p._launch_windows_utc == ["00:00", "12:00"]
        assert p._window_duration_min == 60
        assert p._max_profile_points == 1000


class TestHandleFrame:
    def test_new_sonde_detected(self):
        p = _make_plugin()
        p._handle_frame(_frame())
        assert p._active_sonde is not None
        assert p._active_sonde["id"] == "S1234567"
        assert p._active_sonde["type"] == "RS41"
        assert p._active_sonde["phase"] == "ascent"
        assert p._stats["sondes_tracked_total"] == 1
        assert p._stats["frames_decoded_total"] == 1

    def test_frame_updates_position(self):
        p = _make_plugin()
        p._handle_frame(_frame(lat=40.0, lon=-90.0, alt=5000))
        p._handle_frame(_frame(lat=40.1, lon=-90.1, alt=6000))
        assert p._active_sonde["lat"] == 40.1
        assert p._active_sonde["lon"] == -90.1
        assert p._active_sonde["alt_m"] == 6000
        assert p._active_sonde["frame_count"] == 2

    def test_temperature_and_humidity(self):
        p = _make_plugin()
        p._handle_frame(_frame(temp=-30.0, humidity=65.0))
        assert p._active_sonde["temp_c"] == -30.0
        assert p._active_sonde["humidity_pct"] == 65.0

    def test_empty_id_ignored(self):
        p = _make_plugin()
        p._handle_frame({"id": "", "lat": 1.0})
        assert p._active_sonde is None
        assert p._stats["frames_decoded_total"] == 0

    def test_altitude_profile_populated(self):
        p = _make_plugin()
        p._handle_frame(_frame(alt=5000))
        p._handle_frame(_frame(alt=6000))
        assert len(p._altitude_profile) == 2
        assert p._altitude_profile[0]["alt_m"] == 5000
        assert p._altitude_profile[1]["alt_m"] == 6000

    def test_position_track_populated(self):
        p = _make_plugin()
        p._handle_frame(_frame(lat=40.0, lon=-90.0))
        assert len(p._position_track) == 1


class TestBurstDetection:
    def test_burst_detected(self):
        p = _make_plugin()
        p._handle_frame(_frame(alt=30000, vel_v=5.0))
        assert p._active_sonde["phase"] == "ascent"
        p._handle_frame(_frame(alt=32000, vel_v=-5.0))
        assert p._active_sonde["phase"] == "burst"
        assert p._active_sonde["burst_alt_m"] == 32000

    def test_no_burst_below_15000(self):
        p = _make_plugin()
        p._handle_frame(_frame(alt=10000, vel_v=5.0))
        p._handle_frame(_frame(alt=10000, vel_v=-5.0))
        assert p._active_sonde["phase"] == "ascent"

    def test_descent_phase(self):
        p = _make_plugin()
        p._handle_frame(_frame(alt=30000, vel_v=5.0))
        p._handle_frame(_frame(alt=32000, vel_v=-5.0))
        assert p._active_sonde["phase"] == "burst"
        # vel_v must be < 0 but NOT < -2 with alt > 15000 to hit the elif descent branch
        p._handle_frame(_frame(alt=31000, vel_v=-1.0))
        assert p._active_sonde["phase"] == "descent"


class TestSondeSwitch:
    def test_new_sonde_finalizes_previous(self):
        p = _make_plugin()
        p._handle_frame(_frame(sonde_id="S001"))
        p._handle_frame(_frame(sonde_id="S002"))
        assert p._active_sonde["id"] == "S002"
        assert p._stats["sondes_tracked_total"] == 2
        assert len(p._recent_sondes) == 1
        assert p._recent_sondes[0]["id"] == "S001"


class TestEventEmission:
    def test_new_sonde_emits_detected(self):
        p = _make_plugin()
        p._handle_frame(_frame())
        calls = p.event_bus.publish.call_args_list
        event_types = [c[0][0] for c in calls]
        assert events.RADIOSONDE_DETECTED in event_types

    def test_burst_emits_event(self):
        p = _make_plugin()
        p._handle_frame(_frame(alt=30000, vel_v=5.0))
        p.event_bus.publish.reset_mock()
        p._handle_frame(_frame(alt=32000, vel_v=-5.0))
        calls = p.event_bus.publish.call_args_list
        event_types = [c[0][0] for c in calls]
        assert events.RADIOSONDE_BURST in event_types


class TestPredictBurstAlt:
    def test_too_few_points(self):
        p = _make_plugin()
        for i in range(5):
            p._altitude_profile.append({"alt_m": 5000 + i * 100, "vel_v": 5.0})
        assert p._predict_burst_alt() is None

    def test_prediction_with_enough_points(self):
        p = _make_plugin()
        for i in range(15):
            p._altitude_profile.append({"alt_m": 10000 + i * 500, "vel_v": 5.0})
        result = p._predict_burst_alt()
        assert result is not None
        assert result > 17000

    def test_no_prediction_on_descent(self):
        p = _make_plugin()
        for i in range(15):
            p._altitude_profile.append({"alt_m": 30000 - i * 500, "vel_v": -5.0})
        assert p._predict_burst_alt() is None


class TestNextLaunchWindow:
    def test_returns_next_window(self):
        p = _make_plugin()
        result = p._next_launch_window()
        assert result is not None
        assert "expected_ts" in result
        assert "countdown_s" in result
        assert result["countdown_s"] > 0


class TestSnapshotAndStatus:
    def test_snapshot_empty(self):
        p = _make_plugin()
        p._update_snapshot_cache()
        snap = p.get_snapshot()
        assert snap["active_sonde"] is None
        assert snap["altitude_profile"] == []

    def test_snapshot_with_sonde(self):
        p = _make_plugin()
        p._handle_frame(_frame())
        p._update_snapshot_cache()
        snap = p.get_snapshot()
        assert snap["active_sonde"]["id"] == "S1234567"
        assert len(snap["altitude_profile"]) == 1

    def test_get_status(self):
        p = _make_plugin()
        p._handle_frame(_frame())
        s = p.get_status()
        assert s["active"] is True
        assert s["status"] == "tracking"
        assert s["sondes_tracked"] == 1
        assert s["active_sonde_id"] == "S1234567"
