"""Tests for the MeshCore Observer plugin."""

from __future__ import annotations

import json
import queue
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Mock the meshcore and paho packages before any plugin imports
# ---------------------------------------------------------------------------

class _MockEventType(Enum):
    SELF_INFO = "self_info"
    DEVICE_INFO = "device_info"
    RX_LOG_DATA = "rx_log_data"
    SIGNATURE = "signature"
    PRIVATE_KEY = "private_key"
    DISABLED = "disabled"
    ERROR = "command_error"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    SIGN_START = "sign_start"


@dataclass
class _MockEvent:
    type: _MockEventType
    payload: Any
    attributes: Dict[str, Any] = field(default_factory=dict)


_mock_meshcore = MagicMock()
_mock_meshcore_events = MagicMock()
_mock_meshcore_events.EventType = _MockEventType
_mock_meshcore_events.Event = _MockEvent

_mock_paho = MagicMock()
_mock_paho_client = MagicMock()
_mock_paho_client.CallbackAPIVersion = MagicMock()
_mock_paho_client.CallbackAPIVersion.VERSION2 = 2


@pytest.fixture(autouse=True)
def _patch_modules():
    """Ensure meshcore and paho are always available as mocks."""
    with patch.dict(sys.modules, {
        "meshcore": _mock_meshcore,
        "meshcore.events": _mock_meshcore_events,
        "paho": _mock_paho,
        "paho.mqtt": _mock_paho,
        "paho.mqtt.client": _mock_paho_client,
    }):
        _mock_meshcore.MeshCore = MagicMock()
        _mock_meshcore_events.EventType = _MockEventType
        yield


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def obs_config():
    """Base config dict for the meshcore_observer plugin (standalone)."""
    return {
        "enabled": True,
        "connection_type": "serial",
        "serial_port": "/dev/ttyUSB0",
        "serial_baud": 115200,
        "iata": "AUS",
        "mqtt_broker": "mqtt-us-v1.letsmesh.net",
        "mqtt_port": 443,
        "mqtt_transport": "websockets",
        "publish_debug": False,
        "health_check_interval": 30,
        "reconnect_delay": 5,
        "max_reconnect_attempts": 3,
        "packet_queue_size": 100,
    }


@pytest.fixture
def shared_config():
    """Config dict for shared mode."""
    return {
        "enabled": True,
        "use_gateway_device": True,
        "iata": "AUS",
        "mqtt_broker": "mqtt-us-v1.letsmesh.net",
        "mqtt_port": 443,
        "mqtt_transport": "websockets",
        "packet_queue_size": 100,
    }


def _make_plugin_no_start(mock_app, config):
    """Create observer without calling start()."""
    from reticulumpi.builtin_plugins.meshcore_observer import MeshCoreObserver
    return MeshCoreObserver(mock_app, config)


def _make_started_plugin(mock_app, config):
    """Create and start observer, yielding it and cleaning up after."""
    from reticulumpi.builtin_plugins.meshcore_observer import MeshCoreObserver
    plugin = MeshCoreObserver(mock_app, config)
    plugin.start()
    yield plugin
    plugin._active = False
    plugin._loop_ready.set()
    if plugin._loop and plugin._loop.is_running():
        plugin._loop.call_soon_threadsafe(plugin._loop.stop)
    plugin._join_threads(timeout=2)


@pytest.fixture
def observer_plugin(mock_app, obs_config):
    """Started observer plugin (standalone mode)."""
    yield from _make_started_plugin(mock_app, obs_config)


@pytest.fixture
def shared_observer_plugin(mock_app, shared_config):
    """Started observer plugin (shared mode)."""
    yield from _make_started_plugin(mock_app, shared_config)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

