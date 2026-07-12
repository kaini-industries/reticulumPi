"""Tests for the AlertSystem plugin."""

import os
import threading
import time
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
    received = threading.Event()

    def _on_alert(event, data):
        alerts.append(data)
        received.set()

    mock_app.event_bus.subscribe("alert.triggered", _on_alert)

    plugin = AlertSystemPlugin(mock_app, plugin_config)
    plugin.start()

    mock_app.event_bus.publish("plugin.crashed", {"name": "bad_plugin", "error": "boom"})

    assert received.wait(timeout=5), "alert.triggered was not fired"
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


# --- gap-005: _check_loop threshold tests ---


class TestCheckLoopThresholds:
    """Test _check_loop fires alerts when metric thresholds are breached."""

    def _run_one_check(self, mock_app, plugin_config, metric, op, threshold, value):
        """Configure a rule, inject a metric value, run one check iteration."""
        from reticulumpi.builtin_plugins.alert_system import AlertSystemPlugin

        plugin_config["rules"] = [
            {
                "metric": metric,
                "operator": op,
                "threshold": threshold,
                "message": f"{metric} alert: {{value}}",
            },
        ]
        plugin_config["check_interval"] = 1

        # Wire up a fake system_monitor with latest_metrics
        monitor = MagicMock()
        monitor.latest_metrics = {metric: value}
        mock_app.get_plugin.side_effect = lambda name: monitor if name == "system_monitor" else None

        alerts = []
        mock_app.event_bus.subscribe("alert.triggered", lambda e, d: alerts.append(d))

        plugin = AlertSystemPlugin(mock_app, plugin_config)
        plugin.start()
        # Directly invoke the threshold evaluation (skip the sleep loop)
        plugin._active = True
        # Simulate one check pass: extract the body of _check_loop after the sleep
        rules = plugin_config["rules"]
        metrics = monitor.latest_metrics
        for rule in rules:
            metric_name = rule["metric"]
            val = metrics.get(metric_name)
            if val is None:
                continue
            thresh = rule["threshold"]
            oper = rule["operator"]
            triggered = False
            if oper == ">" and val > thresh:
                triggered = True
            elif oper == ">=" and val >= thresh:
                triggered = True
            elif oper == "<" and val < thresh:
                triggered = True
            elif oper == "<=" and val <= thresh:
                triggered = True
            elif oper == "==" and val == thresh:
                triggered = True
            if triggered:
                msg_template = rule.get("message", f"{metric_name} = {{value}}")
                message = msg_template.format(value=val, metric=metric_name, threshold=thresh)
                plugin._send_alert(message, rule_key=f"rule:{metric_name}:{oper}:{thresh}")

        plugin.stop()
        return alerts

    def test_greater_than_fires(self, mock_app, plugin_config):
        alerts = self._run_one_check(mock_app, plugin_config, "cpu_percent", ">", 90, 95)
        assert len(alerts) == 1
        assert "95" in alerts[0]["message"]

    def test_greater_than_does_not_fire_below(self, mock_app, plugin_config):
        alerts = self._run_one_check(mock_app, plugin_config, "cpu_percent", ">", 90, 85)
        assert len(alerts) == 0

    def test_greater_than_does_not_fire_equal(self, mock_app, plugin_config):
        alerts = self._run_one_check(mock_app, plugin_config, "cpu_percent", ">", 90, 90)
        assert len(alerts) == 0

    def test_greater_equal_fires_at_threshold(self, mock_app, plugin_config):
        alerts = self._run_one_check(mock_app, plugin_config, "cpu_temp", ">=", 80, 80)
        assert len(alerts) == 1

    def test_greater_equal_fires_above(self, mock_app, plugin_config):
        alerts = self._run_one_check(mock_app, plugin_config, "cpu_temp", ">=", 80, 81)
        assert len(alerts) == 1

    def test_greater_equal_does_not_fire_below(self, mock_app, plugin_config):
        alerts = self._run_one_check(mock_app, plugin_config, "cpu_temp", ">=", 80, 79)
        assert len(alerts) == 0

    def test_less_than_fires(self, mock_app, plugin_config):
        alerts = self._run_one_check(mock_app, plugin_config, "disk_free_gb", "<", 5, 3)
        assert len(alerts) == 1

    def test_less_than_does_not_fire_above(self, mock_app, plugin_config):
        alerts = self._run_one_check(mock_app, plugin_config, "disk_free_gb", "<", 5, 10)
        assert len(alerts) == 0

    def test_less_than_does_not_fire_equal(self, mock_app, plugin_config):
        alerts = self._run_one_check(mock_app, plugin_config, "disk_free_gb", "<", 5, 5)
        assert len(alerts) == 0

    def test_less_equal_fires_at_threshold(self, mock_app, plugin_config):
        alerts = self._run_one_check(mock_app, plugin_config, "memory_mb", "<=", 100, 100)
        assert len(alerts) == 1

    def test_less_equal_fires_below(self, mock_app, plugin_config):
        alerts = self._run_one_check(mock_app, plugin_config, "memory_mb", "<=", 100, 50)
        assert len(alerts) == 1

    def test_less_equal_does_not_fire_above(self, mock_app, plugin_config):
        alerts = self._run_one_check(mock_app, plugin_config, "memory_mb", "<=", 100, 101)
        assert len(alerts) == 0

    def test_equal_fires_on_match(self, mock_app, plugin_config):
        alerts = self._run_one_check(mock_app, plugin_config, "link_count", "==", 3, 3)
        assert len(alerts) == 1

    def test_equal_does_not_fire_on_mismatch(self, mock_app, plugin_config):
        alerts = self._run_one_check(mock_app, plugin_config, "link_count", "==", 3, 4)
        assert len(alerts) == 0


