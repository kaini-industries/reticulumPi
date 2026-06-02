"""Tests for the connectivity_monitor plugin."""

from __future__ import annotations

import os
import socket
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi.builtin_plugins.connectivity_monitor import (
    ConnectivityMonitorPlugin,
    _I2P_BOOTSTRAP_GRACE,
)


@pytest.fixture()
def _tmp_log(tmp_path):
    """Return a temp log path."""
    return str(tmp_path / "connectivity.log")


@pytest.fixture()
def plugin(_tmp_log):
    """Create a ConnectivityMonitorPlugin with mocked app."""
    app = MagicMock()
    app.reticulum = MagicMock()
    app.reticulum.configdir = tempfile.mkdtemp()

    # Create minimal storage structure
    storage_dir = os.path.join(app.reticulum.configdir, "storage")
    os.makedirs(storage_dir, exist_ok=True)

    config = {
        "check_interval": 30,
        "log_path": _tmp_log,
        "sam_port": 7656,
        "shared_instance_port": 37428,
    }
    p = ConnectivityMonitorPlugin(app, config)
    return p


class TestValidateConfig:
    def test_valid_config(self, plugin):
        """Default config passes validation."""
        plugin.validate_config()

    def test_invalid_interval(self):
        app = MagicMock()
        app.reticulum = MagicMock()
        with pytest.raises(ValueError, match="check_interval"):
            ConnectivityMonitorPlugin(app, {"check_interval": 3})

    def test_missing_interval_uses_default(self):
        app = MagicMock()
        app.reticulum = MagicMock()
        # Should not raise — default interval is used
        ConnectivityMonitorPlugin(app, {})


class TestLifecycle:
    def test_start_stop(self, plugin):
        """Plugin can start and stop without error."""
        plugin.start()
        assert plugin._active is True
        assert len(plugin._threads) == 1

        plugin.stop()
        assert plugin._active is False

    def test_get_status(self, plugin):
        plugin.start()
        status = plugin.get_status()
        assert status["active"] is True
        assert "rnsd_reachable" in status
        assert "issues" in status
        plugin.stop()

    def test_get_health(self, plugin):
        plugin.start()
        health = plugin.get_health()
        assert "rnsd_reachable" in health
        assert "i2p_status" in health
        assert "interfaces_online" in health
        assert "issues" in health
        assert isinstance(health["issues"], list)
        plugin.stop()


class TestRnsdCheck:
    def test_rnsd_reachable(self, plugin):
        plugin.start()
        plugin.app.reticulum.get_interface_stats.return_value = {
            "interfaces": [{"name": "Auto", "type": "AutoInterface", "status": True}]
        }
        result = plugin._check_rnsd()
        assert result is True
        assert plugin._health["rnsd_reachable"] is True
        plugin.stop()

    def test_rnsd_unreachable(self, plugin):
        plugin.start()
        plugin.app.reticulum.get_interface_stats.side_effect = Exception("No connection")
        result = plugin._check_rnsd()
        assert result is False
        assert plugin._health["rnsd_reachable"] is False
        plugin.stop()

    def test_rnsd_no_reticulum_instance(self, plugin):
        plugin.start()
        plugin.app.reticulum = None
        result = plugin._check_rnsd()
        assert result is False
        plugin.stop()


