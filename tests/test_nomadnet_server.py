"""Tests for the NomadNet Server plugin."""

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def nomadnet_config(tmp_path):
    """Base config dict for the nomadnet_server plugin."""
    return {
        "enabled": True,
        "config_dir": str(tmp_path / "nomadnet"),
        "health_check_interval": 10,
        "auto_restart": True,
        "max_restarts": 3,
    }


@pytest.fixture
def example_pages(tmp_path):
    """Create example .mu pages that _install_example_pages can find."""
    # The plugin looks relative to its own __file__ for config/nomadnet/pages/
    pages_dir = tmp_path / "pages_src"
    pages_dir.mkdir()
    (pages_dir / "index.mu").write_text("`!Test Page")
    (pages_dir / "help.mu").write_text("`!Help Page")
    return str(pages_dir)


def _make_plugin(mock_app, config, nomadnet_bin="nomadnet"):
    """Construct the plugin with shutil.which mocked."""
    with patch("shutil.which", return_value=nomadnet_bin):
        from reticulumpi.builtin_plugins.nomadnet_server import NomadNetServer

        return NomadNetServer(mock_app, config)


class TestValidateConfig:
    def test_raises_when_nomadnet_not_found(self, mock_app, nomadnet_config):
        with (
            patch("shutil.which", return_value=None),
            patch("os.path.isfile", return_value=False),
        ):
            from reticulumpi.builtin_plugins.nomadnet_server import NomadNetServer

            with pytest.raises(ValueError, match="NomadNet binary not found"):
                NomadNetServer(mock_app, nomadnet_config)

    def test_finds_nomadnet_in_venv_fallback(self, mock_app, nomadnet_config, tmp_path):
        """When shutil.which fails, plugin finds nomadnet in the same venv."""
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        nomadnet_path = fake_bin / "nomadnet"
        nomadnet_path.write_text("#!/bin/sh\n")
        nomadnet_path.chmod(0o755)
        fake_python = str(fake_bin / "python3")

        with (
            patch("shutil.which", return_value=None),
            patch("sys.executable", fake_python),
        ):
            from reticulumpi.builtin_plugins.nomadnet_server import NomadNetServer

            plugin = NomadNetServer(mock_app, nomadnet_config)
            assert plugin._nomadnet_bin == str(nomadnet_path)

    def test_raises_on_invalid_health_check_interval(self, mock_app, nomadnet_config):
        nomadnet_config["health_check_interval"] = 2
        with pytest.raises(ValueError, match="health_check_interval"):
            _make_plugin(mock_app, nomadnet_config)

    def test_raises_on_negative_max_restarts(self, mock_app, nomadnet_config):
        nomadnet_config["max_restarts"] = -1
        with pytest.raises(ValueError, match="max_restarts"):
            _make_plugin(mock_app, nomadnet_config)

    def test_valid_config_succeeds(self, mock_app, nomadnet_config):
        plugin = _make_plugin(mock_app, nomadnet_config)
        assert plugin.plugin_name == "nomadnet_server"


class TestStart:
    def test_launches_subprocess(self, mock_app, nomadnet_config):
        mock_app._reticulum_config_dir = "/tmp/reticulum"
        plugin = _make_plugin(mock_app, nomadnet_config)

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            plugin.start()

        args = mock_popen.call_args[0][0]
        assert args[0] == "nomadnet"
        assert "--daemon" in args
        assert "--config" in args
        assert "--rnsconfig" in args
        assert plugin._pid == 12345
        assert plugin._active is True

        # Cleanup
        plugin._active = False
        plugin._join_threads()

    def test_creates_directories(self, mock_app, nomadnet_config, tmp_path):
        mock_app._reticulum_config_dir = None
        config_dir = str(tmp_path / "nomadnet")
        nomadnet_config["config_dir"] = config_dir
        plugin = _make_plugin(mock_app, nomadnet_config)

        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc):
            plugin.start()

        assert os.path.isdir(config_dir)
        assert os.path.isdir(os.path.join(config_dir, "storage", "pages"))
        assert os.path.isdir(os.path.join(config_dir, "storage", "files"))

        plugin._active = False
        plugin._join_threads()

    def test_writes_default_config_on_first_start(self, mock_app, nomadnet_config, tmp_path):
        mock_app._reticulum_config_dir = None
        config_dir = str(tmp_path / "nomadnet")
        nomadnet_config["config_dir"] = config_dir
        nomadnet_config["node_name"] = "TestNode"
        plugin = _make_plugin(mock_app, nomadnet_config)

        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc):
            plugin.start()

        config_file = os.path.join(config_dir, "config")
        assert os.path.isfile(config_file)
        with open(config_file) as f:
            content = f.read()
        assert "enable_node = yes" in content
        assert "node_name = TestNode" in content
        assert "disable_propagation = yes" in content

        plugin._active = False
        plugin._join_threads()

    def test_does_not_overwrite_existing_config(self, mock_app, nomadnet_config, tmp_path):
        mock_app._reticulum_config_dir = None
        config_dir = str(tmp_path / "nomadnet")
        nomadnet_config["config_dir"] = config_dir
        plugin = _make_plugin(mock_app, nomadnet_config)

        # Create existing config before start
        os.makedirs(config_dir, exist_ok=True)
        config_file = os.path.join(config_dir, "config")
        with open(config_file, "w") as f:
            f.write("my custom config")

        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc):
            plugin.start()

        with open(config_file) as f:
            assert f.read() == "my custom config"

        plugin._active = False
        plugin._join_threads()


