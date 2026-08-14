"""Tests for the MeshCore Observer plugin."""

from __future__ import annotations

import asyncio
import hashlib
import json
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from reticulumpi.serial_devices import (
    SerialDeviceBusyError,
    SerialDeviceChangedError,
    SerialDeviceIdentityError,
)


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
    with patch.dict(
        sys.modules,
        {
            "meshcore": _mock_meshcore,
            "meshcore.events": _mock_meshcore_events,
            "paho": _mock_paho,
            "paho.mqtt": _mock_paho,
            "paho.mqtt.client": _mock_paho_client,
        },
    ):
        _mock_meshcore.MeshCore = MagicMock()
        _mock_meshcore_events.EventType = _MockEventType
        registry = MagicMock()
        lease = MagicMock()
        lease.revalidate.return_value = lease.identity
        registry.claim.return_value = lease
        with patch(
            "reticulumpi.builtin_plugins.meshcore_observer.serial_device_registry",
            registry,
        ):
            yield registry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def obs_config():
    """Base config dict for the meshcore_observer plugin (standalone)."""
    return {
        "enabled": True,
        "connection_type": "serial",
        "serial_port": "/dev/meshcore-observer",
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


def _make_serial_ownership_plugin(mock_app, config):
    """Construct only the state needed for serial lease acquisition."""
    plugin = _make_plugin_no_start(mock_app, config)
    plugin._lock = threading.Lock()
    plugin._disconnect_lock = threading.Lock()
    plugin._active = True
    plugin._serial_device_lease = None
    plugin._serial_reopen_blocked = False
    plugin._connection_generation = 0
    plugin._open_attempt = None
    plugin._teardown_attempt = None
    plugin._device_teardown_timeout = 0.2
    plugin._shared_attach_in_progress = False
    plugin._gateway_handlers_subscribed = False
    plugin._shared_mode = bool(config.get("use_gateway_device", False))
    plugin._loop = None
    plugin._mc = None
    plugin._connected_device = False
    plugin._subscriptions = []
    plugin._last_device_response_monotonic = 0.0
    plugin._last_device_response_time = None
    return plugin


def _make_health_plugin(mock_app, config, mc=None):
    plugin = _make_plugin_no_start(mock_app, config)
    plugin._lock = threading.Lock()
    plugin._active = True
    plugin._mc = mc
    plugin._connected_device = mc is not None
    plugin._connection_generation = 1
    plugin._device_info = {}
    plugin._health_query_timeout = float(config.get("health_query_timeout", 5))
    plugin._health_response_max_age = float(config.get("health_response_max_age", 60))
    plugin._health_query_failures = 0
    plugin._last_device_response_monotonic = 0.0
    plugin._last_device_response_time = None
    plugin._run_async = MagicMock()
    return plugin


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
    mock_app.get_plugin.return_value = None
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

    @pytest.mark.parametrize("serial_port", ["/dev/ttyUSB0", "/dev/ttyACM17"])
    def test_rejects_kernel_assigned_serial_indexes(
        self,
        mock_app,
        obs_config,
        serial_port,
    ):
        obs_config["serial_port"] = serial_port
        with pytest.raises(ValueError, match="stable serial device path"):
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

    @pytest.mark.parametrize("value", [0, -1, 31, True, "5"])
    def test_rejects_bad_health_query_timeout(self, mock_app, obs_config, value):
        obs_config["health_query_timeout"] = value
        with pytest.raises(ValueError, match="health_query_timeout"):
            _make_plugin_no_start(mock_app, obs_config)

    @pytest.mark.parametrize("value", [-1, 3601, True, "60"])
    def test_rejects_bad_health_response_max_age(self, mock_app, obs_config, value):
        obs_config["health_response_max_age"] = value
        with pytest.raises(ValueError, match="health_response_max_age"):
            _make_plugin_no_start(mock_app, obs_config)

    @pytest.mark.parametrize("value", [0, -1, True, 1.5, "3"])
    def test_rejects_bad_health_failure_threshold(self, mock_app, obs_config, value):
        obs_config["health_failure_threshold"] = value
        with pytest.raises(ValueError, match="health_failure_threshold"):
            _make_plugin_no_start(mock_app, obs_config)

    def test_tcp_requires_host(self, mock_app, obs_config):
        obs_config["connection_type"] = "tcp"
        obs_config["tcp_host"] = ""
        with pytest.raises(ValueError, match="tcp_host"):
            _make_plugin_no_start(mock_app, obs_config)

    def test_valid_tcp_config(self, mock_app, obs_config):
        obs_config["connection_type"] = "tcp"
        obs_config["serial_port"] = "/dev/ttyUSB0"
        obs_config["tcp_host"] = "192.168.1.100"
        obs_config["tcp_port"] = 5000
        plugin = _make_plugin_no_start(mock_app, obs_config)
        assert plugin.plugin_name == "meshcore_observer"

    def test_shared_mode_skips_serial_validation(self, mock_app, shared_config):
        shared_config["serial_port"] = "/dev/ttyACM0"
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
            "active",
            "mode",
            "device_connected",
            "mqtt_connected",
            "public_key",
            "iata",
            "mqtt_broker",
            "packets_captured",
            "packets_published",
            "packets_failed",
            "last_packet_time",
            "last_mqtt_publish_time",
            "connect_count",
            "reconnect_failures",
            "signing_mode",
            "firmware",
            "model",
            "queue_depth",
        }
        assert expected_fields.issubset(set(status.keys()))

    def test_shared_stop_contains_gateway_handler_unsubscribe_failure(
        self,
        mock_app,
        shared_config,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, shared_config)
        plugin._active = True
        plugin._gateway_handlers_subscribed = True
        plugin._detach_from_gateway = MagicMock(return_value=True)
        plugin._disconnect_mqtt = MagicMock()
        plugin._join_threads = MagicMock()
        mock_app.event_bus.unsubscribe_all.side_effect = RuntimeError("handler registry failed")

        plugin.stop()

        assert plugin._active is False
        plugin._detach_from_gateway.assert_called_once_with()
        plugin._disconnect_mqtt.assert_called_once_with()

    def test_standalone_stop_contains_unexpected_disconnect_failure(
        self,
        mock_app,
        obs_config,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        plugin._active = True
        plugin._disconnect_device = MagicMock(side_effect=RuntimeError("SDK teardown failed"))
        plugin._disconnect_mqtt = MagicMock()
        plugin._join_threads = MagicMock()

        plugin.stop()

        assert plugin._active is False
        plugin._disconnect_mqtt.assert_called_once_with()


class TestAsyncConnectionHelpers:
    def test_run_async_timeout_cancels_submitted_future(self, mock_app, obs_config):
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        plugin._loop = MagicMock()
        plugin._loop.is_running.return_value = True
        future = MagicMock()
        future.result.side_effect = TimeoutError("command timed out")
        awaitable = MagicMock()

        with patch(
            "reticulumpi.builtin_plugins.meshcore_observer.asyncio.run_coroutine_threadsafe",
            return_value=future,
        ):
            with pytest.raises(TimeoutError, match="command timed out"):
                plugin._run_async(awaitable, timeout=0.01)

        future.cancel.assert_called_once_with()

    def test_close_awaitable_calls_available_close(self):
        awaitable = MagicMock()

        from reticulumpi.builtin_plugins.meshcore_observer import MeshCoreObserver

        MeshCoreObserver._close_awaitable(awaitable)

        awaitable.close.assert_called_once_with()

    def test_invalidate_generation_cancels_inflight_open(self, mock_app, obs_config):
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        future = MagicMock()
        attempt = {"abandoned": False, "future": future}
        plugin._open_attempt = attempt
        generation = plugin._connection_generation

        plugin._invalidate_connection_generation()

        assert plugin._connection_generation == generation + 1
        assert attempt["abandoned"] is True
        assert plugin._serial_reopen_blocked is True
        future.cancel.assert_called_once_with()

    def test_async_close_unpublished_client_reports_disconnect_failure(
        self,
        mock_app,
        obs_config,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        mc = MagicMock()
        mc.disconnect = AsyncMock(side_effect=RuntimeError("USB disappeared"))

        assert asyncio.run(plugin._async_close_unpublished_mc(mc)) is False

    def test_tracked_open_rejects_stale_generation_and_closes_coroutine(
        self,
        mock_app,
        obs_config,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        plugin._loop = MagicMock()
        plugin._loop.is_running.return_value = True

        async def open_client():
            return MagicMock()

        coro = open_client()
        with pytest.raises(RuntimeError, match="generation is no longer current"):
            plugin._run_tracked_open(coro, generation=plugin._connection_generation + 1, timeout=1)

    def test_tracked_open_submission_failure_clears_attempt(
        self,
        mock_app,
        obs_config,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        plugin._loop = MagicMock()
        plugin._loop.is_running.return_value = True
        generation = plugin._begin_connection_generation()

        async def open_client():
            return MagicMock()

        with (
            patch(
                "reticulumpi.builtin_plugins.meshcore_observer.asyncio.run_coroutine_threadsafe",
                side_effect=RuntimeError("loop rejected task"),
            ),
            pytest.raises(RuntimeError, match="loop rejected task"),
        ):
            plugin._run_tracked_open(open_client(), generation, timeout=1)

        assert plugin._open_attempt is None
        assert plugin._serial_reopen_blocked is False


# ---------------------------------------------------------------------------
# Standalone serial ownership
# ---------------------------------------------------------------------------


class TestSerialOwnership:
    def test_claims_and_revalidates_before_serial_open(
        self,
        mock_app,
        obs_config,
        _patch_modules,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        calls = []
        lease = MagicMock()
        _patch_modules.claim.side_effect = lambda path, owner: (
            calls.append(("claim", path, owner)) or lease
        )
        lease.revalidate.side_effect = lambda: calls.append(("revalidate",))
        plugin._run_async = MagicMock(return_value=None)

        with patch("meshcore.MeshCore.create_serial") as create_serial:
            create_serial.side_effect = lambda *args, **kwargs: calls.append(("open",)) or object()
            with pytest.raises(ConnectionError, match="did not respond"):
                plugin._connect_device()

        assert calls[:3] == [
            ("claim", "/dev/meshcore-observer", "meshcore_observer"),
            ("revalidate",),
            ("open",),
        ]
        create_serial.assert_called_once_with(
            "/dev/meshcore-observer",
            115200,
            auto_reconnect=False,
        )
        assert plugin._serial_device_lease is lease

    def test_default_serial_port_uses_stable_observer_alias(
        self,
        mock_app,
        obs_config,
        _patch_modules,
    ):
        obs_config.pop("serial_port")
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        plugin._run_async = MagicMock(return_value=None)

        with patch("meshcore.MeshCore.create_serial", return_value=object()) as create_serial:
            with pytest.raises(ConnectionError, match="did not respond"):
                plugin._connect_device()

        _patch_modules.claim.assert_called_once_with(
            "/dev/meshcore-observer",
            "meshcore_observer",
        )
        create_serial.assert_called_once_with(
            "/dev/meshcore-observer",
            115200,
            auto_reconnect=False,
        )

    def test_claim_conflict_prevents_serial_open(
        self,
        mock_app,
        obs_config,
        _patch_modules,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        _patch_modules.claim.side_effect = SerialDeviceBusyError(
            "/dev/meshcore-observer",
            ("meshcore_gateway",),
            MagicMock(),
            external=False,
        )
        plugin._run_async = MagicMock()

        with patch("meshcore.MeshCore.create_serial") as create_serial:
            with pytest.raises(SerialDeviceBusyError, match="meshcore_gateway"):
                plugin._connect_device()

        create_serial.assert_not_called()
        plugin._run_async.assert_not_called()
        assert plugin._serial_device_lease is None

    def test_reconnect_revalidates_existing_lease_without_reclaim(
        self,
        mock_app,
        obs_config,
        _patch_modules,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        lease = MagicMock()
        plugin._serial_device_lease = lease

        assert plugin._ensure_serial_device_lease("/dev/meshcore-observer") is lease

        lease.revalidate.assert_called_once_with()
        _patch_modules.claim.assert_not_called()

    def test_hotplug_identity_change_releases_and_reclaims(
        self,
        mock_app,
        obs_config,
        _patch_modules,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        old_lease = MagicMock()
        old_lease.revalidate.side_effect = SerialDeviceChangedError(
            "/dev/meshcore-observer",
            MagicMock(),
            MagicMock(),
        )
        new_lease = MagicMock()
        _patch_modules.claim.return_value = new_lease
        plugin._serial_device_lease = old_lease

        assert plugin._ensure_serial_device_lease("/dev/meshcore-observer") is new_lease

        old_lease.release.assert_called_once_with()
        _patch_modules.claim.assert_called_once_with(
            "/dev/meshcore-observer",
            "meshcore_observer",
        )
        new_lease.revalidate.assert_called_once_with()
        assert plugin._serial_device_lease is new_lease

    def test_absent_hotplug_device_retains_existing_lease_and_does_not_open(
        self,
        mock_app,
        obs_config,
        _patch_modules,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        lease = MagicMock()
        lease.revalidate.side_effect = SerialDeviceIdentityError("device absent")
        plugin._serial_device_lease = lease

        with patch("meshcore.MeshCore.create_serial") as create_serial:
            with pytest.raises(SerialDeviceIdentityError, match="device absent"):
                plugin._connect_device()

        lease.release.assert_not_called()
        _patch_modules.claim.assert_not_called()
        create_serial.assert_not_called()
        assert plugin._serial_device_lease is lease

    def test_new_claim_is_released_when_immediate_revalidation_fails(
        self,
        mock_app,
        obs_config,
        _patch_modules,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        lease = MagicMock()
        lease.revalidate.side_effect = SerialDeviceIdentityError("changed before open")
        _patch_modules.claim.return_value = lease

        with patch("meshcore.MeshCore.create_serial") as create_serial:
            with pytest.raises(SerialDeviceIdentityError, match="changed before open"):
                plugin._connect_device()

        lease.release.assert_called_once_with()
        create_serial.assert_not_called()
        assert plugin._serial_device_lease is None

    def test_stop_releases_lease_after_threads_join(
        self,
        mock_app,
        obs_config,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        order = []
        lease = MagicMock()
        lease.release.side_effect = lambda: order.append("release")
        plugin._serial_device_lease = lease
        plugin._active = True
        plugin._loop = None
        plugin._mc = None
        plugin._disconnect_mqtt = MagicMock()
        plugin._join_threads = MagicMock(side_effect=lambda: order.append("join"))

        plugin.stop()

        assert order == ["join", "release"]
        assert plugin._serial_device_lease is None

    def test_stop_retains_lease_when_disconnect_cannot_be_proven(
        self,
        mock_app,
        obs_config,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        lease = MagicMock()
        plugin._serial_device_lease = lease
        plugin._active = True
        plugin._loop = None
        plugin._mc = object()
        plugin._disconnect_mqtt = MagicMock()
        plugin._join_threads = MagicMock()

        plugin.stop()

        lease.release.assert_not_called()
        assert plugin._serial_device_lease is lease

    def test_stop_retains_lease_while_managed_worker_is_alive(
        self,
        mock_app,
        obs_config,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        lease = MagicMock()
        live_thread = MagicMock()
        live_thread.name = "observer-device"
        live_thread.is_alive.return_value = True
        plugin._serial_device_lease = lease
        plugin._active = True
        plugin._loop = None
        plugin._mc = None
        plugin._disconnect_mqtt = MagicMock()
        plugin._threads = [live_thread]
        plugin._join_threads = MagicMock()

        plugin.stop()

        lease.release.assert_not_called()
        assert plugin._serial_device_lease is lease

    def test_uncertain_disconnect_retains_handle_and_blocks_reopen(
        self,
        mock_app,
        obs_config,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        mc = MagicMock()
        mc.disconnect.return_value = object()
        plugin._mc = mc
        plugin._connected_device = False
        plugin._loop = MagicMock()
        plugin._loop.is_running.return_value = True
        plugin._run_async = MagicMock(side_effect=TimeoutError("disconnect timed out"))

        assert plugin._disconnect_device() is False

        assert plugin._mc is mc
        assert plugin._serial_reopen_blocked is True

    def test_connection_loop_never_opens_behind_uncertain_stale_handle(
        self,
        mock_app,
        obs_config,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        plugin._mc = MagicMock()
        plugin._connected_device = False
        plugin._active = True
        plugin._reconnect_failures = 0
        plugin._disconnect_device = MagicMock(return_value=False)
        plugin._connect_device = MagicMock()
        plugin._sleep_while_active = MagicMock(
            side_effect=lambda _delay: setattr(plugin, "_active", False)
        )

        plugin._device_connection_loop()

        plugin._disconnect_device.assert_called_once_with()
        plugin._connect_device.assert_not_called()

    def test_failed_initialization_keeps_candidate_for_teardown(
        self,
        mock_app,
        obs_config,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        mc = MagicMock()
        mc.commands = MagicMock()
        mc.commands.set_time.return_value = object()
        plugin._active = True
        plugin._health_query_timeout = 5
        plugin._ensure_serial_device_lease = MagicMock()
        plugin._run_async = MagicMock(side_effect=[mc, RuntimeError("initialization failed")])

        with patch("meshcore.MeshCore.create_serial", return_value=object()):
            with pytest.raises(RuntimeError, match="initialization failed"):
                plugin._connect_device()

        assert plugin._mc is mc
        assert plugin._connected_device is False

    def test_stop_generation_fences_in_progress_setup(self, mock_app, obs_config):
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        plugin._health_query_timeout = 5
        plugin._ensure_serial_device_lease = MagicMock()
        mc = MagicMock()
        mc.commands = MagicMock()
        mc.commands.set_time.return_value = object()
        call_count = 0

        def run_async(awaitable, timeout=15):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mc
            plugin._active = False
            plugin._invalidate_connection_generation()
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            return None

        plugin._run_async = MagicMock(side_effect=run_async)
        with patch("meshcore.MeshCore.create_serial", return_value=object()):
            with pytest.raises(RuntimeError, match="stale"):
                plugin._connect_device()

        assert plugin._mc is mc
        assert plugin._connected_device is False
        mc.subscribe.assert_not_called()

    def test_tcp_mode_neither_claims_nor_releases_serial_device(
        self,
        mock_app,
        obs_config,
        _patch_modules,
    ):
        obs_config.update(connection_type="tcp", tcp_host="127.0.0.1", tcp_port=5000)
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        plugin._run_async = MagicMock(return_value=None)

        with patch("meshcore.MeshCore.create_tcp", return_value=object()) as create_tcp:
            with pytest.raises(ConnectionError, match="did not respond"):
                plugin._connect_device()

        create_tcp.assert_called_once()
        _patch_modules.claim.assert_not_called()

        plugin._active = True
        plugin._loop = None
        plugin._mc = None
        plugin._disconnect_mqtt = MagicMock()
        plugin._join_threads = MagicMock()
        with patch.object(plugin, "_release_serial_device_lease") as release:
            plugin.stop()
        release.assert_not_called()

    def test_shared_mode_borrows_without_claim_release_or_disconnect(
        self,
        mock_app,
        shared_config,
        _patch_modules,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, shared_config)
        plugin._connected_device = False
        plugin._mc = None
        plugin._subscriptions = []
        plugin._connect_count = 0
        plugin._public_key = ""
        plugin._device_info = {}
        plugin._loop = None

        gateway = MagicMock()
        gateway.get_status.return_value = {
            "connected": True,
            "firmware": "1.0",
            "model": "RAK4631",
        }
        borrowed_mc = MagicMock()
        borrowed_mc.self_info = {"public_key": "aa" * 32}
        borrowed_mc.subscribe.return_value = MagicMock()
        gateway.get_device_handle.return_value = borrowed_mc
        gateway.get_async_loop.return_value = MagicMock()
        plugin.get_ready_plugin = MagicMock(return_value=gateway)
        plugin._probe_signing = MagicMock()

        plugin._try_attach_to_gateway()

        assert plugin._mc is borrowed_mc
        _patch_modules.claim.assert_not_called()

        plugin._active = True
        plugin._disconnect_mqtt = MagicMock()
        plugin._join_threads = MagicMock()
        with patch.object(plugin, "_release_serial_device_lease") as release:
            plugin.stop()

        release.assert_not_called()
        borrowed_mc.disconnect.assert_not_called()
        borrowed_mc.unsubscribe.assert_called_once()

    def test_cancellation_resistant_open_is_closed_before_reopen(
        self,
        mock_app,
        obs_config,
    ):
        plugin = _make_plugin_no_start(mock_app, obs_config)
        with (
            patch.object(plugin, "_device_connection_loop"),
            patch.object(plugin, "_mqtt_connection_loop"),
        ):
            plugin.start()

        late_mc = MagicMock()
        late_mc.disconnect = AsyncMock()
        started = threading.Event()
        allow_return = threading.Event()

        async def cancellation_resistant_open():
            started.set()
            try:
                while not allow_return.is_set():
                    await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                while not allow_return.is_set():
                    await asyncio.sleep(0.005)
            return late_mc

        generation = plugin._begin_connection_generation()
        with pytest.raises(TimeoutError, match="create operation timed out"):
            plugin._run_tracked_open(cancellation_resistant_open(), generation, 0.03)

        assert started.is_set()
        assert plugin._open_attempt is not None
        with pytest.raises(RuntimeError, match="not quiescent"):
            plugin._begin_connection_generation()

        allow_return.set()
        deadline = time.monotonic() + 2
        quiescent = False
        while time.monotonic() < deadline:
            with plugin._lock:
                quiescent = plugin._open_attempt is None and plugin._mc is None
            if quiescent:
                break
            time.sleep(0.01)

        assert quiescent is True
        late_mc.disconnect.assert_awaited_once()
        plugin.stop()

    def test_tracked_open_publishes_current_client_before_return(
        self,
        mock_app,
        obs_config,
    ):
        plugin = _make_plugin_no_start(mock_app, obs_config)
        with (
            patch.object(plugin, "_device_connection_loop"),
            patch.object(plugin, "_mqtt_connection_loop"),
        ):
            plugin.start()

        mc = MagicMock()
        mc.disconnect = AsyncMock()

        async def open_client():
            return mc

        generation = plugin._begin_connection_generation()
        assert plugin._run_tracked_open(open_client(), generation, timeout=1) is mc
        assert plugin._mc is mc
        assert plugin._connected_device is False
        assert plugin._open_attempt is None
        assert plugin._serial_reopen_blocked is False

        plugin.stop()

    def test_successful_standalone_connect_binds_and_fences_callbacks(
        self,
        mock_app,
        obs_config,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        plugin._health_query_timeout = 5
        plugin._connect_count = 0
        plugin._public_key = ""
        plugin._device_info = {}
        plugin._ensure_serial_device_lease = MagicMock()
        plugin._probe_signing = MagicMock()
        plugin._handle_rx_log = MagicMock()

        mc = MagicMock()
        mc.self_info = {"public_key": "ab" * 32}
        mc.commands = MagicMock()
        mc.commands.set_time.return_value = object()
        mc.commands.send_device_query.return_value = object()
        mc.commands.send_appstart.return_value = object()
        mc.subscribe.side_effect = ["rx-sub", "disconnect-sub"]

        def publish_open(awaitable, generation, timeout):
            plugin._mc = mc
            return mc

        plugin._run_tracked_open = MagicMock(side_effect=publish_open)
        plugin._run_async = MagicMock(
            side_effect=[
                None,
                None,
                _MockEvent(_MockEventType.SELF_INFO, {"public_key": "cd" * 32}),
            ]
        )

        with patch("meshcore.MeshCore.create_serial", return_value=object()):
            plugin._connect_device()

        assert plugin._connected_device is True
        assert plugin._public_key == ("AB" * 32)
        assert plugin._subscriptions == ["rx-sub", "disconnect-sub"]
        callbacks = [call.args[1] for call in mc.subscribe.call_args_list]

        asyncio.run(callbacks[0](_MockEvent(_MockEventType.RX_LOG_DATA, {})))
        plugin._handle_rx_log.assert_called_once()
        asyncio.run(callbacks[1](_MockEvent(_MockEventType.DISCONNECTED, {})))
        asyncio.run(callbacks[0](_MockEvent(_MockEventType.RX_LOG_DATA, {})))

        assert plugin._connected_device is False
        plugin._handle_rx_log.assert_called_once()

    def test_disconnect_continues_after_subscription_cleanup_error(
        self,
        mock_app,
        obs_config,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, obs_config)
        plugin._active = True
        plugin._loop = MagicMock()
        plugin._loop.is_running.return_value = True
        plugin._run_async = MagicMock(return_value=None)
        plugin._start_thread = MagicMock(side_effect=lambda target, _name: target())
        plugin._last_device_response_monotonic = 12.0
        plugin._last_device_response_time = 34.0
        mc = MagicMock()
        mc.unsubscribe.side_effect = RuntimeError("subscription already gone")
        plugin._mc = mc
        plugin._connected_device = True
        plugin._subscriptions = ["sub"]

        assert plugin._disconnect_device() is True

        assert plugin._mc is None
        assert plugin._subscriptions == []
        assert plugin._last_device_response_monotonic == 0.0
        assert plugin._last_device_response_time is None


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
            "serial_port": "/dev/meshcore-observer",
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
                "snr": 1.0,
                "rssi": -50,
                "raw_hex": "ab",
                "payload_length": 1,
                "route_typename": "FLOOD",
                "payload_type": 0,
                "path_len": 0,
                "path": "",
                "pkt_hash": 0,
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

    @pytest.mark.parametrize("encoded_as_hex", [False, True], ids=["sdk-bytes", "hex"])
    def test_local_signing_uses_meshcore_expanded_key_format(
        self,
        observer_plugin,
        encoded_as_hex,
    ):
        """Sign the RFC 8032 vector from MeshCore's exact 64-byte key shape."""
        seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        public_key = bytes.fromhex(
            "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
        )
        expected_signature = bytes.fromhex(
            "e5564300c360ac729086e2cc806e828a"
            "84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46b"
            "d25bf5f0595bbe24655141438e7a100b"
        )
        expanded_key = bytearray(hashlib.sha512(seed).digest())
        expanded_key[0] &= 248
        expanded_key[31] &= 63
        expanded_key[31] |= 64
        exported_key = bytes(expanded_key)

        observer_plugin._public_key = public_key.hex()
        observer_plugin._mc = MagicMock()
        private_key_payload = exported_key.hex() if encoded_as_hex else exported_key
        private_key_event = _MockEvent(
            _MockEventType.PRIVATE_KEY,
            {"private_key": private_key_payload},
        )

        with patch.object(observer_plugin, "_run_async", return_value=private_key_event):
            signature = observer_plugin._sign_local(b"")

        assert signature == expected_signature
        assert len(signature) == 64
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, b"")

    def test_local_signing_rejects_expanded_key_for_another_public_key(
        self,
        observer_plugin,
    ):
        seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        expanded_key = bytearray(hashlib.sha512(seed).digest())
        expanded_key[0] &= 248
        expanded_key[31] &= 63
        expanded_key[31] |= 64
        observer_plugin._public_key = "00" * 32
        observer_plugin._mc = MagicMock()
        private_key_event = _MockEvent(
            _MockEventType.PRIVATE_KEY,
            {"private_key": bytes(expanded_key)},
        )

        with patch.object(observer_plugin, "_run_async", return_value=private_key_event):
            with pytest.raises(RuntimeError, match="does not match device public key"):
                observer_plugin._sign_local(b"payload")

    @pytest.mark.parametrize(
        ("private_key", "public_key", "error"),
        [
            ("not-hex", "00" * 32, "private key is not valid hexadecimal"),
            (123, "00" * 32, "private key has an unsupported type"),
            (b"short", "00" * 32, "private key must be 64-byte expanded"),
            (None, "not-hex", "public key is not valid hexadecimal"),
            (None, "00" * 31, "public key must be 32 bytes"),
        ],
        ids=[
            "private-not-hex",
            "private-wrong-type",
            "private-wrong-length",
            "public-not-hex",
            "public-wrong-length",
        ],
    )
    def test_local_signing_rejects_malformed_exported_key_material(
        self,
        observer_plugin,
        private_key,
        public_key,
        error,
    ):
        seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        expanded_key = bytearray(hashlib.sha512(seed).digest())
        expanded_key[0] &= 248
        expanded_key[31] &= 63
        expanded_key[31] |= 64
        if private_key is None:
            private_key = bytes(expanded_key)

        observer_plugin._public_key = public_key
        observer_plugin._mc = MagicMock()
        private_key_event = _MockEvent(
            _MockEventType.PRIVATE_KEY,
            {"private_key": private_key},
        )

        with patch.object(observer_plugin, "_run_async", return_value=private_key_event):
            with pytest.raises(RuntimeError, match=error):
                observer_plugin._sign_local(b"payload")

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

    def test_stale_publish_success_is_not_credited_to_replacement(self, observer_plugin):
        observer_plugin._public_key = "aa" * 32
        observer_plugin._connected_mqtt = True
        old_client = MagicMock()
        replacement = MagicMock()
        observer_plugin._mqtt_client = old_client
        generation = observer_plugin._mqtt_generation

        def replace_during_publish(*_args, **_kwargs):
            with observer_plugin._lock:
                observer_plugin._mqtt_generation = generation + 1
                observer_plugin._mqtt_client = replacement
                observer_plugin._connected_mqtt = True
            return MagicMock(rc=0)

        old_client.publish.side_effect = replace_during_publish

        observer_plugin._publish_packet({"origin": "AUS"})

        old_client.publish.assert_called_once()
        replacement.publish.assert_not_called()
        assert observer_plugin._packets_published == 0
        assert observer_plugin._last_mqtt_publish_time is None

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
            "connected": True,
            "firmware": "1.0",
            "model": "RAK4631",
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
            "meshcore.disconnected",
            {"reason": "test"},
        )

        assert shared_observer_plugin._connected_device is False

    def test_plugin_stopping_detaches_when_gateway(self, shared_observer_plugin):
        shared_observer_plugin._mc = MagicMock()
        shared_observer_plugin._connected_device = True
        shared_observer_plugin._subscriptions = []

        shared_observer_plugin._on_plugin_stopping(
            "plugin.stopping",
            {"name": "meshcore_gateway"},
        )

        assert shared_observer_plugin._connected_device is False

    def test_plugin_stopping_ignores_other_plugins(self, shared_observer_plugin):
        shared_observer_plugin._mc = MagicMock()
        shared_observer_plugin._connected_device = True
        shared_observer_plugin._subscriptions = []

        shared_observer_plugin._on_plugin_stopping(
            "plugin.stopping",
            {"name": "some_other_plugin"},
        )

        assert shared_observer_plugin._connected_device is True


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    def test_active_query_proves_health_even_when_cached_flag_is_false(
        self,
        mock_app,
        obs_config,
    ):
        mock_mc = MagicMock()
        mock_mc.is_connected = False
        plugin = _make_health_plugin(mock_app, obs_config, mock_mc)
        plugin._run_async.return_value = _MockEvent(
            _MockEventType.DEVICE_INFO,
            {"ver": "v1.14.1", "model": "RAK4631"},
        )

        assert plugin._check_health() is True
        plugin._run_async.assert_called_once()
        assert plugin._run_async.call_args.kwargs["timeout"] == 5
        assert plugin._device_info["ver"] == "v1.14.1"
        assert plugin._last_device_response_monotonic > 0

    def test_unhealthy_when_no_mc(self, mock_app, obs_config):
        plugin = _make_health_plugin(mock_app, obs_config)
        assert plugin._check_health() is False

    def test_unhealthy_when_not_published_connected(self, mock_app, obs_config):
        mock_mc = MagicMock()
        plugin = _make_health_plugin(mock_app, obs_config, mock_mc)
        plugin._connected_device = False
        assert plugin._check_health() is False
        plugin._run_async.assert_not_called()

    def test_recent_proven_response_tolerates_one_query_failure(self, mock_app, obs_config):
        obs_config["health_response_max_age"] = 60
        mock_mc = MagicMock()
        plugin = _make_health_plugin(mock_app, obs_config, mock_mc)
        plugin._last_device_response_monotonic = time.monotonic() - 10
        plugin._run_async.side_effect = TimeoutError("query timed out")

        assert plugin._check_health() is True
        assert plugin._health_query_failures == 1

    def test_cached_connected_flag_cannot_mask_stale_device(self, mock_app, obs_config):
        obs_config["health_response_max_age"] = 30
        mock_mc = MagicMock()
        mock_mc.is_connected = True
        plugin = _make_health_plugin(mock_app, obs_config, mock_mc)
        plugin._last_device_response_monotonic = time.monotonic() - 31
        plugin._run_async.side_effect = TimeoutError("query timed out")

        assert plugin._check_health() is False
        assert plugin._health_query_failures == 1

    def test_explicit_command_error_is_not_health(self, mock_app, obs_config):
        obs_config["health_response_max_age"] = 0
        mock_mc = MagicMock()
        plugin = _make_health_plugin(mock_app, obs_config, mock_mc)
        plugin._run_async.return_value = _MockEvent(
            _MockEventType.ERROR,
            {"reason": "radio busy"},
        )

        assert plugin._check_health() is False
        assert plugin._last_device_response_monotonic == 0

    @pytest.mark.parametrize(
        "response",
        [
            {"error": "timeout"},
            {"reason": "radio busy"},
            {"status": "timed out", "ver": "looks-valid"},
            {"state": "busy", "model": "looks-valid"},
            {"unrelated": "mapping"},
        ],
    )
    def test_error_or_unrecognized_direct_mapping_cannot_prove_health(
        self,
        mock_app,
        obs_config,
        response,
    ):
        obs_config["health_response_max_age"] = 0
        mock_mc = MagicMock()
        plugin = _make_health_plugin(mock_app, obs_config, mock_mc)
        plugin._run_async.return_value = response

        assert plugin._check_health() is False
        assert plugin._last_device_response_monotonic == 0

    def test_recognizable_direct_device_mapping_proves_health(self, mock_app, obs_config):
        mock_mc = MagicMock()
        plugin = _make_health_plugin(mock_app, obs_config, mock_mc)
        plugin._run_async.return_value = {"ver": "v1.15.0", "model": "RAK4631"}

        assert plugin._check_health() is True
        assert plugin._device_info["ver"] == "v1.15.0"

    @pytest.mark.parametrize("event_wrapped", [False, True], ids=["direct", "event"])
    def test_sdk_fw_ver_only_device_info_proves_health(
        self,
        mock_app,
        obs_config,
        event_wrapped,
    ):
        """MeshCore 2.3.7 always reports ``fw ver``, including pre-v3 firmware."""
        mock_mc = MagicMock()
        payload = {"fw ver": 2}
        response = _MockEvent(_MockEventType.DEVICE_INFO, payload) if event_wrapped else payload
        plugin = _make_health_plugin(mock_app, obs_config, mock_mc)
        plugin._run_async.return_value = response

        assert plugin._check_health() is True
        assert plugin._device_info == payload

    @pytest.mark.parametrize(
        "payload",
        [
            {"error": "timeout"},
            {"reason": "radio busy"},
            {"status": "timed out", "ver": "looks-valid"},
            {"state": "busy", "model": "looks-valid"},
            {"unrelated": "mapping"},
        ],
    )
    def test_error_or_unrecognized_device_info_event_cannot_prove_health(
        self,
        mock_app,
        obs_config,
        payload,
    ):
        obs_config["health_response_max_age"] = 0
        mock_mc = MagicMock()
        plugin = _make_health_plugin(mock_app, obs_config, mock_mc)
        plugin._run_async.return_value = _MockEvent(_MockEventType.DEVICE_INFO, payload)

        assert plugin._check_health() is False
        assert plugin._last_device_response_monotonic == 0

    @pytest.mark.parametrize(
        "event_type",
        [_MockEventType.DISCONNECTED, _MockEventType.SIGNATURE, _MockEventType.CONNECTED],
    )
    def test_unrelated_event_payload_cannot_prove_health(
        self,
        mock_app,
        obs_config,
        event_type,
    ):
        obs_config["health_response_max_age"] = 0
        mock_mc = MagicMock()
        plugin = _make_health_plugin(mock_app, obs_config, mock_mc)
        plugin._run_async.return_value = _MockEvent(event_type, {"ver": "looks-valid"})

        assert plugin._check_health() is False
        assert plugin._last_device_response_monotonic == 0

    def test_empty_device_info_cannot_prove_health(self, mock_app, obs_config):
        obs_config["health_response_max_age"] = 0
        mock_mc = MagicMock()
        plugin = _make_health_plugin(mock_app, obs_config, mock_mc)
        plugin._run_async.return_value = _MockEvent(_MockEventType.DEVICE_INFO, {})

        assert plugin._check_health() is False

    def test_string_device_info_type_remains_compatible(self, mock_app, obs_config):
        mock_mc = MagicMock()
        plugin = _make_health_plugin(mock_app, obs_config, mock_mc)
        plugin._run_async.return_value = _MockEvent(
            "device_info",
            {"ver": "v1.14.1"},
        )

        assert plugin._check_health() is True


# ---------------------------------------------------------------------------
# Regression tests for the bug-fix pass
# ---------------------------------------------------------------------------


class TestBugFixes:
    def test_shared_stop_does_not_stop_borrowed_loop(
        self,
        shared_observer_plugin,
        mock_app,
    ):
        """In shared mode, stop() must not touch the gateway's loop."""
        shared_observer_plugin._detach_from_gateway()

        borrowed_loop = MagicMock()
        borrowed_loop.is_running.return_value = True

        mock_gw = MagicMock()
        mock_gw.get_status.return_value = {
            "connected": True,
            "firmware": "1.0",
            "model": "X",
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
        self,
        shared_observer_plugin,
        mock_app,
    ):
        """Two racing callers must not both subscribe to RX_LOG_DATA."""
        shared_observer_plugin._detach_from_gateway()

        mock_gw = MagicMock()
        mock_gw.get_status.return_value = {
            "connected": True,
            "firmware": "1.0",
            "model": "X",
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

    def test_old_shared_callback_is_ignored_after_gateway_reconnect(
        self,
        shared_observer_plugin,
        mock_app,
    ):
        shared_observer_plugin._detach_from_gateway()
        shared_observer_plugin._probe_signing = MagicMock()

        first_gateway = MagicMock()
        first_gateway.get_status.return_value = {
            "connected": True,
            "firmware": "1.0",
            "model": "first",
        }
        first = MagicMock()
        first.self_info = {"public_key": "aa" * 32}
        first.subscribe.return_value = MagicMock()
        first_gateway.get_device_handle.return_value = first
        first_gateway.get_async_loop.return_value = MagicMock()
        mock_app.get_plugin.return_value = first_gateway

        shared_observer_plugin._try_attach_to_gateway()
        old_callback = first.subscribe.call_args.args[1]
        assert shared_observer_plugin._detach_from_gateway() is True

        second_gateway = MagicMock()
        second_gateway.get_status.return_value = {
            "connected": True,
            "firmware": "1.1",
            "model": "second",
        }
        second = MagicMock()
        second.self_info = {"public_key": "bb" * 32}
        second.subscribe.return_value = MagicMock()
        second_gateway.get_device_handle.return_value = second
        second_gateway.get_async_loop.return_value = MagicMock()
        mock_app.get_plugin.return_value = second_gateway

        shared_observer_plugin._try_attach_to_gateway()
        shared_observer_plugin._handle_rx_log = MagicMock()
        asyncio.run(old_callback(_MockEvent(_MockEventType.RX_LOG_DATA, {"raw_hex": "aa"})))

        shared_observer_plugin._handle_rx_log.assert_not_called()
        assert shared_observer_plugin._mc is second
        assert shared_observer_plugin._connected_device is True

    def test_shared_attach_failure_rolls_back_subscription(
        self,
        shared_observer_plugin,
        mock_app,
    ):
        shared_observer_plugin._detach_from_gateway()
        gateway = MagicMock()
        gateway.get_status.return_value = {
            "connected": True,
            "firmware": "1.0",
            "model": "X",
        }
        mc = MagicMock()
        mc.self_info = {"public_key": "aa" * 32}
        sub = MagicMock()
        mc.subscribe.return_value = sub
        gateway.get_device_handle.return_value = mc
        gateway.get_async_loop.return_value = MagicMock()
        mock_app.get_plugin.return_value = gateway
        shared_observer_plugin._probe_signing = MagicMock(side_effect=RuntimeError("probe failed"))

        shared_observer_plugin._try_attach_to_gateway()

        mc.unsubscribe.assert_called_once_with(sub)
        assert shared_observer_plugin._mc is None
        assert shared_observer_plugin._subscriptions == []
        assert shared_observer_plugin._connected_device is False

    def test_detach_retains_borrowed_client_when_unsubscribe_fails(
        self,
        mock_app,
        shared_config,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, shared_config)
        mc = MagicMock()
        mc.unsubscribe.side_effect = RuntimeError("gateway subscription wedged")
        plugin._mc = mc
        plugin._connected_device = True
        plugin._subscriptions = ["rx-sub"]
        plugin._loop = MagicMock()
        plugin._start_thread = MagicMock(side_effect=lambda target, _name: target())

        assert plugin._detach_from_gateway() is False

        assert plugin._mc is mc
        assert plugin._subscriptions == ["rx-sub"]
        assert plugin._serial_reopen_blocked is True

    def test_hung_shared_unsubscribe_blocks_reattach(
        self,
        shared_observer_plugin,
        mock_app,
    ):
        shared_observer_plugin._detach_from_gateway()
        shared_observer_plugin._probe_signing = MagicMock()
        shared_observer_plugin._device_teardown_timeout = 0.03

        first_gateway = MagicMock()
        first_gateway.get_status.return_value = {
            "connected": True,
            "firmware": "1.0",
            "model": "first",
        }
        first = MagicMock()
        first.self_info = {"public_key": "aa" * 32}
        first.subscribe.return_value = MagicMock()
        first_gateway.get_device_handle.return_value = first
        first_gateway.get_async_loop.return_value = MagicMock()
        mock_app.get_plugin.return_value = first_gateway
        shared_observer_plugin._try_attach_to_gateway()

        release = threading.Event()
        first.unsubscribe.side_effect = lambda _sub: release.wait()
        assert shared_observer_plugin._detach_from_gateway() is False
        assert shared_observer_plugin._mc is first

        second_gateway = MagicMock()
        second_gateway.get_status.return_value = {"connected": True}
        second = MagicMock()
        second_gateway.get_device_handle.return_value = second
        second_gateway.get_async_loop.return_value = MagicMock()
        mock_app.get_plugin.return_value = second_gateway

        shared_observer_plugin._try_attach_to_gateway()
        second.subscribe.assert_not_called()

        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and shared_observer_plugin._mc is not None:
            time.sleep(0.01)
        assert shared_observer_plugin._mc is None

    def test_gateway_callback_cannot_reattach_after_stop(
        self,
        shared_observer_plugin,
        mock_app,
    ):
        shared_observer_plugin.stop()
        gateway = MagicMock()
        gateway.get_status.return_value = {"connected": True}
        mc = MagicMock()
        gateway.get_device_handle.return_value = mc
        gateway.get_async_loop.return_value = MagicMock()
        mock_app.get_plugin.return_value = gateway

        shared_observer_plugin._on_gateway_connected("meshcore.connected", {})

        mc.subscribe.assert_not_called()
        assert shared_observer_plugin._mc is None

    def test_stop_unsubscribes_gateway_handlers_in_shared_mode(
        self,
        shared_observer_plugin,
    ):
        shared_observer_plugin.event_bus.unsubscribe_all.reset_mock()

        shared_observer_plugin.stop()

        unsub_targets = {
            call.args[0] for call in shared_observer_plugin.event_bus.unsubscribe_all.call_args_list
        }
        assert shared_observer_plugin._on_gateway_connected in unsub_targets
        assert shared_observer_plugin._on_gateway_disconnected in unsub_targets

    def test_stop_does_not_unsubscribe_gateway_handlers_in_standalone_mode(self, observer_plugin):
        observer_plugin.event_bus.unsubscribe_all.reset_mock()

        observer_plugin.stop()

        unsubscribed_callbacks = [
            call.args[0] for call in observer_plugin.event_bus.unsubscribe_all.call_args_list
        ]
        assert observer_plugin._on_gateway_connected not in unsubscribed_callbacks
        assert observer_plugin._on_gateway_disconnected not in unsubscribed_callbacks

    def test_uptime_is_seconds_since_start(self, observer_plugin):
        observer_plugin._public_key = "aa" * 32
        observer_plugin._connected_mqtt = True
        observer_plugin._start_monotonic = 100.0
        mock_client = MagicMock()
        observer_plugin._mqtt_client = mock_client

        with (
            patch(
                "reticulumpi.builtin_plugins.meshcore_observer.time.time",
                return_value=9_999_999_999.0,
            ),
            patch(
                "reticulumpi.builtin_plugins.meshcore_observer.time.monotonic",
                return_value=142.0,
            ),
        ):
            observer_plugin._publish_status()

        status = json.loads(mock_client.publish.call_args[0][1])
        # Uptime is seconds since start, not a wall-clock epoch.
        assert 40 <= status["uptime"] <= 60
        assert status["uptime"] < 1_000_000

    def test_mqtt_connect_deadline_ignores_wall_clock_jump(self, mock_app, obs_config):
        plugin = _make_plugin_no_start(mock_app, obs_config)
        plugin._lock = threading.Lock()
        plugin._mqtt_lifecycle_lock = threading.RLock()
        plugin._active = True
        plugin._mqtt_client = None
        plugin._mqtt_generation = 0
        plugin._connected_mqtt = False
        plugin._ws_ping_stop = threading.Event()
        plugin._ws_ping_thread = None
        plugin._public_key = "aa" * 32
        plugin._jwt_token = "fixture-token"
        plugin._refresh_jwt_if_needed = MagicMock()
        client = MagicMock()
        _mock_paho.Client.return_value = client
        _mock_paho_client.Client.return_value = client
        monotonic_values = iter((0.0, 5.0, 11.0))

        with (
            patch(
                "reticulumpi.builtin_plugins.meshcore_observer.time.time",
                side_effect=AssertionError("MQTT deadline consulted wall time"),
            ),
            patch(
                "reticulumpi.builtin_plugins.meshcore_observer.time.monotonic",
                side_effect=lambda: next(monotonic_values),
            ),
            patch("reticulumpi.builtin_plugins.meshcore_observer.time.sleep"),
            pytest.raises(ConnectionError, match="timed out"),
        ):
            plugin._connect_mqtt()

    def test_stop_during_loop_start_closes_published_candidate(
        self,
        mock_app,
        shared_config,
    ):
        plugin = _make_plugin_no_start(mock_app, shared_config)
        plugin._lock = threading.Lock()
        plugin._mqtt_lifecycle_lock = threading.RLock()
        plugin._active = True
        plugin._shared_mode = True
        plugin._gateway_handlers_subscribed = False
        plugin._detach_from_gateway = MagicMock()
        plugin._join_threads = MagicMock()
        plugin._loop = None
        plugin._mqtt_client = None
        plugin._mqtt_generation = 0
        plugin._connected_mqtt = False
        plugin._ws_ping_stop = threading.Event()
        plugin._ws_ping_thread = None
        plugin._public_key = "aa" * 32
        plugin._jwt_token = "fixture-token"
        plugin._refresh_jwt_if_needed = MagicMock()
        client = MagicMock()
        client.loop_start.side_effect = plugin.stop
        _mock_paho.Client.return_value = client
        _mock_paho_client.Client.return_value = client
        _mock_paho.mqtt.client.Client.return_value = client

        with pytest.raises(ConnectionError, match="superseded or stopped"):
            plugin._connect_mqtt()

        assert plugin._mqtt_client is None
        assert plugin._connected_mqtt is False
        assert client.loop_stop.call_count == 2
        assert client.disconnect.call_count == 2

    def test_connect_callbacks_use_exact_published_generation(
        self,
        mock_app,
        shared_config,
    ):
        config = {**shared_config, "mqtt_transport": "tcp"}
        plugin = _make_plugin_no_start(mock_app, config)
        plugin._lock = threading.Lock()
        plugin._mqtt_lifecycle_lock = threading.RLock()
        plugin._active = True
        plugin._mqtt_client = None
        plugin._mqtt_generation = 0
        plugin._connected_mqtt = False
        plugin._ws_ping_stop = threading.Event()
        plugin._ws_ping_thread = None
        plugin._public_key = "aa" * 32
        plugin._jwt_token = "fixture-token"
        plugin._refresh_jwt_if_needed = MagicMock()
        client = MagicMock()
        client.publish.return_value = MagicMock(rc=0)
        client.loop_start.side_effect = lambda: client.on_connect(
            client,
            None,
            None,
            0,
        )
        _mock_paho.Client.return_value = client
        _mock_paho_client.Client.return_value = client
        _mock_paho.mqtt.client.Client.return_value = client
        _mock_paho.mqtt.client.MQTT_ERR_SUCCESS = 0

        plugin._connect_mqtt()

        generation = plugin._mqtt_generation
        assert plugin._mqtt_client is client
        assert plugin._connected_mqtt is True
        client.on_disconnect(client, None, None, 1)
        assert plugin._mqtt_generation == generation
        assert plugin._connected_mqtt is False
        plugin._disconnect_mqtt()

    def test_connect_rejects_stopped_or_existing_candidate(
        self,
        mock_app,
        shared_config,
    ):
        plugin = _make_plugin_no_start(mock_app, shared_config)
        plugin._lock = threading.Lock()
        plugin._mqtt_lifecycle_lock = threading.RLock()
        plugin._mqtt_client = None
        plugin._mqtt_generation = 0
        plugin._connected_mqtt = False
        plugin._public_key = "aa" * 32
        plugin._jwt_token = "fixture-token"
        plugin._refresh_jwt_if_needed = MagicMock()
        client = MagicMock()
        _mock_paho.mqtt.client.Client.return_value = client

        plugin._active = False
        with pytest.raises(ConnectionError, match="stopped during MQTT setup"):
            plugin._connect_mqtt()

        plugin._active = True
        plugin._mqtt_client = MagicMock()
        with pytest.raises(RuntimeError, match="previous MQTT client"):
            plugin._connect_mqtt()

    def test_close_mqtt_client_contains_each_sdk_failure(self):
        client = MagicMock()
        client.loop_stop.side_effect = RuntimeError("loop failed")
        client.disconnect.side_effect = RuntimeError("disconnect failed")

        from reticulumpi.builtin_plugins.meshcore_observer import MeshCoreObserver

        MeshCoreObserver._close_mqtt_client(client)

        client.loop_stop.assert_called_once_with()
        client.disconnect.assert_called_once_with()

    def test_late_connack_cannot_resurrect_stopped_mqtt_generation(
        self,
        mock_app,
        shared_config,
    ):
        plugin = _make_plugin_no_start(mock_app, shared_config)
        plugin._lock = threading.Lock()
        plugin._mqtt_lifecycle_lock = threading.RLock()
        plugin._active = True
        plugin._shared_mode = True
        plugin._gateway_handlers_subscribed = False
        plugin._detach_from_gateway = MagicMock()
        plugin._join_threads = MagicMock()
        plugin._loop = None
        plugin._mqtt_generation = 7
        plugin._connected_mqtt = False
        plugin._ws_ping_stop = threading.Event()
        plugin._ws_ping_thread = None
        client = MagicMock()
        client.publish.return_value = MagicMock(rc=0)
        plugin._mqtt_client = client

        plugin.stop()
        plugin.event_bus.publish.reset_mock()
        plugin._handle_mqtt_connect(
            client,
            rc=0,
            broker="broker",
            port=443,
            iata="AUS",
            status_topic="meshcore/AUS/key/status",
            generation=7,
        )

        client.publish.assert_not_called()
        assert plugin._mqtt_client is None
        assert plugin._connected_mqtt is False
        assert plugin._ws_ping_thread is None
        plugin.event_bus.publish.assert_not_called()

    def test_packet_json_uses_recv_time_for_all_time_fields(self, observer_plugin):
        observer_plugin._public_key = "aa" * 32
        # 2024-04-23 00:00:00 UTC
        recv_time = 1713830400
        payload = {
            "recv_time": recv_time,
            "snr": 0,
            "rssi": 0,
            "raw_hex": "",
            "payload_length": 0,
            "route_typename": "FLOOD",
            "payload_type": 0,
            "path_len": 0,
            "path": "",
            "pkt_hash": 0,
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

        with (
            patch.object(observer_plugin, "_disconnect_mqtt", side_effect=spy_disconnect),
            patch.object(
                observer_plugin,
                "_connect_mqtt",
                side_effect=lambda: setattr(observer_plugin, "_active", False),
            ),
        ):
            loop_thread = threading.Thread(
                target=observer_plugin._mqtt_connection_loop,
                daemon=True,
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

        with (
            patch.object(observer_plugin, "_disconnect_mqtt") as mock_disc,
            patch.object(observer_plugin, "_connect_mqtt"),
        ):
            loop_thread = threading.Thread(
                target=observer_plugin._mqtt_connection_loop,
                daemon=True,
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
        observer_plugin._mqtt_client = mock_client

        def publish_side_effect(*args, **kwargs):
            flag_states_during_publish.append(observer_plugin._connected_mqtt)
            return MagicMock(rc=0)

        mock_client.publish.side_effect = publish_side_effect

        _mock_paho_client.MQTT_ERR_SUCCESS = 0

        observer_plugin._handle_mqtt_connect(
            mock_client,
            rc=0,
            broker="broker",
            port=443,
            iata="AUS",
            status_topic="meshcore/AUS/key/status",
        )

        mock_client.publish.assert_called_once()
        assert flag_states_during_publish == [False]
        assert observer_plugin._connected_mqtt is True

    def test_handle_mqtt_connect_ignores_failure(self, observer_plugin):
        observer_plugin._connected_mqtt = False
        mock_client = MagicMock()
        observer_plugin._mqtt_client = mock_client

        observer_plugin._handle_mqtt_connect(
            mock_client,
            rc=1,  # non-zero → failure
            broker="b",
            port=443,
            iata="AUS",
            status_topic="t",
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
                "snr": 0,
                "rssi": 0,
                "raw_hex": "",
                "payload_length": 0,
                "route_typename": "FLOOD",
                "payload_type": 0,
                "path_len": 0,
                "path": "",
                "pkt_hash": 0,
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
            mock_client,
            rc=0,
            broker="broker",
            port=443,
            iata="AUS",
            status_topic="meshcore/AUS/key/status",
        )

        assert observer_plugin._ws_ping_thread is not None
        assert observer_plugin._ws_ping_thread.is_alive()
        observer_plugin._ws_ping_stop.set()
        observer_plugin._ws_ping_thread.join(timeout=2)

    def test_ws_ping_uses_exact_current_client_generation(self, observer_plugin):
        mock_client = MagicMock()
        mock_client._sock = MagicMock()
        observer_plugin._mqtt_client = mock_client
        observer_plugin._connected_mqtt = True
        generation = observer_plugin._mqtt_generation
        ping_stop = MagicMock()
        ping_stop.wait.side_effect = [False, True]
        observer_plugin._ws_ping_stop = ping_stop

        observer_plugin._ws_ping_loop(mock_client, generation)

        mock_client._sock.ping.assert_called_once_with()

    def test_ws_ping_thread_stops_on_disconnect(self, observer_plugin):
        observer_plugin._ws_ping_stop = __import__("threading").Event()
        observer_plugin._ws_ping_thread = None
        mock_client = MagicMock()
        mock_client.publish.return_value = MagicMock(rc=0)
        observer_plugin._mqtt_client = mock_client

        _mock_paho_client.MQTT_ERR_SUCCESS = 0

        observer_plugin._handle_mqtt_connect(
            mock_client,
            rc=0,
            broker="broker",
            port=443,
            iata="AUS",
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
            "serial_port": "/dev/meshcore-observer",
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
                mock_client,
                rc=0,
                broker="test",
                port=1883,
                iata="AUS",
                status_topic="meshcore/AUS/key/status",
            )

            assert plugin._ws_ping_thread is None
        finally:
            plugin._active = False
            plugin._loop_ready.set()
            if plugin._loop and plugin._loop.is_running():
                plugin._loop.call_soon_threadsafe(plugin._loop.stop)
            plugin._join_threads(timeout=2)
