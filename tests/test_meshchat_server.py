"""Tests for the MeshChat Server plugin."""

import os
import runpy
import sys
from importlib.resources import files
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def meshchat_install(tmp_path):
    """Create a fake MeshChat install directory with meshchat.py and venv."""
    install_dir = tmp_path / "meshchat"
    install_dir.mkdir()

    # Create dummy meshchat.py
    script = install_dir / "meshchat.py"
    script.write_text("#!/usr/bin/env python3\n")

    # Create dummy venv python
    venv_bin = install_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python_bin = venv_bin / "python"
    python_bin.write_text("#!/bin/sh\n")
    python_bin.chmod(0o755)

    # Create dummy launcher script where the plugin expects it
    # (project_root/scripts/meshchat_launcher.py — project_root is 4 levels
    # above the plugin source file, but in tests we mock this via monkeypatch)

    return str(install_dir)


@pytest.fixture
def meshchat_config(meshchat_install):
    """Base config dict for the meshchat_server plugin."""
    return {
        "enabled": True,
        "install_dir": meshchat_install,
        "host": "0.0.0.0",
        "port": 8000,
        "health_check_interval": 10,
        "auto_restart": True,
        "max_restarts": 3,
    }


def _make_plugin(mock_app, config):
    """Construct the plugin with a valid install dir."""
    from reticulumpi.builtin_plugins.meshchat_server import MeshChatServer

    return MeshChatServer(mock_app, config)


class _JumpingMonotonicClock:
    """Advance enough on every read to expire test deadlines immediately."""

    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        self.value += 20.0
        return self.value

    @staticmethod
    def time():
        raise AssertionError("launcher deadline consulted wall time")


def _patched_launcher_downloader(fake_rns):
    launcher_path = files("reticulumpi").joinpath("data/meshchat_launcher.pydata")
    launcher = runpy.run_path(str(launcher_path), run_name="reticulumpi_meshchat_test")
    launcher["_apply_patches"].__globals__["time"] = _JumpingMonotonicClock()

    class Downloader:
        async def download(self):  # pragma: no cover - replaced by launcher
            raise AssertionError("launcher patch was not installed")

        def on_failed(self, _request_receipt=None):
            return None

    meshchat = SimpleNamespace(NomadnetDownloader=Downloader, nomadnet_cached_links={})
    with patch.dict(sys.modules, {"RNS": fake_rns, "database": None}):
        launcher["_apply_patches"](meshchat)
    return Downloader