class TestInterfaceCheck:
    def test_all_interfaces_online(self, plugin):
        plugin.start()
        plugin.app.reticulum.get_interface_stats.return_value = {
            "interfaces": [
                {"name": "Auto", "type": "AutoInterface", "status": True, "rxb": 0, "txb": 0},
                {
                    "name": "TCP",
                    "type": "TCPClientInterface",
                    "status": True,
                    "rxb": 100,
                    "txb": 200,
                },
            ]
        }
        issues = plugin._check_interfaces()
        assert len(issues) == 0
        assert plugin._health["interfaces_online"] == 2
        assert plugin._health["interfaces_total"] == 2
        plugin.stop()

    def test_interface_offline(self, plugin):
        plugin.start()
        plugin.app.reticulum.get_interface_stats.return_value = {
            "interfaces": [
                {
                    "name": "TCP Hub",
                    "type": "TCPClientInterface",
                    "status": False,
                    "rxb": 0,
                    "txb": 0,
                },
            ]
        }
        issues = plugin._check_interfaces()
        assert any("OFFLINE" in i for i in issues)
        assert plugin._health["interfaces_online"] == 0
        plugin.stop()

    def test_skips_local_interfaces(self, plugin):
        plugin.start()
        plugin.app.reticulum.get_interface_stats.return_value = {
            "interfaces": [
                {"name": "Local", "type": "LocalClientInterface", "status": True},
                {"name": "Server", "type": "LocalServerInterface", "status": True},
                {"name": "Auto", "type": "AutoInterface", "status": True, "rxb": 0, "txb": 0},
            ]
        }
        plugin._check_interfaces()
        assert plugin._health["interfaces_total"] == 1  # Only AutoInterface
        plugin.stop()

    def test_i2p_tracks_traffic(self, plugin):
        plugin.start()
        plugin.app.reticulum.get_interface_stats.return_value = {
            "interfaces": [
                {
                    "name": "I2P",
                    "type": "I2PInterface",
                    "status": True,
                    "rxb": 100,
                    "txb": 200,
                    "i2p_peers": 0,
                },
            ]
        }
        issues = plugin._check_interfaces()
        assert plugin._health["i2p_traffic"] == {"rxb": 100, "txb": 200}
        # 0 RNS peers over I2P is not an interface issue
        assert not any("non-functional" in i for i in issues)
        plugin.stop()

    def test_tcp_hub_stale_after_consecutive_checks(self, plugin):
        """TCP hub only warns after _HUB_STALE_CHECKS consecutive zero-delta checks."""
        plugin.start()
        stats = {
            "interfaces": [
                {
                    "name": "Hub1",
                    "type": "TCPClientInterface",
                    "status": True,
                    "rxb": 500,
                    "txb": 300,
                },
            ]
        }
        plugin.app.reticulum.get_interface_stats.return_value = stats

        # First call seeds previous traffic — no warning possible
        issues = plugin._check_interfaces()
        assert not any("stale" in i for i in issues)

        # Next 2 calls with same traffic — still under threshold (3)
        for _ in range(2):
            issues = plugin._check_interfaces()
            assert not any("stale" in i for i in issues)

        # 3rd consecutive zero-delta check — NOW it warns
        issues = plugin._check_interfaces()
        assert any("stale" in i for i in issues)
        plugin.stop()

    def test_tcp_hub_stale_resets_on_traffic(self, plugin):
        """Counter resets when traffic resumes."""
        plugin.start()
        stats_idle = {
            "interfaces": [
                {
                    "name": "Hub1",
                    "type": "TCPClientInterface",
                    "status": True,
                    "rxb": 500,
                    "txb": 300,
                },
            ]
        }
        plugin.app.reticulum.get_interface_stats.return_value = stats_idle

        # Seed + 2 idle checks
        plugin._check_interfaces()
        plugin._check_interfaces()
        plugin._check_interfaces()

        # Traffic resumes
        stats_active = {
            "interfaces": [
                {
                    "name": "Hub1",
                    "type": "TCPClientInterface",
                    "status": True,
                    "rxb": 600,
                    "txb": 400,
                },
            ]
        }
        plugin.app.reticulum.get_interface_stats.return_value = stats_active
        issues = plugin._check_interfaces()
        assert not any("stale" in i for i in issues)

        # Back to idle — counter should have reset, so 1 idle check is fine
        plugin.app.reticulum.get_interface_stats.return_value = stats_idle
        # Re-seed with new baseline
        stats_idle["interfaces"][0]["rxb"] = 600
        stats_idle["interfaces"][0]["txb"] = 400
        issues = plugin._check_interfaces()
        assert not any("stale" in i for i in issues)
        plugin.stop()


