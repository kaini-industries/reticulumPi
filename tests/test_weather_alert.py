"""Tests for the weather alert plugin."""

from __future__ import annotations

import re
import time
from collections import deque
from typing import Any
from unittest.mock import MagicMock

from reticulumpi import events
from reticulumpi.builtin_plugins.weather_alert import (
    WeatherAlert,
    _compute_purge_ts,
    _fips_label,
    _parse_fips_codes,
    _parse_issued_ts,
    _SAME_RE,
)


def _make_app() -> MagicMock:
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.event_bus = MagicMock()
    return app


def _make_plugin(config: dict | None = None) -> WeatherAlert:
    plugin = WeatherAlert(_make_app(), config or {})
    plugin._active = True
    plugin._alert_history = deque(maxlen=plugin._max_history)
    plugin._active_alert = None
    plugin._stats = {
        "headers_decoded_total": 0,
        "alerts_by_type": {},
        "last_header_at": None,
    }
    plugin._decode_errors = 0
    plugin._partial_headers = 0
    plugin._retransmissions = 0
    plugin._seen_headers = {}
    plugin._status = "idle"
    plugin._last_error = None
    plugin._process = None
    plugin._pid = None
    return plugin


def _current_issued() -> str:
    """Build a SAME issued field (JJJHHMM) near the current UTC time."""
    now = time.gmtime()
    return f"{now.tm_yday:03d}{now.tm_hour:02d}{now.tm_min:02d}"


def _same_match(
    org: str = "WXR",
    event_code: str = "TOR",
    fips: str = "029510+029511",
    purge: str = "9900",
    issued: str | None = None,
    callsign: str = "KSPD/NWS",
) -> re.Match:
    if issued is None:
        issued = _current_issued()
    header = f"EAS: ZCZC-{org}-{event_code}-{fips}-{purge}-{issued}-{callsign}-"
    m = _SAME_RE.search(header)
    assert m is not None, f"Regex did not match: {header}"
    return m


class TestParseFipsCodes:
    def test_basic(self):
        assert _parse_fips_codes("029510+029511") == ["029510", "029511"]

    def test_single(self):
        assert _parse_fips_codes("029510") == ["029510"]

    def test_dash_separated(self):
        assert _parse_fips_codes("029510-029511") == ["029510", "029511"]

    def test_invalid_length_filtered(self):
        assert _parse_fips_codes("029510+12") == ["029510"]

    def test_empty(self):
        assert _parse_fips_codes("") == []


class TestFipsLabel:
    def test_known_state(self):
        assert _fips_label("029510") == "FIPS 029510 (MO-510)"

    def test_unknown_state(self):
        assert _fips_label("099999") == "FIPS 099999 (99-999)"

    def test_short_code(self):
        assert _fips_label("123") == "123"


class TestParseIssuedTs:
    def test_valid(self):
        ts = _parse_issued_ts("0011200")
        assert ts is not None
        gm = time.gmtime(ts)
        assert gm.tm_hour == 12
        assert gm.tm_min == 0

    def test_invalid_length(self):
        assert _parse_issued_ts("123") is None

    def test_invalid_chars(self):
        assert _parse_issued_ts("ABCDEFG") is None


class TestComputePurgeTs:
    def test_valid(self):
        issued = 1000000.0
        purge = _compute_purge_ts(issued, "0130")
        assert purge == issued + 1 * 3600 + 30 * 60

    def test_none_issued(self):
        assert _compute_purge_ts(None, "0100") is None

    def test_bad_purge_length(self):
        assert _compute_purge_ts(1000.0, "01") is None


class TestValidateConfig:
    def test_defaults(self):
        p = WeatherAlert(_make_app(), {})
        assert p._freq_hz == 162550000
        assert p._gain_db is None
        assert p._ppm == 0
        assert p._max_history == 50
        assert p._fips_filter is None
        assert p._forward_to_alert_system is True

    def test_custom_config(self):
        p = WeatherAlert(
            _make_app(),
            {
                "freq_mhz": 162.475,
                "gain": 40,
                "max_history": 20,
                "fips_filter": ["029510", "029511"],
                "forward_to_alert_system": False,
            },
        )
        assert p._freq_hz == 162475000
        assert p._gain_db == 40
        assert p._max_history == 20
        assert p._fips_filter == {"029510", "029511"}
        assert p._forward_to_alert_system is False


