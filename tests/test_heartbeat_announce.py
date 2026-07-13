"""Tests for the heartbeat announce plugin."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch


from reticulumpi.builtin_plugins.heartbeat_announce import HeartbeatAnnounce


def _make_app() -> MagicMock:
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.event_bus = MagicMock()
    return app


class TestBuildAppData:
    def test_no_telemetry(self):
        p = HeartbeatAnnounce(_make_app(), {})
        assert p.build_app_data() is None

    def test_telemetry_with_psutil(self):
        mock_psutil = MagicMock()
        mock_psutil.cpu_percent.return_value = 25.0
        mock_psutil.virtual_memory.return_value = MagicMock(percent=45.0)
        p = HeartbeatAnnounce(_make_app(), {"include_telemetry": True})
        with patch.dict(sys.modules, {"psutil": mock_psutil}):
            with patch("socket.gethostname", return_value="testnode"):
                result = p.build_app_data()
        assert result is not None
        assert "testnode" in result
        assert "cpu:25%" in result
        assert "mem:45%" in result

    def test_telemetry_without_psutil(self):
        p = HeartbeatAnnounce(_make_app(), {"include_telemetry": True})
        with patch.dict(sys.modules, {"psutil": None}):
            with patch("socket.gethostname", return_value="testnode"):
                result = p.build_app_data()
        assert result == "testnode"


class TestStartStop:
    @patch("reticulumpi.builtin_plugins.heartbeat_announce.RNS")
    def test_start_creates_destination(self, mock_rns):
        app = _make_app()
        p = HeartbeatAnnounce(app, {})
        with patch.object(p, "_start_thread") as mock_thread:
            p.start()
        assert p.destination is not None
        assert p._active is True
        mock_thread.assert_called_once()

    @patch("reticulumpi.builtin_plugins.heartbeat_announce.RNS")
    def test_stop_clears_destination(self, mock_rns):
        app = _make_app()
        p = HeartbeatAnnounce(app, {})
        with patch.object(p, "_start_thread"):
            p.start()
        with patch.object(p, "_join_threads"):
            p.stop()
        assert p.destination is None
        assert p._active is False


class TestAnnounceLoop:
    @patch("reticulumpi.builtin_plugins.heartbeat_announce.RNS")
    def test_single_iteration(self, mock_rns):
        app = _make_app()
        p = HeartbeatAnnounce(app, {"interval_seconds": 60})
        with patch.object(p, "_start_thread"):
            p.start()
        with patch.object(
            p, "_sleep_while_active", side_effect=lambda _: setattr(p, "_active", False)
        ):
            p._announce_loop()
        p.destination.announce.assert_called_once()

    @patch("reticulumpi.builtin_plugins.heartbeat_announce.RNS")
    def test_exception_does_not_crash_loop(self, mock_rns):
        app = _make_app()
        p = HeartbeatAnnounce(app, {})
        with patch.object(p, "_start_thread"):
            p.start()
        p.destination.announce.side_effect = [RuntimeError("fail"), None]
        call_count = 0

        def stop_after_two(_):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                p._active = False

        with patch.object(p, "_sleep_while_active", side_effect=stop_after_two):
            p._announce_loop()
        assert p.destination.announce.call_count == 2