class TestI2PCheck:
    def test_sam_reachable_and_ok(self, plugin):
        plugin.start()
        with (
            patch.object(ConnectivityMonitorPlugin, "_probe_port", return_value=True),
            patch.object(
                ConnectivityMonitorPlugin,
                "_query_i2pd_console",
                return_value={"network_status": "OK", "routers": 2000, "client_tunnels": 50},
            ),
        ):
            plugin._i2p_start_time = time.monotonic() - _I2P_BOOTSTRAP_GRACE - 60
            issues = plugin._check_i2p()
        assert plugin._health["sam_reachable"] is True
        assert plugin._health["i2p_status"] == "ok"
        assert len(issues) == 0
        plugin.stop()

    def test_sam_unreachable(self, plugin):
        plugin.start()
        with patch.object(ConnectivityMonitorPlugin, "_probe_port", return_value=False):
            issues = plugin._check_i2p()
        assert plugin._health["sam_reachable"] is False
        assert any("SAM API" in i for i in issues)
        plugin.stop()

    def test_i2p_bootstrapping(self, plugin):
        plugin.start()
        plugin._i2p_start_time = time.monotonic()  # just started
        with (
            patch.object(ConnectivityMonitorPlugin, "_probe_port", return_value=True),
            patch.object(
                ConnectivityMonitorPlugin,
                "_query_i2pd_console",
                return_value={"network_status": "Testing", "routers": 0, "client_tunnels": 0},
            ),
        ):
            issues = plugin._check_i2p()
        assert plugin._health["i2p_status"] == "bootstrapping"
        assert len(issues) == 0  # No issues during bootstrap grace
        plugin.stop()

    def test_i2p_firewalled(self, plugin):
        plugin.start()
        plugin._i2p_start_time = time.monotonic() - _I2P_BOOTSTRAP_GRACE - 60
        with (
            patch.object(ConnectivityMonitorPlugin, "_probe_port", return_value=True),
            patch.object(
                ConnectivityMonitorPlugin,
                "_query_i2pd_console",
                return_value={
                    "network_status": "Firewalled",
                    "routers": 2000,
                    "client_tunnels": 50,
                },
            ),
        ):
            issues = plugin._check_i2p()
        assert plugin._health["i2p_status"] == "firewalled"
        # Firewalled with routers is NOT an issue (just informational)
        assert len(issues) == 0
        plugin.stop()

    def test_i2p_no_routers_after_grace(self, plugin):
        plugin.start()
        plugin._i2p_start_time = time.monotonic() - _I2P_BOOTSTRAP_GRACE - 60
        with (
            patch.object(ConnectivityMonitorPlugin, "_probe_port", return_value=True),
            patch.object(
                ConnectivityMonitorPlugin,
                "_query_i2pd_console",
                return_value={"network_status": "Firewalled", "routers": 0, "client_tunnels": 0},
            ),
        ):
            issues = plugin._check_i2p()
        assert any("0 routers" in i for i in issues)
        plugin.stop()


class TestPathCheck:
    def test_transport_active_detected(self, plugin):
        plugin.start()
        plugin._last_iface_stats = {"transport_id": b"\x01" * 16}

        plugin._check_paths()
        assert plugin._health["transport_active"] is True
        plugin.stop()

    def test_transport_inactive_detected(self, plugin):
        plugin.start()
        plugin._last_iface_stats = {"transport_id": None}

        plugin._check_paths()
        assert plugin._health["transport_active"] is False
        plugin.stop()

    def test_no_issues_returned(self, plugin):
        """_check_paths no longer checks file mtime — should return no issues."""
        plugin.start()
        plugin._last_iface_stats = {"transport_id": b"\x01" * 16}

        issues = plugin._check_paths()
        assert issues == []
        plugin.stop()


