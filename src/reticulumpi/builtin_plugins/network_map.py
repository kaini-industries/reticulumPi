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


# Aspects registered by default.  Each gets its own handler so RNS
# provides the aspect string with the announce, populating app_name.
_DEFAULT_ASPECTS = [
    "lxmf.delivery",
    "lxmf.propagation",
    "nomadnetwork.node",
    "reticulumpi.node.heartbeat",
    "reticulumpi.node.telemetry",
    "reticulumpi.emergency.broadcast",
    "reticulumpi.hubexchange",
]


def _infer_app_from_data(app_data_str: str) -> tuple[str, str]:
    """Infer app_name and aspects from app_data content when the announce
    aspect is unknown (wildcard handler).  Returns (app_name, aspects)."""
    if app_data_str.startswith("styrene:"):
        return "styrene", "node"
    if app_data_str.startswith("{") and '"h"' in app_data_str:
        return "sideband", "presence"
    if app_data_str.lower().strip() == "presence":
        return "sideband", "presence"
    if app_data_str.startswith("Anonymous Peer"):
        return "sideband", "client"
    return "", ""


class NetworkMapPlugin(PluginBase):
    """Passively monitors all Reticulum announces and builds a live map of
    known nodes, hop counts, and interface statistics.  Stores history in
    SQLite for trend analysis.
    """

    plugin_name = "network_map"
    plugin_version = "1.1.0"
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

        # One-time: reclassify unclassified nodes using app_data heuristics
        self._reclassify_unclassified()

        # Register per-aspect handlers so RNS provides the aspect string,
        # plus a wildcard handler to catch custom/unknown aspects.
        extra = self.config.get("extra_aspects", [])
        aspects = list(dict.fromkeys(_DEFAULT_ASPECTS + list(extra)))

        self._handlers: list = []
        for aspect in aspects:
            handler = _AspectHandler(self, aspect)
            RNS.Transport.register_announce_handler(handler)
            self._handlers.append(handler)

        self._wildcard_handler = _WildcardHandler(self)
        RNS.Transport.register_announce_handler(self._wildcard_handler)
        self._handlers.append(self._wildcard_handler)

        # Background thread for periodic interface stats and DB pruning
        self._start_thread(self._maintenance_loop, "network-map")

        self.log.info(
            "Network map active — monitoring announces for %d aspects + "
            "wildcard (DB: %s)",
            len(aspects),
            db_path,
        )

    def stop(self) -> None:
        self._active = False
        for handler in getattr(self, "_handlers", []):
            try:
                RNS.Transport.deregister_announce_handler(handler)
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

    def get_known_nodes_paginated(
        self,
        page: int = 1,
        per_page: int = 25,
        sort: str = "last_seen",
        order: str = "desc",
        search: str = "",
        app_filter: str = "",
        view: str = "",
    ) -> dict[str, Any]:
        """Return paginated nodes directly from SQLite for efficiency.

        Returns dict with 'nodes', 'total', 'page', 'pages', 'per_page'.
        """
        # Clamp pagination inputs to prevent DoS via absurd values
        per_page = max(1, min(per_page, 500))
        page = max(1, page)

        allowed_sorts = {
            "last_seen": "last_seen",
            "first_seen": "first_seen",
            "hops": "hops",
            "announce_count": "announce_count",
            "app_name": "app_name",
            "score": "score",
        }
        sort_col = allowed_sorts.get(sort, "last_seen")
        sort_dir = "ASC" if order == "asc" else "DESC"

        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row

                # Build WHERE clause
                conditions = []
                params: list = []

                # View presets add WHERE conditions + default sort
                view_default_sort = ""
                if view == "hubs":
                    conditions.append("announce_count >= 50")
                    view_default_sort = "announce_count DESC"
                elif view == "nearby":
                    conditions.append("hops IS NOT NULL AND hops <= 4")
                    view_default_sort = "hops ASC, last_seen DESC"
                elif view == "recent":
                    conditions.append(
                        f"last_seen > (strftime('%s','now') - 3600)"
                    )
                    view_default_sort = "last_seen DESC"
                elif view == "lxmf":
                    conditions.append("app_name = 'lxmf'")
                    view_default_sort = "last_seen DESC"
                elif view == "nomadnet":
                    conditions.append("app_name = 'nomadnetwork'")
                    view_default_sort = "last_seen DESC"

                if search:
                    conditions.append(
                        "(destination_hash LIKE ? OR app_data_str LIKE ? OR app_name LIKE ?)"
                    )
                    like = f"%{search}%"
                    params.extend([like, like, like])
                if app_filter:
                    conditions.append("app_name = ?")
                    params.append(app_filter)

                where = ""
                if conditions:
                    where = "WHERE " + " AND ".join(conditions)

                # Count total
                count_sql = f"SELECT COUNT(*) FROM known_nodes {where}"
                total = conn.execute(count_sql, params).fetchone()[0]

                # Paginate
                pages = max(1, (total + per_page - 1) // per_page) if per_page > 0 else 1
                page = max(1, min(page, pages))
                offset = (page - 1) * per_page

                # Build ORDER BY clause
                if sort_col == "score":
                    # Proxy reachability score: recency + proximity + activity
                    order_clause = (
                        "("
                        "CASE WHEN last_seen IS NULL THEN 0 "
                        "WHEN (strftime('%s','now') - last_seen) < 600 THEN 15 "
                        "WHEN (strftime('%s','now') - last_seen) < 3600 THEN 12 "
                        "WHEN (strftime('%s','now') - last_seen) < 14400 THEN 8 "
                        "WHEN (strftime('%s','now') - last_seen) < 86400 THEN 4 "
                        "ELSE 1 END "
                        "+ CASE WHEN hops IS NULL THEN 0 "
                        "WHEN hops <= 1 THEN 15 "
                        "WHEN hops <= 3 THEN 12 "
                        "WHEN hops <= 6 THEN 8 "
                        "WHEN hops <= 10 THEN 4 "
                        "ELSE 1 END "
                        "+ CASE WHEN announce_count > 1000 THEN 10 "
                        "WHEN announce_count > 100 THEN 7 "
                        "WHEN announce_count > 10 THEN 4 "
                        "ELSE 1 END"
                        f") {sort_dir}"
                    )
                elif sort_col == "hops":
                    # Push NULLs to end
                    order_clause = f"CASE WHEN hops IS NULL THEN 1 ELSE 0 END, hops {sort_dir}"
                elif view_default_sort and sort == "last_seen" and order == "desc":
                    # Use view's default sort when user hasn't explicitly changed sort
                    order_clause = view_default_sort
                else:
                    order_clause = f"{sort_col} {sort_dir}"

                query = f"""
                    SELECT * FROM known_nodes {where}
                    ORDER BY {order_clause}
                    LIMIT ? OFFSET ?
                """
                rows = conn.execute(query, params + [per_page, offset]).fetchall()

                nodes = []
                for row in rows:
                    # Format hash with angle brackets to match get_known_nodes()
                    raw_hash = row["destination_hash"] or ""
                    fmt_hash = "<" + raw_hash + ">" if raw_hash else ""
                    nodes.append({
                        "destination_hash": fmt_hash,
                        "app_name": row["app_name"] or "",
                        "aspects": row["aspects"] or "",
                        "hops": row["hops"],
                        "last_seen": row["last_seen"],
                        "first_seen": row["first_seen"],
                        "announce_count": row["announce_count"] or 0,
                        "app_data": row["app_data_str"] or "",
                    })

                return {
                    "nodes": nodes,
                    "total": total,
                    "page": page,
                    "pages": pages,
                    "per_page": per_page,
                }
        except Exception:
            self.log.exception("Error querying paginated nodes")
            return {"nodes": [], "total": 0, "page": 1, "pages": 1, "per_page": per_page}

    def get_node_count(self) -> int:
        """Return the total number of known nodes (fast, for WS summary)."""
        with self._nodes_lock:
            return len(self._known_nodes)

    def get_mesh_summary(self) -> dict[str, Any]:
        """Return aggregate mesh stats for the dashboard summary strip."""
        import time as _time
        now = _time.time()
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row

                # Total nodes
                total = conn.execute("SELECT COUNT(*) FROM known_nodes").fetchone()[0]

                # App breakdown
                app_rows = conn.execute(
                    "SELECT COALESCE(app_name, '') AS app, COUNT(*) AS cnt "
                    "FROM known_nodes GROUP BY app ORDER BY cnt DESC"
                ).fetchall()
                app_breakdown = {r["app"]: r["cnt"] for r in app_rows}

                # Hop distribution (bucketed)
                hop_rows = conn.execute(
                    "SELECT "
                    "SUM(CASE WHEN hops = 0 THEN 1 ELSE 0 END) AS h0, "
                    "SUM(CASE WHEN hops BETWEEN 1 AND 3 THEN 1 ELSE 0 END) AS h1_3, "
                    "SUM(CASE WHEN hops BETWEEN 4 AND 10 THEN 1 ELSE 0 END) AS h4_10, "
                    "SUM(CASE WHEN hops BETWEEN 11 AND 50 THEN 1 ELSE 0 END) AS h11_50, "
                    "SUM(CASE WHEN hops > 50 THEN 1 ELSE 0 END) AS h51, "
                    "SUM(CASE WHEN hops IS NULL THEN 1 ELSE 0 END) AS h_null "
                    "FROM known_nodes"
                ).fetchone()
                hop_distribution = {
                    "0": hop_rows["h0"] or 0,
                    "1-3": hop_rows["h1_3"] or 0,
                    "4-10": hop_rows["h4_10"] or 0,
                    "11-50": hop_rows["h11_50"] or 0,
                    "51+": hop_rows["h51"] or 0,
                    "unknown": hop_rows["h_null"] or 0,
                }

                # Activity stats
                act_row = conn.execute(
                    "SELECT "
                    "SUM(CASE WHEN last_seen > ? THEN 1 ELSE 0 END) AS last_1h, "
                    "SUM(CASE WHEN last_seen > ? THEN 1 ELSE 0 END) AS last_24h, "
                    "SUM(CASE WHEN last_seen > ? THEN 1 ELSE 0 END) AS last_7d "
                    "FROM known_nodes",
                    (now - 3600, now - 86400, now - 604800),
                ).fetchone()
                activity_stats = {
                    "last_1h": act_row["last_1h"] or 0,
                    "last_24h": act_row["last_24h"] or 0,
                    "last_7d": act_row["last_7d"] or 0,
                }

                # Growth (new nodes discovered)
                growth_row = conn.execute(
                    "SELECT "
                    "SUM(CASE WHEN first_seen > ? THEN 1 ELSE 0 END) AS new_24h, "
                    "SUM(CASE WHEN first_seen > ? THEN 1 ELSE 0 END) AS new_7d "
                    "FROM known_nodes",
                    (now - 86400, now - 604800),
                ).fetchone()
                growth = {
                    "last_24h": growth_row["new_24h"] or 0,
                    "last_7d": growth_row["new_7d"] or 0,
                }

                # Nearby count (hops <= 4)
                nearby = conn.execute(
                    "SELECT COUNT(*) FROM known_nodes "
                    "WHERE hops IS NOT NULL AND hops <= 4"
                ).fetchone()[0]

                return {
                    "total_nodes": total,
                    "app_breakdown": app_breakdown,
                    "hop_distribution": hop_distribution,
                    "activity_stats": activity_stats,
                    "growth": growth,
                    "nearby": nearby,
                }
        except Exception:
            self.log.exception("Error computing mesh summary")
            return {"total_nodes": 0, "app_breakdown": {},
                    "hop_distribution": {}, "activity_stats": {},
                    "growth": {}, "nearby": 0}

    def get_recent_announces(self, since: float = 0, limit: int = 10) -> list[dict[str, Any]]:
        """Return nodes announced since a given timestamp (for WS deltas)."""
        results = []
        with self._nodes_lock:
            items = list(self._known_nodes.items())
        for dest_hash, info in items:
            if info.get("last_seen", 0) > since:
                results.append({
                    "destination_hash": RNS.prettyhexrep(dest_hash),
                    "app_name": info.get("app_name", ""),
                    "aspects": info.get("aspects", ""),
                    "hops": info.get("hops"),
                    "last_seen": info.get("last_seen"),
                    "announce_count": info.get("announce_count", 0),
                    "app_data": info.get("app_data_str", ""),
                })
        results.sort(key=lambda n: n.get("last_seen", 0), reverse=True)
        return results[:limit]

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
        *,
        from_wildcard: bool = False,
    ) -> None:
        """Called by the announce handler when an announce is received.

        Args:
            from_wildcard: True when called by the wildcard handler. Used
                to skip processing for nodes already classified by a
                specific-aspect handler (avoids double-counting).
        """
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

        # Heuristic: infer app_name from app_data when wildcard handler
        # couldn't determine the aspect (reduces "Other" in dashboard)
        if not app_name and app_data_str and from_wildcard:
            app_name, aspect_parts = _infer_app_from_data(app_data_str)

        with self._nodes_lock:
            existing = self._known_nodes.get(destination_hash)
            if existing:
                # The wildcard handler fires alongside specific-aspect
                # handlers for the same announce.  Once a node has been
                # classified by a specific handler (app_name is set),
                # let only the specific handler process future updates
                # to avoid double-counting.
                if from_wildcard and existing.get("app_name"):
                    return

                existing["last_seen"] = now
                existing["hops"] = hops

                # Only overwrite app info when we have actual data —
                # the wildcard handler passes empty strings, so it must
                # not clobber data set by a specific-aspect handler.
                if app_name:
                    existing["app_name"] = app_name
                    existing["aspects"] = aspect_parts
                if app_data_str:
                    existing["app_data_str"] = app_data_str

                existing["announce_count"] = (
                    existing.get("announce_count", 0) + 1
                )
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

    def _reclassify_unclassified(self) -> None:
        """One-time pass: apply app_data heuristics to nodes with empty app_name."""
        reclassified = 0
        try:
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT destination_hash, app_data_str FROM known_nodes "
                    "WHERE (app_name = '' OR app_name IS NULL) "
                    "AND app_data_str IS NOT NULL AND app_data_str != ''"
                ).fetchall()
                for dest_hex, app_data_str in rows:
                    inferred_app, inferred_aspect = _infer_app_from_data(app_data_str)
                    if inferred_app:
                        conn.execute(
                            "UPDATE known_nodes SET app_name = ?, aspects = ? "
                            "WHERE destination_hash = ?",
                            (inferred_app, inferred_aspect, dest_hex),
                        )
                        # Also update in-memory cache
                        try:
                            dest_hash = bytes.fromhex(dest_hex)
                            if dest_hash in self._known_nodes:
                                self._known_nodes[dest_hash]["app_name"] = inferred_app
                                self._known_nodes[dest_hash]["aspects"] = inferred_aspect
                        except (ValueError, KeyError):
                            pass
                        reclassified += 1
            if reclassified:
                self.log.info("Reclassified %d nodes from app_data heuristics", reclassified)
        except Exception:
            self.log.exception("Error reclassifying nodes")

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


