"""Tests for the lora_scanner plugin (SpectrumScanner subclass)."""

from __future__ import annotations

import threading
from collections import deque
from unittest.mock import MagicMock

from reticulumpi import events
from reticulumpi.builtin_plugins.lora_scanner import LoraScanner


def _make_app() -> MagicMock:
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.event_bus = MagicMock()
    return app


def _make_plugin(config: dict | None = None) -> LoraScanner:
    plugin = LoraScanner(_make_app(), config or {})
    plugin._state_lock = threading.Lock()
    plugin._process = None
    plugin._pid = None
    plugin._restart_count = 0
    plugin._sweep_count = 0
    plugin._last_sweep_at = None
    plugin._rtl_power_path = "/usr/bin/rtl_power"
    plugin._last_error = None
    plugin._status = "starting"
    plugin._event_sweep_topic = events.LORA_SCANNER_SWEEP
    plugin._event_status_topic = events.LORA_SCANNER_STATUS
    plugin._bins_hz = []
    plugin._latest_powers_db = []
    plugin._waterfall = deque(maxlen=plugin._waterfall_rows)
    plugin._segments = {}
    plugin._current_ts = None
    return plugin


class TestClassAttributes:
    def test_plugin_name(self):
        assert LoraScanner.plugin_name == "lora_scanner"

    def test_plugin_version(self):
        assert LoraScanner.plugin_version == "0.2.0"

    def test_plugin_description(self):
        assert "LoRa" in LoraScanner.plugin_description


class TestLoraDefaults:
    def test_freq_range_defaults_to_us_lora(self):
        p = _make_plugin()
        assert p._freq_start_mhz == 902.0
        assert p._freq_stop_mhz == 928.0

    def test_bin_size_defaults_to_12_5_khz(self):
        p = _make_plugin()
        assert p._bin_khz == 12.5

    def test_waterfall_rows_defaults_to_256(self):
        p = _make_plugin()
        assert p._waterfall_rows == 256

    def test_config_overrides_lora_defaults(self):
        p = _make_plugin({
            "freq_start_mhz": 915.0,
            "freq_stop_mhz": 928.0,
            "bin_khz": 25.0,
            "waterfall_rows": 128,
        })
        assert p._freq_start_mhz == 915.0
        assert p._freq_stop_mhz == 928.0
        assert p._bin_khz == 25.0
        assert p._waterfall_rows == 128


class TestEventTopics:
    def test_event_topics_are_lora_specific(self):
        p = _make_plugin()
        assert p._event_sweep_topic == "lora_scanner.sweep"
        assert p._event_status_topic == "lora_scanner.status"


class TestBuildCmd:
    def test_command_uses_lora_defaults(self):
        p = _make_plugin()
        cmd = p._build_cmd()
        assert cmd[0] == "/usr/bin/rtl_power"
        freq_arg = cmd[cmd.index("-f") + 1]
        assert "902.000M" in freq_arg
        assert "928.000M" in freq_arg
        assert "12.500k" in freq_arg


class TestInheritedParsing:
    def test_csv_parse_smoke(self):
        p = _make_plugin()
        line = "2026-04-27, 12:00:00, 902000000, 928000000, 12500.00, 128, " + ", ".join(
            ["-40.0"] * 10
        )
        p._handle_csv_line(line)
        snap = p.get_snapshot()
        assert snap["status"] == "starting"
        assert snap["freq_start_hz"] == 902_000_000
