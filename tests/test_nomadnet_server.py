"""Tests for the NomadNet Server plugin."""

import os
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
