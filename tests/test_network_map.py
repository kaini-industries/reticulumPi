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


# ---------------------------------------------------------------------------
# Mesh-summary cache moved off the broadcast thread (A3b)
# ---------------------------------------------------------------------------


def _quiesce_maintenance(plugin) -> None:
    """Stop the background maintenance thread (so it stops refreshing the
    summary cache) without closing the read connection that ``stop()`` would.
    """
    plugin._active = False
    plugin._join_threads(timeout=5)


@patch("RNS.Transport")
def test_broadcast_snapshot_never_scans_when_cache_populated(
    mock_transport, mock_app, plugin_config
):
    """broadcast_snapshot reads _summary_cache only — never runs the scan."""
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()
    _quiesce_maintenance(plugin)

    # Pre-populate the cache as the maintenance loop would.
    sentinel = {"total_nodes": 42}
    import time as _time

    plugin._summary_cache = (_time.monotonic(), sentinel)

    # Spy on the read connection so we can detect any inline summary scan.
    # sqlite3.Connection.execute is read-only, so wrap the whole connection.
    scan_calls = []

    class _SpyConn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args, **kwargs):
            scan_calls.append(sql)
            return self._conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    real_conn = plugin._broadcast_read_conn
    plugin._broadcast_read_conn = _SpyConn(real_conn)

    # cycle_count % 3 == 0 → want_summary is True; the cache must serve it.
    snap = plugin.broadcast_snapshot(cycle_count=0)

    assert snap is not None
    assert snap["summary"] is sentinel
    # No summary aggregation / app GROUP BY scan from the broadcast thread.
    # (The cheap indexed recent-announces seek is allowed and expected.)
    assert not any("COUNT(*) AS total" in s for s in scan_calls)
    assert not any("GROUP BY app" in s for s in scan_calls)
    real_conn.close()


@patch("RNS.Transport")
def test_broadcast_snapshot_omits_summary_when_cache_missing(
    mock_transport, mock_app, plugin_config
):
    """With no cache yet, broadcast_snapshot must not compute the scan inline."""
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()
    _quiesce_maintenance(plugin)
    plugin._summary_cache = None

    compute_calls = []
    real_compute = plugin._compute_mesh_summary

    def _spy_compute():
        compute_calls.append(True)
        return real_compute()

    plugin._compute_mesh_summary = _spy_compute

    snap = plugin.broadcast_snapshot(cycle_count=0)

    # The heavy scan was NOT run on the broadcast thread.
    assert compute_calls == []
    # Summary key gracefully omitted rather than blocking.
    assert snap is None or "summary" not in snap
    plugin._broadcast_read_conn.close()


@patch("RNS.Transport")
def test_maintenance_refresh_populates_summary_cache(mock_transport, mock_app, plugin_config):
    """_refresh_summary_cache runs the scan and warms _summary_cache."""
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    mock_transport.hops_to.return_value = 1
    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()

    plugin.record_announce(b"\x11" * 16, MagicMock(), b"node", "reticulumpi.node")
    _drain_announce_queue(plugin)
    plugin._flush_pending_upserts()
    _quiesce_maintenance(plugin)

    plugin._summary_cache = None
    plugin._refresh_summary_cache()

    assert plugin._summary_cache is not None
    stamp, summary = plugin._summary_cache
    assert summary["total_nodes"] == 1
    plugin._broadcast_read_conn.close()


@patch("RNS.Transport")
def test_get_mesh_summary_serves_cache_when_fresh(mock_transport, mock_app, plugin_config):
    """REST callers get the cached value without re-running the scan."""
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()
    _quiesce_maintenance(plugin)

    import time as _time

    sentinel = {"total_nodes": 7}
    plugin._summary_cache = (_time.monotonic(), sentinel)

    compute_calls = []
    real_compute = plugin._compute_mesh_summary

    def _spy_compute():
        compute_calls.append(True)
        return real_compute()

    plugin._compute_mesh_summary = _spy_compute

    result = plugin.get_mesh_summary()
    assert result is sentinel
    assert compute_calls == []  # served from cache, no scan
    plugin._broadcast_read_conn.close()


@patch("RNS.Transport")
def test_get_mesh_summary_computes_when_stale(mock_transport, mock_app, plugin_config):
    """A stale/missing cache forces a direct compute for REST callers."""
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()
    _quiesce_maintenance(plugin)
    plugin._summary_cache = None

    compute_calls = []
    real_compute = plugin._compute_mesh_summary

    def _spy_compute():
        compute_calls.append(True)
        return real_compute()

    plugin._compute_mesh_summary = _spy_compute

    result = plugin.get_mesh_summary()
    assert compute_calls == [True]
    assert "total_nodes" in result
    # The fresh result is now cached.
    assert plugin._summary_cache is not None
    plugin._broadcast_read_conn.close()


# ---------------------------------------------------------------------------
# WAL hygiene (A4)
# ---------------------------------------------------------------------------


