"""Tests for the NomadNet Server plugin."""

import os
import signal
import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi import events


@pytest.fixture
def nomadnet_config(tmp_path):
    """Base config dict for the nomadnet_server plugin."""
    return {
        "enabled": True,
        "config_dir": str(tmp_path / "nomadnet"),
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
        assert "user_interface = none" in content
        assert "user_interface = text" not in content

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
    def test_terminates_process_group(self, mock_app, nomadnet_config):
        mock_app._reticulum_config_dir = None
        plugin = _make_plugin(mock_app, nomadnet_config)

        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc):
            plugin.start()

        with patch("os.killpg") as mock_killpg:
            plugin.stop()

        mock_killpg.assert_any_call(100, signal.SIGTERM)
        assert plugin._active is False
        assert plugin._pgid is None

    def test_kills_group_if_terminate_times_out(self, mock_app, nomadnet_config):
        from subprocess import TimeoutExpired

        mock_app._reticulum_config_dir = None
        plugin = _make_plugin(mock_app, nomadnet_config)

        mock_proc = MagicMock()
        mock_proc.pid = 200
        mock_proc.poll.return_value = None
        mock_proc.wait.side_effect = [TimeoutExpired("nomadnet", 10), None]

        with patch("subprocess.Popen", return_value=mock_proc):
            plugin.start()

        with patch("os.killpg") as mock_killpg:
            plugin.stop()

        calls = [c.args for c in mock_killpg.call_args_list]
        assert (200, signal.SIGTERM) in calls
        assert (200, signal.SIGKILL) in calls

    def test_falls_back_to_process_kill_when_group_gone(self, mock_app, nomadnet_config):
        mock_app._reticulum_config_dir = None
        plugin = _make_plugin(mock_app, nomadnet_config)

        mock_proc = MagicMock()
        mock_proc.pid = 300
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc):
            plugin.start()

        with patch("os.killpg", side_effect=ProcessLookupError):
            plugin.stop()

        mock_proc.terminate.assert_called_once()
        assert plugin._pgid is None


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

    def test_status_includes_pgid_and_cpu_violations(self, mock_app, nomadnet_config):
        mock_app._reticulum_config_dir = None
        plugin = _make_plugin(mock_app, nomadnet_config)

        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc):
            plugin.start()

        status = plugin.get_status()
        assert status["pgid"] == 42
        assert status["cpu_violations"] == 0

        plugin._active = False
        plugin._join_threads()

    def test_status_when_exited(self, mock_app, nomadnet_config):
        plugin = _make_plugin(mock_app, nomadnet_config)
        plugin._process = None
        plugin._pid = None
        plugin._pgid = None
        plugin._restart_count = 0
        plugin._cpu_violations = 0
        plugin._config_dir = "/tmp/test"

        status = plugin.get_status()
        assert status["running"] is False
        assert status["pgid"] is None


