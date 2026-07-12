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


def test_missing_reticulumpi_section_is_rejected(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("some_other_key:\n  value: true\n")
    with pytest.raises(ConfigError, match="missing the required 'reticulumpi:' section"):
        AppConfig(str(cfg))


def test_non_mapping_config_root_is_rejected(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("- not\n- a\n- mapping\n")

    with pytest.raises(ConfigError, match="Config root.*must be a mapping"):
        AppConfig(str(cfg))


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("internet: offline", "internet must be a mapping"),
        ("internet:\n    force_offline: 1", "force_offline must be a boolean"),
        ("thread_budget: 0", "thread_budget must be a positive integer"),
        ("plugins:\n    broken: true", "plugin configurations must be mappings"),
    ],
)
def test_nested_config_shapes_are_validated(tmp_path, body, message):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"reticulumpi:\n  {body}\n")

    with pytest.raises(ConfigError, match=message):
        AppConfig(str(cfg))