class TestValidateConfig:

    def test_valid_config_accepted(self, mock_app, obs_config):
        plugin = _make_plugin_no_start(mock_app, obs_config)
        assert plugin.plugin_name == "meshcore_observer"

    def test_rejects_bad_connection_type(self, mock_app, obs_config):
        obs_config["connection_type"] = "bluetooth"
        with pytest.raises(ValueError, match="connection_type"):
            _make_plugin_no_start(mock_app, obs_config)

    def test_rejects_empty_serial_port(self, mock_app, obs_config):
        obs_config["serial_port"] = ""
        with pytest.raises(ValueError, match="serial_port"):
            _make_plugin_no_start(mock_app, obs_config)

    def test_rejects_bad_iata(self, mock_app, obs_config):
        obs_config["iata"] = "TOOLONG"
        with pytest.raises(ValueError, match="iata"):
            _make_plugin_no_start(mock_app, obs_config)

    def test_rejects_short_iata(self, mock_app, obs_config):
        obs_config["iata"] = "AB"
        with pytest.raises(ValueError, match="iata"):
            _make_plugin_no_start(mock_app, obs_config)

    def test_rejects_bad_baud(self, mock_app, obs_config):
        obs_config["serial_baud"] = -1
        with pytest.raises(ValueError, match="serial_baud"):
            _make_plugin_no_start(mock_app, obs_config)

    def test_rejects_small_health_interval(self, mock_app, obs_config):
        obs_config["health_check_interval"] = 1
        with pytest.raises(ValueError, match="health_check_interval"):
            _make_plugin_no_start(mock_app, obs_config)

    def test_tcp_requires_host(self, mock_app, obs_config):
        obs_config["connection_type"] = "tcp"
        obs_config["tcp_host"] = ""
        with pytest.raises(ValueError, match="tcp_host"):
            _make_plugin_no_start(mock_app, obs_config)

    def test_valid_tcp_config(self, mock_app, obs_config):
        obs_config["connection_type"] = "tcp"
        obs_config["tcp_host"] = "192.168.1.100"
        obs_config["tcp_port"] = 5000
        plugin = _make_plugin_no_start(mock_app, obs_config)
        assert plugin.plugin_name == "meshcore_observer"

    def test_shared_mode_skips_serial_validation(self, mock_app, shared_config):
        plugin = _make_plugin_no_start(mock_app, shared_config)
        assert plugin.plugin_name == "meshcore_observer"

    def test_rejects_meshcore_not_installed(self, mock_app, obs_config):
        with patch.dict(sys.modules, {"meshcore": None}):
            with pytest.raises(ValueError, match="meshcore"):
                _make_plugin_no_start(mock_app, obs_config)

    def test_rejects_paho_not_installed(self, mock_app, obs_config):
        with patch.dict(sys.modules, {"paho.mqtt.client": None}):
            with pytest.raises(ValueError, match="paho-mqtt"):
                _make_plugin_no_start(mock_app, obs_config)


# ---------------------------------------------------------------------------
# Plugin lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:

    def test_start_and_stop(self, observer_plugin):
        assert observer_plugin._active is True
        assert observer_plugin._shared_mode is False
        observer_plugin.stop()
        assert observer_plugin._active is False

    def test_initial_stats_are_zero(self, observer_plugin):
        status = observer_plugin.get_status()
        assert status["packets_captured"] == 0
        assert status["packets_published"] == 0
        assert status["packets_failed"] == 0
        assert status["mode"] == "standalone"

    def test_shared_mode_starts(self, shared_observer_plugin):
        assert shared_observer_plugin._shared_mode is True
        status = shared_observer_plugin.get_status()
        assert status["mode"] == "shared"

    def test_get_status_returns_expected_fields(self, observer_plugin):
        status = observer_plugin.get_status()
        expected_fields = {
            "active", "mode", "device_connected", "mqtt_connected",
            "public_key", "iata", "mqtt_broker", "packets_captured",
            "packets_published", "packets_failed", "last_packet_time",
            "last_mqtt_publish_time", "connect_count", "reconnect_failures",
            "signing_mode", "firmware", "model", "queue_depth",
        }
        assert expected_fields.issubset(set(status.keys()))


# ---------------------------------------------------------------------------
# Packet capture
# ---------------------------------------------------------------------------

