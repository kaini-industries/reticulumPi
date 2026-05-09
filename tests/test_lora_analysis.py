"""Tests for the LoraChannelAnalyzer utility class."""

from __future__ import annotations

import math

from reticulumpi.builtin_plugins.lora_analysis import (
    REGION_CHANNELS,
    LoraChannelAnalyzer,
    bisect_left_hz,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _us915_bins(bin_khz: float = 12.5) -> list[int]:
    """Generate a bin frequency array matching a 902–928 MHz sweep at given resolution."""
    start_hz = 902_000_000
    stop_hz = 928_000_000
    step_hz = int(bin_khz * 1000)
    return list(range(start_hz, stop_hz, step_hz))


def _uniform_powers(bins: list[int], db: float = -90.0) -> list[float | None]:
    return [db] * len(bins)


def _powers_with_active_channel(
    bins: list[int],
    channel_idx: int,
    signal_db: float = -50.0,
    floor_db: float = -90.0,
) -> list[float | None]:
    """Create powers array with one channel elevated above the floor."""
    channels = REGION_CHANNELS["US915"]
    ch = channels[channel_idx]
    powers: list[float | None] = [floor_db] * len(bins)
    for i, f in enumerate(bins):
        if ch.lo_hz <= f < ch.hi_hz:
            powers[i] = signal_db
    return powers


# ---------------------------------------------------------------------------
# Channel plan
# ---------------------------------------------------------------------------

class TestChannelPlan:
    def test_us915_has_72_channels(self):
        channels = REGION_CHANNELS["US915"]
        assert len(channels) == 72

    def test_us915_64_uplink_8_downlink(self):
        channels = REGION_CHANNELS["US915"]
        ups = [c for c in channels if c.direction == "up"]
        dns = [c for c in channels if c.direction == "dn"]
        assert len(ups) == 64
        assert len(dns) == 8

    def test_uplink_channels_are_125khz(self):
        channels = REGION_CHANNELS["US915"]
        for c in channels[:64]:
            assert c.bw_hz == 125_000

    def test_downlink_channels_are_500khz(self):
        channels = REGION_CHANNELS["US915"]
        for c in channels[64:]:
            assert c.bw_hz == 500_000

    def test_first_uplink_center(self):
        assert REGION_CHANNELS["US915"][0].center_hz == 902_300_000

    def test_first_downlink_center(self):
        assert REGION_CHANNELS["US915"][64].center_hz == 923_300_000

    def test_channel_namedtuple_fields(self):
        ch = REGION_CHANNELS["US915"][0]
        assert ch.lo_hz == ch.center_hz - ch.bw_hz // 2
        assert ch.hi_hz == ch.center_hz + ch.bw_hz // 2


# ---------------------------------------------------------------------------
# bisect_left_hz
# ---------------------------------------------------------------------------

class TestBisectLeftHz:
    def test_exact_match(self):
        bins = [100, 200, 300, 400]
        assert bisect_left_hz(bins, 200) == 1

    def test_between_values(self):
        bins = [100, 200, 300, 400]
        assert bisect_left_hz(bins, 250) == 2

    def test_below_all(self):
        bins = [100, 200, 300]
        assert bisect_left_hz(bins, 50) == 0

    def test_above_all(self):
        bins = [100, 200, 300]
        assert bisect_left_hz(bins, 400) == 3

    def test_empty_bins(self):
        assert bisect_left_hz([], 100) == 0


# ---------------------------------------------------------------------------
# Construction / reset
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_default_region(self):
        a = LoraChannelAnalyzer()
        assert len(a.channels) == 72

    def test_custom_threshold(self):
        a = LoraChannelAnalyzer(threshold_db=10.0)
        assert a._threshold_db == 10.0

    def test_unknown_region_falls_back_to_us915(self):
        a = LoraChannelAnalyzer(region="UNKNOWN")
        assert len(a.channels) == 72

    def test_initial_noise_floor_is_none(self):
        a = LoraChannelAnalyzer()
        assert a.noise_floor_db is None


class TestReset:
    def test_reset_clears_noise_floor(self):
        a = LoraChannelAnalyzer()
        bins = _us915_bins()
        a.on_sweep(bins, _uniform_powers(bins, -80.0), timestamp=1.0)
        assert a.noise_floor_db is not None
        a.reset()
        assert a.noise_floor_db is None

    def test_reset_clears_channel_powers(self):
        a = LoraChannelAnalyzer()
        bins = _us915_bins()
        a.on_sweep(bins, _uniform_powers(bins, -80.0), timestamp=1.0)
        a.reset()
        assert all(p is None for p in a._channel_powers)


# ---------------------------------------------------------------------------
# Noise floor estimation
# ---------------------------------------------------------------------------

class TestNoiseFloor:
    def test_noise_floor_from_uniform_sweep(self):
        a = LoraChannelAnalyzer()
        bins = _us915_bins()
        a.on_sweep(bins, _uniform_powers(bins, -90.0), timestamp=1.0)
        assert a.noise_floor_db is not None
        assert abs(a.noise_floor_db - (-90.0)) < 0.1

    def test_noise_floor_ignores_none_bins(self):
        a = LoraChannelAnalyzer()
        bins = _us915_bins()
        powers: list[float | None] = [None] * (len(bins) // 2) + [-80.0] * (len(bins) - len(bins) // 2)
        a.on_sweep(bins, powers, timestamp=1.0)
        assert a.noise_floor_db is not None
        assert abs(a.noise_floor_db - (-80.0)) < 0.1

    def test_noise_floor_history_grows(self):
        a = LoraChannelAnalyzer()
        bins = _us915_bins()
        for i in range(5):
            a.on_sweep(bins, _uniform_powers(bins, -90.0 + i), timestamp=float(i))
        assert len(a._noise_floor_history) == 5

    def test_noise_baseline_ema_initialised_from_first_sweep(self):
        a = LoraChannelAnalyzer()
        bins = _us915_bins()
        a.on_sweep(bins, _uniform_powers(bins, -85.0), timestamp=1.0)
        assert a._noise_baseline_db is not None
        assert abs(a._noise_baseline_db - (-85.0)) < 0.1


# ---------------------------------------------------------------------------
# Per-channel analysis
# ---------------------------------------------------------------------------

class TestChannelAnalysis:
    def test_channel_power_computed_for_active_channel(self):
        a = LoraChannelAnalyzer(threshold_db=6.0)
        bins = _us915_bins()
        powers = _powers_with_active_channel(bins, 0, signal_db=-50.0, floor_db=-90.0)
        a.on_sweep(bins, powers, timestamp=1.0)
        assert a._channel_powers[0] is not None
        assert a._channel_powers[0] > -55.0

    def test_inactive_channel_has_floor_power(self):
        a = LoraChannelAnalyzer(threshold_db=6.0)
        bins = _us915_bins()
        a.on_sweep(bins, _uniform_powers(bins, -90.0), timestamp=1.0)
        # Channel power should be near the floor
        ch_power = a._channel_powers[0]
        assert ch_power is not None
        assert ch_power <= -80.0

    def test_duty_cycle_increases_with_active_sweeps(self):
        a = LoraChannelAnalyzer(threshold_db=6.0)
        bins = _us915_bins()
        for i in range(10):
            powers = _powers_with_active_channel(bins, 0, signal_db=-50.0, floor_db=-90.0)
            a.on_sweep(bins, powers, timestamp=float(i))
        assert a._channel_stats[0]["duty_pct"] > 0.0

    def test_detection_count_increments(self):
        a = LoraChannelAnalyzer(threshold_db=6.0)
        bins = _us915_bins()
        for i in range(3):
            powers = _powers_with_active_channel(bins, 0, signal_db=-50.0, floor_db=-90.0)
            a.on_sweep(bins, powers, timestamp=float(i))
        assert a._channel_stats[0]["det_count"] >= 3

    def test_avg_db_tracks_ema(self):
        a = LoraChannelAnalyzer()
        bins = _us915_bins()
        for i in range(5):
            a.on_sweep(bins, _uniform_powers(bins, -80.0), timestamp=float(i))
        assert a._channel_stats[0]["avg_db"] is not None

    def test_peak_db_tracks_maximum(self):
        a = LoraChannelAnalyzer()
        bins = _us915_bins()
        a.on_sweep(bins, _uniform_powers(bins, -80.0), timestamp=1.0)
        a.on_sweep(bins, _uniform_powers(bins, -70.0), timestamp=2.0)
        a.on_sweep(bins, _uniform_powers(bins, -80.0), timestamp=3.0)
        # Peak should reflect the -70 sweep
        assert a._channel_stats[0]["peak_db"] is not None
        assert a._channel_stats[0]["peak_db"] > -75.0

    def test_empty_sweep_is_noop(self):
        a = LoraChannelAnalyzer()
        triggers = a.on_sweep([], [], timestamp=1.0)
        assert triggers == []
        assert a.noise_floor_db is None

    def test_uniform_noise_not_active(self):
        """Uniform noise should NOT make any channel appear active (regression)."""
        a = LoraChannelAnalyzer(threshold_db=6.0)
        bins = _us915_bins()
        powers = _uniform_powers(bins, db=-90.0)
        a.on_sweep(bins, powers, timestamp=1.0)
        result = a.get_channel_analysis()
        assert result["active_count"] == 0
        for ch in result["channels"]:
            assert ch["active"] is False

    def test_active_uses_per_bin_mean(self):
        """Only channels with per-bin mean above threshold should be active."""
        a = LoraChannelAnalyzer(threshold_db=6.0)
        bins = _us915_bins()
        powers = _powers_with_active_channel(bins, 0, signal_db=-50.0, floor_db=-90.0)
        a.on_sweep(bins, powers, timestamp=1.0)
        result = a.get_channel_analysis()
        assert result["channels"][0]["active"] is True
        assert result["channels"][1]["active"] is False

    def test_power_db_output_is_integrated(self):
        """power_db in output should be the integrated (sum) channel power, not per-bin mean."""
        a = LoraChannelAnalyzer(threshold_db=6.0)
        bins = _us915_bins()
        powers = _uniform_powers(bins, db=-90.0)
        a.on_sweep(bins, powers, timestamp=1.0)
        result = a.get_channel_analysis()
        ch0 = result["channels"][0]
        assert ch0["power_db"] is not None
        n_bins = len([b for b in bins if a._channels[0].center_hz - a._channels[0].bw_hz // 2
                      <= b < a._channels[0].center_hz + a._channels[0].bw_hz // 2])
        expected_integrated = -90.0 + 10.0 * math.log10(n_bins)
        assert abs(ch0["power_db"] - expected_integrated) < 0.5


# ---------------------------------------------------------------------------
# Capture triggers (per-bin mean)
# ---------------------------------------------------------------------------

class TestCaptureTriggerPerBinMean:
    def test_capture_trigger_uses_per_bin_mean(self):
        """Capture trigger should compare per-bin mean, not integrated power."""
        a = LoraChannelAnalyzer(
            threshold_db=6.0,
            capture_trigger_threshold_db=6.0,
            capture_trigger_consec=1,
        )
        bins = _us915_bins()
        powers = _uniform_powers(bins, db=-90.0)
        triggers = a.on_sweep(bins, powers, timestamp=1.0)
        assert len(triggers) == 0


# ---------------------------------------------------------------------------
# Capture triggers
# ---------------------------------------------------------------------------

class TestCaptureTriggers:
    def test_trigger_fires_after_consecutive_sweeps(self):
        a = LoraChannelAnalyzer(
            threshold_db=6.0,
            capture_trigger_threshold_db=10.0,
            capture_trigger_consec=3,
            capture_cooldown_s=0.0,
        )
        bins = _us915_bins()
        all_triggers: list[dict] = []
        for i in range(5):
            powers = _powers_with_active_channel(bins, 0, signal_db=-40.0, floor_db=-90.0)
            triggers = a.on_sweep(bins, powers, timestamp=float(i))
            all_triggers.extend(triggers)
        assert len(all_triggers) >= 1
        assert all_triggers[0]["channel_idx"] == 0

    def test_no_trigger_below_threshold(self):
        a = LoraChannelAnalyzer(
            threshold_db=6.0,
            capture_trigger_threshold_db=10.0,
            capture_trigger_consec=3,
        )
        bins = _us915_bins()
        all_triggers: list[dict] = []
        for i in range(5):
            a.on_sweep(bins, _uniform_powers(bins, -90.0), timestamp=float(i))
        assert all_triggers == []

    def test_cooldown_prevents_rapid_retrigger(self):
        a = LoraChannelAnalyzer(
            threshold_db=6.0,
            capture_trigger_threshold_db=10.0,
            capture_trigger_consec=2,
            capture_cooldown_s=100.0,
        )
        bins = _us915_bins()
        ch0_triggers: list[dict] = []
        # Start at t=1000 so the initial 0.0 last-trigger timestamp is far in the past
        for i in range(20):
            powers = _powers_with_active_channel(bins, 0, signal_db=-40.0, floor_db=-90.0)
            triggers = a.on_sweep(bins, powers, timestamp=1000.0 + float(i))
            ch0_triggers.extend(t for t in triggers if t["channel_idx"] == 0)
        # First trigger fires after 2 consecutive sweeps, cooldown prevents more for ch0
        assert len(ch0_triggers) == 1


# ---------------------------------------------------------------------------
# Interference flags
# ---------------------------------------------------------------------------

class TestInterferenceFlags:
    def test_noise_elevated_flag(self):
        a = LoraChannelAnalyzer()
        bins = _us915_bins()
        # Establish baseline at -90
        for i in range(20):
            a.on_sweep(bins, _uniform_powers(bins, -90.0), timestamp=float(i))
        # Force a jump
        a._noise_floor_db = -80.0
        flags = a.build_interference_flags()
        elevated = [f for f in flags if f["type"] == "noise_elevated"]
        assert len(elevated) == 1
        assert elevated[0]["delta_db"] > 3.0

    def test_no_flags_when_clean(self):
        a = LoraChannelAnalyzer()
        bins = _us915_bins()
        a.on_sweep(bins, _uniform_powers(bins, -90.0), timestamp=1.0)
        flags = a.build_interference_flags()
        assert flags == []

    def test_cw_detection_after_sustained_bins(self):
        a = LoraChannelAnalyzer()
        bins = _us915_bins()
        powers = _uniform_powers(bins, -90.0)
        # Inject a sustained strong signal in a narrow cluster
        for sweep in range(20):
            p = list(powers)
            p[100] = -50.0
            p[101] = -50.0
            a.on_sweep(bins, p, timestamp=float(sweep))
        flags = a.build_interference_flags()
        cw = [f for f in flags if f["type"] == "cw"]
        assert len(cw) >= 1


# ---------------------------------------------------------------------------
# get_channel_analysis / get_channel_power_history
# ---------------------------------------------------------------------------

class TestOutputFormats:
    def test_channel_analysis_shape(self):
        a = LoraChannelAnalyzer()
        bins = _us915_bins()
        a.on_sweep(bins, _uniform_powers(bins, -90.0), timestamp=1.0)
        result = a.get_channel_analysis()
        assert "channels" in result
        assert "noise_floor_db" in result
        assert "noise_floor_trend" in result
        assert "noise_baseline_db" in result
        assert "active_count" in result
        assert "threshold_db" in result
        assert "interference_flags" in result
        assert len(result["channels"]) == 72

    def test_channel_entry_fields(self):
        a = LoraChannelAnalyzer()
        bins = _us915_bins()
        a.on_sweep(bins, _uniform_powers(bins, -90.0), timestamp=1.0)
        ch = a.get_channel_analysis()["channels"][0]
        assert "idx" in ch
        assert "center_mhz" in ch
        assert "bw_khz" in ch
        assert "dir" in ch
        assert "power_db" in ch
        assert "avg_db" in ch
        assert "peak_db" in ch
        assert "duty_pct" in ch
        assert "active" in ch
        assert "det_count" in ch
        assert "last_active_at" in ch

    def test_channel_analysis_with_bins(self):
        a = LoraChannelAnalyzer()
        bins = _us915_bins()
        a.on_sweep(bins, _uniform_powers(bins, -90.0), timestamp=1.0)
        result = a.get_channel_analysis(bins_hz=bins)
        assert "interference_flags" in result

    def test_power_history_populated_after_sweeps(self):
        a = LoraChannelAnalyzer()
        bins = _us915_bins()
        for i in range(5):
            a.on_sweep(bins, _uniform_powers(bins, -90.0), timestamp=float(i))
        hist = a.get_channel_power_history()
        assert len(hist) > 0
        first_key = next(iter(hist))
        assert len(hist[first_key]) == 5

    def test_power_history_empty_before_sweeps(self):
        a = LoraChannelAnalyzer()
        hist = a.get_channel_power_history()
        assert hist == {}

    def test_bin_grid_change_triggers_remap(self):
        a = LoraChannelAnalyzer()
        bins_coarse = _us915_bins(bin_khz=25.0)
        bins_fine = _us915_bins(bin_khz=12.5)
        a.on_sweep(bins_coarse, _uniform_powers(bins_coarse, -90.0), timestamp=1.0)
        old_count = a._last_bin_count
        a.on_sweep(bins_fine, _uniform_powers(bins_fine, -90.0), timestamp=2.0)
        assert a._last_bin_count != old_count
