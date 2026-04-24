"""Tests for the NetworkMap plugin."""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi.event_bus import EventBus


def _drain_announce_queue(plugin, timeout: float = 5.0) -> None:
    """Wait until the plugin's announce queue is fully processed.

    Uses ``Queue.join()`` which blocks until every item that was ``get()``-ed
    has had ``task_done()`` called — eliminating the race between dequeue and
    processing that the old polling approach was susceptible to.
    """
    import threading

    done = threading.Event()

    def _wait() -> None:
        plugin._announce_queue.join()
        done.set()

    waiter = threading.Thread(target=_wait, daemon=True)
    waiter.start()
    if not done.wait(timeout):
        raise TimeoutError("Announce queue did not drain within timeout")


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
    _drain_announce_queue(plugin)

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
    _drain_announce_queue(plugin)

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
    _drain_announce_queue(plugin)

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
    _drain_announce_queue(plugin)
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
    _drain_announce_queue(p1)
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


@patch("RNS.Transport")
def test_get_node_name_hit(mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()
    dest_hash = b"\xaa" * 16
    with plugin._nodes_lock:
        plugin._known_nodes[dest_hash] = {"app_data_str": "Alice"}

    assert plugin.get_node_name(dest_hash.hex()) == "Alice"
    plugin.stop()


@patch("RNS.Transport")
def test_get_node_name_accepts_angle_wrapped_hex(mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()
    dest_hash = b"\xbb" * 16
    with plugin._nodes_lock:
        plugin._known_nodes[dest_hash] = {"app_data_str": "Bob"}

    assert plugin.get_node_name(f"<{dest_hash.hex()}>") == "Bob"
    plugin.stop()


@patch("RNS.Transport")
def test_get_node_name_miss(mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()
    assert plugin.get_node_name("cc" * 16) is None
    plugin.stop()


@patch("RNS.Transport")
def test_get_node_name_malformed_hex(mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()
    assert plugin.get_node_name("not-hex") is None
    assert plugin.get_node_name(None) is None
    plugin.stop()


@patch("RNS.Transport")
def test_get_node_name_empty_app_data(mock_transport, mock_app, plugin_config):
    """A known node with no announced name should return None, not ''."""
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()
    dest_hash = b"\xdd" * 16
    with plugin._nodes_lock:
        plugin._known_nodes[dest_hash] = {"app_data_str": ""}

    assert plugin.get_node_name(dest_hash.hex()) is None
    plugin.stop()


@patch("RNS.Transport")
def test_record_announce_parses_lxmf_v050_list_format(mock_transport, mock_app, plugin_config):
    """LXMF 0.5+ packs announces as msgpack list [display_name_bytes, stamp_cost]."""
    import RNS.vendor.umsgpack as umsgpack

    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    mock_transport.hops_to.return_value = 1
    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()

    dest_hash = b"\x11" * 16
    app_data = umsgpack.packb([b"Meshchat User", 8])
    plugin.record_announce(dest_hash, MagicMock(), app_data, "lxmf.delivery")
    _drain_announce_queue(plugin)

    assert plugin._known_nodes[dest_hash]["app_data_str"] == "Meshchat User"
    plugin.stop()


@patch("RNS.Transport")
def test_record_announce_lxmf_list_with_none_name(mock_transport, mock_app, plugin_config):
    """LXMF announce with no display_name set packs [None, stamp_cost] — must not crash."""
    import RNS.vendor.umsgpack as umsgpack

    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    mock_transport.hops_to.return_value = 1
    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()

    dest_hash = b"\x22" * 16
    app_data = umsgpack.packb([None, None])
    plugin.record_announce(dest_hash, MagicMock(), app_data, "lxmf.delivery")
    _drain_announce_queue(plugin)

    assert plugin._known_nodes[dest_hash]["app_data_str"] == ""
    plugin.stop()


# ---------------------------------------------------------------------------
# SQL defensive hardening
# ---------------------------------------------------------------------------


@patch("RNS.Transport")
def test_paginated_rejects_invalid_sort_order(mock_transport, mock_app, plugin_config):
    """Bogus order value falls back to 'desc'."""
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    mock_transport.hops_to.return_value = 1
    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()

    dest_hash = b"\x33" * 16
    plugin.record_announce(dest_hash, MagicMock(), b"test", "test.app")
    _drain_announce_queue(plugin)
    plugin._flush_pending_upserts()

    result = plugin.get_known_nodes_paginated(order="DROP TABLE")
    assert result["total"] == 1
    assert len(result["nodes"]) == 1
    plugin.stop()


@patch("RNS.Transport")
def test_paginated_search_escapes_like_wildcards(mock_transport, mock_app, plugin_config):
    """Search term containing '%' should not match everything."""
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    mock_transport.hops_to.return_value = 1
    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()

    dest1 = b"\x44" * 16
    dest2 = b"\x55" * 16
    plugin.record_announce(dest1, MagicMock(), b"alpha", "test.app")
    plugin.record_announce(dest2, MagicMock(), b"beta", "test.app")
    _drain_announce_queue(plugin)
    plugin._flush_pending_upserts()

    result = plugin.get_known_nodes_paginated(search="%")
    assert result["total"] == 0
    plugin.stop()


@patch("RNS.Transport")
def test_paginated_sort_col_falls_back_on_unknown(mock_transport, mock_app, plugin_config):
    """Unknown sort column defaults to 'last_seen'."""
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    mock_transport.hops_to.return_value = 1
    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()

    dest_hash = b"\x66" * 16
    plugin.record_announce(dest_hash, MagicMock(), b"test", "test.app")
    _drain_announce_queue(plugin)
    plugin._flush_pending_upserts()

    result = plugin.get_known_nodes_paginated(sort="nonexistent_column")
    assert result["total"] == 1
    assert len(result["nodes"]) == 1
    plugin.stop()
