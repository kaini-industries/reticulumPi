"""Tests for the signal_operations plugin.

Covers config validation, contact ID hashing, signal detection /
classification / tracking, contact management, event handlers, query
methods, correlation, and persistence — without spawning threads.
"""
from __future__ import annotations

import os
import queue
import sqlite3
import tempfile
import threading
import time
from collections import deque
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi.builtin_plugins.signal_operations import (
    Contact, DetectedSignal, SignalOperationsPlugin, SignalTrack, _contact_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app() -> MagicMock:
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.event_bus = MagicMock()
    app.plugins = {}
    app.get_plugin = MagicMock(return_value=None)
    return app


def _make_plugin(config: dict | None = None) -> SignalOperationsPlugin:
    """Construct a SignalOperationsPlugin without calling start()."""
    plugin = SignalOperationsPlugin(_make_app(), config or {})
    plugin._active = True
    plugin._baseline_db = {}
    plugin._active_signals = {}
    plugin._contacts = {}
    plugin._contacts_lock = threading.Lock()
    plugin._correlation_events = deque(maxlen=200)
    plugin._detector_queue = queue.Queue(maxsize=100)
    plugin._stats = {
        "signals_detected_total": 0, "signals_classified": 0,
        "signals_unknown": 0, "contacts_total": 0, "contacts_active": 0,
        "correlations_total": 0, "observations_persisted": 0,
    }
    plugin._snapshot_cache = {}
    plugin._snapshot_dirty = True
    plugin._signal_db = []
    plugin._db_path = ":memory:"
    plugin._receiver_lat = None
    plugin._receiver_lon = None
    return plugin


def _make_plugin_with_db(config: dict | None = None):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    plugin = _make_plugin(config)
    plugin._db_path = path
    plugin._db_init()
    return plugin, path


# ---------------------------------------------------------------------------
# _contact_id
# ---------------------------------------------------------------------------

class TestContactId:
    def test_deterministic_and_length(self):
        a = _contact_id("aircraft", "ABC123")
        b = _contact_id("aircraft", "ABC123")
        assert a == b
        assert len(a) == 16
        assert all(c in "0123456789abcdef" for c in a)

    def test_different_types_differ(self):
        assert _contact_id("aircraft", "X") != _contact_id("vessel", "X")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

class TestDataclasses:
    def test_detected_signal(self):
        sig = DetectedSignal(433920000, 25000, -30.5, 1000.0)
        assert sig.center_freq_hz == 433920000
        assert sig.bandwidth_hz == 25000

    def test_contact_defaults(self):
        c = Contact(id="abc", contact_type="aircraft", identifier="ICAO1")
        assert c.observation_count == 0
        assert c.lat is None
        assert isinstance(c.sources, set)

    def test_signal_track_defaults(self):
        t = SignalTrack(100000000, 10000, -40.0, 1.0, 1.0)
        assert t.intermittent is False
        assert t.classification is None


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------

class TestValidateConfig:
    def test_defaults(self):
        p = _make_plugin()
        assert p._detection_threshold_db == 10.0
        assert p._min_bandwidth_hz == 5000
        assert p._baseline_alpha == 0.02
        assert p._max_contacts == 1000
        assert p._stale_contact_timeout == 3600

    def test_custom_config(self):
        p = _make_plugin({
            "detection_threshold_db": 6.0, "min_bandwidth_hz": 2000,
            "baseline_alpha": 0.05, "max_contacts": 500,
            "stale_contact_timeout": 900, "correlation_interval_s": 60,
            "receiver_lat": 40.7128, "receiver_lon": -74.006,
        })
        assert p._detection_threshold_db == 6.0
        assert p._min_bandwidth_hz == 2000
        assert p._max_contacts == 500
        assert p._correlation_interval == 60
        assert p._receiver_lat == pytest.approx(40.7128)
        assert p._receiver_lon == pytest.approx(-74.006)

    def test_receiver_lat_alone(self):
        p = _make_plugin({"receiver_lat": 51.5})
        assert p._receiver_lat == pytest.approx(51.5)
        assert p._receiver_lon is None


# ---------------------------------------------------------------------------
# _update_baseline
# ---------------------------------------------------------------------------

class TestUpdateBaseline:
    def test_first_update_sets_baseline(self):
        p = _make_plugin()
        p._update_baseline([100000, 200000], [-80.0, -90.0])
        assert p._baseline_db[100000] == -80.0
        assert p._baseline_db[200000] == -90.0

    def test_ema_smoothing(self):
        p = _make_plugin({"baseline_alpha": 0.5})
        p._update_baseline([100000], [-80.0])
        p._update_baseline([100000], [-60.0])
        assert p._baseline_db[100000] == pytest.approx(-70.0)

    def test_none_power_skipped(self):
        p = _make_plugin()
        p._update_baseline([100000, 200000], [None, -90.0])
        assert 100000 not in p._baseline_db
        assert p._baseline_db[200000] == -90.0

    def test_mismatched_lengths(self):
        p = _make_plugin()
        p._update_baseline([100000, 200000, 300000], [-80.0])
        assert p._baseline_db[100000] == -80.0
        assert 200000 not in p._baseline_db


# ---------------------------------------------------------------------------
# _find_signals
# ---------------------------------------------------------------------------

class TestFindSignals:
    def test_empty_baseline_returns_nothing(self):
        p = _make_plugin()
        assert p._find_signals([100000], [-50.0], 1.0) == []

    def test_detects_signal_above_threshold(self):
        p = _make_plugin({"detection_threshold_db": 10, "min_bandwidth_hz": 1000})
        bins = list(range(100000, 120000, 1000))
        p._update_baseline(bins, [-80.0] * len(bins))
        sweep = [-80.0] * len(bins)
        for i in range(5, 10):
            sweep[i] = -65.0
        detected = p._find_signals(bins, sweep, 100.0)
        assert len(detected) == 1
        assert detected[0].peak_power_db == -65.0
        assert detected[0].bandwidth_hz >= 1000

    def test_narrow_signal_rejected(self):
        p = _make_plugin({"detection_threshold_db": 10, "min_bandwidth_hz": 50000})
        bins = list(range(100000, 200000, 10000))
        p._update_baseline(bins, [-80.0] * len(bins))
        powers = [-80.0] * len(bins)
        powers[3] = powers[4] = -60.0
        assert p._find_signals(bins, powers, 1.0) == []

    def test_max_20_signals(self):
        p = _make_plugin({"detection_threshold_db": 5, "min_bandwidth_hz": 0})
        bins = list(range(0, 500000, 1000))
        p._update_baseline(bins, [-80.0] * len(bins))
        powers = [-60.0 if i % 3 < 2 else -80.0 for i in range(len(bins))]
        assert len(p._find_signals(bins, powers, 1.0)) <= 20


# ---------------------------------------------------------------------------
# _classify_signal
# ---------------------------------------------------------------------------

class TestClassifySignal:
    def test_empty_db_returns_none(self):
        p = _make_plugin()
        name, conf = p._classify_signal(DetectedSignal(433920000, 25000, -40.0, 1.0))
        assert name is None and conf == 0.0

    def test_match_within_freq_range(self):
        p = _make_plugin()
        p._signal_db = [{"name": "ISM 433", "freq_min_mhz": 433.0,
                         "freq_max_mhz": 434.0, "bandwidth_khz": 25}]
        name, conf = p._classify_signal(DetectedSignal(433500000, 25000, -40.0, 1.0))
        assert name == "ISM 433"
        assert conf > 0.3

    def test_no_match_outside_range(self):
        p = _make_plugin()
        p._signal_db = [{"name": "ISM 433", "freq_min_mhz": 433.0,
                         "freq_max_mhz": 434.0, "bandwidth_khz": 25}]
        name, _ = p._classify_signal(DetectedSignal(900000000, 25000, -40.0, 1.0))
        assert name is None

    def test_bandwidth_affects_score(self):
        p = _make_plugin()
        p._signal_db = [{"name": "NFM", "freq_min_mhz": 150.0,
                         "freq_max_mhz": 160.0, "bandwidth_khz": 12.5}]
        _, conf_good = p._classify_signal(DetectedSignal(155000000, 12500, -40.0, 1.0))
        _, conf_bad = p._classify_signal(DetectedSignal(155000000, 200000, -40.0, 1.0))
        assert conf_good > conf_bad

    def test_best_match_wins(self):
        p = _make_plugin()
        p._signal_db = [
            {"name": "Wide", "freq_min_mhz": 100.0, "freq_max_mhz": 500.0, "bandwidth_khz": 0},
            {"name": "Narrow", "freq_min_mhz": 149.0, "freq_max_mhz": 151.0, "bandwidth_khz": 12},
        ]
        name, _ = p._classify_signal(DetectedSignal(150000000, 12000, -40.0, 1.0))
        assert name == "Narrow"


# ---------------------------------------------------------------------------
# _track_signal
# ---------------------------------------------------------------------------

class TestTrackSignal:
    def test_new_signal_tracked(self):
        p = _make_plugin()
        p._track_signal(DetectedSignal(433920000, 25000, -40.0, 100.0), "ISM", 0.8)
        qfreq = 433920000 // 10000 * 10000
        assert p._active_signals[qfreq].classification == "ISM"
        assert p._stats["signals_detected_total"] == 1
        assert p._stats["signals_classified"] == 1

    def test_unknown_signal_counted(self):
        p = _make_plugin()
        p._track_signal(DetectedSignal(100000000, 10000, -50.0, 1.0), None, 0.0)
        assert p._stats["signals_unknown"] == 1

    def test_update_existing_track(self):
        p = _make_plugin()
        p._track_signal(DetectedSignal(433920000, 25000, -40.0, 100.0), None, 0.0)
        p._track_signal(DetectedSignal(433920000, 25000, -35.0, 105.0), "ISM", 0.9)
        qfreq = 433920000 // 10000 * 10000
        track = p._active_signals[qfreq]
        assert track.observation_count == 2
        assert track.peak_power_db == -35.0
        assert track.classification == "ISM"
        assert track.duration_s == pytest.approx(5.0)

    def test_intermittent_detection(self):
        p = _make_plugin()
        p._track_signal(DetectedSignal(433920000, 25000, -40.0, 100.0), None, 0.0)
        p._track_signal(DetectedSignal(433920000, 25000, -40.0, 200.0), None, 0.0)
        qfreq = 433920000 // 10000 * 10000
        assert p._active_signals[qfreq].intermittent is True


# ---------------------------------------------------------------------------
# _upsert_contact / _update_contact_distance
# ---------------------------------------------------------------------------

class TestUpsertContact:
    def test_creates_new_contact(self):
        p = _make_plugin()
        c = p._upsert_contact("aircraft", "ABC123", "adsb_radar", display_name="UAL123")
        assert c.contact_type == "aircraft"
        assert c.display_name == "UAL123"
        assert c.observation_count == 1
        assert "adsb_radar" in c.sources
        assert p._stats["contacts_total"] == 1

    def test_updates_existing_contact(self):
        p = _make_plugin()
        c1 = p._upsert_contact("aircraft", "ABC123", "adsb_radar")
        c2 = p._upsert_contact("aircraft", "ABC123", "acars_decoder", lat=40.7, lon=-74.0)
        assert c2.observation_count == 2
        assert "acars_decoder" in c2.sources
        assert c1 is c2

    def test_max_contacts_limit(self):
        p = _make_plugin({"max_contacts": 2})
        p._upsert_contact("aircraft", "A", "src")
        p._upsert_contact("vessel", "B", "src")
        c3 = p._upsert_contact("balloon", "C", "src")
        assert c3.observation_count == 0
        assert len(p._contacts) == 2

    @patch("reticulumpi.geo.haversine_nm", return_value=42.5)
    @patch("reticulumpi.geo.bearing_deg", return_value=90.0)
    def test_distance_calculated(self, _mock_b, _mock_h):
        p = _make_plugin()
        p._receiver_lat, p._receiver_lon = 40.0, -74.0
        c = Contact(id="t", contact_type="aircraft", identifier="X", lat=41.0, lon=-73.0)
        p._update_contact_distance(c)
        assert c.distance_nm == 42.5 and c.bearing_deg == 90.0

    def test_distance_skipped_without_receiver_pos(self):
        p = _make_plugin()
        c = Contact(id="t", contact_type="aircraft", identifier="X", lat=41.0, lon=-73.0)
        p._update_contact_distance(c)
        assert c.distance_nm is None


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

class TestEventHandlers:
    def test_on_adsb(self):
        p = _make_plugin()
        p._on_adsb("", {"icao": "ABCDEF", "callsign": "UAL123",
                        "latitude": 40.0, "longitude": -74.0, "altitude": 35000})
        cid = _contact_id("aircraft", "ABCDEF")
        assert p._contacts[cid].display_name == "UAL123"
        assert p._contacts[cid].metadata["altitude"] == 35000

    def test_on_adsb_empty_icao_skipped(self):
        p = _make_plugin()
        p._on_adsb("", {})
        assert len(p._contacts) == 0

    def test_on_adsb_lost(self):
        p = _make_plugin()
        p._on_adsb("", {"icao": "ABC", "callsign": "X"})
        p._on_adsb_lost("", {"icao": "ABC"})
        assert p._contacts[_contact_id("aircraft", "ABC")].metadata["status"] == "lost"

    def test_on_adsb_emergency(self):
        p = _make_plugin()
        p._on_adsb_emergency("", {"icao": "E1", "squawk": "7700"})
        assert len(p._correlation_events) == 1
        assert p._correlation_events[0]["event_type"] == "adsb_emergency"

    def test_on_acars(self):
        p = _make_plugin()
        p._on_acars("", {"tail": "N12345", "flight": "UAL456", "label": "H1"})
        cid = _contact_id("aircraft", "N12345")
        assert p._contacts[cid].metadata["flight"] == "UAL456"

    def test_on_acars_empty_tail_skipped(self):
        p = _make_plugin()
        p._on_acars("", {"flight": "ABC"})
        assert len(p._contacts) == 0

    def test_on_ais(self):
        p = _make_plugin()
        p._on_ais("", {"mmsi": 123456789, "name": "CARGO", "lat": 51.0, "lon": -1.0})
        assert p._contacts[_contact_id("vessel", "123456789")].display_name == "CARGO"

    def test_on_ais_lost(self):
        p = _make_plugin()
        p._on_ais("", {"mmsi": 111, "name": "V"})
        p._on_ais_lost("", {"mmsi": 111})
        assert p._contacts[_contact_id("vessel", "111")].metadata["status"] == "lost"

    def test_on_radiosonde(self):
        p = _make_plugin()
        p._on_radiosonde("", {"id": "S1234", "type": "RS41", "alt_m": 15000})
        assert p._contacts[_contact_id("balloon", "S1234")].display_name == "Sonde S1234"

    def test_on_radiosonde_lost(self):
        p = _make_plugin()
        p._on_radiosonde("", {"id": "S1"})
        p._on_radiosonde_lost("", {"id": "S1"})
        assert p._contacts[_contact_id("balloon", "S1")].metadata["status"] == "lost"

    def test_on_weather_alert_severe(self):
        p = _make_plugin()
        p._on_weather_alert("", {"event_code": "TOR", "event_desc": "Tornado Warning",
                                 "severity": "extreme", "fips_codes": ["012345"]})
        assert len(p._correlation_events) == 1
        assert "Tornado Warning" in p._correlation_events[0]["description"]

    def test_on_weather_alert_minor_ignored(self):
        p = _make_plugin()
        p._on_weather_alert("", {"event_code": "HWO", "severity": "minor",
                                 "fips_codes": ["012345"]})
        assert len(p._correlation_events) == 0

    def test_on_lora_peer(self):
        p = _make_plugin()
        p._on_lora_peer("", {"destination_hash": "deadbeef1234", "app_name": "nomadnet"})
        assert _contact_id("mesh_peer", "deadbeef1234") in p._contacts

    def test_on_ism_device_and_lost(self):
        p = _make_plugin()
        p._on_ism_device("", {"key": "acurite-42", "model": "Acurite Tower"})
        cid = _contact_id("ism_device", "acurite-42")
        assert p._contacts[cid].display_name == "Acurite Tower"
        p._on_ism_lost("", {"key": "acurite-42"})
        assert p._contacts[cid].metadata["status"] == "lost"

    def test_on_gps_fix(self):
        p = _make_plugin()
        p._on_gps_fix("", {"lat": 52.5, "lon": 13.4})
        assert p._receiver_lat == pytest.approx(52.5)
        assert p._receiver_lon == pytest.approx(13.4)

    def test_on_gps_fix_partial_ignored(self):
        p = _make_plugin()
        p._on_gps_fix("", {"lat": 52.5})
        assert p._receiver_lat is None

    def test_on_spectrum_sweep(self):
        p = _make_plugin()
        p._on_spectrum_sweep("", {"bins_hz": [100000], "powers_db": [-80.0], "timestamp": 99.0})
        item = p._detector_queue.get_nowait()
        assert item["bins_hz"] == [100000] and item["timestamp"] == 99.0

    def test_on_spectrum_sweep_inactive(self):
        p = _make_plugin()
        p._active = False
        p._on_spectrum_sweep("", {"bins_hz": [100000], "powers_db": [-80.0]})
        assert p._detector_queue.empty()


# ---------------------------------------------------------------------------
# Query methods
# ---------------------------------------------------------------------------

class TestQueryMethods:
    def test_get_overview(self):
        p = _make_plugin()
        p._stats["signals_detected_total"] = 5
        ov = p.get_overview()
        assert ov["stats"]["signals_detected_total"] == 5
        assert ov["active_signals"] == 0

    def test_get_contacts_all_and_filtered(self):
        p = _make_plugin()
        p._upsert_contact("aircraft", "A1", "src")
        p._upsert_contact("vessel", "V1", "src")
        assert p.get_contacts()["total"] == 2
        assert p.get_contacts(contact_type="vessel")["total"] == 1

    def test_get_contact_detail(self):
        p = _make_plugin()
        p._upsert_contact("aircraft", "A1", "src", display_name="TEST")
        cid = _contact_id("aircraft", "A1")
        assert p.get_contact_detail(cid)["display_name"] == "TEST"
        assert p.get_contact_detail("nonexistent") is None

    def test_get_baseline(self):
        p = _make_plugin()
        p._baseline_db = {100000: -80.0, 200000: -85.5}
        result = p.get_baseline()
        assert result["bin_count"] == 2
        assert result["bins"][0]["freq_hz"] == 100000

    def test_get_correlations(self):
        p = _make_plugin()
        for i in range(5):
            p._add_correlation(f"evt{i}", [], f"d{i}", ["s"])
        assert p.get_correlations()["total"] == 5
        assert len(p.get_correlations(limit=2)["events"]) == 2


# ---------------------------------------------------------------------------
# manual_classify
# ---------------------------------------------------------------------------

class TestManualClassify:
    def test_adds_to_signal_db(self):
        p = _make_plugin()
        result = p.manual_classify(433920000, "My Signal")
        assert result["status"] == "classified"
        entry = p._signal_db[0]
        assert entry["name"] == "My Signal"
        assert entry["freq_min_mhz"] < 433.92 < entry["freq_max_mhz"]

    def test_extra_fields_applied(self):
        p = _make_plugin()
        p.manual_classify(100000000, "Custom", extra={
            "bandwidth_khz": 50, "modulation": "NFM",
            "description": "Custom desc", "ignored_key": "nope",
        })
        entry = p._signal_db[0]
        assert entry["bandwidth_khz"] == 50
        assert "ignored_key" not in entry


# ---------------------------------------------------------------------------
# reset_baseline / broadcast_snapshot
# ---------------------------------------------------------------------------

class TestResetAndSnapshot:
    def test_reset_clears_state(self):
        p = _make_plugin()
        p._baseline_db = {100000: -80.0}
        p._active_signals = {100000: SignalTrack(100000, 10000, -40.0, 1.0, 1.0)}
        p.reset_baseline()
        assert p._baseline_db == {} and p._active_signals == {}

    def test_broadcast_returns_sigops_key(self):
        p = _make_plugin()
        result = p.broadcast_snapshot()
        assert "sigops" in result

    def test_snapshot_includes_contacts(self):
        p = _make_plugin()
        p._upsert_contact("aircraft", "A1", "src")
        p._snapshot_dirty = True
        snap = p.broadcast_snapshot()["sigops"]
        assert len(snap["contacts"]) == 1


# ---------------------------------------------------------------------------
# _evict_stale_contacts
# ---------------------------------------------------------------------------

class TestEvictStaleContacts:
    def test_removes_stale_keeps_fresh(self):
        p = _make_plugin({"stale_contact_timeout": 60})
        stale = p._upsert_contact("aircraft", "OLD", "src")
        stale.last_seen = time.time() - 120
        p._upsert_contact("aircraft", "NEW", "src")
        p._evict_stale_contacts()
        assert len(p._contacts) == 1

    def test_publishes_lost_event(self):
        from reticulumpi import events as ev
        p = _make_plugin({"stale_contact_timeout": 60})
        c = p._upsert_contact("aircraft", "OLD", "src")
        c.last_seen = time.time() - 120
        p._evict_stale_contacts()
        lost_calls = [c for c in p.event_bus.publish.call_args_list
                      if c[0][0] == ev.SIGOPS_CONTACT_LOST]
        assert len(lost_calls) == 1


# ---------------------------------------------------------------------------
# _add_correlation
# ---------------------------------------------------------------------------

class TestAddCorrelation:
    def test_appends_and_stats(self):
        p = _make_plugin()
        p._add_correlation("test", ["c1", "c2"], "description", ["src"])
        assert len(p._correlation_events) == 1
        assert p._correlation_events[0]["contact_ids"] == ["c1", "c2"]
        assert p._stats["correlations_total"] == 1

    def test_sets_snapshot_dirty(self):
        p = _make_plugin()
        p._snapshot_dirty = False
        p._add_correlation("evt", [], "d", ["s"])
        assert p._snapshot_dirty is True


# ---------------------------------------------------------------------------
# Persistence (SQLite)
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_db_init_creates_tables(self):
        p, path = _make_plugin_with_db()
        with sqlite3.connect(path) as conn:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"signal_observations", "contacts", "correlation_events"} <= names
        os.unlink(path)

    def test_save_and_query_observations(self):
        p, path = _make_plugin_with_db()
        p._db_save_observation(DetectedSignal(433920000, 25000, -40.0, 1000.0), "ISM", 0.85)
        dets = p.get_detections()["detections"]
        assert len(dets) == 1
        assert dets[0]["freq_hz"] == 433920000 and dets[0]["classification"] == "ISM"
        os.unlink(path)

    def test_get_detections_with_filters(self):
        p, path = _make_plugin_with_db()
        p._db_save_observation(DetectedSignal(100000000, 10000, -50.0, 1000.0), None, 0.0)
        p._db_save_observation(DetectedSignal(200000000, 20000, -45.0, 2000.0), "FM", 0.9)
        result = p.get_detections(freq_min=150000000)
        assert len(result["detections"]) == 1
        assert result["detections"][0]["freq_hz"] == 200000000
        os.unlink(path)

    def test_get_aggregate_stats(self):
        p, path = _make_plugin_with_db()
        sig = DetectedSignal(100000000, 10000, -50.0, 1.0)
        p._db_save_observation(sig, "FM", 0.9)
        p._db_save_observation(sig, "FM", 0.9)
        stats = p.get_aggregate_stats()
        assert stats["total_observations"] == 2
        assert stats["by_classification"]["FM"] == 2
        os.unlink(path)

    def test_db_flush_persists_contacts(self):
        p, path = _make_plugin_with_db()
        p._upsert_contact("aircraft", "FLUSH1", "src", display_name="Test")
        p._db_flush()
        with sqlite3.connect(path) as conn:
            rows = conn.execute("SELECT identifier FROM contacts").fetchall()
        assert rows[0][0] == "FLUSH1"
        os.unlink(path)

    def test_save_correlation_persisted(self):
        p, path = _make_plugin_with_db()
        p._add_correlation("test_event", ["c1"], "testing", ["src"])
        with sqlite3.connect(path) as conn:
            rows = conn.execute("SELECT event_type, description FROM correlation_events").fetchall()
        assert rows[0] == ("test_event", "testing")
        os.unlink(path)