class TestPacketCapture:

    def test_build_packet_json(self, observer_plugin):
        observer_plugin._public_key = "abcdef1234567890" * 4
        payload = {
            "recv_time": 1713900000,
            "snr": 4.5,
            "rssi": -93,
            "payload": "deadbeef",
            "payload_length": 4,
            "raw_hex": "0102deadbeef",
            "route_type": 1,
            "route_typename": "FLOOD",
            "payload_type": 2,
            "payload_typename": "TEXT_MSG",
            "path_len": 2,
            "path": "aabb",
            "pkt_hash": 123456,
        }
        result = observer_plugin._build_packet_json(payload)
        assert result["origin"] == "AUS"
        assert result["origin_id"] == "abcdef123456"
        assert result["direction"] == "rx"
        assert result["SNR"] == 4.5
        assert result["RSSI"] == -93
        assert result["route"] == "FLOOD"
        assert result["hash"] == "123456"
        assert result["raw"] == "0102deadbeef"
        assert result["path_len"] == 2

    def test_handle_rx_log_enqueues_packet(self, observer_plugin):
        observer_plugin._public_key = "aabb" * 16
        event = _MockEvent(
            type=_MockEventType.RX_LOG_DATA,
            payload={
                "recv_time": int(time.time()),
                "snr": 3.0,
                "rssi": -80,
                "raw_hex": "cafe",
                "payload_length": 2,
                "route_typename": "DIRECT",
                "payload_type": 5,
                "path_len": 0,
                "path": "",
                "pkt_hash": 999,
            },
        )
        observer_plugin._handle_rx_log(event)
        assert observer_plugin._packets_captured == 1
        assert observer_plugin._packet_queue.qsize() == 1
        pkt = observer_plugin._packet_queue.get_nowait()
        assert pkt["origin"] == "AUS"
        assert pkt["SNR"] == 3.0

    def test_queue_overflow_drops_oldest(self, mock_app):
        config = {
            "enabled": True,
            "connection_type": "serial",
            "serial_port": "/dev/ttyUSB0",
            "serial_baud": 115200,
            "iata": "AUS",
            "mqtt_broker": "test",
            "mqtt_port": 443,
            "mqtt_transport": "websockets",
            "packet_queue_size": 2,
            "health_check_interval": 30,
            "reconnect_delay": 5,
            "max_reconnect_attempts": 3,
        }
        plugin = _make_plugin_no_start(mock_app, config)
        plugin._active = True
        plugin._lock = __import__("threading").Lock()
        plugin._packets_captured = 0
        plugin._packets_failed = 0
        plugin._last_packet_time = None
        plugin._public_key = "aa" * 32
        plugin._packet_queue = queue.Queue(maxsize=2)

        base_event = _MockEvent(
            type=_MockEventType.RX_LOG_DATA,
            payload={
                "recv_time": int(time.time()),
                "snr": 1.0, "rssi": -50,
                "raw_hex": "ab", "payload_length": 1,
                "route_typename": "FLOOD", "payload_type": 0,
                "path_len": 0, "path": "", "pkt_hash": 0,
            },
        )
        # Fill the queue
        plugin._handle_rx_log(base_event)
        plugin._handle_rx_log(base_event)
        # Overflow — should drop oldest, not raise
        plugin._handle_rx_log(base_event)
        assert plugin._packets_captured == 3
        assert plugin._packets_failed == 1
        assert plugin._packet_queue.qsize() == 2


# ---------------------------------------------------------------------------
# JWT generation
# ---------------------------------------------------------------------------

class TestJWT:

    def test_jwt_structure(self, observer_plugin):
        observer_plugin._public_key = "aa" * 32
        observer_plugin._signing_mode = "device"
        observer_plugin._mc = MagicMock()
        observer_plugin._loop = MagicMock()
        observer_plugin._loop.is_running.return_value = True

        sig_event = _MockEvent(
            type=_MockEventType.SIGNATURE,
            payload={"signature": b"\x00" * 64},
        )
        with patch.object(observer_plugin, "_run_async", return_value=sig_event):
            token = observer_plugin._generate_jwt()

        parts = token.split(".")
        assert len(parts) == 3

        import base64
        header_bytes = base64.urlsafe_b64decode(parts[0] + "==")
        header = json.loads(header_bytes)
        assert header["alg"] == "Ed25519"
        assert header["typ"] == "JWT"

        payload_bytes = base64.urlsafe_b64decode(parts[1] + "==")
        payload = json.loads(payload_bytes)
        assert payload["publicKey"] == ("aa" * 32).upper()
        assert payload["aud"] == "mqtt-us-v1.letsmesh.net"
        assert "iat" in payload
        assert "exp" in payload
        assert payload["exp"] - payload["iat"] == 3600

    def test_jwt_refresh_when_expired(self, observer_plugin):
        observer_plugin._public_key = "bb" * 32
        observer_plugin._signing_mode = "device"
        observer_plugin._mc = MagicMock()
        observer_plugin._loop = MagicMock()
        observer_plugin._loop.is_running.return_value = True

        sig_event = _MockEvent(
            type=_MockEventType.SIGNATURE,
            payload={"signature": b"\x01" * 64},
        )

        observer_plugin._jwt_token = "old.token.here"
        observer_plugin._jwt_expires = time.time() - 100  # expired

        with patch.object(observer_plugin, "_run_async", return_value=sig_event):
            observer_plugin._refresh_jwt_if_needed()

        assert observer_plugin._jwt_token != "old.token.here"
        assert observer_plugin._jwt_expires > time.time()

    def test_jwt_no_refresh_when_valid(self, observer_plugin):
        observer_plugin._jwt_token = "valid.token.here"
        observer_plugin._jwt_expires = time.time() + 1800  # 30 min left
        observer_plugin._refresh_jwt_if_needed()
        assert observer_plugin._jwt_token == "valid.token.here"

    def test_device_signing_error_raises(self, observer_plugin):
        observer_plugin._public_key = "cc" * 32
        observer_plugin._signing_mode = "device"
        observer_plugin._mc = MagicMock()
        observer_plugin._loop = MagicMock()
        observer_plugin._loop.is_running.return_value = True

        err_event = _MockEvent(
            type=_MockEventType.ERROR,
            payload={"reason": "sign_failed"},
        )
        with patch.object(observer_plugin, "_run_async", return_value=err_event):
            with pytest.raises(RuntimeError, match="sign_failed"):
                observer_plugin._sign_on_device(b"test data")

    def test_no_signing_mode_raises(self, observer_plugin):
        observer_plugin._public_key = "dd" * 32
        observer_plugin._signing_mode = "none"
        with pytest.raises(RuntimeError, match="No signing method"):
            observer_plugin._generate_jwt()


