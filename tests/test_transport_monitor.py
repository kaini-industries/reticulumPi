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
    assert status["auto_discovery_enabled"] is False
    assert status["auto_connected"] == 0
    plugin.stop()


def test_get_hub_health(mock_app, base_config):
    plugin = _start_plugin(mock_app, base_config)
    health = plugin.get_hub_health()
    assert len(health["primaries"]) == 1
    assert health["primaries"][0]["name"] == "Primary"
    assert health["fallback_active"] is False
    assert health["active_fallbacks"] == []
    assert health["auto_discovery"]["enabled"] is False
    assert health["auto_discovery"]["connected"] == []
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


# =============================================================================
# Auto-discovery tests
# =============================================================================


@pytest.fixture
def auto_config(base_config):
    """Config with auto-discovery enabled."""
    base_config["auto_discovery"] = {
        "enabled": True,
        "target_connections": 2,
        "probe_interval": 10,
        "cooldown_seconds": 30,
        "max_cooldown_seconds": 120,
        "prefer_diverse_regions": True,
        "extra_hubs": [],
    }
    return base_config


@pytest.fixture
def sample_hub_pool():
    """Sample hub pool data."""
    return [
        {"name": "Hub-A", "target_host": "a.example.com", "target_port": 4242, "region": "na-east"},
        {"name": "Hub-B", "target_host": "b.example.com", "target_port": 4242, "region": "eu-west"},
        {"name": "Hub-C", "target_host": "c.example.com", "target_port": 4242, "region": "asia"},
        {"name": "Hub-D", "target_host": "d.example.com", "target_port": 4242, "region": "na-east"},
    ]


class TestAutoDiscoveryConfig:
    def test_disabled_by_default(self, mock_app, base_config):
        plugin = _start_plugin(mock_app, base_config)
        assert plugin._auto_enabled is False
        plugin.stop()

    def test_enabled_starts_pool_thread(self, mock_app, auto_config):
        plugin = _start_plugin(mock_app, auto_config)
        assert plugin._auto_enabled is True
        # Should have 2 threads: transport-monitor + hub-pool-manager
        assert len(plugin._threads) == 2
        plugin.stop()

    def test_bad_target_connections(self, mock_app, base_config):
        from reticulumpi.builtin_plugins.transport_monitor import TransportMonitorPlugin
        base_config["auto_discovery"] = {"enabled": True, "target_connections": 0}
        with pytest.raises(ValueError, match="target_connections"):
            with patch("RNS.Transport") as mt:
                mt.interfaces = []
                TransportMonitorPlugin(mock_app, base_config)

    def test_bad_probe_interval(self, mock_app, base_config):
        from reticulumpi.builtin_plugins.transport_monitor import TransportMonitorPlugin
        base_config["auto_discovery"] = {"enabled": True, "probe_interval": 5}
        with pytest.raises(ValueError, match="probe_interval"):
            with patch("RNS.Transport") as mt:
                mt.interfaces = []
                TransportMonitorPlugin(mock_app, base_config)

    def test_bad_cooldown_seconds(self, mock_app, base_config):
        from reticulumpi.builtin_plugins.transport_monitor import TransportMonitorPlugin
        base_config["auto_discovery"] = {"enabled": True, "cooldown_seconds": 10}
        with pytest.raises(ValueError, match="cooldown_seconds"):
            with patch("RNS.Transport") as mt:
                mt.interfaces = []
                TransportMonitorPlugin(mock_app, base_config)

    def test_max_cooldown_less_than_cooldown(self, mock_app, base_config):
        from reticulumpi.builtin_plugins.transport_monitor import TransportMonitorPlugin
        base_config["auto_discovery"] = {
            "enabled": True,
            "cooldown_seconds": 300,
            "max_cooldown_seconds": 100,
        }
        with pytest.raises(ValueError, match="max_cooldown_seconds"):
            with patch("RNS.Transport") as mt:
                mt.interfaces = []
                TransportMonitorPlugin(mock_app, base_config)

    def test_bad_extra_hubs_missing_host(self, mock_app, base_config):
        from reticulumpi.builtin_plugins.transport_monitor import TransportMonitorPlugin
        base_config["auto_discovery"] = {
            "enabled": True,
            "extra_hubs": [{"target_port": 4242}],
        }
        with pytest.raises(ValueError, match="extra_hubs.*target_host"):
            with patch("RNS.Transport") as mt:
                mt.interfaces = []
                TransportMonitorPlugin(mock_app, base_config)


