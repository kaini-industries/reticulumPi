"""Tests for the adsb_radar plugin.

Focuses on the pure-Python surface — config validation, SBS line parsing,
aircraft state management, snapshot generation, distance calculation, and
emergency squawk detection — without touching any real RTL-SDR hardware
or spawning dump1090.
"""

from __future__ import annotations

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
    plugin._pid = None
    plugin._restart_count = 0
    plugin._status = "starting"
    plugin._last_error = None
    plugin._dump1090_path = "/usr/bin/dump1090"
    plugin._aircraft = {}
    plugin._total_messages = 0
    plugin._aircraft_seen_total = 0
    plugin._resolved_index = None
    plugin._rtl_biast_path = None
    plugin._bias_tee_active = False
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
        assert p._gain == "max"
        assert p._ppm == 0
        assert p._enable_bias_tee is False
        assert p._sbs_port == 30003
        assert p._stale_timeout == 300
        assert p._receiver_lat is None
        assert p._receiver_lon is None

    def test_custom_config(self):
        p = _make_plugin({
            "dump1090_bin": "/opt/dump1090-fa/dump1090-fa",
            "device_index": 1,
            "gain": "40",
            "ppm": -3,
            "enable_bias_tee": True,
            "sbs_port": 30004,
            "stale_timeout": 120,
            "receiver_lat": 40.7128,
            "receiver_lon": -74.0060,
        })
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
            capture_output=True, timeout=10,
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
            capture_output=True, timeout=10,
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
        calls = [c for c in p.event_bus.publish.call_args_list
                 if c[0][0] == "adsb.emergency_squawk"]
        assert len(calls) == 1
        assert calls[0][0][1]["squawk"] == "7700"

    def test_7600_triggers_event(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(squawk="7600"))
        calls = [c for c in p.event_bus.publish.call_args_list
                 if c[0][0] == "adsb.emergency_squawk"]
        assert len(calls) == 1

    def test_7500_triggers_event(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(squawk="7500"))
        calls = [c for c in p.event_bus.publish.call_args_list
                 if c[0][0] == "adsb.emergency_squawk"]
        assert len(calls) == 1

    def test_normal_squawk_no_event(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(squawk="1200"))
        calls = [c for c in p.event_bus.publish.call_args_list
                 if c[0][0] == "adsb.emergency_squawk"]
        assert len(calls) == 0

    def test_same_emergency_squawk_only_fires_once(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(squawk="7700"))
        p._parse_sbs_line(self._msg(squawk="7700"))
        calls = [c for c in p.event_bus.publish.call_args_list
                 if c[0][0] == "adsb.emergency_squawk"]
        assert len(calls) == 1

    def test_new_aircraft_detected_event(self):
        p = _make_plugin()
        p._parse_sbs_line(self._msg(icao="AABBCC"))
        calls = [c for c in p.event_bus.publish.call_args_list
                 if c[0][0] == "adsb.aircraft_detected"]
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
        calls = [c for c in p.event_bus.publish.call_args_list
                 if c[0][0] == "adsb.aircraft_lost"]
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

    def test_device_index_fallback(self):
        p = _make_plugin({"device_index": "14342860"})
        assert p._device_id == "14342860"


# ---------------------------------------------------------------------------
# Supervisor: missing binary → graceful 'unavailable' status
# ---------------------------------------------------------------------------

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
        p._on_gps_fix("gps.fix_received", {"lat": 51.5, "lon": -0.1, "alt_m": 10.0, "timestamp": 1.0})
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
            "icao", "callsign", "altitude", "ground_speed", "track",
            "latitude", "longitude", "vertical_rate", "squawk",
            "on_ground", "category", "first_seen", "last_seen",
            "message_count", "distance_nm",
        }
