"""Tests for the NetworkMap plugin."""

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
        "db_path": str(tmp_path / "network_map.db"),
        "max_history_days": 30,
    }


@patch("RNS.Transport")
def test_network_map_start_stop(mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()
    assert plugin._active is True
    assert len(plugin._known_nodes) == 0
    plugin.stop()
    assert plugin._active is False


@patch("RNS.Transport")
def test_record_announce_new_node(mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    mock_transport.hops_to.return_value = 3
    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()

    events_received = []
    mock_app.event_bus.subscribe("node.discovered", lambda e, d: events_received.append(d))

    dest_hash = b"\xaa" * 16
    plugin.record_announce(dest_hash, MagicMock(), b"test data", "reticulumpi.node.heartbeat")

    assert dest_hash in plugin._known_nodes
    assert plugin._known_nodes[dest_hash]["hops"] == 3
    assert plugin._known_nodes[dest_hash]["announce_count"] == 1
    assert len(events_received) == 1

    plugin.stop()


@patch("RNS.Transport")
def test_record_announce_existing_node(mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    mock_transport.hops_to.return_value = 2
    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()

    dest_hash = b"\xbb" * 16
    plugin.record_announce(dest_hash, MagicMock(), b"data1", "app.test")
    plugin.record_announce(dest_hash, MagicMock(), b"data2", "app.test")

    assert plugin._known_nodes[dest_hash]["announce_count"] == 2
    plugin.stop()


@patch("RNS.Transport")
def test_get_known_nodes(mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    mock_transport.hops_to.return_value = 1
    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()

    plugin.record_announce(b"\xcc" * 16, MagicMock(), b"node1", "reticulumpi.node")
    plugin.record_announce(b"\xdd" * 16, MagicMock(), b"node2", "reticulumpi.node")

    nodes = plugin.get_known_nodes()
    assert len(nodes) == 2
    assert all("destination_hash" in n for n in nodes)
    plugin.stop()


@patch("RNS.Transport")
def test_sqlite_persistence(mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    mock_transport.hops_to.return_value = 5
    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()

    dest_hash = b"\xee" * 16
    plugin.record_announce(dest_hash, MagicMock(), b"persist", "app.test")
    plugin.stop()

    # Verify data was written to SQLite
    with sqlite3.connect(plugin_config["db_path"]) as conn:
        conn.row_factory = sqlite3.Row
        rows = list(conn.execute("SELECT * FROM known_nodes"))
    assert len(rows) == 1
    assert rows[0]["destination_hash"] == dest_hash.hex()


@patch("RNS.Transport")
def test_load_from_db(mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    mock_transport.hops_to.return_value = 1

    # First instance: store data
    p1 = NetworkMapPlugin(mock_app, plugin_config)
    p1.start()
    p1.record_announce(b"\xff" * 16, MagicMock(), b"data", "test.app")
    p1.stop()

    # Second instance: should load from DB
    p2 = NetworkMapPlugin(mock_app, plugin_config)
    p2.start()
    assert len(p2._known_nodes) == 1
    p2.stop()


def test_validate_config_bad_max_history(mock_app):
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    with pytest.raises(ValueError, match="max_history_days"):
        NetworkMapPlugin(mock_app, {"max_history_days": 0})


@patch("RNS.Transport")
def test_get_status(mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()
    status = plugin.get_status()
    assert status["active"] is True
    assert status["known_nodes"] == 0
    plugin.stop()