class TestLoadHubPool:
    def test_loads_bundled_yaml(self, mock_app, auto_config):
        plugin = _start_plugin(mock_app, auto_config)
        plugin._load_hub_pool()
        assert len(plugin._hub_pool) > 0
        # Verify structure of first hub
        hub = plugin._hub_pool[0]
        assert "target_host" in hub
        assert "target_port" in hub
        assert "name" in hub
        plugin.stop()

    def test_merges_extra_hubs(self, mock_app, auto_config):
        auto_config["auto_discovery"]["extra_hubs"] = [
            {"name": "Extra", "target_host": "extra.example.com", "target_port": 1234},
        ]
        plugin = _start_plugin(mock_app, auto_config)
        plugin._load_hub_pool()
        hosts = [h["target_host"] for h in plugin._hub_pool]
        assert "extra.example.com" in hosts
        plugin.stop()

    def test_custom_hub_list_path(self, mock_app, auto_config, tmp_path):
        hub_file = tmp_path / "custom_hubs.yaml"
        hub_file.write_text(
            "hubs:\n"
            "  - name: Custom\n"
            "    target_host: custom.example.com\n"
            "    target_port: 9999\n"
        )
        auto_config["auto_discovery"]["hub_list_path"] = str(hub_file)
        plugin = _start_plugin(mock_app, auto_config)
        plugin._load_hub_pool()
        assert len(plugin._hub_pool) == 1
        assert plugin._hub_pool[0]["target_host"] == "custom.example.com"
        plugin.stop()

    def test_bad_hub_list_path_logs_error(self, mock_app, auto_config):
        auto_config["auto_discovery"]["hub_list_path"] = "/nonexistent/path.yaml"
        plugin = _start_plugin(mock_app, auto_config)
        plugin._load_hub_pool()
        assert plugin._hub_pool == []  # Falls back to empty
        plugin.stop()


class TestBuildPinnedSet:
    def test_includes_primary_and_fallback_hubs(self, mock_app, auto_config):
        mock_app.reticulum.get_interface_stats.return_value = {"interfaces": []}
        plugin = _start_plugin(mock_app, auto_config)
        plugin._build_pinned_set()
        assert "192.168.1.1:4242" in plugin._pinned_hubs
        assert "10.0.0.1:4242" in plugin._pinned_hubs
        plugin.stop()

    def test_includes_reticulum_interfaces(self, mock_app, auto_config):
        mock_app.reticulum.get_interface_stats.return_value = {
            "interfaces": [
                {
                    "type": "TCPClientInterface",
                    "name": "TCP Client foo/rns.example.com:4242",
                    "target_ip": "1.2.3.4",
                    "target_port": 4242,
                },
            ]
        }
        plugin = _start_plugin(mock_app, auto_config)
        plugin._build_pinned_set()
        assert "1.2.3.4:4242" in plugin._pinned_hubs
        assert "rns.example.com:4242" in plugin._pinned_hubs
        plugin.stop()

    def test_handles_stats_failure(self, mock_app, auto_config):
        mock_app.reticulum.get_interface_stats.side_effect = Exception("RPC error")
        plugin = _start_plugin(mock_app, auto_config)
        plugin._build_pinned_set()
        # Should still have primary/fallback hubs
        assert "192.168.1.1:4242" in plugin._pinned_hubs
        plugin.stop()


