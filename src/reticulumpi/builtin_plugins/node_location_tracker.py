"""Node Location Tracker plugin -- records position history for mesh nodes.

Polls meshtastic_gateway and meshcore_gateway for node positions, deduplicates
by distance threshold, and stores a rolling history in a local SQLite database.
Provides get_history() and get_summary() for other plugins and the dashboard.
"""

from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
from typing import Any

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

_DEFAULT_DB_PATH = "~/.local/share/reticulumpi/node_positions.db"


class NodeLocationTrackerPlugin(PluginBase):
    """Tracks and persists mesh node positions over time."""

    plugin_name = "node_location_tracker"
    plugin_description = "Records position history for mesh network nodes"
    plugin_version = "1.0.0"

    def validate_config(self) -> None:
        interval = self.config.get("sample_interval", 120)
        if not isinstance(interval, (int, float)) or interval < 30:
            raise ValueError("sample_interval must be >= 30 seconds")
        dist = self.config.get("min_distance_m", 25)
        if not isinstance(dist, (int, float)) or dist < 0:
            raise ValueError("min_distance_m must be >= 0")
        ret = self.config.get("retention_days", 30)
        if not isinstance(ret, (int, float)) or ret < 1:
            raise ValueError("retention_days must be >= 1")

    def start(self) -> None:
        self._sample_interval: int = self.config.get("sample_interval", 120)
        self._min_distance_m: float = float(self.config.get("min_distance_m", 25))
        self._max_silence_s: float = float(
            self.config.get("max_silence_minutes", 60)
        ) * 60
        self._retention_days: int = self.config.get("retention_days", 30)
        self._max_rows: int = self.config.get("max_rows", 500000)
        self._db_path: str = os.path.expanduser(
            self.config.get("db_path", _DEFAULT_DB_PATH)
        )

        self._last_pos: dict[str, tuple[float, float, float]] = {}
        self._lock = threading.Lock()
        self._no_gateways_logged = False

        self._init_db()
        self._load_last_positions()
        self._active = True
        self._start_thread(self._poll_loop, name="node-loc-poll")
        self._start_thread(self._prune_loop, name="node-loc-prune")
        self.log.info(
            "Node location tracker started (interval=%ds, db=%s)",
            self._sample_interval,
            self._db_path,
        )

    def stop(self) -> None:
        self._active = False
        self._join_threads(timeout=5)

    def _init_db(self) -> None:
        db_dir = os.path.dirname(self._db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS node_positions (
                    node_key   TEXT    NOT NULL,
                    timestamp  REAL    NOT NULL,
                    latitude   REAL    NOT NULL,
                    longitude  REAL    NOT NULL,
                    source     TEXT    NOT NULL,
                    name       TEXT,
                    PRIMARY KEY (node_key, timestamp)
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_np_ts "
                "ON node_positions (timestamp)"
            )
            conn.commit()
        finally:
            conn.close()

    def _load_last_positions(self) -> None:
        """Populate _last_pos from the most recent position per node."""
        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT node_key, latitude, longitude, MAX(timestamp) AS ts "
                    "FROM node_positions GROUP BY node_key"
                ).fetchall()
                for r in rows:
                    self._last_pos[r["node_key"]] = (
                        r["latitude"],
                        r["longitude"],
                        r["ts"],
                    )
                if rows:
                    self.log.info(
                        "Loaded last positions for %d nodes from database",
                        len(rows),
                    )
            finally:
                conn.close()
        except Exception:
            self.log.debug("Error loading last positions", exc_info=True)

    def _poll_loop(self) -> None:
        self._sleep_while_active(min(self._sample_interval, 15))
        while self._active:
            try:
                self._collect_positions()
            except Exception:
                self.log.exception("Error collecting positions")
            self._sleep_while_active(self._sample_interval)

    def _prune_loop(self) -> None:
        self._sleep_while_active(3600)
        while self._active:
            try:
                self._prune()
            except Exception:
                self.log.exception("Error pruning positions")
            self._sleep_while_active(3600)

    def _collect_positions(self) -> None:
        """Gather positions from gateway plugins and record new ones."""
        msh_gw = self.app.get_plugin("meshtastic_gateway")
        mc_gw = self.app.get_plugin("meshcore_gateway")
        if msh_gw is None and mc_gw is None:
            if not self._no_gateways_logged:
                self.log.info("No gateway plugins available — skipping collection")
                self._no_gateways_logged = True
            return
        self._no_gateways_logged = False

        positions: list[tuple[str, float, float, str, str | None]] = []
        if msh_gw is not None:
            try:
                nodes = msh_gw.get_meshtastic_nodes()
                for node in nodes:
                    nid = node.get("id")
                    lat = node.get("latitude")
                    lon = node.get("longitude")
                    name = node.get("long_name")
                    if self._valid_position(lat, lon):
                        positions.append(
                            (f"msh:{nid}", float(lat), float(lon), "meshtastic", name)
                        )
            except Exception:
                self.log.debug("Error reading meshtastic nodes", exc_info=True)

            # LoRa neighbors (may have positions not in the main node list)
            try:
                neighbors = msh_gw.get_lora_neighbors()
                seen_keys = {p[0] for p in positions}
                for nb in neighbors:
                    nid = nb.get("id")
                    key = f"msh:{nid}"
                    if key in seen_keys:
                        continue
                    lat = nb.get("latitude")
                    lon = nb.get("longitude")
                    name = nb.get("long_name")
                    if self._valid_position(lat, lon):
                        positions.append(
                            (key, float(lat), float(lon), "meshtastic", name)
                        )
                        seen_keys.add(key)
            except Exception:
                self.log.debug("Error reading lora neighbors", exc_info=True)

        # MeshCore gateway
        if mc_gw is not None:
            try:
                contacts = mc_gw.get_contacts()
                for contact in contacts:
                    pk = contact.get("public_key")
                    lat = contact.get("latitude")
                    lon = contact.get("longitude")
                    name = contact.get("name")
                    if self._valid_position(lat, lon):
                        positions.append(
                            (f"mc:{pk}", float(lat), float(lon), "meshcore", name)
                        )
            except Exception:
                self.log.debug("Error reading meshcore contacts", exc_info=True)

        if not positions:
            return

        now = time.time()
        to_insert: list[tuple[str, float, float, float, str, str | None]] = []

        with self._lock:
            for node_key, lat, lon, source, name in positions:
                last = self._last_pos.get(node_key)
                if last is not None:
                    last_lat, last_lon, last_ts = last
                    dist = _haversine_m(last_lat, last_lon, lat, lon)
                    elapsed = now - last_ts
                    if dist < self._min_distance_m and elapsed < self._max_silence_s:
                        continue
                self._last_pos[node_key] = (lat, lon, now)
                to_insert.append((node_key, now, lat, lon, source, name))

        if to_insert:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO node_positions VALUES (?, ?, ?, ?, ?, ?)",
                    to_insert,
                )
                conn.commit()
            finally:
                conn.close()
            self.app.event_bus.publish(
                events.NODE_POSITION_RECORDED,
                {"count": len(to_insert)},
            )

    def _prune(self) -> None:
        """Remove rows older than retention_days and enforce max_rows cap."""
        cutoff = time.time() - self._retention_days * 86400
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "DELETE FROM node_positions WHERE timestamp < ?", (cutoff,)
            )
            conn.commit()

            count = conn.execute(
                "SELECT COUNT(*) FROM node_positions"
            ).fetchone()[0]
            if count > self._max_rows:
                excess = count - self._max_rows
                conn.execute(
                    "DELETE FROM node_positions WHERE rowid IN "
                    "(SELECT rowid FROM node_positions ORDER BY timestamp ASC LIMIT ?)",
                    (excess,),
                )
                conn.commit()
        finally:
            conn.close()

        # Prune the in-memory last-position cache using the same cutoff so it
        # does not grow unbounded as nodes go stale (the DB prune above only
        # touches sqlite). _prune is called only from _prune_loop, which does
        # not hold self._lock, so acquiring it here cannot deadlock.
        with self._lock:
            stale = [k for k, v in self._last_pos.items() if v[2] < cutoff]
            for k in stale:
                del self._last_pos[k]

    def get_history(
        self,
        node_keys: list[str],
        since: float = 0,
        until: float | None = None,
        limit_per_node: int | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return position history for the given node keys.

        Returns a dict mapping each node_key to a list of position entries
        sorted by timestamp ascending.
        """
        if until is None:
            until = time.time() + 1

        result: dict[str, list[dict[str, Any]]] = {}
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            for key in node_keys:
                params: list[Any] = [key, since, until]
                if limit_per_node is not None:
                    # Select the newest N rows in the window, then reverse so the
                    # returned list stays ascending (docstring contract).
                    sql = (
                        "SELECT * FROM node_positions "
                        "WHERE node_key = ? AND timestamp >= ? AND timestamp <= ? "
                        "ORDER BY timestamp DESC LIMIT ?"
                    )
                    params.append(limit_per_node)
                    rows = list(reversed(conn.execute(sql, params).fetchall()))
                else:
                    sql = (
                        "SELECT * FROM node_positions "
                        "WHERE node_key = ? AND timestamp >= ? AND timestamp <= ? "
                        "ORDER BY timestamp ASC"
                    )
                    rows = conn.execute(sql, params).fetchall()
                result[key] = [
                    {
                        "timestamp": r["timestamp"],
                        "latitude": r["latitude"],
                        "longitude": r["longitude"],
                        "source": r["source"],
                        "name": r["name"],
                    }
                    for r in rows
                ]
        finally:
            conn.close()
        return result

    def get_summary(self) -> dict[str, Any]:
        """Return a summary of the position database."""
        conn = sqlite3.connect(self._db_path)
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM node_positions"
            ).fetchone()[0]
            nodes = conn.execute(
                "SELECT COUNT(DISTINCT node_key) FROM node_positions"
            ).fetchone()[0]
            oldest_row = conn.execute(
                "SELECT MIN(timestamp) FROM node_positions"
            ).fetchone()[0]
        finally:
            conn.close()

        db_size = 0
        try:
            db_size = os.path.getsize(self._db_path)
        except OSError:
            pass

        return {
            "total_nodes_tracked": nodes,
            "total_positions": total,
            "oldest_record": oldest_row or 0,
            "db_size_bytes": db_size,
        }

    @staticmethod
    def _valid_position(lat: Any, lon: Any) -> bool:
        """Return True if lat/lon are valid and not 0,0."""
        if lat is None or lon is None:
            return False
        try:
            flat = float(lat)
            flon = float(lon)
        except (TypeError, ValueError):
            return False
        if flat == 0.0 and flon == 0.0:
            return False
        return True


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the great-circle distance between two points in meters."""
    R = 6371000.0
    rlat1 = math.radians(lat1)
    rlat2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