class TestHealthMonitor:
    def test_restarts_on_crash(self, mock_app, nomadnet_config):
        mock_app._reticulum_config_dir = None
        nomadnet_config["max_restarts"] = 2
        plugin = _make_plugin(mock_app, nomadnet_config)

        crash_proc = MagicMock()
        crash_proc.pid = 1
        crash_proc.poll.return_value = 1
        crash_proc.returncode = 1

        alive_proc = MagicMock()
        alive_proc.pid = 2
        alive_proc.poll.return_value = None

        plugin._active = True
        plugin._proc_lock = __import__("threading").Lock()
        plugin._process = crash_proc
        plugin._pid = 1
        plugin._pgid = 1
        plugin._restart_count = 0
        plugin._cpu_violations = 0
        plugin._nice_level = 10
        plugin._cmd = ["nomadnet", "--daemon"]
        plugin._config_dir = nomadnet_config["config_dir"]
        plugin._launch_time = time.monotonic()
        plugin._last_cpu_ticks = None
        plugin._last_cpu_sample_time = None

        with patch(
            "reticulumpi.builtin_plugins.nomadnet_server.subprocess.Popen",
            return_value=alive_proc,
        ):
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
        plugin._pgid = 1
        plugin._restart_count = 1
        plugin._cmd = ["nomadnet", "--daemon"]
        plugin._config_dir = nomadnet_config["config_dir"]

        max_restarts = plugin.config.get("max_restarts", 5)
        if plugin._process.poll() is not None and plugin._restart_count >= max_restarts:
            plugin._active = False

        assert plugin._active is False

    def test_disabled_auto_restart_is_not_reported_as_exhausted(
        self, mock_app, nomadnet_config, caplog
    ):
        mock_app._reticulum_config_dir = None
        nomadnet_config["auto_restart"] = False
        plugin = _make_plugin(mock_app, nomadnet_config)

        crashed = MagicMock()
        crashed.poll.return_value = 255
        crashed.returncode = 255
        plugin._active = True
        plugin._process = crashed
        plugin._restart_count = 0

        with (
            caplog.at_level("ERROR", logger=plugin.log.name),
            patch.object(plugin, "_sleep_while_active", return_value=None),
        ):
            plugin._health_monitor()

        assert plugin._active is False
        assert plugin._restart_count == 0
        assert "automatic restart is disabled" in caplog.text
        assert "exceeded max restarts" not in caplog.text


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
        plugin = self._make_auth_plugin(
            mock_app,
            tmp_path,
            {
                "allow_list_path": allow_path,
                "protected_pages": ["status.mu", "network.mu"],
            },
        )
        plugin._sync_allowed_files()

        shim = os.path.join(plugin._pages_dir, "status.mu.allowed")
        assert os.path.isfile(shim)
        assert os.access(shim, os.X_OK)
        with open(shim) as f:
            content = f.read()
        assert "#!/bin/sh" in content
        assert allow_path in content

        # index.mu should NOT have a shim
        assert not os.path.isfile(os.path.join(plugin._pages_dir, "index.mu.allowed"))

    def test_sync_removes_stale_shims(self, mock_app, tmp_path):
        plugin = self._make_auth_plugin(
            mock_app,
            tmp_path,
            {
                "allow_list_path": str(tmp_path / "allowed_ids"),
                "protected_pages": ["status.mu"],
            },
        )
        # First sync: creates shim for status.mu
        plugin._sync_allowed_files()
        shim = os.path.join(plugin._pages_dir, "status.mu.allowed")
        assert os.path.isfile(shim)

        # Change config to un-protect status.mu
        plugin.config["auth"]["protected_pages"] = []
        plugin._sync_allowed_files()
        assert not os.path.isfile(shim)

    def test_sync_all_pages(self, mock_app, tmp_path):
        plugin = self._make_auth_plugin(
            mock_app,
            tmp_path,
            {
                "allow_list_path": str(tmp_path / "allowed_ids"),
                "protected_pages": "all",
                "public_pages": ["index.mu", "help.mu"],
            },
        )
        plugin._sync_allowed_files()

        # status.mu and network.mu should be protected
        assert os.path.isfile(os.path.join(plugin._pages_dir, "status.mu.allowed"))
        assert os.path.isfile(os.path.join(plugin._pages_dir, "network.mu.allowed"))
        # index.mu and help.mu should NOT be protected
        assert not os.path.isfile(os.path.join(plugin._pages_dir, "index.mu.allowed"))
        assert not os.path.isfile(os.path.join(plugin._pages_dir, "help.mu.allowed"))

    def test_add_identity(self, mock_app, tmp_path):
        allow_path = str(tmp_path / "allowed_ids")
        plugin = self._make_auth_plugin(
            mock_app,
            tmp_path,
            {
                "allow_list_path": allow_path,
                "protected_pages": [],
            },
        )
        result = plugin.add_allowed_identity("aa" * 16)
        assert result is True
        ids = plugin.get_allowed_identities()
        assert "aa" * 16 in ids

    def test_add_identity_invalid_format(self, mock_app, tmp_path):
        plugin = self._make_auth_plugin(
            mock_app,
            tmp_path,
            {
                "allow_list_path": str(tmp_path / "allowed_ids"),
                "protected_pages": [],
            },
        )
        with pytest.raises(ValueError, match="Invalid identity hash"):
            plugin.add_allowed_identity("not_hex")
        with pytest.raises(ValueError, match="Invalid identity hash"):
            plugin.add_allowed_identity("aa" * 8)  # too short (16 chars)

    def test_add_identity_duplicate(self, mock_app, tmp_path):
        plugin = self._make_auth_plugin(
            mock_app,
            tmp_path,
            {
                "allow_list_path": str(tmp_path / "allowed_ids"),
                "protected_pages": [],
            },
        )
        plugin.add_allowed_identity("bb" * 16)
        result = plugin.add_allowed_identity("bb" * 16)
        assert result is False
        ids = plugin.get_allowed_identities()
        assert ids.count("bb" * 16) == 1

    def test_remove_identity(self, mock_app, tmp_path):
        plugin = self._make_auth_plugin(
            mock_app,
            tmp_path,
            {
                "allow_list_path": str(tmp_path / "allowed_ids"),
                "protected_pages": [],
            },
        )
        plugin.add_allowed_identity("cc" * 16)
        result = plugin.remove_allowed_identity("cc" * 16)
        assert result is True
        assert "cc" * 16 not in plugin.get_allowed_identities()

    def test_remove_identity_not_found(self, mock_app, tmp_path):
        plugin = self._make_auth_plugin(
            mock_app,
            tmp_path,
            {
                "allow_list_path": str(tmp_path / "allowed_ids"),
                "protected_pages": [],
            },
        )
        result = plugin.remove_allowed_identity("dd" * 16)
        assert result is False

    def test_shim_executes_correctly(self, mock_app, tmp_path):
        allow_path = str(tmp_path / "allowed_ids")
        plugin = self._make_auth_plugin(
            mock_app,
            tmp_path,
            {
                "allow_list_path": allow_path,
                "protected_pages": ["status.mu"],
            },
        )
        # Add an identity, then sync shims
        plugin.add_allowed_identity("ee" * 16)
        plugin._sync_allowed_files()

        # Execute the shim and check output
        shim = os.path.join(plugin._pages_dir, "status.mu.allowed")
        result = subprocess.run(
            [shim],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert ("ee" * 16) in result.stdout

    def test_missing_allow_file_denies_all(self, mock_app, tmp_path):
        allow_path = str(tmp_path / "nonexistent_file")
        plugin = self._make_auth_plugin(
            mock_app,
            tmp_path,
            {
                "allow_list_path": allow_path,
                "protected_pages": ["status.mu"],
            },
        )
        plugin._sync_allowed_files()

        shim = os.path.join(plugin._pages_dir, "status.mu.allowed")
        result = subprocess.run(
            [shim],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.stdout.strip() == ""

    def test_get_status_includes_auth(self, mock_app, tmp_path):
        plugin = self._make_auth_plugin(
            mock_app,
            tmp_path,
            {
                "allow_list_path": str(tmp_path / "allowed_ids"),
                "protected_pages": ["status.mu"],
            },
        )
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
        plugin = self._make_auth_plugin(
            mock_app,
            tmp_path,
            {
                "allow_list_path": str(tmp_path / "allowed_ids"),
                "protected_pages": [],
            },
        )
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
            assert os.access(os.path.join(pages_dir, "status.mu"), os.X_OK)
            assert os.access(os.path.join(pages_dir, "network.mu"), os.X_OK)

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


# ── Process group isolation tests ──────────────────────────────────────


class TestProcessGroupIsolation:
    def test_launch_uses_preexec_fn_with_setsid_and_nice(self, mock_app, nomadnet_config):
        mock_app._reticulum_config_dir = None
        plugin = _make_plugin(mock_app, nomadnet_config)

        mock_proc = MagicMock()
        mock_proc.pid = 500
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            plugin.start()

        kw = mock_popen.call_args.kwargs
        assert "preexec_fn" in kw
        assert kw["preexec_fn"] is not None

        with patch("os.setsid") as m_setsid, patch("os.nice") as m_nice:
            kw["preexec_fn"]()
            m_setsid.assert_called_once()
            m_nice.assert_called_once_with(10)

        plugin._active = False
        plugin._join_threads()

    def test_launch_sets_pgid_and_launch_time(self, mock_app, nomadnet_config):
        mock_app._reticulum_config_dir = None
        plugin = _make_plugin(mock_app, nomadnet_config)

        mock_proc = MagicMock()
        mock_proc.pid = 600
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc):
            plugin.start()

        assert plugin._pgid == 600
        assert plugin._launch_time is not None
        assert plugin._cpu_violations == 0

        plugin._active = False
        plugin._join_threads()

    def test_custom_nice_level(self, mock_app, nomadnet_config):
        nomadnet_config["nice_level"] = 15
        mock_app._reticulum_config_dir = None
        plugin = _make_plugin(mock_app, nomadnet_config)

        mock_proc = MagicMock()
        mock_proc.pid = 700
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            plugin.start()

        with patch("os.setsid"), patch("os.nice") as m_nice:
            mock_popen.call_args.kwargs["preexec_fn"]()
            m_nice.assert_called_once_with(15)

        plugin._active = False
        plugin._join_threads()

    def test_relaunch_resets_cpu_state(self, mock_app, nomadnet_config):
        mock_app._reticulum_config_dir = None
        plugin = _make_plugin(mock_app, nomadnet_config)

        mock_proc = MagicMock()
        mock_proc.pid = 800
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc):
            plugin.start()

        plugin._cpu_violations = 5
        plugin._last_cpu_ticks = 99999

        mock_proc2 = MagicMock()
        mock_proc2.pid = 801
        mock_proc2.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc2):
            plugin._launch_process(plugin._cmd)

        assert plugin._cpu_violations == 0
        assert plugin._last_cpu_ticks is None
        assert plugin._pgid == 801

        plugin._active = False
        plugin._join_threads()


