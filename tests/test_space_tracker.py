"""Tests for the space_tracker plugin's rate limiter and TLE parser.

These tests deliberately focus on the quota-enforcement surface — that's
the part the user asked to be bullet-proof.  Network I/O is not exercised
here (it's gated behind the limiter anyway, and mocking urllib cleanly
belongs in a separate integration test).
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi.builtin_plugins.space_tracker import (
    _RateLimiter,
    _bisect_horizon,
    _find_passes,
    _gmst_rad,
    _observer_look_angles,
    _parse_tle_block,
    _sat_el_az_at,
    _sgp4_available,
    _skyfield_available,
    _teme_to_geodetic,
)


# ---------------------------------------------------------------------------
# _RateLimiter
# ---------------------------------------------------------------------------
class TestRateLimiter:
    def test_fresh_limiter_permits_first_request(self):
        rl = _RateLimiter("test", min_interval_s=60.0)
        assert rl.can_request() is True

    def test_blocks_within_min_interval(self):
        rl = _RateLimiter("test", min_interval_s=3600.0)
        rl.record_attempt()
        assert rl.can_request() is False
        # next_allowed_at should be ~ now + 3600s
        assert rl.next_allowed_at() > time.time() + 3500

    def test_permits_after_min_interval_elapses(self):
        rl = _RateLimiter("test", min_interval_s=1.0)
        rl.record_attempt()
        rl.record_success()
        # Simulate time passing by mutating last_request_ts directly
        rl._last_request_ts = time.time() - 5.0
        assert rl.can_request() is True

    def test_record_attempt_counts_even_on_failure(self):
        """A broken endpoint must not get retried on every tick."""
        rl = _RateLimiter("test", min_interval_s=60.0)
        rl.record_attempt()
        rl.record_failure()
        assert rl.can_request() is False

    def test_hourly_cap_enforced(self):
        rl = _RateLimiter("test", min_interval_s=0.0, max_per_hour=3)
        for _ in range(3):
            assert rl.can_request() is True
            rl.record_attempt()
            rl.record_success()
        # Fourth request blocked even though min_interval is 0
        assert rl.can_request() is False

    def test_hourly_cap_window_slides(self):
        rl = _RateLimiter("test", min_interval_s=0.0, max_per_hour=2)
        # Record two requests 2 hours ago
        now = time.time()
        rl._recent.extend([now - 7300, now - 7200])
        rl._last_request_ts = now - 7200
        # Window trim should discard both and allow
        assert rl.can_request() is True

    def test_backoff_grows_exponentially(self):
        rl = _RateLimiter(
            "test",
            min_interval_s=60.0,
            backoff_base_s=3600.0,
            backoff_cap_s=86400.0,
        )
        # After one failure, backoff = 3600s
        rl.record_failure()
        assert rl._current_min_interval() == 3600.0
        # After two failures, backoff = 7200s
        rl.record_failure()
        assert rl._current_min_interval() == 7200.0
        # After 10 failures, capped at 86400s
        for _ in range(10):
            rl.record_failure()
        assert rl._current_min_interval() == 86400.0

    def test_backoff_resets_on_success(self):
        rl = _RateLimiter("test", min_interval_s=60.0)
        rl.record_failure()
        rl.record_failure()
        assert rl._failures == 2
        rl.record_success()
        assert rl._failures == 0

    def test_failure_counter_capped(self):
        """Guard against integer overflow from a pathologically broken endpoint."""
        rl = _RateLimiter("test", min_interval_s=60.0)
        for _ in range(1000):
            rl.record_failure()
        assert rl._failures <= 16

    def test_state_roundtrip(self):
        """Persistence: save + load preserves quota state."""
        rl = _RateLimiter("test", min_interval_s=60.0, max_per_hour=5)
        rl.record_attempt()
        rl.record_success()
        rl.record_failure()
        state = rl.to_dict()

        rl2 = _RateLimiter("test", min_interval_s=60.0, max_per_hour=5)
        rl2.load_dict(state)
        assert rl2._last_request_ts == rl._last_request_ts
        assert rl2._failures == rl._failures
        assert list(rl2._recent) == list(rl._recent)

    def test_state_json_serialisable(self):
        """State must survive a JSON round-trip for disk persistence."""
        rl = _RateLimiter("test", min_interval_s=60.0)
        rl.record_attempt()
        rl.record_success()
        rl.record_failure()
        encoded = json.dumps(rl.to_dict())
        rl2 = _RateLimiter("test", min_interval_s=60.0)
        rl2.load_dict(json.loads(encoded))
        assert rl2._failures == rl._failures

    def test_load_corrupt_state_does_not_crash(self):
        rl = _RateLimiter("test", min_interval_s=60.0)
        # Missing keys, wrong types — should tolerate quietly
        rl.load_dict({})
        rl.load_dict({"last_request_ts": 0, "failures": 0, "recent": []})

    def test_status_report(self):
        rl = _RateLimiter("test", min_interval_s=60.0, max_per_hour=5)
        rl.record_attempt()
        rl.record_success()
        status = rl.status()
        assert status["name"] == "test"
        assert status["requests_last_hour"] == 1
        assert status["max_per_hour"] == 5
        assert status["next_allowed_at"] > time.time()

    def test_persistence_survives_restart_loop(self):
        """A process that keeps crashing must not hammer the upstream."""
        rl = _RateLimiter("test", min_interval_s=3600.0)
        rl.record_attempt()
        state = rl.to_dict()
        # Simulate crash + restart: a fresh limiter loads the prior state
        rl_restarted = _RateLimiter("test", min_interval_s=3600.0)
        rl_restarted.load_dict(state)
        assert rl_restarted.can_request() is False


# ---------------------------------------------------------------------------
# _parse_tle_block
# ---------------------------------------------------------------------------
# A real TLE triplet (ISS, fake epoch — values are syntactically valid).
_SAMPLE_TLE = """\
ISS (ZARYA)
1 25544U 98067A   24001.50000000  .00016717  00000-0  10270-3 0  9008
2 25544  51.6412  23.4567 0004192  45.1234 315.6789 15.50000000123456
NOAA 18
1 28654U 05018A   24001.50000000  .00000123  00000-0  85432-4 0  9991
2 28654  99.1234 123.4567 0013456  78.9012 281.3456 14.12000000123456
"""


class TestTleParser:
    def test_parses_two_satellites(self):
        sats = _parse_tle_block(_SAMPLE_TLE)
        assert len(sats) == 2
        assert sats[0]["name"] == "ISS (ZARYA)"
        assert sats[0]["l1"].startswith("1 25544")
        assert sats[0]["l2"].startswith("2 25544")
        assert sats[1]["name"] == "NOAA 18"

    def test_empty_input(self):
        assert _parse_tle_block("") == []

    def test_malformed_lines_skipped(self):
        junk = "not a tle\ngarbage\nmore garbage\n"
        assert _parse_tle_block(junk) == []

    def test_whitespace_only_lines_ignored(self):
        text = "\n\n" + _SAMPLE_TLE + "\n\n"
        sats = _parse_tle_block(text)
        assert len(sats) == 2

    def test_partial_trailing_triplet_dropped(self):
        text = _SAMPLE_TLE + "\nORPHAN SAT\n1 99999U 00000A"
        sats = _parse_tle_block(text)
        assert len(sats) == 2   # trailing partial not included


# ---------------------------------------------------------------------------
# Optional-dependency helpers
# ---------------------------------------------------------------------------
class TestPropagationMath:
    """Sanity checks on the hand-rolled astrodynamics helpers."""

    def test_gmst_at_j2000(self):
        import math
        # Known: GMST at J2000 (2451545.0) ≈ 280.46 degrees
        g = math.degrees(_gmst_rad(2451545.0))
        assert 280.0 < g < 281.0

    def test_teme_to_geodetic_equator(self):
        # ECEF vector purely on +X axis, GMST=0 → lat=0, lon=0
        lat, lon, alt = _teme_to_geodetic((6778.137, 0.0, 0.0), 0.0)
        assert abs(lat) < 0.01
        assert abs(lon) < 0.01
        assert 399 < alt < 401  # ~400 km

    def test_teme_to_geodetic_pole(self):
        # Z-axis vector → lat ≈ 90
        lat, _lon, _alt = _teme_to_geodetic((0.0, 0.0, 6778.137), 0.0)
        assert 89 < lat < 91

    def test_look_angle_overhead(self):
        # Satellite directly above observer → elevation 90
        _az, el = _observer_look_angles(0, 0, 0, 0, 0, 400)
        assert 89.5 < el <= 90.0

    def test_look_angle_due_north(self):
        # Satellite due north at same-equator observer → az ≈ 0
        az, el = _observer_look_angles(0, 0, 0, 10, 0, 400)
        assert az < 1.0 or az > 359.0
        assert el > 0

    def test_look_angle_due_east(self):
        az, _el = _observer_look_angles(0, 0, 0, 0, 10, 400)
        assert 89 < az < 91

    def test_look_angle_below_horizon(self):
        # Far-side satellite → negative elevation
        _az, el = _observer_look_angles(0, 0, 0, 0, 180, 400)
        assert el < 0


class TestOptionalDeps:
    def test_sgp4_available_returns_bool(self):
        assert isinstance(_sgp4_available(), bool)

    def test_skyfield_available_returns_bool(self):
        assert isinstance(_skyfield_available(), bool)


# ---------------------------------------------------------------------------
# Pass prediction — deterministic tests with a synthetic elevation curve
# (no sgp4 required).  The real-TLE test below is gated on sgp4 import.
# ---------------------------------------------------------------------------
def _sine_el_fn(period_s=600.0, amplitude=30.0, az=90.0, t_offset=0.0):
    """Return an el_fn whose elevation traces a sine wave.

    This gives deterministic AOS/LOS at integer multiples of period/2,
    with peak elevation == amplitude at quarter-period offsets.
    """
    import math

    def fn(t):
        phase = 2.0 * math.pi * (t - t_offset) / period_s
        return (amplitude * math.sin(phase), az)
    return fn


class TestBisectHorizon:
    def test_converges_within_tolerance(self):
        fn = _sine_el_fn(period_s=600.0, amplitude=30.0)
        # Known zero crossing at t=0 and t=300 (period/2).  Bracket around 300.
        ts = _bisect_horizon(fn, 270.0, 330.0, el_lo=fn(270.0)[0], el_hi=fn(330.0)[0], tol_s=1.0)
        assert abs(ts - 300.0) <= 1.0

    def test_narrows_from_wide_bracket(self):
        fn = _sine_el_fn(period_s=600.0, amplitude=30.0)
        ts = _bisect_horizon(fn, 1.0, 299.0, el_lo=fn(1.0)[0], el_hi=fn(299.0)[0], tol_s=1.0)
        # Both ends of this bracket are positive — no crossing.  Bisection
        # still terminates without exploding.
        assert 1.0 <= ts <= 299.0

    def test_handles_rising_crossing(self):
        # el < 0 at t=270 (near period, approaching zero from below) and
        # el > 0 at t=330 (past period, rising).
        fn = _sine_el_fn(period_s=600.0, amplitude=30.0)
        # Shift so [270, 330] brackets the rising zero at t=600.
        # fn at t=570 is negative (sin of 5.969 rad ≈ -0.31), at t=630 positive
        ts = _bisect_horizon(fn, 570.0, 630.0, el_lo=fn(570.0)[0], el_hi=fn(630.0)[0])
        assert abs(ts - 600.0) <= 1.0

    def test_gap_aborts_cleanly(self):
        # el_fn that returns None partway — shouldn't infinite-loop
        calls = {"n": 0}

        def fn(t):
            calls["n"] += 1
            if calls["n"] > 2:
                return None
            return (-1.0 if t < 10.0 else 1.0, 0.0)

        ts = _bisect_horizon(fn, 0.0, 20.0, el_lo=-1.0, el_hi=1.0)
        assert 0.0 <= ts <= 20.0


class TestFindPasses:
    def test_finds_passes_in_range(self):
        # Over one full period (600s), a sine crosses horizon once rising
        # and once setting -> exactly one pass.  Over 3 periods -> 3 passes.
        fn = _sine_el_fn(period_s=600.0, amplitude=45.0, t_offset=50.0)
        passes = _find_passes(fn, t_start=0.0, t_end=1800.0, min_el_deg=0.0)
        assert len(passes) >= 3

    def test_filters_by_min_elevation(self):
        fn = _sine_el_fn(period_s=600.0, amplitude=20.0)
        high = _find_passes(fn, t_start=0.0, t_end=1800.0, min_el_deg=0.0)
        low = _find_passes(fn, t_start=0.0, t_end=1800.0, min_el_deg=25.0)
        assert len(high) >= 3
        assert len(low) == 0  # peak is 20° < threshold 25°

    def test_max_passes_cap_respected(self):
        fn = _sine_el_fn(period_s=600.0, amplitude=45.0)
        passes = _find_passes(
            fn, t_start=0.0, t_end=100000.0, min_el_deg=0.0, max_passes=5,
        )
        assert len(passes) <= 5

    def test_pass_fields_populated(self):
        fn = _sine_el_fn(period_s=600.0, amplitude=45.0, az=135.0)
        passes = _find_passes(fn, t_start=0.0, t_end=1800.0, min_el_deg=0.0)
        assert passes
        p = passes[0]
        assert "aos_ts" in p and "los_ts" in p
        assert p["los_ts"] > p["aos_ts"]
        assert p["duration_s"] == pytest.approx(p["los_ts"] - p["aos_ts"])
        assert 0 <= p["max_el"] <= 45.1
        assert p["aos_az"] == 135.0
        assert p["los_az"] == 135.0

    def test_starting_inside_a_pass_truncates_aos_to_t_start(self):
        # Start at a time where elevation is already positive
        fn = _sine_el_fn(period_s=600.0, amplitude=45.0)
        # At t=100, sin(2π·100/600) ≈ sin(1.047) ≈ 0.866 → el ≈ 39°
        passes = _find_passes(fn, t_start=100.0, t_end=500.0, min_el_deg=0.0)
        assert passes
        assert passes[0]["aos_ts"] == 100.0

    def test_ending_mid_pass_truncates_los(self):
        # Pick a t_end inside a pass — the last entry should be marked truncated
        fn = _sine_el_fn(period_s=600.0, amplitude=45.0)
        passes = _find_passes(fn, t_start=0.0, t_end=200.0, min_el_deg=0.0)
        # Peak is at t=150, still inside the pass at t=200
        assert passes
        last = passes[-1]
        assert last.get("truncated") is True
        assert last["los_ts"] == 200.0
        assert last["los_az"] is None

    def test_empty_when_never_rises(self):
        def always_below(t):
            return (-5.0, 0.0)
        passes = _find_passes(always_below, 0.0, 3600.0, min_el_deg=0.0)
        assert passes == []

    def test_handles_propagation_gap(self):
        # el_fn returns None inside the [t_start, t_end] window
        def gappy(t):
            if 200.0 <= t <= 400.0:
                return None
            # A proper pass that would straddle the gap without it
            if 100.0 <= t <= 500.0:
                return (15.0, 0.0)
            return (-5.0, 0.0)

        # Should not raise and should return a list (possibly empty)
        passes = _find_passes(gappy, 0.0, 600.0, min_el_deg=0.0)
        assert isinstance(passes, list)


@pytest.mark.skipif(not _sgp4_available(), reason="sgp4 not installed")
class TestSgp4IntegratedPasses:
    """End-to-end pass detection against a real SGP4 propagator.

    Uses an ISS TLE whose epoch is 2024-01-01 12:00 UTC.  Pass prediction
    runs starting from the TLE epoch so propagation error stays small.
    """

    def _iss_satrec(self):
        from sgp4.api import Satrec

        # Syntactic ISS TLE at epoch 2024-001.5 (day 1, 12:00 UTC)
        l1 = "1 25544U 98067A   24001.50000000  .00016717  00000-0  10270-3 0  9008"
        l2 = "2 25544  51.6412  23.4567 0004192  45.1234 315.6789 15.50000000123456"
        return Satrec.twoline2rv(l1, l2)

    def _epoch_start(self):
        import datetime as _dt
        # 2024-01-01 12:00 UTC
        return _dt.datetime(2024, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc).timestamp()

    def test_iss_produces_passes_over_24h(self):
        from sgp4.api import jday
        satrec = self._iss_satrec()
        observer = {"lat": 40.0, "lon": 0.0, "elev_m": 0.0}

        t_start = self._epoch_start()
        t_end = t_start + 24 * 3600

        def el_fn(t):
            return _sat_el_az_at(satrec, jday, observer, t)

        passes = _find_passes(el_fn, t_start, t_end, min_el_deg=0.0)
        # 24h at 40°N should yield several ISS passes
        assert len(passes) >= 3
        for p in passes:
            assert p["aos_ts"] < p["los_ts"]
            assert p["max_el"] >= 0.0

    def test_iss_min_elevation_filter(self):
        from sgp4.api import jday
        satrec = self._iss_satrec()
        observer = {"lat": 40.0, "lon": 0.0, "elev_m": 0.0}
        t_start = self._epoch_start()
        t_end = t_start + 24 * 3600

        def el_fn(t):
            return _sat_el_az_at(satrec, jday, observer, t)

        all_passes = _find_passes(el_fn, t_start, t_end, min_el_deg=0.0)
        high_passes = _find_passes(el_fn, t_start, t_end, min_el_deg=60.0)
        assert len(high_passes) <= len(all_passes)

    def test_sat_el_az_at_returns_numeric(self):
        from sgp4.api import jday
        satrec = self._iss_satrec()
        observer = {"lat": 40.0, "lon": 0.0, "elev_m": 0.0}
        result = _sat_el_az_at(satrec, jday, observer, self._epoch_start())
        assert result is not None
        el, az = result
        assert -90.0 <= el <= 90.0
        assert 0.0 <= az < 360.0


# ---------------------------------------------------------------------------
# Plugin config validation — ensures the floors are actually enforced
# ---------------------------------------------------------------------------
@pytest.fixture
def mock_app(tmp_path):
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.identity.hash = b"\x02" * 16
    from reticulumpi.event_bus import EventBus
    app.event_bus = EventBus()
    app.plugins = {}
    app.node_name = "TestNode"
    return app


class TestValidateConfig:
    def test_rejects_tle_refresh_too_low(self, mock_app):
        from reticulumpi.builtin_plugins.space_tracker import SpaceTrackerPlugin
        with pytest.raises(ValueError, match="tle_refresh_hours"):
            SpaceTrackerPlugin(mock_app, {"tle_refresh_hours": 1})

    def test_rejects_launch_interval_too_low(self, mock_app):
        from reticulumpi.builtin_plugins.space_tracker import SpaceTrackerPlugin
        with pytest.raises(ValueError, match="launches.poll_interval_minutes"):
            SpaceTrackerPlugin(
                mock_app,
                {"launches": {"enabled": True, "poll_interval_minutes": 5}},
            )

    def test_rejects_weather_interval_too_low(self, mock_app):
        from reticulumpi.builtin_plugins.space_tracker import SpaceTrackerPlugin
        with pytest.raises(ValueError, match="space_weather.poll_interval_minutes"):
            SpaceTrackerPlugin(
                mock_app,
                {"space_weather": {"enabled": True, "poll_interval_minutes": 1}},
            )

    def test_accepts_valid_config(self, mock_app):
        from reticulumpi.builtin_plugins.space_tracker import SpaceTrackerPlugin
        plugin = SpaceTrackerPlugin(
            mock_app,
            {
                "tle_refresh_hours": 24,
                "launches": {"poll_interval_minutes": 30},
                "space_weather": {"poll_interval_minutes": 15},
            },
        )
        assert plugin is not None

    def test_rejects_non_list_groups(self, mock_app):
        from reticulumpi.builtin_plugins.space_tracker import SpaceTrackerPlugin
        with pytest.raises(ValueError, match="celestrak_groups"):
            SpaceTrackerPlugin(mock_app, {"celestrak_groups": "stations"})

    def test_rejects_pass_min_elevation_out_of_range(self, mock_app):
        from reticulumpi.builtin_plugins.space_tracker import SpaceTrackerPlugin
        with pytest.raises(ValueError, match="min_elevation_deg"):
            SpaceTrackerPlugin(
                mock_app,
                {"passes": {"enabled": True, "min_elevation_deg": 120}},
            )
        with pytest.raises(ValueError, match="min_elevation_deg"):
            SpaceTrackerPlugin(
                mock_app,
                {"passes": {"enabled": True, "min_elevation_deg": -5}},
            )

    def test_rejects_pass_lookahead_out_of_range(self, mock_app):
        from reticulumpi.builtin_plugins.space_tracker import SpaceTrackerPlugin
        with pytest.raises(ValueError, match="lookahead_hours"):
            SpaceTrackerPlugin(
                mock_app,
                {"passes": {"enabled": True, "lookahead_hours": 0}},
            )
        with pytest.raises(ValueError, match="lookahead_hours"):
            SpaceTrackerPlugin(
                mock_app,
                {"passes": {"enabled": True, "lookahead_hours": 200}},
            )

    def test_rejects_non_list_watchlist(self, mock_app):
        from reticulumpi.builtin_plugins.space_tracker import SpaceTrackerPlugin
        with pytest.raises(ValueError, match="watchlist"):
            SpaceTrackerPlugin(
                mock_app,
                {"passes": {"enabled": True, "watchlist": "ISS (ZARYA)"}},
            )

    def test_accepts_valid_passes_config(self, mock_app):
        from reticulumpi.builtin_plugins.space_tracker import SpaceTrackerPlugin
        plugin = SpaceTrackerPlugin(
            mock_app,
            {
                "passes": {
                    "enabled": True,
                    "min_elevation_deg": 10,
                    "lookahead_hours": 24,
                    "watchlist": ["ISS (ZARYA)", "NOAA 18"],
                },
            },
        )
        assert plugin is not None

    def test_passes_disabled_skips_validation(self, mock_app):
        """When passes.enabled is false, out-of-range values don't error."""
        from reticulumpi.builtin_plugins.space_tracker import SpaceTrackerPlugin
        plugin = SpaceTrackerPlugin(
            mock_app,
            {"passes": {"enabled": False, "min_elevation_deg": 999}},
        )
        assert plugin is not None