# ---------------------------------------------------------------------------
# MQTT publishing
# ---------------------------------------------------------------------------

class TestMQTT:

    def test_publish_packet_correct_topic(self, observer_plugin):
        observer_plugin._public_key = "ee" * 32
        observer_plugin._connected_mqtt = True
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.rc = 0
        mock_client.publish.return_value = mock_result
        observer_plugin._mqtt_client = mock_client
        observer_plugin._jwt_token = "tok"
        observer_plugin._jwt_expires = time.time() + 3600

        packet = {"origin": "AUS", "SNR": 5.0, "RSSI": -70}
        observer_plugin._publish_packet(packet)

        expected_topic = f"meshcore/AUS/{'ee' * 32}/packets"
        mock_client.publish.assert_called_once()
        call_args = mock_client.publish.call_args
        assert call_args[0][0] == expected_topic
        published_json = json.loads(call_args[0][1])
        assert published_json["SNR"] == 5.0
        assert observer_plugin._packets_published == 1

    def test_publish_increments_failed_on_error(self, observer_plugin):
        observer_plugin._public_key = "ff" * 32
        observer_plugin._connected_mqtt = True
        mock_client = MagicMock()
        mock_client.publish.side_effect = Exception("network error")
        observer_plugin._mqtt_client = mock_client
        observer_plugin._jwt_token = "tok"
        observer_plugin._jwt_expires = time.time() + 3600

        observer_plugin._publish_packet({"origin": "AUS"})
        assert observer_plugin._packets_failed == 1

    def test_publish_skipped_when_not_connected(self, observer_plugin):
        observer_plugin._connected_mqtt = False
        observer_plugin._mqtt_client = MagicMock()
        observer_plugin._publish_packet({"origin": "AUS"})
        observer_plugin._mqtt_client.publish.assert_not_called()

    def test_publish_status(self, observer_plugin):
        observer_plugin._public_key = "aa" * 32
        observer_plugin._connected_mqtt = True
        mock_client = MagicMock()
        observer_plugin._mqtt_client = mock_client

        observer_plugin._publish_status()

        expected_topic = f"meshcore/AUS/{'aa' * 32}/status"
        mock_client.publish.assert_called_once()
        call_args = mock_client.publish.call_args
        assert call_args[0][0] == expected_topic
        status = json.loads(call_args[0][1])
        assert status["online"] is True


# ---------------------------------------------------------------------------
# Shared mode
# ---------------------------------------------------------------------------