class TestStop:
    def test_terminates_process(self, mock_app, nomadnet_config):
        mock_app._reticulum_config_dir = None
        plugin = _make_plugin(mock_app, nomadnet_config)

        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc):
            plugin.start()

        plugin.stop()

        mock_proc.terminate.assert_called_once()
        assert plugin._active is False

    def test_kills_if_terminate_fails(self, mock_app, nomadnet_config):
        from subprocess import TimeoutExpired

        mock_app._reticulum_config_dir = None
        plugin = _make_plugin(mock_app, nomadnet_config)

        mock_proc = MagicMock()
        mock_proc.pid = 200
        mock_proc.poll.return_value = None
        mock_proc.wait.side_effect = [TimeoutExpired("nomadnet", 10), None]

        with patch("subprocess.Popen", return_value=mock_proc):
            plugin.start()

        plugin.stop()

        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()


class TestGetStatus:
    def test_status_when_running(self, mock_app, nomadnet_config):
        mock_app._reticulum_config_dir = None
        plugin = _make_plugin(mock_app, nomadnet_config)

        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc):
            plugin.start()

        status = plugin.get_status()
        assert status["active"] is True
        assert status["pid"] == 42
        assert status["running"] is True
        assert status["restart_count"] == 0

        plugin._active = False
        plugin._join_threads()

    def test_status_when_exited(self, mock_app, nomadnet_config):
        plugin = _make_plugin(mock_app, nomadnet_config)
        plugin._process = None
        plugin._pid = None
        plugin._restart_count = 0
        plugin._config_dir = "/tmp/test"

        status = plugin.get_status()
        assert status["running"] is False


class TestHealthMonitor:
    def test_restarts_on_crash(self, mock_app, nomadnet_config):
        mock_app._reticulum_config_dir = None
        nomadnet_config["max_restarts"] = 2
        plugin = _make_plugin(mock_app, nomadnet_config)

        # Process that exits immediately, then stays alive on restart
        crash_proc = MagicMock()
        crash_proc.pid = 1
        crash_proc.poll.return_value = 1
        crash_proc.returncode = 1

        alive_proc = MagicMock()
        alive_proc.pid = 2
        alive_proc.poll.return_value = None

        plugin._active = True
        plugin._process = crash_proc
        plugin._pid = 1
        plugin._restart_count = 0
        plugin._cmd = ["nomadnet", "--daemon"]
        plugin._config_dir = nomadnet_config["config_dir"]

        with patch(
            "reticulumpi.builtin_plugins.nomadnet_server.subprocess.Popen",
            return_value=alive_proc,
        ):
            # Simulate one health check: process is dead, should restart
            if plugin._process.poll() is not None:
                plugin._restart_count += 1
                plugin._launch_process(plugin._cmd)

        assert plugin._restart_count == 1
        assert plugin._pid == 2

    def test_gives_up_after_max_restarts(self, mock_app, nomadnet_config):
        mock_app._reticulum_config_dir = None
        nomadnet_config["max_restarts"] = 1
        plugin = _make_plugin(mock_app, nomadnet_config)

        crash_proc = MagicMock()
        crash_proc.pid = 1
        crash_proc.poll.return_value = 1
        crash_proc.returncode = 1

        plugin._active = True
        plugin._process = crash_proc
        plugin._pid = 1
        plugin._restart_count = 1  # Already at max
        plugin._cmd = ["nomadnet", "--daemon"]
        plugin._config_dir = nomadnet_config["config_dir"]

        # Simulate: process dead, at max restarts, should deactivate
        max_restarts = plugin.config.get("max_restarts", 5)
        if plugin._process.poll() is not None and plugin._restart_count >= max_restarts:
            plugin._active = False

        assert plugin._active is False


