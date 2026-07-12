"""Tests for the config module."""

import os
import socket
import threading

import yaml
import pytest

from reticulumpi.config import AppConfig, ConfigError


def test_default_config():
    config = AppConfig()
    assert config.use_shared_instance is True
    assert config.log_level == 4
    assert config.plugin_paths == []
    assert config.plugins == {}


def test_load_from_file(tmp_config):
    config = AppConfig(tmp_config)
    assert config.use_shared_instance is False
    assert config.log_level == 4
    assert "heartbeat_announce" in config.plugins
    assert config.plugins["heartbeat_announce"]["enabled"] is True
    assert config.plugins["heartbeat_announce"]["interval_seconds"] == 5


def test_explicit_missing_config_file_is_fatal():
    with pytest.raises(ConfigError, match="Config file not found"):
        AppConfig("/nonexistent/path/config.yaml")


def test_identity_path_expansion(tmp_config):
    config = AppConfig(tmp_config)
    assert "~" not in config.identity_path


def test_plugin_paths_expansion():
    config = AppConfig()
    config._data["plugin_paths"] = ["~/my_plugins"]
    paths = config.plugin_paths
    assert len(paths) == 1
    assert "~" not in paths[0]


def test_reticulum_config_dir_none():
    config = AppConfig()
    assert config.reticulum_config_dir is None


def test_config_path_stored(tmp_config):
    config = AppConfig(tmp_config)
    assert config.config_path == tmp_config


def test_config_path_none_when_no_file():
    config = AppConfig()
    assert config.config_path is None


def test_node_name_defaults_to_hostname():
    config = AppConfig()
    expected = f"ReticulumPi-{socket.gethostname()}"
    assert config.node_name == expected


def test_node_name_from_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("reticulumpi:\n  node_name: MyMeshNode\n")
    config = AppConfig(str(cfg))
    assert config.node_name == "MyMeshNode"


def test_reticulum_config_dir_expansion(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("reticulumpi:\n  reticulum_config_dir: ~/my_reticulum\n")
    config = AppConfig(str(cfg))
    assert "~" not in config.reticulum_config_dir
    assert config.reticulum_config_dir.endswith("my_reticulum")


def test_offgrid_mode_default():
    config = AppConfig()
    assert config.offgrid_mode is False


def test_set_internet_force_offline_persists(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("reticulumpi:\n  log_level: 4\n")
    config = AppConfig(str(cfg))
    result = config.set_internet_force_offline(True)
    assert result is True
    assert config.offgrid_mode is True
    with open(config.runtime_overrides_path, "r") as f:
        raw = yaml.safe_load(f)
    assert raw["internet"]["force_offline"] is True
    assert "internet" not in yaml.safe_load(cfg.read_text())["reticulumpi"]
    assert os.stat(config.runtime_overrides_path).st_mode & 0o777 == 0o600


def test_runtime_persist_does_not_rewrite_corrupt_primary_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("reticulumpi:\n  log_level: 4\n")
    config = AppConfig(str(cfg))
    cfg.write_text("{{{invalid yaml")
    result = config.set_internet_force_offline(True)
    assert result is True
    assert cfg.read_text() == "{{{invalid yaml"
    with open(config.runtime_overrides_path) as fh:
        assert yaml.safe_load(fh)["internet"]["force_offline"] is True


def test_persist_no_config_path():
    config = AppConfig()
    result = config.set_internet_force_offline(True)
    assert result is False
    assert config.offgrid_mode is True
    assert config.last_persistence_reason == "no_override_path"


def test_set_internet_force_offline_returns_false_on_oserror(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("reticulumpi:\n  log_level: 4\n")
    config = AppConfig(str(cfg))
    from unittest.mock import patch

    with patch.object(
        config,
        "_persist_runtime_overrides",
        side_effect=OSError("read-only filesystem"),
    ):
        result = config.set_internet_force_offline(True)
    assert result is False
    assert config.offgrid_mode is True  # in-memory state still updated
    assert config.last_persistence_reason == "write_failed"


def test_runtime_override_fails_closed_when_directory_open_fails(tmp_path):
    from unittest.mock import patch

    cfg = tmp_path / "config.yaml"
    cfg.write_text("reticulumpi:\n  log_level: 4\n")
    config = AppConfig(str(cfg))
    real_open = os.open

    def fail_directory_open(path, flags, *args):
        if os.fspath(path) == os.fspath(tmp_path):
            raise OSError("directory handle unavailable")
        return real_open(path, flags, *args)

    with patch("reticulumpi.config.os.open", side_effect=fail_directory_open):
        assert config.set_internet_force_offline(True) is False

    assert config.offgrid_mode is True
    assert config.last_persistence_reason == "write_failed"
    assert list(tmp_path.glob(".reticulumpi_*.tmp")) == []


def test_runtime_override_fails_closed_when_directory_fsync_fails(tmp_path):
    from unittest.mock import patch

    cfg = tmp_path / "config.yaml"
    cfg.write_text("reticulumpi:\n  log_level: 4\n")
    config = AppConfig(str(cfg))
    real_open = os.open
    real_fsync = os.fsync
    directory_fds: set[int] = set()

    def capture_directory_open(path, flags, *args):
        descriptor = real_open(path, flags, *args)
        if os.fspath(path) == os.fspath(tmp_path):
            directory_fds.add(descriptor)
        return descriptor

    def fail_directory_fsync(descriptor):
        if descriptor in directory_fds:
            raise OSError("directory fsync unavailable")
        return real_fsync(descriptor)

    with (
        patch("reticulumpi.config.os.open", side_effect=capture_directory_open),
        patch("reticulumpi.config.os.fsync", side_effect=fail_directory_fsync),
    ):
        assert config.set_internet_force_offline(True) is False

    assert config.offgrid_mode is True
    assert config.last_persistence_reason == "write_failed"
    assert list(tmp_path.glob(".reticulumpi_*.tmp")) == []


def test_runtime_override_loaded_on_restart(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("reticulumpi:\n  log_level: 4\n")
    first = AppConfig(str(cfg))
    assert first.set_internet_force_offline(True) is True

    second = AppConfig(str(cfg))
    assert second.offgrid_mode is True


def test_runtime_override_rejects_unknown_keys(tmp_path):
    import pytest

    from reticulumpi.config import ConfigError

    cfg = tmp_path / "config.yaml"
    cfg.write_text("reticulumpi:\n  log_level: 4\n")
    override = tmp_path / "override.yaml"
    override.write_text("plugins:\n  web_dashboard:\n    password: exposed\n")

    with pytest.raises(ConfigError, match="Unsupported runtime override keys"):
        AppConfig(str(cfg), runtime_overrides_path=str(override))


def test_set_internet_force_offline_thread_safe(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("reticulumpi:\n  log_level: 4\n")
    config = AppConfig(str(cfg))
    errors = []

    def toggle(val):
        try:
            config.set_internet_force_offline(val)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=toggle, args=(i % 2 == 0,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(errors) == 0
    assert isinstance(config.offgrid_mode, bool)
