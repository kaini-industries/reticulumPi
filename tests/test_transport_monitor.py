"""Tests for the TransportMonitor plugin."""

import time
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi import events
from reticulumpi.event_bus import EventBus


@pytest.fixture
def mock_app():
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.identity.hash = b"\x01" * 16
    app.event_bus = EventBus()
    app.plugins = {}
    app.node_name = "TestNode"
    app.get_plugin.return_value = None
    return app


@pytest.fixture
def base_config():
    return {
        "enabled": True,
        "check_interval": 5,
        "down_threshold": 10,
        "auto_teardown_fallback": True,
        "primary_hubs": [
            {"name": "Primary", "target_host": "192.168.1.1", "target_port": 4242},
        ],
        "fallback_hubs": [
            {"name": "Fallback", "target_host": "10.0.0.1", "target_port": 4242},
        ],
    }


def _start_plugin(mock_app, config):
    from reticulumpi.builtin_plugins.transport_monitor import TransportMonitorPlugin

    with patch("RNS.Transport") as mt:
        mt.interfaces = []
        plugin = TransportMonitorPlugin(mock_app, config)
        plugin.start()
    return plugin


# --- Config validation ---


def test_validate_config_valid(mock_app, base_config):
    _start_plugin(mock_app, base_config).stop()


def test_validate_config_bad_interval(mock_app, base_config):
    from reticulumpi.builtin_plugins.transport_monitor import TransportMonitorPlugin
    base_config["check_interval"] = 2
    with pytest.raises(ValueError, match="check_interval"):
        with patch("RNS.Transport") as mt:
            mt.interfaces = []
            TransportMonitorPlugin(mock_app, base_config)


def test_validate_config_bad_threshold(mock_app, base_config):
    from reticulumpi.builtin_plugins.transport_monitor import TransportMonitorPlugin
    base_config["down_threshold"] = 5
    with pytest.raises(ValueError, match="down_threshold"):
        with patch("RNS.Transport") as mt:
            mt.interfaces = []
            TransportMonitorPlugin(mock_app, base_config)


def test_validate_config_missing_hub_host(mock_app, base_config):
    from reticulumpi.builtin_plugins.transport_monitor import TransportMonitorPlugin
    base_config["primary_hubs"] = [{"target_port": 4242}]
    with pytest.raises(ValueError, match="target_host"):
        with patch("RNS.Transport") as mt:
            mt.interfaces = []
            TransportMonitorPlugin(mock_app, base_config)


def test_validate_config_missing_hub_port(mock_app, base_config):
    from reticulumpi.builtin_plugins.transport_monitor import TransportMonitorPlugin
    base_config["fallback_hubs"] = [{"target_host": "x.com"}]
    with pytest.raises(ValueError, match="target_port"):
        with patch("RNS.Transport") as mt:
            mt.interfaces = []
            TransportMonitorPlugin(mock_app, base_config)


def test_validate_config_hubs_not_list(mock_app, base_config):
    from reticulumpi.builtin_plugins.transport_monitor import TransportMonitorPlugin
    base_config["primary_hubs"] = "not-a-list"
    with pytest.raises(ValueError, match="primary_hubs must be a list"):
        with patch("RNS.Transport") as mt:
            mt.interfaces = []
            TransportMonitorPlugin(mock_app, base_config)


# --- Lifecycle ---


def test_start_stop(mock_app, base_config):
    plugin = _start_plugin(mock_app, base_config)
    assert plugin._active is True
    assert len(plugin._hub_status) == 1
    plugin.stop()
    assert plugin._active is False


# --- Health check ---


def test_hub_offline_event(mock_app, base_config):
    plugin = _start_plugin(mock_app, base_config)

    # Simulate: was online, now offline
    key = "192.168.1.1:4242"
    with plugin._lock:
        plugin._hub_status[key]["online"] = True

    received = []
    mock_app.event_bus.subscribe(events.HUB_OFFLINE, lambda t, d: received.append(d))

    with patch.object(plugin, "_probe_tcp", return_value=False):
        plugin._check_health()

    assert len(received) == 1
    assert received[0]["target_host"] == "192.168.1.1"
    plugin.stop()


def test_hub_online_event(mock_app, base_config):
    plugin = _start_plugin(mock_app, base_config)

    received = []
    mock_app.event_bus.subscribe(events.HUB_ONLINE, lambda t, d: received.append(d))

    with patch.object(plugin, "_probe_tcp", return_value=True):
        plugin._check_health()

    assert len(received) == 1
    plugin.stop()


def test_no_event_when_stable(mock_app, base_config):
    plugin = _start_plugin(mock_app, base_config)

    received = []
    mock_app.event_bus.subscribe(events.HUB_ONLINE, lambda t, d: received.append(d))
    mock_app.event_bus.subscribe(events.HUB_OFFLINE, lambda t, d: received.append(d))

    # Both checks report online — second check should fire no event
    with patch.object(plugin, "_probe_tcp", return_value=True):
        plugin._check_health()
        plugin._check_health()

    assert len(received) == 1  # Only the initial offline -> online transition
    plugin.stop()