class TestHandleSameHeader:
    def test_basic_tornado_warning(self):
        p = _make_plugin()
        p._handle_same_header(_same_match(event_code="TOR"))
        assert p._stats["headers_decoded_total"] == 1
        assert len(p._alert_history) == 1
        alert = p._alert_history[0]
        assert alert["event_code"] == "TOR"
        assert alert["event_desc"] == "Tornado Warning"
        assert alert["severity"] == "extreme"
        assert alert["originator_desc"] == "National Weather Service"
        assert alert["fips_codes"] == ["029510", "029511"]
        assert alert["expired"] is False

    def test_severe_thunderstorm(self):
        p = _make_plugin()
        p._handle_same_header(_same_match(event_code="SVR"))
        alert = p._alert_history[0]
        assert alert["event_desc"] == "Severe Thunderstorm Warning"
        assert alert["severity"] == "severe"

    def test_unknown_event_code(self):
        p = _make_plugin()
        p._handle_same_header(_same_match(event_code="ZZZ"))
        alert = p._alert_history[0]
        assert alert["event_desc"] == "Unknown (ZZZ)"
        assert alert["severity"] == "info"

    def test_fips_filter_passes(self):
        p = _make_plugin({"fips_filter": ["029510"]})
        p._handle_same_header(_same_match(fips="029510+029511"))
        assert p._stats["headers_decoded_total"] == 1

    def test_fips_filter_blocks(self):
        p = _make_plugin({"fips_filter": ["099999"]})
        p._handle_same_header(_same_match(fips="029510"))
        assert p._stats["headers_decoded_total"] == 0

    def test_retransmission_detection(self):
        p = _make_plugin()
        m = _same_match()
        p._handle_same_header(m)
        p._handle_same_header(m)
        assert p._retransmissions == 1
        assert p._alert_history[0]["retransmission"] is True

    def test_active_alert_set_to_most_severe(self):
        p = _make_plugin()
        p._handle_same_header(_same_match(event_code="SVA"))
        assert p._active_alert["severity"] == "moderate"
        p._handle_same_header(_same_match(event_code="TOR"))
        assert p._active_alert["severity"] == "extreme"


class TestEventEmission:
    def test_tornado_emits_received_and_severe(self):
        p = _make_plugin()
        p._handle_same_header(_same_match(event_code="TOR"))
        calls = p.event_bus.publish.call_args_list
        event_types = [c[0][0] for c in calls]
        assert events.WEATHER_ALERT_RECEIVED in event_types
        assert events.WEATHER_ALERT_SEVERE in event_types

    def test_tornado_forwards_to_alert_system(self):
        p = _make_plugin()
        p._handle_same_header(_same_match(event_code="TOR"))
        calls = p.event_bus.publish.call_args_list
        event_types = [c[0][0] for c in calls]
        assert events.ALERT_TRIGGERED in event_types

    def test_info_event_no_severe(self):
        p = _make_plugin()
        p._handle_same_header(_same_match(event_code="RWT"))
        calls = p.event_bus.publish.call_args_list
        event_types = [c[0][0] for c in calls]
        assert events.WEATHER_ALERT_RECEIVED in event_types
        assert events.WEATHER_ALERT_SEVERE not in event_types

    def test_forward_disabled(self):
        p = _make_plugin({"forward_to_alert_system": False})
        p._handle_same_header(_same_match(event_code="TOR"))
        calls = p.event_bus.publish.call_args_list
        event_types = [c[0][0] for c in calls]
        assert events.ALERT_TRIGGERED not in event_types


class TestCheckExpired:
    def test_expired_alert_cleared(self):
        p = _make_plugin()
        p._handle_same_header(_same_match(event_code="TOR"))
        p._active_alert["purge_ts"] = time.time() - 10
        p._check_expired()
        assert p._active_alert is None

    def test_unexpired_alert_kept(self):
        p = _make_plugin()
        p._handle_same_header(_same_match(event_code="TOR"))
        p._active_alert["purge_ts"] = time.time() + 3600
        p._check_expired()
        assert p._active_alert is not None

    def test_seen_headers_pruned(self):
        p = _make_plugin()
        p._seen_headers = {"old": time.time() - 600, "new": time.time()}
        p._check_expired()
        assert "old" not in p._seen_headers
        assert "new" in p._seen_headers


class TestGetStatus:
    def test_status_fields(self):
        p = _make_plugin()
        s = p.get_status()
        assert s["active"] is True
        assert s["status"] == "idle"
        assert s["freq_mhz"] == 162.55
        assert s["headers_decoded"] == 0
        assert s["active_alert"] is False
