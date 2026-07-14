"""Tests for the fail-closed recovery configuration projection."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from reticulumpi.recovery_config import (
    MAX_CONFIG_BYTES,
    RecoveryConfigError,
    load_migration_plugin_configs,
)


def _load(tmp_path: Path, text: str) -> dict[str, dict[str, object]]:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return load_migration_plugin_configs(path)


def test_projects_only_enabled_migration_plugin_fields(tmp_path: Path) -> None:
    projected = _load(
        tmp_path,
        """reticulumpi:
  display_name: ignored
  plugins:
    messaging_hub:
      enabled: true
      db_path: "/var/lib/reticulumpi/messages #1.db" # a real comment
      unrelated: ignored
    network_map:
      enabled: false
      db_path: /must/not/be/projected.db
    sensor_framework:
      enabled: true
      storage:
        type: sqlite
        path: '/var/lib/reticulumpi/sensors.db'
    third_party_plugin:
      enabled: true
      arbitrary: [configuration, is, ignored]
""",
    )

    assert projected == {
        "messaging_hub": {"db_path": "/var/lib/reticulumpi/messages #1.db"},
        "sensor_framework": {
            "storage": {
                "type": "sqlite",
                "path": "/var/lib/reticulumpi/sensors.db",
            }
        },
    }


def test_quoted_escape_projection_preserves_hashes_and_ignores_blank_lines(
    tmp_path: Path,
) -> None:
    projected = _load(
        tmp_path,
        "reticulumpi:\n"
        "  # recovery projection intentionally ignores comments\n"
        "\n"
        "  plugins:\n"
        "    messaging_hub:\n"
        "      enabled: true\n"
        '      db_path: "/var/lib/reticulumpi/message\\"#archive.db"\n',
    )

    assert projected == {"messaging_hub": {"db_path": '/var/lib/reticulumpi/message"#archive.db'}}


def test_quoted_mapping_keys_are_not_silently_ignored(tmp_path: Path) -> None:
    projected = _load(
        tmp_path,
        "'reticulumpi':\n"
        '  "plugins":\n'
        "    'messaging_hub':\n"
        '      "enabled": true\n'
        "      'db_path': /var/lib/reticulumpi/messages.db\n"
        '    "sensor_framework":\n'
        "      'enabled': true\n"
        '      "storage":\n'
        "        'type': sqlite\n"
        '        "path": /var/lib/reticulumpi/sensors.db\n',
    )

    assert projected == {
        "messaging_hub": {"db_path": "/var/lib/reticulumpi/messages.db"},
        "sensor_framework": {
            "storage": {
                "type": "sqlite",
                "path": "/var/lib/reticulumpi/sensors.db",
            }
        },
    }


def test_yaml_scalar_semantics_preserve_hashes_and_single_quoted_backslashes(
    tmp_path: Path,
) -> None:
    projected = _load(
        tmp_path,
        "reticulumpi:\n"
        "  plugins:\n"
        "    messaging_hub:\n"
        "      enabled: true\n"
        "      db_path: /tmp/a#b.db\n"
        "    network_map:\n"
        "      enabled: true\n"
        "      db_path: '/tmp/a\\nb.db'\n"
        "    node_location_tracker:\n"
        "      enabled: true\n"
        "      db_path: 123.db\n",
    )

    assert projected == {
        "messaging_hub": {"db_path": "/tmp/a#b.db"},
        "network_map": {"db_path": "/tmp/a\\nb.db"},
        "node_location_tracker": {"db_path": "123.db"},
    }


def test_yaml_double_quoted_escape_semantics(tmp_path: Path) -> None:
    projected = _load(
        tmp_path,
        "reticulumpi:\n"
        "  plugins:\n"
        "    messaging_hub:\n"
        "      enabled: true\n"
        '      db_path: "/tmp/\\_\\u0061\\x62.db"\n',
    )

    assert projected == {"messaging_hub": {"db_path": "/tmp/\xa0ab.db"}}


@pytest.mark.parametrize(
    "value",
    [
        "/tmp/a#b.db",
        "123.db",
        "'/tmp/a\\nb.db'",
        '"/tmp/\\_\\u0061\\x62.db"',
        '"/tmp/path with spaces.db"',
    ],
)
def test_accepted_path_scalars_match_pyyaml(tmp_path: Path, value: str) -> None:
    text = (
        "reticulumpi:\n"
        "  plugins:\n"
        "    messaging_hub:\n"
        "      enabled: true\n"
        f"      db_path: {value}\n"
    )

    projected = _load(tmp_path, text)
    loaded = yaml.safe_load(text)
    assert (
        projected["messaging_hub"]["db_path"]
        == loaded["reticulumpi"]["plugins"]["messaging_hub"]["db_path"]
    )


@pytest.mark.parametrize(
    "text",
    [
        "reticulumpi:\n",
        "reticulumpi:\n  plugins: {}\n",
        "reticulumpi:\n  unrelated: true\n",
        "reticulumpi:\n  plugins:\n    messaging_hub:\n      enabled: false\n",
        "reticulumpi:\n  plugins:\n    messaging_hub:\n      db_path: ignored.db\n",
    ],
)
def test_empty_or_disabled_plugin_sets_project_to_empty(tmp_path: Path, text: str) -> None:
    assert _load(tmp_path, text) == {}


def test_projects_empty_and_default_sensor_storage(tmp_path: Path) -> None:
    projected = _load(
        tmp_path,
        """reticulumpi:
  plugins:
    sensor_framework:
      enabled: true
      storage: {}
    transport_health:
      enabled: true