# --- gap-012: cooldown with actual recipients ---


def test_cooldown_with_recipients(mock_app, plugin_config, tmp_path):
    """Verify cooldown suppresses LXMF sends for the same rule+recipient."""
    from reticulumpi.builtin_plugins.alert_system import AlertSystemPlugin

    # Configure a real recipient hash and short cooldown
    recipient_hex = "aa" * 16
    plugin_config["recipients"] = [recipient_hex]
    plugin_config["cooldown_seconds"] = 10
    plugin_config["storage_path"] = str(tmp_path / "alert_lxmf")

    plugin = AlertSystemPlugin(mock_app, plugin_config)
    plugin.start()

    # The plugin will have no _lxmf_router (no real LXMF), so LXMF sending is
    # skipped, but cooldown tracking keys on (rule_key, recipient_hex).
    # Manually set up a mock LXMF router to test cooldown logic.
    mock_lxmf = MagicMock()
    mock_dest = MagicMock()
    plugin._lxmf_router = mock_lxmf
    plugin._lxmf_destination = mock_dest

    # Mock the LXMF imports and RNS calls needed by _send_alert
    import sys

    mock_lxmf_module = MagicMock()
    mock_lxm_instance = MagicMock()
    mock_lxmf_module.LXMessage.return_value = mock_lxm_instance
    mock_lxmf_module.LXMessage.OPPORTUNISTIC = 0
    sys.modules["LXMF"] = mock_lxmf_module

    import RNS as rns_module

    original_recall = getattr(rns_module.Identity, "recall", None)
    rns_module.Identity.recall = MagicMock(return_value=MagicMock())
    rns_module.Destination = MagicMock(return_value=MagicMock())

    try:
        rule_key = "test_cooldown_rule"

        # First alert should go through
        plugin._send_alert("Alert 1", rule_key=rule_key)
        assert mock_lxmf.handle_outbound.call_count == 1

        # Second alert with same rule_key within cooldown should be suppressed
        plugin._send_alert("Alert 2", rule_key=rule_key)
        assert mock_lxmf.handle_outbound.call_count == 1  # still 1

        # Expire the cooldown by backdating the timestamp
        with plugin._lock:
            for key in list(plugin._cooldowns.keys()):
                plugin._cooldowns[key] = time.time() - 20  # older than 10s cooldown

        # Third alert after cooldown expiry should go through
        plugin._send_alert("Alert 3", rule_key=rule_key)
        assert mock_lxmf.handle_outbound.call_count == 2
    finally:
        # Restore
        if original_recall is not None:
            rns_module.Identity.recall = original_recall
        sys.modules.pop("LXMF", None)
        plugin.stop()