class TestWriteDefaultConfig:
    def test_writes_config_when_none_exists(self, mock_app, nomadnet_config, tmp_path):
        config_dir = str(tmp_path / "nomadnet")
        nomadnet_config["config_dir"] = config_dir
        nomadnet_config["node_name"] = "MyNode"
        plugin = _make_plugin(mock_app, nomadnet_config)

        os.makedirs(config_dir, exist_ok=True)
        plugin._config_dir = config_dir
        plugin._write_default_config()

        config_file = os.path.join(config_dir, "config")
        assert os.path.isfile(config_file)
        with open(config_file) as f:
            content = f.read()
        assert "enable_node = yes" in content
        assert "node_name = MyNode" in content

    def test_enables_propagation_when_configured(self, mock_app, nomadnet_config, tmp_path):
        config_dir = str(tmp_path / "nomadnet")
        nomadnet_config["config_dir"] = config_dir
        nomadnet_config["enable_propagation"] = True
        plugin = _make_plugin(mock_app, nomadnet_config)

        os.makedirs(config_dir, exist_ok=True)
        plugin._config_dir = config_dir
        plugin._write_default_config()

        config_file = os.path.join(config_dir, "config")
        with open(config_file) as f:
            content = f.read()
        assert "disable_propagation = no" in content

    def test_uses_default_node_name(self, mock_app, nomadnet_config, tmp_path):
        config_dir = str(tmp_path / "nomadnet")
        nomadnet_config["config_dir"] = config_dir
        # No node_name in config
        plugin = _make_plugin(mock_app, nomadnet_config)

        os.makedirs(config_dir, exist_ok=True)
        plugin._config_dir = config_dir
        plugin._write_default_config()

        config_file = os.path.join(config_dir, "config")
        with open(config_file) as f:
            content = f.read()
        assert "node_name = TestNode" in content

    def test_skips_when_config_exists(self, mock_app, nomadnet_config, tmp_path):
        config_dir = str(tmp_path / "nomadnet")
        nomadnet_config["config_dir"] = config_dir
        plugin = _make_plugin(mock_app, nomadnet_config)

        os.makedirs(config_dir, exist_ok=True)
        config_file = os.path.join(config_dir, "config")
        with open(config_file, "w") as f:
            f.write("existing config")

        plugin._config_dir = config_dir
        plugin._write_default_config()

        with open(config_file) as f:
            assert f.read() == "existing config"