class TestValidateConfig:
    def test_packaged_launcher_is_available(self):
        launcher = files("reticulumpi").joinpath("data/meshchat_launcher.pydata")
        assert launcher.is_file()

    @pytest.mark.asyncio
    async def test_packaged_path_deadline_ignores_wall_clock_jump(self):
        fake_rns = MagicMock()
        fake_rns.Transport.has_path.return_value = False
        downloader_type = _patched_launcher_downloader(fake_rns)
        failures = []
        downloader = downloader_type()
        downloader.destination_hash = b"\xaa" * 16
        downloader.path = "/page/index.mu"
        downloader.on_download_failure = failures.append

        await downloader.download(path_lookup_timeout=15)

        assert failures and "Could not find path" in failures[0]

    @pytest.mark.asyncio
    async def test_packaged_link_deadline_ignores_wall_clock_jump(self):
        class FakeDestination:
            OUT = 1
            SINGLE = 2

            def __init__(self, *_args):
                pass

        class FakeLink:
            PENDING = 0
            ACTIVE = 2
            ESTABLISHMENT_TIMEOUT_PER_HOP = 0

            def __init__(self, _destination, established_callback=None):
                self.status = self.PENDING
                self.teardown_reason = 1

        fake_rns = MagicMock()
        fake_rns.Transport.has_path.return_value = True
        fake_rns.Transport.hops_to.return_value = 1
        fake_rns.Identity.recall.return_value = object()
        fake_rns.Destination = FakeDestination
        fake_rns.Link = FakeLink
        downloader_type = _patched_launcher_downloader(fake_rns)
        failures = []
        downloader = downloader_type()
        downloader.destination_hash = b"\xbb" * 16
        downloader.path = "/page/index.mu"
        downloader.app_name = "nomadnetwork"
        downloader.aspects = ("node",)
        downloader.link_established = MagicMock()
        downloader.on_download_failure = failures.append

        await downloader.download(link_establishment_timeout=1)

        assert failures and "failed after" in failures[0]

    def test_raises_when_meshchat_not_found(self, mock_app, tmp_path):
        config = {
            "enabled": True,
            "install_dir": str(tmp_path / "nonexistent"),
        }
        from reticulumpi.builtin_plugins.meshchat_server import MeshChatServer

        with pytest.raises(ValueError, match="MeshChat not found"):
            MeshChatServer(mock_app, config)

    def test_raises_when_venv_not_found(self, mock_app, tmp_path):
        install_dir = tmp_path / "meshchat"
        install_dir.mkdir()
        (install_dir / "meshchat.py").write_text("#!/usr/bin/env python3\n")
        # No .venv created

        config = {"enabled": True, "install_dir": str(install_dir)}
        from reticulumpi.builtin_plugins.meshchat_server import MeshChatServer

        with pytest.raises(ValueError, match="venv not found"):
            MeshChatServer(mock_app, config)

    def test_raises_on_invalid_port(self, mock_app, meshchat_config):
        meshchat_config["port"] = 99999
        with pytest.raises(ValueError, match="port"):
            _make_plugin(mock_app, meshchat_config)

    def test_raises_on_zero_port(self, mock_app, meshchat_config):
        meshchat_config["port"] = 0
        with pytest.raises(ValueError, match="port"):
            _make_plugin(mock_app, meshchat_config)

    def test_raises_on_invalid_health_check_interval(self, mock_app, meshchat_config):
        meshchat_config["health_check_interval"] = 2
        with pytest.raises(ValueError, match="health_check_interval"):
            _make_plugin(mock_app, meshchat_config)

    def test_raises_on_negative_max_restarts(self, mock_app, meshchat_config):
        meshchat_config["max_restarts"] = -1
        with pytest.raises(ValueError, match="max_restarts"):
            _make_plugin(mock_app, meshchat_config)

    def test_raises_on_invalid_link_timeout(self, mock_app, meshchat_config):
        meshchat_config["link_timeout"] = -5
        with pytest.raises(ValueError, match="link_timeout"):
            _make_plugin(mock_app, meshchat_config)

    def test_raises_on_zero_link_timeout(self, mock_app, meshchat_config):
        meshchat_config["link_timeout"] = 0
        with pytest.raises(ValueError, match="link_timeout"):
            _make_plugin(mock_app, meshchat_config)

    def test_raises_on_invalid_path_lookup_timeout(self, mock_app, meshchat_config):
        meshchat_config["path_lookup_timeout"] = "bad"
        with pytest.raises(ValueError, match="path_lookup_timeout"):
            _make_plugin(mock_app, meshchat_config)

    def test_valid_config_succeeds(self, mock_app, meshchat_config):
        plugin = _make_plugin(mock_app, meshchat_config)
        assert plugin.plugin_name == "meshchat_server"

    def test_required_artifact_policy_rejects_storage_inside_install(
        self, mock_app, meshchat_config
    ):
        mock_app.config = SimpleNamespace(external_artifact_policy=SimpleNamespace(required=True))
        meshchat_config["storage_dir"] = os.path.join(meshchat_config["install_dir"], "storage")

        with pytest.raises(ValueError, match="outside the immutable install tree"):
            _make_plugin(mock_app, meshchat_config)


class TestStart:
    def test_launches_subprocess_with_launcher(self, mock_app, meshchat_config):
        mock_app._reticulum_config_dir = "/tmp/reticulum"
        plugin = _make_plugin(mock_app, meshchat_config)

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            plugin.start()

        args = mock_popen.call_args[0][0]
        assert args[1].endswith("meshchat_launcher.pydata")
        assert "--headless" in args
        assert "--host" in args
        assert "--port" in args
        assert "--storage-dir" in args
        assert "--reticulum-config-dir" in args
        assert plugin._pid == 12345
        assert plugin._active is True

        # Verify env vars were passed
        env = mock_popen.call_args[1].get("env") or mock_popen.call_args.kwargs.get("env")
        assert env is not None
        assert env["MESHCHAT_DIR"] == meshchat_config["install_dir"]
        assert env["MESHCHAT_LINK_TIMEOUT"] == "75"
        assert env["MESHCHAT_PATH_LOOKUP_TIMEOUT"] == "15"
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert mock_popen.call_args.kwargs["cwd"] == os.path.join(
            meshchat_config["install_dir"], "storage"
        )

        # Cleanup
        plugin._active = False
        plugin._join_threads()

    def test_custom_timeouts_passed_as_env(self, mock_app, meshchat_config):
        mock_app._reticulum_config_dir = "/tmp/reticulum"
        meshchat_config["link_timeout"] = 120
        meshchat_config["path_lookup_timeout"] = 30
        plugin = _make_plugin(mock_app, meshchat_config)

        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            plugin.start()

        env = mock_popen.call_args[1].get("env") or mock_popen.call_args.kwargs.get("env")
        assert env["MESHCHAT_LINK_TIMEOUT"] == "120"
        assert env["MESHCHAT_PATH_LOOKUP_TIMEOUT"] == "30"

        plugin._active = False
        plugin._join_threads()

    def test_creates_storage_directory(self, mock_app, meshchat_config, tmp_path):
        mock_app._reticulum_config_dir = None
        storage_dir = str(tmp_path / "meshchat_storage")
        meshchat_config["storage_dir"] = storage_dir
        plugin = _make_plugin(mock_app, meshchat_config)

        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc):
            plugin.start()

        assert os.path.isdir(storage_dir)

        plugin._active = False
        plugin._join_threads()

    def test_uses_rns_config_from_app(self, mock_app, meshchat_config):
        mock_app._reticulum_config_dir = "/custom/reticulum"
        plugin = _make_plugin(mock_app, meshchat_config)

        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            plugin.start()

        args = mock_popen.call_args[0][0]
        rns_idx = args.index("--reticulum-config-dir")
        assert args[rns_idx + 1] == "/custom/reticulum"

        plugin._active = False
        plugin._join_threads()