class TestSharedMode:

    def test_attach_to_gateway(self, shared_observer_plugin, mock_app):
        shared_observer_plugin._detach_from_gateway()
        mock_gw = MagicMock()
        mock_gw.get_status.return_value = {
            "connected": True, "firmware": "1.0", "model": "RAK4631",
        }
        mock_mc = MagicMock()
        mock_mc.self_info = {"public_key": "aa" * 32}
        mock_mc.subscribe.return_value = MagicMock()
        mock_gw.get_device_handle.return_value = mock_mc
        mock_gw.get_async_loop.return_value = MagicMock()

        mock_app.get_plugin.return_value = mock_gw

        shared_observer_plugin._try_attach_to_gateway()

        assert shared_observer_plugin._connected_device is True
        assert shared_observer_plugin._public_key == ("aa" * 32).upper()
        mock_mc.subscribe.assert_called_once()

    def test_attach_fails_when_gateway_missing(self, shared_observer_plugin, mock_app):
        # Reset any auto-attachment from the watcher thread
        shared_observer_plugin._detach_from_gateway()
        mock_app.get_plugin.return_value = None
        shared_observer_plugin._try_attach_to_gateway()
        assert shared_observer_plugin._connected_device is False

    def test_attach_fails_when_gateway_disconnected(self, shared_observer_plugin, mock_app):
        shared_observer_plugin._detach_from_gateway()
        mock_gw = MagicMock()
        mock_gw.get_status.return_value = {"connected": False}
        mock_app.get_plugin.return_value = mock_gw
        shared_observer_plugin._try_attach_to_gateway()
        assert shared_observer_plugin._connected_device is False

    def test_detach_from_gateway(self, shared_observer_plugin):
        mock_mc = MagicMock()
        mock_sub = MagicMock()
        shared_observer_plugin._mc = mock_mc
        shared_observer_plugin._connected_device = True
        shared_observer_plugin._subscriptions = [mock_sub]

        shared_observer_plugin._detach_from_gateway()

        assert shared_observer_plugin._connected_device is False
        assert shared_observer_plugin._mc is None
        mock_mc.unsubscribe.assert_called_once_with(mock_sub)

    def test_gateway_disconnect_triggers_detach(self, shared_observer_plugin):
        shared_observer_plugin._mc = MagicMock()
        shared_observer_plugin._connected_device = True
        shared_observer_plugin._subscriptions = []

        shared_observer_plugin._on_gateway_disconnected(
            "meshcore.disconnected", {"reason": "test"},
        )

        assert shared_observer_plugin._connected_device is False

    def test_plugin_stopping_detaches_when_gateway(self, shared_observer_plugin):
        shared_observer_plugin._mc = MagicMock()
        shared_observer_plugin._connected_device = True
        shared_observer_plugin._subscriptions = []

        shared_observer_plugin._on_plugin_stopping(
            "plugin.stopping", {"name": "meshcore_gateway"},
        )

        assert shared_observer_plugin._connected_device is False

    def test_plugin_stopping_ignores_other_plugins(self, shared_observer_plugin):
        shared_observer_plugin._mc = MagicMock()
        shared_observer_plugin._connected_device = True
        shared_observer_plugin._subscriptions = []

        shared_observer_plugin._on_plugin_stopping(
            "plugin.stopping", {"name": "some_other_plugin"},
        )

        assert shared_observer_plugin._connected_device is True


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class TestHealthCheck:

    def test_healthy_when_connected(self, observer_plugin):
        mock_mc = MagicMock()
        mock_mc.is_connected = True
        observer_plugin._mc = mock_mc
        assert observer_plugin._check_health() is True

    def test_unhealthy_when_no_mc(self, observer_plugin):
        observer_plugin._mc = None
        assert observer_plugin._check_health() is False

    def test_unhealthy_when_disconnected(self, observer_plugin):
        mock_mc = MagicMock()
        mock_mc.is_connected = False
        observer_plugin._mc = mock_mc
        assert observer_plugin._check_health() is False

    def test_unhealthy_on_exception(self, observer_plugin):
        mock_mc = MagicMock()
        type(mock_mc).is_connected = property(lambda s: (_ for _ in ()).throw(RuntimeError("fail")))
        observer_plugin._mc = mock_mc
        assert observer_plugin._check_health() is False


# ---------------------------------------------------------------------------
# Regression tests for the bug-fix pass
# ---------------------------------------------------------------------------

