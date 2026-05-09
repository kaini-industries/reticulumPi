"""Tests for the yggdrasil_transport plugin."""

from __future__ import annotations

import json
import os
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi import events
from reticulumpi.builtin_plugins.yggdrasil_transport import (
    YggdrasilTransportPlugin,
    _RNS_INTERFACE_NAME,
    _BOOTSTRAP_GRACE,
)


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def mock_app():
    """Create a mock ReticulumPiApp."""
    app = MagicMock()
    app.reticulum = MagicMock()
    app.reticulum.configdir = tempfile.mkdtemp()
    app.reticulum_config_dir = None
    app.event_bus = MagicMock()
    return app


@pytest.fixture()
def base_config():
    """Minimal valid config."""
    return {"check_interval": 30, "rns_listen_port": 4242}


@pytest.fixture()
def plugin(mock_app, base_config):
    """Create a YggdrasilTransportPlugin (not started)."""
    return YggdrasilTransportPlugin(mock_app, base_config)


# ── Config Validation ────────────────────────────────────────────────


class TestValidateConfig:
    def test_valid_defaults(self, mock_app):
        """Default config passes validation."""
        p = YggdrasilTransportPlugin(mock_app, {})
        p.validate_config()

    def test_valid_custom(self, mock_app):
        """Custom valid config passes."""
        p = YggdrasilTransportPlugin(
            mock_app, {"check_interval": 60, "rns_listen_port": 5555}
        )
        p.validate_config()

    def test_interval_too_low(self, mock_app):
        with pytest.raises(ValueError, match="check_interval"):
            YggdrasilTransportPlugin(mock_app, {"check_interval": 5})

    def test_interval_wrong_type(self, mock_app):
        with pytest.raises(ValueError, match="check_interval"):
            YggdrasilTransportPlugin(mock_app, {"check_interval": "fast"})

    def test_port_too_low(self, mock_app):
        with pytest.raises(ValueError, match="rns_listen_port"):
            YggdrasilTransportPlugin(mock_app, {"rns_listen_port": 0})

    def test_port_too_high(self, mock_app):
        with pytest.raises(ValueError, match="rns_listen_port"):
            YggdrasilTransportPlugin(mock_app, {"rns_listen_port": 99999})

    def test_port_wrong_type(self, mock_app):
        with pytest.raises(ValueError, match="rns_listen_port"):
            YggdrasilTransportPlugin(mock_app, {"rns_listen_port": "http"})


# ── Lifecycle ────────────────────────────────────────────────────────


class TestLifecycle:
    def test_start_stop(self, plugin):
        """Plugin can start and stop cleanly."""
        plugin.start()
        assert plugin._active is True
        assert len(plugin._threads) == 1
        assert plugin._threads[0].name == "yggdrasil-monitor"

        plugin.stop()
        assert plugin._active is False

    def test_initial_health(self, plugin):
        """Health snapshot is initialised on start."""
        plugin.start()
        health = plugin.get_health()
        assert health["installed"] is False
        assert health["running"] is False
        assert health["address"] is None
        assert health["peer_count"] == 0
        assert health["rns_interface_configured"] is False
        assert health["issues"] == []
        plugin.stop()

    def test_get_status(self, plugin):
        """get_status returns compact summary."""
        plugin.start()
        status = plugin.get_status()
        assert "active" in status
        assert "installed" in status
        assert "running" in status
        assert "address" in status
        assert "peer_count" in status
        assert "issues" in status
        plugin.stop()


# ── Admin API Queries ────────────────────────────────────────────────


