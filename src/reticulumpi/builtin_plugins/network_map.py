"""Network Map plugin — passively monitors announces to build a mesh topology view."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any

import RNS

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase


class NetworkMapPlugin(PluginBase):
    """Passively monitors all Reticulum announces and builds a live map of
    known nodes, hop counts, and interface statistics.  Stores history in
    SQLite for trend analysis.
    """

    plugin_name = "network_map"
    plugin_version = "1.0.0"
    plugin_description = "Passive network topology mapping via announce monitoring"

    def validate_config(self) -> None:
        max_days = self.config.get("max_history_days", 30)
        if not isinstance(max_days, (int, float)) or max_days < 1:
            raise ValueError("max_history_days must be >= 1")

    def start(self) -> None:
        self._active = True
        self._known_nodes: dict[bytes, dict[str, Any]] = {}
        self._nodes_lock = threading.Lock()

        db_path = os.path.expanduser(
            self.config.get(
                "db_path", "~/.local/share/reticulumpi/network_map.db"
            )
        )
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._db_path = db_path
        self._init_db()

        # Load previously known nodes from DB
        self._load_from_db()

        # Register a wildcard announce handler
        self._handler = _AnnounceHandler(self)
        RNS.Transport.register_announce_handler(self._handler)

        # Background thread for periodic interface stats and DB pruning
        self._start_thread(self._maintenance_loop, "network-map")

        self.log.info(
            "Network map active — monitoring announces (DB: %s)", db_path
        )

    def stop(self) -> None:
        self._active = False
        try:
            RNS.Transport.deregister_announce_handler(self._handler)
        except Exception:
            pass
        self._join_threads()

    def get_status(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "known_nodes": len(self._known_nodes),
            "db_path": getattr(self, "_db_path", None),
        }

    def get_known_nodes(self) -> list[dict[str, Any]]:
        """Return all known nodes as a list of dicts (for API consumption)."""
        nodes = []
        with self._nodes_lock:
            items = list(self._known_nodes.items())
        for dest_hash, info in items:
            nodes.append({
                "destination_hash": RNS.prettyhexrep(dest_hash),
                "app_name": info.get("app_name", ""),
                "aspects": info.get("aspects", ""),
                "hops": info.get("hops"),
                "last_seen": info.get("last_seen"),
                "first_seen": info.get("first_seen"),
                "announce_count": info.get("announce_count", 0),
                "app_data": info.get("app_data_str", ""),
            })
        return sorted(nodes, key=lambda n: n.get("last_seen", 0), reverse=True)

    def get_interface_stats(self) -> list[dict[str, Any]]:
        """Collect current interface statistics."""
        stats = []
        try:
            for iface in RNS.Transport.interfaces:
                stat = {
                    "name": str(iface),
                    "type": type(iface).__name__,
                    "online": getattr(iface, "online", True),
                }
                for attr in ("rxb", "txb", "bitrate", "peers"):
                    val = getattr(iface, attr, None)
                    if val is not None:
                        if attr == "peers":
                            stat[attr] = len(val) if hasattr(val, "__len__") else val
                        else:
                            stat[attr] = val
                stats.append(stat)
        except Exception:
            self.log.debug("Error collecting interface stats", exc_info=True)
        return stats

    def record_announce(
        self,
        destination_hash: bytes,
        identity: Any,
        app_data: bytes | None,
        aspect: str,
    ) -> None:
        """Called by the announce handler when an announce is received."""
        now = time.time()
        hops = None
        try:
            hops = RNS.Transport.hops_to(destination_hash)
        except Exception:
            pass

        app_data_str = ""
        if app_data:
            # Try msgpack first (many nodes send structured data)
            try:
                import RNS.vendor.umsgpack as umsgpack
                unpacked = umsgpack.unpackb(app_data)
                if isinstance(unpacked, dict):
                    app_data_str = str(
                        unpacked.get("name")
                        or unpacked.get("node_name")
                        or unpacked.get("display_name")
                        or ""
                    )
                elif isinstance(unpacked, str):
                    app_data_str = unpacked
            except Exception:
                pass
            # Fall back to plain UTF-8 if msgpack didn't produce a name
            if not app_data_str:
                try:
                    decoded = app_data.decode("utf-8")
                    if decoded.isprintable():
                        app_data_str = decoded
                except (UnicodeDecodeError, ValueError):
                    pass

        # Parse aspect into app_name + aspects
        parts = aspect.split(".") if aspect else []
        app_name = parts[0] if parts else ""
        aspect_parts = ".".join(parts[1:]) if len(parts) > 1 else ""

        with self._nodes_lock:
            existing = self._known_nodes.get(destination_hash)
            if existing:
                existing["last_seen"] = now
                existing["hops"] = hops
                existing["announce_count"] = existing.get("announce_count", 0) + 1
                existing["app_data_str"] = app_data_str
                existing["app_name"] = app_name
                existing["aspects"] = aspect_parts
                is_new = False
            else:
                self._known_nodes[destination_hash] = {
                    "app_name": app_name,
                    "aspects": aspect_parts,
                    "hops": hops,
                    "last_seen": now,
                    "first_seen": now,
                    "announce_count": 1,
                    "app_data_str": app_data_str,
                }
                is_new = True
            node_info = dict(self._known_nodes[destination_hash])

        # Persist to DB (outside lock)
        self._upsert_node(destination_hash, node_info)

        if is_new:
            self.log.info(
                "New node discovered: %s (%s.%s) %s hops",
                RNS.prettyhexrep(destination_hash),
                app_name,
                aspect_parts,
                hops if hops is not None else "?",
            )
            self.event_bus.publish(events.NODE_DISCOVERED, {
                "destination_hash": destination_hash,
                "app_name": app_name,
                "aspects": aspect_parts,
                "hops": hops,
            })

    # --- SQLite ---

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS known_nodes (
                    destination_hash TEXT PRIMARY KEY,
                    app_name TEXT,
                    aspects TEXT,
                    hops INTEGER,
                    last_seen REAL,
                    first_seen REAL,
                    announce_count INTEGER,
                    app_data_str TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS interface_stats (
                    timestamp REAL,
                    name TEXT,
                    type TEXT,
                    online INTEGER,
                    rxb INTEGER,
                    txb INTEGER,
                    bitrate INTEGER,
                    peers INTEGER
                )
            """)

    def _load_from_db(self) -> None:
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                for row in conn.execute("SELECT * FROM known_nodes"):
                    dest_hash = bytes.fromhex(row["destination_hash"])
                    self._known_nodes[dest_hash] = {
                        "app_name": row["app_name"],
                        "aspects": row["aspects"],
                        "hops": row["hops"],
                        "last_seen": row["last_seen"],
                        "first_seen": row["first_seen"],
                        "announce_count": row["announce_count"],
                        "app_data_str": row["app_data_str"] or "",
                    }
            self.log.info("Loaded %d known nodes from database", len(self._known_nodes))
        except Exception:
            self.log.exception("Error loading known nodes from database")

    def _upsert_node(self, dest_hash: bytes, info: dict[str, Any]) -> None:
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO known_nodes
                    (destination_hash, app_name, aspects, hops, last_seen, first_seen, announce_count, app_data_str)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    dest_hash.hex(),
                    info.get("app_name", ""),
                    info.get("aspects", ""),
                    info.get("hops"),
                    info.get("last_seen"),
                    info.get("first_seen"),
                    info.get("announce_count", 1),
                    info.get("app_data_str", ""),
                ))
        except Exception:
            self.log.debug("Error upserting node to database", exc_info=True)

    def _save_interface_stats(self) -> None:
        stats = self.get_interface_stats()
        if not stats:
            return
        now = time.time()
        try:
            with sqlite3.connect(self._db_path) as conn:
                for s in stats:
                    conn.execute("""
                        INSERT INTO interface_stats (timestamp, name, type, online, rxb, txb, bitrate, peers)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        now,
                        s.get("name", ""),
                        s.get("type", ""),
                        1 if s.get("online", True) else 0,
                        s.get("rxb"),
                        s.get("txb"),
                        s.get("bitrate"),
                        s.get("peers"),
                    ))
        except Exception:
            self.log.debug("Error saving interface stats", exc_info=True)

    def _prune_old_data(self) -> None:
        max_days = self.config.get("max_history_days", 30)
        cutoff = time.time() - (max_days * 86400)
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("DELETE FROM interface_stats WHERE timestamp < ?", (cutoff,))
                conn.execute("DELETE FROM known_nodes WHERE last_seen < ?", (cutoff,))
            # Also prune from memory
            with self._nodes_lock:
                expired = [h for h, info in self._known_nodes.items() if info["last_seen"] < cutoff]
                for h in expired:
                    del self._known_nodes[h]
        except Exception:
            self.log.debug("Error pruning old data", exc_info=True)

    def _maintenance_loop(self) -> None:
        """Periodically collect interface stats and prune old data."""
        cycles_since_prune = 0
        while self._active:
            try:
                self._save_interface_stats()
            except Exception:
                self.log.debug("Error in maintenance loop", exc_info=True)

            self._sleep_while_active(60)
            if not self._active:
                break

            # Prune every 60 cycles (once per hour)
            cycles_since_prune += 1
            if cycles_since_prune >= 60:
                self._prune_old_data()
                cycles_since_prune = 0


class _AnnounceHandler:
    """Registered with RNS.Transport to receive all announces."""

    def __init__(self, plugin: NetworkMapPlugin):
        self._plugin = plugin
        # No aspect_filter means we receive all announces
        self.aspect_filter = None

    def received_announce(
        self,
        destination_hash: bytes,
        announced_identity: Any,
        app_data: bytes | None,
    ) -> None:
        try:
            # With aspect_filter=None, RNS does not provide the destination
            # aspect directly to the handler. We record what we can; the aspect
            # may be empty for wildcard handlers.
            aspect = ""
            try:
                # Look up any locally-known destination matching this hash
                if hasattr(RNS.Transport, "destination_table"):
                    entry = RNS.Transport.destination_table.get(destination_hash)
                    if entry and len(entry) > 4:
                        aspect = entry[4] if isinstance(entry[4], str) else ""
            except Exception:
                pass

            self._plugin.record_announce(
                destination_hash, announced_identity, app_data, aspect
            )
        except Exception:
            self._plugin.log.debug("Error handling announce", exc_info=True)
