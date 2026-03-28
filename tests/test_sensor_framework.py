"""Tests for the SensorFramework plugin."""

import os
import sqlite3
import time
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi.event_bus import EventBus


@pytest.fixture
def mock_app(tmp_path):
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.identity.hash = b"\x01" * 16
    app.event_bus = EventBus()
    app.plugins = {}
    app.node_name = "TestNode"
    return app


@pytest.fixture
def plugin_config(tmp_path):
    return {
        "enabled": True,
        "sensors": [
            {
                "name": "test_temp",
                "driver": "command",
                "command": "echo 22.5",
                "reading_name": "temperature",
            }
        ],
        "read_interval": 1,
        "storage": {
            "type": "sqlite",
            "path": str(tmp_path / "sensor_data.db"),
            "retention_days": 7,
        },
        "broadcast": {"enabled": False},
    }


def test_validate_config_bad_sensors(mock_app):
    from reticulumpi.builtin_plugins.sensor_framework import SensorFrameworkPlugin

    with pytest.raises(ValueError, match="sensors must be a list"):
        SensorFrameworkPlugin(mock_app, {"sensors": "bad"})


def test_validate_config_bad_sensor_entry(mock_app):
    from reticulumpi.builtin_plugins.sensor_framework import SensorFrameworkPlugin

    with pytest.raises(ValueError, match="must be a dict"):
        SensorFrameworkPlugin(mock_app, {"sensors": ["bad"]})


def test_validate_config_sensor_missing_name(mock_app):
    from reticulumpi.builtin_plugins.sensor_framework import SensorFrameworkPlugin

    with pytest.raises(ValueError, match="missing 'name'"):
        SensorFrameworkPlugin(mock_app, {"sensors": [{"driver": "command"}]})


def test_validate_config_sensor_missing_driver(mock_app):
    from reticulumpi.builtin_plugins.sensor_framework import SensorFrameworkPlugin

    with pytest.raises(ValueError, match="missing 'driver'"):
        SensorFrameworkPlugin(mock_app, {"sensors": [{"name": "x"}]})


def test_validate_config_unknown_driver(mock_app):
    from reticulumpi.builtin_plugins.sensor_framework import SensorFrameworkPlugin

    with pytest.raises(ValueError, match="unknown driver"):
        SensorFrameworkPlugin(mock_app, {"sensors": [{"name": "x", "driver": "nonexistent"}]})


def test_validate_config_bad_interval(mock_app):
    from reticulumpi.builtin_plugins.sensor_framework import SensorFrameworkPlugin

    with pytest.raises(ValueError, match="read_interval"):
        SensorFrameworkPlugin(mock_app, {"sensors": [], "read_interval": 0})


def test_validate_config_bad_storage_type(mock_app):
    from reticulumpi.builtin_plugins.sensor_framework import SensorFrameworkPlugin

    with pytest.raises(ValueError, match="storage.type"):
        SensorFrameworkPlugin(mock_app, {"sensors": [], "storage": {"type": "bad"}})


