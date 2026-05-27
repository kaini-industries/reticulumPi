"""Tests for the InternetProbe module."""

import socket
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi import events
from reticulumpi.event_bus import EventBus
from reticulumpi.internet_probe import InternetProbe


@pytest.fixture(autouse=True)
def _mock_ip_detection():
    with (
        patch.object(InternetProbe, "_detect_lan_ip", return_value="192.168.1.100"),
        patch.object(InternetProbe, "_detect_wan_ip", return_value="203.0.113.1"),
    ):
        yield

_DEFAULT_CONFIG = {
    "force_offline": False,
    "probe_interval": 30,
    "probe_timeout": 3,
    "offline_threshold": 3,
    "targets": [
        {"host": "1.1.1.1", "port": 53},
        {"host": "8.8.8.8", "port": 53},
        {"host": "9.9.9.9", "port": 53},
    ],
}


def _make_probe(config=None, event_bus=None):
    bus = event_bus or EventBus()
    cfg = dict(_DEFAULT_CONFIG)
    if config:
        cfg.update(config)
    return InternetProbe(bus, cfg), bus


class TestProbeOnce:
    @patch("reticulumpi.internet_probe.socket.create_connection")
    def test_succeeds_when_reachable(self, mock_conn):
        mock_sock = MagicMock()
        mock_conn.return_value = mock_sock
        probe, _ = _make_probe()
        assert probe.probe_once() is True
        mock_sock.close.assert_called_once()

    @patch("reticulumpi.internet_probe.socket.create_connection")
    def test_fails_when_all_unreachable(self, mock_conn):
        mock_conn.side_effect = OSError("Connection refused")
        probe, _ = _make_probe()
        assert probe.probe_once() is False

    @patch("reticulumpi.internet_probe.socket.create_connection")
    def test_any_target_sufficient(self, mock_conn):
        mock_sock = MagicMock()
        mock_conn.side_effect = [
            OSError("fail"),
            OSError("fail"),
            mock_sock,
        ]
        probe, _ = _make_probe()
        assert probe.probe_once() is True

    def test_force_offline_skips_network(self):
        probe, _ = _make_probe({"force_offline": True})
        with patch("reticulumpi.internet_probe.socket.create_connection") as mock_conn:
            assert probe.probe_once() is False
            mock_conn.assert_not_called()

    @patch("reticulumpi.internet_probe.socket.create_connection")
    def test_timeout_treated_as_failure(self, mock_conn):
        mock_conn.side_effect = socket.timeout("timed out")
        probe, _ = _make_probe()
        assert probe.probe_once() is False


class TestHysteresis:
    @patch("reticulumpi.internet_probe.socket.create_connection")
    def test_no_offline_on_single_failure(self, mock_conn):
        mock_sock = MagicMock()
        mock_conn.return_value = mock_sock
        probe, bus = _make_probe()
        probe.probe_once()
        probe._set_state(True)

        published = []
        bus.subscribe(events.INTERNET_OFFLINE, lambda e, d: published.append(e))

        mock_conn.side_effect = OSError("fail")
        probe._run_check()
        assert probe.is_online is True
        assert len(published) == 0

    @patch("reticulumpi.internet_probe.socket.create_connection")
    def test_threshold_triggers_offline(self, mock_conn):
        probe, bus = _make_probe({"offline_threshold": 3})
        probe._set_state(True)

        published = []
        bus.subscribe(events.INTERNET_OFFLINE, lambda e, d: published.append(e))

        mock_conn.side_effect = OSError("fail")
        for _ in range(3):
            probe._run_check()

        assert probe.is_online is False
        assert len(published) == 1

    @patch("reticulumpi.internet_probe.socket.create_connection")
    def test_single_success_restores_online(self, mock_conn):
        probe, bus = _make_probe()
        probe._set_state(False)

        published = []
        bus.subscribe(events.INTERNET_ONLINE, lambda e, d: published.append(e))

        mock_sock = MagicMock()
        mock_conn.return_value = mock_sock
        probe._run_check()

        assert probe.is_online is True
        assert len(published) == 1

    @patch("reticulumpi.internet_probe.socket.create_connection")
    def test_no_duplicate_events(self, mock_conn):
        mock_sock = MagicMock()
        mock_conn.return_value = mock_sock
        probe, bus = _make_probe()
        probe._set_state(True)

        published = []
        bus.subscribe(events.INTERNET_ONLINE, lambda e, d: published.append(e))

        probe._run_check()
        probe._run_check()

        assert len(published) == 0


