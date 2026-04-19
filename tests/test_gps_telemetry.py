"""Tests for the gps_telemetry plugin.

Uses a fake serial.Serial so no physical GPS is required; canned NMEA sentences
exercise RMC/GGA/GSA/GSV/VTG ingestion and the reconnect/stale code paths.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Sample NMEA fixtures
# ---------------------------------------------------------------------------

RMC_VALID = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"
GGA_VALID = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
GSA_3D    = "$GPGSA,A,3,04,05,,09,12,,,24,,,,,2.5,1.3,2.1*39"
VTG_VALID = "$GPVTG,054.7,T,034.4,M,005.5,N,010.2,K*48"

# A three-message GSV set from the NMEA spec example (12 sats across 3 msgs of 4)
GSV_1 = "$GPGSV,3,1,11,03,03,111,00,04,15,270,00,06,01,010,00,13,06,292,00*74"
GSV_2 = "$GPGSV,3,2,11,14,25,170,00,16,57,208,39,18,67,296,40,19,40,246,00*74"
GSV_3 = "$GPGSV,3,3,11,22,42,067,42,24,14,311,43,27,05,244,00*4D"


# ---------------------------------------------------------------------------
# Fake serial.Serial
# ---------------------------------------------------------------------------


class FakeSerial:
    """Stand-in for serial.Serial: hands out canned lines then blocks/timeouts."""

    def __init__(self, port: str, baudrate: int, timeout: float = 2.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._lines: list[bytes] = []
        self._idx = 0
        self.closed = False
        # Let tests seed/steer the stream
        self._exhausted = threading.Event()
        self._raise_next: BaseException | None = None

    def feed(self, sentence: str) -> None:
        self._lines.append((sentence + "\r\n").encode("ascii"))

    def raise_next(self, exc: BaseException) -> None:
        self._raise_next = exc

    def readline(self) -> bytes:
        if self._raise_next is not None:
            exc = self._raise_next
            self._raise_next = None
            raise exc
        if self.closed:
            # readline on closed serial should surface an error
            import serial
            raise serial.SerialException("port closed")
        if self._idx < len(self._lines):
            line = self._lines[self._idx]
            self._idx += 1
            return line
        self._exhausted.set()
        # Emulate read timeout — return empty so the plugin loops
        time.sleep(0.01)
        return b""

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Plugin import + fixtures
# ---------------------------------------------------------------------------


def _make_plugin(mock_app, config, *, start: bool = False):
    """Construct the plugin, optionally calling start()."""
    from reticulumpi.builtin_plugins.gps_telemetry import GpsTelemetry

    plugin = GpsTelemetry(mock_app, config)
    if start:
        plugin.start()
    return plugin


@pytest.fixture
def gps_config():
    return {
        "enabled": True,
        "serial_port": "/dev/ttyUSB0",
        "baudrate": 4800,
        "read_timeout": 0.1,
        "reconnect_delay": 1,
        "max_reconnect_attempts": 0,
        "fix_stale_seconds": 5,
        "stale_check_interval": 1,
    }


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------


class TestValidateConfig:
    def test_valid_config_accepted(self, mock_app, gps_config):
        plugin = _make_plugin(mock_app, gps_config)
        assert plugin.plugin_name == "gps_telemetry"

    def test_raises_for_empty_serial_port(self, mock_app, gps_config):
        gps_config["serial_port"] = ""
        with pytest.raises(ValueError, match="serial_port"):
            _make_plugin(mock_app, gps_config)

    def test_raises_for_bad_baudrate(self, mock_app, gps_config):
        gps_config["baudrate"] = 0
        with pytest.raises(ValueError, match="baudrate"):
            _make_plugin(mock_app, gps_config)

    def test_raises_for_bad_read_timeout(self, mock_app, gps_config):
        gps_config["read_timeout"] = 0
        with pytest.raises(ValueError, match="read_timeout"):
            _make_plugin(mock_app, gps_config)

    def test_raises_for_bad_reconnect_delay(self, mock_app, gps_config):
        gps_config["reconnect_delay"] = 0
        with pytest.raises(ValueError, match="reconnect_delay"):
            _make_plugin(mock_app, gps_config)

    def test_raises_for_bad_max_reconnect_attempts(self, mock_app, gps_config):
        gps_config["max_reconnect_attempts"] = -1
        with pytest.raises(ValueError, match="max_reconnect_attempts"):
            _make_plugin(mock_app, gps_config)

    def test_raises_for_bad_fix_stale_seconds(self, mock_app, gps_config):
        gps_config["fix_stale_seconds"] = 1
        with pytest.raises(ValueError, match="fix_stale_seconds"):
            _make_plugin(mock_app, gps_config)

    def test_raises_if_deps_missing(self, mock_app, gps_config):
        with patch.dict(sys.modules, {"pynmea2": None}):
            with pytest.raises(ValueError, match="pynmea2"):
                _make_plugin(mock_app, gps_config)


# ---------------------------------------------------------------------------
# Sentence dispatch (invoked directly; no threads / no real serial)
# ---------------------------------------------------------------------------


class TestSentenceHandling:
    def test_rmc_populates_last_fix_and_publishes_first_fix(
        self, mock_app, gps_config
    ):
        from reticulumpi import events

        plugin = _make_plugin(mock_app, gps_config)
        # Bypass the full start(): initialise state directly
        plugin._active = True
        plugin._lock = threading.Lock()
        plugin.last_fix = None
        plugin._satellites_in_view = []
        plugin._gsv_accum = {}
        plugin._gsv_expected = {}
        plugin._have_fix = False
        plugin._msgs_received = 0
        plugin._sentences_parsed = 0
        plugin._parse_errors = 0
        plugin._last_msg_time = 0.0

        plugin._handle_sentence(RMC_VALID)

        fix = plugin.last_fix
        assert fix is not None
        assert fix["lat"] == pytest.approx(48.1173, rel=1e-3)
        assert fix["lon"] == pytest.approx(11.5167, rel=1e-3)
        assert fix["speed_kn"] == pytest.approx(22.4)
        assert fix["heading_deg"] == pytest.approx(84.4)
        assert fix["utc_time"] == "12:35:19"
        assert fix["utc_date"] == "1994-03-23"

        published = [c.args[0] for c in mock_app.event_bus.publish.call_args_list]
        assert events.GPS_FIX_RECEIVED in published

    def test_second_rmc_publishes_fix_updated(self, mock_app, gps_config):
        from reticulumpi import events

        plugin = _make_plugin(mock_app, gps_config)
        plugin._active = True
        plugin._lock = threading.Lock()
        plugin.last_fix = None
        plugin._satellites_in_view = []
        plugin._gsv_accum = {}
        plugin._gsv_expected = {}
        plugin._have_fix = False
        plugin._msgs_received = 0
        plugin._sentences_parsed = 0
        plugin._parse_errors = 0
        plugin._last_msg_time = 0.0

        plugin._handle_sentence(RMC_VALID)
        mock_app.event_bus.publish.reset_mock()
        plugin._handle_sentence(RMC_VALID)
        published = [c.args[0] for c in mock_app.event_bus.publish.call_args_list]
        assert events.GPS_FIX_UPDATED in published
        assert events.GPS_FIX_RECEIVED not in published

    def test_gga_fills_altitude_hdop_quality_sats(self, mock_app, gps_config):
        plugin = _make_plugin(mock_app, gps_config)
        plugin._active = True
        plugin._lock = threading.Lock()
        plugin.last_fix = None
        plugin._satellites_in_view = []
        plugin._gsv_accum = {}
        plugin._gsv_expected = {}
        plugin._have_fix = False
        plugin._msgs_received = 0
        plugin._sentences_parsed = 0
        plugin._parse_errors = 0
        plugin._last_msg_time = 0.0

        # RMC first so last_fix exists; then GGA augments it.
        plugin._handle_sentence(RMC_VALID)
        plugin._handle_sentence(GGA_VALID)

        fix = plugin.last_fix
        assert fix["alt_m"] == pytest.approx(545.4)
        assert fix["hdop"] == pytest.approx(0.9)
        assert fix["fix_quality"] == 1
        assert fix["satellites_used"] == 8

    def test_gsa_fills_fix_type_and_dops(self, mock_app, gps_config):
        plugin = _make_plugin(mock_app, gps_config)
        plugin._active = True
        plugin._lock = threading.Lock()
        plugin.last_fix = None
        plugin._satellites_in_view = []
        plugin._gsv_accum = {}
        plugin._gsv_expected = {}
        plugin._have_fix = False
        plugin._msgs_received = 0
        plugin._sentences_parsed = 0
        plugin._parse_errors = 0
        plugin._last_msg_time = 0.0

        plugin._handle_sentence(RMC_VALID)
        plugin._handle_sentence(GSA_3D)

        fix = plugin.last_fix
        assert fix["fix_type"] == 3
        assert fix["pdop"] == pytest.approx(2.5)
        assert fix["vdop"] == pytest.approx(2.1)

    def test_gsv_reassembles_full_group(self, mock_app, gps_config):
        plugin = _make_plugin(mock_app, gps_config)
        plugin._active = True
        plugin._lock = threading.Lock()
        plugin.last_fix = None
        plugin._satellites_in_view = []
        plugin._gsv_accum = {}
        plugin._gsv_expected = {}
        plugin._have_fix = False
        plugin._msgs_received = 0
        plugin._sentences_parsed = 0
        plugin._parse_errors = 0
        plugin._last_msg_time = 0.0

        plugin._handle_sentence(GSV_1)
        # Partial — should still be empty until group completes
        assert plugin._satellites_in_view == []
        plugin._handle_sentence(GSV_2)
        assert plugin._satellites_in_view == []
        plugin._handle_sentence(GSV_3)

        # Eleven satellites promised across 3 messages (4+4+3)
        assert len(plugin._satellites_in_view) == 11
        prns = sorted(s["prn"] for s in plugin._satellites_in_view)
        assert prns == [3, 4, 6, 13, 14, 16, 18, 19, 22, 24, 27]
        # SNR is blank for some sats — ensure it surfaces as None
        by_prn = {s["prn"]: s for s in plugin._satellites_in_view}
        assert by_prn[3]["snr_db"] is None or by_prn[3]["snr_db"] == 0
        assert by_prn[22]["snr_db"] == 42

    def test_malformed_sentence_bumps_parse_errors(self, mock_app, gps_config):
        plugin = _make_plugin(mock_app, gps_config)
        plugin._active = True
        plugin._lock = threading.Lock()
        plugin.last_fix = None
        plugin._satellites_in_view = []
        plugin._gsv_accum = {}
        plugin._gsv_expected = {}
        plugin._have_fix = False
        plugin._msgs_received = 0
        plugin._sentences_parsed = 0
        plugin._parse_errors = 0
        plugin._last_msg_time = 0.0

        plugin._handle_sentence("$GPABC,bogus,data,here*XX")
        assert plugin._parse_errors >= 1


# ---------------------------------------------------------------------------
# get_status / get_snapshot shape
# ---------------------------------------------------------------------------


class TestSnapshotShape:
    def test_snapshot_shape_with_fix(self, mock_app, gps_config):
        plugin = _make_plugin(mock_app, gps_config)
        plugin._active = True
        plugin._lock = threading.Lock()
        plugin.last_fix = None
        plugin._satellites_in_view = []
        plugin._gsv_accum = {}
        plugin._gsv_expected = {}
        plugin._have_fix = False
        plugin._msgs_received = 0
        plugin._sentences_parsed = 0
        plugin._parse_errors = 0
        plugin._last_msg_time = 0.0
        plugin._connected = True
        plugin._reconnect_failures = 0
        plugin._start_time = time.time()
        plugin._serial_port = "/dev/ttyUSB0"
        plugin._baudrate = 4800

        plugin._handle_sentence(RMC_VALID)
        plugin._handle_sentence(GGA_VALID)
        plugin._handle_sentence(GSA_3D)
        for s in (GSV_1, GSV_2, GSV_3):
            plugin._handle_sentence(s)

        status = plugin.get_status()
        assert set(status) >= {
            "active",
            "connected",
            "serial_port",
            "baudrate",
            "msgs_received",
            "reconnect_failures",
            "have_fix",
            "satellites_in_view_count",
        }
        assert status["have_fix"] is True
        assert status["satellites_in_view_count"] == 11

        snap = plugin.get_snapshot()
        assert snap["last_fix"] is not None
        assert snap["last_fix"]["satellites_used"] == 8
        assert len(snap["satellites_in_view"]) == 11

    def test_snapshot_shape_without_fix(self, mock_app, gps_config):
        plugin = _make_plugin(mock_app, gps_config)
        plugin._active = False
        plugin._lock = threading.Lock()
        plugin.last_fix = None
        plugin._satellites_in_view = []
        plugin._gsv_accum = {}
        plugin._gsv_expected = {}
        plugin._have_fix = False
        plugin._msgs_received = 0
        plugin._sentences_parsed = 0
        plugin._parse_errors = 0
        plugin._last_msg_time = 0.0
        plugin._connected = False
        plugin._reconnect_failures = 0
        plugin._start_time = time.time()
        plugin._serial_port = "/dev/ttyUSB0"
        plugin._baudrate = 4800

        snap = plugin.get_snapshot()
        assert snap["last_fix"] is None
        assert snap["satellites_in_view"] == []
        assert snap["have_fix"] is False


# ---------------------------------------------------------------------------
# End-to-end: run _read_loop against a fake serial
# ---------------------------------------------------------------------------


class TestReadLoopAndStaleness:
    def test_full_end_to_end_with_fake_serial(self, mock_app, gps_config):
        from reticulumpi import events

        fake = FakeSerial("/dev/ttyUSB0", 4800, timeout=0.1)
        for s in (RMC_VALID, GGA_VALID, GSA_3D, GSV_1, GSV_2, GSV_3):
            fake.feed(s)

        with patch("serial.Serial", return_value=fake):
            plugin = _make_plugin(mock_app, gps_config, start=True)
            # Wait until the fake has been drained
            fake._exhausted.wait(timeout=3)
            # Give handlers a moment
            time.sleep(0.1)
            assert plugin.last_fix is not None
            assert plugin.last_fix["lat"] == pytest.approx(48.1173, rel=1e-3)
            assert len(plugin._satellites_in_view) == 11

            published = [c.args[0] for c in mock_app.event_bus.publish.call_args_list]
            assert events.GPS_DEVICE_CONNECTED in published
            assert events.GPS_FIX_RECEIVED in published
            plugin.stop()

    def test_fix_lost_after_stale_threshold(self, mock_app, gps_config):
        from reticulumpi import events

        gps_config["fix_stale_seconds"] = 5
        gps_config["stale_check_interval"] = 1
        plugin = _make_plugin(mock_app, gps_config)
        # Minimal state wiring
        plugin._active = True
        plugin._lock = threading.Lock()
        plugin.last_fix = {
            "lat": 10.0,
            "lon": 20.0,
            "alt_m": 0.0,
            "fix_quality": 1,
            "fix_type": 3,
            "satellites_used": 8,
            "hdop": None,
            "pdop": None,
            "vdop": None,
            # Backdated so the monitor classes it as stale on the first check
            "timestamp": time.time() - 30,
        }
        plugin._have_fix = True
        plugin._stale_check_interval = 0.2
        plugin._fix_stale_seconds = 5

        # Run the monitor once manually by flipping _active off mid-wait
        t = threading.Thread(target=plugin._stale_monitor, daemon=True)
        t.start()
        # Wait long enough for one iteration
        time.sleep(0.5)
        plugin._active = False
        t.join(timeout=2)

        assert plugin._have_fix is False
        published = [c.args[0] for c in mock_app.event_bus.publish.call_args_list]
        assert events.GPS_FIX_LOST in published

    def test_reconnect_after_serial_exception(self, mock_app, gps_config):
        """Open raises once, then succeeds — ensures backoff + retry path runs."""
        from reticulumpi import events
        import serial as pyserial

        gps_config["reconnect_delay"] = 1
        gps_config["max_reconnect_attempts"] = 2  # give up fast for the test

        attempts: list[Any] = []

        def _fake_serial_ctor(*args, **kwargs):
            attempts.append(args)
            if len(attempts) == 1:
                raise pyserial.SerialException("simulated open failure")
            fake = FakeSerial(*args, **kwargs)
            # Feed one sentence so we don't spin forever
            fake.feed(RMC_VALID)
            return fake

        with patch("serial.Serial", side_effect=_fake_serial_ctor):
            plugin = _make_plugin(mock_app, gps_config, start=True)
            deadline = time.time() + 5
            while time.time() < deadline:
                if plugin._connected:
                    break
                time.sleep(0.05)
            assert len(attempts) >= 2
            assert plugin._connected is True
            plugin.stop()

        published = [c.args[0] for c in mock_app.event_bus.publish.call_args_list]
        assert events.GPS_DEVICE_CONNECTED in published


# ---------------------------------------------------------------------------
# space_tracker integration
# ---------------------------------------------------------------------------


class TestSpaceTrackerIntegration:
    def test_space_tracker_resolves_observer_from_gps_plugin(
        self, mock_app, gps_config
    ):
        """space_tracker._resolve_observer() should pick up last_fix when lat/lon
        are unset in its own config."""
        # Build a gps_telemetry plugin in a known-fixed state
        gps = _make_plugin(mock_app, gps_config)
        gps._lock = threading.Lock()
        gps.last_fix = {
            "lat": 42.5,
            "lon": -71.2,
            "alt_m": 50.0,
            "fix_quality": 1,
            "fix_type": 3,
            "satellites_used": 9,
            "hdop": 0.9,
            "pdop": 1.1,
            "vdop": 0.8,
            "timestamp": time.time(),
        }

        # Make mock_app.get_plugin resolve 'gps_telemetry' to our instance
        def _get_plugin(name: str):
            return gps if name == "gps_telemetry" else None

        mock_app.get_plugin = _get_plugin

        from reticulumpi.builtin_plugins.space_tracker import SpaceTrackerPlugin

        st = SpaceTrackerPlugin.__new__(SpaceTrackerPlugin)
        st.app = mock_app
        st._observer_cfg = {
            "latitude": None,
            "longitude": None,
            "elevation_m": 0,
        }

        obs = st._resolve_observer()
        assert obs is not None
        assert obs["lat"] == pytest.approx(42.5)
        assert obs["lon"] == pytest.approx(-71.2)
        assert obs["elev_m"] == pytest.approx(50.0)
