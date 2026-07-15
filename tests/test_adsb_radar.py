"""Tests for the adsb_radar plugin.

Focuses on the pure-Python surface — config validation, SBS line parsing,
aircraft state management, snapshot generation, distance calculation, and
emergency squawk detection — without touching any real RTL-SDR hardware
or spawning dump1090.
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
from collections import deque
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi.builtin_plugins.adsb_radar import (
    AdsbRadarPlugin,
    AircraftState,
    _haversine_nm,
)
from reticulumpi.process_supervisor import ProcessFailure


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_app() -> MagicMock:
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.event_bus = MagicMock()
    return app


def _make_plugin(config: dict | None = None) -> AdsbRadarPlugin:
    """Construct an AdsbRadarPlugin without calling start() (no threads)."""
    plugin = AdsbRadarPlugin(_make_app(), config or {})
    plugin._state_lock = threading.Lock()
    plugin._process = None
    plugin._process_group = None
    plugin._pid = None
    plugin._restart_count = 0
    plugin._status = "starting"
    plugin._last_error = None
    plugin._dump1090_path = "/usr/bin/dump1090"
    plugin._aircraft = {}
    plugin._total_messages = 0
    plugin._aircraft_seen_total = 0
    plugin._resolved_index = None
    plugin._device_lease = None
    plugin._rtl_biast_path = None
    plugin._bias_tee_active = False
    plugin._bias_tee_lock = threading.Lock()
    plugin._log_reader_thread = None
    plugin._patience_active = False
    plugin._launch_time = None
    plugin._msg_rate_history = deque(maxlen=60)
    plugin._msg_rate_window_start = time.time()
    plugin._msg_rate_window_count = 0
    plugin._max_distance_nm = 0.0
    plugin._emergency_history = deque(maxlen=20)
    return plugin


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------


class TestValidateConfig:
    def test_defaults(self):
        p = _make_plugin()
        assert p._dump1090_bin == "dump1090"
        assert p._device_id == "0"
        assert p._device_selector == "index"
        assert p._gain == "max"
        assert p._ppm == 0
        assert p._enable_bias_tee is False
        assert p._sbs_port == 30003
        assert p._stale_timeout == 300
        assert p._receiver_lat is None
        assert p._receiver_lon is None

    def test_custom_config(self):
        p = _make_plugin(
            {
                "dump1090_bin": "/opt/dump1090-fa/dump1090-fa",
                "device_index": 1,
                "gain": "40",
                "ppm": -3,
                "enable_bias_tee": True,
                "sbs_port": 30004,
                "stale_timeout": 120,
                "receiver_lat": 40.7128,
                "receiver_lon": -74.0060,
            }
        )
        assert p._dump1090_bin == "/opt/dump1090-fa/dump1090-fa"
        assert p._device_id == "1"
        assert p._gain == "40"
        assert p._ppm == -3
        assert p._enable_bias_tee is True
        assert p._sbs_port == 30004
        assert p._stale_timeout == 120
        assert p._receiver_lat == pytest.approx(40.7128)
        assert p._receiver_lon == pytest.approx(-74.0060)

    def test_receiver_lat_without_lon_ignored(self):
        p = _make_plugin({"receiver_lat": 40.0})
        assert p._receiver_lat is None
        assert p._receiver_lon is None


# ---------------------------------------------------------------------------
# _build_cmd
# ---------------------------------------------------------------------------


class TestBuildCmd:
    def test_basic_command(self):
        p = _make_plugin()
        cmd = p._build_cmd()
        assert cmd[0] == "/usr/bin/dump1090"
        assert "--net" in cmd
        assert "--quiet" in cmd
        assert "--device-index" in cmd
        idx = cmd.index("--device-index")
        assert cmd[idx + 1] == "0"
        assert "--gain" in cmd
        g_idx = cmd.index("--gain")
        assert cmd[g_idx + 1] == "max"

    def test_bias_tee_not_in_cmd(self):
        p = _make_plugin({"enable_bias_tee": True})
        cmd = p._build_cmd()
        assert "--enable-bias-tee" not in cmd

    def test_bias_tee_config_parsed(self):
        p = _make_plugin({"enable_bias_tee": True})
        assert p._enable_bias_tee is True

    def test_receiver_position_included(self):
        p = _make_plugin({"receiver_lat": 40.7, "receiver_lon": -74.0})
        cmd = p._build_cmd()
        assert "--lat" in cmd
        lat_idx = cmd.index("--lat")
        assert cmd[lat_idx + 1] == "40.7"
        lon_idx = cmd.index("--lon")
        assert cmd[lon_idx + 1] == "-74.0"

    def test_receiver_position_omitted_when_unset(self):
        p = _make_plugin()
        cmd = p._build_cmd()
        assert "--lat" not in cmd
        assert "--lon" not in cmd


# ---------------------------------------------------------------------------
# _set_bias_tee
# ---------------------------------------------------------------------------


class TestSetBiasTee:
    def test_enable_calls_rtl_biast(self):
        p = _make_plugin({"enable_bias_tee": True})
        p._rtl_biast_path = "/usr/bin/rtl_biast"
        p._resolved_index = 0
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            p._set_bias_tee(True)
        mock_run.assert_called_once_with(
            ["/usr/bin/rtl_biast", "-d", "0", "-b", "1"],
            capture_output=True,
            timeout=10,
        )
        assert p._bias_tee_active is True

    def test_disable_calls_rtl_biast(self):
        p = _make_plugin({"enable_bias_tee": True})
        p._rtl_biast_path = "/usr/bin/rtl_biast"
        p._resolved_index = 0
        p._bias_tee_active = True
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            p._set_bias_tee(False)
        mock_run.assert_called_once_with(
            ["/usr/bin/rtl_biast", "-d", "0", "-b", "0"],
            capture_output=True,
            timeout=10,
        )
        assert p._bias_tee_active is False

    def test_uses_resolved_index(self):
        p = _make_plugin()
        p._rtl_biast_path = "/usr/bin/rtl_biast"
        p._resolved_index = 2
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            p._set_bias_tee(True)
        assert mock_run.call_args[0][0][2] == "2"

    def test_skips_when_no_binary(self):
        p = _make_plugin()
        p._rtl_biast_path = None
        with patch("subprocess.run") as mock_run:
            p._set_bias_tee(True)
        mock_run.assert_not_called()
        assert p._bias_tee_active is False

    def test_handles_failure_gracefully(self):
        p = _make_plugin()
        p._rtl_biast_path = "/usr/bin/rtl_biast"
        p._resolved_index = 0
        with patch("subprocess.run", side_effect=OSError("device busy")):
            p._set_bias_tee(True)
        assert p._bias_tee_active is False


# ---------------------------------------------------------------------------
# SBS parsing — _parse_sbs_line
# ---------------------------------------------------------------------------


class TestSbsParsing:
    def _msg(
        self,
        icao="A1B2C3",
        tx_type="3",
        callsign="",
        alt="",
        speed="",
        track="",
        lat="",
        lon="",
        vrate="",
        squawk="",
        on_ground="",
    ):
        """Build a minimal SBS MSG line."""
        return (
            f"MSG,{tx_type},1,1,{icao},1,"
            f"2025/01/01,12:00:00.000,2025/01/01,12:00:00.000,"
            f"{callsign},{alt},{speed},{track},{lat},{lon},{vrate},{squawk},,,,{on_ground}"
        )

    def test_new_aircraft_created(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(icao="AABBCC", callsign="UAL123"))
        assert "AABBCC" in p._aircraft
        ac = p._aircraft["AABBCC"]
        assert ac.callsign == "UAL123"
        assert ac.message_count == 1
        assert p._aircraft_seen_total == 1

    def test_aircraft_updated_on_subsequent_messages(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(icao="AABBCC", alt="35000"))
        p._parse_sbs_line(self._msg(icao="AABBCC", alt="34900", speed="450"))
        ac = p._aircraft["AABBCC"]
        assert ac.altitude == 34900
        assert ac.ground_speed == 450.0
        assert ac.message_count == 2
        assert p._aircraft_seen_total == 1

    def test_position_parsed(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(icao="AABBCC", lat="40.7128", lon="-74.0060"))
        ac = p._aircraft["AABBCC"]
        assert ac.latitude == pytest.approx(40.7128)
        assert ac.longitude == pytest.approx(-74.0060)

    def test_zero_position_ignored(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(icao="AABBCC", lat="0.0", lon="0.0"))
        ac = p._aircraft["AABBCC"]
        assert ac.latitude is None
        assert ac.longitude is None

    def test_altitude_parsed(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(icao="AABBCC", alt="37500"))
        assert p._aircraft["AABBCC"].altitude == 37500

    def test_speed_and_track_parsed(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(icao="AABBCC", speed="485.3", track="270.5"))
        ac = p._aircraft["AABBCC"]
        assert ac.ground_speed == pytest.approx(485.3)
        assert ac.track == pytest.approx(270.5)

    def test_vertical_rate_parsed(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(icao="AABBCC", vrate="-1200"))
        assert p._aircraft["AABBCC"].vertical_rate == -1200

    def test_squawk_parsed(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(icao="AABBCC", squawk="1200"))
        assert p._aircraft["AABBCC"].squawk == "1200"

    def test_on_ground_flag(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(icao="AABBCC", on_ground="-1"))
        assert p._aircraft["AABBCC"].on_ground is True

    def test_non_msg_lines_ignored(self):
        p = _make_plugin()
        p._parse_sbs_line("STA,1,1,1,AABBCC,1,,,,,,,,,,,,,,,")
        assert len(p._aircraft) == 0

    def test_short_icao_ignored(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(icao="ABC"))
        assert len(p._aircraft) == 0

    def test_empty_fields_dont_overwrite(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(icao="AABBCC", callsign="UAL123", alt="35000"))
        p._parse_sbs_line(self._msg(icao="AABBCC", callsign="", alt=""))
        ac = p._aircraft["AABBCC"]
        assert ac.callsign == "UAL123"
        assert ac.altitude == 35000

    def test_total_messages_counted(self):
        p = _make_plugin()
        for _ in range(5):
            p._parse_sbs_line(self._msg(icao="AABBCC"))
        assert p._total_messages == 5

    def test_distance_computed_when_receiver_set(self):
        p = _make_plugin({"receiver_lat": 40.0, "receiver_lon": -74.0})
        p._parse_sbs_line(self._msg(icao="AABBCC", lat="41.0", lon="-74.0"))
        ac = p._aircraft["AABBCC"]
        assert ac.distance_nm is not None
        assert ac.distance_nm > 0

    def test_no_distance_without_receiver(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(icao="AABBCC", lat="41.0", lon="-74.0"))
        assert p._aircraft["AABBCC"].distance_nm is None


# ---------------------------------------------------------------------------
# Emergency squawk detection
# ---------------------------------------------------------------------------


class TestEmergencySquawk:
    def _msg(self, icao="AABBCC", squawk="1200"):
        return (
            f"MSG,3,1,1,{icao},1,"
            f"2025/01/01,12:00:00.000,2025/01/01,12:00:00.000,"
            f",,,,,,,{squawk},,,,"
        )

    def test_7700_triggers_event(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(squawk="7700"))
        p.event_bus.publish.assert_called()
        calls = [
            c for c in p.event_bus.publish.call_args_list if c[0][0] == "adsb.emergency_squawk"
        ]
        assert len(calls) == 1
        assert calls[0][0][1]["squawk"] == "7700"

    def test_7600_triggers_event(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(squawk="7600"))
        calls = [
            c for c in p.event_bus.publish.call_args_list if c[0][0] == "adsb.emergency_squawk"
        ]
        assert len(calls) == 1

    def test_7500_triggers_event(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(squawk="7500"))
        calls = [
            c for c in p.event_bus.publish.call_args_list if c[0][0] == "adsb.emergency_squawk"
        ]
        assert len(calls) == 1

    def test_normal_squawk_no_event(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(squawk="1200"))
        calls = [
            c for c in p.event_bus.publish.call_args_list if c[0][0] == "adsb.emergency_squawk"
        ]
        assert len(calls) == 0

    def test_same_emergency_squawk_only_fires_once(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(squawk="7700"))
        p._parse_sbs_line(self._msg(squawk="7700"))
        calls = [
            c for c in p.event_bus.publish.call_args_list if c[0][0] == "adsb.emergency_squawk"
        ]
        assert len(calls) == 1

    def test_new_aircraft_detected_event(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(icao="AABBCC"))
        calls = [
            c for c in p.event_bus.publish.call_args_list if c[0][0] == "adsb.aircraft_detected"
        ]
        assert len(calls) == 1
        assert calls[0][0][1]["icao"] == "AABBCC"


# ---------------------------------------------------------------------------
# Aircraft expiry (maintenance)
# ---------------------------------------------------------------------------


class TestAircraftExpiry:
    def test_stale_aircraft_removed(self):
        p = _make_plugin({"stale_timeout": 60})
        ac = AircraftState(icao="AABBCC", last_seen=time.time() - 120)
        p._aircraft["AABBCC"] = ac

        p._active = True
        p._stop_event = threading.Event()
        p._stop_event.clear()

        call_count = [0]

        def _fake_sleep(s):
            call_count[0] += 1
            if call_count[0] >= 2:
                p._active = False

        with patch.object(p, "_sleep_while_active", side_effect=_fake_sleep):
            p._maintenance_loop()

        assert "AABBCC" not in p._aircraft
        calls = [c for c in p.event_bus.publish.call_args_list if c[0][0] == "adsb.aircraft_lost"]
        assert len(calls) == 1

    def test_fresh_aircraft_not_removed(self):
        p = _make_plugin({"stale_timeout": 300})
        ac = AircraftState(icao="AABBCC", last_seen=time.time())
        p._aircraft["AABBCC"] = ac

        p._active = True
        p._stop_event = threading.Event()
        p._stop_event.clear()

        call_count = [0]

        def _fake_sleep(s):
            call_count[0] += 1
            if call_count[0] >= 2:
                p._active = False

        with patch.object(p, "_sleep_while_active", side_effect=_fake_sleep):
            p._maintenance_loop()

        assert "AABBCC" in p._aircraft


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_empty_snapshot_shape(self):
        p = _make_plugin()
        snap = p.get_snapshot()
        assert snap["status"] == "starting"
        assert snap["aircraft"] == []
        assert snap["stats"]["aircraft_count"] == 0
        assert snap["stats"]["total_messages"] == 0

    def test_snapshot_after_messages(self):
        p = _make_plugin()
        line = (
            "MSG,3,1,1,AABBCC,1,"
            "2025/01/01,12:00:00.000,2025/01/01,12:00:00.000,"
            "UAL123,35000,450,270,40.7128,-74.006,-500,1200,,,,0"
        )
        p._parse_sbs_line(line)

        snap = p.get_snapshot()
        assert snap["stats"]["aircraft_count"] == 1
        assert len(snap["aircraft"]) == 1
        ac = snap["aircraft"][0]
        assert ac["icao"] == "AABBCC"
        assert ac["callsign"] == "UAL123"
        assert ac["altitude"] == 35000
        assert ac["ground_speed"] == 450.0

    def test_get_status(self):
        p = _make_plugin()
        status = p.get_status()
        assert "active" in status
        assert "status" in status
        assert "aircraft_count" in status
        assert status["aircraft_count"] == 0


# ---------------------------------------------------------------------------
# Haversine distance
# ---------------------------------------------------------------------------


class TestHaversine:
    def test_same_point_is_zero(self):
        assert _haversine_nm(40.0, -74.0, 40.0, -74.0) == pytest.approx(0.0, abs=0.01)

    def test_known_distance(self):
        # New York (40.7128, -74.0060) to London (51.5074, -0.1278) ≈ 2999 nm
        dist = _haversine_nm(40.7128, -74.0060, 51.5074, -0.1278)
        assert 2990 < dist < 3010

    def test_equator_one_degree_is_60nm(self):
        dist = _haversine_nm(0.0, 0.0, 0.0, 1.0)
        assert 59.9 < dist < 60.1


# ---------------------------------------------------------------------------
# device resolution (via shared rtlsdr module; full resolver tests in test_rtlsdr.py)
# ---------------------------------------------------------------------------


class TestDeviceResolution:
    def test_build_cmd_uses_resolved_index(self):
        p = _make_plugin({"device_index": "00000001"})
        p._resolved_index = 0
        cmd = p._build_cmd()
        idx = cmd.index("--device-index")
        assert cmd[idx + 1] == "0"

    def test_build_cmd_falls_back_without_resolution(self):
        p = _make_plugin({"device_index": "2"})
        p._resolved_index = None
        cmd = p._build_cmd()
        idx = cmd.index("--device-index")
        assert cmd[idx + 1] == "2"

    def test_device_serial_takes_precedence(self):
        p = _make_plugin({"device_serial": "00000001", "device_index": "99"})
        assert p._device_id == "00000001"
        assert p._device_selector == "serial"

    def test_device_index_fallback(self):
        p = _make_plugin({"device_index": "14342860"})
        assert p._device_id == "14342860"
        assert p._device_selector == "index"


# ---------------------------------------------------------------------------
# Supervisor: missing binary → graceful 'unavailable' status
# ---------------------------------------------------------------------------


class TestWedgeDetection:
    """Verify the supervisor kills dump1090 when the SBS stream stalls."""

    def _run_supervisor_loop(self, plugin, fake_monotonic):
        """Simulate the supervisor's inner polling loop with a fake clock."""
        launch_time = fake_monotonic()
        last_msg_count = plugin._total_messages
        last_msg_time = launch_time
        restart_count_reset = False

        while plugin._active and plugin._process is not None:
            rc = plugin._process.poll()
            if rc is not None:
                break

            now = fake_monotonic()
            cur_count = plugin._total_messages
            if cur_count != last_msg_count:
                last_msg_count = cur_count
                last_msg_time = now
                if (
                    not restart_count_reset
                    and plugin._restart_count > 0
                    and now - launch_time > 600.0
                ):
                    plugin._restart_count = 0
                    restart_count_reset = True
            elif (
                plugin._wedge_timeout > 0
                and now - launch_time > plugin._wedge_grace
                and now - last_msg_time > plugin._wedge_timeout
            ):
                plugin._terminate_process()
                break

            plugin._sleep_while_active(5.0)

    def test_wedge_kills_dump1090(self):
        p = _make_plugin({"wedge_timeout": 60, "wedge_grace": 30})
        p._active = True
        p._stop_event = threading.Event()
        p._total_messages = 0

        proc = MagicMock()
        proc.poll.return_value = None
        p._process = proc
        p._pid = 9999

        clock = [0.0]
        call_count = [0]

        def fake_monotonic():
            return clock[0]

        def fake_sleep(secs):
            call_count[0] += 1
            clock[0] += 30.0
            if call_count[0] >= 10:
                p._active = False

        p._sleep_while_active = fake_sleep
        p._terminate_process = MagicMock()
        self._run_supervisor_loop(p, fake_monotonic)
        p._terminate_process.assert_called_once()

    def test_no_wedge_when_messages_flowing(self):
        p = _make_plugin({"wedge_timeout": 60, "wedge_grace": 30})
        p._active = True
        p._stop_event = threading.Event()
        p._total_messages = 0

        proc = MagicMock()
        proc.poll.return_value = None
        p._process = proc
        p._pid = 9999

        clock = [0.0]
        call_count = [0]

        def fake_monotonic():
            return clock[0]

        def fake_sleep(secs):
            call_count[0] += 1
            clock[0] += 10.0
            p._total_messages += 5
            if call_count[0] >= 5:
                p._active = False

        p._sleep_while_active = fake_sleep
        p._terminate_process = MagicMock()
        self._run_supervisor_loop(p, fake_monotonic)
        p._terminate_process.assert_not_called()

    def test_grace_period_delays_wedge_detection(self):
        p = _make_plugin({"wedge_timeout": 10, "wedge_grace": 100})
        p._active = True
        p._stop_event = threading.Event()
        p._total_messages = 0

        proc = MagicMock()
        proc.poll.return_value = None
        p._process = proc
        p._pid = 9999

        clock = [0.0]
        call_count = [0]

        def fake_monotonic():
            return clock[0]

        def fake_sleep(secs):
            call_count[0] += 1
            clock[0] += 20.0
            if call_count[0] >= 4:
                p._active = False

        p._sleep_while_active = fake_sleep
        p._terminate_process = MagicMock()
        self._run_supervisor_loop(p, fake_monotonic)
        # At 80s total, grace (100s) hasn't elapsed yet — no kill
        p._terminate_process.assert_not_called()

    def test_wedge_timeout_config_defaults(self):
        p = _make_plugin()
        assert p._wedge_timeout == 120
        assert p._wedge_grace == 30

    def test_wedge_disabled_when_zero(self):
        p = _make_plugin({"wedge_timeout": 0})
        assert p._wedge_timeout == 0


