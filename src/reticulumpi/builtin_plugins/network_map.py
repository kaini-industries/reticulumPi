"""Network Map plugin — passively monitors announces to build a mesh topology view."""

from __future__ import annotations

import os
import queue
import sqlite3
import threading
import time
from contextlib import closing
from typing import Any

import RNS

from reticulumpi import events
from reticulumpi.migration_catalog import migration_targets
from reticulumpi.migrations import MigrationTarget
from reticulumpi.plugin_base import PluginBase
from reticulumpi.runtime_metrics import instrument_sqlite_class


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


@instrument_sqlite_class
class NetworkMapPlugin(PluginBase):
    """Passively monitors all Reticulum announces and builds a live map of
    known nodes, hop counts, and interface statistics.  Stores history in
    SQLite for trend analysis.
    """

    plugin_name = "network_map"
    plugin_version = "1.1.0"
    plugin_description = "Passive network topology mapping via announce monitoring"
    broadcast_tier = 2
    broadcast_keys = "mesh"

    def get_migration_targets(self) -> tuple[MigrationTarget, ...]:
        return migration_targets(self.plugin_name, self.config)

    def validate_config(self) -> None:
        max_days = self.config.get("max_history_days", 30)
        if not isinstance(max_days, (int, float)) or max_days < 1:
            raise ValueError("max_history_days must be >= 1")
        max_cached = self.config.get("max_cached_nodes", 10000)
        if not isinstance(max_cached, int) or max_cached < 100:
            raise ValueError("max_cached_nodes must be an integer >= 100")
        max_stats = self.config.get("max_stats_rows", 50000)
        if not isinstance(max_stats, int) or max_stats < 1000:
            raise ValueError("max_stats_rows must be an integer >= 1000")

    def start(self) -> None:
        try:
            self._start()
        except BaseException:
            # start() may fail after one or more persistent SQLite handles or
            # announce workers have been created.  Tear down every resource
            # before propagating the original startup error.
            try:
                self.stop()
            except Exception:
                self.log.exception("Error cleaning up partial network-map startup")
            raise

    def _start(self) -> None:
        self._active = True
        self._known_nodes: dict[bytes, dict[str, Any]] = {}
        self._max_cached_nodes: int = self.config.get("max_cached_nodes", 10000)
        self._max_stats_rows: int = self.config.get("max_stats_rows", 50000)
        self._stats_save_count: int = 0
        self._nodes_lock = threading.Lock()

        db_path = os.path.expanduser(
            self.config.get("db_path", "~/.local/share/reticulumpi/network_map.db")
        )
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._db_path = db_path
        self._init_db()

        # Persistent write connection — all writes go through this.
        self._write_conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._write_conn.execute("PRAGMA journal_mode=WAL")
        self._write_conn.execute("PRAGMA synchronous=NORMAL")
        self._write_conn_lock = threading.Lock()

        # Load previously known nodes from DB
        self._load_from_db()

        # One-time: reclassify unclassified nodes using app_data heuristics
        self._reclassify_unclassified()

        # Announce processing queue — the shared AnnounceDispatcher delivers
        # callbacks on a single worker thread, so we still queue+batch to
        # keep the dispatcher responsive.
        self._announce_queue: queue.Queue = queue.Queue(maxsize=10000)
        self._pending_upserts: dict[bytes, dict[str, Any]] = {}
        self._start_thread(self._announce_worker, "network-map-announces")

        # Subscribe to announces via the shared dispatcher instead of
        # registering per-aspect RNS handlers (which each spawn a thread).
        extra = self.config.get("extra_aspects", [])
        aspects = list(dict.fromkeys(_DEFAULT_ASPECTS + list(extra)))

        self._sub_ids: list[str] = []
        for aspect in aspects:

            def _on_aspect(dest, identity, app_data, _a=aspect):
                self.record_announce(dest, identity, app_data, _a)

            self._sub_ids.append(self.announce_dispatcher.subscribe(aspect, _on_aspect))

        def _on_wildcard(dest, identity, app_data):
            self.record_announce(dest, identity, app_data, "", from_wildcard=True)

        self._sub_ids.append(self.announce_dispatcher.subscribe(None, _on_wildcard))

        self._broadcast_cache: tuple[float, int, dict] | None = None
        self._broadcast_cache_ttl = 15.0
        self._summary_cache: tuple[float, dict] | None = None
        self._summary_cache_ttl = 120.0
        # Guards _summary_cache so the maintenance thread (writer) and the
        # broadcast/REST threads (readers) never see a torn tuple.
        self._summary_cache_lock = threading.Lock()

        self._announces_dirty = threading.Event()
        self._announces_dirty.set()
        self._recent_announces_cache: list[dict[str, Any]] | None = None
        self._recent_announces_time: float = 0.0
        self._recent_announces_ttl = 5.0

        self._broadcast_read_conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._broadcast_read_conn.row_factory = sqlite3.Row
        self._broadcast_read_conn.execute("PRAGMA journal_mode=WAL")
        self._broadcast_read_conn.execute("PRAGMA query_only=ON")

        # Persistent read connection for paginated / general queries
        self._query_read_conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._query_read_conn.row_factory = sqlite3.Row
        self._query_read_conn.execute("PRAGMA journal_mode=WAL")
        self._query_read_conn.execute("PRAGMA query_only=ON")
        self._query_read_conn_lock = threading.Lock()

        self._maintenance_read_conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._maintenance_read_conn.row_factory = sqlite3.Row
        self._maintenance_read_conn.execute("PRAGMA journal_mode=WAL")
        self._maintenance_read_conn.execute("PRAGMA query_only=ON")
        self._maintenance_conn_lock = threading.Lock()

        # Background thread for periodic interface stats and DB pruning
        self._start_thread(self._maintenance_loop, "network-map")

        self.log.info(
            "Network map active — monitoring announces for %d aspects + wildcard (DB: %s)",
            len(aspects),
            db_path,
        )

    def stop(self) -> None:
        self._active = False
        try:
            for sub_id in getattr(self, "_sub_ids", []):
                try:
                    self.announce_dispatcher.unsubscribe(sub_id)
                except Exception:
                    self.log.debug(
                        "Error unsubscribing network-map announce handler",
                        exc_info=True,
                    )
            self._join_threads()
        finally:
            for conn_attr in (
                "_broadcast_read_conn",
                "_maintenance_read_conn",
                "_query_read_conn",
                "_write_conn",
            ):
                conn = getattr(self, conn_attr, None)
                if conn is not None:
                    try:
                        conn.close()
                    except (OSError, sqlite3.Error):
                        pass
                    finally:
                        setattr(self, conn_attr, None)

    def get_status(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "known_nodes": len(self._known_nodes),
            "db_path": getattr(self, "_db_path", None),
        }

    def broadcast_snapshot(self, cycle_count: int = 0) -> dict | None:
        now = time.monotonic()
        want_summary = cycle_count % 3 == 0
        cached = self._broadcast_cache
        if cached is not None:
            age = now - cached[0]
            had_summary = cached[1]
            if age < self._broadcast_cache_ttl and (not want_summary or had_summary):
                return cached[2]

        mesh = {}
        if hasattr(self, "get_node_count"):
            mesh["node_count"] = self.get_node_count()
        if hasattr(self, "get_recent_announces"):
            ra_age = now - self._recent_announces_time
            if ra_age >= self._recent_announces_ttl or self._recent_announces_cache is None:
                self._recent_announces_cache = self.get_recent_announces()
                self._recent_announces_time = now
            mesh["recent_announces"] = self._recent_announces_cache
        has_summary = False
        if want_summary:
            # Read-only: the maintenance loop owns the heavy scan and keeps
            # _summary_cache warm.  Never compute inline on the broadcast
            # thread — if the cache is missing, serve the last known value or
            # omit the key gracefully rather than block the broadcast.
            with self._summary_cache_lock:
                sc = self._summary_cache
            if sc is not None:
                mesh["summary"] = sc[1]
                has_summary = True
        result = mesh or None
        self._broadcast_cache = (now, has_summary, result)
        return result

    def get_known_nodes(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return known nodes as a list of dicts (for API consumption).

        Args:
            limit: When provided, return only this many nodes sorted by
                last_seen descending.  Omit to get all nodes.
        """
        nodes = []
        with self._nodes_lock:
            items = list(self._known_nodes.items())
        for dest_hash, info in items:
            nodes.append(
                {
                    "destination_hash": RNS.prettyhexrep(dest_hash),
                    "app_name": info.get("app_name", ""),
                    "aspects": info.get("aspects", ""),
                    "hops": info.get("hops"),
                    "last_seen": info.get("last_seen"),
                    "first_seen": info.get("first_seen"),
                    "announce_count": info.get("announce_count", 0),
                    "app_data": info.get("app_data_str", ""),
                }
            )
        result = sorted(nodes, key=lambda n: n.get("last_seen", 0), reverse=True)
        if limit is not None:
            return result[:limit]
        return result

    def get_node_by_hash(self, h: bytes) -> dict | None:
        """Return a single node dict by its raw destination hash.

        Returns None if the hash is not in the known-nodes cache.
        """
        with self._nodes_lock:
            info = self._known_nodes.get(h)
            if info is None:
                return None
            return dict(info)

    def get_nodes_by_hashes(self, hashes: list[bytes]) -> list[dict]:
        """Return node dicts for a list of raw destination hashes.

        Each returned dict includes ``destination_hash`` formatted with
        angle brackets (matching ``get_known_nodes()`` output).
        Unknown hashes are silently skipped.
        """
        results: list[dict] = []
        with self._nodes_lock:
            for h in hashes:
                info = self._known_nodes.get(h)
                if info is not None:
                    node = {
                        "destination_hash": RNS.prettyhexrep(h),
                        "app_name": info.get("app_name", ""),
                        "aspects": info.get("aspects", ""),
                        "hops": info.get("hops"),
                        "last_seen": info.get("last_seen"),
                        "first_seen": info.get("first_seen"),
                        "announce_count": info.get("announce_count", 0),
                        "app_data": info.get("app_data_str", ""),
                    }
                    results.append(node)
        return results

    def get_node_name(self, destination_hash: str) -> str | None:
        """Return the announced display name for a node, or None if unknown."""
        try:
            dh = bytes.fromhex(destination_hash.strip().strip("<>"))
        except (ValueError, AttributeError):
            return None
        with self._nodes_lock:
            info = self._known_nodes.get(dh)
        if not info:
            return None
        name = info.get("app_data_str")
        return name if name else None

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
        if order not in ("asc", "desc"):
            order = "desc"
        sort_dir = order.upper()

        try:
            with self._query_read_conn_lock:
                conn = self._query_read_conn

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
                    conditions.append("last_seen > (strftime('%s','now') - 3600)")
                    view_default_sort = "last_seen DESC"
                elif view == "lxmf":
                    conditions.append("app_name = 'lxmf'")
                    view_default_sort = "last_seen DESC"
                elif view == "nomadnet":
                    conditions.append("app_name = 'nomadnetwork'")
                    view_default_sort = "last_seen DESC"

                if search:
                    conditions.append(
                        "(destination_hash LIKE ? ESCAPE '\\'"
                        " OR app_data_str LIKE ? ESCAPE '\\'"
                        " OR app_name LIKE ? ESCAPE '\\')"
                    )
                    escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    like = f"%{escaped}%"
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
                    nodes.append(
                        {
                            "destination_hash": fmt_hash,
                            "app_name": row["app_name"] or "",
                            "aspects": row["aspects"] or "",
                            "hops": row["hops"],
                            "last_seen": row["last_seen"],
                            "first_seen": row["first_seen"],
                            "announce_count": row["announce_count"] or 0,
                            "app_data": row["app_data_str"] or "",
                        }
                    )

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
        """Return aggregate mesh stats for the dashboard summary strip.

        Cache-aware entry point for REST callers: serve the cache when it is
        still fresh, otherwise compute it directly (rare path — the broadcast
        thread never reaches this; the maintenance loop keeps the cache warm).
        """
        now = time.monotonic()
        with self._summary_cache_lock:
            sc = self._summary_cache
        if sc and (now - sc[0]) < self._summary_cache_ttl:
            return sc[1]
        summary = self._compute_mesh_summary()
        with self._summary_cache_lock:
            self._summary_cache = (now, summary)
        return summary

    def _compute_mesh_summary(self) -> dict[str, Any]:
        """Run the two full known_nodes scans that back the summary strip.

        Consolidates into 2 queries (one aggregation + one GROUP BY)
        instead of 6 separate full-table scans.  This is the heavy path —
        call it from the maintenance loop, never on the broadcast thread.
        """
        now = time.time()
        try:
            with self._maintenance_conn_lock:
                conn = self._maintenance_read_conn

                # Single-pass aggregation for totals, hops, activity, growth, nearby
                agg = conn.execute(
                    "SELECT "
                    "COUNT(*) AS total, "
                    "SUM(CASE WHEN hops = 0 THEN 1 ELSE 0 END) AS h0, "
                    "SUM(CASE WHEN hops BETWEEN 1 AND 3 THEN 1 ELSE 0 END) AS h1_3, "
                    "SUM(CASE WHEN hops BETWEEN 4 AND 10 THEN 1 ELSE 0 END) AS h4_10, "
                    "SUM(CASE WHEN hops BETWEEN 11 AND 50 THEN 1 ELSE 0 END) AS h11_50, "
                    "SUM(CASE WHEN hops > 50 THEN 1 ELSE 0 END) AS h51, "
                    "SUM(CASE WHEN hops IS NULL THEN 1 ELSE 0 END) AS h_null, "
                    "SUM(CASE WHEN last_seen > :t_1h THEN 1 ELSE 0 END) AS last_1h, "
                    "SUM(CASE WHEN last_seen > :t_24h THEN 1 ELSE 0 END) AS last_24h, "
                    "SUM(CASE WHEN last_seen > :t_7d THEN 1 ELSE 0 END) AS last_7d, "
                    "SUM(CASE WHEN first_seen > :t_24h THEN 1 ELSE 0 END) AS new_24h, "
                    "SUM(CASE WHEN first_seen > :t_7d THEN 1 ELSE 0 END) AS new_7d, "
                    "SUM(CASE WHEN hops IS NOT NULL AND hops <= 4 THEN 1 ELSE 0 END) AS nearby "
                    "FROM known_nodes",
                    {
                        "t_1h": now - 3600,
                        "t_24h": now - 86400,
                        "t_7d": now - 604800,
                    },
                ).fetchone()

                app_rows = conn.execute(
                    "SELECT COALESCE(app_name, '') AS app, COUNT(*) AS cnt "
                    "FROM known_nodes GROUP BY app ORDER BY cnt DESC"
                ).fetchall()
                app_breakdown = {r["app"]: r["cnt"] for r in app_rows}

                return {
                    "total_nodes": agg["total"] or 0,
                    "app_breakdown": app_breakdown,
                    "hop_distribution": {
                        "0": agg["h0"] or 0,
                        "1-3": agg["h1_3"] or 0,
                        "4-10": agg["h4_10"] or 0,
                        "11-50": agg["h11_50"] or 0,
                        "51+": agg["h51"] or 0,
                        "unknown": agg["h_null"] or 0,
                    },
                    "activity_stats": {
                        "last_1h": agg["last_1h"] or 0,
                        "last_24h": agg["last_24h"] or 0,
                        "last_7d": agg["last_7d"] or 0,
                    },
                    "growth": {
                        "last_24h": agg["new_24h"] or 0,
                        "last_7d": agg["new_7d"] or 0,
                    },
                    "nearby": agg["nearby"] or 0,
                }
        except Exception:
            self.log.exception("Error computing mesh summary")
            return {
                "total_nodes": 0,
                "app_breakdown": {},
                "hop_distribution": {},
                "activity_stats": {},
                "growth": {},
                "nearby": 0,
            }

    def get_recent_announces(self, since: float = 0, limit: int = 10) -> list[dict[str, Any]]:
        """Return nodes announced since a given timestamp (for WS deltas).

        Uses the idx_known_nodes_last_seen index for an O(limit) seek
        instead of scanning the full in-memory dict.
        """
        try:
            conn = self._broadcast_read_conn
            rows = conn.execute(
                "SELECT * FROM known_nodes WHERE last_seen > ? ORDER BY last_seen DESC LIMIT ?",
                (since, limit),
            ).fetchall()
            return [
                {
                    "destination_hash": "<" + (row["destination_hash"] or "") + ">",
                    "app_name": row["app_name"] or "",
                    "aspects": row["aspects"] or "",
                    "hops": row["hops"],
                    "last_seen": row["last_seen"],
                    "announce_count": row["announce_count"] or 0,
                    "app_data": row["app_data_str"] or "",
                }
                for row in rows
            ]
        except Exception:
            self.log.exception("Error querying recent announces")
            return []

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

        This method is invoked in an RNS-spawned daemon thread.  To prevent
        thread exhaustion (RNS creates a new thread per callback), we just
        enqueue the announce data and return immediately.  The single
        ``_announce_worker`` thread does the actual processing.
        """
        try:
            self._announce_queue.put_nowait(
                (destination_hash, identity, app_data, aspect, from_wildcard)
            )
        except queue.Full:
            self.log.warning(
                "Announce queue full — dropped announce from %s", RNS.prettyhexrep(destination_hash)
            )

    def _process_announce(
        self,
        destination_hash: bytes,
        identity: Any,
        app_data: bytes | None,
        aspect: str,
        from_wildcard: bool,
    ) -> None:
        """Process a single announce (called from the worker thread)."""
        now = time.time()
        hops = None
        try:
            hops = RNS.Transport.hops_to(destination_hash)
        except Exception:
            self.log.debug("hops_to lookup failed", exc_info=True)

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
                elif isinstance(unpacked, list) and unpacked:
                    # LXMF 0.5+ format: [display_name_bytes, stamp_cost]
                    dn = unpacked[0]
                    if isinstance(dn, bytes):
                        try:
                            app_data_str = dn.decode("utf-8")
                        except UnicodeDecodeError:
                            pass
                    elif isinstance(dn, str):
                        app_data_str = dn
                elif isinstance(unpacked, str):
                    app_data_str = unpacked
            except (ValueError, TypeError):
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

                existing["announce_count"] = existing.get("announce_count", 0) + 1
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
                if len(self._known_nodes) > self._max_cached_nodes:
                    oldest_hash = min(
                        self._known_nodes,
                        key=lambda h: self._known_nodes[h].get("last_seen", 0),
                    )
                    del self._known_nodes[oldest_hash]
            node_info = dict(self._known_nodes[destination_hash])

        # _nodes_lock is released here — event publishing and DB writes
        # happen outside the lock to avoid deadlocks with event handlers.
        self._pending_upserts[destination_hash] = node_info
        self._announces_dirty.set()

        if is_new:
            self.log.info(
                "New node discovered: %s (%s.%s) %s hops",
                RNS.prettyhexrep(destination_hash),
                app_name,
                aspect_parts,
                hops if hops is not None else "?",
            )
            self.event_bus.publish(
                events.NODE_DISCOVERED,
                {
                    "destination_hash": destination_hash,
                    "app_name": app_name,
                    "aspects": aspect_parts,
                    "hops": hops,
                },
            )

    def _announce_worker(self) -> None:
        """Drain the announce queue and batch-write to SQLite.

        Processes announces one at a time from the queue, then flushes
        all pending DB upserts in a single transaction every 2 seconds
        or when the queue is empty (whichever comes first).
        """
        flush_interval = 2.0
        last_flush = time.monotonic()

        while self._active:
            try:
                item = self._announce_queue.get(timeout=1.0)
                dest_hash, identity, app_data, aspect, from_wildcard = item
                try:
                    self._process_announce(dest_hash, identity, app_data, aspect, from_wildcard)
                except Exception:
                    self.log.debug("Error processing announce", exc_info=True)
                finally:
                    self._announce_queue.task_done()

                # Flush pending upserts periodically
                now = time.monotonic()
                if now - last_flush >= flush_interval:
                    self._flush_pending_upserts()
                    last_flush = now

            except queue.Empty:
                # Queue drained — flush any pending writes
                if self._pending_upserts:
                    self._flush_pending_upserts()
                    last_flush = time.monotonic()

        # Final flush on shutdown
        self._flush_pending_upserts()

    def _flush_pending_upserts(self) -> None:
        """Batch-write all pending node upserts in a single transaction."""
        if not self._pending_upserts:
            return

        batch = dict(self._pending_upserts)
        self._pending_upserts.clear()

        rows = [
            (
                dest_hash.hex(),
                info.get("app_name", ""),
                info.get("aspects", ""),
                info.get("hops"),
                info.get("last_seen"),
                info.get("first_seen"),
                info.get("announce_count", 1),
                info.get("app_data_str", ""),
            )
            for dest_hash, info in batch.items()
        ]

        try:
            with self._write_conn_lock:
                self._write_conn.executemany(
                    """
                    INSERT INTO known_nodes
                    (destination_hash, app_name, aspects, hops,
                     last_seen, first_seen, announce_count, app_data_str)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(destination_hash) DO UPDATE SET
                        app_name = excluded.app_name,
                        aspects = excluded.aspects,
                        hops = excluded.hops,
                        last_seen = excluded.last_seen,
                        first_seen = excluded.first_seen,
                        announce_count = excluded.announce_count,
                        app_data_str = excluded.app_data_str
                    """,
                    rows,
                )
                self._write_conn.commit()
        except Exception:
            self.log.debug(
                "Error flushing %d node upserts to database",
                len(batch),
                exc_info=True,
            )

    # --- SQLite ---

    def _init_db(self) -> None:
        with closing(sqlite3.connect(self._db_path)) as conn, conn:
            # auto_vacuum must be set before any table exists to take effect on
            # a new DB (no-op on existing DBs — the live file is VACUUMed
            # separately).  Lets incremental_vacuum reclaim freed pages.
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
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
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_known_nodes_app_name ON known_nodes(app_name)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_known_nodes_last_seen ON known_nodes(last_seen DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_known_nodes_announce_count ON known_nodes(announce_count DESC)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_known_nodes_hops ON known_nodes(hops)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_known_nodes_app_lastseen ON known_nodes(app_name, last_seen DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_interface_stats_timestamp ON interface_stats(timestamp)"
            )

    def _load_from_db(self) -> None:
        try:
            with closing(sqlite3.connect(self._db_path)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM known_nodes ORDER BY last_seen DESC LIMIT ?",
                    (self._max_cached_nodes,),
                ).fetchall()
                for row in rows:
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
            self.log.info(
                "Loaded %d known nodes from database",
                len(self._known_nodes),
            )
        except Exception:
            self.log.exception("Error loading known nodes from database")

    def _reclassify_unclassified(self) -> None:
        """One-time pass: apply app_data heuristics to nodes with empty app_name."""
        reclassified = 0
        try:
            with self._write_conn_lock:
                rows = self._write_conn.execute(
                    "SELECT destination_hash, app_data_str FROM known_nodes "
                    "WHERE (app_name = '' OR app_name IS NULL) "
                    "AND app_data_str IS NOT NULL AND app_data_str != ''"
                ).fetchall()
                for dest_hex, app_data_str in rows:
                    inferred_app, inferred_aspect = _infer_app_from_data(app_data_str)
                    if inferred_app:
                        self._write_conn.execute(
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
                self._write_conn.commit()
            if reclassified:
                self.log.info(
                    "Reclassified %d nodes from app_data heuristics",
                    reclassified,
                )
        except Exception:
            self.log.exception("Error reclassifying nodes")

    def _save_interface_stats(self) -> None:
        stats = self.get_interface_stats()
        if not stats:
            return
        now = time.time()
        rows = [
            (
                now,
                s.get("name", ""),
                s.get("type", ""),
                1 if s.get("online", True) else 0,
                s.get("rxb"),
                s.get("txb"),
                s.get("bitrate"),
                s.get("peers"),
            )
            for s in stats
        ]
        try:
            with self._write_conn_lock:
                self._write_conn.executemany(
                    "INSERT INTO interface_stats "
                    "(timestamp, name, type, online, rxb, txb, "
                    "bitrate, peers) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                self._write_conn.commit()
        except Exception:
            self.log.debug("Error saving interface stats", exc_info=True)
        self._stats_save_count += 1
        if self._stats_save_count % 10 == 0:
            try:
                with self._write_conn_lock:
                    (count,) = self._write_conn.execute(
                        "SELECT COUNT(*) FROM interface_stats"
                    ).fetchone()
                    if count > self._max_stats_rows:
                        excess = count - self._max_stats_rows
                        self._write_conn.execute(
                            "DELETE FROM interface_stats "
                            "WHERE rowid IN "
                            "(SELECT rowid FROM interface_stats "
                            "ORDER BY timestamp ASC LIMIT ?)",
                            (excess,),
                        )
                        self._write_conn.commit()
                        self.log.debug(
                            "Trimmed %d old interface_stats rows",
                            excess,
                        )
            except Exception:
                self.log.debug("Error trimming interface stats", exc_info=True)

    def _prune_old_data(self) -> None:
        max_days = self.config.get("max_history_days", 30)
        cutoff = time.time() - (max_days * 86400)
        try:
            with self._write_conn_lock:
                self._write_conn.execute(
                    "DELETE FROM interface_stats WHERE timestamp < ?",
                    (cutoff,),
                )
                self._write_conn.execute(
                    "DELETE FROM known_nodes WHERE last_seen < ?",
                    (cutoff,),
                )
                self._write_conn.commit()
                # Reclaim WAL/file space the deletes freed up — without
                # this the WAL grows unbounded and the file never shrinks.
                try:
                    self._write_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    self.log.debug("wal_checkpoint(TRUNCATE) failed", exc_info=True)
            # Also prune from memory
            with self._nodes_lock:
                expired = [h for h, info in self._known_nodes.items() if info["last_seen"] < cutoff]
                for h in expired:
                    del self._known_nodes[h]
        except Exception:
            self.log.debug("Error pruning old data", exc_info=True)

    def _refresh_summary_cache(self) -> None:
        """Run the heavy mesh-summary scan and warm _summary_cache.

        Owned by the maintenance loop so the broadcast thread never has to run
        the two full known_nodes scans inline.
        """
        try:
            summary = self._compute_mesh_summary()
        except Exception:
            self.log.debug("Error refreshing mesh summary cache", exc_info=True)
            return
        with self._summary_cache_lock:
            self._summary_cache = (time.monotonic(), summary)

    def _maintenance_loop(self) -> None:
        """Periodically collect interface stats and prune old data."""
        cycles_since_prune = 0
        # Warm the summary cache once up front so broadcasts have it from the
        # start instead of waiting a full 60s cycle.
        self._refresh_summary_cache()
        while self._active:
            try:
                self._save_interface_stats()
            except Exception:
                self.log.debug("Error in maintenance loop", exc_info=True)

            # Keep the broadcast-thread summary cache warm (heavy scan lives
            # here, off the broadcast thread).
            self._refresh_summary_cache()

            self._sleep_while_active(60)
            if not self._active:
                break

            # Prune every 60 cycles (once per hour)
            cycles_since_prune += 1
            if cycles_since_prune >= 60:
                self._prune_old_data()
                cycles_since_prune = 0
