"""Tests for the system monitor plugin."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from reticulumpi.builtin_plugins.system_monitor import SystemMonitor


def _make_app() -> MagicMock:
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.event_bus = MagicMock()
    return app


_mock_psutil = MagicMock()
_mock_psutil.cpu_percent.return_value = 25.0
_mock_psutil.virtual_memory.return_value = MagicMock(percent=45.0)
_mock_psutil.disk_usage.return_value = MagicMock(percent=60.0)
_mock_psutil.sensors_temperatures.return_value = {
    "cpu_thermal": [MagicMock(current=55.0)]
}


class TestCollectMetrics:
    def test_all_metrics_enabled(self):
        p = SystemMonitor(_make_app(), {})
        with patch.dict(sys.modules, {"psutil": _mock_psutil}):
            metrics = p._collect_metrics()
        assert metrics["cpu_percent"] == 25.0
        assert metrics["memory_percent"] == 45.0
        assert metrics["disk_percent"] == 60.0
        assert metrics["cpu_temp"] == 55.0
        assert "timestamp" in metrics

    def test_subset_metrics(self):
        p = SystemMonitor(_make_app(), {"metrics": ["cpu_percent"]})
        with patch.dict(sys.modules, {"psutil": _mock_psutil}):
            metrics = p._collect_metrics()
        assert "cpu_percent" in metrics
        assert "memory_percent" not in metrics
        assert "disk_percent" not in metrics
        assert "cpu_temp" not in metrics

    def test_empty_metrics_list(self):
        p = SystemMonitor(_make_app(), {"metrics": []})
        with patch.dict(sys.modules, {"psutil": _mock_psutil}):
            metrics = p._collect_metrics()
        assert "timestamp" in metrics
        assert "cpu_percent" not in metrics


class TestReadCpuTemp:
    def test_cpu_thermal(self):
        mock = MagicMock()
        mock.sensors_temperatures.return_value = {
            "cpu_thermal": [MagicMock(current=55.0)]
        }
        with patch.dict(sys.modules, {"psutil": mock}):
            result = SystemMonitor._read_cpu_temp()
        assert result == 55.0

    def test_cpu_thermal_dash(self):
        mock = MagicMock()
        mock.sensors_temperatures.return_value = {
            "cpu-thermal": [MagicMock(current=60.0)]
        }
        with patch.dict(sys.modules, {"psutil": mock}):
            result = SystemMonitor._read_cpu_temp()
        assert result == 60.0

    def test_fallback_sensor(self):
        mock = MagicMock()
        mock.sensors_temperatures.return_value = {
            "coretemp": [MagicMock(current=42.0)]
        }
        with patch.dict(sys.modules, {"psutil": mock}):
            result = SystemMonitor._read_cpu_temp()
        assert result == 42.0

    def test_no_sensors(self):
        mock = MagicMock()
        mock.sensors_temperatures.return_value = {}
        with patch.dict(sys.modules, {"psutil": mock}):
            result = SystemMonitor._read_cpu_temp()
        assert result is None

    def test_exception_returns_none(self):
        mock = MagicMock()
        mock.sensors_temperatures.side_effect = RuntimeError("no sensors")
        with patch.dict(sys.modules, {"psutil": mock}):
            result = SystemMonitor._read_cpu_temp()
        assert result is None


class TestStartStop:
    def test_start_sets_active(self):
        p = SystemMonitor(_make_app(), {})
        with patch.object(p, "_start_thread"):
            p.start()
        assert p._active is True
        assert p.latest_metrics == {}

    def test_stop_clears_active(self):
        p = SystemMonitor(_make_app(), {})
        with patch.object(p, "_start_thread"):
            p.start()
        with patch.object(p, "_join_threads"):
            p.stop()
        assert p._active is False


class TestGetStatus:
    def test_status_fields(self):
        p = SystemMonitor(_make_app(), {})
        with patch.object(p, "_start_thread"):
            p.start()
        p.latest_metrics = {"cpu_percent": 25.0}
        s = p.get_status()
        assert s["active"] is True
        assert s["metrics"]["cpu_percent"] == 25.0


class TestBroadcastSnapshot:
    def test_returns_latest_metrics(self):
        p = SystemMonitor(_make_app(), {})
        with patch.object(p, "_start_thread"):
            p.start()
        p.latest_metrics = {"cpu_percent": 30.0}
        snap = p.broadcast_snapshot()
        assert snap == {"cpu_percent": 30.0}