class TestAdminAPI:
    def test_query_ctl_success(self, plugin):
        """yggdrasilctl fallback returns parsed JSON."""
        fake_response = {"address": "200:abcd::1", "key": "abc123"}

        with patch("shutil.which", return_value="/usr/bin/yggdrasilctl"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout=json.dumps(fake_response)
                )
                result = plugin._query_ctl("getself")

        assert result == fake_response

    def test_query_ctl_with_envelope(self, plugin):
        """yggdrasilctl returning a response envelope is unwrapped."""
        envelope = {
            "status": "success",
            "response": {"address": "200:1234::1", "key": "def456"},
        }

        with patch("shutil.which", return_value="/usr/bin/yggdrasilctl"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout=json.dumps(envelope)
                )
                result = plugin._query_ctl("getself")

        assert result == {"address": "200:1234::1", "key": "def456"}

    def test_query_ctl_not_installed(self, plugin):
        """Returns None when yggdrasilctl is not found."""
        with patch("shutil.which", return_value=None):
            result = plugin._query_ctl("getself")
        assert result is None

    def test_query_ctl_timeout(self, plugin):
        """Returns None on subprocess timeout."""
        import subprocess as sp

        with patch("shutil.which", return_value="/usr/bin/yggdrasilctl"):
            with patch("subprocess.run", side_effect=sp.TimeoutExpired("x", 10)):
                result = plugin._query_ctl("getself")
        assert result is None

    def test_query_ctl_bad_json(self, plugin):
        """Returns None on invalid JSON output."""
        with patch("shutil.which", return_value="/usr/bin/yggdrasilctl"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="not json"
                )
                result = plugin._query_ctl("getself")
        assert result is None

    def test_query_ctl_nonzero_exit(self, plugin):
        """Returns None on non-zero exit code."""
        with patch("shutil.which", return_value="/usr/bin/yggdrasilctl"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1, stdout="")
                result = plugin._query_ctl("getself")
        assert result is None

    def test_find_admin_socket_configured(self, plugin):
        """Uses configured socket path when it exists."""
        plugin._admin_socket = "/tmp/test.sock"
        with patch("os.path.exists", return_value=True):
            assert plugin._find_admin_socket() == "/tmp/test.sock"

    def test_find_admin_socket_configured_missing(self, plugin):
        """Returns None when configured socket doesn't exist."""
        plugin._admin_socket = "/tmp/nonexistent.sock"
        with patch("os.path.exists", return_value=False):
            assert plugin._find_admin_socket() is None

    def test_find_admin_socket_auto(self, plugin):
        """Auto-detects socket from known paths."""
        plugin._admin_socket = None

        def exists_side_effect(path):
            return path == "/var/run/yggdrasil/yggdrasil.sock"

        with patch("os.path.exists", side_effect=exists_side_effect):
            assert (
                plugin._find_admin_socket()
                == "/var/run/yggdrasil/yggdrasil.sock"
            )

    def test_find_admin_socket_alt_path(self, plugin):
        """Falls back to alternative socket paths."""
        plugin._admin_socket = None

        def exists_side_effect(path):
            return path == "/run/yggdrasil/yggdrasil.sock"

        with patch("os.path.exists", side_effect=exists_side_effect):
            assert (
                plugin._find_admin_socket()
                == "/run/yggdrasil/yggdrasil.sock"
            )

    def test_extract_response_success(self):
        """Extracts response from success envelope."""
        parsed = {
            "status": "success",
            "response": {"address": "200::1"},
        }
        result = YggdrasilTransportPlugin._extract_response(parsed)
        assert result == {"address": "200::1"}

    def test_extract_response_direct(self):
        """Handles direct response (no envelope)."""
        parsed = {"address": "200::1", "key": "abc"}
        result = YggdrasilTransportPlugin._extract_response(parsed)
        assert result == parsed

    def test_extract_response_failure(self):
        """Returns None on error status."""
        parsed = {"status": "error", "error": "bad request"}
        result = YggdrasilTransportPlugin._extract_response(parsed)
        assert result is None

    def test_admin_request_tries_socket_then_ctl(self, plugin):
        """_admin_request tries socket first, falls back to ctl."""
        with patch.object(plugin, "_query_socket", return_value=None):
            with patch.object(
                plugin, "_query_ctl", return_value={"address": "200::1"}
            ) as mock_ctl:
                result = plugin._admin_request("getself")

        assert result == {"address": "200::1"}
        mock_ctl.assert_called_once_with("getself")

    def test_admin_request_socket_success_skips_ctl(self, plugin):
        """Doesn't call ctl when socket succeeds."""
        with patch.object(
            plugin, "_query_socket", return_value={"address": "200::2"}
        ):
            with patch.object(plugin, "_query_ctl") as mock_ctl:
                result = plugin._admin_request("getself")

        assert result == {"address": "200::2"}
        mock_ctl.assert_not_called()


