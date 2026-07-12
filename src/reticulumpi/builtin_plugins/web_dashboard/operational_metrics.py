"""Secret-free aggregate operational metrics for the web dashboard."""

from __future__ import annotations

import threading
from typing import Any


AUTH_ADMISSION_CAPACITY = 4
_MAX_COUNTER = (1 << 63) - 1
_WS_CLOSE_REASONS = (
    "normal",
    "going_away",
    "authentication",
    "capacity",
    "origin",
    "message_too_large",
    "protocol",
    "abnormal",
    "other",
)
_AUTH_OUTCOMES = ("admitted", "saturated", "rejected", "bypassed")
_API_REFRESH_OUTCOMES = ("succeeded", "failed", "skipped", "cancelled")
_TILE_REJECT_REASONS = (
    "unavailable",
    "invalid_request",
    "upstream",
    "invalid_content",
    "oversize",
    "capacity",
    "write_error",
)

_lock = threading.Lock()
_ws_close_counts = {reason: 0 for reason in _WS_CLOSE_REASONS}
_auth_counts = {
    "attempts": 0,
    **{outcome: 0 for outcome in _AUTH_OUTCOMES},
    "work_failures": 0,
    "release_failures": 0,
    "in_flight": 0,
    "peak_in_flight": 0,
}
_api_refresh_counts = {
    "started": 0,
    **{outcome: 0 for outcome in _API_REFRESH_OUTCOMES},
    "pending": 0,
}
_tile_counts = {
    "hits": 0,
    "misses": 0,
    "stored": 0,
    "evictions": 0,
    "rejects": {reason: 0 for reason in _TILE_REJECT_REASONS},
}
_worker_counts = {"broadcast_hung_total": 0}


def _increment(mapping: dict[str, int], key: str) -> None:
    mapping[key] = min(_MAX_COUNTER, mapping[key] + 1)


def _bounded_nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return min(_MAX_COUNTER, max(0, parsed))


def _classify_websocket_close(code: object) -> str:
    if code is None:
        return "abnormal"
    if isinstance(code, bool) or not isinstance(code, int):
        return "other"
    if code == 1000:
        return "normal"
    if code == 1001:
        return "going_away"
    if code == 4001:
        return "authentication"
    if code == 4002:
        return "capacity"
    if code == 4003:
        return "origin"
    if code == 1009:
        return "message_too_large"
    if code in {1002, 1003, 1007, 1008}:
        return "protocol"
    if code == 1006:
        return "abnormal"
    return "other"


def record_websocket_close(code: object) -> None:
    """Count one WebSocket close under a fixed, non-identifying category."""

    reason = _classify_websocket_close(code)
    with _lock:
        _increment(_ws_close_counts, reason)


def record_auth_admission(outcome: str) -> None:
    """Count one bounded-auth admission decision."""

    if outcome not in _AUTH_OUTCOMES:
        raise ValueError("unknown dashboard auth admission outcome")
    with _lock:
        _increment(_auth_counts, "attempts")
        _increment(_auth_counts, outcome)
        if outcome == "admitted":
            _auth_counts["in_flight"] = min(
                AUTH_ADMISSION_CAPACITY,
                _auth_counts["in_flight"] + 1,
            )
            _auth_counts["peak_in_flight"] = max(
                _auth_counts["peak_in_flight"],
                _auth_counts["in_flight"],
            )


def record_auth_completion(*, succeeded: bool) -> None:
    """Release one in-flight auth gauge and count executor failures."""

    with _lock:
        _auth_counts["in_flight"] = max(0, _auth_counts["in_flight"] - 1)
        if not succeeded:
            _increment(_auth_counts, "work_failures")


def record_auth_release_failure() -> None:
    """Count a semaphore release failure without recording request data."""

    with _lock:
        _increment(_auth_counts, "release_failures")