@patch("RNS.Destination")
def test_start_stop(mock_dest, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.sensor_framework import SensorFrameworkPlugin

    plugin = SensorFrameworkPlugin(mock_app, plugin_config)
    plugin.start()
    assert plugin._active is True
    assert len(plugin._drivers) == 1
    plugin.stop()
    assert plugin._active is False


def test_command_driver_read():
    from reticulumpi.builtin_plugins.sensor_framework import CommandDriver

    driver = CommandDriver({"command": "echo 42.0", "reading_name": "value"})
    reading = driver.read()
    assert reading["value"] == 42.0


def test_command_driver_no_command():
    from reticulumpi.builtin_plugins.sensor_framework import CommandDriver

    driver = CommandDriver({"command": "", "reading_name": "value"})
    reading = driver.read()
    assert "error" in reading


def test_ds18b20_driver_missing_device():
    from reticulumpi.builtin_plugins.sensor_framework import DS18B20Driver

    driver = DS18B20Driver({"address": "28-nonexistent"})
    reading = driver.read()
    assert "error" in reading


def test_adc_driver_missing_path():
    from reticulumpi.builtin_plugins.sensor_framework import ADCDriver

    driver = ADCDriver({"sysfs_path": "/nonexistent/path", "reading_name": "voltage"})
    reading = driver.read()
    assert "error" in reading


@patch("RNS.Destination")
def test_sqlite_storage(mock_dest, mock_app, tmp_path):
    from reticulumpi.builtin_plugins.sensor_framework import SensorFrameworkPlugin

    # Use no sensors to avoid the read loop storing extra readings
    config = {
        "sensors": [],
        "read_interval": 60,
        "storage": {
            "type": "sqlite",
            "path": str(tmp_path / "test_storage.db"),
        },
    }
    plugin = SensorFrameworkPlugin(mock_app, config)
    plugin.start()

    # Manually store a reading
    plugin._store_reading("test_sensor", {"temperature": 22.5}, time.time())

    # Verify it was stored
    db_path = config["storage"]["path"]
    with sqlite3.connect(db_path) as conn:
        rows = list(conn.execute("SELECT * FROM sensor_readings"))
    assert len(rows) == 1
    assert rows[0][1] == "test_sensor"
    assert rows[0][2] == "temperature"
    assert rows[0][3] == 22.5

    plugin.stop()


@patch("RNS.Destination")
def test_csv_storage(mock_dest, mock_app, tmp_path):
    from reticulumpi.builtin_plugins.sensor_framework import SensorFrameworkPlugin

    csv_path = str(tmp_path / "data.csv")
    config = {
        "sensors": [],
        "read_interval": 60,
        "storage": {"type": "csv", "path": csv_path},
    }
    plugin = SensorFrameworkPlugin(mock_app, config)
    plugin.start()

    plugin._store_reading("s1", {"value": 10.5}, time.time())

    with open(csv_path) as f:
        lines = f.readlines()
    assert len(lines) == 2  # header + 1 row
    assert "s1" in lines[1]

    plugin.stop()


@patch("RNS.Destination")
def test_get_sensor_history(mock_dest, mock_app, tmp_path):
    from reticulumpi.builtin_plugins.sensor_framework import SensorFrameworkPlugin

    config = {
        "sensors": [],
        "read_interval": 60,
        "storage": {
            "type": "sqlite",
            "path": str(tmp_path / "test_history.db"),
        },
    }
    plugin = SensorFrameworkPlugin(mock_app, config)
    plugin.start()

    now = time.time()
    plugin._store_reading("test_sensor", {"temperature": 20.0}, now - 10)
    plugin._store_reading("test_sensor", {"temperature": 22.0}, now)

    history = plugin.get_sensor_history("test_sensor")
    assert len(history) == 2
    assert history[0]["value"] == 22.0  # Most recent first

    plugin.stop()


@patch("RNS.Destination")
def test_get_status(mock_dest, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.sensor_framework import SensorFrameworkPlugin

    plugin = SensorFrameworkPlugin(mock_app, plugin_config)
    plugin.start()
    status = plugin.get_status()
    assert status["active"] is True
    assert status["sensor_count"] == 1
    assert status["readings_total"] == 0
    plugin.stop()


@patch("RNS.Destination")
def test_event_published_on_read(mock_dest, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.sensor_framework import SensorFrameworkPlugin

    events_received = []
    mock_app.event_bus.subscribe("sensor.reading", lambda e, d: events_received.append(d))

    plugin = SensorFrameworkPlugin(mock_app, plugin_config)
    plugin.start()

    # Manually trigger a read cycle for the command driver
    for sensor_cfg, driver in plugin._drivers:
        reading = driver.read()
        reading["timestamp"] = time.time()
        plugin._last_readings[sensor_cfg["name"]] = reading
        plugin.event_bus.publish("sensor.reading", {
            "sensor": sensor_cfg["name"],
            "driver": sensor_cfg["driver"],
            "reading": reading,
        })

    assert len(events_received) == 1
    assert events_received[0]["sensor"] == "test_temp"

    plugin.stop()