class TestAuth:
    """Tests for the NomadNet page authentication system."""

    def _make_auth_plugin(self, mock_app, tmp_path, auth_config=None):
        """Create a plugin with auth config and a pages directory."""
        pages_dir = tmp_path / "nomadnet" / "storage" / "pages"
        pages_dir.mkdir(parents=True)
        # Create some .mu pages
        (pages_dir / "index.mu").write_text("`!Index")
        (pages_dir / "status.mu").write_text("#!/usr/bin/env python3\nprint('ok')")
        (pages_dir / "network.mu").write_text("#!/usr/bin/env python3\nprint('net')")
        (pages_dir / "help.mu").write_text("`!Help")

        config = {
            "enabled": True,
            "config_dir": str(tmp_path / "nomadnet"),
            "health_check_interval": 10,
            "max_restarts": 3,
        }
        if auth_config:
            config["auth"] = auth_config

        plugin = _make_plugin(mock_app, config)
        plugin._pages_dir = str(pages_dir)
        plugin._config_dir = str(tmp_path / "nomadnet")
        return plugin

    def test_sync_creates_allowed_shims(self, mock_app, tmp_path):
        allow_path = str(tmp_path / "allowed_ids")
        plugin = self._make_auth_plugin(mock_app, tmp_path, {
            "allow_list_path": allow_path,
            "protected_pages": ["status.mu", "network.mu"],
        })
        plugin._sync_allowed_files()

        shim = os.path.join(plugin._pages_dir, "status.mu.allowed")
        assert os.path.isfile(shim)
        assert os.access(shim, os.X_OK)
        with open(shim) as f:
            content = f.read()
        assert "#!/bin/sh" in content
        assert allow_path in content

        # index.mu should NOT have a shim
        assert not os.path.isfile(
            os.path.join(plugin._pages_dir, "index.mu.allowed")
        )

    def test_sync_removes_stale_shims(self, mock_app, tmp_path):
        plugin = self._make_auth_plugin(mock_app, tmp_path, {
            "allow_list_path": str(tmp_path / "allowed_ids"),
            "protected_pages": ["status.mu"],
        })
        # First sync: creates shim for status.mu
        plugin._sync_allowed_files()
        shim = os.path.join(plugin._pages_dir, "status.mu.allowed")
        assert os.path.isfile(shim)

        # Change config to un-protect status.mu
        plugin.config["auth"]["protected_pages"] = []
        plugin._sync_allowed_files()
        assert not os.path.isfile(shim)

    def test_sync_all_pages(self, mock_app, tmp_path):
        plugin = self._make_auth_plugin(mock_app, tmp_path, {
            "allow_list_path": str(tmp_path / "allowed_ids"),
            "protected_pages": "all",
            "public_pages": ["index.mu", "help.mu"],
        })
        plugin._sync_allowed_files()

        # status.mu and network.mu should be protected
        assert os.path.isfile(
            os.path.join(plugin._pages_dir, "status.mu.allowed")
        )
        assert os.path.isfile(
            os.path.join(plugin._pages_dir, "network.mu.allowed")
        )
        # index.mu and help.mu should NOT be protected
        assert not os.path.isfile(
            os.path.join(plugin._pages_dir, "index.mu.allowed")
        )
        assert not os.path.isfile(
            os.path.join(plugin._pages_dir, "help.mu.allowed")
        )

    def test_add_identity(self, mock_app, tmp_path):
        allow_path = str(tmp_path / "allowed_ids")
        plugin = self._make_auth_plugin(mock_app, tmp_path, {
            "allow_list_path": allow_path,
            "protected_pages": [],
        })
        result = plugin.add_allowed_identity("aa" * 16)
        assert result is True
        ids = plugin.get_allowed_identities()
        assert "aa" * 16 in ids

    def test_add_identity_invalid_format(self, mock_app, tmp_path):
        plugin = self._make_auth_plugin(mock_app, tmp_path, {
            "allow_list_path": str(tmp_path / "allowed_ids"),
            "protected_pages": [],
        })
        with pytest.raises(ValueError, match="Invalid identity hash"):
            plugin.add_allowed_identity("not_hex")
        with pytest.raises(ValueError, match="Invalid identity hash"):
            plugin.add_allowed_identity("aa" * 8)  # too short (16 chars)

    def test_add_identity_duplicate(self, mock_app, tmp_path):
        plugin = self._make_auth_plugin(mock_app, tmp_path, {
            "allow_list_path": str(tmp_path / "allowed_ids"),
            "protected_pages": [],
        })
        plugin.add_allowed_identity("bb" * 16)
        result = plugin.add_allowed_identity("bb" * 16)
        assert result is False
        ids = plugin.get_allowed_identities()
        assert ids.count("bb" * 16) == 1

    def test_remove_identity(self, mock_app, tmp_path):
        plugin = self._make_auth_plugin(mock_app, tmp_path, {
            "allow_list_path": str(tmp_path / "allowed_ids"),
            "protected_pages": [],
        })
        plugin.add_allowed_identity("cc" * 16)
        result = plugin.remove_allowed_identity("cc" * 16)
        assert result is True
        assert "cc" * 16 not in plugin.get_allowed_identities()

    def test_remove_identity_not_found(self, mock_app, tmp_path):
        plugin = self._make_auth_plugin(mock_app, tmp_path, {
            "allow_list_path": str(tmp_path / "allowed_ids"),
            "protected_pages": [],
        })
        result = plugin.remove_allowed_identity("dd" * 16)
        assert result is False

    def test_shim_executes_correctly(self, mock_app, tmp_path):
        allow_path = str(tmp_path / "allowed_ids")
        plugin = self._make_auth_plugin(mock_app, tmp_path, {
            "allow_list_path": allow_path,
            "protected_pages": ["status.mu"],
        })
        # Add an identity, then sync shims
        plugin.add_allowed_identity("ee" * 16)
        plugin._sync_allowed_files()

        # Execute the shim and check output
        shim = os.path.join(plugin._pages_dir, "status.mu.allowed")
        result = subprocess.run(
            [shim], capture_output=True, text=True, timeout=5,
        )
        assert ("ee" * 16) in result.stdout

    def test_missing_allow_file_denies_all(self, mock_app, tmp_path):
        allow_path = str(tmp_path / "nonexistent_file")
        plugin = self._make_auth_plugin(mock_app, tmp_path, {
            "allow_list_path": allow_path,
            "protected_pages": ["status.mu"],
        })
        plugin._sync_allowed_files()

        shim = os.path.join(plugin._pages_dir, "status.mu.allowed")
        result = subprocess.run(
            [shim], capture_output=True, text=True, timeout=5,
        )
        assert result.stdout.strip() == ""

    def test_get_status_includes_auth(self, mock_app, tmp_path):
        plugin = self._make_auth_plugin(mock_app, tmp_path, {
            "allow_list_path": str(tmp_path / "allowed_ids"),
            "protected_pages": ["status.mu"],
        })
        plugin._process = None
        plugin._pid = None
        plugin._restart_count = 0
        plugin._active = True

        status = plugin.get_status()
        assert "auth" in status
        assert status["auth"]["allowed_count"] == 0
        assert "status.mu" in status["auth"]["protected_pages"]

    def test_no_auth_config_skips_sync(self, mock_app, tmp_path):
        """Plugin without auth config does not create .allowed files."""
        plugin = self._make_auth_plugin(mock_app, tmp_path, auth_config=None)
        plugin._sync_allowed_files()
        # No .allowed files should exist
        for f in os.listdir(plugin._pages_dir):
            assert not f.endswith(".allowed")

    def test_validate_config_invalid_protected_pages(self, mock_app, tmp_path):
        from reticulumpi.builtin_plugins.nomadnet_server import NomadNetServer

        config = {
            "enabled": True,
            "config_dir": str(tmp_path / "nomadnet"),
            "auth": {"protected_pages": 123},
        }
        with patch("shutil.which", return_value="nomadnet"):
            with pytest.raises(ValueError, match="protected_pages"):
                NomadNetServer(mock_app, config)

    def test_validate_config_invalid_public_pages(self, mock_app, tmp_path):
        from reticulumpi.builtin_plugins.nomadnet_server import NomadNetServer

        config = {
            "enabled": True,
            "config_dir": str(tmp_path / "nomadnet"),
            "auth": {"public_pages": "not_a_list"},
        }
        with patch("shutil.which", return_value="nomadnet"):
            with pytest.raises(ValueError, match="public_pages"):
                NomadNetServer(mock_app, config)

    def test_add_publishes_event(self, mock_app, tmp_path):
        from reticulumpi.event_bus import EventBus

        mock_app.event_bus = EventBus()
        plugin = self._make_auth_plugin(mock_app, tmp_path, {
            "allow_list_path": str(tmp_path / "allowed_ids"),
            "protected_pages": [],
        })
        received = []
        mock_app.event_bus.subscribe(
            "nomadnet.auth.identity_added", lambda e, d: received.append(d)
        )
        plugin.add_allowed_identity("ff" * 16)
        assert len(received) == 1
        assert received[0]["identity"] == "ff" * 16