class TestLogFile:
    def test_log_file_created(self, plugin, _tmp_log):
        plugin.start()
        assert os.path.exists(os.path.dirname(_tmp_log))
        plugin.stop()

    def test_diagnostics_writes_log(self, plugin, _tmp_log):
        plugin.start()
        # Mock rnsd as reachable
        with patch.object(socket, "create_connection", return_value=MagicMock()):
            plugin.app.reticulum.get_interface_stats.return_value = {
                "interfaces": [
                    {"name": "Auto", "type": "AutoInterface", "status": True, "rxb": 0, "txb": 0},
                ]
            }
            plugin._health["i2p_peers"] = 1
            plugin._run_diagnostics()

        # Check log file was written
        assert os.path.exists(_tmp_log)
        with open(_tmp_log) as f:
            content = f.read()
        assert "Diagnostics" in content
        plugin.stop()


class TestProbePort:
    def test_probe_port_open(self):
        with patch.object(socket, "create_connection", return_value=MagicMock()):
            assert ConnectivityMonitorPlugin._probe_port("127.0.0.1", 7656) is True

    def test_probe_port_closed(self):
        with patch.object(
            socket,
            "create_connection",
            side_effect=OSError("refused"),
        ):
            assert ConnectivityMonitorPlugin._probe_port("127.0.0.1", 9999) is False


def _make_path_entry(hash_hex, hops, iface, age_seconds=60, expires_in=86400):
    """Helper to create a mock path table entry."""
    now = time.time()
    return {
        "hash": bytes.fromhex(hash_hex),
        "hops": hops,
        "via": bytes.fromhex("bb" * 16),
        "interface": iface,
        "timestamp": now - age_seconds,
        "expires": now + expires_in,
    }


