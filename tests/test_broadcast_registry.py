"""Tests for BroadcastRegistry budget and tier collection."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

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
        registry = BroadcastRegistry(metrics_interval=5.0, callback_timeout_ms=4000)
        plugins = {
            "slow_t0": _make_plugin(0, "sys", delay=3.0, result={"cpu": 50}),
            "fast_t1": _make_plugin(1, "mesh", delay=0.0, result={"peers": []}),
        }
        data = registry.collect(plugins, cycle_count=1)
        assert "sys" in data
        assert "mesh" in data

    def test_tier1_budget_is_independent(self):
        """Tier-1 plugins get their own budget window, not leftover from tier-0."""
        registry = BroadcastRegistry(metrics_interval=1.0, callback_timeout_ms=1000)
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
        registry = BroadcastRegistry(metrics_interval=1.0, callback_timeout_ms=1000)
        plugins = {
            "slow_t0": _make_plugin(0, "sys", delay=0.5, result={"cpu": 50}),
            "slow_t1": _make_plugin(1, "mesh", delay=0.5, result={"p": []}),
            "fast_t2": _make_plugin(2, "gps", delay=0.0, result={"lat": 0}),
        }
        data = registry.collect(plugins, cycle_count=1)
        assert "gps" in data


class TestBudgetMultiplier:
    def test_high_multiplier_collects_all_plugins(self):
        """With a large budget_multiplier, no plugins should be skipped."""
        registry = BroadcastRegistry(metrics_interval=0.01)
        plugins = {
            "t0": _make_plugin(0, "sys", delay=0.05, result={"cpu": 50}),
            "t1": _make_plugin(1, "mesh", delay=0.05, result={"peers": []}),
            "t2": _make_plugin(2, "gps", delay=0.05, result={"lat": 0}),
        }
        data = registry.collect(plugins, cycle_count=0, budget_multiplier=100.0)
        assert "sys" in data
        assert "mesh" in data
        assert "gps" in data

    def test_normal_budget_can_skip(self):
        """With default budget multiplier, a tiny budget should skip slow plugins."""
        registry = BroadcastRegistry(metrics_interval=0.01)
        plugins = {
            "t1_slow": _make_plugin(1, "mesh", delay=0.1, result={"peers": []}),
            "t1_after": _make_plugin(1, "alerts", delay=0.0, result={"count": 0}),
        }
        # cycle_count=0 keeps the rotation start index at 0 so iteration order
        # is the dict order (slow plugin first), and the fast one is skipped.
        data = registry.collect(plugins, cycle_count=0)
        assert "mesh" in data
        assert "alerts" not in data


class TestFairnessRotation:
    def test_no_permanent_starvation_across_cycles(self):
        """With N tier-2 plugins and a budget fitting only one, every plugin
        should run at least once across N cycles thanks to start-index rotation.
        """
        n = 4
        # Budget fits roughly one plugin per cycle (each ~50ms; tier2 budget
        # ~= 0.06 * 0.225 = ~13.5ms, so only the first plugin runs).
        registry = BroadcastRegistry(metrics_interval=0.06)
        keys = [f"k{i}" for i in range(n)]
        plugins = {
            f"t2_{i}": _make_plugin(2, keys[i], delay=0.05, result={keys[i]: i}) for i in range(n)
        }

        ran_keys: set[str] = set()
        for cycle in range(n):
            data = registry.collect(plugins, cycle_count=cycle)
            ran_keys.update(k for k in keys if k in data)

        # Every plugin must have produced output at least once across the cycles.
        assert ran_keys == set(keys)

    def test_rotation_start_index_advances(self):
        """The first plugin run in a tier should advance with cycle_count."""
        n = 3
        # Large enough budget so the first plugin always runs (others may be
        # skipped depending on remaining time); we only assert the head order.
        registry = BroadcastRegistry(metrics_interval=10.0)
        order: list[str] = []

        def make_recording(idx):
            p = _make_plugin(2, f"k{idx}", delay=0.0, result={f"k{idx}": idx})
            orig = p.broadcast_snapshot

            def snap(cycle_count=0):
                order.append(f"t2_{idx}")
                return orig(cycle_count=cycle_count)

            p.broadcast_snapshot = snap
            return p

        plugins = {f"t2_{i}": make_recording(i) for i in range(n)}

        for cycle in range(n):
            order.clear()
            registry.collect(plugins, cycle_count=cycle)
            # The first plugin invoked this cycle is rotated by cycle % n.
            assert order[0] == f"t2_{cycle % n}"


class TestConfigurableParams:
    def test_slow_threshold_override_changes_warning(self, caplog):
        """A higher slow_threshold_ms suppresses the slow-plugin warning."""
        plugins = {"t1": _make_plugin(1, "mesh", delay=0.05, result={"x": 1})}

        # Default threshold (200ms) — 50ms delay does NOT warn.
        default_reg = BroadcastRegistry(metrics_interval=5.0)
        with caplog.at_level("WARNING"):
            default_reg.collect(plugins, cycle_count=0)
        assert not any("Slow broadcast plugin" in r.message for r in caplog.records)

        caplog.clear()
        # Very low threshold (10ms) — 50ms delay DOES warn.
        low_reg = BroadcastRegistry(metrics_interval=5.0, slow_threshold_ms=10.0)
        with caplog.at_level("WARNING"):
            low_reg.collect(plugins, cycle_count=0)
        assert any("Slow broadcast plugin" in r.message for r in caplog.records)

    def test_tier_factor_overrides_change_budgets(self):
        """Constructor tier factors override the hardcoded defaults."""
        reg = BroadcastRegistry(
            metrics_interval=10.0,
            tier1_factor=0.5,
            tier2_factor=0.25,
        )
        assert reg._tier1_budget == pytest.approx(10.0 * 0.5)
        assert reg._tier2_budget == pytest.approx(10.0 * 0.25)

        default_reg = BroadcastRegistry(metrics_interval=10.0)
        assert default_reg._tier1_budget == pytest.approx(10.0 * (0.75 * 0.70))
        assert default_reg._tier2_budget == pytest.approx(10.0 * (0.75 * 0.30))
        # Overridden budgets differ from the defaults.
        assert reg._tier1_budget != default_reg._tier1_budget
        assert reg._tier2_budget != default_reg._tier2_budget


class TestInstrumentation:
    def test_records_per_tier_elapsed_and_skipped(self):
        registry = BroadcastRegistry(metrics_interval=0.01)
        plugins = {
            "t0": _make_plugin(0, "sys", delay=0.0, result={"cpu": 1}),
            "t1_slow": _make_plugin(1, "mesh", delay=0.05, result={"peers": []}),
            "t1_after": _make_plugin(1, "alerts", delay=0.0, result={"count": 0}),
        }
        registry.collect(plugins, cycle_count=0)
        # Per-tier elapsed populated for all three tiers.
        assert set(registry.last_tier_ms) == {0, 1, 2}
        assert registry.last_tier_ms[1] >= 0.0
        # The fast tier-1 plugin after the slow one was skipped.
        assert registry.last_skipped >= 1

    def test_skipped_zero_when_all_fit(self):
        registry = BroadcastRegistry(metrics_interval=5.0)
        plugins = {
            "t1": _make_plugin(1, "mesh", delay=0.0, result={"peers": []}),
            "t2": _make_plugin(2, "gps", delay=0.0, result={"lat": 0}),
        }
        registry.collect(plugins, cycle_count=0)
        assert registry.last_skipped == 0


def test_hung_callback_is_bounded_and_disabled_after_first_timeout(caplog):
    from reticulumpi.builtin_plugins.web_dashboard.operational_metrics import (
        get_dashboard_operational_metrics,
    )

    release = __import__("threading").Event()
    plugin = _make_plugin(0, "sys")
    plugin.broadcast_snapshot = MagicMock(side_effect=lambda **_kwargs: release.wait(timeout=2))
    registry = BroadcastRegistry(metrics_interval=5.0, callback_timeout_ms=20)
    before = get_dashboard_operational_metrics()["workers"]["broadcast_hung_total"]

    started = time.monotonic()
    with caplog.at_level("WARNING"):
        assert registry.collect({"hung": plugin}, cycle_count=0) == {}
        assert registry.collect({"hung": plugin}, cycle_count=1) == {}

    assert time.monotonic() - started < 0.2
    assert plugin.broadcast_snapshot.call_count == 1
    assert "disabled until restart" in caplog.text
    assert get_dashboard_operational_metrics()["workers"]["broadcast_hung_total"] == before + 1
    release.set()