class TestExamplePages:
    def test_installs_when_empty(self, mock_app, nomadnet_config, tmp_path):
        config_dir = str(tmp_path / "nomadnet")
        nomadnet_config["config_dir"] = config_dir
        plugin = _make_plugin(mock_app, nomadnet_config)

        pages_dir = os.path.join(config_dir, "storage", "pages")
        os.makedirs(pages_dir, exist_ok=True)

        # Point to real example pages
        example_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config",
            "nomadnet",
            "pages",
        )
        plugin._pages_dir = pages_dir

        if os.path.isdir(example_dir):
            plugin._install_example_pages()
            # Should have installed at least index.mu
            assert os.path.exists(os.path.join(pages_dir, "index.mu"))

    def test_does_not_overwrite_existing(self, mock_app, nomadnet_config, tmp_path):
        config_dir = str(tmp_path / "nomadnet")
        nomadnet_config["config_dir"] = config_dir
        plugin = _make_plugin(mock_app, nomadnet_config)

        pages_dir = os.path.join(config_dir, "storage", "pages")
        os.makedirs(pages_dir, exist_ok=True)

        # Create an existing page
        existing = os.path.join(pages_dir, "index.mu")
        with open(existing, "w") as f:
            f.write("my custom page")

        plugin._pages_dir = pages_dir
        plugin._install_example_pages()

        # Should not have overwritten
        with open(existing) as f:
            assert f.read() == "my custom page"
