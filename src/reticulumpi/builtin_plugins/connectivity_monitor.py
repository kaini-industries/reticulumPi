"""Connectivity Monitor plugin — diagnoses link failures and logs transport health."""

from __future__ import annotations

import logging
import math
import os
import socket
import threading
import time
from logging.handlers import RotatingFileHandler
from typing import Any

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

# Default log path
_DEFAULT_LOG_PATH = "~/.local/share/reticulumpi/connectivity.log"

# How often to run a full diagnostics cycle (seconds)
_DEFAULT_CHECK_INTERVAL = 30

# How long I2P gets to bootstrap before we start warning (seconds)
_I2P_BOOTSTRAP_GRACE = 600  # 10 minutes

# Paths expiring within this many seconds are flagged
_EXPIRING_SOON_THRESHOLD = 600  # 10 minutes

# Path data staleness threshold (seconds) — if ALL paths in the routing
# table are older than this, transport may not be receiving announces.
_PATH_STALE_THRESHOLD = 1800  # 30 minutes

# Consecutive zero-traffic-delta checks before warning about a TCP hub.
# At the default 30s check interval, 3 checks = ~90s of silence.
_HUB_STALE_CHECKS = 3


def _hex_hash(raw: Any) -> str:
    """Convert a raw hash (bytes or str) to a hex string."""
    if isinstance(raw, bytes):
        return raw.hex()
    if isinstance(raw, str):
        return raw
    return str(raw)


