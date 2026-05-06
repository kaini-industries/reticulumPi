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

_SLOW_THRESHOLD = 0.2


class BroadcastRegistry:

    def __init__(self, metrics_interval: float = 5.0) -> None:
        self._budget = metrics_interval * 0.75
        self._tier1_budget = self._budget * 0.70

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
        elapsed = time.monotonic() - t
        if elapsed > _SLOW_THRESHOLD:
            log.warning(
                "Slow broadcast plugin %s: %.0fms", name, elapsed * 1000,
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
    ) -> dict[str, Any]:
        t0 = time.monotonic()
        data: dict[str, Any] = {}
        skipped: list[str] = []

        by_tier: dict[int, list[tuple[str, Any]]] = {0: [], 1: [], 2: []}
        for name, p in all_plugins.items():
            tier = getattr(p, "broadcast_tier", None)
            if tier is not None and tier in by_tier:
                by_tier[tier].append((name, p))

        for tier in (0, 1, 2):
            cutoff = (
                float("inf") if tier == 0
                else self._tier1_budget if tier == 1
                else self._budget
            )
            for name, p in by_tier[tier]:
                if (time.monotonic() - t0) >= cutoff:
                    skipped.append(name)
                    continue
                self._run_plugin(name, p, cycle_count, data)

        if skipped:
            log.info(
                "Broadcast budget exceeded — skipped: %s", ", ".join(skipped),
            )

        return data