class TestDuplicateAvoidance:
    def test_skips_pinned_hubs(self, mock_app, auto_config, sample_hub_pool):
        mock_app.reticulum.get_interface_stats.return_value = {"interfaces": []}
        plugin = _start_plugin(mock_app, auto_config)
        plugin._hub_pool = sample_hub_pool
        plugin._pinned_hubs = {"a.example.com:4242"}

        candidates = plugin._select_candidates(4)
        hosts = [f"{c['target_host']}:{c['target_port']}" for c in candidates]
        assert "a.example.com:4242" not in hosts
        plugin.stop()

    def test_skips_already_connected(self, mock_app, auto_config, sample_hub_pool):
        mock_app.reticulum.get_interface_stats.return_value = {"interfaces": []}
        plugin = _start_plugin(mock_app, auto_config)
        plugin._hub_pool = sample_hub_pool
        plugin._auto_interfaces["a.example.com:4242"] = MagicMock()

        candidates = plugin._select_candidates(4)
        hosts = [f"{c['target_host']}:{c['target_port']}" for c in candidates]
        assert "a.example.com:4242" not in hosts
        plugin.stop()

    def test_skips_hubs_in_cooldown(self, mock_app, auto_config, sample_hub_pool):
        mock_app.reticulum.get_interface_stats.return_value = {"interfaces": []}
        plugin = _start_plugin(mock_app, auto_config)
        plugin._hub_pool = sample_hub_pool
        plugin._hub_cooldowns["b.example.com:4242"] = {
            "until": time.monotonic() + 999,
            "failures": 1,
        }

        candidates = plugin._select_candidates(4)
        hosts = [f"{c['target_host']}:{c['target_port']}" for c in candidates]
        assert "b.example.com:4242" not in hosts
        plugin.stop()


class TestConnectDisconnect:
    def test_connect_auto_hub_success(self, mock_app, auto_config):
        plugin = _start_plugin(mock_app, auto_config)

        hub = {"name": "TestHub", "target_host": "test.com", "target_port": 4242, "region": "na-east"}
        mock_iface = MagicMock()

        received = []
        mock_app.event_bus.subscribe(events.HUB_POOL_CONNECTED, lambda t, d: received.append(d))

        with patch.object(plugin, "_probe_tcp", return_value=True), \
             patch(
                 "reticulumpi.builtin_plugins.transport_monitor.TCPClientInterface",
                 create=True, return_value=mock_iface,
             ), \
             patch("RNS.Transport") as mt:
            mt.interfaces = []
            result = plugin._connect_auto_hub(hub)

        assert result is True
        assert "test.com:4242" in plugin._auto_interfaces
        assert len(received) == 1
        assert received[0]["name"] == "TestHub"
        plugin.stop()

    def test_connect_auto_hub_probe_fail(self, mock_app, auto_config):
        plugin = _start_plugin(mock_app, auto_config)

        hub = {"name": "BadHub", "target_host": "bad.com", "target_port": 4242, "region": "eu-west"}

        with patch.object(plugin, "_probe_tcp", return_value=False):
            result = plugin._connect_auto_hub(hub)

        assert result is False
        assert "bad.com:4242" not in plugin._auto_interfaces
        assert "bad.com:4242" in plugin._hub_cooldowns
        plugin.stop()

    def test_disconnect_auto_hub(self, mock_app, auto_config):
        plugin = _start_plugin(mock_app, auto_config)

        mock_iface = MagicMock()
        mock_iface.name = "Pool-TestHub"
        plugin._auto_interfaces["test.com:4242"] = mock_iface

        received = []
        mock_app.event_bus.subscribe(events.HUB_POOL_DISCONNECTED, lambda t, d: received.append(d))

        with patch("RNS.Transport") as mt:
            mt.interfaces = [mock_iface]
            plugin._disconnect_auto_hub("test.com:4242", "probe_failed")

        assert "test.com:4242" not in plugin._auto_interfaces
        mock_iface.detach.assert_called_once()
        assert len(received) == 1
        assert received[0]["reason"] == "probe_failed"
        assert "test.com:4242" in plugin._hub_cooldowns
        plugin.stop()