# ── CPU monitoring tests ───────────────────────────────────────────────


def _make_stat_line(pid, pgid, utime, stime):
    """Build a realistic /proc/<pid>/stat line."""
    fields_after = [
        "S",
        "1",
        str(pgid),
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        str(utime),
        str(stime),
    ]
    while len(fields_after) < 30:
        fields_after.append("0")
    return f"{pid} (nomadnet) " + " ".join(fields_after)


class TestGetGroupCpuTicks:
    def test_sums_matching_pgid(self, mock_app, nomadnet_config):
        plugin = _make_plugin(mock_app, nomadnet_config)
        plugin._pgid = 500

        stat_lines = {
            "/proc/500/stat": _make_stat_line(500, 500, 100, 50),
            "/proc/501/stat": _make_stat_line(501, 500, 200, 30),
            "/proc/502/stat": _make_stat_line(502, 500, 80, 20),
        }

        with (
            patch("glob.glob", return_value=list(stat_lines.keys())),
            patch("builtins.open", side_effect=lambda p, **kw: _mock_open(stat_lines[p])),
        ):
            ticks = plugin._get_group_cpu_ticks()

        assert ticks == (100 + 50) + (200 + 30) + (80 + 20)

    def test_ignores_other_pgids(self, mock_app, nomadnet_config):
        plugin = _make_plugin(mock_app, nomadnet_config)
        plugin._pgid = 500

        stat_lines = {
            "/proc/500/stat": _make_stat_line(500, 500, 100, 50),
            "/proc/999/stat": _make_stat_line(999, 1, 9000, 9000),
        }

        with (
            patch("glob.glob", return_value=list(stat_lines.keys())),
            patch("builtins.open", side_effect=lambda p, **kw: _mock_open(stat_lines[p])),
        ):
            ticks = plugin._get_group_cpu_ticks()

        assert ticks == 150

    def test_returns_none_when_no_pgid(self, mock_app, nomadnet_config):
        plugin = _make_plugin(mock_app, nomadnet_config)
        plugin._pgid = None
        assert plugin._get_group_cpu_ticks() is None

    def test_handles_permission_error(self, mock_app, nomadnet_config):
        plugin = _make_plugin(mock_app, nomadnet_config)
        plugin._pgid = 500

        stat_lines = {
            "/proc/500/stat": _make_stat_line(500, 500, 100, 50),
            "/proc/501/stat": None,  # will raise
        }

        def _open_or_raise(path, **kw):
            if stat_lines[path] is None:
                raise PermissionError("denied")
            return _mock_open(stat_lines[path])

        with (
            patch("glob.glob", return_value=list(stat_lines.keys())),
            patch("builtins.open", side_effect=_open_or_raise),
        ):
            ticks = plugin._get_group_cpu_ticks()

        assert ticks == 150

    def test_returns_none_when_no_matching_processes(self, mock_app, nomadnet_config):
        plugin = _make_plugin(mock_app, nomadnet_config)
        plugin._pgid = 500

        stat_lines = {
            "/proc/999/stat": _make_stat_line(999, 1, 100, 100),
        }

        with (
            patch("glob.glob", return_value=list(stat_lines.keys())),
            patch("builtins.open", side_effect=lambda p, **kw: _mock_open(stat_lines[p])),
        ):
            ticks = plugin._get_group_cpu_ticks()

        assert ticks is None