def record_broadcast_worker_hung() -> None:
    """Count a timed-out dashboard snapshot worker without naming its plugin."""

    with _lock:
        _increment(_worker_counts, "broadcast_hung_total")


def record_api_refresh_started() -> None:
    """Count a newly scheduled stale-cache refresh."""

    with _lock:
        _increment(_api_refresh_counts, "started")
        _api_refresh_counts["pending"] = min(
            _MAX_COUNTER,
            _api_refresh_counts["pending"] + 1,
        )


def record_api_refresh_finished(outcome: str) -> None:
    """Complete one cache refresh under a fixed outcome category."""

    if outcome not in _API_REFRESH_OUTCOMES:
        raise ValueError("unknown dashboard API refresh outcome")
    with _lock:
        _api_refresh_counts["pending"] = max(0, _api_refresh_counts["pending"] - 1)
        _increment(_api_refresh_counts, outcome)


def record_tile_hit() -> None:
    with _lock:
        _increment(_tile_counts, "hits")


def record_tile_miss() -> None:
    with _lock:
        _increment(_tile_counts, "misses")


def record_tile_stored() -> None:
    with _lock:
        _increment(_tile_counts, "stored")


def record_tile_eviction() -> None:
    with _lock:
        _increment(_tile_counts, "evictions")


def record_tile_reject(reason: str) -> None:
    """Count one tile rejection using fixed-cardinality reasons only."""

    if reason not in _TILE_REJECT_REASONS:
        raise ValueError("unknown dashboard tile rejection reason")
    with _lock:
        _increment(_tile_counts["rejects"], reason)


def get_dashboard_operational_metrics(plugin: Any | None = None) -> dict[str, Any]:
    """Return one bounded aggregate snapshot for ``ReticulumPiApp`` metrics.

    The snapshot deliberately excludes request labels, paths, cache filenames,
    tokens, client addresses, and upstream URLs.
    """

    config = getattr(plugin, "config", {}) if plugin is not None else {}
    if not isinstance(config, dict):
        config = {}
    tile_config = config.get("tile_proxy", {})
    if not isinstance(tile_config, dict):
        tile_config = {}
    tile_enabled = bool(tile_config.get("enabled", False))
    usage_bytes = _bounded_nonnegative_int(getattr(plugin, "_tile_cache_bytes", 0))
    limit_bytes = _bounded_nonnegative_int(getattr(plugin, "_tile_max_bytes", 0))

    from reticulumpi import __version__

    with _lock:
        rejects = dict(_tile_counts["rejects"])
        return {
            "websocket": {"close_reasons": dict(_ws_close_counts)},
            "auth_admission": {
                "capacity": AUTH_ADMISSION_CAPACITY,
                **dict(_auth_counts),
            },
            "api_cache_refresh": dict(_api_refresh_counts),
            "workers": dict(_worker_counts),
            "tile_cache": {
                "enabled": tile_enabled,
                "usage_bytes": usage_bytes,
                "limit_bytes": limit_bytes,
                "hits": _tile_counts["hits"],
                "misses": _tile_counts["misses"],
                "stored": _tile_counts["stored"],
                "evictions": _tile_counts["evictions"],
                "rejects": sum(rejects.values()),
                "reject_reasons": rejects,
            },
            "service_worker": {"version": str(__version__)[:128]},
        }


def _reset_dashboard_operational_metrics() -> None:
    """Reset process counters for isolated tests only."""

    with _lock:
        for reason in _WS_CLOSE_REASONS:
            _ws_close_counts[reason] = 0
        for key in _auth_counts:
            _auth_counts[key] = 0
        for key in _api_refresh_counts:
            _api_refresh_counts[key] = 0
        for key in _worker_counts:
            _worker_counts[key] = 0
        for key in ("hits", "misses", "stored", "evictions"):
            _tile_counts[key] = 0
        for reason in _TILE_REJECT_REASONS:
            _tile_counts["rejects"][reason] = 0
