"""Tests for the ACARS decoder plugin."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

from reticulumpi import events
from reticulumpi.builtin_plugins.acars_decoder import ACARSDecoder


def _make_app() -> MagicMock:
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.event_bus = MagicMock()
    return app


def _make_plugin(config: dict | None = None) -> ACARSDecoder:
    plugin = ACARSDecoder(_make_app(), config or {})
    plugin._active = True
    plugin._recent_messages = __import__("collections").deque(maxlen=200)
    plugin._stats = {
        "messages_total": 0,
        "messages_by_label": {},
        "messages_by_freq": {},
        "unique_flights_today": 0,
        "unique_tails_today": 0,
        "error_count": 0,
        "last_message_at": None,
    }
    plugin._seen_flights = set()
    plugin._seen_tails = set()
    plugin._daily_reset_ts = time.time()
    plugin._airline_stats = {}
    plugin._hourly_rate = __import__("collections").deque(maxlen=24)
    plugin._hourly_count = 0
    plugin._hourly_ts = time.time()
    plugin._level_min = None
    plugin._level_max = None
    plugin._level_sum = 0.0
    plugin._level_count = 0
    plugin._status = "idle"
    plugin._last_error = None
    plugin._restart_count = 0
    plugin._snapshot_dirty = True
    plugin._process = None
    plugin._pid = None
    return plugin


def _acars_msg(
    flight: str = "UAL123",
    tail: str = "N12345",
    label: str = "H1",
    freq: float = 131.55,
    text: str = "HELLO WORLD",
    level: float = -20.0,
    error: int = 0,
) -> dict[str, Any]:
    return {
        "timestamp": time.time(),
        "flight": flight,
        "tail": tail,
        "label": label,
        "freq": freq,
        "text": text,
        "level": level,
        "error": error,
        "mode": "2",
        "block_id": "1",
        "msgno": "M01A",
    }


class TestValidateConfig:
    def test_defaults(self):
        p = ACARSDecoder(_make_app(), {})
        assert p._decoder_bin == "acarsdec"
        assert p._gain is None
        assert p._ppm == 0
        assert p._frequencies == [131.550, 131.525, 131.725]
        assert p._max_messages == 200
        assert p._max_restarts == 5
        assert p._station_id == "reticulumpi"

    def test_custom_config(self):
        p = ACARSDecoder(
            _make_app(),
            {
                "decoder_bin": "/usr/local/bin/acarsdec",
                "gain": 42,
                "ppm": 3,
                "frequencies_mhz": [130.025, 131.550],
                "max_messages": 100,
                "max_restarts": 2,
                "station_id": "mystation",
            },
        )
        assert p._decoder_bin == "/usr/local/bin/acarsdec"
        assert p._gain == 42
        assert p._ppm == 3
        assert p._frequencies == [130.025, 131.550]
        assert p._max_messages == 100
        assert p._max_restarts == 2
        assert p._station_id == "mystation"


class TestHandleMessage:
    def test_basic_message(self):
        p = _make_plugin()
        p._handle_message(_acars_msg())
        assert p._stats["messages_total"] == 1
        assert len(p._recent_messages) == 1
        rec = p._recent_messages[0]
        assert rec["flight"] == "UAL123"
        assert rec["tail"] == "N12345"
        assert rec["label"] == "H1"
        assert rec["label_desc"] == "General message"

    def test_flight_and_tail_tracking(self):
        p = _make_plugin()
        p._handle_message(_acars_msg(flight="UAL123", tail="N12345"))
        p._handle_message(_acars_msg(flight="UAL123", tail="N12345"))
        p._handle_message(_acars_msg(flight="DAL456", tail="N67890"))
        assert p._stats["unique_flights_today"] == 2
        assert p._stats["unique_tails_today"] == 2

    def test_airline_code_tracking(self):
        p = _make_plugin()
        p._handle_message(_acars_msg(flight="UAL123"))
        p._handle_message(_acars_msg(flight="UAL456"))
        p._handle_message(_acars_msg(flight="DAL789"))
        assert p._airline_stats["UAL"] == 2
        assert p._airline_stats["DAL"] == 1

    def test_error_count(self):
        p = _make_plugin()
        p._handle_message(_acars_msg(error=1))
        assert p._stats["error_count"] == 1

    def test_freq_and_label_counts(self):
        p = _make_plugin()
        p._handle_message(_acars_msg(label="H1", freq=131.55))
        p._handle_message(_acars_msg(label="SA", freq=131.55))
        p._handle_message(_acars_msg(label="H1", freq=131.525))
        assert p._stats["messages_by_label"]["H1"] == 2
        assert p._stats["messages_by_label"]["SA"] == 1
        assert p._stats["messages_by_freq"]["131.55"] == 2
        assert p._stats["messages_by_freq"]["131.525"] == 1

    def test_signal_level_tracking(self):
        p = _make_plugin()
        p._handle_message(_acars_msg(level=-10.0))
        p._handle_message(_acars_msg(level=-30.0))
        assert p._level_min == -30.0
        assert p._level_max == -10.0
        assert p._level_count == 2

    def test_empty_flight_not_tracked(self):
        p = _make_plugin()
        p._handle_message(_acars_msg(flight="", tail=""))
        assert p._stats["unique_flights_today"] == 0
        assert p._stats["unique_tails_today"] == 0
        assert p._airline_stats == {}


class TestEventEmission:
    def test_message_emits_event(self):
        p = _make_plugin()
        p._handle_message(_acars_msg())
        calls = p.event_bus.publish.call_args_list
        event_types = [c[0][0] for c in calls]
        assert events.ACARS_MESSAGE_DECODED in event_types

    def test_event_payload(self):
        p = _make_plugin()
        p._handle_message(_acars_msg(flight="UAL123", tail="N12345", label="SA"))
        call = p.event_bus.publish.call_args
        assert call[0][1]["flight"] == "UAL123"
        assert call[0][1]["tail"] == "N12345"
        assert call[0][1]["label"] == "SA"


class TestDailyReset:
    def test_reset_clears_seen_sets(self):
        p = _make_plugin()
        p._handle_message(_acars_msg(flight="UAL1", tail="N1"))
        assert p._stats["unique_flights_today"] == 1
        p._daily_reset_ts = time.time() - 90000
        p._handle_message(_acars_msg(flight="UAL1", tail="N1"))
        assert p._stats["unique_flights_today"] == 1
        assert len(p._seen_flights) == 1


class TestSnapshotAndStatus:
    def test_snapshot_cache(self):
        p = _make_plugin()
        p._handle_message(_acars_msg())
        p._update_snapshot_cache()
        snap = p.get_snapshot()
        assert snap is not None
        assert snap["status"] == "idle"
        assert len(snap["recent_messages"]) == 1

    def test_get_status(self):
        p = _make_plugin()
        p._handle_message(_acars_msg())
        s = p.get_status()
        assert s["active"] is True
        assert s["status"] == "idle"
        assert s["messages_total"] == 1
        assert s["unique_flights_today"] == 1