class TestCooldownBackoff:
    def test_first_failure_uses_base_cooldown(self, mock_app, auto_config):
        plugin = _start_plugin(mock_app, auto_config)
        before = time.monotonic()
        plugin._update_cooldown("x.com:4242")
        cd = plugin._hub_cooldowns["x.com:4242"]
        assert cd["failures"] == 1
        assert cd["until"] >= before + 30  # cooldown_seconds = 30
        assert cd["until"] <= before + 31
        plugin.stop()

    def test_exponential_backoff(self, mock_app, auto_config):
        plugin = _start_plugin(mock_app, auto_config)
        plugin._update_cooldown("x.com:4242")  # failures=1, backoff=30
        plugin._update_cooldown("x.com:4242")  # failures=2, backoff=60
        plugin._update_cooldown("x.com:4242")  # failures=3, backoff=120 (capped)
        cd = plugin._hub_cooldowns["x.com:4242"]
        assert cd["failures"] == 3
        # 30 * 2^(3-1) = 120 = max_cooldown_seconds
        before = time.monotonic()
        assert cd["until"] >= before + 119
        assert cd["until"] <= before + 121
        plugin.stop()

    def test_backoff_capped_at_max(self, mock_app, auto_config):
        plugin = _start_plugin(mock_app, auto_config)
        for _ in range(10):
            plugin._update_cooldown("x.com:4242")
        cd = plugin._hub_cooldowns["x.com:4242"]
        assert cd["failures"] == 10
        # Should not exceed max_cooldown_seconds (120)
        before = time.monotonic()
        assert cd["until"] <= before + 121
        plugin.stop()

    def test_cooldown_cleared_on_connect(self, mock_app, auto_config):
        plugin = _start_plugin(mock_app, auto_config)
        plugin._hub_cooldowns["test.com:4242"] = {
            "until": time.monotonic() + 999,
            "failures": 5,
        }

        hub = {"name": "Test", "target_host": "test.com", "target_port": 4242, "region": "na-east"}
        mock_iface = MagicMock()

        with patch.object(plugin, "_probe_tcp", return_value=True), \
             patch(
                 "reticulumpi.builtin_plugins.transport_monitor.TCPClientInterface",
                 create=True, return_value=mock_iface,
             ), \
             patch("RNS.Transport") as mt:
            mt.interfaces = []
            plugin._connect_auto_hub(hub)

        assert "test.com:4242" not in plugin._hub_cooldowns
        plugin.stop()


class TestRegionDiversity:
    def test_prefers_unrepresented_regions(self, mock_app, auto_config, sample_hub_pool):
        plugin = _start_plugin(mock_app, auto_config)
        plugin._hub_pool = sample_hub_pool
        # Hub-A (na-east) is already connected
        plugin._auto_interfaces["a.example.com:4242"] = MagicMock()

        candidates = plugin._select_candidates(2)
        # Hub-B (eu-west) and Hub-C (asia) should be preferred over Hub-D (na-east)
        regions = [c.get("region") for c in candidates[:2]]
        # na-east should be ranked last since it's already connected
        assert "eu-west" in regions or "asia" in regions
        plugin.stop()


