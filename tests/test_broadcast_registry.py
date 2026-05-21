"""Tests for BroadcastRegistry budget and tier collection."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from reticulumpi.builtin_plugins.web_dashboard.broadcast_registry import (
    BroadcastRegistry,
)


def _make_plugin(tier: int, keys, *, delay: float = 0.0, result=None):
    p = MagicMock()
    p.broadcast_tier = tier
    p.broadcast_keys = keys

    def snapshot(cycle_count=0):
        if delay:
            time.sleep(delay)
        return result if result is not None else {"value": 1}

    p.broadcast_snapshot = snapshot
    return p


class TestPerTierBudget:
    def test_slow_tier0_does_not_starve_tier1(self):
        """Tier-0 taking a long time should not eat into tier-1's budget."""
        registry = BroadcastRegistry(metrics_interval=5.0)
        plugins = {
            "slow_t0": _make_plugin(0, "sys", delay=3.0, result={"cpu": 50}),
            "fast_t1": _make_plugin(1, "mesh", delay=0.0, result={"peers": []}),
        }
        data = registry.collect(plugins, cycle_count=1)
        assert "sys" in data
        assert "mesh" in data

    def test_tier1_budget_is_independent(self):
        """Tier-1 plugins get their own budget window, not leftover from tier-0."""
        registry = BroadcastRegistry(metrics_interval=1.0)
        plugins = {
            "slow_t0": _make_plugin(0, "sys", delay=0.8, result={"cpu": 50}),
            "t1_a": _make_plugin(1, "a", delay=0.0, result={"x": 1}),
            "t1_b": _make_plugin(1, "b", delay=0.0, result={"x": 2}),
        }
        data = registry.collect(plugins, cycle_count=1)
        assert "a" in data
        assert "b" in data

    def test_tier2_gets_own_budget(self):
        """Tier-2 budget starts when tier-2 begins, not from t0."""
        registry = BroadcastRegistry(metrics_interval=1.0)
        plugins = {
            "slow_t0": _make_plugin(0, "sys", delay=0.5, result={"cpu": 50}),
            "slow_t1": _make_plugin(1, "mesh", delay=0.5, result={"p": []}),
            "fast_t2": _make_plugin(2, "gps", delay=0.0, result={"lat": 0}),
        }
        data = registry.collect(plugins, cycle_count=1)
        assert "gps" in data


class TestSkipBudget:
    def test_skip_budget_collects_all_plugins(self):
        """With skip_budget=True, no plugins should be skipped regardless of time."""
        registry = BroadcastRegistry(metrics_interval=0.01)
        plugins = {
            "t0": _make_plugin(0, "sys", delay=0.05, result={"cpu": 50}),
            "t1": _make_plugin(1, "mesh", delay=0.05, result={"peers": []}),
            "t2": _make_plugin(2, "gps", delay=0.05, result={"lat": 0}),
        }
        data = registry.collect(plugins, cycle_count=0, skip_budget=True)
        assert "sys" in data
        assert "mesh" in data
        assert "gps" in data

    def test_normal_budget_can_skip(self):
        """Without skip_budget, a tiny budget should skip slow plugins."""
        registry = BroadcastRegistry(metrics_interval=0.01)
        plugins = {
            "t1_slow": _make_plugin(1, "mesh", delay=0.1, result={"peers": []}),
            "t1_after": _make_plugin(1, "alerts", delay=0.0, result={"count": 0}),
        }
        data = registry.collect(plugins, cycle_count=1)
        assert "mesh" in data
        assert "alerts" not in data
