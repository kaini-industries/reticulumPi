"""Tests for the NTP server plugin."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi import events
from reticulumpi.builtin_plugins.ntp_server import NtpServerPlugin


def _make_app() -> MagicMock:
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.event_bus = MagicMock()
    return app


def _make_plugin(config: dict | None = None) -> NtpServerPlugin:
    plugin = NtpServerPlugin(_make_app(), config or {})
    plugin._active = True
    plugin._lock = threading.Lock()
    plugin._start_time = time.time()
    plugin._check_interval = plugin.config.get("check_interval", 30)
    plugin._sync_state = "unknown"
    plugin._prev_sync_state = "unknown"
    plugin._last_synced_time = 0.0
    plugin._sync_lost_alerted = False
    plugin._tracking = {}
    plugin._sources = []
    plugin._last_check = 0.0
    plugin._check_errors = 0
    plugin._gps_refclock_active = False
    plugin._gps_refclock_configured = False
    plugin._manage_chrony = plugin.config.get("manage_chrony_config", True)
    plugin._conf_dir = plugin.config.get("chrony_conf_dir", "/etc/chrony/conf.d")
    plugin._conf_path = f"{plugin._conf_dir}/reticulumpi-gps.conf"
    plugin._use_sudo = plugin.config.get("sudo_chronyc", True)
    return plugin


SAMPLE_TRACKING = (
    "GPS,47505030,1,1717350000.000,0.000000123,0.000000100,"
    "0.000000200,0.500,-0.010,0.020,0.001,0.002,64.0,Normal"
)

SAMPLE_SOURCES = (
    "#,*,GPS,1,6,377,30,0.000000100,0.000000050\n"
    "^,+,pool.ntp.org,2,10,377,40,0.000001000,0.000000500"
)


class TestValidateConfig:
    def test_valid_defaults(self):
        NtpServerPlugin(_make_app(), {})

    def test_bad_check_interval(self):
        with pytest.raises(ValueError, match="check_interval"):
            NtpServerPlugin(_make_app(), {"check_interval": 2})

    def test_bad_chrony_conf_dir(self):
        with pytest.raises(ValueError, match="chrony_conf_dir"):
            NtpServerPlugin(_make_app(), {"chrony_conf_dir": ""})

    def test_bad_sync_loss_threshold(self):
        with pytest.raises(ValueError, match="sync_loss_threshold"):
            NtpServerPlugin(_make_app(), {"sync_loss_threshold": -1})


class TestParseTracking:
    def test_valid_output(self):
        p = _make_plugin()
        result = p._parse_tracking(SAMPLE_TRACKING)
        assert result["ref_id_name"] == "GPS"
        assert result["stratum"] == 1
        assert result["leap_status"] == "Normal"
        assert isinstance(result["system_time_offset"], float)
        assert "system_time_offset_ms" in result

    def test_empty_output(self):
        p = _make_plugin()
        assert p._parse_tracking("") == {}

    def test_stratum_parsed_as_int(self):
        p = _make_plugin()
        result = p._parse_tracking(SAMPLE_TRACKING)
        assert isinstance(result["stratum"], int)


class TestParseSources:
    def test_valid_output(self):
        p = _make_plugin()
        result = p._parse_sources(SAMPLE_SOURCES)
        assert len(result) == 2
        assert result[0]["name"] == "GPS"
        assert result[0]["state"] == "*"
        assert result[0]["state_label"] == "synced"
        assert result[1]["name"] == "pool.ntp.org"
        assert result[1]["state_label"] == "candidate"

    def test_empty_output(self):
        p = _make_plugin()
        assert p._parse_sources("") == []

    def test_offset_ms_computed(self):
        p = _make_plugin()
        result = p._parse_sources(SAMPLE_SOURCES)
        assert "offset_ms" in result[0]


class TestDetermineSyncState:
    def test_unsynced_no_stratum(self):
        p = _make_plugin()
        assert p._determine_sync_state({}, []) == "unsynced"

    def test_unsynced_not_synchronised(self):
        p = _make_plugin()
        tracking = {"stratum": 3, "leap_status": "Not synchronised"}
        assert p._determine_sync_state(tracking, []) == "unsynced"

    def test_unsynced_stratum_zero(self):
        p = _make_plugin()
        tracking = {"stratum": 0, "ref_id_name": "", "leap_status": "Normal"}
        assert p._determine_sync_state(tracking, []) == "unsynced"

    def test_gps_disciplined(self):
        p = _make_plugin()
        tracking = {"stratum": 1, "ref_id_name": "GPS", "leap_status": "Normal"}
        assert p._determine_sync_state(tracking, []) == "gps_disciplined"

    def test_gps_disciplined_pps(self):
        p = _make_plugin()
        tracking = {"stratum": 1, "ref_id_name": "PPS", "leap_status": "Normal"}
        assert p._determine_sync_state(tracking, []) == "gps_disciplined"

    def test_synced_via_star_source(self):
        p = _make_plugin()
        tracking = {"stratum": 2, "ref_id_name": "pool.ntp.org", "leap_status": "Normal"}
        sources = [{"state": "*", "name": "pool.ntp.org"}]
        assert p._determine_sync_state(tracking, sources) == "synced"

    def test_synced_via_stratum(self):
        p = _make_plugin()
        tracking = {"stratum": 3, "ref_id_name": "some-ntp", "leap_status": "Normal"}
        assert p._determine_sync_state(tracking, []) == "synced"


class TestStateTransitions:
    def test_sync_acquired(self):
        p = _make_plugin()
        p._prev_sync_state = "unsynced"
        p._sync_state = "synced"
        p._tracking = {"stratum": 2, "ref_id_name": "pool.ntp.org", "system_time_offset_ms": 0.5}
        p._handle_state_transitions()
        p.event_bus.publish.assert_called_with(
            events.NTP_SYNC_ACQUIRED,
            {
                "stratum": 2,
                "ref_id": "pool.ntp.org",
                "offset_ms": 0.5,
            },
        )

    def test_no_event_same_state(self):
        p = _make_plugin()
        p._prev_sync_state = "synced"
        p._sync_state = "synced"
        p._handle_state_transitions()
        p.event_bus.publish.assert_not_called()

    def test_sync_lost_after_threshold(self):
        p = _make_plugin({"sync_loss_threshold": 10, "alert_on_sync_loss": True})
        p._prev_sync_state = "unsynced"
        p._sync_state = "unsynced"
        p._last_synced_time = time.time() - 20
        p._handle_state_transitions()
        calls = p.event_bus.publish.call_args_list
        event_types = [c[0][0] for c in calls]
        assert events.NTP_SYNC_LOST in event_types
        assert p._sync_lost_alerted is True

    def test_sync_lost_not_alerted_twice(self):
        p = _make_plugin({"sync_loss_threshold": 10})
        p._prev_sync_state = "unsynced"
        p._sync_state = "unsynced"
        p._last_synced_time = time.time() - 20
        p._sync_lost_alerted = True
        p._handle_state_transitions()
        p.event_bus.publish.assert_not_called()


class TestGpsRefclock:
    def test_on_gps_fix_configures(self):
        p = _make_plugin()
        with patch.object(p, "_configure_gps_refclock") as mock_cfg:
            p._on_gps_fix(events.GPS_FIX_RECEIVED, {})
        mock_cfg.assert_called_once()

    def test_on_gps_fix_skips_if_configured(self):
        p = _make_plugin()
        p._gps_refclock_configured = True
        with patch.object(p, "_configure_gps_refclock") as mock_cfg:
            p._on_gps_fix(events.GPS_FIX_RECEIVED, {})
        mock_cfg.assert_not_called()

    def test_on_gps_lost(self):
        p = _make_plugin()
        p._gps_refclock_active = True
        p._on_gps_lost(events.GPS_FIX_LOST, {})
        assert p._gps_refclock_active is False

    @patch("reticulumpi.builtin_plugins.ntp_server.subprocess.run")
    def test_configure_writes_and_restarts(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        p = _make_plugin({"gps_refclock": {"enabled": True}})
        p._conf_path = "/tmp/nonexistent-reticulumpi-gps.conf"
        p._configure_gps_refclock()
        assert mock_run.call_count == 2
        assert p._gps_refclock_configured is True
        assert p._gps_refclock_active is True
        p.event_bus.publish.assert_called()


class TestGetStatusAndSnapshot:
    def test_get_status(self):
        p = _make_plugin()
        p._last_check = time.time()
        s = p.get_status()
        assert s["active"] is True
        assert s["sync_state"] == "unknown"
        assert s["gps_refclock_active"] is False

    def test_get_snapshot_includes_tracking(self):
        p = _make_plugin()
        p._tracking = {"stratum": 1, "ref_id_name": "GPS"}
        p._sources = [{"name": "GPS", "state": "*"}]
        snap = p.get_snapshot()
        assert snap["tracking"]["stratum"] == 1
        assert snap["sources_count"] == 1