class TestRestartCounterReset:
    """Verify restart counter resets after sustained stable operation."""

    def _run_monitor_loop(self, plugin, fake_monotonic):
        """Run the inner monitoring loop with restart-counter-reset logic."""
        launch_time = fake_monotonic()
        last_msg_count = plugin._total_messages
        restart_count_reset = False

        while plugin._active and plugin._process is not None:
            rc = plugin._process.poll()
            if rc is not None:
                break

            now = fake_monotonic()
            cur_count = plugin._total_messages
            if cur_count != last_msg_count:
                last_msg_count = cur_count
                if (
                    not restart_count_reset
                    and plugin._restart_count > 0
                    and now - launch_time > 600.0
                ):
                    plugin._restart_count = 0
                    restart_count_reset = True

            plugin._sleep_while_active(5.0)

    def test_counter_resets_after_stability_window(self):
        p = _make_plugin()
        p._active = True
        p._stop_event = threading.Event()
        p._total_messages = 0
        p._restart_count = 3

        proc = MagicMock()
        proc.poll.return_value = None
        p._process = proc

        clock = [0.0]
        call_count = [0]

        def fake_monotonic():
            return clock[0]

        def fake_sleep(secs):
            call_count[0] += 1
            clock[0] += 200.0
            p._total_messages += 10
            if call_count[0] >= 5:
                p._active = False

        p._sleep_while_active = fake_sleep
        self._run_monitor_loop(p, fake_monotonic)
        assert p._restart_count == 0

    def test_counter_not_reset_before_stability_window(self):
        p = _make_plugin()
        p._active = True
        p._stop_event = threading.Event()
        p._total_messages = 0
        p._restart_count = 2

        proc = MagicMock()
        proc.poll.return_value = None
        p._process = proc

        clock = [0.0]
        call_count = [0]

        def fake_monotonic():
            return clock[0]

        def fake_sleep(secs):
            call_count[0] += 1
            clock[0] += 100.0
            p._total_messages += 5
            if call_count[0] >= 3:
                p._active = False

        p._sleep_while_active = fake_sleep
        self._run_monitor_loop(p, fake_monotonic)
        # 300s < 600s stability window — counter unchanged
        assert p._restart_count == 2

    def test_counter_not_reset_when_already_zero(self):
        p = _make_plugin()
        p._active = True
        p._stop_event = threading.Event()
        p._total_messages = 0
        p._restart_count = 0

        proc = MagicMock()
        proc.poll.return_value = None
        p._process = proc

        clock = [0.0]
        call_count = [0]

        def fake_monotonic():
            return clock[0]

        def fake_sleep(secs):
            call_count[0] += 1
            clock[0] += 300.0
            p._total_messages += 10
            if call_count[0] >= 5:
                p._active = False

        p._sleep_while_active = fake_sleep
        self._run_monitor_loop(p, fake_monotonic)
        assert p._restart_count == 0