class TestBugFixes:

    def test_shared_stop_does_not_stop_borrowed_loop(
        self, shared_observer_plugin, mock_app,
    ):
        """In shared mode, stop() must not touch the gateway's loop."""
        shared_observer_plugin._detach_from_gateway()

        borrowed_loop = MagicMock()
        borrowed_loop.is_running.return_value = True

        mock_gw = MagicMock()
        mock_gw.get_status.return_value = {
            "connected": True, "firmware": "1.0", "model": "X",
        }
        mock_mc = MagicMock()
        mock_mc.self_info = {"public_key": "aa" * 32}
        mock_mc.subscribe.return_value = MagicMock()
        mock_gw.get_device_handle.return_value = mock_mc
        mock_gw.get_async_loop.return_value = borrowed_loop
        mock_app.get_plugin.return_value = mock_gw

        shared_observer_plugin._try_attach_to_gateway()
        assert shared_observer_plugin._loop is borrowed_loop

        shared_observer_plugin.stop()

        borrowed_loop.call_soon_threadsafe.assert_not_called()

    def test_detach_clears_borrowed_loop(self, shared_observer_plugin):
        shared_observer_plugin._loop = MagicMock()
        shared_observer_plugin._mc = MagicMock()
        shared_observer_plugin._connected_device = True
        shared_observer_plugin._subscriptions = []

        shared_observer_plugin._detach_from_gateway()

        assert shared_observer_plugin._loop is None

    def test_mqtt_reconnect_tears_down_old_client(self, observer_plugin):
        """When health-check detects MQTT drop, old client must be stopped."""
        old_client = MagicMock()
        old_client.is_connected.return_value = False
        observer_plugin._mqtt_client = old_client
        observer_plugin._connected_mqtt = True

        # Exercise the health-check branch directly.
        if observer_plugin._mqtt_client and not observer_plugin._mqtt_client.is_connected():
            observer_plugin._disconnect_mqtt()

        old_client.loop_stop.assert_called_once()
        old_client.disconnect.assert_called_once()
        assert observer_plugin._mqtt_client is None
        assert observer_plugin._connected_mqtt is False

    def test_double_attach_subscribes_once(
        self, shared_observer_plugin, mock_app,
    ):
        """Two racing callers must not both subscribe to RX_LOG_DATA."""
        shared_observer_plugin._detach_from_gateway()

        mock_gw = MagicMock()
        mock_gw.get_status.return_value = {
            "connected": True, "firmware": "1.0", "model": "X",
        }
        mock_mc = MagicMock()
        mock_mc.self_info = {"public_key": "aa" * 32}
        mock_mc.subscribe.return_value = MagicMock()
        mock_gw.get_device_handle.return_value = mock_mc
        mock_gw.get_async_loop.return_value = MagicMock()
        mock_app.get_plugin.return_value = mock_gw

        shared_observer_plugin._try_attach_to_gateway()
        shared_observer_plugin._try_attach_to_gateway()

        assert mock_mc.subscribe.call_count == 1

    def test_stop_unsubscribes_gateway_handlers_in_shared_mode(
        self, shared_observer_plugin,
    ):
        shared_observer_plugin.event_bus.unsubscribe_all.reset_mock()

        shared_observer_plugin.stop()

        unsub_targets = {
            call.args[0] for call in
            shared_observer_plugin.event_bus.unsubscribe_all.call_args_list
        }
        assert shared_observer_plugin._on_gateway_connected in unsub_targets
        assert shared_observer_plugin._on_gateway_disconnected in unsub_targets

    def test_stop_does_not_unsubscribe_in_standalone_mode(self, observer_plugin):
        observer_plugin.event_bus.unsubscribe_all.reset_mock()

        observer_plugin.stop()

        observer_plugin.event_bus.unsubscribe_all.assert_not_called()

    def test_uptime_is_seconds_since_start(self, observer_plugin):
        observer_plugin._public_key = "aa" * 32
        observer_plugin._connected_mqtt = True
        observer_plugin._start_time = time.time() - 42
        mock_client = MagicMock()
        observer_plugin._mqtt_client = mock_client

        observer_plugin._publish_status()

        status = json.loads(mock_client.publish.call_args[0][1])
        # Uptime is seconds since start, not a wall-clock epoch.
        assert 40 <= status["uptime"] <= 60
        assert status["uptime"] < 1_000_000

    def test_packet_json_uses_recv_time_for_all_time_fields(self, observer_plugin):
        observer_plugin._public_key = "aa" * 32
        # 2024-04-23 00:00:00 UTC
        recv_time = 1713830400
        payload = {
            "recv_time": recv_time,
            "snr": 0, "rssi": 0,
            "raw_hex": "", "payload_length": 0,
            "route_typename": "FLOOD", "payload_type": 0,
            "path_len": 0, "path": "", "pkt_hash": 0,
        }
        result = observer_plugin._build_packet_json(payload)

        from datetime import datetime, timezone
        expected = datetime.fromtimestamp(recv_time, tz=timezone.utc)
        assert result["timestamp"] == expected.isoformat()
        assert result["time"] == expected.strftime("%H:%M:%S")
        assert result["date"] == expected.strftime("%d/%m/%Y")

    def test_refresh_jwt_does_not_call_username_pw_set(self, observer_plugin):
        """_refresh_jwt_if_needed must not call client.username_pw_set anymore.

        paho's username_pw_set only takes effect on the next connect, so doing
        this mid-session is misleading. Expiry is handled by clean reconnect.
        """
        observer_plugin._public_key = "aa" * 32
        observer_plugin._signing_mode = "device"
        observer_plugin._mc = MagicMock()
        observer_plugin._loop = MagicMock()
        observer_plugin._loop.is_running.return_value = True

        mock_client = MagicMock()
        observer_plugin._mqtt_client = mock_client
        observer_plugin._jwt_token = None  # force refresh path

        sig_event = _MockEvent(
            type=_MockEventType.SIGNATURE,
            payload={"signature": b"\x00" * 64},
        )
        with patch.object(observer_plugin, "_run_async", return_value=sig_event):
            observer_plugin._refresh_jwt_if_needed()

        assert observer_plugin._jwt_token is not None
        mock_client.username_pw_set.assert_not_called()

    def test_publish_packet_does_not_refresh_jwt(self, observer_plugin):
        """_publish_packet must not refresh the JWT per publish — that's the loop's job."""
        observer_plugin._public_key = "aa" * 32
        observer_plugin._connected_mqtt = True
        mock_client = MagicMock()
        mock_client.publish.return_value = MagicMock(rc=0)
        observer_plugin._mqtt_client = mock_client

        with patch.object(observer_plugin, "_refresh_jwt_if_needed") as mock_refresh:
            observer_plugin._publish_packet({"origin": "AUS"})

        mock_refresh.assert_not_called()
        mock_client.publish.assert_called_once()

    def test_jwt_expiry_triggers_clean_reconnect(self, observer_plugin):
        """Drain loop must cleanly reconnect when JWT is near expiry."""
        import threading
        observer_plugin._public_key = "aa" * 32
        observer_plugin._jwt_token = "expiring.token"
        observer_plugin._jwt_expires = time.time() + 10  # inside 300s buffer
        observer_plugin._active = True
        observer_plugin._connected_mqtt = True

        mock_client = MagicMock()
        mock_client.is_connected.return_value = True
        observer_plugin._mqtt_client = mock_client
        observer_plugin._packet_queue = queue.Queue(maxsize=10)

        disconnect_called = threading.Event()
        real_disconnect = observer_plugin._disconnect_mqtt

        def spy_disconnect():
            real_disconnect()
            disconnect_called.set()

        with patch.object(observer_plugin, "_disconnect_mqtt", side_effect=spy_disconnect), \
             patch.object(observer_plugin, "_connect_mqtt",
                          side_effect=lambda: setattr(observer_plugin, "_active", False)):
            loop_thread = threading.Thread(
                target=observer_plugin._mqtt_connection_loop, daemon=True,
            )
            loop_thread.start()
            assert disconnect_called.wait(timeout=3), (
                "JWT expiry check did not trigger _disconnect_mqtt"
            )
            loop_thread.join(timeout=3)

        assert not loop_thread.is_alive()

    def test_jwt_valid_does_not_trigger_reconnect(self, observer_plugin):
        """Drain loop must not reconnect when JWT has plenty of lifetime left."""
        import threading
        observer_plugin._public_key = "aa" * 32
        observer_plugin._jwt_token = "fresh.token"
        observer_plugin._jwt_expires = time.time() + 3600  # full hour
        observer_plugin._active = True
        observer_plugin._connected_mqtt = True

        mock_client = MagicMock()
        mock_client.is_connected.return_value = True
        observer_plugin._mqtt_client = mock_client
        observer_plugin._packet_queue = queue.Queue(maxsize=10)

        with patch.object(observer_plugin, "_disconnect_mqtt") as mock_disc, \
             patch.object(observer_plugin, "_connect_mqtt"):
            loop_thread = threading.Thread(
                target=observer_plugin._mqtt_connection_loop, daemon=True,
            )
            loop_thread.start()
            # Let the loop do ~2 drain iterations (each ~1s due to get-timeout).
            time.sleep(2.2)
            observer_plugin._active = False
            loop_thread.join(timeout=3)

        mock_disc.assert_not_called()

    def test_handle_mqtt_connect_publishes_lwt_before_setting_flag(self, observer_plugin):
        """LWT online publish must land before _connected_mqtt flips to True."""
        observer_plugin._connected_mqtt = False

        flag_states_during_publish = []

        mock_client = MagicMock()

        def publish_side_effect(*args, **kwargs):
            flag_states_during_publish.append(observer_plugin._connected_mqtt)
            return MagicMock(rc=0)

        mock_client.publish.side_effect = publish_side_effect

        _mock_paho_client.MQTT_ERR_SUCCESS = 0

        observer_plugin._handle_mqtt_connect(
            mock_client, rc=0,
            broker="broker", port=443, iata="AUS",
            status_topic="meshcore/AUS/key/status",
        )

        mock_client.publish.assert_called_once()
        assert flag_states_during_publish == [False]
        assert observer_plugin._connected_mqtt is True

    def test_handle_mqtt_connect_ignores_failure(self, observer_plugin):
        observer_plugin._connected_mqtt = False
        mock_client = MagicMock()

        observer_plugin._handle_mqtt_connect(
            mock_client, rc=1,  # non-zero → failure
            broker="b", port=443, iata="AUS", status_topic="t",
        )

        mock_client.publish.assert_not_called()
        assert observer_plugin._connected_mqtt is False

    def test_ws_ping_interval_rejects_low_value(self, mock_app, obs_config):
        obs_config["ws_ping_interval"] = 5
        with pytest.raises(ValueError, match="ws_ping_interval"):
            _make_plugin_no_start(mock_app, obs_config)

    def test_ws_ping_interval_accepts_valid(self, mock_app, obs_config):
        obs_config["ws_ping_interval"] = 15
        plugin = _make_plugin_no_start(mock_app, obs_config)
        assert plugin.plugin_name == "meshcore_observer"

    def test_queue_double_full_counts_once(self, mock_app, obs_config):
        """If the slot refills between get and put, failure is counted once."""
        plugin = _make_plugin_no_start(mock_app, obs_config)
        plugin._active = True
        plugin._lock = __import__("threading").Lock()
        plugin._packets_captured = 0
        plugin._packets_failed = 0
        plugin._last_packet_time = None
        plugin._public_key = "aa" * 32

        full_queue = MagicMock()
        full_queue.put_nowait.side_effect = queue.Full
        full_queue.get_nowait.return_value = {"dropped": True}
        plugin._packet_queue = full_queue

        event = _MockEvent(
            type=_MockEventType.RX_LOG_DATA,
            payload={
                "recv_time": int(time.time()),
                "snr": 0, "rssi": 0,
                "raw_hex": "", "payload_length": 0,
                "route_typename": "FLOOD", "payload_type": 0,
                "path_len": 0, "path": "", "pkt_hash": 0,
            },
        )

        # Must not raise even though both puts hit queue.Full.
        plugin._handle_rx_log(event)

        assert plugin._packets_captured == 1
        assert plugin._packets_failed == 1