class TestAutoDiscoveryTick:
    def test_removes_unreachable_and_adds_new(self, mock_app, auto_config, sample_hub_pool):
        plugin = _start_plugin(mock_app, auto_config)
        plugin._hub_pool = sample_hub_pool
        plugin._pinned_hubs = set()

        # Start with one connected hub that will go offline
        mock_dead = MagicMock()
        plugin._auto_interfaces["a.example.com:4242"] = mock_dead

        # Probe results: a.example.com fails, b.example.com succeeds
        def probe_side_effect(host, port, timeout=5):
            if host == "a.example.com":
                return False
            return True

        mock_new_iface = MagicMock()

        with patch.object(plugin, "_probe_tcp", side_effect=probe_side_effect), \
             patch(
                 "reticulumpi.builtin_plugins.transport_monitor.TCPClientInterface",
                 create=True, return_value=mock_new_iface,
             ), \
             patch("RNS.Transport") as mt:
            mt.interfaces = [mock_dead]
            plugin._auto_discovery_tick()

        # Dead hub should be disconnected
        assert "a.example.com:4242" not in plugin._auto_interfaces
        mock_dead.detach.assert_called_once()
        # New hubs should be connected (target=2, had 1 dead, need 2)
        assert len(plugin._auto_interfaces) == 2
        plugin.stop()

    def test_pool_exhausted_event(self, mock_app, auto_config):
        plugin = _start_plugin(mock_app, auto_config)
        plugin._hub_pool = [
            {"name": "Only", "target_host": "only.com", "target_port": 4242, "region": "na-east"},
        ]
        plugin._pinned_hubs = set()
        # Put the only candidate in cooldown
        plugin._hub_cooldowns["only.com:4242"] = {
            "until": time.monotonic() + 999,
            "failures": 1,
        }

        received = []
        mock_app.event_bus.subscribe(events.HUB_POOL_EXHAUSTED, lambda t, d: received.append(d))

        with patch.object(plugin, "_probe_tcp", return_value=False):
            plugin._auto_discovery_tick()

        assert len(received) == 1
        assert received[0]["target"] == 2
        assert received[0]["connected"] == 0
        plugin.stop()


class TestAutoDiscoveryStatus:
    def test_get_status_includes_auto_fields(self, mock_app, auto_config):
        plugin = _start_plugin(mock_app, auto_config)
        status = plugin.get_status()
        assert status["auto_discovery_enabled"] is True
        assert status["auto_target"] == 2
        assert status["auto_connected"] == 0
        assert status["pool_size"] == 0  # Not loaded yet
        assert status["in_cooldown"] == 0
        plugin.stop()

    def test_get_hub_health_includes_pool(self, mock_app, auto_config):
        plugin = _start_plugin(mock_app, auto_config)
        mock_iface = MagicMock()
        mock_iface.name = "Pool-TestHub"
        mock_iface.target_ip = "1.2.3.4"
        mock_iface.target_port = 4242
        plugin._auto_interfaces["test.com:4242"] = mock_iface

        health = plugin.get_hub_health()
        ad = health["auto_discovery"]
        assert ad["enabled"] is True
        assert ad["target_connections"] == 2
        assert len(ad["connected"]) == 1
        assert ad["connected"][0]["key"] == "test.com:4242"
        plugin.stop()


class TestTeardownAutoInterfaces:
    def test_stop_tears_down_pool(self, mock_app, auto_config):
        plugin = _start_plugin(mock_app, auto_config)

        mock_iface1 = MagicMock()
        mock_iface2 = MagicMock()
        plugin._auto_interfaces = {
            "a.com:4242": mock_iface1,
            "b.com:4242": mock_iface2,
        }

        with patch("RNS.Transport") as mt:
            mt.interfaces = [mock_iface1, mock_iface2]
            plugin._teardown_auto_interfaces()

        mock_iface1.detach.assert_called_once()
        mock_iface2.detach.assert_called_once()
        assert len(plugin._auto_interfaces) == 0


