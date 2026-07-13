"""Regression tests for the dependency-free recovery migration catalog."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from reticulumpi.migration_catalog import MIGRATION_PLUGIN_NAMES, migration_targets
from reticulumpi.migrations import migrate_target


EXPECTED_CHECKSUMS = {
    "messaging_hub": "12b2e1c83e074d185961b16e54a658c4b3da4e060fe8c316527b42ced6c0a8f8",
    "network_map": "2eabe56521267ba2ab386909c94ebba664821dec15f1cfd0d84c008509de9a11",
    "node_location_tracker": "f68293915918a65a83efc3a960fc1a03bb0e643f0e4f5cd26514e04e8d8ddd98",
    "sensor_framework": "0e960d6da3be54b6350ec04a8420a3fbf2d1296b7bdc53409be5ad04994e6a5a",
    "transport_health": "53ad6430870d0ef803b7123e22f2ad430dc3c4c838988edf5fb428eb174928ae",
}


@pytest.mark.parametrize(
    ("name", "config", "filename", "tables"),
    [
        ("messaging_hub", "db_path", "messages.db", {"messages"}),
        ("network_map", "db_path", "network-map.db", {"known_nodes", "interface_stats"}),
        (
            "node_location_tracker",
            "db_path",
            "node-positions.db",
            {"node_positions"},
        ),
        ("sensor_framework", "storage", "sensors.db", {"sensor_readings"}),
        (
            "transport_health",
            "db_path",
            "transport-health.db",
            {"transport_nodes", "transport_node_history"},
        ),
    ],
)
def test_catalog_histories_are_frozen_and_dry_runnable(
    tmp_path: Path,
    name: str,
    config: str,
    filename: str,
    tables: set[str],
) -> None:
    path = tmp_path / filename
    value = {config: {"type": "sqlite", "path": path}} if config == "storage" else {config: path}

    target = migration_targets(name, value)[0]

    assert target.name == name
    assert target.path == path
    assert [migration.version for migration in target.migrations] == [1]
    assert target.migrations[0].stable_checksum == EXPECTED_CHECKSUMS[name]

    result = migrate_target(target, dry_run=True)
    assert result.applied == (1,)
    assert not path.exists()

    result = migrate_target(target, dry_run=False)
    assert result.applied == (1,)
    with closing(sqlite3.connect(path)) as connection:
        found = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert tables <= found
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)


def test_catalog_plugin_set_and_checksums_are_complete() -> None:
    assert MIGRATION_PLUGIN_NAMES == frozenset(EXPECTED_CHECKSUMS)


@pytest.mark.parametrize(
    "storage_type",
    ["csv", "none"],
)
def test_supported_non_sqlite_sensor_storage_declares_no_target(
    storage_type: str,
) -> None:
    assert (
        migration_targets(
            "sensor_framework",
            {"storage": {"type": storage_type, "path": "ignored.db"}},
        )
        == ()
    )


@pytest.mark.parametrize(
    "storage",
    [
        {"type": "postgres", "path": "ignored.db"},
        "not-a-mapping",
    ],
)
def test_malformed_sensor_storage_is_rejected(storage: object) -> None:
    with pytest.raises((TypeError, ValueError), match="sensor_framework.storage"):
        migration_targets("sensor_framework", {"storage": storage})


def test_catalog_rejects_unknown_plugins() -> None:
    with pytest.raises(ValueError, match="unsupported migration plugin"):
        migration_targets("unknown", {})