# ---------------------------------------------------------------------------
# WebSocket keepalive patch
# ---------------------------------------------------------------------------

class TestWebSocketPatch:

    def test_patch_adds_ping_method(self):
        from reticulumpi.builtin_plugins.meshcore_observer import _patch_paho_websocket
        _patch_paho_websocket()
        from paho.mqtt.client import _WebsocketWrapper
        assert hasattr(_WebsocketWrapper, "ping")
        assert callable(_WebsocketWrapper.ping)

    def test_patch_is_idempotent(self):
        from reticulumpi.builtin_plugins.meshcore_observer import _patch_paho_websocket
        _patch_paho_websocket()
        _patch_paho_websocket()
        from paho.mqtt.client import _WebsocketWrapper
        assert hasattr(_WebsocketWrapper, "ping")

    def test_ws_ping_thread_starts_on_connect(self, observer_plugin):
        observer_plugin._connected_mqtt = False
        observer_plugin._ws_ping_thread = None
        mock_client = MagicMock()
        mock_client.publish.return_value = MagicMock(rc=0)
        observer_plugin._mqtt_client = mock_client

        _mock_paho_client.MQTT_ERR_SUCCESS = 0

        observer_plugin._handle_mqtt_connect(
            mock_client, rc=0,
            broker="broker", port=443, iata="AUS",
            status_topic="meshcore/AUS/key/status",
        )

        assert observer_plugin._ws_ping_thread is not None
        assert observer_plugin._ws_ping_thread.is_alive()
        observer_plugin._ws_ping_stop.set()
        observer_plugin._ws_ping_thread.join(timeout=2)

    def test_ws_ping_thread_stops_on_disconnect(self, observer_plugin):
        observer_plugin._ws_ping_stop = __import__("threading").Event()
        observer_plugin._ws_ping_thread = None
        mock_client = MagicMock()
        mock_client.publish.return_value = MagicMock(rc=0)
        observer_plugin._mqtt_client = mock_client

        _mock_paho_client.MQTT_ERR_SUCCESS = 0

        observer_plugin._handle_mqtt_connect(
            mock_client, rc=0,
            broker="broker", port=443, iata="AUS",
            status_topic="meshcore/AUS/key/status",
        )
        assert observer_plugin._ws_ping_thread is not None

        observer_plugin._disconnect_mqtt()

        assert observer_plugin._ws_ping_thread is None
        assert observer_plugin._connected_mqtt is False

    def test_get_status_includes_ws_ping(self, observer_plugin):
        status = observer_plugin.get_status()
        assert "ws_ping_active" in status

    def test_ws_ping_not_started_for_tcp_transport(self, mock_app):
        config = {
            "enabled": True,
            "connection_type": "serial",
            "serial_port": "/dev/ttyUSB0",
            "serial_baud": 115200,
            "iata": "AUS",
            "mqtt_broker": "test",
            "mqtt_port": 1883,
            "mqtt_transport": "tcp",
            "health_check_interval": 30,
            "reconnect_delay": 5,
            "max_reconnect_attempts": 3,
            "packet_queue_size": 100,
        }
        gen = _make_started_plugin(mock_app, config)
        plugin = next(gen)
        try:
            mock_client = MagicMock()
            mock_client.publish.return_value = MagicMock(rc=0)
            plugin._mqtt_client = mock_client

            _mock_paho_client.MQTT_ERR_SUCCESS = 0

            plugin._handle_mqtt_connect(
                mock_client, rc=0,
                broker="test", port=1883, iata="AUS",
                status_topic="meshcore/AUS/key/status",
            )

            assert plugin._ws_ping_thread is None
        finally:
            plugin._active = False
            plugin._loop_ready.set()
            if plugin._loop and plugin._loop.is_running():
                plugin._loop.call_soon_threadsafe(plugin._loop.stop)
            plugin._join_threads(timeout=2)
