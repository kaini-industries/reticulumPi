"""Tests for the node_location_tracker plugin."""

from __future__ import annotations

import os
import sqlite3
import time
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi.event_bus import EventBus
from reticulumpi.runtime_metrics import get_runtime_metrics


@pytest.fixture
def mock_app():
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.identity.hash = b"\x01" * 16
    app.event_bus = EventBus()
    app.plugins = {}
    app.node_name = "TestNode"
    app.get_plugin = MagicMock(return_value=None)
    app.announce_dispatcher = MagicMock()
    app.internet_probe = None
    return app


@pytest.fixture
def base_config(tmp_path):
    return {
        "enabled": True,
        "sample_interval": 120,
        "min_distance_m": 25,
        "max_silence_minutes": 60,
        "retention_days": 30,
        "max_rows": 500000,
        "db_path": str(tmp_path / "node_positions.db"),
    }


def _make_plugin(mock_app, config):
    from reticulumpi.builtin_plugins.node_location_tracker import (
        NodeLocationTrackerPlugin,
    )

    return NodeLocationTrackerPlugin(mock_app, config)


def _mock_meshtastic_gateway(nodes=None, lora_neighbors=None):
    gw = MagicMock()
    gw.plugin_name = "meshtastic_gateway"
    gw.get_meshtastic_nodes = MagicMock(return_value=nodes or [])
    gw.get_lora_neighbors = MagicMock(return_value=lora_neighbors or [])
    return gw


def _mock_meshcore_gateway(contacts=None):
    gw = MagicMock()
    gw.plugin_name = "meshcore_gateway"
    gw.get_contacts = MagicMock(return_value=contacts or [])
    return gw


def _meshtastic_node(node_id, lat, lon, name=None):
    return {
        "id": node_id,
        "long_name": name,
        "short_name": None,
        "hw_model": None,
        "snr": -5.0,
        "last_heard": time.time(),
        "latitude": lat,
        "longitude": lon,
        "via_mqtt": False,
        "via_lora": True,
    }


def _meshcore_contact(public_key, lat, lon, name=None):
    return {
        "public_key": public_key,
        "name": name or "",
        "type": 0,
        "last_advert": time.time(),
        "latitude": lat,
        "longitude": lon,
        "flags": 0,
        "out_path_len": 1,
    }


def _lora_neighbor(node_id, lat, lon, name=None):
    return {
        "id": node_id,
        "long_name": name,
        "short_name": None,
        "hw_model": None,
        "hops_away": 1,
        "snr": -3.0,
        "last_heard": time.time(),
        "latitude": lat,
        "longitude": lon,
    }