# ── Health Check Logic ───────────────────────────────────────────────


class TestRunCheck:
    def test_not_installed(self, plugin):
        """Reports issue when Yggdrasil is not installed."""
        plugin.start()

        with patch("shutil.which", return_value=None):
            plugin._run_check()

        health = plugin.get_health()
        assert health["installed"] is False
        assert health["running"] is False
        assert any("not installed" in i for i in health["issues"])
        plugin.stop()

    def test_daemon_not_responding(self, plugin):
        """Reports issue when daemon is unreachable."""
        plugin.start()
        plugin._start_time = time.monotonic() - _BOOTSTRAP_GRACE - 1

        with patch("shutil.which", return_value="/usr/bin/yggdrasil"):
            with patch.object(plugin, "_admin_request", return_value=None):
                plugin._run_check()

        health = plugin.get_health()
        assert health["installed"] is True
        assert health["running"] is False
        assert any("not responding" in i for i in health["issues"])
        plugin.stop()

    def test_daemon_bootstrapping(self, plugin):
        """Mentions bootstrapping during grace period."""
        plugin.start()
        plugin._start_time = time.monotonic()  # just started

        with patch("shutil.which", return_value="/usr/bin/yggdrasil"):
            with patch.object(plugin, "_admin_request", return_value=None):
                plugin._run_check()

        health = plugin.get_health()
        assert any("starting up" in i for i in health["issues"])
        plugin.stop()

    def test_online_with_peers(self, plugin):
        """Healthy state with address and peers."""
        plugin.start()

        self_info = {
            "address": "200:abcd:ef01::1",
            "subnet": "300:abcd::/64",
            "key": "abc123def456",
            "build_version": "0.5.13",
            "coords": [1, 2, 3],
        }
        peers = [
            {
                "address": "200:1111::1",
                "remote": "tcp://1.2.3.4:5555",
                "bytes_sent": 1000,
                "bytes_recvd": 2000,
            },
            {
                "address": "200:2222::2",
                "remote": "tcp://5.6.7.8:6666",
                "bytes_sent": 3000,
                "bytes_recvd": 4000,
            },
        ]

        def admin_side_effect(req):
            if req == "getself":
                return self_info
            if req == "getpeers":
                return peers
            return None

        with patch("shutil.which", return_value="/usr/bin/yggdrasil"):
            with patch.object(
                plugin, "_admin_request", side_effect=admin_side_effect
            ):
                with patch.object(plugin, "_check_rns_interface_exists"):
                    plugin._run_check()

        health = plugin.get_health()
        assert health["installed"] is True
        assert health["running"] is True
        assert health["address"] == "200:abcd:ef01::1"
        assert health["subnet"] == "300:abcd::/64"
        assert health["public_key"] == "abc123def456"
        assert health["build_version"] == "0.5.13"
        assert health["peer_count"] == 2
        assert health["traffic"]["bytes_sent"] == 4000
        assert health["traffic"]["bytes_recvd"] == 6000
        assert health["issues"] == []
        plugin.stop()

    def test_zero_peers_after_grace(self, plugin):
        """Issues warning when 0 peers after bootstrap grace."""
        plugin.start()
        plugin._start_time = time.monotonic() - _BOOTSTRAP_GRACE - 1

        self_info = {"address": "200::1", "key": "abc"}

        def admin_side_effect(req):
            if req == "getself":
                return self_info
            if req == "getpeers":
                return []
            return None

        with patch("shutil.which", return_value="/usr/bin/yggdrasil"):
            with patch.object(
                plugin, "_admin_request", side_effect=admin_side_effect
            ):
                with patch.object(plugin, "_check_rns_interface_exists"):
                    plugin._run_check()

        health = plugin.get_health()
        assert any("0 peers" in i for i in health["issues"])
        plugin.stop()

    def test_zero_peers_within_grace_no_warning(self, plugin):
        """No peer warning during bootstrap grace period."""
        plugin.start()
        plugin._start_time = time.monotonic()  # just started

        self_info = {"address": "200::1", "key": "abc"}

        def admin_side_effect(req):
            if req == "getself":
                return self_info
            if req == "getpeers":
                return []
            return None

        with patch("shutil.which", return_value="/usr/bin/yggdrasil"):
            with patch.object(
                plugin, "_admin_request", side_effect=admin_side_effect
            ):
                with patch.object(plugin, "_check_rns_interface_exists"):
                    plugin._run_check()

        health = plugin.get_health()
        assert health["issues"] == []
        plugin.stop()


