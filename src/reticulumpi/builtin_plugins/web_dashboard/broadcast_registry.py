"""Plugin-driven broadcast data collection registry.

Replaces the manual per-plugin collection in ``_collect_broadcast_data``
with a declarative protocol: each plugin sets ``broadcast_tier`` and
``broadcast_keys`` class attributes, and optionally overrides
``broadcast_snapshot()``.  The registry iterates plugins by tier,
respects a time budget, and assembles the result dict.
"""

from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# Default values; constructor params override these (sourced from plugin config).
_SLOW_THRESHOLD = 0.2
_TIER1_FACTOR = 0.75 * 0.70
_TIER2_FACTOR = 0.75 * 0.30


class BroadcastRegistry:
    def __init__(
        self,
        metrics_interval: float = 5.0,
        *,
        slow_threshold_ms: float = _SLOW_THRESHOLD * 1000,
        tier1_factor: float = _TIER1_FACTOR,
        tier2_factor: float = _TIER2_FACTOR,
    ) -> None:
        self._slow_threshold = slow_threshold_ms / 1000.0
        self._tier1_budget = metrics_interval * tier1_factor
        self._tier2_budget = metrics_interval * tier2_factor
        # Per-tier instrumentation from the last collect() call, surfaced to
        # the WS health log.  Keyed by tier (0/1/2) -> elapsed ms.
        self.last_tier_ms: dict[int, float] = {0: 0.0, 1: 0.0, 2: 0.0}
        self.last_skipped: int = 0

    def _run_plugin(
        self,
        name: str,
        p: Any,
        cycle_count: int,
        data: dict[str, Any],
    ) -> None:
        t = time.monotonic()
        try:
            result = p.broadcast_snapshot(cycle_count=cycle_count)
        except Exception:
            result = None
        elapsed_ms = (time.monotonic() - t) * 1000
        if elapsed_ms > self._slow_threshold * 1000:
            log.warning(
                "Slow broadcast plugin %s: %.0fms",
                name,
                elapsed_ms,
            )

        if result is None:
            return

        keys = getattr(p, "broadcast_keys", None)
        if keys is None:
            return

        if isinstance(keys, str):
            data[keys] = result
        elif isinstance(keys, (list, tuple)):
            for key in keys:
                if key in result:
                    data[key] = result[key]

    def collect(
        self,
        all_plugins: dict[str, Any],
        cycle_count: int,
        *,
        budget_multiplier: float = 1.0,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {}
        skipped: list[str] = []

        by_tier: dict[int, list[tuple[str, Any]]] = {0: [], 1: [], 2: []}
        for name, p in all_plugins.items():
            tier = getattr(p, "broadcast_tier", None)
            if tier is not None and tier in by_tier:
                by_tier[tier].append((name, p))

        for tier in (0, 1, 2):
            tier_start = time.monotonic()
            if tier == 0:
                cutoff_secs = float("inf")
            elif tier == 1:
                cutoff_secs = self._tier1_budget * budget_multiplier
            else:
                cutoff_secs = self._tier2_budget * budget_multiplier
            # Rotate the iteration start by cycle_count so a tight budget
            # doesn't permanently starve the same plugins each cycle.
            plugins = by_tier[tier]
            n = len(plugins)
            if n:
                start = cycle_count % n
                plugins = plugins[start:] + plugins[:start]
            for name, p in plugins:
                if (time.monotonic() - tier_start) >= cutoff_secs:
                    skipped.append(name)
                    continue
                self._run_plugin(name, p, cycle_count, data)
            self.last_tier_ms[tier] = (time.monotonic() - tier_start) * 1000

        self.last_skipped = len(skipped)
        if skipped:
            log.info(
                "Broadcast budget exceeded — skipped: %s",
                ", ".join(skipped),
            )

        return data