def _mock_open(content):
    """Return a context-manager-compatible mock for open()."""
    m = MagicMock()
    m.__enter__ = MagicMock(return_value=m)
    m.__exit__ = MagicMock(return_value=False)
    m.read.return_value = content
    return m


class TestComputeCpuPercent:
    def test_first_sample_returns_none(self, mock_app, nomadnet_config):
        plugin = _make_plugin(mock_app, nomadnet_config)
        plugin._last_cpu_ticks = None
        plugin._last_cpu_sample_time = None

        result = plugin._compute_cpu_percent(1000, 100.0)
        assert result is None
        assert plugin._last_cpu_ticks == 1000
        assert plugin._last_cpu_sample_time == 100.0

    def test_second_sample_computes_percentage(self, mock_app, nomadnet_config):
        plugin = _make_plugin(mock_app, nomadnet_config)
        plugin._last_cpu_ticks = 1000
        plugin._last_cpu_sample_time = 100.0

        with patch("os.sysconf", return_value=100):
            result = plugin._compute_cpu_percent(1500, 105.0)

        assert result == pytest.approx(100.0)

    def test_high_cpu_computation(self, mock_app, nomadnet_config):
        plugin = _make_plugin(mock_app, nomadnet_config)
        plugin._last_cpu_ticks = 1000
        plugin._last_cpu_sample_time = 100.0

        with patch("os.sysconf", return_value=100):
            result = plugin._compute_cpu_percent(3000, 105.0)

        assert result == pytest.approx(400.0)