# ── Event Publishing ─────────────────────────────────────────────────


class TestEvents:
    def test_online_event(self, plugin):
        """YGGDRASIL_ONLINE published on first successful check."""
        plugin.start()
        self_info = {"address": "200::1", "key": "x"}

        def admin_side_effect(req):
            return self_info if req == "getself" else []

        with patch("shutil.which", return_value="/usr/bin/yggdrasil"):
            with patch.object(
                plugin, "_admin_request", side_effect=admin_side_effect
            ):
                with patch.object(plugin, "_check_rns_interface_exists"):
                    plugin._run_check()

        plugin.app.event_bus.publish.assert_any_call(
            events.YGGDRASIL_ONLINE, {"address": "200::1"}
        )
        plugin.stop()

    def test_offline_event(self, plugin):
        """YGGDRASIL_OFFLINE published when daemon goes down."""
        plugin.start()
        plugin._was_online = True  # simulate previously online

        with patch("shutil.which", return_value="/usr/bin/yggdrasil"):
            with patch.object(plugin, "_admin_request", return_value=None):
                plugin._run_check()

        plugin.app.event_bus.publish.assert_any_call(
            events.YGGDRASIL_OFFLINE, {}
        )
        plugin.stop()

    def test_no_offline_event_when_already_offline(self, plugin):
        """No duplicate YGGDRASIL_OFFLINE when already offline."""
        plugin.start()
        plugin._was_online = False

        with patch("shutil.which", return_value=None):
            plugin._run_check()

        # Should NOT have published YGGDRASIL_OFFLINE
        for call_args in plugin.app.event_bus.publish.call_args_list:
            assert call_args[0][0] != events.YGGDRASIL_OFFLINE
        plugin.stop()

    def test_peer_change_event(self, plugin):
        """YGGDRASIL_PEERS_CHANGED published when peer count changes."""
        plugin.start()
        plugin._was_online = True
        plugin._last_peer_count = 2  # had 2 peers

        self_info = {"address": "200::1", "key": "x"}
        peers = [{"address": "200::a", "bytes_sent": 0, "bytes_recvd": 0}]

        def admin_side_effect(req):
            if req == "getself":
                return self_info
            if req == "getpeers":
                return peers
            return None

        with patch("shutil.which", return_value="/usr/bin/yggdrasil"):
            with patch.object(
                plugin, "_admin_request", side_effect=admin_side_effect
            ):
                with patch.object(plugin, "_check_rns_interface_exists"):
                    plugin._run_check()

        plugin.app.event_bus.publish.assert_any_call(
            events.YGGDRASIL_PEERS_CHANGED,
            {"count": 1, "previous": 2},
        )
        plugin.stop()

    def test_no_peer_event_on_first_check(self, plugin):
        """No peer-change event on the first check (unknown → N)."""
        plugin.start()
        assert plugin._last_peer_count == -1  # unknown

        self_info = {"address": "200::1", "key": "x"}
        peers = [{"address": "200::a", "bytes_sent": 0, "bytes_recvd": 0}]

        def admin_side_effect(req):
            if req == "getself":
                return self_info
            if req == "getpeers":
                return peers
            return None

        with patch("shutil.which", return_value="/usr/bin/yggdrasil"):
            with patch.object(
                plugin, "_admin_request", side_effect=admin_side_effect
            ):
                with patch.object(plugin, "_check_rns_interface_exists"):
                    plugin._run_check()

        # Should NOT have published PEERS_CHANGED on first observation
        for call_args in plugin.app.event_bus.publish.call_args_list:
            assert call_args[0][0] != events.YGGDRASIL_PEERS_CHANGED
        plugin.stop()