class TestStartStop:
    @patch("reticulumpi.internet_probe.socket.create_connection")
    def test_start_sets_initial_state(self, mock_conn):
        mock_sock = MagicMock()
        mock_conn.return_value = mock_sock
        probe, _ = _make_probe({"probe_interval": 300})
        probe.start()
        try:
            assert probe.is_online is True
        finally:
            probe.stop()

    @patch("reticulumpi.internet_probe.socket.create_connection")
    def test_start_offline_initial_state(self, mock_conn):
        mock_conn.side_effect = OSError("fail")
        probe, _ = _make_probe({"probe_interval": 300})
        probe.start()
        try:
            assert probe.is_online is False
        finally:
            probe.stop()

    @patch("reticulumpi.internet_probe.socket.create_connection")
    def test_stop_joins_thread(self, mock_conn):
        mock_sock = MagicMock()
        mock_conn.return_value = mock_sock
        probe, _ = _make_probe({"probe_interval": 300})
        probe.start()
        assert probe._thread is not None
        assert probe._thread.is_alive()
        probe.stop()
        assert probe._thread is None

    def test_is_online_false_before_start(self):
        probe, _ = _make_probe()
        assert probe.is_online is False


class TestEventData:
    @patch("reticulumpi.internet_probe.socket.create_connection")
    def test_event_includes_timestamp(self, mock_conn):
        mock_sock = MagicMock()
        mock_conn.return_value = mock_sock
        probe, bus = _make_probe()

        received = []
        bus.subscribe(events.INTERNET_ONLINE, lambda e, d: received.append(d))

        probe._set_state(False)
        probe._run_check()

        assert len(received) == 1
        assert "timestamp" in received[0]
        assert isinstance(received[0]["timestamp"], float)


class TestSetForceOffline:
    @patch("reticulumpi.internet_probe.socket.create_connection")
    def test_transitions_offline(self, mock_conn):
        mock_sock = MagicMock()
        mock_conn.return_value = mock_sock
        probe, bus = _make_probe()
        probe._set_state(True)
        assert probe.is_online is True

        published = []
        bus.subscribe(events.INTERNET_OFFLINE, lambda e, d: published.append(e))

        probe.set_force_offline(True)
        assert probe.is_online is False
        assert len(published) == 1

    @patch("reticulumpi.internet_probe.socket.create_connection")
    def test_disable_wakes_monitor(self, mock_conn):
        """Disabling force-offline wakes the monitor loop asynchronously."""
        probe, _ = _make_probe({"force_offline": True, "probe_interval": 300})
        probe._set_state(False)
        probe.set_force_offline(False)
        assert probe._wake_event.is_set()

    def test_force_offline_property(self):
        probe, _ = _make_probe()
        assert probe.force_offline is False
        probe.set_force_offline(True)
        assert probe.force_offline is True
        probe.set_force_offline(False)
        assert probe.force_offline is False

    @patch("reticulumpi.internet_probe.socket.create_connection")
    def test_run_check_respects_force_offline(self, mock_conn):
        """A successful probe is discarded if force_offline was set mid-flight."""
        mock_sock = MagicMock()
        mock_conn.return_value = mock_sock
        probe, bus = _make_probe()
        probe._set_state(False)

        published = []
        bus.subscribe(events.INTERNET_ONLINE, lambda e, d: published.append(e))

        with probe._lock:
            probe._force_offline = True
        probe._run_check()

        assert probe.is_online is False
        assert len(published) == 0

    @patch("reticulumpi.internet_probe.socket.create_connection")
    def test_disable_force_offline_monitor_loop_checks(self, mock_conn):
        """Monitor loop runs a check promptly after force-offline is cleared."""
        mock_sock = MagicMock()
        mock_conn.return_value = mock_sock
        probe, bus = _make_probe({"force_offline": True, "probe_interval": 300})
        probe._set_state(False)

        published = []
        bus.subscribe(events.INTERNET_ONLINE, lambda e, d: published.append(e))

        probe.start()
        try:
            probe.set_force_offline(False)
            import time
            deadline = time.monotonic() + 2.0
            while not published and time.monotonic() < deadline:
                time.sleep(0.05)
            assert len(published) == 1
            assert probe.is_online is True
        finally:
            probe.stop()
