"""Tests for the ISM band decoder plugin."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

from reticulumpi import events
from reticulumpi.builtin_plugins.ism_decoder import ISMDecoder


def _make_app() -> MagicMock:
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.event_bus = MagicMock()
    return app


def _make_plugin(config: dict | None = None) -> ISMDecoder:
    plugin = ISMDecoder(_make_app(), config or {})
    plugin._active = True
    plugin._devices = {}
    plugin._stats = {
        "messages_total": 0,
        "devices_total": 0,
        "devices_active": 0,
        "last_message_at": None,
    }
    plugin._status = "idle"
    plugin._last_error = None
    plugin._restart_count = 0
    plugin._process = None
    plugin._pid = None
    return plugin


def _weather_msg(
    model: str = "Acurite-5n1",
    dev_id: Any = 1234,
    temperature: float = 22.5,
    humidity: int = 55,
    channel: str | None = "A",
) -> dict[str, Any]:
    msg: dict[str, Any] = {"model": model, "id": dev_id}
    if channel is not None:
        msg["channel"] = channel
    msg["temperature_C"] = temperature
    msg["humidity"] = humidity
    msg["battery_ok"] = 1
    return msg


class TestValidateConfig:
    def test_defaults(self):
        p = ISMDecoder(_make_app(), {})
        assert p._decoder_bin == "rtl_433"
        assert p._gain is None
        assert p._ppm == 0
        assert p._protocols == []
        assert p._protocol_blacklist == []
        assert p._max_devices == 100
        assert p._stale_timeout == 600.0
        assert p._max_restarts == 5

    def test_custom_config(self):
        p = ISMDecoder(
            _make_app(),
            {
                "decoder_bin": "/usr/local/bin/rtl_433",
                "gain": 40,
                "ppm": 2,
                "protocols": [40, 41, 42],
                "protocol_blacklist": [100, 101],
                "max_devices": 50,
                "stale_timeout": 300,
                "max_restarts": 3,
            },
        )
        assert p._decoder_bin == "/usr/local/bin/rtl_433"
        assert p._gain == 40
        assert p._ppm == 2
        assert p._protocols == [40, 41, 42]
        assert p._protocol_blacklist == [100, 101]
        assert p._max_devices == 50
        assert p._stale_timeout == 300.0
        assert p._max_restarts == 3


class TestHandleDevice:
    def test_new_device_creates_entry(self):
        p = _make_plugin()
        msg = _weather_msg()
        p._handle_device(msg)
        assert len(p._devices) == 1
        key = "Acurite-5n1:1234"
        assert key in p._devices
        dev = p._devices[key]
        assert dev["model"] == "Acurite-5n1"
        assert dev["id"] == 1234
        assert dev["temperature_C"] == 22.5
        assert dev["humidity"] == 55
        assert dev["battery_ok"] == 1
        assert dev["channel"] == "A"
        assert dev["message_count"] == 1
        assert p._stats["messages_total"] == 1
        assert p._stats["devices_total"] == 1

    def test_existing_device_updates(self):
        p = _make_plugin()
        p._handle_device(_weather_msg(temperature=20.0))
        p._handle_device(_weather_msg(temperature=25.0))
        assert len(p._devices) == 1
        dev = p._devices["Acurite-5n1:1234"]
        assert dev["temperature_C"] == 25.0
        assert dev["message_count"] == 2
        assert p._stats["messages_total"] == 2
        assert p._stats["devices_total"] == 1

    def test_model_only_key(self):
        p = _make_plugin()
        p._handle_device({"model": "Generic-Remote", "id": ""})
        assert "Generic-Remote" in p._devices

    def test_empty_model_ignored(self):
        p = _make_plugin()
        p._handle_device({"id": "123"})
        assert len(p._devices) == 0
        assert p._stats["messages_total"] == 0

    def test_max_devices_limit(self):
        p = _make_plugin({"max_devices": 2})
        p._handle_device(_weather_msg(model="A", dev_id=1))
        p._handle_device(_weather_msg(model="B", dev_id=2))
        p._handle_device(_weather_msg(model="C", dev_id=3))
        assert len(p._devices) == 2
        assert "A:1" in p._devices
        assert "B:2" in p._devices
        assert "C:3" not in p._devices

    def test_existing_device_update_above_limit(self):
        p = _make_plugin({"max_devices": 2})
        p._handle_device(_weather_msg(model="A", dev_id=1))
        p._handle_device(_weather_msg(model="B", dev_id=2))
        # Updating existing device should still work
        p._handle_device(_weather_msg(model="A", dev_id=1, temperature=30.0))
        assert p._devices["A:1"]["temperature_C"] == 30.0
        assert p._devices["A:1"]["message_count"] == 2

    def test_weather_fields(self):
        p = _make_plugin()
        p._handle_device(
            {
                "model": "WS",
                "id": 1,
                "wind_avg_km_h": 15.5,
                "wind_max_km_h": 22.0,
                "wind_dir_deg": 180,
                "rain_mm": 5.2,
                "pressure_hPa": 1013.25,
            }
        )
        dev = p._devices["WS:1"]
        assert dev["wind_avg_km_h"] == 15.5
        assert dev["wind_max_km_h"] == 22.0
        assert dev["wind_dir_deg"] == 180
        assert dev["rain_mm"] == 5.2
        assert dev["pressure_hPa"] == 1013.25

    def test_signal_fields(self):
        p = _make_plugin()
        p._handle_device(
            {
                "model": "Sensor",
                "id": 5,
                "rssi": -85.0,
                "snr": 12.0,
                "noise": -97.0,
                "freq": 433.92,
                "protocol": 42,
            }
        )
        dev = p._devices["Sensor:5"]
        assert dev["rssi"] == -85.0
        assert dev["snr"] == 12.0
        assert dev["noise"] == -97.0
        assert dev["freq_mhz"] == 433.92
        assert dev["protocol"] == 42


class TestEventEmission:
    def test_new_device_emits_detected(self):
        p = _make_plugin()
        p._handle_device(_weather_msg())
        calls = p.event_bus.publish.call_args_list
        event_types = [c[0][0] for c in calls]
        assert events.ISM_DEVICE_DETECTED in event_types

    def test_existing_device_no_detected_event(self):
        p = _make_plugin()
        p._handle_device(_weather_msg())
        p.event_bus.publish.reset_mock()
        p._handle_device(_weather_msg(temperature=30.0))
        calls = p.event_bus.publish.call_args_list
        event_types = [c[0][0] for c in calls]
        assert events.ISM_DEVICE_DETECTED not in event_types


class TestEvictStale:
    def test_stale_device_removed(self):
        p = _make_plugin({"stale_timeout": 60})
        p._handle_device(_weather_msg())
        key = "Acurite-5n1:1234"
        # Backdate last_seen
        p._devices[key]["last_seen"] = time.time() - 120
        p._evict_stale()
        assert key not in p._devices
        p.event_bus.publish.assert_any_call(
            events.ISM_DEVICE_LOST,
            {
                "key": key,
                "model": "Acurite-5n1",
                "id": 1234,
                "last_seen": p.event_bus.publish.call_args_list[-1][0][1]["last_seen"],
            },
        )

    def test_fresh_device_kept(self):
        p = _make_plugin({"stale_timeout": 600})
        p._handle_device(_weather_msg())
        p._evict_stale()
        assert "Acurite-5n1:1234" in p._devices


class TestSnapshotAndInventory:
    def test_snapshot_empty(self):
        p = _make_plugin()
        p._update_snapshot_cache()
        snap = p.get_snapshot()
        assert snap["devices_active"] == 0
        assert snap["devices"] == []

    def test_snapshot_with_device(self):
        p = _make_plugin()
        p._handle_device(_weather_msg())
        snap = p.get_snapshot()
        assert snap["devices_active"] == 1
        assert len(snap["devices"]) == 1
        dev = snap["devices"][0]
        assert dev["model"] == "Acurite-5n1"
        assert dev["temperature_C"] == 22.5

    def test_snapshot_caps_at_20(self):
        p = _make_plugin({"max_devices": 30})
        for i in range(25):
            p._handle_device(_weather_msg(model=f"M{i}", dev_id=i))
        snap = p.get_snapshot()
        assert snap["devices_active"] == 25
        assert len(snap["devices"]) == 20

    def test_inventory_returns_all(self):
        p = _make_plugin({"max_devices": 30})
        for i in range(25):
            p._handle_device(_weather_msg(model=f"M{i}", dev_id=i))
        inv = p.get_device_inventory()
        assert len(inv["devices"]) == 25
        assert inv["devices_active"] == 25

    def test_inventory_excludes_first_seen(self):
        p = _make_plugin()
        p._handle_device(_weather_msg())
        inv = p.get_device_inventory()
        assert "first_seen" not in inv["devices"][0]


class TestGetStatus:
    def test_status_fields(self):
        p = _make_plugin()
        p._handle_device(_weather_msg())
        s = p.get_status()
        assert s["active"] is True
        assert s["status"] == "idle"
        assert s["error"] is None
        assert s["devices_active"] == 1
        assert s["messages_total"] == 1

    def test_status_unavailable(self):
        p = _make_plugin()
        p._status = "unavailable"
        p._last_error = "rtl_433 not found"
        s = p.get_status()
        assert s["status"] == "unavailable"
        assert s["error"] == "rtl_433 not found"