# ── Public API ───────────────────────────────────────────────────────


class TestPublicAPI:
    def test_get_address(self, plugin):
        """get_address returns address from health."""
        plugin.start()
        with plugin._lock:
            plugin._health["address"] = "200:abcd::1"
        assert plugin.get_address() == "200:abcd::1"
        plugin.stop()

    def test_get_address_none(self, plugin):
        """get_address returns None when not available."""
        plugin.start()
        assert plugin.get_address() is None
        plugin.stop()

    def test_get_peers(self, plugin):
        """get_peers returns a copy of the peer list."""
        plugin.start()
        peers = [{"address": "200::1"}, {"address": "200::2"}]
        with plugin._lock:
            plugin._health["peers"] = peers

        result = plugin.get_peers()
        assert result == peers
        assert result is not peers  # must be a copy
        plugin.stop()

    def test_get_peering_uri(self, plugin):
        """get_peering_uri returns formatted URI when configured."""
        plugin.start()
        with plugin._lock:
            plugin._health["address"] = "200:abcd::1"
            plugin._health["rns_interface_configured"] = True

        uri = plugin.get_peering_uri()
        assert uri == "tcp://[200:abcd::1]:4242"
        plugin.stop()

    def test_get_peering_uri_not_configured(self, plugin):
        """get_peering_uri returns None when interface not configured."""
        plugin.start()
        with plugin._lock:
            plugin._health["address"] = "200:abcd::1"
            plugin._health["rns_interface_configured"] = False

        assert plugin.get_peering_uri() is None
        plugin.stop()

    def test_get_peering_uri_no_address(self, plugin):
        """get_peering_uri returns None without address."""
        plugin.start()
        assert plugin.get_peering_uri() is None
        plugin.stop()

    def test_get_peering_uri_custom_port(self, mock_app):
        """Peering URI uses configured port."""
        plugin = YggdrasilTransportPlugin(
            mock_app, {"rns_listen_port": 5555}
        )
        plugin.start()
        with plugin._lock:
            plugin._health["address"] = "200::1"
            plugin._health["rns_interface_configured"] = True

        assert plugin.get_peering_uri() == "tcp://[200::1]:5555"
        plugin.stop()


# ── RNS Interface Configuration ──────────────────────────────────────