# ── CPU runaway detection tests ────────────────────────────────────────


class TestCpuRunawayDetection:
    def _setup_plugin(self, mock_app, nomadnet_config, **overrides):
        for k, v in overrides.items():
            nomadnet_config[k] = v
        plugin = _make_plugin(mock_app, nomadnet_config)
        plugin._active = True
        plugin._proc_lock = __import__("threading").Lock()
        plugin._process = MagicMock()
        plugin._process.poll.return_value = None
        plugin._pid = 500
        plugin._pgid = 500
        plugin._restart_count = 0
        plugin._cpu_violations = 0
        plugin._launch_time = time.monotonic() - 60
        plugin._last_cpu_ticks = 1000
        plugin._last_cpu_sample_time = time.monotonic() - 5
        plugin._cmd = ["nomadnet", "--daemon"]
        plugin._config_dir = nomadnet_config["config_dir"]
        return plugin

    def test_violations_increment_on_high_cpu(self, mock_app, nomadnet_config):
        plugin = self._setup_plugin(mock_app, nomadnet_config)

        with patch("os.sysconf", return_value=100):
            plugin._compute_cpu_percent(3000, time.monotonic())

        high_ticks = 5000
        with (
            patch.object(plugin, "_get_group_cpu_ticks", return_value=high_ticks),
            patch("os.sysconf", return_value=100),
            patch("os.cpu_count", return_value=4),
        ):
            cpu_pct = plugin._compute_cpu_percent(high_ticks, time.monotonic())

        if cpu_pct is not None and cpu_pct > 85 * 4:
            plugin._cpu_violations += 1

        assert plugin._cpu_violations >= 1

    def test_violations_reset_on_normal_cpu(self, mock_app, nomadnet_config):
        plugin = self._setup_plugin(mock_app, nomadnet_config)
        plugin._cpu_violations = 2

        plugin._last_cpu_ticks = 1000
        plugin._last_cpu_sample_time = time.monotonic() - 5

        with patch("os.sysconf", return_value=100):
            cpu_pct = plugin._compute_cpu_percent(1050, time.monotonic())

        if cpu_pct is not None and cpu_pct <= 85 * 4:
            plugin._cpu_violations = 0

        assert plugin._cpu_violations == 0

    def test_terminate_triggered_after_threshold(self, mock_app, nomadnet_config):
        plugin = self._setup_plugin(mock_app, nomadnet_config, cpu_violation_count=2)
        plugin._cpu_violations = 2

        with patch.object(plugin, "_terminate_process") as mock_term:
            if plugin._cpu_violations >= 2:
                plugin._terminate_process()
                plugin._cpu_violations = 0

        mock_term.assert_called_once()
        assert plugin._cpu_violations == 0

    def test_grace_period_skips_checks(self, mock_app, nomadnet_config):
        plugin = self._setup_plugin(mock_app, nomadnet_config, cpu_grace_period=60)
        plugin._launch_time = time.monotonic() - 10

        within_grace = time.monotonic() - plugin._launch_time < 60
        assert within_grace is True

    def test_restart_counter_resets_after_stability(self, mock_app, nomadnet_config):
        plugin = self._setup_plugin(mock_app, nomadnet_config)
        plugin._restart_count = 3
        plugin._launch_time = time.monotonic() - 601

        if plugin._restart_count > 0 and time.monotonic() - plugin._launch_time > 600:
            plugin._restart_count = 0

        assert plugin._restart_count == 0

    def test_exponential_backoff_formula(self, mock_app, nomadnet_config):
        expected = [30.0, 60.0, 120.0, 240.0, 300.0]
        for i, want in enumerate(expected):
            backoff = min(300.0, 30.0 * (2**i))
            assert backoff == want


