"""Tests for the MeshTelemetry plugin."""

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
    app.get_plugin.return_value = None
    return app


@pytest.fixture
def plugin_config():
    return {
        "enabled": True,
        "announce_interval": 60,
        "include_metrics": ["cpu_percent", "cpu_temp"],
    }


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_start_stop(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.mesh_telemetry import MeshTelemetryPlugin

    plugin = MeshTelemetryPlugin(mock_app, plugin_config)
    plugin.start()
    assert plugin._active is True
    assert len(plugin._peer_metrics) == 0
    plugin.stop()
    assert plugin._active is False


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_record_peer_metrics_umsgpack(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.mesh_telemetry import MeshTelemetryPlugin
    import RNS.vendor.umsgpack as umsgpack

    mock_transport.hops_to.return_value = 2
    plugin = MeshTelemetryPlugin(mock_app, plugin_config)
    plugin.start()

    events_received = []
    mock_app.event_bus.subscribe("node.metrics_received", lambda e, d: events_received.append(d))

    dest_hash = b"\xaa" * 16
    payload = umsgpack.packb({"name": "RemoteNode", "cpu": 45.2, "temp": 55.0, "mem": 60.1})
    plugin.record_peer_metrics(dest_hash, payload)

    assert dest_hash in plugin._peer_metrics
    assert plugin._peer_metrics[dest_hash]["name"] == "RemoteNode"
    assert plugin._peer_metrics[dest_hash]["cpu"] == 45.2
    assert plugin._peer_metrics[dest_hash]["hops"] == 2
    assert len(events_received) == 1

    plugin.stop()


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_record_peer_metrics_utf8_fallback(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.mesh_telemetry import MeshTelemetryPlugin

    mock_transport.hops_to.return_value = 1
    plugin = MeshTelemetryPlugin(mock_app, plugin_config)
    plugin.start()

    dest_hash = b"\xbb" * 16
    plugin.record_peer_metrics(dest_hash, b"hostname|cpu:50%|mem:70%")

    assert dest_hash in plugin._peer_metrics
    assert "raw" in plugin._peer_metrics[dest_hash]
    plugin.stop()


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_record_peer_metrics_none_data(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.mesh_telemetry import MeshTelemetryPlugin

    plugin = MeshTelemetryPlugin(mock_app, plugin_config)
    plugin.start()
    plugin.record_peer_metrics(b"\xcc" * 16, None)
    assert len(plugin._peer_metrics) == 0
    plugin.stop()


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_get_peer_metrics(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.mesh_telemetry import MeshTelemetryPlugin
    import RNS.vendor.umsgpack as umsgpack

    mock_transport.hops_to.return_value = 1
    plugin = MeshTelemetryPlugin(mock_app, plugin_config)
    plugin.start()

    plugin.record_peer_metrics(b"\xdd" * 16, umsgpack.packb({"name": "Node1", "cpu": 30.0}))
    plugin.record_peer_metrics(b"\xee" * 16, umsgpack.packb({"name": "Node2", "cpu": 70.0}))

    peers = plugin.get_peer_metrics()
    assert len(peers) == 2
    assert all("destination_hash" in p for p in peers)
    plugin.stop()


def test_validate_config_bad_interval(mock_app):
    from reticulumpi.builtin_plugins.mesh_telemetry import MeshTelemetryPlugin

    with pytest.raises(ValueError, match="announce_interval"):
        MeshTelemetryPlugin(mock_app, {"announce_interval": 5})


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_get_status(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.mesh_telemetry import MeshTelemetryPlugin

    plugin = MeshTelemetryPlugin(mock_app, plugin_config)
    plugin.start()
    status = plugin.get_status()
    assert status["active"] is True
    assert status["peer_count"] == 0
    plugin.stop()