""",
    )

    assert projected == {
        "sensor_framework": {"storage": {}},
        "transport_health": {},
    }


def test_projects_production_sensor_config_with_indentless_sequence(
    tmp_path: Path,
) -> None:
    text = """reticulumpi:
  plugins:
    sensor_framework:
      enabled: true
      read_interval: 30
      sensors:
      - name: cpu_temperature
        driver: sysfs
        sysfs_path: /sys/class/thermal/thermal_zone0/temp
        scale: 0.001
        reading_name: temperature
      - name: enclosure_temperature
        driver: sysfs
        sysfs_path: /sys/class/hwmon/hwmon0/temp1_input
        scale: 0.001
        reading_name: temperature
      storage:
        type: sqlite
        path: /var/lib/reticulumpi/sensor_data.db
        retention_days: 30
"""

    assert (
        yaml.safe_load(text)["reticulumpi"]["plugins"]["sensor_framework"]["sensors"][0]["name"]
        == "cpu_temperature"
    )
    assert _load(tmp_path, text) == {
        "sensor_framework": {
            "storage": {
                "type": "sqlite",
                "path": "/var/lib/reticulumpi/sensor_data.db",
            }
        }
    }


def test_relevant_keys_inside_irrelevant_indentless_sequence_are_not_projected(
    tmp_path: Path,
) -> None:
    projected = _load(
        tmp_path,
        """reticulumpi:
  plugins:
    sensor_framework:
      enabled: true
      sensors:
      - enabled: false
        db_path: /must/not/be/projected.db
        storage:
          type: postgres
          path: /must/not/be/projected.db
      storage:
        type: sqlite
        path: /var/lib/reticulumpi/sensor_data.db
