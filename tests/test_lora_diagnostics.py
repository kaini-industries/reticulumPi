"""Tests for the lora_diagnostics plugin."""

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
        "lora_interface_name": "RNode LoRa Interface",
        "monitor_interval": 30,
        "beacon_interval": 120,
        "monitored_destinations": [
            {"hash": "c99eced76cb1bbc2e6711b6fbea115eb", "name": "Ratcom"},
            {"hash": "611ed890ce0b13ab0a581563ffd044c0", "name": "MeshChat"},
        ],
    }


def _make_plugin(mock_app, config):
    from reticulumpi.builtin_plugins.lora_diagnostics import LoRaDiagnosticsPlugin

    with patch("RNS.Transport"):
        return LoRaDiagnosticsPlugin(mock_app, config)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestValidateConfig:
    @patch("RNS.Transport")
    def test_valid_config(self, mock_transport, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.validate_config()

    @patch("RNS.Transport")
    def test_default_config(self, mock_transport, mock_app):
        """Empty config uses defaults without error."""
        plugin = _make_plugin(mock_app, {})
        plugin.validate_config()

    @patch("RNS.Transport")
    def test_invalid_monitor_interval(self, mock_transport, mock_app, base_config):
        from reticulumpi.builtin_plugins.lora_diagnostics import (
            LoRaDiagnosticsPlugin,
        )

        base_config["monitor_interval"] = 5
        with pytest.raises(ValueError, match="monitor_interval"):
            LoRaDiagnosticsPlugin(mock_app, base_config)

    @patch("RNS.Transport")
    def test_invalid_beacon_interval(self, mock_transport, mock_app, base_config):
        from reticulumpi.builtin_plugins.lora_diagnostics import (
            LoRaDiagnosticsPlugin,
        )

        base_config["beacon_interval"] = 10
        with pytest.raises(ValueError, match="beacon_interval"):
            LoRaDiagnosticsPlugin(mock_app, base_config)

    @patch("RNS.Transport")
    def test_invalid_destination_hash(self, mock_transport, mock_app, base_config):
        from reticulumpi.builtin_plugins.lora_diagnostics import (
            LoRaDiagnosticsPlugin,
        )

        base_config["monitored_destinations"] = [{"hash": "not_hex"}]
        with pytest.raises(ValueError, match="Invalid hex hash"):
            LoRaDiagnosticsPlugin(mock_app, base_config)

    @patch("RNS.Transport")
    def test_missing_hash_key(self, mock_transport, mock_app, base_config):
        from reticulumpi.builtin_plugins.lora_diagnostics import (
            LoRaDiagnosticsPlugin,
        )

        base_config["monitored_destinations"] = [{"name": "no hash"}]
        with pytest.raises(ValueError, match="must have a 'hash' key"):
            LoRaDiagnosticsPlugin(mock_app, base_config)

    @patch("RNS.Transport")
    def test_non_list_destinations(self, mock_transport, mock_app, base_config):
        from reticulumpi.builtin_plugins.lora_diagnostics import (
            LoRaDiagnosticsPlugin,
        )

        base_config["monitored_destinations"] = "not a list"
        with pytest.raises(ValueError, match="must be a list"):
            LoRaDiagnosticsPlugin(mock_app, base_config)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @patch("RNS.Transport")
    def test_start_stop(self, mock_transport, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        assert plugin._active is True
        assert len(plugin._threads) == 2  # monitor + beacon
        plugin.stop()
        assert plugin._active is False

    @patch("RNS.Transport")
    def test_get_status(self, mock_transport, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        status = plugin.get_status()
        assert status["active"] is True
        assert status["monitored_destinations"] == 2
        assert status["beacons_sent"] == 0
        plugin.stop()

    @patch("RNS.Transport")
    def test_get_status_empty_config(self, mock_transport, mock_app):
        plugin = _make_plugin(mock_app, {})
        plugin.start()
        status = plugin.get_status()
        assert status["monitored_destinations"] == 0
        plugin.stop()


# ---------------------------------------------------------------------------
# Monitored destinations parsing
# ---------------------------------------------------------------------------


class TestMonitoredDestinations:
    @patch("RNS.Transport")
    def test_parses_destinations(self, mock_transport, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        assert len(plugin._monitored) == 2
        ratcom = plugin._monitored["c99eced76cb1bbc2e6711b6fbea115eb"]
        assert ratcom["name"] == "Ratcom"
        assert ratcom["hash_bytes"] == bytes.fromhex(
            "c99eced76cb1bbc2e6711b6fbea115eb"
        )
        assert ratcom["has_path"] is False
        plugin.stop()

    @patch("RNS.Transport")
    def test_default_name_from_hash(self, mock_transport, mock_app):
        config = {
            "monitored_destinations": [
                {"hash": "aabbccddee0011223344556677889900"}
            ],
        }
        plugin = _make_plugin(mock_app, config)
        plugin.start()
        dest = plugin._monitored["aabbccddee0011223344556677889900"]
        assert dest["name"] == "aabbccddee00"  # first 12 chars of hash
        plugin.stop()


# ---------------------------------------------------------------------------
# get_diagnostics
# ---------------------------------------------------------------------------


class TestGetDiagnostics:
    @patch("RNS.Transport")
    def test_diagnostics_structure(self, mock_transport, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        diag = plugin.get_diagnostics()

        # Top-level keys
        assert "lora_interface" in diag
        assert "monitored_destinations" in diag
        assert "beacon" in diag

        # LoRa interface defaults
        assert diag["lora_interface"]["name"] == "RNode LoRa Interface"
        assert diag["lora_interface"]["online"] is False

        # Monitored destinations
        assert len(diag["monitored_destinations"]) == 2
        first = diag["monitored_destinations"][0]
        assert "hash" in first
        assert "name" in first
        assert "has_path" in first
        assert "hops" in first
        assert "last_announce_seen" in first

        # Beacon
        assert diag["beacon"]["beacons_sent"] == 0
        assert diag["beacon"]["interval"] == 120

        plugin.stop()


# ---------------------------------------------------------------------------
# Path checking
# ---------------------------------------------------------------------------


class TestPathChecking:
    @patch("RNS.Transport")
    def test_path_discovered_event(self, mock_transport, mock_app, base_config):
        mock_transport.has_path.return_value = True
        mock_transport.hops_to.return_value = 1

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        events_received = []
        mock_app.event_bus.subscribe(
            "lora.peer_path_lost", lambda e, d: events_received.append(("lost", d))
        )

        # Initial state: has_path is False
        # After check: has_path becomes True -> path discovered (no "lost" event)
        plugin._check_monitored_paths()

        ratcom = plugin._monitored["c99eced76cb1bbc2e6711b6fbea115eb"]
        assert ratcom["has_path"] is True
        assert ratcom["hops"] == 1
        assert len(events_received) == 0  # No loss event
        plugin.stop()

    @patch("RNS.Transport")
    def test_path_lost_event(self, mock_transport, mock_app, base_config):
        mock_transport.has_path.return_value = False
        mock_transport.hops_to.return_value = None

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        # Simulate previously having a path
        plugin._monitored["c99eced76cb1bbc2e6711b6fbea115eb"]["has_path"] = True

        events_received = []
        mock_app.event_bus.subscribe(
            "lora.peer_path_lost", lambda e, d: events_received.append(d)
        )

        plugin._check_monitored_paths()

        ratcom = plugin._monitored["c99eced76cb1bbc2e6711b6fbea115eb"]
        assert ratcom["has_path"] is False
        assert len(events_received) >= 1
        assert events_received[0]["hash"] == "c99eced76cb1bbc2e6711b6fbea115eb"
        plugin.stop()


# ---------------------------------------------------------------------------
# Announce handler
# ---------------------------------------------------------------------------


class TestAnnounceHandler:
    @patch("RNS.Transport")
    def test_monitored_announce_tracked(self, mock_transport, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        events_received = []
        mock_app.event_bus.subscribe(
            "lora.peer_announce_received", lambda e, d: events_received.append(d)
        )

        dest_hash = bytes.fromhex("c99eced76cb1bbc2e6711b6fbea115eb")
        plugin.on_announce_received(dest_hash, MagicMock(), b"test")

        ratcom = plugin._monitored["c99eced76cb1bbc2e6711b6fbea115eb"]
        assert ratcom["last_announce_seen"] is not None
        assert len(events_received) == 1
        assert events_received[0]["name"] == "Ratcom"
        plugin.stop()

    @patch("RNS.Transport")
    def test_unmonitored_announce_ignored(self, mock_transport, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        events_received = []
        mock_app.event_bus.subscribe(
            "lora.peer_announce_received", lambda e, d: events_received.append(d)
        )

        unknown_hash = bytes.fromhex("aa" * 16)
        plugin.on_announce_received(unknown_hash, MagicMock(), b"test")

        assert len(events_received) == 0
        plugin.stop()

    @patch("RNS.Transport")
    def test_announce_callback(self, mock_transport, mock_app, base_config):
        """on_announce_received updates monitored destination state."""
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        dest_hash = bytes.fromhex("c99eced76cb1bbc2e6711b6fbea115eb")
        plugin.on_announce_received(dest_hash, MagicMock(), b"test")

        ratcom = plugin._monitored["c99eced76cb1bbc2e6711b6fbea115eb"]
        assert ratcom["last_announce_seen"] is not None
        plugin.stop()


# ---------------------------------------------------------------------------
# Beacon
# ---------------------------------------------------------------------------


class TestBeacon:
    @patch("RNS.Transport")
    def test_beacon_announces_heartbeat(self, mock_transport, mock_app, base_config):
        mock_dest = MagicMock()
        mock_heartbeat = MagicMock()
        mock_heartbeat.destination = mock_dest
        mock_heartbeat._build_app_data.return_value = "TestNode|cpu:5%"

        mock_app.get_plugin = MagicMock(
            side_effect=lambda name: mock_heartbeat if name == "heartbeat_announce" else None
        )

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        plugin._send_beacons()

        mock_dest.announce.assert_called_once()
        assert plugin._beacons_sent == 1
        assert plugin._last_beacon_time is not None
        plugin.stop()

    @patch("RNS.Transport")
    def test_beacon_no_plugins_no_error(self, mock_transport, mock_app, base_config):
        """Beacon gracefully handles missing sibling plugins."""
        mock_app.get_plugin.return_value = None

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        plugin._send_beacons()

        assert plugin._beacons_sent == 0
        plugin.stop()

    @patch("RNS.Transport")
    def test_beacon_announces_messaging_hub(self, mock_transport, mock_app, base_config):
        mock_lxmf_dest = MagicMock()
        mock_adapter = MagicMock()
        mock_adapter._destination = mock_lxmf_dest

        mock_hub = MagicMock()
        mock_hub._adapters = {"lxmf": mock_adapter}

        mock_app.get_plugin = MagicMock(
            side_effect=lambda name: mock_hub if name == "messaging_hub" else None
        )

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        plugin._send_beacons()

        mock_lxmf_dest.announce.assert_called_once()
        assert plugin._beacons_sent == 1
        plugin.stop()

    @patch("RNS.Transport")
    def test_beacon_counts_multiple_destinations(
        self, mock_transport, mock_app, base_config
    ):
        mock_hb_dest = MagicMock()
        mock_heartbeat = MagicMock()
        mock_heartbeat.destination = mock_hb_dest
        mock_heartbeat._build_app_data.return_value = None

        mock_lxmf_dest = MagicMock()
        mock_adapter = MagicMock()
        mock_adapter._destination = mock_lxmf_dest
        mock_hub = MagicMock()
        mock_hub._adapters = {"lxmf": mock_adapter}

        def get_plugin(name):
            if name == "heartbeat_announce":
                return mock_heartbeat
            if name == "messaging_hub":
                return mock_hub
            return None

        mock_app.get_plugin = MagicMock(side_effect=get_plugin)

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        plugin._send_beacons()

        assert plugin._beacons_sent == 2
        mock_hb_dest.announce.assert_called_once()
        mock_lxmf_dest.announce.assert_called_once()
        plugin.stop()


# ---------------------------------------------------------------------------
# Interface stats polling
# ---------------------------------------------------------------------------


class TestInterfaceStats:
    @patch("RNS.Transport")
    def test_poll_interface_stats(self, mock_transport, mock_app, base_config):
        mock_app.reticulum.get_interface_stats.return_value = {
            "interfaces": [
                {
                    "name": "RNodeInterface[RNode LoRa Interface]",
                    "short_name": "RNode LoRa Interface",
                    "status": True,
                    "rxb": 5000,
                    "txb": 3000,
                    "airtime_short": 11.45,
                    "airtime_long": 2.97,
                    "announce_queue": 42,
                    "channel_load_short": 4.5,
                    "channel_load_long": 2.1,
                },
                {
                    "name": "TCPInterface[TCP Client beleth]",
                    "short_name": "TCP Client beleth",
                    "status": True,
                    "rxb": 100000,
                    "txb": 50000,
                },
            ],
            "rxb": 0,
            "txb": 0,
        }

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        plugin._poll_interface_stats("RNode LoRa Interface")

        assert plugin._lora_stats["online"] is True
        assert plugin._lora_stats["rxb"] == 5000
        assert plugin._lora_stats["txb"] == 3000
        assert plugin._lora_stats["airtime_short"] == 11.45
        plugin.stop()

    @patch("RNS.Transport")
    def test_poll_no_lora_interface(self, mock_transport, mock_app, base_config):
        """Gracefully handles missing LoRa interface in stats."""
        mock_app.reticulum.get_interface_stats.return_value = {
            "interfaces": [
                {"name": "TCPInterface[TCP Client beleth]", "short_name": "TCP Client beleth", "status": True, "rxb": 100, "txb": 50},
            ],
        }

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        plugin._poll_interface_stats("RNode LoRa Interface")

        # Stats should remain at defaults
        assert plugin._lora_stats["online"] is False
        assert plugin._lora_stats["rxb"] == 0
        plugin.stop()

    @patch("RNS.Transport")
    def test_poll_exception_handled(self, mock_transport, mock_app, base_config):
        """Exception in get_interface_stats doesn't crash."""
        mock_app.reticulum.get_interface_stats.side_effect = Exception("timeout")

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        plugin._poll_interface_stats("RNode LoRa Interface")
        # Should not raise
        plugin.stop()

    @patch("RNS.Transport")
    def test_diagnostics_includes_announce_mode(self, mock_transport, mock_app, base_config):
        """Diagnostics response includes current announce mode info."""
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        # Mock _detect_announce_mode to avoid filesystem access
        plugin._detect_announce_mode = lambda: "all"

        diag = plugin.get_diagnostics()
        assert "announce_mode" in diag
        assert diag["announce_mode"]["current"] == "all"
        assert "all" in diag["announce_mode"]["available"]
        assert "local_priority" in diag["announce_mode"]["available"]
        assert "silent" in diag["announce_mode"]["available"]
        plugin.stop()


class TestAnnounceMode:
    @patch("RNS.Transport")
    def test_set_invalid_mode_raises(self, mock_transport, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        with pytest.raises(ValueError, match="Invalid mode"):
            plugin.set_announce_mode("invalid_mode")
        plugin.stop()

    @patch("RNS.Transport")
    def test_detect_mode_all(self, mock_transport, mock_app, base_config):
        """Detect 'all' mode from config with announce_cap > 1."""
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        from reticulumpi.rns_config import InterfaceEntry
        iface = InterfaceEntry(
            name="RNode LoRa Interface",
            properties={"announce_cap": "5"},
        )
        with patch(
            "reticulumpi.rns_config.parse_rns_config",
            return_value=([], [iface]),
        ):
            mode = plugin._detect_announce_mode()
            assert mode == "all"
        plugin.stop()

    @patch("RNS.Transport")
    def test_detect_mode_local_priority(self, mock_transport, mock_app, base_config):
        """Detect 'local_priority' mode from config with announce_cap <= 1."""
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        from reticulumpi.rns_config import InterfaceEntry
        iface = InterfaceEntry(
            name="RNode LoRa Interface",
            properties={"announce_cap": "1"},
        )
        with patch(
            "reticulumpi.rns_config.parse_rns_config",
            return_value=([], [iface]),
        ):
            mode = plugin._detect_announce_mode()
            assert mode == "local_priority"
        plugin.stop()

    @patch("RNS.Transport")
    def test_detect_mode_silent(self, mock_transport, mock_app, base_config):
        """Detect 'silent' mode from config with interface_mode=access_point."""
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        from reticulumpi.rns_config import InterfaceEntry
        iface = InterfaceEntry(
            name="RNode LoRa Interface",
            properties={"interface_mode": "access_point", "announce_cap": "5"},
        )
        with patch(
            "reticulumpi.rns_config.parse_rns_config",
            return_value=([], [iface]),
        ):
            mode = plugin._detect_announce_mode()
            assert mode == "silent"
        plugin.stop()

    @patch("RNS.Transport")
    def test_detect_mode_handles_parse_error(self, mock_transport, mock_app, base_config):
        """Detection gracefully handles config parse errors."""
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        with patch(
            "reticulumpi.rns_config.parse_rns_config",
            side_effect=FileNotFoundError("no config"),
        ):
            mode = plugin._detect_announce_mode()
            assert mode == "unknown"
        plugin.stop()


# ---------------------------------------------------------------------------
# Interface stats polling
# ---------------------------------------------------------------------------


class TestInterfaceStats:
    @patch("RNS.Transport")
    def test_delta_tracking(self, mock_transport, mock_app, base_config):
        mock_app.reticulum.get_interface_stats.return_value = {
            "interfaces": [
                {"name": "RNodeInterface[RNode LoRa Interface]", "short_name": "RNode LoRa Interface", "status": True, "rxb": 1000, "txb": 500},
            ],
        }

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        # First poll
        plugin._poll_interface_stats("RNode LoRa Interface")
        assert plugin._lora_stats["rxb"] == 1000

        # Second poll with updated counters
        mock_app.reticulum.get_interface_stats.return_value = {
            "interfaces": [
                {"name": "RNodeInterface[RNode LoRa Interface]", "short_name": "RNode LoRa Interface", "status": True, "rxb": 1500, "txb": 800},
            ],
        }
        plugin._poll_interface_stats("RNode LoRa Interface")
        assert plugin._lora_stats["rxb"] == 1500
        assert plugin._lora_stats["rxb_delta"] == 500
        assert plugin._lora_stats["txb_delta"] == 300
        plugin.stop()