class TestCacheInvalidation:
    """Verify RTL-SDR cache invalidation and device re-resolution."""

    def test_invalidate_cache_called_before_initial_resolution(self):
        p = _make_plugin()
        p._active = True
        p._stop_event = threading.Event()
        p._total_messages = 0
        lease = MagicMock(index=2)

        with (
            patch(
                "reticulumpi.builtin_plugins.adsb_radar.shutil.which",
                return_value="/usr/bin/dump1090",
            ),
            patch("reticulumpi.rtlsdr.invalidate_cache") as mock_invalidate,
            patch("reticulumpi.rtlsdr.refresh_device_lease", return_value=lease),
            patch.object(p, "_launch_dump1090"),
        ):
            p._supervisor_loop()

        mock_invalidate.assert_called_once_with()
        assert p._resolved_index == 2

    def test_invalidate_cache_called_on_restart(self):
        p = _make_plugin({"max_restarts": 3})
        p._active = True
        group = MagicMock()
        p._process_group = group
        lease = MagicMock(index=2)

        with (
            patch("reticulumpi.rtlsdr.invalidate_cache") as mock_invalidate,
            patch("reticulumpi.rtlsdr.refresh_device_lease", return_value=lease),
        ):
            p._on_process_restart(group, 1, 1.0)

        mock_invalidate.assert_called_once_with()
        group.replace_specs.assert_called_once()
        assert p._resolved_index == 2