""",
    )

    assert projected == {
        "sensor_framework": {
            "storage": {
                "type": "sqlite",
                "path": "/var/lib/reticulumpi/sensor_data.db",
            }
        }
    }


@pytest.mark.parametrize(
    "text",
    [
        "reticulumpi:\n  - orphan\n  plugins: {}\n",
        "reticulumpi:\n  plugins:\n  - messaging_hub: {}\n",
        "reticulumpi:\n  plugins:\n    messaging_hub:\n    - enabled: true\n",
        ("reticulumpi:\n  plugins:\n    messaging_hub:\n      enabled:\n      - true\n"),
        (
            "reticulumpi:\n  plugins:\n    messaging_hub:\n"
            "      enabled: true\n      db_path:\n      - messages.db\n"
        ),
        (
            "reticulumpi:\n  plugins:\n    sensor_framework:\n"
            "      enabled: true\n      storage:\n      - type: sqlite\n"
        ),
        (
            "reticulumpi:\n  plugins:\n    sensor_framework:\n"
            "      enabled: true\n      storage:\n        type:\n"
            "        - sqlite\n"
        ),
        (
            "reticulumpi:\n  plugins:\n    sensor_framework:\n"
            "      enabled: true\n      storage:\n        path:\n"
            "        - sensors.db\n"
        ),
        (
            "reticulumpi:\n  plugins:\n    sensor_framework:\n"
            "      enabled: true\n      sensors: configured\n      - orphan\n"
        ),
        (
            "reticulumpi:\n  plugins:\n    sensor_framework:\n"
            "      enabled: true\n      sensors:\n      -foo\n"
        ),
        ("reticulumpi:\n  root_field:\n    nested: value\n  - illegal\n  plugins: {}\n"),
        (
            "reticulumpi:\n  plugins:\n    messaging_hub:\n"
            "      enabled: true\n      hooks:\n        nested: value\n"
            "      - illegal\n      db_path: messages.db\n"
        ),
        (
            "reticulumpi:\n  plugins:\n    sensor_framework:\n"
            "      enabled: true\n      storage:\n        options:\n"
            "          nested: value\n        - illegal\n"
            "        type: sqlite\n        path: sensors.db\n"
        ),
    ],
)
def test_rejects_unsafe_indentless_sequence_ownership(tmp_path: Path, text: str) -> None:
    with pytest.raises(RecoveryConfigError, match="malformed block mapping"):
        _load(tmp_path, text)


def test_accepts_irrelevant_indentless_sequences_at_projection_scopes(
    tmp_path: Path,
) -> None:
    projected = _load(
        tmp_path,
        """reticulumpi:
  root_list:
  - ignored
  plugins:
    third_party:
    - enabled: true
    messaging_hub:
      enabled: true
      hooks:
      - enabled: false
        db_path: /must/not/be-projected.db
      db_path: /var/lib/reticulumpi/messages.db
    sensor_framework:
      enabled: true
      storage:
        options:
        - type: postgres
          path: /must/not/be-projected.db
        type: sqlite
        path: /var/lib/reticulumpi/sensors.db
""",
    )

    assert projected == {
        "messaging_hub": {"db_path": "/var/lib/reticulumpi/messages.db"},
        "sensor_framework": {
            "storage": {
                "type": "sqlite",
                "path": "/var/lib/reticulumpi/sensors.db",
            }
        },
    }


def test_duplicate_relevant_field_around_ignored_indentless_sequence_is_rejected(
    tmp_path: Path,
) -> None:
    text = """reticulumpi:
  plugins:
    messaging_hub:
      enabled: true
      hooks:
      - ignored
      enabled: false