class TestRoutingDataCollection:
    def test_collect_routing_data_basic(self, plugin):
        plugin.start()
        plugin._last_iface_stats = {
            "transport_id": b"\xaa" * 16,
            "transport_uptime": 3600,
            "probe_responder": b"\xcc" * 16,
        }
        plugin.app.reticulum.get_path_table.return_value = [
            _make_path_entry("aa" * 16, 1, "TCP Client A"),
            _make_path_entry("bb" * 16, 2, "TCP Client A"),
            _make_path_entry("cc" * 16, 2, "TCP Client B"),
            _make_path_entry("dd" * 16, 3, "I2P Interface"),
            _make_path_entry("ee" * 16, 3, "I2P Interface"),
        ]
        plugin.app.reticulum.get_rate_table.return_value = []
        plugin.app.reticulum.get_link_count.return_value = 2
        plugin.app.reticulum.get_blackholed_identities.return_value = {}

        plugin._collect_routing_data()

        routing = plugin._health["routing"]
        assert routing["path_count"] == 5
        assert routing["hop_distribution"][1] == 1
        assert routing["hop_distribution"][2] == 2
        assert routing["hop_distribution"][3] == 2
        assert routing["interface_distribution"]["TCP Client A"] == 2
        assert routing["interface_distribution"]["TCP Client B"] == 1
        assert routing["interface_distribution"]["I2P Interface"] == 2
        assert routing["link_count"] == 2
        assert routing["transport_id"] is not None
        assert routing["transport_uptime"] == 3600
        assert plugin._health["path_count"] == 5
        plugin.stop()

    def test_collect_routing_data_empty(self, plugin):
        plugin.start()
        plugin._last_iface_stats = {}
        plugin.app.reticulum.get_path_table.return_value = []
        plugin.app.reticulum.get_rate_table.return_value = []
        plugin.app.reticulum.get_link_count.return_value = 0
        plugin.app.reticulum.get_blackholed_identities.return_value = {}

        issues = plugin._collect_routing_data()

        assert any("empty" in i.lower() for i in issues)
        assert plugin._health["routing"]["path_count"] == 0
        plugin.stop()

    def test_collect_routing_data_single_interface_spof(self, plugin):
        plugin.start()
        plugin._last_iface_stats = {}
        plugin.app.reticulum.get_path_table.return_value = [
            _make_path_entry("aa" * 16, 2, "TCP Client A"),
            _make_path_entry("bb" * 16, 3, "TCP Client A"),
            _make_path_entry("cc" * 16, 4, "TCP Client A"),
        ]
        plugin.app.reticulum.get_rate_table.return_value = []
        plugin.app.reticulum.get_link_count.return_value = 0
        plugin.app.reticulum.get_blackholed_identities.return_value = {}

        issues = plugin._collect_routing_data()

        assert any("single point" in i.lower() for i in issues)
        plugin.stop()

    def test_collect_routing_data_rate_limited(self, plugin):
        plugin.start()
        plugin._last_iface_stats = {}
        plugin.app.reticulum.get_path_table.return_value = [
            _make_path_entry("aa" * 16, 1, "TCP A"),
        ]
        plugin.app.reticulum.get_rate_table.return_value = [
            {
                "hash": b"\xaa" * 16,
                "last": time.time(),
                "rate_violations": 3,
                "blocked_until": time.time() + 60,
                "timestamps": [],
            },
        ]
        plugin.app.reticulum.get_link_count.return_value = 0
        plugin.app.reticulum.get_blackholed_identities.return_value = {}

        issues = plugin._collect_routing_data()

        assert any("rate-limited" in i.lower() for i in issues)
        assert plugin._health["routing"]["rate_limited_count"] == 1
        assert plugin._health["routing"]["rate_tracked_count"] == 1
        plugin.stop()

    def test_collect_routing_data_rate_table_counts(self, plugin):
        """rate_limited_count reflects only actively blocked destinations."""
        plugin.start()
        plugin._last_iface_stats = {}
        now = time.time()
        plugin.app.reticulum.get_path_table.return_value = [
            _make_path_entry("aa" * 16, 1, "TCP A"),
        ]
        plugin.app.reticulum.get_rate_table.return_value = [
            {
                "hash": b"\xaa" * 16,
                "last": now,
                "rate_violations": 3,
                "blocked_until": now + 60,
                "timestamps": [],
            },
            {
                "hash": b"\xbb" * 16,
                "last": now - 3600,
                "rate_violations": 1,
                "blocked_until": now - 10,
                "timestamps": [],
            },
            {
                "hash": b"\xcc" * 16,
                "last": now - 7200,
                "rate_violations": 0,
                "blocked_until": 0,
                "timestamps": [],
            },
        ]
        plugin.app.reticulum.get_link_count.return_value = 0
        plugin.app.reticulum.get_blackholed_identities.return_value = {}

        issues = plugin._collect_routing_data()

        routing = plugin._health["routing"]
        assert routing["rate_limited_count"] == 1
        assert routing["rate_tracked_count"] == 3
        rate_diags = [i for i in issues if "rate" in i.lower()]
        assert len(rate_diags) == 1
        assert "1 destination" in rate_diags[0]
        assert "3 tracked" in rate_diags[0]
        plugin.stop()

    def test_collect_routing_data_no_active_blocks(self, plugin):
        """No diagnostic when rate table has entries but none actively blocked."""
        plugin.start()
        plugin._last_iface_stats = {}
        plugin.app.reticulum.get_path_table.return_value = [
            _make_path_entry("aa" * 16, 1, "TCP A"),
        ]
        plugin.app.reticulum.get_rate_table.return_value = [
            {
                "hash": b"\xdd" * 16,
                "last": time.time() - 3600,
                "rate_violations": 0,
                "blocked_until": 0,
                "timestamps": [],
            },
            {
                "hash": b"\xee" * 16,
                "last": time.time() - 7200,
                "rate_violations": 0,
                "blocked_until": 0,
                "timestamps": [],
            },
        ]
        plugin.app.reticulum.get_link_count.return_value = 0
        plugin.app.reticulum.get_blackholed_identities.return_value = {}

        issues = plugin._collect_routing_data()

        routing = plugin._health["routing"]
        assert routing["rate_limited_count"] == 0
        assert routing["rate_tracked_count"] == 2
        assert not any("rate" in i.lower() for i in issues)
        plugin.stop()

    def test_collect_routing_data_rpc_failure(self, plugin):
        plugin.start()
        plugin._last_iface_stats = {}
        plugin.app.reticulum.get_path_table.side_effect = Exception("RPC error")
        plugin.app.reticulum.get_rate_table.side_effect = Exception("RPC error")
        plugin.app.reticulum.get_link_count.side_effect = Exception("RPC error")
        plugin.app.reticulum.get_blackholed_identities.side_effect = Exception("RPC error")

        # Should not raise
        issues = plugin._collect_routing_data()
        assert isinstance(issues, list)
        plugin.stop()

    def test_get_routing_data_pagination(self, plugin):
        plugin.start()
        # Populate with 250 mock entries
        entries = []
        for i in range(250):
            h = f"{i:032x}"
            entries.append(
                {
                    "hash": h,
                    "hops": (i % 5) + 1,
                    "via": "bb" * 16,
                    "interface": "TCP A",
                    "timestamp": time.time() - 60,
                    "age_s": 60,
                    "expires": time.time() + 86400,
                    "expires_in_s": 86400,
                }
            )
        plugin._routing_data["path_table"] = entries

        result = plugin.get_routing_data(page=2, per_page=100)
        assert len(result["paths"]) == 100
        assert result["page"] == 2
        assert result["total_paths"] == 250
        assert result["pages"] == 3
        plugin.stop()

    def test_get_routing_data_filtering(self, plugin):
        plugin.start()
        plugin._routing_data["path_table"] = [
            {
                "hash": "aa" * 16,
                "hops": 1,
                "via": "",
                "interface": "TCP Client A",
                "timestamp": 0,
                "age_s": 0,
                "expires": 0,
                "expires_in_s": 0,
            },
            {
                "hash": "bb" * 16,
                "hops": 2,
                "via": "",
                "interface": "I2P Interface",
                "timestamp": 0,
                "age_s": 0,
                "expires": 0,
                "expires_in_s": 0,
            },
            {
                "hash": "cc" * 16,
                "hops": 3,
                "via": "",
                "interface": "TCP Client A",
                "timestamp": 0,
                "age_s": 0,
                "expires": 0,
                "expires_in_s": 0,
            },
        ]

        result = plugin.get_routing_data(iface_filter="I2P")
        assert result["total_paths"] == 1
        assert result["paths"][0]["interface"] == "I2P Interface"
        plugin.stop()

    def test_get_routing_data_sorting(self, plugin):
        plugin.start()
        plugin._routing_data["path_table"] = [
            {
                "hash": "aa" * 16,
                "hops": 3,
                "via": "",
                "interface": "A",
                "timestamp": 0,
                "age_s": 0,
                "expires": 0,
                "expires_in_s": 0,
            },
            {
                "hash": "bb" * 16,
                "hops": 1,
                "via": "",
                "interface": "A",
                "timestamp": 0,
                "age_s": 0,
                "expires": 0,
                "expires_in_s": 0,
            },
            {
                "hash": "cc" * 16,
                "hops": 2,
                "via": "",
                "interface": "A",
                "timestamp": 0,
                "age_s": 0,
                "expires": 0,
                "expires_in_s": 0,
            },
        ]

        # Ascending
        result = plugin.get_routing_data(sort="hops", order="asc")
        hops = [p["hops"] for p in result["paths"]]
        assert hops == [1, 2, 3]

        # Descending
        result = plugin.get_routing_data(sort="hops", order="desc")
        hops = [p["hops"] for p in result["paths"]]
        assert hops == [3, 2, 1]
        plugin.stop()

    def test_get_routing_data_summary_only(self, plugin):
        plugin.start()
        plugin._routing_data["path_table"] = [
            {
                "hash": "aa" * 16,
                "hops": 1,
                "via": "",
                "interface": "A",
                "timestamp": 0,
                "age_s": 0,
                "expires": 0,
                "expires_in_s": 0,
            },
        ]

        result = plugin.get_routing_data(per_page=0)
        assert result["paths"] == []
        assert result["per_page"] == 0
        assert result["total_paths"] == 1
        plugin.stop()