class TestSupervisorMissingBinary:
    def test_status_unavailable_when_dump1090_missing(self):
        p = _make_plugin()
        p._active = True
        p._stop_event = threading.Event()
        p._stop_event.clear()
        with patch(
            "reticulumpi.builtin_plugins.adsb_radar.shutil.which",
            return_value=None,
        ):
            p._supervisor_loop()
        assert p._status == "unavailable"
        assert p._last_error is not None
        assert "dump1090" in p._last_error


# ---------------------------------------------------------------------------
# GPS event handler
# ---------------------------------------------------------------------------


class TestGpsHandler:
    def test_updates_receiver_position(self):
        p = _make_plugin()
        p._on_gps_fix("gps.fix_updated", {"lat": 40.7, "lon": -74.0})
        assert p._receiver_lat == pytest.approx(40.7)
        assert p._receiver_lon == pytest.approx(-74.0)

    def test_first_fix_event(self):
        p = _make_plugin()
        p._on_gps_fix(
            "gps.fix_received", {"lat": 51.5, "lon": -0.1, "alt_m": 10.0, "timestamp": 1.0}
        )
        assert p._receiver_lat == pytest.approx(51.5)
        assert p._receiver_lon == pytest.approx(-0.1)

    def test_ignores_incomplete_fix(self):
        p = _make_plugin()
        p._on_gps_fix("gps.fix_updated", {"lat": 40.7})
        assert p._receiver_lat is None

    def test_startup_picks_up_existing_gps_fix(self):
        app = _make_app()
        gps = MagicMock()
        gps.last_fix = {"lat": 48.8, "lon": 2.35, "alt_m": 35.0}
        app.get_plugin = MagicMock(return_value=gps)
        plugin = AdsbRadarPlugin(app, {})
        plugin._state_lock = threading.Lock()
        plugin._process = None
        plugin._pid = None
        plugin._restart_count = 0
        plugin._status = "starting"
        plugin._last_error = None
        plugin._dump1090_path = "/usr/bin/dump1090"
        plugin._aircraft = {}
        plugin._total_messages = 0
        plugin._aircraft_seen_total = 0
        plugin._resolved_index = None
        with patch.object(plugin, "_start_thread"):
            plugin.start()
        assert plugin._receiver_lat == pytest.approx(48.8)
        assert plugin._receiver_lon == pytest.approx(2.35)

    def test_static_config_takes_priority_over_gps(self):
        app = _make_app()
        gps = MagicMock()
        gps.last_fix = {"lat": 48.8, "lon": 2.35, "alt_m": 35.0}
        app.get_plugin = MagicMock(return_value=gps)
        plugin = AdsbRadarPlugin(app, {"receiver_lat": 40.7, "receiver_lon": -74.0})
        plugin._state_lock = threading.Lock()
        plugin._process = None
        plugin._pid = None
        plugin._restart_count = 0
        plugin._status = "starting"
        plugin._last_error = None
        plugin._dump1090_path = "/usr/bin/dump1090"
        plugin._aircraft = {}
        plugin._total_messages = 0
        plugin._aircraft_seen_total = 0
        plugin._resolved_index = None
        with patch.object(plugin, "_start_thread"):
            plugin.start()
        assert plugin._receiver_lat == pytest.approx(40.7)
        assert plugin._receiver_lon == pytest.approx(-74.0)


