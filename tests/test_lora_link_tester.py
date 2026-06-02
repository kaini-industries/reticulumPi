"""Tests for the lora_link_tester plugin."""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Mock meshtastic before importing the plugin
# ---------------------------------------------------------------------------

_mock_meshtastic = MagicMock()
_mock_meshtastic_serial = MagicMock()
_mock_meshtastic.serial_interface = _mock_meshtastic_serial

_mock_portnums = MagicMock()
_mock_portnums.PortNum.TEXT_MESSAGE_APP = 1


@pytest.fixture(autouse=True)
def _patch_meshtastic():
    with patch.dict(
        sys.modules,
        {
            "meshtastic": _mock_meshtastic,
            "meshtastic.serial_interface": _mock_meshtastic_serial,
            "meshtastic.portnums_pb2": _mock_portnums,
        },
    ):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app() -> MagicMock:
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.identity.hash = b"\x00" * 16
    app.node_name = "TestNode"
    app.plugins = {}
    app.event_bus = MagicMock()
    return app


def _make_config(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {"serial_port": "/dev/ttyACM1"}
    cfg.update(overrides)
    return cfg


def _make_plugin(config: dict[str, Any] | None = None) -> Any:
    from reticulumpi.builtin_plugins.lora_link_tester import LoraLinkTester

    return LoraLinkTester(_make_app(), config or _make_config())


def _make_started_plugin(config: dict[str, Any] | None = None) -> Any:
    """Create a plugin with state initialized but no threads running."""
    plugin = _make_plugin(config)
    plugin._serial_port = plugin.config["serial_port"]
    plugin._target_node_id = plugin.config.get("target_node_id")
    plugin._channel_index = plugin.config.get("channel_index", 0)
    plugin._probe_interval = plugin.config.get("probe_interval", 30)
    plugin._probe_count = plugin.config.get("probe_count", 20)
    plugin._probe_timeout = plugin.config.get("probe_timeout", 30)
    plugin._max_history = plugin.config.get("max_history", 500)
    plugin._hop_limit = plugin.config.get("hop_limit")
    plugin._reconnect_delay = plugin.config.get("reconnect_delay", 10)
    plugin._max_reconnect_attempts = plugin.config.get("max_reconnect_attempts", 5)
    plugin._probe_prefix = plugin.config.get("probe_text_prefix", "LT")
    plugin._lock = threading.Lock()
    plugin._interface = MagicMock()
    plugin._connected = True
    plugin._status = "idle"
    plugin._test_running = False
    plugin._test_target = None
    plugin._test_stop_event = threading.Event()
    plugin._current_sequence = 0
    plugin._probes_sent = 0
    plugin._probes_acked = 0
    plugin._probes_lost = 0
    plugin._pending_probes = {}
    plugin._history = deque(maxlen=plugin._max_history)
    plugin._active = True
    return plugin


# ===========================================================================
# TestValidateConfig
# ===========================================================================


class TestValidateConfig:
    def test_valid_minimal_config(self):
        _make_plugin()  # should not raise

    def test_valid_full_config(self):
        _make_plugin(
            _make_config(
                target_node_id="!abcd1234",
                channel_index=3,
                probe_interval=15,
                probe_count=50,
                probe_timeout=20,
                max_history=100,
                hop_limit=4,
                reconnect_delay=5,
                max_reconnect_attempts=3,
            )
        )

    def test_missing_serial_port(self):
        with pytest.raises(ValueError, match="serial_port"):
            _make_plugin({"serial_port": ""})

    def test_serial_port_not_string(self):
        with pytest.raises(ValueError, match="serial_port"):
            _make_plugin({"serial_port": 42})

    def test_invalid_target_node_id(self):
        with pytest.raises(ValueError, match="target_node_id"):
            _make_plugin(_make_config(target_node_id="badid"))

    def test_target_node_id_too_short(self):
        with pytest.raises(ValueError, match="target_node_id"):
            _make_plugin(_make_config(target_node_id="!abc"))

    def test_channel_index_out_of_range(self):
        with pytest.raises(ValueError, match="channel_index"):
            _make_plugin(_make_config(channel_index=8))

    def test_channel_index_negative(self):
        with pytest.raises(ValueError, match="channel_index"):
            _make_plugin(_make_config(channel_index=-1))

    def test_probe_interval_too_low(self):
        with pytest.raises(ValueError, match="probe_interval"):
            _make_plugin(_make_config(probe_interval=5))

    def test_probe_count_negative(self):
        with pytest.raises(ValueError, match="probe_count"):
            _make_plugin(_make_config(probe_count=-1))

    def test_probe_count_zero_is_valid(self):
        _make_plugin(_make_config(probe_count=0))

    def test_probe_timeout_too_low(self):
        with pytest.raises(ValueError, match="probe_timeout"):
            _make_plugin(_make_config(probe_timeout=2))

    def test_max_history_too_low(self):
        with pytest.raises(ValueError, match="max_history"):
            _make_plugin(_make_config(max_history=5))

    def test_hop_limit_out_of_range(self):
        with pytest.raises(ValueError, match="hop_limit"):
            _make_plugin(_make_config(hop_limit=0))

    def test_hop_limit_too_high(self):
        with pytest.raises(ValueError, match="hop_limit"):
            _make_plugin(_make_config(hop_limit=8))

    def test_hop_limit_none_is_valid(self):
        _make_plugin(_make_config(hop_limit=None))

    def test_reconnect_delay_too_low(self):
        with pytest.raises(ValueError, match="reconnect_delay"):
            _make_plugin(_make_config(reconnect_delay=0))

    def test_max_reconnect_attempts_negative(self):
        with pytest.raises(ValueError, match="max_reconnect_attempts"):
            _make_plugin(_make_config(max_reconnect_attempts=-1))


# ===========================================================================
# TestStartTest
# ===========================================================================


class TestStartTest:
    def test_start_returns_ok(self):
        plugin = _make_started_plugin(_make_config(target_node_id="!abcd1234"))
        result = plugin.start_test()
        assert result["ok"] is True
        assert result["target"] == "!abcd1234"

    def test_start_with_runtime_target(self):
        plugin = _make_started_plugin()
        result = plugin.start_test(target="!11223344")
        assert result["ok"] is True
        assert result["target"] == "!11223344"

    def test_start_no_target_fails(self):
        plugin = _make_started_plugin()
        result = plugin.start_test()
        assert result["ok"] is False
        assert "no target" in result["reason"]

    def test_start_invalid_target_fails(self):
        plugin = _make_started_plugin()
        result = plugin.start_test(target="badid")
        assert result["ok"] is False
        assert "invalid target" in result["reason"]

    def test_start_while_running_fails(self):
        plugin = _make_started_plugin(_make_config(target_node_id="!abcd1234"))
        plugin._test_running = True
        result = plugin.start_test()
        assert result["ok"] is False
        assert "already running" in result["reason"]

    def test_start_while_disconnected_fails(self):
        plugin = _make_started_plugin(_make_config(target_node_id="!abcd1234"))
        plugin._connected = False
        result = plugin.start_test()
        assert result["ok"] is False
        assert "not connected" in result["reason"]

    def test_start_custom_count(self):
        plugin = _make_started_plugin(_make_config(target_node_id="!abcd1234"))
        result = plugin.start_test(count=5)
        assert result["ok"] is True
        assert result["count"] == 5


# ===========================================================================
# TestStopTest
# ===========================================================================


class TestStopTest:
    def test_stop_when_not_running(self):
        plugin = _make_started_plugin()
        result = plugin.stop_test()
        assert result["ok"] is True
        assert "no test running" in result["reason"]

    def test_stop_running_test(self):
        plugin = _make_started_plugin(_make_config(target_node_id="!abcd1234"))
        plugin._test_running = True
        plugin._probes_sent = 10
        plugin._probes_acked = 8
        plugin._probes_lost = 2
        result = plugin.stop_test()
        assert result["ok"] is True
        assert result["stats"]["sent"] == 10
        assert result["stats"]["acked"] == 8
        assert result["stats"]["lost"] == 2
        assert not plugin._test_running


# ===========================================================================
# TestProbeCallback
# ===========================================================================


class TestProbeCallback:
    def test_ack_records_result(self):
        plugin = _make_started_plugin()
        send_mono = time.monotonic()
        send_wall = time.time()
        callback = plugin._make_probe_callback(seq=0, send_mono=send_mono, send_wall=send_wall)

        plugin._pending_probes[42] = (send_mono, send_wall, 0)

        callback({"id": 42, "rxRssi": -95, "rxSnr": 6.5})

        assert plugin._probes_acked == 1
        assert 42 not in plugin._pending_probes
        assert len(plugin._history) == 1

        result = plugin._history[0]
        assert result["seq"] == 0
        assert result["status"] == "ack"
        assert result["rssi"] == -95
        assert result["snr"] == 6.5
        assert result["rtt_ms"] is not None
        assert result["rtt_ms"] >= 0

    def test_ack_with_missing_rssi(self):
        plugin = _make_started_plugin()
        send_mono = time.monotonic()
        send_wall = time.time()
        callback = plugin._make_probe_callback(seq=1, send_mono=send_mono, send_wall=send_wall)
        callback({"id": 99})

        result = plugin._history[0]
        assert result["rssi"] is None
        assert result["snr"] is None
        assert result["status"] == "ack"


# ===========================================================================
# TestTimeoutSweep
# ===========================================================================


class TestTimeoutSweep:
    def test_timeout_marks_lost(self):
        plugin = _make_started_plugin()
        old_mono = time.monotonic() - 60
        old_wall = time.time() - 60
        plugin._pending_probes[100] = (old_mono, old_wall, 5)

        plugin._sweep_timeouts()

        assert plugin._probes_lost == 1
        assert 100 not in plugin._pending_probes
        assert len(plugin._history) == 1
        assert plugin._history[0]["status"] == "lost"
        assert plugin._history[0]["seq"] == 5
        assert plugin._history[0]["rtt_ms"] is None

    def test_no_timeout_for_recent_probes(self):
        plugin = _make_started_plugin()
        plugin._pending_probes[200] = (time.monotonic(), time.time(), 0)

        plugin._sweep_timeouts()

        assert plugin._probes_lost == 0
        assert 200 in plugin._pending_probes
        assert len(plugin._history) == 0


# ===========================================================================
# TestStatistics
# ===========================================================================


class TestStatistics:
    def test_empty_stats(self):
        plugin = _make_started_plugin()
        stats = plugin._compute_stats()
        assert stats["sent"] == 0
        assert stats["loss_pct"] == 0.0
        assert stats["rtt_min"] is None
        assert stats["rssi_avg"] is None

    def test_stats_with_data(self):
        plugin = _make_started_plugin()
        plugin._probes_sent = 3
        plugin._probes_acked = 2
        plugin._probes_lost = 1
        plugin._history.extend(
            [
                {"seq": 0, "time": 1.0, "rtt_ms": 1000.0, "rssi": -90, "snr": 8.0, "status": "ack"},
                {
                    "seq": 1,
                    "time": 2.0,
                    "rtt_ms": 2000.0,
                    "rssi": -100,
                    "snr": 4.0,
                    "status": "ack",
                },
                {
                    "seq": 2,
                    "time": 3.0,
                    "rtt_ms": None,
                    "rssi": None,
                    "snr": None,
                    "status": "lost",
                },
            ]
        )

        stats = plugin._compute_stats()
        assert stats["sent"] == 3
        assert stats["acked"] == 2
        assert stats["lost"] == 1
        assert stats["loss_pct"] == pytest.approx(33.3, abs=0.1)
        assert stats["rtt_min"] == 1000.0
        assert stats["rtt_avg"] == 1500.0
        assert stats["rtt_max"] == 2000.0
        assert stats["rssi_avg"] == -95.0
        assert stats["snr_avg"] == 6.0


# ===========================================================================
# TestSnapshot
# ===========================================================================


class TestSnapshot:
    def test_snapshot_structure(self):
        plugin = _make_started_plugin()
        snap = plugin.get_snapshot()
        assert snap["available"] is True
        assert "connected" in snap
        assert "status" in snap
        assert "test_running" in snap
        assert "results" in snap
        assert "stats" in snap
        assert isinstance(snap["results"], list)

    def test_snapshot_tails_history(self):
        plugin = _make_started_plugin()
        for i in range(20):
            plugin._history.append(
                {
                    "seq": i,
                    "time": float(i),
                    "rtt_ms": 100.0,
                    "rssi": -80,
                    "snr": 5.0,
                    "status": "ack",
                }
            )

        snap = plugin.get_snapshot()
        assert len(snap["results"]) == 10
        assert snap["results"][0]["seq"] == 10

    def test_history_returns_full_buffer(self):
        plugin = _make_started_plugin()
        for i in range(20):
            plugin._history.append(
                {
                    "seq": i,
                    "time": float(i),
                    "rtt_ms": 100.0,
                    "rssi": -80,
                    "snr": 5.0,
                    "status": "ack",
                }
            )

        hist = plugin.get_history()
        assert len(hist["results"]) == 20


# ===========================================================================
# TestClearHistory
# ===========================================================================


class TestClearHistory:
    def test_clear_empties_buffer(self):
        plugin = _make_started_plugin()
        plugin._history.append({"seq": 0})
        plugin._probes_sent = 5
        plugin._probes_acked = 3
        plugin._probes_lost = 2

        result = plugin.clear_history()
        assert result["ok"] is True
        assert len(plugin._history) == 0
        assert plugin._probes_sent == 0
        assert plugin._probes_acked == 0
        assert plugin._probes_lost == 0


# ===========================================================================
# TestGetStatus
# ===========================================================================


class TestGetStatus:
    def test_status_fields(self):
        plugin = _make_started_plugin(_make_config(target_node_id="!abcd1234"))
        status = plugin.get_status()
        assert status["active"] is True
        assert status["connected"] is True
        assert status["status"] == "idle"
        assert status["test_running"] is False
        assert status["target"] == "!abcd1234"
        assert status["serial_port"] == "/dev/ttyACM1"
        assert status["probes_sent"] == 0