class TestStop:
    def test_terminates_process(self, mock_app, meshchat_config):
        mock_app._reticulum_config_dir = None
        plugin = _make_plugin(mock_app, meshchat_config)

        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc):
            plugin.start()

        plugin.stop()

        mock_proc.terminate.assert_called_once()
        assert plugin._active is False

    def test_kills_if_terminate_fails(self, mock_app, meshchat_config):
        from subprocess import TimeoutExpired

        mock_app._reticulum_config_dir = None
        plugin = _make_plugin(mock_app, meshchat_config)

        mock_proc = MagicMock()
        mock_proc.pid = 200
        mock_proc.poll.return_value = None
        mock_proc.wait.side_effect = [TimeoutExpired("meshchat", 10), None]

        with patch("subprocess.Popen", return_value=mock_proc):
            plugin.start()

        plugin.stop()

        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()


class TestGetStatus:
    def test_status_when_running(self, mock_app, meshchat_config):
        mock_app._reticulum_config_dir = None
        plugin = _make_plugin(mock_app, meshchat_config)

        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc):
            plugin.start()

        status = plugin.get_status()
        assert status["active"] is True
        assert status["pid"] == 42
        assert status["running"] is True
        assert status["web_url"] == "http://0.0.0.0:8000"
        assert status["restart_count"] == 0

        plugin._active = False
        plugin._join_threads()

    def test_status_when_exited(self, mock_app, meshchat_config):
        plugin = _make_plugin(mock_app, meshchat_config)
        plugin._process = None
        plugin._pid = None
        plugin._restart_count = 0

        status = plugin.get_status()
        assert status["running"] is False


class TestHealthMonitor:
    def test_restarts_on_crash(self, mock_app, meshchat_config):
        mock_app._reticulum_config_dir = None
        meshchat_config["max_restarts"] = 2
        plugin = _make_plugin(mock_app, meshchat_config)

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
        plugin._cmd = ["/usr/bin/python", "meshchat.py", "--headless"]
        plugin._storage_dir = meshchat_config.get("storage_dir", "/tmp/storage")
        plugin._host = "0.0.0.0"
        plugin._port = 8000

        with patch(
            "reticulumpi.builtin_plugins.meshchat_server.subprocess.Popen",
            return_value=alive_proc,
        ):
            if plugin._process.poll() is not None:
                plugin._restart_count += 1
                plugin._launch_process(plugin._cmd)

        assert plugin._restart_count == 1
        assert plugin._pid == 2

    def test_gives_up_after_max_restarts(self, mock_app, meshchat_config):
        mock_app._reticulum_config_dir = None
        meshchat_config["max_restarts"] = 1
        plugin = _make_plugin(mock_app, meshchat_config)

        crash_proc = MagicMock()
        crash_proc.pid = 1
        crash_proc.poll.return_value = 1
        crash_proc.returncode = 1

        plugin._active = True
        plugin._process = crash_proc
        plugin._pid = 1
        plugin._restart_count = 1  # Already at max
        plugin._cmd = ["/usr/bin/python", "meshchat.py", "--headless"]
        plugin._storage_dir = meshchat_config.get("storage_dir", "/tmp/storage")
        plugin._host = "0.0.0.0"
        plugin._port = 8000

        max_restarts = plugin.config.get("max_restarts", 5)
        if plugin._process.poll() is not None and plugin._restart_count >= max_restarts:
            plugin._active = False

        assert plugin._active is False

    def test_stability_window_ignores_wall_clock_jump(self, mock_app, meshchat_config):
        meshchat_config["auto_restart"] = False
        plugin = _make_plugin(mock_app, meshchat_config)
        crashed = MagicMock()
        crashed.poll.return_value = 1
        crashed.returncode = 1
        plugin._active = True
        plugin._process = crashed
        plugin._consecutive_failures = 4
        plugin._last_start_monotonic = 100.0
        plugin._stability_threshold = 60.0

        with (
            patch(
                "reticulumpi.builtin_plugins.meshchat_server.time.time",
                return_value=-9_999_999_999.0,
            ),
            patch(
                "reticulumpi.builtin_plugins.meshchat_server.time.monotonic",
                return_value=161.0,
            ),
            patch.object(plugin, "_sleep_while_active", return_value=None),
        ):
            plugin._health_monitor()

        assert plugin._consecutive_failures == 0
