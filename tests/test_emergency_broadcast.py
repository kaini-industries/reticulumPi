"""Tests for the EmergencyBroadcast plugin."""

from unittest.mock import MagicMock, patch

import pytest

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
    return app


@pytest.fixture
def plugin_config():
    return {
        "enabled": True,
        "max_ttl": 5,
        "max_stored_messages": 50,
        "rebroadcast_delay": 0,
        "rebroadcast": True,
    }


def test_validate_config_bad_ttl(mock_app):
    from reticulumpi.builtin_plugins.emergency_broadcast import EmergencyBroadcastPlugin

    with pytest.raises(ValueError, match="max_ttl"):
        EmergencyBroadcastPlugin(mock_app, {"max_ttl": 0})


def test_validate_config_ttl_too_high(mock_app):
    from reticulumpi.builtin_plugins.emergency_broadcast import EmergencyBroadcastPlugin

    with pytest.raises(ValueError, match="max_ttl"):
        EmergencyBroadcastPlugin(mock_app, {"max_ttl": 25})


def test_validate_config_bad_max_stored(mock_app):
    from reticulumpi.builtin_plugins.emergency_broadcast import EmergencyBroadcastPlugin

    with pytest.raises(ValueError, match="max_stored_messages"):
        EmergencyBroadcastPlugin(mock_app, {"max_stored_messages": 0})


