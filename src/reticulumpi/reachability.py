"""Reachability scoring for Reticulum mesh nodes.

Computes a 0-100 connection likelihood score based on path existence,
path freshness, hop count, announce recency, and transport relay health.
"""

from __future__ import annotations

import time
from typing import Any


def compute_reachability(
    node: dict[str, Any],
    path_entry: dict[str, Any] | None,
    relay_health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute a reachability score for a single mesh node.

    Args:
        node: Node data from network_map (destination_hash, hops, last_seen).
        path_entry: Path table entry from connectivity_monitor
                    (age_s, hops, via, interface).  None if no path exists.
        relay_health: Transport health record for the relay this path transits.
                      None if direct path or relay not tracked.

    Returns:
        Dict with ``score`` (0--100), ``label``, and ``factors`` breakdown.
    """
    factors: dict[str, dict[str, Any]] = {}
    score = 0
    now = time.time()

    # ── Factor 1: Path exists (0-30) ────────────────────────────────
    if path_entry:
        factors["path"] = {"points": 30, "max": 30, "detail": "Path exists"}
        score += 30
    else:
        factors["path"] = {"points": 0, "max": 30, "detail": "No known path"}

    # ── Factor 2: Path freshness (0-20) ─────────────────────────────
    if path_entry:
        age_s = path_entry.get("age_s", 99999)
        if age_s < 300:  # < 5 min
            pts = 20
            detail = f"Fresh ({fmt_age(age_s)})"
        elif age_s < 1200:  # < 20 min
            pts = 15
            detail = f"Recent ({fmt_age(age_s)})"
        elif age_s < 3600:  # < 1 hour
            pts = 10
            detail = f"Aging ({fmt_age(age_s)})"
        elif age_s < 14400:  # < 4 hours
            pts = 5
            detail = f"Stale ({fmt_age(age_s)})"
        else:
            pts = 0
            detail = f"Very stale ({fmt_age(age_s)})"
        factors["freshness"] = {"points": pts, "max": 20, "detail": detail}
        score += pts
    else:
        factors["freshness"] = {"points": 0, "max": 20, "detail": "No path"}

    # ── Factor 3: Hop count (0-15) ──────────────────────────────────
    hops = None
    if path_entry:
        hops = path_entry.get("hops")
    if hops is None:
        hops = node.get("hops")

    if hops is not None:
        if hops <= 1:
            pts = 15
            detail = f"Direct ({hops} hop{'s' if hops != 1 else ''})"
        elif hops <= 3:
            pts = 12
            detail = f"Near ({hops} hops)"
        elif hops <= 6:
            pts = 8
            detail = f"Moderate ({hops} hops)"
        elif hops <= 10:
            pts = 4
            detail = f"Distant ({hops} hops)"
        else:
            pts = 1
            detail = f"Very distant ({hops} hops)"
        factors["hops"] = {"points": pts, "max": 15, "detail": detail}
        score += pts
    else:
        factors["hops"] = {"points": 0, "max": 15, "detail": "Unknown"}

    # ── Factor 4: Announce recency (0-15) ───────────────────────────
    last_seen = node.get("last_seen")
    if last_seen:
        age = now - last_seen
        if age < 600:  # < 10 min
            pts = 15
            detail = f"Just announced ({fmt_age(age)} ago)"
        elif age < 3600:  # < 1 hour
            pts = 12
            detail = f"Recent ({fmt_age(age)} ago)"
        elif age < 14400:  # < 4 hours
            pts = 8
            detail = f"Hours ago ({fmt_age(age)})"
        elif age < 86400:  # < 1 day
            pts = 4
            detail = f"Today ({fmt_age(age)} ago)"
        else:
            pts = 1
            detail = f"Old ({fmt_age(age)} ago)"
        factors["announce"] = {"points": pts, "max": 15, "detail": detail}
        score += pts
    else:
        factors["announce"] = {"points": 0, "max": 15, "detail": "Never seen"}

    # ── Factor 5: Transport relay health (0-20) ─────────────────────
    if path_entry:
        via = path_entry.get("via", "")
        is_direct = not via or via == "0" * len(via)

        if is_direct:
            pts = 20
            detail = "Direct (no relay)"
        elif relay_health:
            status = relay_health.get("status", "unknown")
            avail = relay_health.get("availability_pct", 0)
            if status == "healthy":
                pts = 20
                detail = f"Relay healthy ({avail:.0f}%)"
            elif status == "degraded":
                pts = 10
                detail = f"Relay degraded ({avail:.0f}%)"
            elif status == "down":
                pts = 0
                detail = "Relay DOWN"
            elif status == "new":
                pts = 15
                detail = "Relay new (monitoring)"
            else:
                pts = 10
                detail = f"Relay: {status}"
        else:
            # Path through a relay we aren't tracking yet
            pts = 12
            detail = "Via relay (unmonitored)"
        factors["relay"] = {"points": pts, "max": 20, "detail": detail}
        score += pts
    else:
        factors["relay"] = {"points": 0, "max": 20, "detail": "No path"}

    score = max(0, min(100, score))
    label = _score_to_label(score)

    return {"score": score, "label": label, "factors": factors}


def score_all_nodes(
    nodes: list[dict[str, Any]],
    path_table: list[dict[str, Any]],
    transport_nodes: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Score all known nodes and return sorted results.

    Args:
        nodes: From ``network_map.get_known_nodes()``.
        path_table: From ``connectivity_monitor.get_routing_data()["paths"]``.
        transport_nodes: From ``transport_health.get_transport_nodes()``.

    Returns:
        List of scored node dicts, sorted by score descending.
    """
    # Build lookup maps (strip angle-bracket formatting from hashes)
    path_by_hash: dict[str, dict] = {}
    for p in path_table:
        h = _clean_hash(p.get("hash", ""))
        if h:
            path_by_hash[h] = p

    relay_by_hash: dict[str, dict] = {}
    if transport_nodes:
        for r in transport_nodes:
            h = _clean_hash(r.get("hash", ""))
            if h:
                relay_by_hash[h] = r

    results = []
    for node in nodes:
        dest_hash = node.get("destination_hash", "")
        clean = _clean_hash(dest_hash)

        path = path_by_hash.get(clean)

        relay = None
        if path:
            via = path.get("via", "")
            if via and via != "0" * len(via):
                relay = relay_by_hash.get(via)

        reach = compute_reachability(node, path, relay)

        results.append({
            "destination_hash": dest_hash,
            "app_name": node.get("app_name", ""),
            "app_data": node.get("app_data_str", ""),
            "hops": path.get("hops") if path else node.get("hops"),
            "last_seen": node.get("last_seen"),
            "announce_count": node.get("announce_count", 0),
            "interface": path.get("interface", "") if path else "",
            "score": reach["score"],
            "label": reach["label"],
            "factors": reach["factors"],
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


# ── Helpers ──────────────────────────────────────────────────────────


def _score_to_label(score: int) -> str:
    """Convert numeric score to a human-readable label."""
    if score >= 80:
        return "High"
    if score >= 60:
        return "Good"
    if score >= 40:
        return "Fair"
    if score >= 20:
        return "Low"
    return "Unlikely"


def _clean_hash(h: str) -> str:
    """Strip angle brackets and spaces from a hex hash."""
    return h.replace("<", "").replace(">", "").replace(" ", "")


def fmt_age(seconds: float) -> str:
    """Format a duration in seconds as a compact human-readable string."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    h = s // 3600
    m = (s % 3600) // 60
    if s < 86400:
        return f"{h}h{m}m" if m else f"{h}h"
    d = s // 86400
    h = (s % 86400) // 3600
    return f"{d}d{h}h" if h else f"{d}d"


def fmt_score_bar(score: int, width: int = 5) -> str:
    """Render a score as a simple bar (e.g. [###--] for 60/100)."""
    filled = round(score / 100 * width)
    return "[" + "#" * filled + "-" * (width - filled) + "]"
