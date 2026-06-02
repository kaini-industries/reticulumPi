"""Tests for the transport_health plugin."""

from __future__ import annotations

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
    app.get_plugin = MagicMock(return_value=None)
    return app


@pytest.fixture
def base_config(tmp_path):
    return {
        "check_interval": 60,
        "db_path": str(tmp_path / "transport_health.db"),
        "history_retention_hours": 168,
        "down_threshold_checks": 3,
        "degraded_threshold_pct": 80,
        "alert_on_critical_down": True,
        "critical_path_count": 5,
    }


def _make_plugin(mock_app, config):
    from reticulumpi.builtin_plugins.transport_health import TransportHealthPlugin

    with patch("RNS.Transport"):
        return TransportHealthPlugin(mock_app, config)


def _mock_routing_data(via_entries):
    """Create a mock connectivity_monitor that returns given via entries.

    via_entries: list of (via_hash_hex, interface_name) tuples
    """
    paths = []
    for via_hex, iface in via_entries:
        paths.append({"hash": "ab" * 16, "via": via_hex, "interface": iface, "hops": 2})
    mock = MagicMock()
    mock.get_routing_data.return_value = {"paths": paths}
    return mock


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestValidateConfig:
    @patch("RNS.Transport")
    def test_valid_config(self, mock_transport, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.validate_config()

    @patch("RNS.Transport")
    def test_invalid_interval(self, mock_transport, mock_app, base_config):
        from reticulumpi.builtin_plugins.transport_health import TransportHealthPlugin

        base_config["check_interval"] = 5
        with pytest.raises(ValueError, match="check_interval"):
            TransportHealthPlugin(mock_app, base_config)

    @patch("RNS.Transport")
    def test_invalid_down_threshold(self, mock_transport, mock_app, base_config):
        from reticulumpi.builtin_plugins.transport_health import TransportHealthPlugin

        base_config["down_threshold_checks"] = 0
        with pytest.raises(ValueError, match="down_threshold_checks"):
            TransportHealthPlugin(mock_app, base_config)

    @patch("RNS.Transport")
    def test_default_config(self, mock_transport, mock_app, tmp_path):
        """Missing config keys use defaults without error."""
        plugin = _make_plugin(mock_app, {"db_path": str(tmp_path / "th.db")})
        plugin.validate_config()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @patch("RNS.Transport")
    def test_start_stop(self, mock_transport, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        assert plugin._active is True
        assert len(plugin._threads) == 1
        plugin.stop()
        assert plugin._active is False

    @patch("RNS.Transport")
    def test_get_status(self, mock_transport, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        status = plugin.get_status()
        assert status["active"] is True
        assert status["transport_nodes_tracked"] == 0
        assert status["healthy"] == 0
        plugin.stop()

    @patch("RNS.Transport")
    def test_get_transport_summary(self, mock_transport, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        summary = plugin.get_transport_summary()
        assert "total" in summary
        assert "healthy" in summary
        assert summary["total"] == 0
        plugin.stop()


# ---------------------------------------------------------------------------
# Transport node detection
# ---------------------------------------------------------------------------


class TestNodeDetection:
    @patch("RNS.Transport")
    def test_discover_transport_node(self, mock_transport, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        via_hash = "cc" * 16
        mock_conn = _mock_routing_data([(via_hash, "TCPInterface")])
        mock_app.get_plugin.return_value = mock_conn

        events_received = []
        mock_app.event_bus.subscribe(
            "transport_node.discovered", lambda e, d: events_received.append(d)
        )

        plugin._run_check()

        assert via_hash in plugin._transport_nodes
        record = plugin._transport_nodes[via_hash]
        assert record["status"] == "new"
        assert record["paths_via"] == 1
        assert len(events_received) == 1
        plugin.stop()

    @patch("RNS.Transport")
    def test_ignore_direct_paths(self, mock_transport, mock_app, base_config):
        """Paths with via='0000...' (direct) are not transport nodes."""
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        zero_via = "00" * 16
        mock_conn = _mock_routing_data([(zero_via, "AutoInterface")])
        mock_app.get_plugin.return_value = mock_conn

        plugin._run_check()

        assert len(plugin._transport_nodes) == 0
        plugin.stop()

    @patch("RNS.Transport")
    def test_multiple_paths_via_same_node(self, mock_transport, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        via_hash = "dd" * 16
        mock_conn = _mock_routing_data(
            [
                (via_hash, "TCPInterface"),
                (via_hash, "TCPInterface"),
                (via_hash, "TCPInterface"),
            ]
        )
        mock_app.get_plugin.return_value = mock_conn

        plugin._run_check()

        assert plugin._transport_nodes[via_hash]["paths_via"] == 3
        plugin.stop()


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------


class TestStatusTransitions:
    @patch("RNS.Transport")
    def test_new_to_healthy(self, mock_transport, mock_app, base_config):
        """After enough checks, a present node transitions from 'new' to 'healthy'."""
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        via_hash = "ee" * 16
        mock_conn = _mock_routing_data([(via_hash, "TCPInterface")])
        mock_app.get_plugin.return_value = mock_conn

        # Run 4 checks (threshold for "new" is 3)
        for _ in range(4):
            plugin._run_check()

        assert plugin._transport_nodes[via_hash]["status"] == "healthy"
        plugin.stop()

    @patch("RNS.Transport")
    def test_healthy_to_down(self, mock_transport, mock_app, base_config):
        """Node disappearing for down_threshold_checks becomes 'down'."""
        base_config["down_threshold_checks"] = 2
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        via_hash = "ff" * 16

        # First: establish the node as healthy (4 checks present)
        mock_conn = _mock_routing_data([(via_hash, "TCPInterface")])
        mock_app.get_plugin.return_value = mock_conn
        for _ in range(4):
            plugin._run_check()
        assert plugin._transport_nodes[via_hash]["status"] == "healthy"

        # Now: node disappears
        mock_conn_empty = _mock_routing_data([])
        mock_app.get_plugin.return_value = mock_conn_empty

        events_received = []
        mock_app.event_bus.subscribe("transport_node.down", lambda e, d: events_received.append(d))

        # Run enough checks to trigger "down"
        for _ in range(2):
            plugin._run_check()

        assert plugin._transport_nodes[via_hash]["status"] == "down"
        assert len(events_received) == 1
        assert events_received[0]["hash"] == via_hash
        plugin.stop()

    @patch("RNS.Transport")
    def test_down_to_recovered(self, mock_transport, mock_app, base_config):
        """Node reappearing after being down publishes recovery event."""
        base_config["down_threshold_checks"] = 2
        base_config["degraded_threshold_pct"] = 50  # low threshold so recovery = healthy
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        via_hash = "ab" * 16

        # Establish as healthy (4 checks present)
        mock_conn = _mock_routing_data([(via_hash, "TCPInterface")])
        mock_app.get_plugin.return_value = mock_conn
        for _ in range(4):
            plugin._run_check()

        # Make it go down (2 checks absent)
        mock_app.get_plugin.return_value = _mock_routing_data([])
        for _ in range(2):
            plugin._run_check()
        assert plugin._transport_nodes[via_hash]["status"] == "down"

        # Recover
        events_received = []
        mock_app.event_bus.subscribe(
            "transport_node.recovered", lambda e, d: events_received.append(d)
        )

        mock_app.get_plugin.return_value = mock_conn
        plugin._run_check()

        # After 4 present + 2 absent + 1 present = 5/7 ≈ 71% availability
        # With degraded_threshold_pct=50 this is healthy
        assert plugin._transport_nodes[via_hash]["status"] == "healthy"
        assert len(events_received) == 1
        plugin.stop()

    @patch("RNS.Transport")
    def test_degraded_status(self, mock_transport, mock_app, base_config):
        """Node with low availability becomes 'degraded'."""
        base_config["degraded_threshold_pct"] = 80
        base_config["down_threshold_checks"] = 10  # high so we don't hit "down"
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        via_hash = "ac" * 16

        # 4 checks present (to get past "new" status)
        mock_conn = _mock_routing_data([(via_hash, "TCPInterface")])
        mock_app.get_plugin.return_value = mock_conn
        for _ in range(4):
            plugin._run_check()

        # Now alternate: mostly absent to bring availability below 80%
        mock_empty = _mock_routing_data([])
        for _ in range(10):
            mock_app.get_plugin.return_value = mock_empty
            plugin._run_check()
            mock_app.get_plugin.return_value = mock_conn
            plugin._run_check()

        # 4 present out of first 4, then 10 present out of next 20 = 14/24 = 58%
        record = plugin._transport_nodes[via_hash]
        assert record["availability_pct"] < 80
        assert record["status"] == "degraded"
        plugin.stop()


# ---------------------------------------------------------------------------
# Status classification
# ---------------------------------------------------------------------------


class TestClassifyStatus:
    def test_new_within_threshold(self):
        from reticulumpi.builtin_plugins.transport_health import TransportHealthPlugin

        record = {"total_checks": 2, "consecutive_absent": 0, "availability_pct": 100}
        assert TransportHealthPlugin._classify_status(record, 3, 80) == "new"

    def test_down_after_threshold(self):
        from reticulumpi.builtin_plugins.transport_health import TransportHealthPlugin

        record = {"total_checks": 10, "consecutive_absent": 5, "availability_pct": 50}
        assert TransportHealthPlugin._classify_status(record, 3, 80) == "down"

    def test_degraded(self):
        from reticulumpi.builtin_plugins.transport_health import TransportHealthPlugin

        record = {"total_checks": 10, "consecutive_absent": 1, "availability_pct": 60}
        assert TransportHealthPlugin._classify_status(record, 3, 80) == "degraded"

    def test_healthy(self):
        from reticulumpi.builtin_plugins.transport_health import TransportHealthPlugin

        record = {"total_checks": 10, "consecutive_absent": 0, "availability_pct": 95}
        assert TransportHealthPlugin._classify_status(record, 3, 80) == "healthy"


# ---------------------------------------------------------------------------
# Dashboard integration
# ---------------------------------------------------------------------------


class TestDashboardData:
    @patch("RNS.Transport")
    def test_get_transport_nodes_sorted(self, mock_transport, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        # Inject two nodes manually
        plugin._transport_nodes["aa" * 16] = {
            "hash": "aa" * 16,
            "paths_via": 5,
            "status": "healthy",
        }
        plugin._transport_nodes["bb" * 16] = {
            "hash": "bb" * 16,
            "paths_via": 10,
            "status": "healthy",
        }

        nodes = plugin.get_transport_nodes()
        assert len(nodes) == 2
        # Sorted by paths_via descending
        assert nodes[0]["paths_via"] == 10
        assert nodes[1]["paths_via"] == 5
        plugin.stop()

    @patch("RNS.Transport")
    def test_summary_counts(self, mock_transport, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        plugin._transport_nodes["a"] = {"status": "healthy", "paths_via": 3}
        plugin._transport_nodes["b"] = {"status": "healthy", "paths_via": 2}
        plugin._transport_nodes["c"] = {"status": "down", "paths_via": 0}
        plugin._transport_nodes["d"] = {"status": "degraded", "paths_via": 1}
        plugin._transport_nodes["e"] = {"status": "new", "paths_via": 1}

        summary = plugin.get_transport_summary()
        assert summary["total"] == 5
        assert summary["healthy"] == 2
        assert summary["down"] == 1
        assert summary["degraded"] == 1
        assert summary["new"] == 1
        assert summary["total_paths_relayed"] == 7
        plugin.stop()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    @patch("RNS.Transport")
    def test_persist_and_reload(self, mock_transport, mock_app, base_config):
        from reticulumpi.builtin_plugins.transport_health import TransportHealthPlugin

        # Create and populate plugin
        plugin = TransportHealthPlugin(mock_app, base_config)
        plugin.start()

        via_hash = "dd" * 16
        mock_conn = _mock_routing_data([(via_hash, "TCPInterface")])
        mock_app.get_plugin.return_value = mock_conn
        plugin._run_check()
        assert via_hash in plugin._transport_nodes

        plugin.stop()

        # Create a new plugin instance with the same db
        plugin2 = TransportHealthPlugin(mock_app, base_config)
        plugin2.start()
        assert via_hash in plugin2._transport_nodes
        assert plugin2._transport_nodes[via_hash]["paths_via"] == 1
        plugin2.stop()