"""

    with pytest.raises(RecoveryConfigError, match="configured more than once"):
        _load(tmp_path, text)


@pytest.mark.parametrize(
    "text",
    [
        "reticulumpi:\n\tplugins: {}\n",
        "%YAML 1.2\nreticulumpi:\n  plugins: {}\n",
        "---\nreticulumpi:\n  plugins: {}\n",
        "reticulumpi:\n  defaults: &defaults {}\n  plugins: {}\n",
        "reticulumpi:\n  plugins:\n    messaging_hub: *defaults\n",
        "reticulumpi:\n  plugins:\n    messaging_hub:\n      enabled: !!bool true\n",
        "reticulumpi:\n  plugins:\n    messaging_hub:\n      enabled: true\n      db_path: |\n        /var/lib/messages.db\n",
        'reticulumpi:\n  plugins:\n    messaging_hub:\n      enabled: true\n      db_path: "unterminated\n',
        'reticulumpi:\n  plugins:\n    messaging_hub:\n      enabled: true\n      db_path: "bad\\qescape"\n',
        'reticulumpi:\n  plugins:\n    messaging_hub:\n      enabled: true\n      db_path: "/tmp/a\\0b.db"\n',
        "reticulumpi:\n  plugins:\n    messaging_hub:\n      ? enabled\n      : true\n",
        "reticulumpi:\n  plugins:\n    messaging_hub:\n      enabled: true\n      db_path: /tmp/a\n        continued.db\n",
        "reticulumpi:\n  plugins:\n    messaging_hub:\n      <<: *defaults\n      enabled: true\n",
        "reticulumpi:\n  plugins:\n    messaging_hub:\n      <<: {}\n      enabled: true\n",
        "reticulumpi:\n  plugins:\n    messaging_hub:\n      enabled: True\n",
        "reticulumpi:\n  plugins:\n    messaging_hub:\n      enabled:\ttrue\n",
        "reticulumpi:\n  plugins:\n    messaging_hub:\n      enabled:\u00a0true\n",
        "reticulumpi:\n  plugins:\n    messaging_hub:\n      enabled: true\u00a0# comment\n",
        "reticulumpi:\n  plugins:\n    messaging_hub: {enabled: true}\n",
        "reticulumpi:\n  plugins:\n    sensor_framework:\n      enabled: true\n      storage: sqlite\n",
        "reticulumpi:\n  plugins: []\n",
        "reticulumpi:\n  plugins:\n    - malformed\n",
    ],
)
def test_rejects_ambiguous_or_unsupported_yaml(tmp_path: Path, text: str) -> None:
    with pytest.raises(RecoveryConfigError):
        _load(tmp_path, text)


@pytest.mark.parametrize(
    "text",
    [
        "reticulumpi:\n  plugins: {}\nreticulumpi:\n  plugins: {}\n",
        "reticulumpi:\n  plugins: {}\n  plugins: {}\n",
        "reticulumpi:\n  plugins:\n    messaging_hub:\n      enabled: true\n    messaging_hub:\n      enabled: true\n",
        "reticulumpi:\n  plugins:\n    messaging_hub:\n      enabled: true\n      enabled: false\n",
        "reticulumpi:\n  plugins:\n    messaging_hub:\n      enabled: true\n      'enabled': false\n",
        "reticulumpi:\n  plugins:\n    messaging_hub:\n      enabled: true\n      db_path: one.db\n      db_path: two.db\n",
        "reticulumpi:\n  plugins:\n    sensor_framework:\n      enabled: true\n      storage:\n        path: one.db\n        path: two.db\n",
    ],
)
def test_rejects_duplicate_relevant_mappings(tmp_path: Path, text: str) -> None:
    with pytest.raises(RecoveryConfigError, match="more than once|one top-level"):
        _load(tmp_path, text)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "~",
        "null",
        "path with spaces.db",
        "[messages.db]",
        '""',
        '"valid" trailing',
        "false",
        "123",
        "-2",
        "1.5",
        "1.0e+2",
        "1.0e-2",
        ".5e+2",
        "5.e+2",
        "2026-07-13",
        "<<",
        "=",
        "%foo",
        ",foo",
        "]foo",
        "}foo",
    ],
)
def test_rejects_invalid_database_path_scalars(tmp_path: Path, value: str) -> None:
    text = (
        "reticulumpi:\n"
        "  plugins:\n"
        "    messaging_hub:\n"
        "      enabled: true\n"
        f"      db_path: {value}\n"
    )
    with pytest.raises(RecoveryConfigError):
        _load(tmp_path, text)


def test_rejects_missing_nonregular_symlink_and_invalid_utf8(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"
    with pytest.raises(RecoveryConfigError, match="unavailable or unsafe"):
        load_migration_plugin_configs(missing)

    directory = tmp_path / "directory.yaml"
    directory.mkdir()
    with pytest.raises(RecoveryConfigError, match="regular file"):
        load_migration_plugin_configs(directory)

    target = tmp_path / "target.yaml"
    target.write_text("reticulumpi:\n  plugins: {}\n", encoding="utf-8")
    link = tmp_path / "link.yaml"
    link.symlink_to(target)
    with pytest.raises(RecoveryConfigError, match="unavailable or unsafe"):
        load_migration_plugin_configs(link)

    invalid = tmp_path / "invalid.yaml"
    invalid.write_bytes(b"reticulumpi:\n  plugins: \xff\n")
    with pytest.raises(RecoveryConfigError, match="valid UTF-8"):
        load_migration_plugin_configs(invalid)


def test_rejects_oversized_and_untrusted_configuration(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.yaml"
    oversized.write_bytes(b" " * (MAX_CONFIG_BYTES + 1))
    with pytest.raises(RecoveryConfigError, match="1 MiB"):
        load_migration_plugin_configs(oversized)

    untrusted = tmp_path / "untrusted.yaml"
    untrusted.write_text("reticulumpi:\n  plugins: {}\n", encoding="utf-8")
    os.chmod(untrusted, 0o666)
    with pytest.raises(RecoveryConfigError, match="root-owned"):
        load_migration_plugin_configs(untrusted, require_trusted=True)


def test_enabled_sensor_without_storage_uses_catalog_default(tmp_path: Path) -> None:
    assert _load(
        tmp_path,
        "reticulumpi:\n  plugins:\n    sensor_framework:\n      enabled: true\n",
    ) == {"sensor_framework": {}}
