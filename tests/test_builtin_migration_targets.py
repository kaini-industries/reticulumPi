"""Regression tests for built-in declarative SQLite migration targets."""

from contextlib import closing
import sqlite3

import pytest

from reticulumpi.builtin_plugins.messaging_hub import MessagingHubPlugin
from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin
from reticulumpi.builtin_plugins.node_location_tracker import NodeLocationTrackerPlugin
from reticulumpi.builtin_plugins.sensor_framework import SensorFrameworkPlugin
from reticulumpi.builtin_plugins.transport_health import TransportHealthPlugin
from reticulumpi.migrations import migrate_target


@pytest.mark.parametrize(
    ("plugin_class", "config", "tables"),
    [
        (
            NodeLocationTrackerPlugin,
            {"db_path": "node-locations.db"},
            {"node_positions"},
        ),
        (
            NetworkMapPlugin,
            {"db_path": "network-map.db"},
            {"known_nodes", "interface_stats"},
        ),
        (
            TransportHealthPlugin,
            {"db_path": "transport-health.db"},
            {"transport_nodes", "transport_node_history"},
        ),
        (
            SensorFrameworkPlugin,
            {"storage": {"type": "sqlite", "path": "sensors.db"}},
            {"sensor_readings"},
        ),
        (
            MessagingHubPlugin,
            {"db_path": "messages.db"},
            {"messages"},
        ),
    ],
)
def test_builtin_target_is_dry_runnable_and_atomic(tmp_path, plugin_class, config, tables):
    plugin = object.__new__(plugin_class)
    plugin.config = config
    target = plugin.get_migration_targets()[0]
    target = type(target)(target.name, tmp_path / target.path.name, target.migrations)

    dry_run = migrate_target(target, dry_run=True)
    assert dry_run.applied == (1,)
    assert not target.path.exists()

    applied = migrate_target(target, dry_run=False)
    assert applied.applied == (1,)
    # sqlite3's context manager controls transactions but does not own/close
    # the connection.  Explicit ownership keeps the warnings-as-errors lane
    # from deferring five live handles until interpreter shutdown.
    with closing(sqlite3.connect(target.path)) as connection:
        found = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert tables <= found
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_non_sqlite_sensor_storage_declares_no_target(tmp_path):
    plugin = object.__new__(SensorFrameworkPlugin)
    plugin.config = {"storage": {"type": "csv", "path": str(tmp_path / "sensors.csv")}}
    assert plugin.get_migration_targets() == ()