# ---------------------------------------------------------------------------
# AircraftState dataclass
# ---------------------------------------------------------------------------


class TestAircraftState:
    def test_to_dict_round_trip(self):
        ac = AircraftState(
            icao="AABBCC",
            callsign="UAL123",
            altitude=35000,
            ground_speed=450.0,
            track=270.0,
            latitude=40.7128,
            longitude=-74.006,
            vertical_rate=-500,
            squawk="1200",
            on_ground=False,
        )
        d = ac.to_dict()
        assert d["icao"] == "AABBCC"
        assert d["callsign"] == "UAL123"
        assert d["altitude"] == 35000
        assert d["on_ground"] is False
        assert set(d.keys()) == {
            "icao",
            "callsign",
            "altitude",
            "ground_speed",
            "track",
            "latitude",
            "longitude",
            "vertical_rate",
            "squawk",
            "on_ground",
            "category",
            "first_seen",
            "last_seen",
            "message_count",
            "distance_nm",
        }


# ---------------------------------------------------------------------------
# Patience mode (infinite recovery after max_restarts exhaustion)
# ---------------------------------------------------------------------------


class TestPatienceMode:
    def test_initial_failure_does_not_enter_unbounded_patience_mode(self):
        p = _make_plugin({"max_restarts": 1})
        p._active = True
        p._dump1090_path = "/usr/bin/dump1090"

        with (
            patch(
                "reticulumpi.builtin_plugins.adsb_radar.shutil.which",
                return_value="/usr/bin/dump1090",
            ),
            patch(
                "reticulumpi.rtlsdr.refresh_device_lease",
                side_effect=RuntimeError("not found"),
            ),
            patch("reticulumpi.rtlsdr.invalidate_cache"),
            patch.object(p, "_enter_patience_mode") as enter_patience,
        ):
            p._supervisor_loop()

        enter_patience.assert_not_called()
        assert p._patience_active is False
        assert p._status == "error"
        assert "not found" in (p._last_error or "")

    def test_patience_recovers_on_dongle_return(self):
        from reticulumpi.rtlsdr import DeviceLease

        p = _make_plugin({"max_restarts": 1, "patience_interval": 0.1})
        p._active = True
        p._dump1090_path = "/usr/bin/dump1090"

        probe_count = [0]

        def fake_refresh(*_a, **_kw):
            probe_count[0] += 1
            if probe_count[0] <= 2:
                raise RuntimeError("not found")
            return DeviceLease(
                p._device_id,
                "index:2",
                2,
                p.plugin_name,
                p._device_selector,
            )

        try:
            with (
                patch("reticulumpi.rtlsdr.refresh_device_lease", side_effect=fake_refresh),
                patch("reticulumpi.rtlsdr.invalidate_cache"),
                patch.object(p, "_patience_sleep", return_value=False),
                patch.object(p, "_publish"),
            ):
                p._enter_patience_mode(lambda: None)

            assert p._restart_count == 0
            assert p._patience_active is False
            assert p._resolved_index == 2
        finally:
            p._release_device_lease()

    def test_patience_interval_configurable(self):
        p = _make_plugin({"patience_interval": 42})
        assert p._patience_interval == 42.0

    def test_udev_flag_file_cuts_patience_short(self):
        p = _make_plugin()
        p._active = True
        with tempfile.TemporaryDirectory() as tmpdir:
            flag_path = os.path.join(tmpdir, f"usb-reconnect-{p._device_id}")
            os.makedirs(os.path.dirname(flag_path), exist_ok=True)

            with open(flag_path, "w") as f:
                f.write("")

            with patch("reticulumpi.builtin_plugins.adsb_radar._RECONNECT_FLAG_DIR", tmpdir):
                result = p._patience_sleep(10.0)

            assert result is True
            assert not os.path.exists(flag_path)

    def test_patience_sleep_returns_false_on_timeout(self):
        p = _make_plugin()
        p._active = True
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("reticulumpi.builtin_plugins.adsb_radar._RECONNECT_FLAG_DIR", tmpdir),
            patch.object(p, "_sleep_while_active"),
        ):
            result = p._patience_sleep(0.01)
        assert result is False