class TestRNSConfig:
    def _write_rns_config(self, path, content):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

    def test_auto_configure_adds_interface(self, plugin):
        """Auto-configure adds TCPServerInterface to Reticulum config."""
        config_path = os.path.join(
            plugin.app.reticulum.configdir, "config"
        )
        self._write_rns_config(
            config_path,
            "[reticulum]\n  enable_transport = True\n\n[interfaces]\n\n"
            "[[Auto Discovery Interface]]\n  type = AutoInterface\n"
            "  enabled = yes\n",
        )

        plugin.start()
        plugin._auto_configure_rns = True
        with plugin._lock:
            plugin._health["address"] = "200:abcd::1"

        plugin._maybe_configure_rns_interface()

        # Verify interface was written
        with open(config_path) as f:
            content = f.read()
        assert _RNS_INTERFACE_NAME in content
        assert "200:abcd::1" in content
        assert "TCPServerInterface" in content

        health = plugin.get_health()
        assert health["rns_interface_configured"] is True

        # Verify event was published
        plugin.app.event_bus.publish.assert_any_call(
            events.YGGDRASIL_RNS_CONFIGURED,
            {
                "interface_name": _RNS_INTERFACE_NAME,
                "address": "200:abcd::1",
                "port": 4242,
            },
        )
        plugin.stop()

    def test_auto_configure_skips_existing(self, plugin):
        """Doesn't duplicate interface if it already exists."""
        config_path = os.path.join(
            plugin.app.reticulum.configdir, "config"
        )
        self._write_rns_config(
            config_path,
            "[reticulum]\n  enable_transport = True\n\n[interfaces]\n\n"
            f"[[{_RNS_INTERFACE_NAME}]]\n  type = TCPServerInterface\n"
            "  enabled = yes\n  listen_ip = 200:old::1\n"
            "  listen_port = 4242\n",
        )

        plugin.start()
        plugin._auto_configure_rns = True
        with plugin._lock:
            plugin._health["address"] = "200:new::1"

        plugin._maybe_configure_rns_interface()

        # Should detect existing and NOT write a new one
        with open(config_path) as f:
            content = f.read()
        assert content.count(_RNS_INTERFACE_NAME) == 1  # only the original
        assert plugin._rns_configured is True
        plugin.stop()

    def test_auto_configure_detects_address_match(self, plugin):
        """Detects interface bound to our address even with different name."""
        config_path = os.path.join(
            plugin.app.reticulum.configdir, "config"
        )
        self._write_rns_config(
            config_path,
            "[reticulum]\n  enable_transport = True\n\n[interfaces]\n\n"
            "[[My Custom Ygg Interface]]\n  type = TCPServerInterface\n"
            "  enabled = yes\n  listen_ip = 200:abcd::1\n"
            "  listen_port = 4242\n",
        )

        plugin.start()
        plugin._auto_configure_rns = True
        with plugin._lock:
            plugin._health["address"] = "200:abcd::1"

        plugin._maybe_configure_rns_interface()

        # Should detect the address match
        assert plugin._rns_configured is True
        plugin.stop()

    def test_check_rns_interface_exists_true(self, plugin):
        """_check_rns_interface_exists detects configured interface."""
        config_path = os.path.join(
            plugin.app.reticulum.configdir, "config"
        )
        self._write_rns_config(
            config_path,
            "[reticulum]\n\n[interfaces]\n\n"
            f"[[{_RNS_INTERFACE_NAME}]]\n  type = TCPServerInterface\n"
            "  enabled = yes\n",
        )

        plugin.start()
        plugin._check_rns_interface_exists()

        assert plugin._rns_configured is True
        assert plugin.get_health()["rns_interface_configured"] is True
        plugin.stop()

    def test_check_rns_interface_exists_false(self, plugin):
        """_check_rns_interface_exists reports missing interface."""
        config_path = os.path.join(
            plugin.app.reticulum.configdir, "config"
        )
        self._write_rns_config(
            config_path,
            "[reticulum]\n\n[interfaces]\n\n"
            "[[Auto Discovery Interface]]\n  type = AutoInterface\n"
            "  enabled = yes\n",
        )

        plugin.start()
        plugin._check_rns_interface_exists()

        assert plugin._rns_configured is False
        assert plugin.get_health()["rns_interface_configured"] is False
        plugin.stop()

    def test_auto_configure_no_config_file(self, plugin):
        """Gracefully handles missing Reticulum config."""
        plugin.start()
        plugin._auto_configure_rns = True
        plugin.app.reticulum.configdir = "/nonexistent/path"
        with plugin._lock:
            plugin._health["address"] = "200::1"

        # Should not raise
        plugin._maybe_configure_rns_interface()
        assert plugin._rns_configured is False
        plugin.stop()
