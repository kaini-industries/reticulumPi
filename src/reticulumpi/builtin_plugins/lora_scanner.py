"""Dedicated RTL-SDR LoRa-band spectrum scanner with channel-level analysis.

Extends SpectrumScanner with LoRaWAN-aware signal processing:
  - US915 channel plan (64 uplink 125 kHz + 8 downlink 500 kHz)
  - Per-channel integrated power, duty cycle, and activity detection
  - Statistical noise floor estimation (bottom-60% median)
  - Per-bin min/max hold and EMA average
  - CW interference detection and noise-floor elevation alerting
"""

from __future__ import annotations

from typing import Any

from reticulumpi import events
from reticulumpi.builtin_plugins.lora_analysis import (
    LoraChannelAnalyzer,
)
from reticulumpi.builtin_plugins.spectrum_scanner import SpectrumScanner

_LORA_DEFAULTS: dict[str, object] = {
    "freq_start_mhz": 902.0,
    "freq_stop_mhz": 928.0,
    "bin_khz": 12.5,
    "sweep_seconds": 1,
    "gain_db": 34.0,
    "waterfall_rows": 256,
    "capture_trigger_threshold_db": 10.0,
    "capture_trigger_consecutive_sweeps": 3,
    "capture_cooldown_s": 5.0,
}

_DEFAULT_THRESHOLD_DB = 6.0


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
        self._lora_snapshot_cache: tuple[int, dict[str, Any]] | None = None
        super().validate_config()

    def start(self) -> None:
        super().start()
        self.log.warning(
            "lora_scanner is deprecated — use spectrum_scanner with "
            "default_preset: lora_us915 instead (same features, one dongle)",
        )
        self._event_sweep_topic = events.LORA_SCANNER_SWEEP
        self._event_status_topic = events.LORA_SCANNER_STATUS
        self._lora_snapshot_cache: tuple[int, dict[str, Any]] | None = None

        self._analyzer = LoraChannelAnalyzer(
            region=self._lora_region,
            threshold_db=self._threshold_db,
            capture_trigger_threshold_db=float(
                self.config.get("capture_trigger_threshold_db", 10.0),
            ),
            capture_trigger_consec=int(
                self.config.get("capture_trigger_consecutive_sweeps", 3),
            ),
            capture_cooldown_s=float(
                self.config.get("capture_cooldown_s", 5.0),
            ),
        )

    # ------------------------------------------------------------------
    # Core override: signal processing after every sweep
    # ------------------------------------------------------------------

    def _flush_current_sweep(self) -> None:
        # Parent now feeds self._analyzer and publishes LORA_CAPTURE_TRIGGER.
        super()._flush_current_sweep()
        self._lora_snapshot_cache = None

    # ------------------------------------------------------------------
    # Enhanced snapshot / history
    # ------------------------------------------------------------------

    def get_snapshot(self) -> dict[str, Any]:
        cached = self._lora_snapshot_cache
        if cached is not None and cached[0] == self._sweep_count:
            return cached[1]

        snap = super().get_snapshot()

        if self._analyzer is None:
            self._lora_snapshot_cache = (self._sweep_count, snap)
            return snap

        with self._state_lock:
            bins = self._bins_hz

        snap["channel_analysis"] = self._analyzer.get_channel_analysis(bins_hz=bins)

        self._lora_snapshot_cache = (self._sweep_count, snap)
        return snap

    def get_history(self) -> dict[str, Any]:
        hist = super().get_history()

        if self._analyzer is None:
            return hist

        hist["channel_power_history"] = self._analyzer.get_channel_power_history()
        return hist