class _AspectHandler:
    """Receives announces for a specific aspect (e.g. ``lxmf.delivery``).

    Because ``aspect_filter`` is set, RNS guarantees that only announces
    matching this aspect reach the handler, and we can pass the known
    aspect string into ``record_announce``.
    """

    def __init__(self, plugin: NetworkMapPlugin, aspect: str):
        self._plugin = plugin
        self.aspect_filter = aspect
        self._aspect = aspect

    def received_announce(
        self,
        destination_hash: bytes,
        announced_identity: Any,
        app_data: bytes | None,
    ) -> None:
        try:
            self._plugin.record_announce(
                destination_hash, announced_identity, app_data, self._aspect
            )
        except Exception:
            self._plugin.log.debug("Error handling announce", exc_info=True)


class _WildcardHandler:
    """Catches announces for any aspect, including unknown/custom ones.

    The wildcard handler cannot determine the aspect from RNS, so it
    passes an empty string.  ``record_announce`` will not overwrite
    app_name data already set by a specific-aspect handler.
    """

    def __init__(self, plugin: NetworkMapPlugin):
        self._plugin = plugin
        self.aspect_filter = None

    def received_announce(
        self,
        destination_hash: bytes,
        announced_identity: Any,
        app_data: bytes | None,
    ) -> None:
        try:
            self._plugin.record_announce(
                destination_hash, announced_identity, app_data, "",
                from_wildcard=True,
            )
        except Exception:
            self._plugin.log.debug("Error handling announce", exc_info=True)