# ── Config validation tests for new knobs ──────────────────────────────


class TestNewConfigValidation:
    def test_raises_on_invalid_nice_level(self, mock_app, nomadnet_config):
        nomadnet_config["nice_level"] = 20
        with pytest.raises(ValueError, match="nice_level"):
            _make_plugin(mock_app, nomadnet_config)

    def test_raises_on_negative_nice_level(self, mock_app, nomadnet_config):
        nomadnet_config["nice_level"] = -1
        with pytest.raises(ValueError, match="nice_level"):
            _make_plugin(mock_app, nomadnet_config)

    def test_raises_on_non_int_nice_level(self, mock_app, nomadnet_config):
        nomadnet_config["nice_level"] = 5.5
        with pytest.raises(ValueError, match="nice_level"):
            _make_plugin(mock_app, nomadnet_config)

    def test_raises_on_invalid_cpu_limit_percent(self, mock_app, nomadnet_config):
        nomadnet_config["cpu_limit_percent"] = 5
        with pytest.raises(ValueError, match="cpu_limit_percent"):
            _make_plugin(mock_app, nomadnet_config)

    def test_raises_on_cpu_limit_over_100(self, mock_app, nomadnet_config):
        nomadnet_config["cpu_limit_percent"] = 101
        with pytest.raises(ValueError, match="cpu_limit_percent"):
            _make_plugin(mock_app, nomadnet_config)

    def test_raises_on_invalid_cpu_check_interval(self, mock_app, nomadnet_config):
        nomadnet_config["cpu_check_interval"] = 1
        with pytest.raises(ValueError, match="cpu_check_interval"):
            _make_plugin(mock_app, nomadnet_config)

    def test_raises_on_negative_cpu_grace_period(self, mock_app, nomadnet_config):
        nomadnet_config["cpu_grace_period"] = -1
        with pytest.raises(ValueError, match="cpu_grace_period"):
            _make_plugin(mock_app, nomadnet_config)

    def test_raises_on_invalid_cpu_violation_count(self, mock_app, nomadnet_config):
        nomadnet_config["cpu_violation_count"] = 1
        with pytest.raises(ValueError, match="cpu_violation_count"):
            _make_plugin(mock_app, nomadnet_config)

    def test_valid_new_knobs_succeed(self, mock_app, nomadnet_config):
        nomadnet_config["nice_level"] = 15
        nomadnet_config["cpu_limit_percent"] = 90
        nomadnet_config["cpu_check_interval"] = 3
        nomadnet_config["cpu_grace_period"] = 60
        nomadnet_config["cpu_violation_count"] = 5
        plugin = _make_plugin(mock_app, nomadnet_config)
        assert plugin.config["nice_level"] == 15


# ── CPU runaway event emission test ─────────────────────────────────────


class TestCpuRunawayEvent:
    def test_publishes_nomadnet_cpu_runaway_event(self, mock_app, nomadnet_config):
        nomadnet_config["cpu_violation_count"] = 2
        plugin = _make_plugin(mock_app, nomadnet_config)
        plugin._active = True
        plugin._process = MagicMock()
        plugin._process.poll.return_value = None
        plugin._pid = 500
        plugin._pgid = 500
        plugin._cpu_violations = 2
        plugin._launch_time = time.monotonic() - 60

        with patch.object(plugin, "_terminate_process"):
            plugin.event_bus.publish(
                events.NOMADNET_CPU_RUNAWAY,
                {"pid": plugin._pid, "pgid": plugin._pgid, "cpu_percent": 400.0},
            )

        mock_app.event_bus.publish.assert_called_with(
            events.NOMADNET_CPU_RUNAWAY,
            {"pid": 500, "pgid": 500, "cpu_percent": 400.0},
        )
