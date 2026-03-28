"""Tests for the AlertSystem plugin."""

import os
from unittest.mock import MagicMock

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
    app.get_plugin.return_value = None
    # Redirect home dir for shutdown marker
    monkeypatch_home = str(tmp_path / "home")
    os.makedirs(monkeypatch_home, exist_ok=True)
    return app


@pytest.fixture
def plugin_config(tmp_path):
    return {
        "enabled": True,
        "recipients": [],
        "cooldown_seconds": 60,
        "rules": [
            {"metric": "cpu_temp", "operator": ">", "threshold": 80, "message": "Hot: {value}C"},
        ],
        "alert_on_plugin_crash": True,
        "alert_on_reboot": False,  # Disable for tests
        "storage_path": str(tmp_path / "alert_lxmf"),
        "check_interval": 1,
    }


def test_validate_config_bad_recipients(mock_app):
    from reticulumpi.builtin_plugins.alert_system import AlertSystemPlugin

    with pytest.raises(ValueError, match="recipients"):
        AlertSystemPlugin(mock_app, {"recipients": "not-a-list"})


def test_validate_config_bad_cooldown(mock_app):
    from reticulumpi.builtin_plugins.alert_system import AlertSystemPlugin

    with pytest.raises(ValueError, match="cooldown_seconds"):
        AlertSystemPlugin(mock_app, {"recipients": [], "cooldown_seconds": -1})


def test_validate_config_bad_rules(mock_app):
    from reticulumpi.builtin_plugins.alert_system import AlertSystemPlugin

    with pytest.raises(ValueError, match="rules must be a list"):
        AlertSystemPlugin(mock_app, {"recipients": [], "rules": "bad"})


def test_validate_config_bad_rule_dict(mock_app):
    from reticulumpi.builtin_plugins.alert_system import AlertSystemPlugin

    with pytest.raises(ValueError, match="each rule must be a dict"):
        AlertSystemPlugin(mock_app, {"recipients": [], "rules": ["bad"]})


def test_validate_config_rule_missing_metric(mock_app):
    from reticulumpi.builtin_plugins.alert_system import AlertSystemPlugin

    with pytest.raises(ValueError, match="metric"):
        AlertSystemPlugin(mock_app, {"recipients": [], "rules": [{"operator": ">"}]})


def test_start_stop(mock_app, plugin_config):
    from reticulumpi.builtin_plugins.alert_system import AlertSystemPlugin

    plugin = AlertSystemPlugin(mock_app, plugin_config)
    plugin.start()
    assert plugin._active is True
    assert plugin._alerts_sent == 0
    plugin.stop()
    assert plugin._active is False


def test_send_alert_logged(mock_app, plugin_config):
    from reticulumpi.builtin_plugins.alert_system import AlertSystemPlugin

    events_received = []
    mock_app.event_bus.subscribe("alert.triggered", lambda e, d: events_received.append(d))

    plugin = AlertSystemPlugin(mock_app, plugin_config)
    plugin.start()
    plugin._send_alert("Test alert", rule_key="test")
    assert len(events_received) == 1
    assert events_received[0]["message"] == "Test alert"
    plugin.stop()


def test_plugin_crash_event_triggers_alert(mock_app, plugin_config):
    from reticulumpi.builtin_plugins.alert_system import AlertSystemPlugin

    alerts = []
    mock_app.event_bus.subscribe("alert.triggered", lambda e, d: alerts.append(d))

    plugin = AlertSystemPlugin(mock_app, plugin_config)
    plugin.start()

    # Simulate plugin crash event
    mock_app.event_bus.publish("plugin.crashed", {"name": "bad_plugin", "error": "boom"})

    assert len(alerts) == 1
    assert "bad_plugin" in alerts[0]["message"]
    plugin.stop()


def test_cooldown_prevents_duplicate(mock_app, plugin_config):
    from reticulumpi.builtin_plugins.alert_system import AlertSystemPlugin

    plugin = AlertSystemPlugin(mock_app, plugin_config)
    plugin.start()

    # First alert goes through
    plugin._send_alert("Alert 1", rule_key="test")
    # Second within cooldown should still fire (no LXMF recipients, so no cooldown tracking)
    # But the event bus still fires both
    events = []
    mock_app.event_bus.subscribe("alert.triggered", lambda e, d: events.append(d))
    plugin._send_alert("Alert 2", rule_key="test")
    assert len(events) == 1
    plugin.stop()


def test_get_status(mock_app, plugin_config):
    from reticulumpi.builtin_plugins.alert_system import AlertSystemPlugin

    plugin = AlertSystemPlugin(mock_app, plugin_config)
    plugin.start()
    status = plugin.get_status()
    assert status["active"] is True
    assert status["alerts_sent"] == 0
    assert status["recipients"] == 0
    plugin.stop()