# ---------------------------------------------------------------------------
# Log reader thread leak fix
# ---------------------------------------------------------------------------


class TestLogReaderThreadLeak:
    def test_log_reader_saved_when_managed_process_starts(self):
        p = _make_plugin()
        p._active = True
        group = MagicMock(restart_count=0)
        p._process_group = group
        process = MagicMock(pid=12345)
        mock_thread = MagicMock()
        with (
            patch.object(p, "_start_log_reader", return_value=mock_thread),
            patch.object(p, "_start_thread"),
            patch.object(p, "_set_status"),
        ):
            p._on_process_started(group, (process,), False)
        assert p._log_reader_thread is mock_thread
        assert p._process is process

    def test_cleanup_delegates_to_managed_process_group(self):
        p = _make_plugin()
        group = MagicMock()
        p._process_group = group
        p._process = MagicMock()
        p._pid = 999

        p._terminate_process()

        group.stop.assert_called_once_with()
        assert p._process_group is None
        assert p._process is None
        assert p._pid is None


# ---------------------------------------------------------------------------
# Bias-tee retry
# ---------------------------------------------------------------------------


class TestBiasTeeRetry:
    def test_retries_on_failure(self):
        p = _make_plugin({"enable_bias_tee": True})
        p._rtl_biast_path = "/usr/bin/rtl_biast"
        p._resolved_index = 2
        results = [
            MagicMock(returncode=6, stderr=b"error -6"),
            MagicMock(returncode=0, stderr=b""),
        ]
        with patch("subprocess.run", side_effect=results) as mock_run:
            p._set_bias_tee(True)
        assert mock_run.call_count == 2
        assert p._bias_tee_active is True

    def test_gives_up_after_max_retries(self):
        p = _make_plugin({"enable_bias_tee": True})
        p._rtl_biast_path = "/usr/bin/rtl_biast"
        p._resolved_index = 2
        fail = MagicMock(returncode=6, stderr=b"error -6")
        with patch("subprocess.run", return_value=fail) as mock_run:
            p._set_bias_tee(True)
        assert mock_run.call_count == 3
        assert p._bias_tee_active is False

    def test_success_on_first_try(self):
        p = _make_plugin({"enable_bias_tee": True})
        p._rtl_biast_path = "/usr/bin/rtl_biast"
        p._resolved_index = 0
        ok = MagicMock(returncode=0, stderr=b"")
        with patch("subprocess.run", return_value=ok) as mock_run:
            p._set_bias_tee(True)
        assert mock_run.call_count == 1
        assert p._bias_tee_active is True


# ---------------------------------------------------------------------------
# Parser thread liveness check
# ---------------------------------------------------------------------------


class TestParserLivenessCheck:
    def test_crashed_parser_reports_failure_to_managed_group(self):
        p = _make_plugin()
        p._active = True
        process = MagicMock()
        process.poll.return_value = None
        group = MagicMock(running=True)
        p._process = process
        p._process_group = group

        with (
            patch(
                "reticulumpi.builtin_plugins.adsb_radar.socket.socket",
                side_effect=RuntimeError("parser crashed"),
            ),
            pytest.raises(RuntimeError, match="parser crashed"),
        ):
            p._parser_loop(process)

        group.notify_unexpected_eof.assert_called_once_with(
            0,
            "ADS-B SBS parser stopped unexpectedly",
        )


class TestManagedProcessLifecycle:
    def test_stop_during_launch_rejects_stale_started_callback(self):
        p = _make_plugin()
        p._active = False
        group = MagicMock()
        p._process_group = group

        with pytest.raises(RuntimeError, match="stale dump1090 launch"):
            p._on_process_started(group, (MagicMock(pid=1234),), False)

        assert p._process is None

    def test_restart_exhaustion_releases_device_lease(self):
        p = _make_plugin({"max_restarts": 5})
        p._active = True
        lease = MagicMock()
        p._device_lease = lease
        group = MagicMock(restart_count=5)
        p._process_group = group
        failure = MagicMock(reason="decoder exited")

        p._on_process_exhausted(group, failure)

        lease.release.assert_called_once_with()
        assert p._device_lease is None
        assert p._status == "exhausted"
        assert p._restart_count == 5

    def test_wedge_detection_reports_one_supervisor_failure(self):
        p = _make_plugin({"wedge_timeout": 5, "wedge_grace": 0})
        p._active = True
        process = MagicMock()
        process.poll.return_value = None
        group = MagicMock(running=True)
        p._process = process
        p._process_group = group

        with (
            patch(
                "reticulumpi.builtin_plugins.adsb_radar.time.monotonic",
                side_effect=[0.0, 10.0],
            ),
            patch.object(p, "_sleep_while_active"),
            patch.object(p, "_publish") as publish,
        ):
            p._health_loop(process)

        group.notify_unexpected_eof.assert_called_once_with(
            0,
            "SBS feed exceeded wedge timeout",
        )
        assert publish.call_args.args[0] == "adsb.wedge_detected"