# --- Failover ---


def test_failover_not_before_threshold(mock_app, base_config):
    plugin = _start_plugin(mock_app, base_config)

    with patch.object(plugin, "_probe_tcp", return_value=False):
        plugin._check_health()

    assert plugin._all_down_since is not None
    assert plugin._fallback_active is False
    plugin.stop()


def test_failover_after_threshold(mock_app, base_config):
    base_config["down_threshold"] = 10
    plugin = _start_plugin(mock_app, base_config)

    with patch.object(plugin, "_probe_tcp", return_value=False):
        plugin._check_health()

    # Push past threshold
    with plugin._lock:
        plugin._all_down_since = time.monotonic() - 15

    mock_fallback = MagicMock()
    with patch.object(plugin, "_probe_tcp", return_value=False), \
         patch("reticulumpi.builtin_plugins.transport_monitor.TCPClientInterface",
               create=True, return_value=mock_fallback), \
         patch("RNS.Transport") as mt:
        mt.interfaces = []
        plugin._check_health()

    assert plugin._fallback_active is True
    assert len(plugin._active_fallbacks) == 1
    plugin.stop()


def test_fallback_teardown_on_recovery(mock_app, base_config):
    plugin = _start_plugin(mock_app, base_config)

    mock_fallback = MagicMock()
    mock_fallback.name = "FallbackIface"
    with plugin._lock:
        plugin._active_fallbacks.append(mock_fallback)
        plugin._fallback_active = True

    received = []
    mock_app.event_bus.subscribe(events.FALLBACK_DEACTIVATED, lambda t, d: received.append(d))

    with patch.object(plugin, "_probe_tcp", return_value=True), \
         patch("RNS.Transport") as mt:
        mt.interfaces = [mock_fallback]
        plugin._check_health()

    assert plugin._fallback_active is False
    assert len(plugin._active_fallbacks) == 0
    mock_fallback.detach.assert_called_once()
    assert len(received) == 1
    plugin.stop()


def test_fallback_kept_when_auto_teardown_false(mock_app, base_config):
    base_config["auto_teardown_fallback"] = False
    plugin = _start_plugin(mock_app, base_config)

    mock_fallback = MagicMock()
    with plugin._lock:
        plugin._active_fallbacks.append(mock_fallback)
        plugin._fallback_active = True

    with patch.object(plugin, "_probe_tcp", return_value=True):
        plugin._check_health()

    assert plugin._fallback_active is True
    plugin.stop()


# --- Status ---


def test_get_status(mock_app, base_config):
    plugin = _start_plugin(mock_app, base_config)
    status = plugin.get_status()
    assert status["active"] is True
    assert status["primary_count"] == 1
    assert status["fallback_active"] is False
    plugin.stop()


def test_get_hub_health(mock_app, base_config):
    plugin = _start_plugin(mock_app, base_config)
    health = plugin.get_hub_health()
    assert len(health["primaries"]) == 1
    assert health["primaries"][0]["name"] == "Primary"
    assert health["fallback_active"] is False
    assert health["active_fallbacks"] == []
    plugin.stop()


# --- Edge cases ---


def test_no_primaries_no_crash(mock_app, base_config):
    base_config["primary_hubs"] = []
    plugin = _start_plugin(mock_app, base_config)

    with patch.object(plugin, "_probe_tcp", return_value=False):
        plugin._check_health()  # Should not crash

    assert plugin._fallback_active is False
    plugin.stop()


def test_no_fallback_hubs_configured(mock_app, base_config):
    base_config["fallback_hubs"] = []
    base_config["down_threshold"] = 10
    plugin = _start_plugin(mock_app, base_config)

    with patch.object(plugin, "_probe_tcp", return_value=False):
        plugin._check_health()
        with plugin._lock:
            plugin._all_down_since = time.monotonic() - 15
        plugin._check_health()

    assert plugin._fallback_active is False
    plugin.stop()


def test_probe_tcp_reachable(mock_app, base_config):
    """Test that _probe_tcp works with a mock socket."""
    from reticulumpi.builtin_plugins.transport_monitor import TransportMonitorPlugin

    with patch("reticulumpi.builtin_plugins.transport_monitor.socket") as mock_sock:
        mock_conn = MagicMock()
        mock_sock.create_connection.return_value = mock_conn
        assert TransportMonitorPlugin._probe_tcp("1.2.3.4", 4242) is True
        mock_conn.close.assert_called_once()


def test_probe_tcp_unreachable(mock_app, base_config):
    """Test that _probe_tcp returns False on connection failure."""
    from reticulumpi.builtin_plugins.transport_monitor import TransportMonitorPlugin

    with patch("reticulumpi.builtin_plugins.transport_monitor.socket") as mock_sock:
        mock_sock.create_connection.side_effect = OSError("Connection refused")
        mock_sock.timeout = OSError
        assert TransportMonitorPlugin._probe_tcp("1.2.3.4", 4242) is False