def _db_row_count(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM node_positions").fetchone()[0]
    finally:
        conn.close()


def _db_rows(db_path, node_key=None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if node_key:
            rows = conn.execute(
                "SELECT * FROM node_positions WHERE node_key = ? ORDER BY timestamp",
                (node_key,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM node_positions ORDER BY timestamp").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


class TestLifecycle:
    def test_plugin_starts_and_stops(self, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        assert plugin._active is True
        plugin.stop()
        assert plugin._active is False

    def test_connection_setup_failure_closes_handle(self, mock_app, base_config):
        from reticulumpi.builtin_plugins import node_location_tracker

        plugin = _make_plugin(mock_app, base_config)
        plugin._db_path = base_config["db_path"]
        connection = MagicMock()
        connection.execute.side_effect = sqlite3.OperationalError("pragma failed")

        before = get_runtime_metrics()["sqlite_failures_total"]
        with patch.object(node_location_tracker.sqlite3, "connect", return_value=connection):
            with pytest.raises(sqlite3.OperationalError, match="pragma failed"):
                plugin._connect()

        connection.close.assert_called_once_with()
        assert get_runtime_metrics()["sqlite_failures_total"] == before + 1

    def test_db_schema_created(self, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        conn = sqlite3.connect(base_config["db_path"])
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='node_positions'"
            ).fetchall()
            assert len(tables) == 1

            indexes = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_np_ts'"
            ).fetchall()
            assert len(indexes) == 1

            cols = conn.execute("PRAGMA table_info(node_positions)").fetchall()
            col_names = {c[1] for c in cols}
            assert col_names >= {"node_key", "timestamp", "latitude", "longitude", "source", "name"}
        finally:
            conn.close()

        plugin.stop()


class TestPositionRecording:
    def test_records_meshtastic_position(self, mock_app, base_config):
        msh_gw = _mock_meshtastic_gateway(
            nodes=[_meshtastic_node("!aabbccdd", 40.7128, -74.0060, "NodeAlpha")]
        )
        mock_app.get_plugin = MagicMock(
            side_effect=lambda name: {
                "meshtastic_gateway": msh_gw,
                "meshcore_gateway": None,
            }.get(name)
        )

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        plugin._collect_positions()

        rows = _db_rows(base_config["db_path"], "msh:!aabbccdd")
        assert len(rows) == 1
        assert abs(rows[0]["latitude"] - 40.7128) < 1e-6
        assert abs(rows[0]["longitude"] - (-74.0060)) < 1e-6
        assert rows[0]["source"] == "meshtastic"
        assert rows[0]["name"] == "NodeAlpha"

        plugin.stop()

    def test_records_meshcore_position(self, mock_app, base_config):
        mc_gw = _mock_meshcore_gateway(
            contacts=[_meshcore_contact("abc123def", 51.5074, -0.1278, "MCNode")]
        )
        mock_app.get_plugin = MagicMock(
            side_effect=lambda name: {
                "meshtastic_gateway": None,
                "meshcore_gateway": mc_gw,
            }.get(name)
        )

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        plugin._collect_positions()

        rows = _db_rows(base_config["db_path"], "mc:abc123def")
        assert len(rows) == 1
        assert abs(rows[0]["latitude"] - 51.5074) < 1e-6
        assert abs(rows[0]["longitude"] - (-0.1278)) < 1e-6
        assert rows[0]["source"] == "meshcore"
        assert rows[0]["name"] == "MCNode"

        plugin.stop()

    def test_skips_zero_zero_position(self, mock_app, base_config):
        msh_gw = _mock_meshtastic_gateway(nodes=[_meshtastic_node("!zeroed", 0.0, 0.0, "ZeroNode")])
        mock_app.get_plugin = MagicMock(
            side_effect=lambda name: {
                "meshtastic_gateway": msh_gw,
                "meshcore_gateway": None,
            }.get(name)
        )

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        plugin._collect_positions()

        assert _db_row_count(base_config["db_path"]) == 0
        plugin.stop()

    def test_skips_missing_latlon(self, mock_app, base_config):
        node_no_lat = {
            "id": "!nolat",
            "long_name": "NoLat",
            "short_name": None,
            "hw_model": None,
            "snr": -5.0,
            "last_heard": time.time(),
            "longitude": -74.0,
            "via_mqtt": False,
            "via_lora": True,
        }
        node_none = {
            "id": "!nopos",
            "long_name": "NoPos",
            "short_name": None,
            "hw_model": None,
            "snr": -5.0,
            "last_heard": time.time(),
            "latitude": None,
            "longitude": None,
            "via_mqtt": False,
            "via_lora": True,
        }
        msh_gw = _mock_meshtastic_gateway(nodes=[node_no_lat, node_none])
        mock_app.get_plugin = MagicMock(
            side_effect=lambda name: {
                "meshtastic_gateway": msh_gw,
                "meshcore_gateway": None,
            }.get(name)
        )

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        plugin._collect_positions()

        assert _db_row_count(base_config["db_path"]) == 0
        plugin.stop()

    def test_merges_lora_neighbors(self, mock_app, base_config):
        msh_gw = _mock_meshtastic_gateway(
            nodes=[],
            lora_neighbors=[_lora_neighbor("!lora01", 35.6895, 139.6917, "LoRaPeer")],
        )
        mock_app.get_plugin = MagicMock(
            side_effect=lambda name: {
                "meshtastic_gateway": msh_gw,
                "meshcore_gateway": None,
            }.get(name)
        )

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        plugin._collect_positions()

        rows = _db_rows(base_config["db_path"], "msh:!lora01")
        assert len(rows) == 1
        assert abs(rows[0]["latitude"] - 35.6895) < 1e-6
        assert rows[0]["name"] == "LoRaPeer"

        plugin.stop()

    def test_publishes_position_recorded_event(self, mock_app, base_config):
        from reticulumpi import events

        msh_gw = _mock_meshtastic_gateway(
            nodes=[_meshtastic_node("!evtnode", 40.7128, -74.0060, "EvtNode")]
        )
        mock_app.get_plugin = MagicMock(
            side_effect=lambda name: {
                "meshtastic_gateway": msh_gw,
                "meshcore_gateway": None,
            }.get(name)
        )

        received = []
        mock_app.event_bus.subscribe(
            events.NODE_POSITION_RECORDED,
            lambda et, data: received.append((et, data)),
        )

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        plugin._collect_positions()

        assert len(received) == 1
        assert received[0][0] == events.NODE_POSITION_RECORDED
        assert received[0][1] == {"count": 1}

        plugin.stop()


class TestDeduplication:
    def test_dedup_by_distance(self, mock_app, base_config):
        base_config["min_distance_m"] = 25

        msh_gw = _mock_meshtastic_gateway(
            nodes=[_meshtastic_node("!dedup", 40.7128, -74.0060, "DedupNode")]
        )
        mock_app.get_plugin = MagicMock(
            side_effect=lambda name: {
                "meshtastic_gateway": msh_gw,
                "meshcore_gateway": None,
            }.get(name)
        )

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        now = time.time()
        with patch("time.time", return_value=now):
            plugin._collect_positions()
        assert _db_row_count(base_config["db_path"]) == 1

        with patch("time.time", return_value=now + 130):
            plugin._collect_positions()
        assert _db_row_count(base_config["db_path"]) == 1

        moved_node = _meshtastic_node("!dedup", 40.71325, -74.0060, "DedupNode")
        msh_gw.get_meshtastic_nodes.return_value = [moved_node]

        with patch("time.time", return_value=now + 260):
            plugin._collect_positions()
        assert _db_row_count(base_config["db_path"]) == 2

        plugin.stop()

    def test_force_record_after_silence(self, mock_app, base_config):
        base_config["max_silence_minutes"] = 60

        msh_gw = _mock_meshtastic_gateway(
            nodes=[_meshtastic_node("!silence", 40.7128, -74.0060, "SilentNode")]
        )
        mock_app.get_plugin = MagicMock(
            side_effect=lambda name: {
                "meshtastic_gateway": msh_gw,
                "meshcore_gateway": None,
            }.get(name)
        )

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        now = time.time()
        with patch("time.time", return_value=now):
            plugin._collect_positions()
        assert _db_row_count(base_config["db_path"]) == 1

        with patch("time.time", return_value=now + 3601):
            plugin._collect_positions()
        assert _db_row_count(base_config["db_path"]) == 2

        plugin.stop()


class TestPruning:
    def test_prune_by_retention(self, mock_app, base_config):
        base_config["retention_days"] = 7

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        conn = sqlite3.connect(base_config["db_path"])
        now = time.time()
        old_ts = now - (8 * 86400)
        recent_ts = now - (1 * 86400)
        conn.execute(
            "INSERT INTO node_positions VALUES (?, ?, ?, ?, ?, ?)",
            ("msh:!old", old_ts, 40.0, -74.0, "meshtastic", "OldNode"),
        )
        conn.execute(
            "INSERT INTO node_positions VALUES (?, ?, ?, ?, ?, ?)",
            ("msh:!new", recent_ts, 41.0, -73.0, "meshtastic", "NewNode"),
        )
        conn.commit()
        conn.close()

        plugin._prune()

        rows = _db_rows(base_config["db_path"])
        assert len(rows) == 1
        assert rows[0]["node_key"] == "msh:!new"

        plugin.stop()

    def test_prune_drops_stale_last_pos(self, mock_app, base_config):
        retention_days = base_config.get("retention_days", 30)

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        now = time.time()
        cutoff = now - retention_days * 86400
        # Fresh entry stays, stale entry (older than the retention cutoff) is dropped.
        plugin._last_pos["msh:!fresh"] = (40.0, -74.0, now)
        plugin._last_pos["msh:!stale"] = (41.0, -73.0, cutoff - 86400)

        plugin._prune()

        assert "msh:!fresh" in plugin._last_pos
        assert "msh:!stale" not in plugin._last_pos

        plugin.stop()

    def test_prune_by_row_cap(self, mock_app, base_config):
        base_config["max_rows"] = 5
        base_config["retention_days"] = 365

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        conn = sqlite3.connect(base_config["db_path"])
        now = time.time()
        for i in range(10):
            conn.execute(
                "INSERT INTO node_positions VALUES (?, ?, ?, ?, ?, ?)",
                (f"msh:!n{i}", now - (10 - i) * 100, 40.0 + i * 0.001, -74.0, "meshtastic", None),
            )
        conn.commit()
        conn.close()

        plugin._prune()

        count = _db_row_count(base_config["db_path"])
        assert count <= 5

        rows = _db_rows(base_config["db_path"])
        timestamps = [r["timestamp"] for r in rows]
        assert timestamps == sorted(timestamps)
        assert rows[-1]["node_key"] == "msh:!n9"

        plugin.stop()


class TestGetHistory:
    def _populate(self, db_path):
        conn = sqlite3.connect(db_path)
        now = time.time()
        entries = [
            ("msh:!a1", now - 7200, 40.71, -74.00, "meshtastic", "AlphaA"),
            ("msh:!a1", now - 3600, 40.72, -74.01, "meshtastic", "AlphaA"),
            ("msh:!a1", now - 1800, 40.73, -74.02, "meshtastic", "AlphaA"),
            ("mc:beta", now - 5400, 51.50, -0.12, "meshcore", "BetaB"),
            ("mc:beta", now - 2700, 51.51, -0.13, "meshcore", "BetaB"),
        ]
        conn.executemany("INSERT INTO node_positions VALUES (?, ?, ?, ?, ?, ?)", entries)
        conn.commit()
        conn.close()
        return now

    def test_get_history(self, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        now = self._populate(base_config["db_path"])

        result = plugin.get_history(["msh:!a1", "mc:beta"], since=now - 8000)
        assert "msh:!a1" in result
        assert "mc:beta" in result
        assert len(result["msh:!a1"]) == 3
        assert len(result["mc:beta"]) == 2

        for entry in result["msh:!a1"]:
            assert "timestamp" in entry
            assert "latitude" in entry
            assert "longitude" in entry

        result_filtered = plugin.get_history(["msh:!a1"], since=now - 4000, until=now - 1000)
        assert len(result_filtered["msh:!a1"]) == 2

        result_absent = plugin.get_history(["msh:!missing"], since=now - 8000)
        assert result_absent.get("msh:!missing", []) == []

        plugin.stop()

    def test_get_history_limit_per_node(self, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        now = self._populate(base_config["db_path"])

        result = plugin.get_history(["msh:!a1"], since=now - 8000, limit_per_node=2)
        assert len(result["msh:!a1"]) == 2

        # Regression guard: must return the NEWEST 2 rows (now-3600, now-1800),
        # not the oldest 2, and still in ascending timestamp order.
        timestamps = [e["timestamp"] for e in result["msh:!a1"]]
        assert timestamps == [pytest.approx(now - 3600), pytest.approx(now - 1800)]

        plugin.stop()

    def test_get_history_no_limit_returns_all_ascending(self, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        now = self._populate(base_config["db_path"])

        result = plugin.get_history(["msh:!a1"], since=now - 8000)
        assert len(result["msh:!a1"]) == 3

        # No-limit path returns ALL rows in ascending timestamp order.
        timestamps = [e["timestamp"] for e in result["msh:!a1"]]
        assert timestamps == [
            pytest.approx(now - 7200),
            pytest.approx(now - 3600),
            pytest.approx(now - 1800),
        ]

        plugin.stop()


class TestGetSummary:
    def test_get_summary(self, mock_app, base_config):
        plugin = _make_plugin(mock_app, base_config)
        plugin.start()

        conn = sqlite3.connect(base_config["db_path"])
        now = time.time()
        conn.execute(
            "INSERT INTO node_positions VALUES (?, ?, ?, ?, ?, ?)",
            ("msh:!s1", now - 3600, 40.0, -74.0, "meshtastic", "Sum1"),
        )
        conn.execute(
            "INSERT INTO node_positions VALUES (?, ?, ?, ?, ?, ?)",
            ("mc:s2", now - 1800, 51.0, -0.1, "meshcore", "Sum2"),
        )
        conn.execute(
            "INSERT INTO node_positions VALUES (?, ?, ?, ?, ?, ?)",
            ("msh:!s1", now - 900, 40.01, -74.01, "meshtastic", "Sum1"),
        )
        conn.commit()
        conn.close()

        summary = plugin.get_summary()
        assert summary["total_nodes_tracked"] == 2
        assert summary["total_positions"] == 3
        assert summary["oldest_record"] <= now - 3600
        assert summary["db_size_bytes"] > 0
        assert os.path.exists(base_config["db_path"])

        plugin.stop()


class TestGracefulDegradation:
    def test_graceful_without_gateways(self, mock_app, base_config):
        mock_app.get_plugin = MagicMock(return_value=None)

        plugin = _make_plugin(mock_app, base_config)
        plugin.start()
        plugin._collect_positions()

        assert _db_row_count(base_config["db_path"]) == 0
        plugin.stop()
