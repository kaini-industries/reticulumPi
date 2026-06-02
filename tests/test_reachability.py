"""Tests for the reachability scoring module."""

from __future__ import annotations

import time

from reticulumpi.reachability import (
    compute_reachability,
    fmt_age,
    fmt_score_bar,
    score_all_nodes,
)


# ---------------------------------------------------------------------------
# fmt_age
# ---------------------------------------------------------------------------


class TestFmtAge:
    def test_seconds(self):
        assert fmt_age(30) == "30s"

    def test_minutes(self):
        assert fmt_age(300) == "5m"

    def test_hours(self):
        assert fmt_age(7200) == "2h"

    def test_hours_minutes(self):
        assert fmt_age(5400) == "1h30m"

    def test_days(self):
        assert fmt_age(86400) == "1d"

    def test_days_hours(self):
        assert fmt_age(90000) == "1d1h"


# ---------------------------------------------------------------------------
# fmt_score_bar
# ---------------------------------------------------------------------------


class TestFmtScoreBar:
    def test_full(self):
        assert fmt_score_bar(100) == "[#####]"

    def test_empty(self):
        assert fmt_score_bar(0) == "[-----]"

    def test_half(self):
        # 50/100 * 5 = 2.5, rounds to 2 or 3 depending on rounding
        bar = fmt_score_bar(50)
        assert bar.startswith("[")
        assert bar.endswith("]")
        assert len(bar) == 7

    def test_custom_width(self):
        bar = fmt_score_bar(100, width=10)
        assert bar == "[##########]"


# ---------------------------------------------------------------------------
# compute_reachability
# ---------------------------------------------------------------------------


class TestComputeReachability:
    def test_no_path_scores_low(self):
        node = {"last_seen": time.time() - 86400 * 2}
        result = compute_reachability(node, path_entry=None)
        assert result["score"] < 20
        assert result["label"] in ("Low", "Unlikely")
        assert result["factors"]["path"]["points"] == 0

    def test_fresh_direct_path_scores_high(self):
        node = {"last_seen": time.time() - 60, "hops": 1}
        path = {"age_s": 60, "hops": 1, "via": "0" * 32}
        result = compute_reachability(node, path)
        assert result["score"] >= 80
        assert result["label"] == "High"

    def test_stale_path_scores_lower(self):
        node = {"last_seen": time.time() - 7200}
        path_fresh = {"age_s": 120, "hops": 2, "via": "0" * 32}
        path_stale = {"age_s": 10000, "hops": 2, "via": "0" * 32}

        fresh = compute_reachability(node, path_fresh)
        stale = compute_reachability(node, path_stale)
        assert fresh["score"] > stale["score"]

    def test_more_hops_scores_lower(self):
        now = time.time()
        node = {"last_seen": now - 300}
        path_near = {"age_s": 100, "hops": 1, "via": "0" * 32}
        path_far = {"age_s": 100, "hops": 8, "via": "0" * 32}

        near = compute_reachability(node, path_near)
        far = compute_reachability(node, path_far)
        assert near["score"] > far["score"]

    def test_healthy_relay_scores_full(self):
        node = {"last_seen": time.time() - 60}
        path = {"age_s": 60, "hops": 3, "via": "ab" * 16}
        relay = {"status": "healthy", "availability_pct": 99.0}
        result = compute_reachability(node, path, relay)
        assert result["factors"]["relay"]["points"] == 20

    def test_down_relay_scores_zero(self):
        node = {"last_seen": time.time() - 60}
        path = {"age_s": 60, "hops": 3, "via": "ab" * 16}
        relay = {"status": "down", "availability_pct": 10.0}
        result = compute_reachability(node, path, relay)
        assert result["factors"]["relay"]["points"] == 0

    def test_degraded_relay_partial(self):
        node = {"last_seen": time.time() - 60}
        path = {"age_s": 60, "hops": 3, "via": "ab" * 16}
        relay = {"status": "degraded", "availability_pct": 60.0}
        result = compute_reachability(node, path, relay)
        assert result["factors"]["relay"]["points"] == 10

    def test_unmonitored_relay(self):
        node = {"last_seen": time.time() - 60}
        path = {"age_s": 60, "hops": 3, "via": "ab" * 16}
        result = compute_reachability(node, path, relay_health=None)
        assert result["factors"]["relay"]["points"] == 12

    def test_direct_path_relay_full(self):
        """Direct paths (via all zeros) get full relay points."""
        node = {"last_seen": time.time() - 60}
        path = {"age_s": 60, "hops": 1, "via": "00" * 16}
        result = compute_reachability(node, path)
        assert result["factors"]["relay"]["points"] == 20

    def test_score_clamped_to_100(self):
        node = {"last_seen": time.time(), "hops": 1}
        path = {"age_s": 1, "hops": 1, "via": "0" * 32}
        result = compute_reachability(node, path)
        assert result["score"] <= 100

    def test_score_clamped_to_0(self):
        node = {}
        result = compute_reachability(node, path_entry=None)
        assert result["score"] >= 0

    def test_factors_all_present(self):
        node = {"last_seen": time.time() - 300}
        path = {"age_s": 300, "hops": 2, "via": "ab" * 16}
        result = compute_reachability(node, path)
        assert "path" in result["factors"]
        assert "freshness" in result["factors"]
        assert "hops" in result["factors"]
        assert "announce" in result["factors"]
        assert "relay" in result["factors"]

    def test_no_last_seen(self):
        """Node with no last_seen still scores (announce factor = 0)."""
        node = {}
        path = {"age_s": 60, "hops": 2, "via": "0" * 32}
        result = compute_reachability(node, path)
        assert result["factors"]["announce"]["points"] == 0
        assert result["score"] > 0  # path + freshness + hops + relay still contribute

    def test_label_brackets(self):
        """Score labels cover the full range."""
        for score, expected in [
            (95, "High"),
            (70, "Good"),
            (50, "Fair"),
            (30, "Low"),
            (5, "Unlikely"),
        ]:
            from reticulumpi.reachability import _score_to_label

            assert _score_to_label(score) == expected