class TestHubExchange:
    def test_exchange_setup_creates_destination(self, mock_app, auto_config):
        mock_dest = MagicMock()
        mock_dest.hash = b"\xdd" * 16
        with patch("RNS.Destination", return_value=mock_dest), \
             patch("RNS.Transport") as mt:
            mt.interfaces = []
            mt.register_announce_handler = MagicMock()
            from reticulumpi.builtin_plugins.transport_monitor import TransportMonitorPlugin
            plugin = TransportMonitorPlugin(mock_app, auto_config)
            plugin.start()
        assert plugin._exchange_destination is not None
        assert plugin._announce_handler is not None
        # Should have 3 threads: transport-monitor + hub-pool-manager + hub-exchange
        assert len(plugin._threads) == 3
        plugin.stop()

    def test_handle_hub_request_returns_pool(self, mock_app, auto_config):
        import RNS.vendor.umsgpack as umsgpack
        plugin = _start_plugin(mock_app, auto_config)
        plugin._hub_pool = [
            {"target_host": "a.com", "target_port": 4242, "name": "HubA", "region": "na-east"},
            {"target_host": "b.com", "target_port": 4242, "name": "HubB", "region": "eu-west"},
        ]
        plugin._hub_cooldowns = {}

        result = plugin._handle_hub_request("/hubs", None, None, None, None, None)
        data = umsgpack.unpackb(result)
        assert data["v"] == 1
        assert len(data["hubs"]) == 2
        assert data["hubs"][0]["h"] == "a.com"
        assert data["hubs"][0]["p"] == 4242
        plugin.stop()

    def test_handle_hub_request_excludes_cooldown(self, mock_app, auto_config):
        import RNS.vendor.umsgpack as umsgpack
        plugin = _start_plugin(mock_app, auto_config)
        plugin._hub_pool = [
            {"target_host": "a.com", "target_port": 4242, "name": "HubA", "region": "na-east"},
            {"target_host": "b.com", "target_port": 4242, "name": "HubB", "region": "eu-west"},
        ]
        plugin._hub_cooldowns = {
            "a.com:4242": {"until": time.monotonic() + 999, "failures": 1},
        }

        result = plugin._handle_hub_request("/hubs", None, None, None, None, None)
        data = umsgpack.unpackb(result)
        assert len(data["hubs"]) == 1
        assert data["hubs"][0]["h"] == "b.com"
        plugin.stop()

    def test_merge_exchanged_hubs_adds_new(self, mock_app, auto_config):
        plugin = _start_plugin(mock_app, auto_config)
        plugin._hub_pool = [
            {"target_host": "existing.com", "target_port": 4242, "name": "Existing"},
        ]
        plugin._pinned_hubs = set()

        received = [
            {"h": "new1.com", "p": 4242, "n": "New1", "r": "eu-west"},
            {"h": "new2.com", "p": 1234, "n": "New2", "r": "asia"},
            {"h": "existing.com", "p": 4242, "n": "Dup", "r": "na-east"},  # duplicate
        ]
        added = plugin._merge_exchanged_hubs(received)
        assert added == 2
        assert len(plugin._hub_pool) == 3
        hosts = [f"{h['target_host']}:{h['target_port']}" for h in plugin._hub_pool]
        assert "new1.com:4242" in hosts
        assert "new2.com:1234" in hosts
        plugin.stop()

    def test_merge_skips_pinned(self, mock_app, auto_config):
        plugin = _start_plugin(mock_app, auto_config)
        plugin._hub_pool = []
        plugin._pinned_hubs = {"pinned.com:4242"}

        received = [{"h": "pinned.com", "p": 4242, "n": "Pinned", "r": "na-east"}]
        added = plugin._merge_exchanged_hubs(received)
        assert added == 0
        plugin.stop()

    def test_merge_skips_invalid_entries(self, mock_app, auto_config):
        plugin = _start_plugin(mock_app, auto_config)
        plugin._hub_pool = []
        plugin._pinned_hubs = set()

        received = [
            {"h": "", "p": 4242},  # empty host
            {"h": "ok.com", "p": 0},  # zero port
            {"p": 4242},  # missing host
            {"h": "valid.com", "p": 4242, "n": "Valid", "r": "eu-west"},
        ]
        added = plugin._merge_exchanged_hubs(received)
        assert added == 1
        plugin.stop()

    def test_on_peer_announced(self, mock_app, auto_config):
        plugin = _start_plugin(mock_app, auto_config)
        dest_hash = b"\xaa" * 16
        plugin._on_peer_announced(dest_hash)
        assert dest_hash in plugin._exchange_peers
        assert plugin._exchange_peers[dest_hash] == 0.0  # never queried
        plugin.stop()

    def test_on_peer_announced_skips_self(self, mock_app, auto_config):
        plugin = _start_plugin(mock_app, auto_config)
        # Manually set up exchange destination with a known hash
        mock_dest = MagicMock()
        mock_dest.hash = b"\xdd" * 16
        plugin._exchange_destination = mock_dest

        from reticulumpi.builtin_plugins.transport_monitor import _HubExchangeAnnounceHandler
        handler = _HubExchangeAnnounceHandler(plugin)

        handler.received_announce(
            destination_hash=b"\xdd" * 16,
            announced_identity=MagicMock(),
            app_data=None,
        )
        assert b"\xdd" * 16 not in plugin._exchange_peers
        plugin.stop()

    def test_on_peer_announced_evicts_oldest(self, mock_app, auto_config):
        plugin = _start_plugin(mock_app, auto_config)
        # Fill up to MAX_EXCHANGE_PEERS
        from reticulumpi.builtin_plugins.transport_monitor import _MAX_EXCHANGE_PEERS
        for i in range(_MAX_EXCHANGE_PEERS):
            h = bytes([i]) * 16
            plugin._exchange_peers[h] = float(i)

        assert len(plugin._exchange_peers) == _MAX_EXCHANGE_PEERS

        # Add one more — should evict the oldest (key=b'\x00'*16, value=0.0)
        new_hash = b"\xff" * 16
        plugin._on_peer_announced(new_hash)
        assert len(plugin._exchange_peers) == _MAX_EXCHANGE_PEERS
        assert new_hash in plugin._exchange_peers
        assert bytes([0]) * 16 not in plugin._exchange_peers
        plugin.stop()

    def test_exchange_config_validation(self, mock_app, base_config):
        from reticulumpi.builtin_plugins.transport_monitor import TransportMonitorPlugin
        base_config["auto_discovery"] = {
            "enabled": True,
            "exchange_interval": 30,  # too low, must be >= 60
        }
        with pytest.raises(ValueError, match="exchange_interval"):
            with patch("RNS.Transport") as mt:
                mt.interfaces = []
                TransportMonitorPlugin(mock_app, base_config)

    def test_get_status_includes_exchange_peers(self, mock_app, auto_config):
        plugin = _start_plugin(mock_app, auto_config)
        plugin._exchange_peers[b"\xaa" * 16] = time.monotonic()
        status = plugin.get_status()
        assert status["exchange_peers"] == 1
        plugin.stop()

    def test_get_hub_health_includes_exchange_peers(self, mock_app, auto_config):
        plugin = _start_plugin(mock_app, auto_config)
        plugin._exchange_peers[b"\xaa" * 16] = time.monotonic()
        health = plugin.get_hub_health()
        assert health["auto_discovery"]["exchange_peers"] == 1
        plugin.stop()


class TestBackwardCompatibility:
    def test_no_auto_discovery_config(self, mock_app, base_config):
        """Plugin works exactly as before when auto_discovery is absent."""
        plugin = _start_plugin(mock_app, base_config)
        assert plugin._auto_enabled is False
        assert plugin._hub_pool == []
        assert plugin._auto_interfaces == {}
        # Only 1 thread (transport-monitor), no hub-pool-manager or hub-exchange
        assert len(plugin._threads) == 1
        plugin.stop()

    def test_auto_discovery_disabled_explicitly(self, mock_app, base_config):
        base_config["auto_discovery"] = {"enabled": False}
        plugin = _start_plugin(mock_app, base_config)
        assert plugin._auto_enabled is False
        assert len(plugin._threads) == 1
        plugin.stop()
