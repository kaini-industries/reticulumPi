"""Tests for the AIS receiver plugin."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any
from unittest.mock import MagicMock

from reticulumpi import events
from reticulumpi.builtin_plugins.ais_receiver import AISReceiver, _ship_type_desc


def _make_app() -> MagicMock:
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.event_bus = MagicMock()
    return app


def _make_plugin(config: dict | None = None) -> AISReceiver:
    plugin = AISReceiver(_make_app(), config or {})
    plugin._active = True
    plugin._vessels = {}
    plugin._vessels_lock = threading.Lock()
    plugin._vessel_type_counts = {}
    plugin._stats = {
        "messages_total": 0,
        "messages_by_type": {},
        "vessels_seen_total": 0,
    }
    plugin._status = "idle"
    plugin._last_error = None
    plugin._restart_count = 0
    plugin._snapshot_dirty = True
    plugin._maintenance_alive = False
    plugin._process = None
    plugin._pid = None
    return plugin


def _position_msg(
    mmsi: str = "123456789",
    msg_type: int = 1,
    lat: float = 40.7128,
    lon: float = -74.006,
    speed: float = 12.5,
    course: float = 180.0,
    heading: int = 175,
    status: int = 0,
) -> dict[str, Any]:
    return {
        "mmsi": mmsi,
        "type": msg_type,
        "lat": lat,
        "lon": lon,
        "speed": speed,
        "course": course,
        "heading": heading,
        "status": status,
    }


def _static_msg(
    mmsi: str = "123456789",
    shipname: str = "VESSEL ONE",
    destination: str = "NEW YORK",
    shiptype: int = 70,
) -> dict[str, Any]:
    return {
        "mmsi": mmsi,
        "type": 5,
        "shipname": shipname,
        "destination": destination,
        "shiptype": shiptype,
    }


class TestValidateConfig:
    def test_defaults(self):
        p = AISReceiver(_make_app(), {})
        assert p._decoder_bin == "AIS-catcher"
        assert p._gain is None
        assert p._ppm == 0
        assert p._stale_timeout == 600.0
        assert p._max_vessels == 200
        assert p._max_restarts == 5

    def test_custom_config(self):
        p = AISReceiver(
            _make_app(),
            {
                "decoder_bin": "rtl_ais",
                "gain": 40,
                "stale_timeout": 300,
                "max_vessels": 50,
            },
        )
        assert p._decoder_bin == "rtl_ais"
        assert p._gain == 40
        assert p._stale_timeout == 300.0
        assert p._max_vessels == 50


class TestShipTypeDesc:
    def test_known_type(self):
        assert _ship_type_desc(70) == "Cargo"
        assert _ship_type_desc(80) == "Tanker"

    def test_decade_fallback(self):
        assert _ship_type_desc(71) == "Cargo"
        assert _ship_type_desc(82) == "Tanker"

    def test_unknown_type(self):
        assert _ship_type_desc(99) == "Other"
        assert _ship_type_desc(10) == "Type 10"


class TestHandleMessage:
    def test_new_vessel_position(self):
        p = _make_plugin()
        p._handle_message(_position_msg())
        assert len(p._vessels) == 1
        assert "123456789" in p._vessels
        v = p._vessels["123456789"]
        assert v["lat"] == 40.7128
        assert v["lon"] == -74.006
        assert v["speed_kts"] == 12.5
        assert v["course"] == 180.0
        assert v["nav_status"] == "Under way using engine"
        assert v["message_count"] == 1

    def test_update_existing_vessel(self):
        p = _make_plugin()
        p._handle_message(_position_msg(lat=40.0))
        p._handle_message(_position_msg(lat=41.0))
        assert len(p._vessels) == 1
        assert p._vessels["123456789"]["lat"] == 41.0
        assert p._vessels["123456789"]["message_count"] == 2
        assert p._stats["vessels_seen_total"] == 1

    def test_type5_static_data(self):
        p = _make_plugin()
        p._handle_message(_position_msg())
        p._handle_message(_static_msg())
        v = p._vessels["123456789"]
        assert v["name"] == "VESSEL ONE"
        assert v["destination"] == "NEW YORK"
        assert v["ship_type"] == 70
        assert v["ship_type_desc"] == "Cargo"

    def test_empty_mmsi_ignored(self):
        p = _make_plugin()
        p._handle_message({"type": 1, "lat": 1.0})
        assert len(p._vessels) == 0

    def test_heading_511_ignored(self):
        p = _make_plugin()
        p._handle_message(_position_msg(heading=511))
        v = p._vessels["123456789"]
        assert v["heading"] is None

    def test_max_vessels_evicts_oldest(self):
        p = _make_plugin({"max_vessels": 2})
        p._handle_message(_position_msg(mmsi="001"))
        p._handle_message(_position_msg(mmsi="002"))
        p._handle_message(_position_msg(mmsi="003"))
        assert len(p._vessels) == 2
        assert "003" in p._vessels

    def test_message_type_counting(self):
        p = _make_plugin()
        p._handle_message(_position_msg(msg_type=1))
        p._handle_message(_position_msg(msg_type=1))
        p._handle_message(_position_msg(msg_type=3))
        assert p._stats["messages_by_type"]["1"] == 2
        assert p._stats["messages_by_type"]["3"] == 1

    def test_track_history(self):
        p = _make_plugin()
        p._handle_message(_position_msg(lat=40.0, lon=-74.0))
        v = p._vessels["123456789"]
        assert len(v["track_history"]) == 1
        assert v["track_history"][0]["lat"] == 40.0


class TestEventEmission:
    def test_new_vessel_emits_detected(self):
        p = _make_plugin()
        p._handle_message(_position_msg())
        calls = p.event_bus.publish.call_args_list
        event_types = [c[0][0] for c in calls]
        assert events.AIS_VESSEL_DETECTED in event_types

    def test_existing_vessel_no_detected_event(self):
        p = _make_plugin()
        p._handle_message(_position_msg())
        p.event_bus.publish.reset_mock()
        p._handle_message(_position_msg(lat=41.0))
        calls = p.event_bus.publish.call_args_list
        event_types = [c[0][0] for c in calls]
        assert events.AIS_VESSEL_DETECTED not in event_types


class TestEvictOldest:
    def test_evicts_least_recently_seen(self):
        p = _make_plugin({"max_vessels": 2})
        p._handle_message(_position_msg(mmsi="AAA"))
        p._vessels["AAA"]["last_seen"] = time.time() - 1000
        p._handle_message(_position_msg(mmsi="BBB"))
        p._handle_message(_position_msg(mmsi="CCC"))
        assert "AAA" not in p._vessels
        assert "BBB" in p._vessels
        assert "CCC" in p._vessels


class TestSnapshotAndStatus:
    def test_snapshot_empty(self):
        p = _make_plugin()
        p._update_snapshot_cache()
        snap = p.get_snapshot()
        assert snap["vessels"] == []
        assert snap["stats"]["vessel_count"] == 0

    def test_snapshot_with_vessel(self):
        p = _make_plugin()
        p._handle_message(_position_msg())
        p._update_snapshot_cache()
        snap = p.get_snapshot()
        assert len(snap["vessels"]) == 1
        assert snap["stats"]["vessel_count"] == 1

    def test_get_status(self):
        p = _make_plugin()
        p._handle_message(_position_msg())
        s = p.get_status()
        assert s["active"] is True
        assert s["status"] == "idle"
        assert s["vessel_count"] == 1
        assert s["messages_total"] == 1
