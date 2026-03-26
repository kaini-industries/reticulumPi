"""Tests for config validation and error handling."""

import pytest

from reticulumpi.config import AppConfig, ConfigError


def test_malformed_yaml(tmp_path):
    bad_file = tmp_path / "bad.yaml"
    bad_file.write_text("reticulumpi:\n  log_level: [invalid\n")
    with pytest.raises(ConfigError, match="Invalid YAML"):
        AppConfig(str(bad_file))


def test_log_level_out_of_range(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("reticulumpi:\n  log_level: 99\n")
    with pytest.raises(ConfigError, match="log_level must be an integer 0-7"):
        AppConfig(str(cfg))


def test_log_level_negative(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("reticulumpi:\n  log_level: -1\n")
    with pytest.raises(ConfigError, match="log_level must be an integer 0-7"):
        AppConfig(str(cfg))


def test_log_level_non_integer(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("reticulumpi:\n  log_level: high\n")
    with pytest.raises(ConfigError, match="log_level must be an integer 0-7"):
        AppConfig(str(cfg))


def test_plugin_paths_not_a_list(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("reticulumpi:\n  plugin_paths: /just/a/string\n")
    with pytest.raises(ConfigError, match="plugin_paths must be a list"):
        AppConfig(str(cfg))


def test_plugins_not_a_dict(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("reticulumpi:\n  plugins:\n    - not_a_dict\n")
    with pytest.raises(ConfigError, match="plugins must be a mapping"):
        AppConfig(str(cfg))


def test_unknown_keys_warns(tmp_path, caplog):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("reticulumpi:\n  log_level: 4\n  bogus_key: true\n")
    AppConfig(str(cfg))
    assert "Unknown config keys" in caplog.text
    assert "bogus_key" in caplog.text


def test_valid_config_does_not_raise(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "reticulumpi:\n"
        "  log_level: 4\n"
        "  plugin_paths: []\n"
        "  plugins:\n"
        "    heartbeat_announce:\n"
        "      enabled: true\n"
    )
    config = AppConfig(str(cfg))
    assert config.log_level == 4


def test_empty_yaml_uses_defaults(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("")
    config = AppConfig(str(cfg))
    assert config.log_level == 4
    assert config.plugins == {}


def test_missing_reticulumpi_section_warns(tmp_path, caplog):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("some_other_key:\n  value: true\n")
    config = AppConfig(str(cfg))
    assert "missing 'reticulumpi:' section" in caplog.text
    # Should fall back to defaults
    assert config.log_level == 4
    assert config.plugins == {}