@patch("RNS.Transport")
def test_prune_checkpoints_wal(mock_transport, mock_app, plugin_config):
    """The hourly prune issues PRAGMA wal_checkpoint(TRUNCATE) after deletes."""
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()

    # sqlite3.Connection.execute is a read-only C slot, so we cannot patch it
    # directly.  Instead, swap the entire _write_conn with a thin wrapper that
    # delegates to the real connection but records every SQL statement.
    executed_sql = []
    real_conn = plugin._write_conn

    class _RecordingConn:
        """Proxy that records execute() calls, forwarding to the real conn."""

        def execute(self, sql, *args, **kwargs):
            executed_sql.append(sql)
            return real_conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(real_conn, name)

    plugin._write_conn = _RecordingConn()
    try:
        plugin._prune_old_data()
    finally:
        plugin._write_conn = real_conn

    assert any("wal_checkpoint(TRUNCATE)" in s for s in executed_sql)
    # The deletes still ran first.
    assert any("DELETE FROM known_nodes" in s for s in executed_sql)
    plugin.stop()


@patch("RNS.Transport")
def test_init_db_sets_incremental_auto_vacuum(mock_transport, mock_app, plugin_config):
    """New DBs are created with auto_vacuum=INCREMENTAL (mode 2)."""
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()
    plugin.stop()

    with sqlite3.connect(plugin_config["db_path"]) as conn:
        (mode,) = conn.execute("PRAGMA auto_vacuum").fetchone()
    # 2 == INCREMENTAL
    assert mode == 2


# ---------------------------------------------------------------------------
# Connection contention regression (split read connections)
# ---------------------------------------------------------------------------


@patch("RNS.Transport")
def test_broadcast_does_not_block_on_maintenance(mock_transport, mock_app, plugin_config):
    """get_recent_announces() must complete fast even when _maintenance_read_conn is held."""
    import threading
    import time as _time

    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    mock_transport.hops_to.return_value = 1
    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()
    _quiesce_maintenance(plugin)

    # Seed a node directly in the DB so the query has something to return.
    # We cannot use record_announce + _drain_announce_queue here because
    # _quiesce_maintenance already stopped the announce worker thread.
    import time as _time2

    with sqlite3.connect(plugin_config["db_path"]) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO known_nodes "
            "(destination_hash, app_name, aspects, hops, last_seen, first_seen, announce_count, app_data_str) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("22" * 16, "reticulumpi", "node", 1, _time2.time(), _time2.time(), 1, "test"),
        )

    # Hold the maintenance connection busy for 2 seconds (simulates the
    # heavy _compute_mesh_summary scan on slow SD I/O).
    held = threading.Event()
    release = threading.Event()

    def _hold_maintenance():
        with plugin._maintenance_conn_lock:
            held.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=_hold_maintenance, daemon=True)
    holder.start()
    held.wait(timeout=2)

    # get_recent_announces uses _broadcast_read_conn — must NOT block.
    start = _time.monotonic()
    result = plugin.get_recent_announces()
    elapsed_ms = (_time.monotonic() - start) * 1000

    release.set()
    holder.join(timeout=2)

    assert elapsed_ms < 50, f"get_recent_announces blocked for {elapsed_ms:.0f}ms"
    assert len(result) >= 1
    plugin._broadcast_read_conn.close()
    plugin._maintenance_read_conn.close()


@patch("RNS.Transport")
def test_interface_stats_capped(mock_transport, mock_app, plugin_config):
    """_save_interface_stats trims rows when count exceeds max_stats_rows."""
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    plugin_config["max_stats_rows"] = 1000
    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()
    _quiesce_maintenance(plugin)

    # Seed 1500 rows directly into the DB (exceeds max_stats_rows=1000).
    with sqlite3.connect(plugin_config["db_path"]) as conn:
        for i in range(1500):
            conn.execute(
                "INSERT INTO interface_stats (timestamp, name, type, online) VALUES (?, ?, ?, ?)",
                (float(i), f"iface_{i}", "TCPInterface", 1),
            )

    # Force the trim check (every 10th save cycle).
    # Provide a dummy interface so get_interface_stats() returns non-empty
    # and the method doesn't bail before reaching the trim logic.
    plugin._stats_save_count = 9
    dummy_iface = MagicMock()
    dummy_iface.__str__ = lambda s: "DummyInterface"
    dummy_iface.online = True
    mock_transport.interfaces = [dummy_iface]
    plugin._save_interface_stats()

    with sqlite3.connect(plugin_config["db_path"]) as conn:
        (count,) = conn.execute("SELECT COUNT(*) FROM interface_stats").fetchone()
    assert count <= 1000, f"Expected <= 1000 rows after trim, got {count}"
    plugin._broadcast_read_conn.close()
    plugin._maintenance_read_conn.close()


@patch("RNS.Transport")
def test_no_shared_read_lock(mock_transport, mock_app, plugin_config):
    """Verify the old _read_db_lock and _read_conn attributes are gone."""
    from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin

    plugin = NetworkMapPlugin(mock_app, plugin_config)
    plugin.start()

    assert not hasattr(plugin, "_read_db_lock"), "_read_db_lock should be removed"
    assert not hasattr(plugin, "_read_conn"), "_read_conn should be replaced by split connections"
    assert hasattr(plugin, "_broadcast_read_conn")
    assert hasattr(plugin, "_maintenance_read_conn")
    assert hasattr(plugin, "_maintenance_conn_lock")

    plugin.stop()