# ---------------------------------------------------------------------------
# Plugin lifecycle — does not hit the network (all threads sleep first, and
# the limiters already have a cooldown set, so no fetch fires in time).
# ---------------------------------------------------------------------------
class TestPluginLifecycle:
    def test_start_stop_clean(self, mock_app, tmp_path):
        from reticulumpi.builtin_plugins.space_tracker import SpaceTrackerPlugin
        cfg = {
            "cache_dir": str(tmp_path),
            "launches": {"enabled": False},
            "space_weather": {"enabled": False},
            "propagation": {"enabled": False},
        }
        plugin = SpaceTrackerPlugin(mock_app, cfg)
        plugin.start()
        assert plugin._active is True
        plugin.stop()
        assert plugin._active is False

    def test_state_persisted_on_stop(self, mock_app, tmp_path):
        from reticulumpi.builtin_plugins.space_tracker import SpaceTrackerPlugin
        cfg = {
            "cache_dir": str(tmp_path),
            "launches": {"enabled": False},
            "space_weather": {"enabled": False},
            "propagation": {"enabled": False},
        }
        plugin = SpaceTrackerPlugin(mock_app, cfg)
        plugin.start()
        # Force a limiter into a non-default state
        plugin._limiters["celestrak"].record_attempt()
        plugin._limiters["celestrak"].record_failure()
        plugin.stop()
        state_file = tmp_path / "rate_state.json"
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["celestrak"]["failures"] == 1

    def test_state_loaded_on_start(self, mock_app, tmp_path):
        from reticulumpi.builtin_plugins.space_tracker import SpaceTrackerPlugin
        # Pre-seed a state file that marks celestrak as blocked
        state_file = tmp_path / "rate_state.json"
        state_file.write_text(json.dumps({
            "celestrak": {
                "last_request_ts": time.time(),
                "failures": 0,
                "recent": [],
            }
        }))
        cfg = {
            "cache_dir": str(tmp_path),
            "launches": {"enabled": False},
            "space_weather": {"enabled": False},
            "propagation": {"enabled": False},
        }
        plugin = SpaceTrackerPlugin(mock_app, cfg)
        plugin.start()
        try:
            assert plugin._limiters["celestrak"].can_request() is False
        finally:
            plugin.stop()

    def test_get_status_reports_limiters(self, mock_app, tmp_path):
        from reticulumpi.builtin_plugins.space_tracker import SpaceTrackerPlugin
        cfg = {
            "cache_dir": str(tmp_path),
            "launches": {"enabled": False},
            "space_weather": {"enabled": False},
            "propagation": {"enabled": False},
        }
        plugin = SpaceTrackerPlugin(mock_app, cfg)
        plugin.start()
        try:
            status = plugin.get_status()
            assert "rate_limiters" in status
            assert "celestrak" in status["rate_limiters"]
            assert "sgp4_available" in status
        finally:
            plugin.stop()

    def test_http_not_called_when_limiter_blocks(self, mock_app, tmp_path):
        """When a limiter is cool-down locked, _fetch_group must not call http.get."""
        from reticulumpi.builtin_plugins.space_tracker import SpaceTrackerPlugin
        cfg = {
            "cache_dir": str(tmp_path),
            "launches": {"enabled": False},
            "space_weather": {"enabled": False},
            "propagation": {"enabled": False},
        }
        plugin = SpaceTrackerPlugin(mock_app, cfg)
        plugin.start()
        try:
            # Force limiter into cooldown
            plugin._limiters["celestrak"].record_attempt()
            with patch.object(plugin._http, "get") as mock_get:
                plugin._refresh_due_groups()
                mock_get.assert_not_called()
        finally:
            plugin.stop()

    def test_passes_loop_stays_idle_without_watchlist(self, mock_app, tmp_path):
        """Empty watchlist → no passes thread starts (nothing to predict for)."""
        from reticulumpi.builtin_plugins.space_tracker import SpaceTrackerPlugin
        cfg = {
            "cache_dir": str(tmp_path),
            "launches": {"enabled": False},
            "space_weather": {"enabled": False},
            "propagation": {"enabled": False},
            "passes": {"enabled": True, "watchlist": []},
        }
        plugin = SpaceTrackerPlugin(mock_app, cfg)
        plugin.start()
        try:
            thread_names = [t.name for t in plugin._threads]
            assert "space-passes" not in thread_names
            # Snapshot still reports an empty passes list
            snap = plugin.get_snapshot()
            assert snap["passes"] == []
            assert snap["passes_computed_at"] is None
        finally:
            plugin.stop()

    def test_get_status_reports_upcoming_passes(self, mock_app, tmp_path):
        from reticulumpi.builtin_plugins.space_tracker import SpaceTrackerPlugin
        cfg = {
            "cache_dir": str(tmp_path),
            "launches": {"enabled": False},
            "space_weather": {"enabled": False},
            "propagation": {"enabled": False},
            "passes": {"enabled": False},
        }
        plugin = SpaceTrackerPlugin(mock_app, cfg)
        plugin.start()
        try:
            status = plugin.get_status()
            assert status["upcoming_passes"] == 0
        finally:
            plugin.stop()

    def test_recompute_passes_skips_without_observer(self, mock_app, tmp_path):
        """No observer → no passes computed, no event published."""
        if not _sgp4_available():
            pytest.skip("sgp4 not installed")
        from sgp4.api import Satrec, jday
        from reticulumpi.builtin_plugins.space_tracker import SpaceTrackerPlugin
        cfg = {
            "cache_dir": str(tmp_path),
            "launches": {"enabled": False},
            "space_weather": {"enabled": False},
            "propagation": {"enabled": False},
            "passes": {"enabled": True, "watchlist": ["ISS (ZARYA)"]},
            # no observer block and gps_telemetry is absent on mock_app
        }
        plugin = SpaceTrackerPlugin(mock_app, cfg)
        plugin.start()
        try:
            plugin._recompute_passes(Satrec, jday)
            assert plugin._passes == []
        finally:
            plugin.stop()
