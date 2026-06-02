"""Tests for the path_warmer plugin."""

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
def base_config():
    return {
        "warm_interval": 120,
        "max_requests_per_cycle": 10,
        "path_age_threshold": 1200,
        "pre_send_timeout": 2,
        "request_timeout": 2,
        "warm_recently_seen": True,
        "warm_recent_hours": 24,
        "priority_nodes": [],
    }


def _make_plugin(mock_app, config):
    from reticulumpi.builtin_plugins.path_warmer import PathWarmerPlugin

    with patch("RNS.Transport"):
        return PathWarmerPlugin(mock_app, config)


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
        from reticulumpi.builtin_plugins.path_warmer import PathWarmerPlugin

        base_config["warm_interval"] = 5
        with pytest.raises(ValueError, match="warm_interval"):
            PathWarmerPlugin(mock_app, base_config)

    @patch("RNS.Transport")
    def test_invalid_max_requests(self, mock_transport, mock_app, base_config):
        from reticulumpi.builtin_plugins.path_warmer import PathWarmerPlugin

        base_config["max_requests_per_cycle"] = 0
        with pytest.raises(ValueError, match="max_requests_per_cycle"):
            PathWarmerPlugin(mock_app, base_config)

    @patch("RNS.Transport")
    def test_default_config(self, mock_transport, mock_app):
        """Missing config keys use defaults without error."""
        plugin = _make_plugin(mock_app, {})
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
        assert status["paths_warmed"] == 0
        assert status["paths_failed"] == 0
        assert status["priority_nodes"] == 0
        plugin.stop()

    @patch("RNS.Transport")
    def test_get_warming_stats(self, mock_transport, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        stats = plugin.get_warming_stats()
        assert "warm_interval" in stats
        assert "max_requests_per_cycle" in stats
        plugin.stop()


# ---------------------------------------------------------------------------
# Priority nodes parsing
# ---------------------------------------------------------------------------


class TestPriorityNodes:
    @patch("RNS.Transport")
    def test_valid_priority_nodes(self, mock_transport, mock_app, base_config):
        base_config["priority_nodes"] = ["aa" * 16, "bb" * 16]
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        assert len(plugin._priority_hashes) == 2
        assert plugin._priority_hashes[0] == b"\xaa" * 16
        plugin.stop()

    @patch("RNS.Transport")
    def test_invalid_priority_nodes_skipped(self, mock_transport, mock_app, base_config):
        base_config["priority_nodes"] = ["not_hex", "bb" * 16]
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        assert len(plugin._priority_hashes) == 1
        plugin.stop()


# ---------------------------------------------------------------------------
# ensure_path
# ---------------------------------------------------------------------------


class TestEnsurePath:
    @patch("RNS.Transport")
    def test_path_already_exists(self, mock_transport, mock_app, base_config):
        mock_transport.has_path.return_value = True
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        result = plugin.ensure_path(b"\xaa" * 16)
        assert result is True
        # Should not request a path if one already exists
        mock_transport.request_path.assert_not_called()
        plugin.stop()

    @patch("RNS.Transport")
    def test_path_requested_and_found(self, mock_transport, mock_app, base_config):
        # First call returns False, subsequent calls return True
        mock_transport.has_path.side_effect = [False, True]
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        events_received = []
        mock_app.event_bus.subscribe("path.warmed", lambda e, d: events_received.append(d))

        result = plugin.ensure_path(b"\xaa" * 16, timeout=2)
        assert result is True
        mock_transport.request_path.assert_called_once()
        assert len(events_received) == 1
        assert events_received[0]["source"] == "ensure_path"
        plugin.stop()

    @patch("RNS.Transport")
    def test_path_request_timeout(self, mock_transport, mock_app, base_config):
        mock_transport.has_path.return_value = False
        base_config["pre_send_timeout"] = 1
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        events_received = []
        mock_app.event_bus.subscribe("path.warm_failed", lambda e, d: events_received.append(d))

        result = plugin.ensure_path(b"\xaa" * 16, timeout=1)
        assert result is False
        assert len(events_received) == 1
        plugin.stop()


# ---------------------------------------------------------------------------
# Warming cycle logic
# ---------------------------------------------------------------------------


class TestWarmCycle:
    @patch("RNS.Transport")
    def test_warm_node_already_has_path(self, mock_transport, mock_app, base_config):
        mock_transport.has_path.return_value = True
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        result = plugin._warm_node(b"\xaa" * 16)
        assert result is False  # not counted as "warmed" since already reachable
        plugin.stop()

    @patch("RNS.Transport")
    def test_warm_node_success(self, mock_transport, mock_app, base_config):
        mock_transport.has_path.side_effect = [False, True]
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        result = plugin._warm_node(b"\xaa" * 16)
        assert result is True
        assert plugin._paths_warmed == 1
        plugin.stop()

    @patch("RNS.Transport")
    def test_warm_node_failure(self, mock_transport, mock_app, base_config):
        mock_transport.has_path.return_value = False
        base_config["request_timeout"] = 1
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        result = plugin._warm_node(b"\xaa" * 16)
        assert result is False
        assert plugin._paths_failed == 1
        plugin.stop()

    @patch("RNS.Transport")
    def test_max_requests_respected(self, mock_transport, mock_app, base_config):
        """Warming cycle respects max_requests_per_cycle."""
        base_config["max_requests_per_cycle"] = 2
        base_config["request_timeout"] = 1
        base_config["priority_nodes"] = [("aa" * 16), ("bb" * 16), ("cc" * 16)]

        mock_transport.has_path.return_value = False
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        # Mock no connectivity_monitor
        mock_app.get_plugin.return_value = None

        plugin._run_warm_cycle()
        # Should have attempted at most 2
        total = plugin._paths_warmed + plugin._paths_failed
        assert total <= 2
        plugin.stop()

    @patch("RNS.Transport")
    def test_build_candidates_priority_first(self, mock_transport, mock_app, base_config):
        """Priority nodes appear first in candidates."""
        base_config["priority_nodes"] = ["aa" * 16]
        base_config["warm_recently_seen"] = False

        mock_transport.has_path.return_value = False
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        candidates = plugin._build_candidates({}, 1200)
        assert len(candidates) == 1
        assert candidates[0] == b"\xaa" * 16
        plugin.stop()


# ---------------------------------------------------------------------------
# Cycle event publishing
# ---------------------------------------------------------------------------


class TestEventPublishing:
    @patch("RNS.Transport")
    def test_warming_cycle_event(self, mock_transport, mock_app, base_config):
        mock_transport.has_path.return_value = True  # all paths exist
        base_config["priority_nodes"] = []
        base_config["warm_recently_seen"] = False
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        events_received = []
        mock_app.event_bus.subscribe("path.warming_cycle", lambda e, d: events_received.append(d))

        mock_app.get_plugin.return_value = None
        plugin._run_warm_cycle()

        assert len(events_received) == 1
        assert "warmed" in events_received[0]
        assert "candidates" in events_received[0]
        plugin.stop()
