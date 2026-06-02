"""LoRa channel analysis — standalone utility for LoRaWAN-aware signal processing.

Provides per-channel power, duty cycle, noise floor estimation, CW interference
detection, and noise elevation alerting.  Designed as a composable helper with
no dependency on PluginBase — call ``on_sweep()`` after each rtl_power sweep
and read results via ``get_channel_analysis()`` / ``get_channel_power_history()``.
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Any, NamedTuple


# ---------------------------------------------------------------------------
# Channel plan definitions (LoRaWAN Regional Parameters v1.0.3)
# ---------------------------------------------------------------------------


class Channel(NamedTuple):
    idx: int
    center_hz: int
    bw_hz: int
    direction: str  # "up" or "dn"
    lo_hz: int
    hi_hz: int


def _build_us915_channels() -> tuple[Channel, ...]:
    channels: list[Channel] = []
    for n in range(64):
        center = int(902_300_000 + n * 200_000)
        bw = 125_000
        channels.append(Channel(n, center, bw, "up", center - bw // 2, center + bw // 2))
    for n in range(8):
        center = int(923_300_000 + n * 600_000)
        bw = 500_000
        channels.append(Channel(64 + n, center, bw, "dn", center - bw // 2, center + bw // 2))
    return tuple(channels)


REGION_CHANNELS: dict[str, tuple[Channel, ...]] = {
    "US915": _build_us915_channels(),
}

_DUTY_WINDOW = 30
_NF_HISTORY_LEN = 60
_CW_CONSEC_THRESHOLD = 15
_CH_POWER_HISTORY_LEN = 128


def bisect_left_hz(bins: list[int], target_hz: int) -> int:
    """Binary search for the leftmost bin index >= target_hz."""
    lo, hi = 0, len(bins)
    while lo < hi:
        mid = (lo + hi) // 2
        if bins[mid] < target_hz:
            lo = mid + 1
        else:
            hi = mid
    return lo


# ---------------------------------------------------------------------------
# LoRa channel analyzer
# ---------------------------------------------------------------------------


class LoraChannelAnalyzer:
    """Stateful per-sweep LoRa channel analysis.

    Instantiate with a region and threshold, then call ``on_sweep()`` after
    each spectrum sweep.  Read results with ``get_channel_analysis()`` and
    ``get_channel_power_history()``.  Call ``reset()`` to clear all state
    (e.g. when switching presets).
    """

    def __init__(
        self,
        region: str = "US915",
        threshold_db: float = 6.0,
        capture_trigger_threshold_db: float = 10.0,
        capture_trigger_consec: int = 3,
        capture_cooldown_s: float = 5.0,
        noise_baseline_alpha: float = 0.005,
    ) -> None:
        self._region = region
        self._threshold_db = threshold_db
        self._capture_trigger_threshold_db = capture_trigger_threshold_db
        self._capture_trigger_consec = capture_trigger_consec
        self._capture_cooldown_s = capture_cooldown_s
        self._noise_baseline_alpha = noise_baseline_alpha

        self._channels = REGION_CHANNELS.get(region, REGION_CHANNELS["US915"])
        self._init_state()

    def _init_state(self) -> None:
        num_ch = len(self._channels)

        self._channel_bin_ranges: list[tuple[int, int]] = []
        self._last_bin_count = 0
        self._last_bins_hz: list[int] = []

        self._channel_powers: list[float | None] = [None] * num_ch
        self._channel_power_per_bin_db: list[float | None] = [None] * num_ch
        self._channel_stats: list[dict[str, Any]] = [
            {
                "duty_ring": deque([False] * _DUTY_WINDOW, maxlen=_DUTY_WINDOW),
                "duty_pct": 0.0,
                "avg_db": None,
                "peak_db": None,
                "det_count": 0,
                "last_active_at": None,
            }
            for _ in range(num_ch)
        ]
        self._capture_consec_counts: list[int] = [0] * num_ch
        self._capture_last_trigger_ts: list[float] = [0.0] * num_ch
        self._channel_power_history: list[deque[tuple[float, float | None]]] = [
            deque(maxlen=_CH_POWER_HISTORY_LEN) for _ in range(num_ch)
        ]

        self._noise_floor_db: float | None = None
        self._noise_floor_history: deque[tuple[float, float]] = deque(maxlen=_NF_HISTORY_LEN)
        self._noise_baseline_db: float | None = None
        self._noise_floor_hourly: deque[tuple[float, float]] = deque(maxlen=168)  # 7 days
        self._last_hourly_ts: float = 0.0

        self._bin_max_hold: list[float | None] = []
        self._bin_consec_count: list[int] = []
        self._sweep_seconds: float = 1.0

    def reset(self) -> None:
        """Clear all accumulated state (e.g. after a preset switch)."""
        self._init_state()

    @property
    def channels(self) -> tuple[Channel, ...]:
        return self._channels

    @property
    def noise_floor_db(self) -> float | None:
        return self._noise_floor_db

    # ------------------------------------------------------------------
    # Bin-to-channel mapping
    # ------------------------------------------------------------------

    def _rebuild_bin_channel_map(self, bins: list[int]) -> None:
        num_bins = len(bins)
        self._channel_bin_ranges = []
        for ch in self._channels:
            lo_idx = bisect_left_hz(bins, ch.lo_hz)
            hi_idx = bisect_left_hz(bins, ch.hi_hz)
            self._channel_bin_ranges.append((lo_idx, hi_idx))
        self._last_bin_count = num_bins
        self._last_bins_hz = list(bins)
        self._bin_max_hold = [None] * num_bins
        self._bin_consec_count = [0] * num_bins

    # ------------------------------------------------------------------
    # Core: process one sweep
    # ------------------------------------------------------------------

    def on_sweep(
        self,
        bins_hz: list[int],
        powers_db: list[float | None],
        timestamp: float | None = None,
        sweep_seconds: float = 1.0,
    ) -> list[dict[str, Any]]:
        """Process a completed spectrum sweep through channel analysis.

        Returns a list of capture-trigger payloads (may be empty).
        """
        if not powers_db or not bins_hz:
            return []

        now = timestamp if timestamp is not None else time.time()
        self._sweep_seconds = sweep_seconds
        num_bins = len(bins_hz)

        if num_bins != self._last_bin_count:
            self._rebuild_bin_channel_map(bins_hz)

        # -- Noise floor (bottom-60% median) --
        valid_powers = sorted(v for v in powers_db if v is not None)
        if valid_powers:
            cutoff = max(1, int(len(valid_powers) * 0.6))
            bottom = valid_powers[:cutoff]
            nf = bottom[len(bottom) // 2]
            self._noise_floor_db = nf
            self._noise_floor_history.append((now, nf))
            if self._noise_baseline_db is None:
                self._noise_baseline_db = nf
            else:
                a = self._noise_baseline_alpha
                self._noise_baseline_db = a * nf + (1 - a) * self._noise_baseline_db

            # Hourly noise floor tracking — record once per hour boundary
            hour_ts = now - (now % 3600)
            if hour_ts > self._last_hourly_ts:
                self._noise_floor_hourly.append((hour_ts, nf))
                self._last_hourly_ts = hour_ts

        nf = self._noise_floor_db

        # -- Per-bin statistics --
        for i in range(min(num_bins, len(self._bin_max_hold))):
            v = powers_db[i]
            if v is None:
                continue
            prev_max = self._bin_max_hold[i]
            if prev_max is None or v > prev_max:
                self._bin_max_hold[i] = v
            if nf is not None and v > nf + 10.0:
                self._bin_consec_count[i] += 1
            else:
                self._bin_consec_count[i] = 0

        # -- Per-channel analysis --
        for ci, ch in enumerate(self._channels):
            if ci >= len(self._channel_bin_ranges):
                break
            lo_idx, hi_idx = self._channel_bin_ranges[ci]
            ch_bins = powers_db[lo_idx:hi_idx]

            linear_sum = 0.0
            count = 0
            for v in ch_bins:
                if v is not None:
                    linear_sum += 10.0 ** (v / 10.0)
                    count += 1
            if count > 0:
                ch_power = 10.0 * math.log10(linear_sum)
                ch_power_per_bin = ch_power - 10.0 * math.log10(count)
            else:
                ch_power = None
                ch_power_per_bin = None

            self._channel_powers[ci] = ch_power
            self._channel_power_per_bin_db[ci] = ch_power_per_bin
            self._channel_power_history[ci].append((now, ch_power))

            stats = self._channel_stats[ci]

            active = False
            if ch_power_per_bin is not None and nf is not None:
                active = ch_power_per_bin > nf + self._threshold_db

            stats["duty_ring"].append(active)
            ring = stats["duty_ring"]
            stats["duty_pct"] = round(100.0 * sum(ring) / len(ring), 1)

            if active:
                stats["det_count"] += 1
                stats["last_active_at"] = now

            if ch_power is not None:
                prev = stats["avg_db"]
                a = 0.1
                stats["avg_db"] = round(
                    ch_power if prev is None else a * ch_power + (1 - a) * prev, 1
                )

            if ch_power is not None:
                prev_peak = stats["peak_db"]
                if prev_peak is None or ch_power > prev_peak:
                    stats["peak_db"] = round(ch_power, 1)

        return self._check_capture_triggers(now)

    # ------------------------------------------------------------------
    # Capture triggers
    # ------------------------------------------------------------------

    def _check_capture_triggers(self, now: float) -> list[dict[str, Any]]:
        nf = self._noise_floor_db
        if nf is None:
            return []
        threshold = nf + self._capture_trigger_threshold_db
        triggers: list[dict[str, Any]] = []

        for ci, ch in enumerate(self._channels):
            power_per_bin = self._channel_power_per_bin_db[ci]
            if power_per_bin is not None and power_per_bin > threshold:
                self._capture_consec_counts[ci] += 1
            else:
                self._capture_consec_counts[ci] = 0
                continue

            if self._capture_consec_counts[ci] < self._capture_trigger_consec:
                continue
            if now - self._capture_last_trigger_ts[ci] < self._capture_cooldown_s:
                continue

            self._capture_last_trigger_ts[ci] = now
            self._capture_consec_counts[ci] = 0

            power = self._channel_powers[ci]
            triggers.append(
                {
                    "channel_idx": ch.idx,
                    "center_hz": ch.center_hz,
                    "bw_hz": ch.bw_hz,
                    "direction": ch.direction,
                    "power_db": round(power, 1) if power is not None else None,
                    "noise_floor_db": round(nf, 1),
                    "excess_db": round(power - nf, 1) if power is not None else None,
                    "timestamp": round(now, 3),
                }
            )

        return triggers

    # ------------------------------------------------------------------
    # Interference classification
    # ------------------------------------------------------------------

    def build_interference_flags(self, bins_hz: list[int] | None = None) -> list[dict[str, Any]]:
        """Build interference flags, resolving CW freq from bins_hz or last-seen bins."""
        flags: list[dict[str, Any]] = []
        nf = self._noise_floor_db
        freq_bins = bins_hz if bins_hz else self._last_bins_hz

        consec = self._bin_consec_count
        i = 0
        while i < len(consec):
            if consec[i] >= _CW_CONSEC_THRESHOLD:
                j = i + 1
                while j < len(consec) and consec[j] >= _CW_CONSEC_THRESHOLD:
                    j += 1
                if j - i <= 3:
                    center_idx = (i + j) // 2
                    freq_mhz = (
                        freq_bins[center_idx] / 1_000_000 if center_idx < len(freq_bins) else 0
                    )
                    power = (
                        self._bin_max_hold[center_idx]
                        if center_idx < len(self._bin_max_hold)
                        else None
                    )
                    duration_s = consec[center_idx] * self._sweep_seconds
                    flags.append(
                        {
                            "type": "cw",
                            "freq_mhz": round(freq_mhz, 4),
                            "power_db": round(power, 1) if power is not None else None,
                            "duration_s": round(duration_s, 1),
                        }
                    )
                i = j
            else:
                i += 1

        if nf is not None and self._noise_baseline_db is not None:
            delta = nf - self._noise_baseline_db
            if delta > 3.0:
                flags.append(
                    {
                        "type": "noise_elevated",
                        "current_db": round(nf, 1),
                        "baseline_db": round(self._noise_baseline_db, 1),
                        "delta_db": round(delta, 1),
                    }
                )

        return flags

    # ------------------------------------------------------------------
    # Output: snapshot / history
    # ------------------------------------------------------------------

    def get_channel_recommendations(self) -> list[dict[str, Any]]:
        """Score channels by cleanliness and return top 10 recommendations.

        Score = 100 - duty_pct - interference_penalty.
        Interference penalty: 20 for CW interference overlapping the channel,
        10 if noise is elevated.
        """
        interference_flags = self.build_interference_flags()

        # Build set of CW interference frequencies for fast lookup
        cw_freqs_mhz: list[float] = []
        noise_elevated = False
        for flag in interference_flags:
            if flag["type"] == "cw":
                cw_freqs_mhz.append(flag["freq_mhz"])
            elif flag["type"] == "noise_elevated":
                noise_elevated = True

        scored: list[dict[str, Any]] = []
        for ci, ch in enumerate(self._channels):
            stats = self._channel_stats[ci]
            duty_pct = stats["duty_pct"]

            # Interference penalty
            interference_penalty = 0.0
            ch_lo_mhz = ch.lo_hz / 1_000_000
            ch_hi_mhz = ch.hi_hz / 1_000_000
            for cw_mhz in cw_freqs_mhz:
                if ch_lo_mhz <= cw_mhz <= ch_hi_mhz:
                    interference_penalty += 20.0
                    break
            if noise_elevated:
                interference_penalty += 10.0

            score = max(0.0, 100.0 - duty_pct - interference_penalty)
            scored.append(
                {
                    "idx": ch.idx,
                    "center_mhz": round(ch.center_hz / 1_000_000, 4),
                    "bw_khz": ch.bw_hz // 1000,
                    "dir": ch.direction,
                    "score": round(score, 1),
                    "duty_pct": duty_pct,
                    "interference_penalty": round(interference_penalty, 1),
                }
            )

        scored.sort(key=lambda c: c["score"], reverse=True)
        return scored[:10]

    def get_channel_analysis(self, bins_hz: list[int] | None = None) -> dict[str, Any]:
        """Return the channel analysis dict matching the current lora_scanner snapshot shape."""
        nf = self._noise_floor_db

        channels_out = []
        active_count = 0
        for ci, ch in enumerate(self._channels):
            stats = self._channel_stats[ci]
            power = self._channel_powers[ci]
            power_per_bin = self._channel_power_per_bin_db[ci]
            active = False
            if power_per_bin is not None and nf is not None:
                active = power_per_bin > nf + self._threshold_db
            if active:
                active_count += 1
            channels_out.append(
                {
                    "idx": ch.idx,
                    "center_mhz": round(ch.center_hz / 1_000_000, 4),
                    "bw_khz": ch.bw_hz // 1000,
                    "dir": ch.direction,
                    "power_db": round(power, 1) if power is not None else None,
                    "avg_db": stats["avg_db"],
                    "peak_db": stats["peak_db"],
                    "duty_pct": stats["duty_pct"],
                    "active": active,
                    "det_count": stats["det_count"],
                    "last_active_at": stats["last_active_at"],
                }
            )

        nf_trend = [
            {"t": round(t, 3), "db": round(db, 1)}
            for t, db in list(self._noise_floor_history)[-30:]
        ]

        interference = self.build_interference_flags(bins_hz)

        # Hourly noise floor — last 24 entries
        hourly = [
            {"t": round(t, 0), "db": round(db, 1)} for t, db in list(self._noise_floor_hourly)[-24:]
        ]

        return {
            "channels": channels_out,
            "noise_floor_db": round(nf, 1) if nf is not None else None,
            "noise_floor_trend": nf_trend,
            "noise_baseline_db": round(self._noise_baseline_db, 1)
            if self._noise_baseline_db is not None
            else None,
            "active_count": active_count,
            "threshold_db": self._threshold_db,
            "interference_flags": interference,
            "noise_floor_hourly": hourly,
            "channel_recommendations": self.get_channel_recommendations(),
        }

    def get_channel_power_history(self) -> dict[int, list[list[float | None]]]:
        """Return per-channel power time-series for history drill-down."""
        ch_hist: dict[int, list[list[float | None]]] = {}
        for ci, _ch in enumerate(self._channels):
            entries = list(self._channel_power_history[ci])
            if entries:
                ch_hist[ci] = [
                    [round(t, 3), round(p, 1) if p is not None else None] for t, p in entries
                ]
        return ch_hist