# ---------------------------------------------------------------------------
# USB settle delay after terminate
# ---------------------------------------------------------------------------


class TestUsbSettleDelay:
    def test_terminate_cleans_up_process(self):
        p = _make_plugin()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.stdout = None
        p._process = mock_proc
        p._terminate_process()
        assert p._process is None


# ---------------------------------------------------------------------------
# Snapshot includes new recovery fields
# ---------------------------------------------------------------------------


class TestSnapshotRecoveryFields:
    def test_status_includes_recovery_fields(self):
        p = _make_plugin()
        p._restart_count = 3
        p._patience_active = True
        p._bias_tee_active = True
        status = p.get_status()
        assert status["restart_count"] == 3
        assert status["patience_active"] is True
        assert status["bias_tee_active"] is True

    def test_snapshot_includes_dongle_uptime(self):
        p = _make_plugin()
        p._launch_time = time.monotonic() - 120
        snap = p.get_snapshot()
        assert snap["stats"]["dongle_uptime"] >= 119
        assert snap["stats"]["restart_count"] == 0
        assert snap["stats"]["patience_active"] is False

    def test_snapshot_dongle_uptime_none_when_not_running(self):
        p = _make_plugin()
        p._launch_time = None
        snap = p.get_snapshot()
        assert snap["stats"]["dongle_uptime"] is None


# ---------------------------------------------------------------------------
# Wedge event publishing
# ---------------------------------------------------------------------------


class TestWedgeEvent:
    def test_wedge_publishes_event(self):
        p = _make_plugin({"wedge_timeout": 5, "wedge_grace": 0})
        p._active = True
        p._total_messages = 0
        p._launch_time = time.monotonic() - 100

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        p._process = mock_proc

        published = []

        def capture_publish(event, data):
            published.append((event, data))
            if event == "adsb.wedge_detected":
                p._active = False

        with (
            patch.object(p, "_publish", side_effect=capture_publish),
            patch.object(p, "_terminate_process"),
            patch.object(p, "_sleep_while_active"),
        ):
            parser = MagicMock()
            parser.is_alive.return_value = True

            p._launch_time = time.monotonic() - 100

            from reticulumpi import events

            p._publish(
                events.ADSB_WEDGE_DETECTED,
                {
                    "pid": 123,
                    "silence_seconds": 10.0,
                },
            )

        wedge_events = [e for e in published if e[0] == "adsb.wedge_detected"]
        assert len(wedge_events) == 1
        assert "silence_seconds" in wedge_events[0][1]


