"""Dependency-free SQLite migration declarations shared with recovery tooling.

The independently packaged administrator runs with ``python -I -S`` and an
empty third-party runtime.  Keep this module limited to the standard library
and :mod:`reticulumpi.migrations` so database planning never imports plugin
implementations, Reticulum, or the normal YAML configuration loader.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from reticulumpi.migrations import Migration, MigrationTarget


MIGRATION_PLUGIN_NAMES = frozenset(
    {
        "messaging_hub",
        "network_map",
        "node_location_tracker",
        "sensor_framework",
        "transport_health",
    }
)


def _expanded_path(value: object) -> Path:
    return Path(os.path.expanduser(os.fspath(value)))


def _messaging_hub(config: Mapping[str, Any]) -> tuple[MigrationTarget, ...]:
    path = _expanded_path(config.get("db_path", "~/.local/share/reticulumpi/messaging_hub.db"))
    return (
        MigrationTarget(
            "messaging_hub",
            path,
            (
                Migration(
                    1,
                    "adopt legacy message store",
                    (
                        "CREATE TABLE IF NOT EXISTS messages (\n"
                        "                                id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
                        "                                timestamp REAL NOT NULL,\n"
                        "                                transport TEXT NOT NULL,\n"
                        "                                direction TEXT NOT NULL,\n"
                        "                                msg_type TEXT NOT NULL,\n"
                        "                                from_id TEXT,\n"
                        "                                from_name TEXT,\n"
                        "                                to_id TEXT,\n"
                        "                                to_name TEXT,\n"
                        "                                text TEXT NOT NULL,\n"
                        "                                status TEXT DEFAULT 'sent',\n"
                        "                                metadata TEXT\n"
                        "                            )",
                        "CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages(timestamp)",
                        "CREATE INDEX IF NOT EXISTS idx_msg_transport ON messages(transport)",
                    ),
                ),
            ),
        ),
    )


def _network_map(config: Mapping[str, Any]) -> tuple[MigrationTarget, ...]:
    path = _expanded_path(config.get("db_path", "~/.local/share/reticulumpi/network_map.db"))
    statements = (
        "CREATE TABLE IF NOT EXISTS known_nodes (\n"
        "                destination_hash TEXT PRIMARY KEY,\n"
        "                app_name TEXT,\n"
        "                aspects TEXT,\n"
        "                hops INTEGER,\n"
        "                last_seen REAL,\n"
        "                first_seen REAL,\n"
        "                announce_count INTEGER,\n"
        "                app_data_str TEXT\n"
        "            )",
        "CREATE TABLE IF NOT EXISTS interface_stats (\n"
        "                timestamp REAL,\n"
        "                name TEXT,\n"
        "                type TEXT,\n"
        "                online INTEGER,\n"
        "                rxb INTEGER,\n"
        "                txb INTEGER,\n"
        "                bitrate INTEGER,\n"
        "                peers INTEGER\n"
        "            )",
        "CREATE INDEX IF NOT EXISTS idx_known_nodes_app_name ON known_nodes(app_name)",
        "CREATE INDEX IF NOT EXISTS idx_known_nodes_last_seen ON known_nodes(last_seen DESC)",
        "CREATE INDEX IF NOT EXISTS idx_known_nodes_announce_count "
        "ON known_nodes(announce_count DESC)",
        "CREATE INDEX IF NOT EXISTS idx_known_nodes_hops ON known_nodes(hops)",
        "CREATE INDEX IF NOT EXISTS idx_known_nodes_app_lastseen "
        "ON known_nodes(app_name, last_seen DESC)",
        "CREATE INDEX IF NOT EXISTS idx_interface_stats_timestamp ON interface_stats(timestamp)",
    )
    return (
        MigrationTarget(
            "network_map",
            path,
            (Migration(1, "adopt network map schema", statements),),
        ),
    )


def _node_location_tracker(config: Mapping[str, Any]) -> tuple[MigrationTarget, ...]:
    path = _expanded_path(config.get("db_path", "~/.local/share/reticulumpi/node_positions.db"))
    return (
        MigrationTarget(
            "node_location_tracker",
            path,
            (
                Migration(
                    1,
                    "create node position history",
                    (
                        "CREATE TABLE IF NOT EXISTS node_positions (\n"
                        "                                node_key TEXT NOT NULL,\n"
                        "                                timestamp REAL NOT NULL,\n"
                        "                                latitude REAL NOT NULL,\n"
                        "                                longitude REAL NOT NULL,\n"
                        "                                source TEXT NOT NULL,\n"
                        "                                name TEXT,\n"
                        "                                PRIMARY KEY (node_key, timestamp)\n"
                        "                            )",
                        "CREATE INDEX IF NOT EXISTS idx_np_ts ON node_positions(timestamp)",
                    ),
                ),
            ),
        ),
    )


def _sensor_framework(config: Mapping[str, Any]) -> tuple[MigrationTarget, ...]:
    storage = config.get("storage", {})
    if not isinstance(storage, Mapping):
        raise TypeError("sensor_framework.storage must be a mapping")
    storage_type = storage.get("type", "sqlite")
    if storage_type not in {"sqlite", "csv", "none"}:
        raise ValueError("sensor_framework.storage.type must be sqlite, csv, or none")
    if storage_type != "sqlite":
        return ()
    path = _expanded_path(storage.get("path", "~/.local/share/reticulumpi/sensor_data.db"))
    return (
        MigrationTarget(
            "sensor_framework",
            path,
            (
                Migration(
                    1,
                    "create sensor reading history",
                    (
                        "CREATE TABLE IF NOT EXISTS sensor_readings (\n"
                        "                                id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
                        "                                sensor_name TEXT NOT NULL,\n"
                        "                                reading_name TEXT NOT NULL,\n"
                        "                                value REAL NOT NULL,\n"
                        "                                timestamp REAL NOT NULL\n"
                        "                            )",
                        "CREATE INDEX IF NOT EXISTS idx_readings_sensor_time "
                        "ON sensor_readings(sensor_name, timestamp)",
                    ),
                ),
            ),
        ),
    )


def _transport_health(config: Mapping[str, Any]) -> tuple[MigrationTarget, ...]:
    path = _expanded_path(config.get("db_path", "~/.local/share/reticulumpi/transport_health.db"))
    statements = (
        "CREATE TABLE IF NOT EXISTS transport_nodes (\n"
        "                hash TEXT PRIMARY KEY,\n"
        "                first_seen REAL,\n"
        "                last_seen REAL,\n"
        "                paths_via INTEGER,\n"
        "                max_paths_via INTEGER,\n"
        "                total_appearances INTEGER,\n"
        "                total_checks INTEGER,\n"
        "                availability_pct REAL,\n"
        "                interface TEXT,\n"
        "                status TEXT,\n"
        "                node_name TEXT\n"
        "            )",
        "CREATE TABLE IF NOT EXISTS transport_node_history (\n"
        "                hash TEXT,\n"
        "                timestamp REAL,\n"
        "                paths_via INTEGER,\n"
        "                status TEXT,\n"
        "                PRIMARY KEY (hash, timestamp)\n"
        "            )",
        "CREATE INDEX IF NOT EXISTS idx_history_ts ON transport_node_history(timestamp)",
    )
    return (
        MigrationTarget(
            "transport_health",
            path,
            (Migration(1, "adopt transport health schema", statements),),
        ),
    )


def migration_targets(
    name: str,
    config: Mapping[str, Any],
) -> tuple[MigrationTarget, ...]:
    """Return the immutable migration history for one fixed built-in plugin."""

    builders = {
        "messaging_hub": _messaging_hub,
        "network_map": _network_map,
        "node_location_tracker": _node_location_tracker,
        "sensor_framework": _sensor_framework,
        "transport_health": _transport_health,
    }
    try:
        builder = builders[name]
    except KeyError as exc:
        raise ValueError(f"unsupported migration plugin: {name}") from exc
    return builder(config)