# ---------------------------------------------------------------------------
# score_all_nodes
# ---------------------------------------------------------------------------


class TestScoreAllNodes:
    def test_empty_inputs(self):
        result = score_all_nodes([], [], [])
        assert result == []

    def test_basic_scoring(self):
        now = time.time()
        nodes = [
            {
                "destination_hash": "<aa" + "bb" * 15 + ">",
                "last_seen": now - 60,
                "app_name": "test",
                "app_data_str": "NodeA",
                "hops": 1,
                "announce_count": 5,
            },
            {
                "destination_hash": "<cc" + "dd" * 15 + ">",
                "last_seen": now - 86400,
                "app_name": "test",
                "app_data_str": "NodeB",
                "hops": 8,
                "announce_count": 1,
            },
        ]
        paths = [
            {"hash": "aa" + "bb" * 15, "age_s": 60, "hops": 1, "via": "0" * 32},
        ]
        result = score_all_nodes(nodes, paths)
        assert len(result) == 2
        # Node A has a fresh path, should score higher
        assert result[0]["app_data"] == "NodeA"
        assert result[0]["score"] > result[1]["score"]

    def test_sorted_by_score(self):
        now = time.time()
        nodes = [
            {
                "destination_hash": "aabb",
                "last_seen": now - 86400 * 5,
                "app_name": "t",
                "hops": 10,
                "announce_count": 0,
            },
            {
                "destination_hash": "ccdd",
                "last_seen": now - 30,
                "app_name": "t",
                "hops": 1,
                "announce_count": 10,
            },
        ]
        result = score_all_nodes(nodes, [])
        # ccdd should be first (recent announce)
        assert result[0]["destination_hash"] == "ccdd"

    def test_relay_health_applied(self):
        now = time.time()
        via_hash = "ee" * 16
        nodes = [
            {
                "destination_hash": "aabb",
                "last_seen": now - 60,
                "app_name": "t",
                "hops": 3,
                "announce_count": 1,
            },
        ]
        paths = [
            {"hash": "aabb", "age_s": 60, "hops": 3, "via": via_hash},
        ]
        healthy_relay = [{"hash": via_hash, "status": "healthy", "availability_pct": 99}]
        down_relay = [{"hash": via_hash, "status": "down", "availability_pct": 5}]

        with_healthy = score_all_nodes(nodes, paths, healthy_relay)
        with_down = score_all_nodes(nodes, paths, down_relay)

        assert with_healthy[0]["score"] > with_down[0]["score"]

    def test_output_fields(self):
        now = time.time()
        nodes = [
            {
                "destination_hash": "aabb",
                "last_seen": now - 60,
                "app_name": "TestApp",
                "app_data_str": "MyNode",
                "hops": 2,
                "announce_count": 3,
            },
        ]
        result = score_all_nodes(nodes, [])
        assert len(result) == 1
        entry = result[0]
        assert "destination_hash" in entry
        assert "app_name" in entry
        assert "app_data" in entry
        assert "score" in entry
        assert "label" in entry
        assert "factors" in entry
        assert "hops" in entry
        assert "last_seen" in entry
        assert "announce_count" in entry

    def test_hash_cleaning(self):
        """Hashes with angle brackets and spaces are matched correctly."""
        now = time.time()
        raw_hash = "aabbccdd"
        nodes = [
            {
                "destination_hash": f"<{raw_hash}>",
                "last_seen": now - 60,
                "app_name": "t",
                "hops": 1,
                "announce_count": 1,
            },
        ]
        paths = [
            {"hash": raw_hash, "age_s": 60, "hops": 1, "via": "0" * 32},
        ]
        result = score_all_nodes(nodes, paths)
        # Path should have been matched despite angle brackets
        assert result[0]["factors"]["path"]["points"] == 30