def test_validate_config_bad_rebroadcast_delay(mock_app):
    from reticulumpi.builtin_plugins.emergency_broadcast import EmergencyBroadcastPlugin

    with pytest.raises(ValueError, match="rebroadcast_delay"):
        EmergencyBroadcastPlugin(mock_app, {"rebroadcast_delay": -1})


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_start_stop(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.emergency_broadcast import EmergencyBroadcastPlugin

    plugin = EmergencyBroadcastPlugin(mock_app, plugin_config)
    plugin.start()
    assert plugin._active is True
    assert len(plugin._messages) == 0
    plugin.stop()
    assert plugin._active is False


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_send_emergency(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.emergency_broadcast import (
        EmergencyBroadcastPlugin,
        PRIORITY_EMERGENCY,
    )

    events_received = []
    mock_app.event_bus.subscribe("emergency.received", lambda e, d: events_received.append(d))

    plugin = EmergencyBroadcastPlugin(mock_app, plugin_config)
    plugin.start()

    msg_id = plugin.send_emergency("Test emergency!", PRIORITY_EMERGENCY)
    assert msg_id is not None
    assert len(msg_id) == 32
    assert plugin._messages_sent == 1
    assert len(plugin._messages) == 1
    assert plugin._messages[0]["message"] == "Test emergency!"
    assert len(events_received) == 1

    plugin.stop()


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_receive_emergency(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.emergency_broadcast import EmergencyBroadcastPlugin
    import RNS.vendor.umsgpack as umsgpack

    events_received = []
    mock_app.event_bus.subscribe("emergency.received", lambda e, d: events_received.append(d))

    plugin = EmergencyBroadcastPlugin(mock_app, plugin_config)
    plugin.start()

    msg = {
        "type": "emergency",
        "id": "a" * 32,
        "origin": "test",
        "origin_name": "RemoteNode",
        "ttl": 3,
        "priority": 3,
        "message": "Incoming storm!",
        "timestamp": 1234567890.0,
    }
    payload = umsgpack.packb(msg)
    plugin.receive_emergency(b"\xaa" * 16, payload)

    assert plugin._messages_received == 1
    assert len(plugin._messages) == 1
    assert plugin._messages[0]["message"] == "Incoming storm!"
    assert len(events_received) == 1

    plugin.stop()


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_deduplication(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.emergency_broadcast import EmergencyBroadcastPlugin
    import RNS.vendor.umsgpack as umsgpack

    plugin = EmergencyBroadcastPlugin(mock_app, plugin_config)
    plugin.start()

    msg = {
        "type": "emergency",
        "id": "b" * 32,
        "origin": "test",
        "ttl": 3,
        "priority": 2,
        "message": "Test",
        "timestamp": 1234567890.0,
    }
    payload = umsgpack.packb(msg)

    plugin.receive_emergency(b"\xaa" * 16, payload)
    plugin.receive_emergency(b"\xbb" * 16, payload)  # Same ID

    assert plugin._messages_received == 1  # Only counted once
    assert len(plugin._messages) == 1

    plugin.stop()


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_ttl_decrement_rebroadcast(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.emergency_broadcast import EmergencyBroadcastPlugin
    import RNS.vendor.umsgpack as umsgpack

    plugin = EmergencyBroadcastPlugin(mock_app, plugin_config)
    plugin.start()

    # Mock the destination's announce method to capture calls
    mock_dest_instance = MagicMock()
    plugin.destination = mock_dest_instance

    msg = {
        "type": "emergency",
        "id": "c" * 32,
        "origin": "test",
        "ttl": 3,
        "priority": 3,
        "message": "Rebroadcast test",
        "timestamp": 1234567890.0,
    }
    payload = umsgpack.packb(msg)
    plugin.receive_emergency(b"\xaa" * 16, payload)

    # With rebroadcast_delay=0, should have been rebroadcast
    assert plugin._messages_rebroadcast == 1
    mock_dest_instance.announce.assert_called_once()

    # Verify TTL was decremented in the rebroadcast
    import RNS.vendor.umsgpack as umsgpack
    call_kwargs = mock_dest_instance.announce.call_args
    rebroadcast_data = umsgpack.unpackb(call_kwargs[1]["app_data"])
    assert rebroadcast_data["ttl"] == 2  # Was 3, now 2

    plugin.stop()


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_ttl_one_not_rebroadcast(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.emergency_broadcast import EmergencyBroadcastPlugin
    import RNS.vendor.umsgpack as umsgpack

    plugin = EmergencyBroadcastPlugin(mock_app, plugin_config)
    plugin.start()

    mock_dest_instance = MagicMock()
    plugin.destination = mock_dest_instance

    msg = {
        "type": "emergency",
        "id": "d" * 32,
        "origin": "test",
        "ttl": 1,  # TTL=1 means don't rebroadcast
        "priority": 3,
        "message": "No rebroadcast",
        "timestamp": 1234567890.0,
    }
    payload = umsgpack.packb(msg)
    plugin.receive_emergency(b"\xaa" * 16, payload)

    assert plugin._messages_rebroadcast == 0
    mock_dest_instance.announce.assert_not_called()

    plugin.stop()


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_receive_none_data(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.emergency_broadcast import EmergencyBroadcastPlugin

    plugin = EmergencyBroadcastPlugin(mock_app, plugin_config)
    plugin.start()
    plugin.receive_emergency(b"\xaa" * 16, None)
    assert plugin._messages_received == 0
    plugin.stop()


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_receive_invalid_data(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.emergency_broadcast import EmergencyBroadcastPlugin

    plugin = EmergencyBroadcastPlugin(mock_app, plugin_config)
    plugin.start()
    plugin.receive_emergency(b"\xaa" * 16, b"not valid msgpack \xff\xfe")
    assert plugin._messages_received == 0
    plugin.stop()


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_max_stored_messages(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.emergency_broadcast import EmergencyBroadcastPlugin

    plugin_config["max_stored_messages"] = 3
    plugin = EmergencyBroadcastPlugin(mock_app, plugin_config)
    plugin.start()

    for i in range(5):
        plugin.send_emergency(f"Message {i}", ttl=1)

    assert len(plugin._messages) == 3
    # Most recent should be first
    assert plugin._messages[0]["message"] == "Message 4"

    plugin.stop()


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_get_status(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.emergency_broadcast import EmergencyBroadcastPlugin

    plugin = EmergencyBroadcastPlugin(mock_app, plugin_config)
    plugin.start()
    status = plugin.get_status()
    assert status["active"] is True
    assert status["messages_sent"] == 0
    assert status["messages_received"] == 0
    assert status["stored_messages"] == 0
    plugin.stop()


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_get_messages(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.emergency_broadcast import EmergencyBroadcastPlugin

    plugin = EmergencyBroadcastPlugin(mock_app, plugin_config)
    plugin.start()

    plugin.send_emergency("Alert 1", ttl=1)
    plugin.send_emergency("Alert 2", ttl=1)

    msgs = plugin.get_messages(limit=1)
    assert len(msgs) == 1
    assert msgs[0]["message"] == "Alert 2"

    plugin.stop()