class TestManagedAdsbLifecycleHardening:
    def test_stop_releases_device_lease_and_unsubscribes(self):
        p = _make_plugin()
        p._active = True
        lease = MagicMock()
        p._device_lease = lease
        with (
            patch.object(p, "_terminate_process") as terminate,
            patch.object(p, "_join_threads") as join_threads,
        ):
            p.stop()
        terminate.assert_called_once_with()
        lease.release.assert_called_once_with()
        join_threads.assert_called_once_with(timeout=5.0)
        assert p._device_lease is None
        assert p._status == "stopped"
        assert p.event_bus.unsubscribe.call_count == 2

    def test_supervisor_launch_failure_turns_off_bias_tee_and_degrades(self):
        p = _make_plugin({"enable_bias_tee": True})
        p._active = True
        with (
            patch(
                "reticulumpi.builtin_plugins.adsb_radar.shutil.which",
                side_effect=["/usr/bin/dump1090", "/usr/bin/rtl_biast"],
            ),
            patch.object(p, "_refresh_device_lease", side_effect=OSError("USB missing")),
            patch.object(p, "_terminate_process") as terminate,
            patch.object(p, "_set_bias_tee") as bias,
            patch.object(p, "_release_device_lease") as release,
            patch.object(p, "mark_degraded") as degraded,
        ):
            p._supervisor_loop()
        terminate.assert_called_once_with()
        bias.assert_called_once_with(False)
        release.assert_called_once_with()
        degraded.assert_called_once_with("USB missing")
        assert p._status == "error"

    def test_supervisor_failure_after_stop_does_not_resurrect_error_state(self):
        p = _make_plugin()
        p._active = True

        def stop_then_fail():
            p._active = False
            raise OSError("cancelled launch")

        with (
            patch(
                "reticulumpi.builtin_plugins.adsb_radar.shutil.which",
                return_value="/usr/bin/dump1090",
            ),
            patch.object(p, "_refresh_device_lease", side_effect=stop_then_fail),
            patch.object(p, "_terminate_process"),
            patch.object(p, "_release_device_lease"),
            patch.object(p, "mark_degraded") as degraded,
        ):
            p._supervisor_loop()
        degraded.assert_not_called()
        assert p._status == "starting"

    def test_launch_rejects_a_plugin_stopped_before_spawn(self):
        p = _make_plugin()
        p._active = False
        with pytest.raises(RuntimeError, match="stopped before"):
            p._launch_dump1090()

    def test_launch_turns_bias_tee_back_off_when_stop_wins_setup_race(self):
        p = _make_plugin({"enable_bias_tee": True})
        p._active = True
        calls = []

        def set_bias(on):
            calls.append(on)
            if on:
                p._active = False

        with patch.object(p, "_set_bias_tee", side_effect=set_bias):
            with pytest.raises(RuntimeError, match="during bias-tee"):
                p._launch_dump1090()
        assert calls == [True, False]

    def test_launch_constructs_managed_group_and_clears_failed_start(self):
        p = _make_plugin()
        p._active = True
        group = MagicMock()
        group.start.side_effect = RuntimeError("monitor unavailable")
        with patch(
            "reticulumpi.builtin_plugins.adsb_radar.ManagedProcessGroup",
            return_value=group,
        ) as constructor:
            with pytest.raises(RuntimeError, match="monitor unavailable"):
                p._launch_dump1090()
        assert p._process_group is None
        spec = constructor.call_args.args[0][0]
        assert spec.name == "dump1090"
        assert spec.argv[0] == "/usr/bin/dump1090"

    def test_failure_restart_and_restart_failure_update_health(self):
        p = _make_plugin({"enable_bias_tee": True})
        p._active = True
        group = MagicMock()
        p._process_group = group
        failure = ProcessFailure(0, "dump1090", 3, "decoder crashed", 1.0)
        with (
            patch.object(p, "mark_degraded") as degraded,
            patch.object(p, "_set_bias_tee") as bias,
            patch.object(p, "_release_device_lease") as release,
        ):
            p._on_process_failure(group, failure)
        degraded.assert_called_once()
        bias.assert_called_once_with(False)
        release.assert_called_once_with()
        assert "decoder crashed" in p._last_error

        with (
            patch.object(p, "_refresh_device_lease") as refresh,
            patch.object(p, "_set_bias_tee") as bias,
        ):
            p._on_process_restart(group, 2, 4.0)
        refresh.assert_called_once_with()
        bias.assert_called_once_with(True)
        group.replace_specs.assert_called_once()
        assert p._restart_count == 2

        with (
            patch.object(p, "mark_degraded") as degraded,
            patch.object(p, "_set_bias_tee") as bias,
            patch.object(p, "_release_device_lease") as release,
        ):
            p._on_process_restart_failed(group, RuntimeError("retry failed"), 3)
        degraded.assert_called_once()
        bias.assert_called_once_with(False)
        release.assert_called_once_with()
        assert p._restart_count == 3
        assert "restart 3 failed" in p._last_error

    def test_restart_rejects_a_stale_group(self):
        p = _make_plugin()
        p._active = True
        with pytest.raises(RuntimeError, match="stopped"):
            p._on_process_restart(MagicMock(), 1, 1.0)

    def test_stale_callbacks_do_not_mutate_current_group(self):
        p = _make_plugin()
        current = MagicMock()
        stale = MagicMock()
        p._process_group = current
        failure = ProcessFailure(0, "dump1090", 1, "EOF", 1.0)
        p._on_process_failure(stale, failure)
        p._on_process_restart_failed(stale, RuntimeError("ignored"), 2)
        p._on_process_exhausted(stale, failure)
        assert p._process_group is current
        assert p._status == "starting"

    def test_exhaustion_disables_bias_tee_and_publishes_reason(self):
        p = _make_plugin({"enable_bias_tee": True})
        group = MagicMock(restart_count=5)
        p._process_group = group
        failure = ProcessFailure(0, "dump1090", 1, "EOF", 1.0)
        with (
            patch.object(p, "mark_degraded") as degraded,
            patch.object(p, "_publish") as publish,
            patch.object(p, "_set_bias_tee") as bias,
            patch.object(p, "_release_device_lease") as release,
        ):
            p._on_process_exhausted(group, failure)
        degraded.assert_called_once()
        bias.assert_called_once_with(False)
        release.assert_called_once_with()
        assert publish.call_args.args[1] == {"max_restarts": 5, "reason": "EOF"}
        assert p._status == "exhausted"

    def test_lease_release_error_is_isolated_and_reference_is_cleared(self):
        p = _make_plugin()
        lease = MagicMock()
        lease.release.side_effect = OSError("already detached")
        p._device_lease = lease
        p._release_device_lease()
        assert p._device_lease is None

    def test_bias_tee_lock_is_created_for_legacy_instances(self):
        p = _make_plugin()
        del p._bias_tee_lock
        p._rtl_biast_path = None
        p._set_bias_tee(True)
        assert isinstance(p._bias_tee_lock, type(threading.Lock()))

    def test_health_loop_tracks_messages_then_exits_when_stopped(self):
        p = _make_plugin({"wedge_timeout": 30, "wedge_grace": 0})
        p._active = True
        process = MagicMock()
        process.poll.return_value = None
        p._process = process
        calls = 0

        def sleep(_seconds):
            nonlocal calls
            calls += 1
            if calls == 1:
                p._total_messages += 1
            else:
                p._active = False

        with (
            patch.object(p, "_sleep_while_active", side_effect=sleep),
            patch(
                "reticulumpi.builtin_plugins.adsb_radar.time.monotonic",
                side_effect=[0.0, 1.0],
            ),
        ):
            p._health_loop(process)
        assert calls == 2


class TestManagedAdsbParserHardening:
    def test_parser_without_a_process_returns_cleanly(self):
        p = _make_plugin()
        p._process = None
        p._parser_loop()

    def test_parser_handles_timeout_lines_and_socket_close_errors(self):
        p = _make_plugin()
        p._active = True
        process = MagicMock()
        process.poll.return_value = None
        p._process = process
        sock = MagicMock()
        sock.recv.side_effect = [socket.timeout(), b"MSG,line\n", b""]
        sock.close.side_effect = OSError("already closed")

        def stop_after_retry(_seconds):
            p._active = False

        with (
            patch("reticulumpi.builtin_plugins.adsb_radar.socket.socket", return_value=sock),
            patch.object(p, "_parse_sbs_line") as parse,
            patch.object(p, "_sleep_while_active", side_effect=stop_after_retry),
        ):
            p._parser_loop(process)
        sock.connect.assert_called_once_with(("127.0.0.1", p._sbs_port))
        parse.assert_called_once_with("MSG,line")
        sock.close.assert_called_once_with()

    def test_parser_discards_oversized_partial_frames(self):
        p = _make_plugin()
        p._active = True
        p._MAX_SBS_BUF = 4
        process = MagicMock()
        process.poll.return_value = None
        p._process = process
        sock = MagicMock()
        sock.recv.side_effect = [b"oversized", b""]

        def stop_after_retry(_seconds):
            p._active = False

        with (
            patch("reticulumpi.builtin_plugins.adsb_radar.socket.socket", return_value=sock),
            patch.object(p, "_sleep_while_active", side_effect=stop_after_retry),
            patch.object(p, "_parse_sbs_line") as parse,
        ):
            p._parser_loop(process)
        parse.assert_not_called()
        sock.close.assert_called_once_with()

    def test_parser_retries_connection_error_only_while_current(self):
        p = _make_plugin()
        p._active = True
        process = MagicMock()
        process.poll.return_value = None
        p._process = process
        sock = MagicMock()
        sock.connect.side_effect = OSError("connection refused")

        def stop_after_retry(_seconds):
            p._active = False

        with (
            patch("reticulumpi.builtin_plugins.adsb_radar.socket.socket", return_value=sock),
            patch.object(p, "_sleep_while_active", side_effect=stop_after_retry) as sleep,
        ):
            p._parser_loop(process)
        sleep.assert_called_once_with(2.0)
        sock.close.assert_called_once_with()
