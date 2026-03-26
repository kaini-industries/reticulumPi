"""Tests for the config module."""

from reticulumpi.config import AppConfig


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


def test_missing_config_file_uses_defaults():
    config = AppConfig("/nonexistent/path/config.yaml")
    assert config.use_shared_instance is True
    assert config.plugins == {}


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


def test_reticulum_config_dir_expansion(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "reticulumpi:\n  reticulum_config_dir: ~/my_reticulum\n"
    )
    config = AppConfig(str(cfg))
    assert "~" not in config.reticulum_config_dir
    assert config.reticulum_config_dir.endswith("my_reticulum")
