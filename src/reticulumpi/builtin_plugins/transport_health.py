"""Transport Health plugin — monitors reliability of transport/relay nodes."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

# Defaults
_DEFAULT_CHECK_INTERVAL = 60
_DEFAULT_DB_PATH = "~/.local/share/reticulumpi/transport_health.db"
_DEFAULT_HISTORY_HOURS = 168  # 7 days
_DEFAULT_DOWN_THRESHOLD = 3
_DEFAULT_DEGRADED_PCT = 80
_DEFAULT_CRITICAL_PATHS = 5


class TransportHealthPlugin(PluginBase):
    """Tracks reliability of transport (relay) nodes in the mesh.

    Identifies transport nodes by analysing the ``via`` field in path table
    entries — any hash that appears as a next-hop for other destinations is
    a transport node.  Tracks their availability over time and alerts when
    critical relays go down.
    """

    plugin_name = "transport_health"
    plugin_version = "1.0.0"
    plugin_description = "Monitors reliability of transport/relay nodes"
    broadcast_tier = 1
    broadcast_keys = "transport_health"

    def validate_config(self) -> None:
        interval = self.config.get("check_interval", _DEFAULT_CHECK_INTERVAL)
        if not isinstance(interval, (int, float)) or interval < 10:
            raise ValueError("check_interval must be >= 10 seconds")
        threshold = self.config.get("down_threshold_checks", _DEFAULT_DOWN_THRESHOLD)
        if not isinstance(threshold, int) or threshold < 1:
            raise ValueError("down_threshold_checks must be >= 1")

    def start(self) -> None:
        self._active = True
        self._lock = threading.Lock()

        # In-memory transport node records keyed by hex hash
        self._transport_nodes: dict[str, dict[str, Any]] = {}

        # Set up SQLite
        db_path = os.path.expanduser(
            self.config.get("db_path", _DEFAULT_DB_PATH)
        )
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._db_path = db_path
        self._init_db()
        self._load_from_db()

        self._start_thread(self._monitor_loop, "transport-health")

        self.log.info(
            "Transport health active (interval=%ds, db=%s, "
            "down_threshold=%d, critical_paths=%d)",
            self.config.get("check_interval", _DEFAULT_CHECK_INTERVAL),
            db_path,
            self.config.get("down_threshold_checks", _DEFAULT_DOWN_THRESHOLD),
            self.config.get("critical_path_count", _DEFAULT_CRITICAL_PATHS),
        )

    def stop(self) -> None:
        self._active = False
        self._join_threads()

    # --- Public API ---

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            nodes = list(self._transport_nodes.values())
        healthy = sum(1 for n in nodes if n["status"] == "healthy")
        degraded = sum(1 for n in nodes if n["status"] == "degraded")
        down = sum(1 for n in nodes if n["status"] == "down")
        return {
            "active": self._active,
            "transport_nodes_tracked": len(nodes),
            "healthy": healthy,
            "degraded": degraded,
            "down": down,
        }

    def get_transport_nodes(self) -> list[dict[str, Any]]:
        """Return all tracked transport nodes with health metrics."""
        with self._lock:
            return sorted(
                list(self._transport_nodes.values()),
                key=lambda n: n.get("paths_via", 0),
                reverse=True,
            )

    def broadcast_snapshot(self, cycle_count: int = 0) -> dict | None:
        return self.get_transport_summary()

    def get_transport_summary(self) -> dict[str, Any]:
        """Aggregate summary for dashboard WebSocket broadcast."""
        with self._lock:
            nodes = list(self._transport_nodes.values())
        healthy = sum(1 for n in nodes if n["status"] == "healthy")
        degraded = sum(1 for n in nodes if n["status"] == "degraded")
        down = sum(1 for n in nodes if n["status"] == "down")
        new = sum(1 for n in nodes if n["status"] == "new")
        total_paths = sum(n.get("paths_via", 0) for n in nodes)
        return {
            "total": len(nodes),
            "healthy": healthy,
            "degraded": degraded,
            "down": down,
            "new": new,
            "total_paths_relayed": total_paths,
        }

    # --- Internal ---

    def _monitor_loop(self) -> None:
        interval = self.config.get("check_interval", _DEFAULT_CHECK_INTERVAL)

        # Let the system settle
        self._sleep_while_active(min(interval, 15))

        while self._active:
            try:
                self._run_check()
            except Exception:
                self.log.warning("Error in transport health check", exc_info=True)

            self._sleep_while_active(interval)

    def _run_check(self) -> None:
        """Execute one health check cycle."""
        down_threshold = self.config.get(
            "down_threshold_checks", _DEFAULT_DOWN_THRESHOLD
        )
        degraded_pct = self.config.get(
            "degraded_threshold_pct", _DEFAULT_DEGRADED_PCT
        )
        critical_paths = self.config.get(
            "critical_path_count", _DEFAULT_CRITICAL_PATHS
        )
        now = time.time()

        # Get current path table from connectivity_monitor
        current_via = self._get_current_via_map()

        with self._lock:
            # Update existing nodes
            for hex_hash, record in self._transport_nodes.items():
                record["total_checks"] += 1

                if hex_hash in current_via:
                    # Node is present
                    record["last_seen"] = now
                    record["paths_via"] = current_via[hex_hash]["count"]
                    record["interface"] = current_via[hex_hash].get(
                        "interface", record.get("interface", "")
                    )
                    record["max_paths_via"] = max(
                        record.get("max_paths_via", 0),
                        record["paths_via"],
                    )
                    record["total_appearances"] += 1
                    record["consecutive_absent"] = 0
                    record["consecutive_present"] = record.get(
                        "consecutive_present", 0
                    ) + 1
                    record["longest_present_streak"] = max(
                        record.get("longest_present_streak", 0),
                        record["consecutive_present"],
                    )
                else:
                    # Node is absent
                    record["paths_via"] = 0
                    record["consecutive_present"] = 0
                    record["consecutive_absent"] = record.get(
                        "consecutive_absent", 0
                    ) + 1
                    record["longest_absent_streak"] = max(
                        record.get("longest_absent_streak", 0),
                        record["consecutive_absent"],
                    )

                # Recalculate availability
                total_checks = record["total_checks"]
                total_appear = record["total_appearances"]
                record["availability_pct"] = (
                    (total_appear / total_checks * 100) if total_checks > 0 else 0
                )

                # Determine status
                old_status = record["status"]
                record["status"] = self._classify_status(
                    record, down_threshold, degraded_pct
                )

                # Detect transitions
                self._handle_transition(
                    hex_hash, old_status, record["status"],
                    record.get("paths_via", 0),
                    record.get("max_paths_via", 0),
                    critical_paths,
                )

            # Discover new transport nodes
            for hex_hash, info in current_via.items():
                if hex_hash not in self._transport_nodes:
                    name = self._resolve_node_name(hex_hash)
                    self._transport_nodes[hex_hash] = {
                        "hash": hex_hash,
                        "first_seen": now,
                        "last_seen": now,
                        "paths_via": info["count"],
                        "max_paths_via": info["count"],
                        "total_appearances": 1,
                        "total_checks": 1,
                        "consecutive_present": 1,
                        "consecutive_absent": 0,
                        "longest_present_streak": 1,
                        "longest_absent_streak": 0,
                        "availability_pct": 100.0,
                        "interface": info.get("interface", ""),
                        "status": "new",
                        "node_name": name,
                    }
                    self.log.info(
                        "Transport node discovered: %s%s (%d paths via)",
                        hex_hash[:16],
                        f" ({name})" if name else "",
                        info["count"],
                    )
                    self.event_bus.publish(
                        events.TRANSPORT_NODE_DISCOVERED,
                        {"hash": hex_hash, "paths_via": info["count"]},
                    )

        # Persist and prune
        self._persist_to_db()
        self._record_history(now)
        self._prune_history()

    @staticmethod
    def _classify_status(
        record: dict[str, Any],
        down_threshold: int,
        degraded_pct: float,
    ) -> str:
        """Determine node status based on current metrics."""
        # New nodes stay "new" for first 3 checks
        if record["total_checks"] <= 3:
            return "new"

        # Down if absent for too long
        if record.get("consecutive_absent", 0) >= down_threshold:
            return "down"

        # Degraded if availability is low (flapping)
        if record.get("availability_pct", 100) < degraded_pct:
            return "degraded"

        return "healthy"

    def _handle_transition(
        self,
        hex_hash: str,
        old_status: str,
        new_status: str,
        current_paths: int,
        max_paths: int,
        critical_threshold: int,
    ) -> None:
        """Publish events on status transitions."""
        if old_status == new_status:
            return

        if new_status == "down" and old_status in ("healthy", "degraded", "new"):
            self.log.warning(
                "Transport node DOWN: %s (was routing up to %d paths)",
                hex_hash[:16],
                max_paths,
            )
            event_data = {
                "hash": hex_hash,
                "max_paths_via": max_paths,
                "previous_status": old_status,
            }
            self.event_bus.publish(events.TRANSPORT_NODE_DOWN, event_data)

            if (
                self.config.get("alert_on_critical_down", True)
                and max_paths >= critical_threshold
            ):
                self.log.warning(
                    "CRITICAL: Transport node %s was carrying %d paths",
                    hex_hash[:16],
                    max_paths,
                )

        elif new_status == "healthy" and old_status == "down":
            self.log.info(
                "Transport node RECOVERED: %s (%d paths via)",
                hex_hash[:16],
                current_paths,
            )
            self.event_bus.publish(
                events.TRANSPORT_NODE_RECOVERED,
                {"hash": hex_hash, "paths_via": current_paths},
            )

        elif new_status == "degraded" and old_status == "healthy":
            self.log.info(
                "Transport node DEGRADED: %s", hex_hash[:16]
            )
            self.event_bus.publish(
                events.TRANSPORT_NODE_DEGRADED,
                {"hash": hex_hash},
            )

    def _get_current_via_map(self) -> dict[str, dict[str, Any]]:
        """Extract transport nodes from the current path table.

        Returns a dict mapping via_hex_hash -> {"count": N, "interface": "..."}.
        """
        via_map: dict[str, dict[str, Any]] = {}
        try:
            conn_mon = self.app.get_plugin("connectivity_monitor")
            if not conn_mon or not hasattr(conn_mon, "get_routing_data"):
                return via_map

            data = conn_mon.get_routing_data(per_page=500)
            for entry in data.get("paths", []):
                via = entry.get("via", "")
                if not via or via == "0" * len(via):
                    # Skip direct paths (no relay)
                    continue
                iface = entry.get("interface", "")
                if via not in via_map:
                    via_map[via] = {"count": 0, "interface": iface}
                via_map[via]["count"] += 1
        except Exception:
            self.log.debug(
                "Error reading path table from connectivity_monitor",
                exc_info=True,
            )
        return via_map

    def _resolve_node_name(self, hex_hash: str) -> str:
        """Try to get a human-readable name from network_map."""
        try:
            net_map = self.app.get_plugin("network_map")
            if not net_map or not hasattr(net_map, "get_known_nodes"):
                return ""
            for node in net_map.get_known_nodes():
                h = node.get("destination_hash", "")
                clean = h.replace("<", "").replace(">", "").replace(" ", "")
                if clean == hex_hash:
                    return node.get("app_data", "") or node.get("app_name", "")
        except Exception:
            pass
        return ""

    # --- SQLite ---

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transport_nodes (
                    hash TEXT PRIMARY KEY,
                    first_seen REAL,
                    last_seen REAL,
                    paths_via INTEGER,
                    max_paths_via INTEGER,
                    total_appearances INTEGER,
                    total_checks INTEGER,
                    availability_pct REAL,
                    interface TEXT,
                    status TEXT,
                    node_name TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transport_node_history (
                    hash TEXT,
                    timestamp REAL,
                    paths_via INTEGER,
                    status TEXT,
                    PRIMARY KEY (hash, timestamp)
                )
            """)

    def _load_from_db(self) -> None:
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                for row in conn.execute("SELECT * FROM transport_nodes"):
                    self._transport_nodes[row["hash"]] = {
                        "hash": row["hash"],
                        "first_seen": row["first_seen"],
                        "last_seen": row["last_seen"],
                        "paths_via": row["paths_via"],
                        "max_paths_via": row["max_paths_via"],
                        "total_appearances": row["total_appearances"],
                        "total_checks": row["total_checks"],
                        "consecutive_present": 0,
                        "consecutive_absent": 0,
                        "longest_present_streak": 0,
                        "longest_absent_streak": 0,
                        "availability_pct": row["availability_pct"],
                        "interface": row["interface"] or "",
                        "status": row["status"] or "new",
                        "node_name": row["node_name"] or "",
                    }
            self.log.info(
                "Loaded %d transport nodes from database",
                len(self._transport_nodes),
            )
        except Exception:
            self.log.debug("Error loading transport nodes from database", exc_info=True)

    def _persist_to_db(self) -> None:
        try:
            with self._lock:
                snapshot = list(self._transport_nodes.values())
            with sqlite3.connect(self._db_path) as conn:
                for record in snapshot:
                    conn.execute("""
                        INSERT OR REPLACE INTO transport_nodes
                        (hash, first_seen, last_seen, paths_via, max_paths_via,
                         total_appearances, total_checks, availability_pct,
                         interface, status, node_name)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        record["hash"],
                        record["first_seen"],
                        record["last_seen"],
                        record["paths_via"],
                        record["max_paths_via"],
                        record["total_appearances"],
                        record["total_checks"],
                        record["availability_pct"],
                        record.get("interface", ""),
                        record["status"],
                        record.get("node_name", ""),
                    ))
        except Exception:
            self.log.debug("Error persisting transport nodes", exc_info=True)

    def _record_history(self, now: float) -> None:
        try:
            with self._lock:
                snapshot = list(self._transport_nodes.values())
            with sqlite3.connect(self._db_path) as conn:
                for record in snapshot:
                    if record.get("paths_via", 0) > 0 or record["status"] != "new":
                        conn.execute("""
                            INSERT OR REPLACE INTO transport_node_history
                            (hash, timestamp, paths_via, status)
                            VALUES (?, ?, ?, ?)
                        """, (
                            record["hash"],
                            now,
                            record.get("paths_via", 0),
                            record["status"],
                        ))
        except Exception:
            self.log.debug("Error recording history", exc_info=True)

    def _prune_history(self) -> None:
        retention = self.config.get(
            "history_retention_hours", _DEFAULT_HISTORY_HOURS
        )
        cutoff = time.time() - (retention * 3600)
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "DELETE FROM transport_node_history WHERE timestamp < ?",
                    (cutoff,),
                )
                # Remove nodes not seen in retention period
                conn.execute(
                    "DELETE FROM transport_nodes WHERE last_seen < ?",
                    (cutoff,),
                )
            # Also prune from memory
            with self._lock:
                expired = [
                    h
                    for h, r in self._transport_nodes.items()
                    if r["last_seen"] < cutoff
                ]
                for h in expired:
                    del self._transport_nodes[h]
        except Exception:
            self.log.debug("Error pruning history", exc_info=True)
