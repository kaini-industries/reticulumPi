"""Dedicated RTL-SDR LoRa-band spectrum scanner with channel-level analysis.

Extends SpectrumScanner with LoRaWAN-aware signal processing:
  - US915 channel plan (64 uplink 125 kHz + 8 downlink 500 kHz)
  - Per-channel integrated power, duty cycle, and activity detection
  - Statistical noise floor estimation (bottom-60% median)
  - Per-bin min/max hold and EMA average
  - CW interference detection and noise-floor elevation alerting
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Any, NamedTuple

from reticulumpi import events
from reticulumpi.builtin_plugins.spectrum_scanner import SpectrumScanner


# ---------------------------------------------------------------------------
# US915 LoRaWAN channel plan (LoRaWAN Regional Parameters v1.0.3)
# ---------------------------------------------------------------------------
class _Channel(NamedTuple):
    idx: int
    center_hz: int
    bw_hz: int
    direction: str  # "up" or "dn"
    lo_hz: int
    hi_hz: int


def _build_us915_channels() -> tuple[_Channel, ...]:
    channels: list[_Channel] = []
    # 64 uplink: 125 kHz BW, 902.3 + n*0.2 MHz  (n=0..63)
    for n in range(64):
        center = int(902_300_000 + n * 200_000)
        bw = 125_000
        channels.append(_Channel(n, center, bw, "up", center - bw // 2, center + bw // 2))
    # 8 downlink: 500 kHz BW, 923.3 + n*0.6 MHz  (n=0..7)
    for n in range(8):
        center = int(923_300_000 + n * 600_000)
        bw = 500_000
        channels.append(_Channel(64 + n, center, bw, "dn", center - bw // 2, center + bw // 2))
    return tuple(channels)


_REGION_CHANNELS = {
    "US915": _build_us915_channels(),
}

_LORA_DEFAULTS: dict[str, object] = {
    "freq_start_mhz": 902.0,
    "freq_stop_mhz": 928.0,
    "bin_khz": 12.5,
    "sweep_seconds": 1,
    "gain_db": 34.0,
    "waterfall_rows": 256,
}

# Signal detection threshold above noise floor (dB).
_DEFAULT_THRESHOLD_DB = 6.0

# Rolling window for duty cycle calculation (number of sweeps).
_DUTY_WINDOW = 30

# Noise floor history depth (sweeps) for trend display.
_NF_HISTORY_LEN = 60

# Number of consecutive above-threshold sweeps to flag CW interference.
_CW_CONSEC_THRESHOLD = 15

# Channel power history depth per channel (for time-series drill-down).
_CH_POWER_HISTORY_LEN = 128


class LoraScanner(SpectrumScanner):
    plugin_name = "lora_scanner"
    plugin_version = "0.2.0"
    plugin_description = "Dedicated RTL-SDR LoRa-band scanner with channel analysis"
    broadcast_tier = 2
    broadcast_keys = "lora_scanner"

    def validate_config(self) -> None:
        for key, default in _LORA_DEFAULTS.items():
            self.config.setdefault(key, default)
        self._threshold_db = float(self.config.get("threshold_db", _DEFAULT_THRESHOLD_DB))
        self._lora_region = str(self.config.get("lora_region", "US915"))
        super().validate_config()

    def start(self) -> None:
        super().start()
        self._event_sweep_topic = events.LORA_SCANNER_SWEEP
        self._event_status_topic = events.LORA_SCANNER_STATUS

        # Channel plan
        self._channels = _REGION_CHANNELS.get(self._lora_region, _REGION_CHANNELS["US915"])
        num_ch = len(self._channels)

        # Bin-to-channel mapping (rebuilt when bin grid changes)
        self._channel_bin_ranges: list[tuple[int, int]] = []
        self._last_bin_count = 0

        # Per-channel rolling stats
        self._channel_powers: list[float | None] = [None] * num_ch
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
        self._channel_power_history: list[deque[tuple[float, float | None]]] = [
            deque(maxlen=_CH_POWER_HISTORY_LEN) for _ in range(num_ch)
        ]

        # Noise floor
        self._noise_floor_db: float | None = None
        self._noise_floor_history: deque[tuple[float, float]] = deque(maxlen=_NF_HISTORY_LEN)
        self._noise_baseline_db: float | None = None
        self._noise_baseline_alpha = 0.005

        # Per-bin statistics
        self._bin_max_hold: list[float | None] = []

        # CW interference detection
        self._bin_consec_count: list[int] = []

    # ------------------------------------------------------------------
    # Bin-to-channel mapping
    # ------------------------------------------------------------------

    def _rebuild_bin_channel_map(self) -> None:
        """Two-pointer sweep mapping each channel to its [start, end) bin range."""
        bins = self._bins_hz
        num_bins = len(bins)
        self._channel_bin_ranges = []

        for ch in self._channels:
            lo_idx = _bisect_left_hz(bins, ch.lo_hz)
            hi_idx = _bisect_left_hz(bins, ch.hi_hz)
            self._channel_bin_ranges.append((lo_idx, hi_idx))

        self._last_bin_count = num_bins

        # Resize per-bin stat arrays
        self._bin_max_hold = [None] * num_bins
        self._bin_consec_count = [0] * num_bins

    # ------------------------------------------------------------------
    # Core override: signal processing after every sweep
    # ------------------------------------------------------------------

    def _flush_current_sweep(self) -> None:
        super()._flush_current_sweep()

        if not getattr(self, "_channel_bin_ranges", None) and not getattr(self, "_last_bin_count", None):
            return

        with self._state_lock:
            powers = self._latest_powers_db
            bins = self._bins_hz
        if not powers or not bins:
            return

        num_bins = len(bins)

        # Rebuild mapping if bin grid changed
        if num_bins != self._last_bin_count:
            self._rebuild_bin_channel_map()

        now = time.time()

        # -- Noise floor (bottom-60% median) --
        valid_powers = sorted(v for v in powers if v is not None)
        if valid_powers:
            cutoff = max(1, int(len(valid_powers) * 0.6))
            bottom = valid_powers[:cutoff]
            nf = bottom[len(bottom) // 2]
            self._noise_floor_db = nf
            self._noise_floor_history.append((now, nf))
            # Long-term baseline EMA
            if self._noise_baseline_db is None:
                self._noise_baseline_db = nf
            else:
                a = self._noise_baseline_alpha
                self._noise_baseline_db = a * nf + (1 - a) * self._noise_baseline_db

        nf = self._noise_floor_db

        # -- Per-bin statistics --
        for i in range(min(num_bins, len(self._bin_max_hold))):
            v = powers[i]
            if v is None:
                continue
            # Max hold (used by CW interference detection)
            prev_max = self._bin_max_hold[i]
            if prev_max is None or v > prev_max:
                self._bin_max_hold[i] = v
            # CW consecutive counter
            if nf is not None and v > nf + 10.0:
                self._bin_consec_count[i] += 1
            else:
                self._bin_consec_count[i] = 0

        # -- Per-channel analysis --
        for ci, ch in enumerate(self._channels):
            if ci >= len(self._channel_bin_ranges):
                break
            lo_idx, hi_idx = self._channel_bin_ranges[ci]
            ch_bins = powers[lo_idx:hi_idx]

            # Integrated power (linear sum → dB)
            linear_sum = 0.0
            count = 0
            for v in ch_bins:
                if v is not None:
                    linear_sum += 10.0 ** (v / 10.0)
                    count += 1
            if count > 0:
                ch_power = 10.0 * math.log10(linear_sum)
            else:
                ch_power = None

            self._channel_powers[ci] = ch_power
            self._channel_power_history[ci].append((now, ch_power))

            stats = self._channel_stats[ci]

            # Detection
            active = False
            if ch_power is not None and nf is not None:
                active = ch_power > nf + self._threshold_db

            stats["duty_ring"].append(active)
            ring = stats["duty_ring"]
            stats["duty_pct"] = round(100.0 * sum(ring) / len(ring), 1)

            if active:
                stats["det_count"] += 1
                stats["last_active_at"] = now

            # EMA average power
            if ch_power is not None:
                prev = stats["avg_db"]
                a = 0.1
                stats["avg_db"] = round(ch_power if prev is None else a * ch_power + (1 - a) * prev, 1)

            # Peak power
            if ch_power is not None:
                prev_peak = stats["peak_db"]
                if prev_peak is None or ch_power > prev_peak:
                    stats["peak_db"] = round(ch_power, 1)

    # ------------------------------------------------------------------
    # Interference classification
    # ------------------------------------------------------------------

    def _build_interference_flags(self) -> list[dict[str, Any]]:
        flags: list[dict[str, Any]] = []
        if not getattr(self, "_noise_floor_db", None) and not getattr(self, "_bin_consec_count", None):
            return flags
        nf = self._noise_floor_db

        # CW detection: single-bin or tight cluster sustained above threshold
        bins = self._bins_hz
        consec = self._bin_consec_count
        i = 0
        while i < len(consec):
            if consec[i] >= _CW_CONSEC_THRESHOLD:
                # Find cluster width
                j = i + 1
                while j < len(consec) and consec[j] >= _CW_CONSEC_THRESHOLD:
                    j += 1
                if j - i <= 3:  # narrow cluster = CW
                    center_idx = (i + j) // 2
                    freq_mhz = bins[center_idx] / 1_000_000 if center_idx < len(bins) else 0
                    power = self._bin_max_hold[center_idx] if center_idx < len(self._bin_max_hold) else None
                    duration_s = consec[center_idx] * self._sweep_seconds
                    flags.append({
                        "type": "cw",
                        "freq_mhz": round(freq_mhz, 4),
                        "power_db": round(power, 1) if power is not None else None,
                        "duration_s": round(duration_s, 1),
                    })
                i = j
            else:
                i += 1

        # Noise floor elevation
        if nf is not None and self._noise_baseline_db is not None:
            delta = nf - self._noise_baseline_db
            if delta > 3.0:
                flags.append({
                    "type": "noise_elevated",
                    "current_db": round(nf, 1),
                    "baseline_db": round(self._noise_baseline_db, 1),
                    "delta_db": round(delta, 1),
                })

        return flags

    # ------------------------------------------------------------------
    # Enhanced snapshot / history
    # ------------------------------------------------------------------

    def get_snapshot(self) -> dict[str, Any]:
        snap = super().get_snapshot()

        if not getattr(self, "_channel_bin_ranges", None):
            return snap

        nf = self._noise_floor_db

        # Channel analysis
        channels_out = []
        active_count = 0
        for ci, ch in enumerate(self._channels):
            stats = self._channel_stats[ci]
            power = self._channel_powers[ci]
            active = False
            if power is not None and nf is not None:
                active = power > nf + self._threshold_db
            if active:
                active_count += 1
            channels_out.append({
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
            })

        # Noise floor trend (last 30 for sparkline)
        nf_trend = [
            {"t": round(t, 3), "db": round(db, 1)}
            for t, db in list(self._noise_floor_history)[-30:]
        ]

        snap["channel_analysis"] = {
            "channels": channels_out,
            "noise_floor_db": round(nf, 1) if nf is not None else None,
            "noise_floor_trend": nf_trend,
            "noise_baseline_db": round(self._noise_baseline_db, 1) if self._noise_baseline_db is not None else None,
            "active_count": active_count,
            "threshold_db": self._threshold_db,
            "interference_flags": self._build_interference_flags(),
        }

        return snap

    def get_history(self) -> dict[str, Any]:
        hist = super().get_history()

        if not getattr(self, "_channel_power_history", None):
            return hist

        # Per-channel power history for time-series drill-down
        ch_hist: dict[int, list[list[float | None]]] = {}
        for ci, ch in enumerate(self._channels):
            entries = list(self._channel_power_history[ci])
            if entries:
                ch_hist[ci] = [
                    [round(t, 3), round(p, 1) if p is not None else None]
                    for t, p in entries
                ]

        hist["channel_power_history"] = ch_hist
        return hist


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _bisect_left_hz(bins: list[int], target_hz: int) -> int:
    """Binary search for the leftmost bin index >= target_hz."""
    lo, hi = 0, len(bins)
    while lo < hi:
        mid = (lo + hi) // 2
        if bins[mid] < target_hz:
            lo = mid + 1
        else:
            hi = mid
    return lo