class ConnectivityMonitorPlugin(PluginBase):
    """Monitors transport health and logs diagnostics for link failures.

    Writes a dedicated connectivity log file with actionable information
    about why link establishment may be failing.  Surfaces a health
    summary on the web dashboard.

    Monitors:
    - rnsd shared instance availability
    - Interface health (traffic, online status)
    - I2P tunnel bootstrap and peer count
    - i2pd SAM API reachability
    - Path table statistics (size, hop distribution, interface spread)
    - Routing table with full path details
    - Transport hub reachability
    """

    plugin_name = "connectivity_monitor"
    plugin_version = "2.0.0"
    plugin_description = "Diagnoses link failures and logs transport/routing health"

    def validate_config(self) -> None:
        interval = self.config.get("check_interval", _DEFAULT_CHECK_INTERVAL)
        if not isinstance(interval, (int, float)) or interval < 10:
            raise ValueError("check_interval must be >= 10 seconds")

    def start(self) -> None:
        self._active = True
        self._lock = threading.Lock()
        self._check_interval = self.config.get("check_interval", _DEFAULT_CHECK_INTERVAL)
        self._sam_port = self.config.get("sam_port", 7656)
        self._shared_instance_port = self.config.get("shared_instance_port", 37428)

        # Latest health snapshot for API/dashboard
        self._health: dict[str, Any] = {
            "rnsd_reachable": False,
            "i2p_status": "unknown",
            "i2p_peers": 0,
            "sam_reachable": False,
            "interfaces_online": 0,
            "interfaces_total": 0,
            "i2p_traffic": {"rxb": 0, "txb": 0},
            "path_count": 0,
            "path_via_i2p": 0,
            "path_via_tcp": 0,
            "path_hop_avg": 0.0,
            "path_hop_max": 0,
            "issues": [],
            "last_check": 0.0,
            # Routing summary (sent via WebSocket)
            "routing": {
                "path_count": 0,
                "hop_distribution": {},
                "interface_distribution": {},
                "freshness": {
                    "newest_age_s": 0,
                    "oldest_age_s": 0,
                    "avg_age_s": 0,
                    "expiring_soon": 0,
                },
                "link_count": 0,
                "rate_limited_count": 0,
                "blackholed_count": 0,
                "transport_id": None,
                "transport_uptime": 0,
                "probe_responder": None,
                "diagnostics": [],
            },
        }

        # Full routing data cache (served via REST, NOT sent over WS)
        self._routing_data: dict[str, Any] = {
            "path_table": [],
            "rate_table": [],
            "blackholed": {},
        }

        # Cached interface stats from last _check_rnsd() call
        self._last_iface_stats: dict[str, Any] | None = None

        # Track rnsd outage duration
        self._rnsd_down_since: float | None = None
        self._i2p_start_time: float = time.monotonic()

        # Previous interface traffic for delta calculation
        self._prev_iface_traffic: dict[str, dict[str, int]] = {}
        # Consecutive zero-delta checks per TCP hub (warn after 3 = ~90s)
        self._hub_zero_delta_count: dict[str, int] = {}

        # Set up dedicated connectivity log file
        self._conn_log = self._setup_log_file()

        self._start_thread(self._monitor_loop, "connectivity-monitor")

        self.log.info(
            "Connectivity monitor active (interval=%ds, log=%s)",
            self._check_interval,
            self.config.get("log_path", _DEFAULT_LOG_PATH),
        )

    def stop(self) -> None:
        self._active = False
        self._join_threads()

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._active,
                "rnsd_reachable": self._health["rnsd_reachable"],
                "interfaces_online": self._health["interfaces_online"],
                "path_count": self._health["path_count"],
                "issues": len(self._health["issues"]),
            }

    def get_health(self) -> dict[str, Any]:
        """Return the full health snapshot for the dashboard API."""
        with self._lock:
            return dict(self._health)

    def get_routing_data(
        self,
        page: int = 1,
        per_page: int = 100,
        sort: str = "hops",
        order: str = "asc",
        iface_filter: str = "",
        min_hops: int | None = None,
        max_hops: int | None = None,
        search: str = "",
    ) -> dict[str, Any]:
        """Return paginated, filtered routing data for the REST API.

        Args:
            page: Page number (1-based).
            per_page: Items per page (0 = summary only, max 500).
            sort: Sort field (hops, timestamp, expires, hash, interface).
            order: Sort order (asc or desc).
            iface_filter: Substring filter on interface name.
            min_hops: Minimum hop count filter.
            max_hops: Maximum hop count filter.
            search: Hex prefix filter on destination hash.

        Returns:
            Dict with summary, paginated paths, rate_table, blackholed.
        """
        with self._lock:
            summary = dict(self._health.get("routing", {}))
            paths = list(self._routing_data.get("path_table", []))
            rate_table = list(self._routing_data.get("rate_table", []))
            blackholed = dict(self._routing_data.get("blackholed", {}))

        # Apply filters
        if search:
            search_lower = search.lower()
            paths = [p for p in paths if p.get("hash", "").lower().startswith(search_lower)]
        if iface_filter:
            iface_lower = iface_filter.lower()
            paths = [p for p in paths if iface_lower in p.get("interface", "").lower()]
        if min_hops is not None:
            paths = [p for p in paths if p.get("hops", 0) >= min_hops]
        if max_hops is not None:
            paths = [p for p in paths if p.get("hops", 0) <= max_hops]

        # Sort
        valid_sort_keys = {"hops", "timestamp", "expires", "hash", "interface"}
        if sort not in valid_sort_keys:
            sort = "hops"
        reverse = order.lower() == "desc"
        paths.sort(key=lambda p: p.get(sort, 0) or 0, reverse=reverse)

        total_paths = len(paths)

        # Pagination
        per_page = min(max(per_page, 0), 500)
        if per_page == 0:
            # Summary only
            return {
                "summary": summary,
                "paths": [],
                "total_paths": total_paths,
                "page": 1,
                "per_page": 0,
                "pages": 0,
                "rate_table": rate_table,
                "blackholed": blackholed,
            }

        pages = max(1, math.ceil(total_paths / per_page))
        page = max(1, min(page, pages))
        start = (page - 1) * per_page
        end = start + per_page

        return {
            "summary": summary,
            "paths": paths[start:end],
            "total_paths": total_paths,
            "page": page,
            "per_page": per_page,
            "pages": pages,
            "rate_table": rate_table,
            "blackholed": blackholed,
        }

    # --- Internal ---

    def _setup_log_file(self) -> logging.Logger:
        """Create a dedicated logger that writes to the connectivity log file."""
        log_path = os.path.expanduser(
            self.config.get("log_path", _DEFAULT_LOG_PATH)
        )
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

        logger = logging.getLogger("reticulumpi.connectivity")
        logger.setLevel(logging.DEBUG)
        # Avoid duplicate handlers on restart
        logger.handlers.clear()

        handler = RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)-8s] %(message)s")
        )
        logger.addHandler(handler)
        return logger

    def _monitor_loop(self) -> None:
        """Periodically run diagnostics and log findings."""
        # Initial delay to let things settle
        self._sleep_while_active(5)

        while self._active:
            try:
                self._run_diagnostics()
            except Exception:
                self.log.debug("Error in connectivity monitor loop", exc_info=True)

            self._sleep_while_active(self._check_interval)

    def _run_diagnostics(self) -> None:
        """Run a full diagnostics cycle."""
        issues: list[str] = []
        now = time.monotonic()

        # 1. Check rnsd shared instance (also caches interface stats)
        rnsd_ok = self._check_rnsd()
        if not rnsd_ok:
            if self._rnsd_down_since is None:
                self._rnsd_down_since = now
                self._conn_log.critical(
                    "rnsd shared instance UNREACHABLE — "
                    "ALL link establishment will fail"
                )
            downtime = now - self._rnsd_down_since
            issues.append(
                f"rnsd unreachable for {downtime:.0f}s — all links will fail"
            )
        else:
            if self._rnsd_down_since is not None:
                downtime = now - self._rnsd_down_since
                self._conn_log.info(
                    "rnsd shared instance RECOVERED after %.0fs downtime",
                    downtime,
                )
                self._rnsd_down_since = None

        # 2. Collect interface stats
        iface_issues = self._check_interfaces()
        issues.extend(iface_issues)

        # 3. Check I2P / i2pd health
        i2p_issues = self._check_i2p()
        issues.extend(i2p_issues)

        # 4. Analyze path table and collect routing data
        path_issues = self._check_paths()
        issues.extend(path_issues)

        # 5. Collect full routing data (path table, rate table, etc.)
        routing_issues = self._collect_routing_data()
        issues.extend(routing_issues)

        # Store health snapshot
        with self._lock:
            self._health["issues"] = issues
            self._health["last_check"] = time.time()

        # Log summary periodically
        if issues:
            self._conn_log.warning(
                "Diagnostics found %d issue(s): %s",
                len(issues),
                "; ".join(issues),
            )
        else:
            self._conn_log.info(
                "Diagnostics OK — rnsd=%s, interfaces=%d/%d online, "
                "paths=%d, i2p=%s (%d peers)",
                "up" if rnsd_ok else "DOWN",
                self._health["interfaces_online"],
                self._health["interfaces_total"],
                self._health["path_count"],
                self._health["i2p_status"],
                self._health["i2p_peers"],
            )

    def _check_rnsd(self) -> bool:
        """Check if the rnsd shared instance is reachable.

        The shared instance port is dynamically assigned by rnsd, so we
        can't just probe a fixed port.  Instead we check whether the
        Reticulum instance still has a working connection by calling
        ``get_interface_stats()`` — this queries rnsd across the
        LocalClientInterface and will fail if rnsd is down.

        Caches the interface stats result for use by other methods.
        """
        try:
            rns_instance = self.app.reticulum
            if not rns_instance:
                self._last_iface_stats = None
                with self._lock:
                    self._health["rnsd_reachable"] = False
                return False

            # get_interface_stats() queries rnsd — if it responds, rnsd is alive
            stats = rns_instance.get_interface_stats()
            if stats and "interfaces" in stats:
                self._last_iface_stats = stats
                with self._lock:
                    self._health["rnsd_reachable"] = True
                return True

            self._last_iface_stats = None
            with self._lock:
                self._health["rnsd_reachable"] = False
            return False
        except Exception:
            self._last_iface_stats = None
            with self._lock:
                self._health["rnsd_reachable"] = False
            return False

    def _check_interfaces(self) -> list[str]:
        """Check interface health via get_interface_stats()."""
        issues: list[str] = []
        try:
            rns_instance = self.app.reticulum
            if not rns_instance or not hasattr(rns_instance, "get_interface_stats"):
                return issues

            stats = rns_instance.get_interface_stats()
            interfaces = stats.get("interfaces", [])

            online_count = 0
            total_count = 0

            for entry in interfaces:
                itype = entry.get("type", "")
                name = entry.get("name", "?")
                status = entry.get("status", False)

                # Skip internal interfaces
                if itype in ("LocalClientInterface", "LocalServerInterface"):
                    continue

                total_count += 1
                if status:
                    online_count += 1
                else:
                    issues.append(f"Interface '{name}' ({itype}) is OFFLINE")
                    self._conn_log.warning(
                        "Interface OFFLINE: %s (%s)", name, itype
                    )

                # Track I2P-specific stats
                if itype == "I2PInterface":
                    rxb = entry.get("rxb", 0)
                    txb = entry.get("txb", 0)
                    peers = entry.get("i2p_peers", 0)

                    with self._lock:
                        self._health["i2p_traffic"] = {"rxb": rxb, "txb": txb}
                        self._health["i2p_peers"] = peers

                    self._prev_iface_traffic["i2p"] = {"rxb": rxb, "txb": txb}

                # Track TCP client traffic — only warn after several
                # consecutive zero-delta checks to avoid false alarms
                # during normal quiet periods.
                if itype == "TCPClientInterface":
                    rxb = entry.get("rxb", 0)
                    txb = entry.get("txb", 0)
                    prev = self._prev_iface_traffic.get(name, {})
                    if prev:
                        delta_rx = rxb - prev.get("rxb", 0)
                        delta_tx = txb - prev.get("txb", 0)
                        if delta_rx == 0 and delta_tx == 0:
                            count = self._hub_zero_delta_count.get(name, 0) + 1
                            self._hub_zero_delta_count[name] = count
                            if count >= _HUB_STALE_CHECKS:
                                issues.append(
                                    f"TCP hub '{name}' has had 0 traffic "
                                    f"for {count} checks — may be stale"
                                )
                        else:
                            self._hub_zero_delta_count[name] = 0
                    self._prev_iface_traffic[name] = {"rxb": rxb, "txb": txb}

            with self._lock:
                self._health["interfaces_online"] = online_count
                self._health["interfaces_total"] = total_count

            if online_count == 0 and total_count > 0:
                self._conn_log.critical(
                    "ALL %d interfaces are OFFLINE — no connectivity",
                    total_count,
                )

        except Exception:
            self.log.debug("Error checking interfaces", exc_info=True)

        return issues

    def _check_i2p(self) -> list[str]:
        """Check i2pd SAM API, daemon health, and I2P network status.

        Queries i2pd's web console (port 7070) to get the actual network
        status (OK/Firewalled/Testing) and tunnel counts, which gives a
        much more accurate picture than just checking RNS peer count.

        Having 0 RNS peers over I2P is normal if no other RNS nodes use
        I2P — that's not a failure, just no I2P peers available.
        """
        issues: list[str] = []

        # 1. Check SAM API port
        sam_ok = self._probe_port("127.0.0.1", self._sam_port)
        with self._lock:
            self._health["sam_reachable"] = sam_ok

        if not sam_ok:
            issues.append(
                f"i2pd SAM API unreachable on port {self._sam_port} — "
                "I2P interface cannot function"
            )
            self._conn_log.warning(
                "i2pd SAM API UNREACHABLE on port %d",
                self._sam_port,
            )
            with self._lock:
                self._health["i2p_status"] = "sam_unreachable"
            return issues

        # 2. Query i2pd web console for actual network state
        i2pd_info = self._query_i2pd_console()
        with self._lock:
            self._health["i2pd_network_status"] = i2pd_info.get(
                "network_status", "unknown"
            )
            self._health["i2pd_known_routers"] = i2pd_info.get("routers", 0)
            self._health["i2pd_client_tunnels"] = i2pd_info.get(
                "client_tunnels", 0
            )

        net_status = i2pd_info.get("network_status", "unknown").lower()
        routers = i2pd_info.get("routers", 0)
        client_tunnels = i2pd_info.get("client_tunnels", 0)
        elapsed = time.monotonic() - self._i2p_start_time

        # 3. Evaluate status
        if routers == 0 and elapsed < _I2P_BOOTSTRAP_GRACE:
            with self._lock:
                self._health["i2p_status"] = "bootstrapping"
            self._conn_log.info(
                "I2P bootstrapping (%.0f/%ds elapsed, %d routers known)",
                elapsed,
                _I2P_BOOTSTRAP_GRACE,
                routers,
            )
        elif "firewalled" in net_status:
            with self._lock:
                self._health["i2p_status"] = "firewalled"
            # Firewalled is common behind NAT — informational, not an error
            if elapsed > _I2P_BOOTSTRAP_GRACE:
                self._conn_log.info(
                    "i2pd status: firewalled (behind NAT) — "
                    "%d routers known, %d client tunnels. "
                    "Outbound I2P works; inbound requires port forwarding. "
                    "RNS I2P peers: %d",
                    routers,
                    client_tunnels,
                    self._health.get("i2p_peers", 0),
                )
        elif "ok" in net_status:
            with self._lock:
                self._health["i2p_status"] = "ok"
            self._conn_log.info(
                "i2pd status: OK — %d routers, %d tunnels, %d RNS peers",
                routers,
                client_tunnels,
                self._health.get("i2p_peers", 0),
            )
        elif "testing" in net_status:
            with self._lock:
                self._health["i2p_status"] = "testing"
        else:
            with self._lock:
                self._health["i2p_status"] = net_status or "unknown"

        # Only flag as an issue if i2pd itself is failing
        if routers == 0 and elapsed > _I2P_BOOTSTRAP_GRACE:
            issues.append(
                "i2pd knows 0 routers after "
                f"{elapsed / 60:.0f} min — I2P network unreachable"
            )
            self._conn_log.warning(
                "i2pd has 0 known routers — may need restart: "
                "sudo systemctl restart i2pd"
            )

        return issues

    @staticmethod
    def _query_i2pd_console() -> dict[str, Any]:
        """Query i2pd's HTTP console for network status.

        Returns a dict with keys: network_status, routers, floodfills,
        client_tunnels, uptime_minutes.
        """
        import urllib.request

        info: dict[str, Any] = {}
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:7070/", timeout=3
            ) as resp:
                import re

                html = resp.read().decode("utf-8", errors="replace")
                # Strip HTML tags for text parsing
                text = re.sub(r"<[^>]+>", "\n", html)
                text = re.sub(r"\s+", " ", text)

                # Extract key metrics
                for pattern, key, conv in [
                    (r"Network\s+status:\s+(\w+)", "network_status", str),
                    (r"Routers:\s+(\d+)", "routers", int),
                    (r"Floodfills:\s+(\d+)", "floodfills", int),
                    (r"Client\s+Tunnels:\s+(\d+)", "client_tunnels", int),
                    (r"Transit\s+Tunnels:\s+(\d+)", "transit_tunnels", int),
                ]:
                    m = re.search(pattern, text, re.IGNORECASE)
                    if m:
                        info[key] = conv(m.group(1))

        except Exception:
            pass

        return info

    def _check_paths(self) -> list[str]:
        """Analyze RNS path table for basic connectivity insights.

        Checks destination table freshness and transport state.
        The detailed path table analysis is done in _collect_routing_data().
        """
        issues: list[str] = []

        try:
            rns_instance = self.app.reticulum
            if not rns_instance:
                return issues

            # Check transport state from cached interface stats
            stats = self._last_iface_stats
            if stats:
                transport_id = stats.get("transport_id")
                if transport_id:
                    with self._lock:
                        self._health["transport_active"] = True
                else:
                    with self._lock:
                        self._health["transport_active"] = False

            # Note: We previously checked the destination_table file mtime
            # here, but rnsd flushes that file very infrequently (can be 9+
            # hours between writes even when healthy).  Actual path freshness
            # is now checked in _collect_routing_data() using live RPC data.

        except Exception:
            self.log.debug("Error analyzing path table", exc_info=True)

        return issues

    def _collect_routing_data(self) -> list[str]:
        """Collect full routing table data from rnsd via RPC.

        Calls get_path_table(), get_rate_table(), get_link_count(), and
        get_blackholed_identities() to build a complete routing snapshot.
        Computes summary statistics and runs diagnostic rules.

        Returns a list of routing-specific issues.
        """
        issues: list[str] = []

        try:
            rns_instance = self.app.reticulum
            if not rns_instance:
                return issues

            # 1. Get path table
            path_table_raw: list[dict] = []
            try:
                path_table_raw = rns_instance.get_path_table() or []
            except Exception:
                self.log.debug("Failed to get path table via RPC", exc_info=True)

            # 2. Get rate table
            rate_table_raw: list[dict] = []
            try:
                rate_table_raw = rns_instance.get_rate_table() or []
            except Exception:
                self.log.debug("Failed to get rate table via RPC", exc_info=True)

            # 3. Get link count
            link_count = 0
            try:
                link_count = rns_instance.get_link_count() or 0
            except Exception:
                self.log.debug("Failed to get link count via RPC", exc_info=True)

            # 4. Get blackholed identities
            blackholed: dict = {}
            try:
                blackholed = rns_instance.get_blackholed_identities() or {}
            except Exception:
                self.log.debug("Failed to get blackholed identities", exc_info=True)

            # 5. Extract transport info from cached interface stats
            transport_id = None
            transport_uptime = 0
            probe_responder = None
            stats = self._last_iface_stats
            if stats:
                raw_tid = stats.get("transport_id")
                if raw_tid:
                    transport_id = _hex_hash(raw_tid)
                transport_uptime = stats.get("transport_uptime", 0)
                raw_probe = stats.get("probe_responder")
                if raw_probe:
                    probe_responder = _hex_hash(raw_probe)

            # 6. Process path table entries
            now = time.time()
            path_entries: list[dict[str, Any]] = []
            hop_dist: dict[int, int] = {}
            iface_dist: dict[str, int] = {}
            ages: list[float] = []
            expiring_soon = 0

            for entry in path_table_raw:
                hops = entry.get("hops", 0)
                iface = entry.get("interface", "unknown")
                ts = entry.get("timestamp", 0)
                expires = entry.get("expires", 0)

                age_s = max(0, now - ts) if ts else 0
                expires_in_s = max(0, expires - now) if expires else 0

                path_entry = {
                    "hash": _hex_hash(entry.get("hash", "")),
                    "hops": hops,
                    "via": _hex_hash(entry.get("via", "")),
                    "interface": iface,
                    "timestamp": ts,
                    "age_s": round(age_s),
                    "expires": expires,
                    "expires_in_s": round(expires_in_s),
                }
                path_entries.append(path_entry)

                # Accumulate stats
                hop_dist[hops] = hop_dist.get(hops, 0) + 1
                iface_dist[iface] = iface_dist.get(iface, 0) + 1
                if ts:
                    ages.append(age_s)
                if 0 < expires_in_s < _EXPIRING_SOON_THRESHOLD:
                    expiring_soon += 1

            # 7. Process rate table entries
            rate_entries: list[dict[str, Any]] = []
            for entry in rate_table_raw:
                rate_entries.append({
                    "hash": _hex_hash(entry.get("hash", "")),
                    "last": entry.get("last", 0),
                    "rate_violations": entry.get("rate_violations", 0),
                    "blocked_until": entry.get("blocked_until", 0),
                    "timestamps": entry.get("timestamps", []),
                })

            # 8. Process blackholed identities
            blackholed_clean: dict[str, Any] = {}
            for identity_hash, info in blackholed.items():
                blackholed_clean[_hex_hash(identity_hash)] = {
                    "until": info.get("until"),
                    "reason": info.get("reason", ""),
                }

            # 9. Compute freshness summary
            freshness: dict[str, Any] = {
                "newest_age_s": 0,
                "oldest_age_s": 0,
                "avg_age_s": 0,
                "expiring_soon": expiring_soon,
            }
            if ages:
                freshness["newest_age_s"] = round(min(ages))
                freshness["oldest_age_s"] = round(max(ages))
                freshness["avg_age_s"] = round(sum(ages) / len(ages))

            # 10. Compute hop stats for backward compatibility
            path_count = len(path_entries)
            hop_values = [e["hops"] for e in path_entries]
            hop_avg = sum(hop_values) / len(hop_values) if hop_values else 0.0
            hop_max = max(hop_values) if hop_values else 0

            # Count paths by interface type
            i2p_paths = sum(v for k, v in iface_dist.items() if "I2P" in k)
            tcp_paths = sum(v for k, v in iface_dist.items() if "TCP" in k)

            # 11. Run diagnostic rules
            routing_diags: list[str] = []

            if path_count == 0:
                routing_diags.append("Path table is empty — node may be isolated")
                self._publish_event(events.PATH_TABLE_EMPTY)

            if ages and min(ages) > _PATH_STALE_THRESHOLD:
                stale_min = round(min(ages) / 60)
                routing_diags.append(
                    f"No paths refreshed in {stale_min} min — "
                    "transport may not be receiving announces"
                )
                self._publish_event(events.PATHS_STALE)

            # Single point of failure: all paths via one interface
            # In shared-instance mode every path appears via LocalInterface
            # because reticulumpi talks to rnsd over a local socket — the
            # real interface diversity lives inside rnsd, so suppress the
            # warning for LocalInterface.
            if path_count > 0 and len(iface_dist) == 1:
                sole_iface = next(iter(iface_dist))
                if "LocalInterface" not in sole_iface:
                    routing_diags.append(
                        f"All {path_count} paths route via '{sole_iface}' — "
                        "single point of failure"
                    )
                    self._publish_event(events.SINGLE_INTERFACE_SPOF)

            if len(rate_entries) > 0:
                routing_diags.append(
                    f"{len(rate_entries)} destination(s) are rate-limited"
                )

            if len(blackholed_clean) > 0:
                routing_diags.append(
                    f"{len(blackholed_clean)} identity(ies) are blackholed"
                )

            # Note: "paths expiring soon" is normal Reticulum lifecycle
            # behaviour — it's already visible in the freshness stats
            # (expiring_soon count) so we don't flag it as a diagnostic.

            # 12. Store everything
            routing_summary = {
                "path_count": path_count,
                "hop_distribution": hop_dist,
                "interface_distribution": iface_dist,
                "freshness": freshness,
                "link_count": link_count,
                "rate_limited_count": len(rate_entries),
                "blackholed_count": len(blackholed_clean),
                "transport_id": transport_id,
                "transport_uptime": transport_uptime,
                "probe_responder": probe_responder,
                "diagnostics": routing_diags,
            }

            with self._lock:
                self._health["routing"] = routing_summary
                self._health["path_count"] = path_count
                self._health["path_hop_avg"] = round(hop_avg, 1)
                self._health["path_hop_max"] = hop_max
                self._health["path_via_i2p"] = i2p_paths
                self._health["path_via_tcp"] = tcp_paths

                self._routing_data["path_table"] = path_entries
                self._routing_data["rate_table"] = rate_entries
                self._routing_data["blackholed"] = blackholed_clean

            # Return routing diagnostics as issues for the main issues list
            issues.extend(routing_diags)

        except Exception:
            self.log.debug("Error collecting routing data", exc_info=True)

        return issues

    def _publish_event(self, event_type: str) -> None:
        """Publish an event to the event bus if available."""
        try:
            if hasattr(self, "event_bus") and self.event_bus:
                self.event_bus.publish(event_type, {})
        except Exception:
            pass

    @staticmethod
    def _probe_port(host: str, port: int, timeout: float = 3) -> bool:
        """Test if a TCP port is reachable."""
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
            return True
        except (OSError, socket.timeout):
            return False
