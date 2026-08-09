"""Tests for the MeshCore Gateway plugin."""

from __future__ import annotations

import asyncio
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
# Mock the meshcore package before any plugin imports
# ---------------------------------------------------------------------------


# Build a realistic mock of the meshcore event types
class _MockEventType(Enum):
    CONTACTS = "contacts"
    SELF_INFO = "self_info"
    CONTACT_MSG_RECV = "contact_message"
    CHANNEL_MSG_RECV = "channel_message"
    CURRENT_TIME = "time_update"
    NO_MORE_MSGS = "no_more_messages"
    DEVICE_INFO = "device_info"
    MSG_SENT = "message_sent"
    NEW_CONTACT = "new_contact"
    NEXT_CONTACT = "next_contact"
    ADVERTISEMENT = "advertisement"
    PATH_UPDATE = "path_update"
    ACK = "acknowledgement"
    MESSAGES_WAITING = "messages_waiting"
    OK = "command_ok"
    ERROR = "command_error"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


@dataclass
class _MockEvent:
    type: _MockEventType
    payload: Any
    attributes: Dict[str, Any] = field(default_factory=dict)


class _MockSubscription:
    def __init__(self):
        self.unsubscribe = MagicMock()


_mock_meshcore = MagicMock()
_mock_meshcore_events = MagicMock()
_mock_meshcore_events.EventType = _MockEventType
_mock_meshcore_events.Event = _MockEvent


@pytest.fixture(autouse=True)
def _patch_meshcore():
    """Ensure MeshCore tests cannot leak unsolicited background workers.

    Connection-supervisor behavior is exercised directly by the focused
    ``_connection_loop`` tests below.  Starting that supervisor in every
    otherwise-unrelated unit test races the mocked SDK open against teardown
    and can leave its bounded open wait alive after the test has finished.
    Keep the real managed asyncio loop, but suppress only that implicit
    hardware-connect worker while ``start()`` initializes test state.
    """
    with patch.dict(
        sys.modules,
        {
            "meshcore": _mock_meshcore,
            "meshcore.events": _mock_meshcore_events,
        },
    ):
        # Also patch the EventType import inside the module
        _mock_meshcore.MeshCore = MagicMock()
        _mock_meshcore_events.EventType = _MockEventType
        registry = MagicMock()
        lease = MagicMock()
        lease.revalidate.return_value = lease.identity
        registry.claim.return_value = lease
        from reticulumpi.builtin_plugins.meshcore_gateway import MeshCoreGateway

        started_plugins = []
        real_start = MeshCoreGateway.start

        def start_without_unsolicited_connect(plugin):
            started_plugins.append(plugin)
            with patch.object(plugin, "_connection_loop"):
                return real_start(plugin)

        with (
            patch(
                "reticulumpi.builtin_plugins.meshcore_gateway.serial_device_registry",
                registry,
            ),
            patch.object(MeshCoreGateway, "start", new=start_without_unsolicited_connect),
        ):
            try:
                yield registry
            finally:
                # Tests normally stop explicitly; this guard also makes an
                # assertion failure incapable of contaminating later tests.
                for plugin in reversed(started_plugins):
                    with plugin._threads_lock:
                        has_live_worker = any(thread.is_alive() for thread in plugin._threads)
                    loop = getattr(plugin, "_loop", None)
                    if (
                        plugin._active
                        or has_live_worker
                        or (loop is not None and loop.is_running())
                    ):
                        plugin.stop()
                    with plugin._threads_lock:
                        live_workers = [
                            thread.name for thread in plugin._threads if thread.is_alive()
                        ]
                    if live_workers:
                        pytest.fail(
                            "MeshCore test leaked managed worker(s): " + ", ".join(live_workers)
                        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def gw_config(tmp_path):
    """Base config dict for the meshcore_gateway plugin."""
    return {
        "enabled": True,
        "serial_port": "/dev/meshcore",
        "baudrate": 115200,
        "storage_path": str(tmp_path / "meshcore_gw"),
        "health_check_interval": 30,
        "reconnect_delay": 5,
        "max_reconnect_attempts": 3,
        "max_messages_per_minute": 0,
    }


def _make_mock_meshcore_device():
    """Create a mock MeshCore device instance."""
    mc = MagicMock()
    mc.is_connected = True
    mc.contacts = {
        "aabb" * 8: {
            "public_key": "aabb" * 8,
            "adv_name": "TestNode1",
            "type": 0,
            "last_advert": int(time.time()) - 3600,
            "adv_lat": 30.0,
            "adv_lon": -97.0,
            "flags": 0,
            "out_path_len": 2,
        },
        "ccdd" * 8: {
            "public_key": "ccdd" * 8,
            "adv_name": "TestNode2",
            "type": 0,
            "last_advert": int(time.time()) - 1800,
            "adv_lat": 0.0,
            "adv_lon": 0.0,
            "flags": 0,
            "out_path_len": -1,
        },
    }

    # Async methods
    mc.commands = MagicMock()
    mc.commands.set_time = AsyncMock()
    mc.commands.send_device_query = AsyncMock(
        return_value=_MockEvent(
            _MockEventType.DEVICE_INFO,
            {
                "fw ver": 10,
                "max_contacts": 350,
                "max_channels": 40,
                "ver": "v1.14.1",
                "model": "RAK 4631",
                "fw_build": "20-Mar-2026",
            },
        )
    )
    mc.commands.get_contacts = AsyncMock()
    mc.commands.send_msg = AsyncMock(
        return_value=_MockEvent(
            _MockEventType.MSG_SENT,
            {"expected_ack": b"\x01\x02", "suggested_timeout": 5000},
        )
    )
    mc.commands.send_chan_msg = AsyncMock(
        return_value=_MockEvent(
            _MockEventType.OK,
            {},
        )
    )

    mc.subscribe = MagicMock(return_value=_MockSubscription())
    mc.unsubscribe = MagicMock()
    mc.start_auto_message_fetching = AsyncMock(return_value=_MockSubscription())
    mc.stop_auto_message_fetching = AsyncMock()
    mc.disconnect = AsyncMock()

    return mc


def _make_plugin_no_start(mock_app, config):
    """Construct a MeshCoreGateway without calling start()."""
    from reticulumpi.builtin_plugins.meshcore_gateway import MeshCoreGateway

    return MeshCoreGateway(mock_app, config)


def _make_health_plugin(mock_app, config, mc=None):
    """Construct only the deterministic state needed by health checks."""
    plugin = _make_plugin_no_start(mock_app, config)
    plugin._lock = threading.Lock()
    plugin._active = True
    plugin._mc = mc
    plugin._connected = mc is not None
    plugin._connection_generation = 1
    plugin._device_info = {}
    plugin._health_query_timeout = float(config.get("health_query_timeout", 5))
    plugin._health_response_max_age = float(
        config.get(
            "health_response_max_age",
            max(float(config.get("health_check_interval", 30)) * 2, plugin._health_query_timeout),
        )
    )
    plugin._health_failure_threshold = int(config.get("health_failure_threshold", 3))
    plugin._health_consecutive_failures = 0
    plugin._health_query_failures = 0
    plugin._last_device_response_monotonic = 0.0
    plugin._last_device_response_time = None
    plugin._run_async = MagicMock(side_effect=lambda coro, timeout=15: asyncio.run(coro))
    return plugin


def _make_serial_ownership_plugin(mock_app, config):
    """Construct the state needed to exercise serial lease acquisition."""
    plugin = _make_plugin_no_start(mock_app, config)
    plugin._lock = threading.Lock()
    plugin._disconnect_lock = threading.Lock()
    plugin._serial_device_lease = None
    plugin._serial_reopen_blocked = False
    plugin._connection_generation = 0
    plugin._open_attempt = None
    plugin._teardown_attempt = None
    plugin._device_teardown_timeout = 0.2
    plugin._loop = None
    plugin._mc = None
    plugin._connected = False
    plugin._subscriptions = []
    plugin._last_device_response_monotonic = 0.0
    plugin._last_device_response_time = None
    return plugin


# ---------------------------------------------------------------------------
# TestValidateConfig
# ---------------------------------------------------------------------------


class TestValidateConfig:
    def test_raises_when_meshcore_not_installed(self, mock_app, gw_config):
        with patch.dict(sys.modules, {"meshcore": None}):
            with pytest.raises(ValueError, match="meshcore package not found"):
                _make_plugin_no_start(mock_app, gw_config)

    def test_raises_for_bad_serial_port(self, mock_app, gw_config):
        gw_config["serial_port"] = ""
        with pytest.raises(ValueError, match="serial_port"):
            _make_plugin_no_start(mock_app, gw_config)

    @pytest.mark.parametrize("serial_port", ["/dev/ttyUSB0", "/dev/ttyACM17"])
    def test_rejects_kernel_assigned_serial_indexes(
        self,
        mock_app,
        gw_config,
        serial_port,
    ):
        gw_config["serial_port"] = serial_port
        with pytest.raises(ValueError, match="stable serial device path"):
            _make_plugin_no_start(mock_app, gw_config)

    def test_raises_for_bad_baudrate(self, mock_app, gw_config):
        gw_config["baudrate"] = -1
        with pytest.raises(ValueError, match="baudrate"):
            _make_plugin_no_start(mock_app, gw_config)

    def test_raises_for_bad_health_check_interval(self, mock_app, gw_config):
        gw_config["health_check_interval"] = 2
        with pytest.raises(ValueError, match="health_check_interval"):
            _make_plugin_no_start(mock_app, gw_config)

    @pytest.mark.parametrize("value", [0, 31, True])
    def test_raises_for_bad_health_query_timeout(self, mock_app, gw_config, value):
        gw_config["health_query_timeout"] = value
        with pytest.raises(ValueError, match="health_query_timeout"):
            _make_plugin_no_start(mock_app, gw_config)

    @pytest.mark.parametrize("value", [-1, 3601, True])
    def test_raises_for_bad_health_response_max_age(self, mock_app, gw_config, value):
        gw_config["health_response_max_age"] = value
        with pytest.raises(ValueError, match="health_response_max_age"):
            _make_plugin_no_start(mock_app, gw_config)

    @pytest.mark.parametrize("value", [0, -1, True, 1.5])
    def test_raises_for_bad_health_failure_threshold(self, mock_app, gw_config, value):
        gw_config["health_failure_threshold"] = value
        with pytest.raises(ValueError, match="health_failure_threshold"):
            _make_plugin_no_start(mock_app, gw_config)

    def test_raises_for_bad_reconnect_delay(self, mock_app, gw_config):
        gw_config["reconnect_delay"] = 0
        with pytest.raises(ValueError, match="reconnect_delay"):
            _make_plugin_no_start(mock_app, gw_config)

    def test_raises_for_bad_max_reconnect_attempts(self, mock_app, gw_config):
        gw_config["max_reconnect_attempts"] = -1
        with pytest.raises(ValueError, match="max_reconnect_attempts"):
            _make_plugin_no_start(mock_app, gw_config)

    def test_raises_for_bad_rate_limit(self, mock_app, gw_config):
        gw_config["max_messages_per_minute"] = -5
        with pytest.raises(ValueError, match="max_messages_per_minute"):
            _make_plugin_no_start(mock_app, gw_config)

    def test_valid_config_accepted(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        assert plugin.plugin_name == "meshcore_gateway"


# ---------------------------------------------------------------------------
# TestPluginLifecycle
# ---------------------------------------------------------------------------


class TestPluginLifecycle:
    def test_start_and_stop(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()
        assert plugin._active is True
        assert plugin._loop is not None
        assert plugin._loop_ready.is_set()
        plugin.stop()
        assert plugin._active is False

    def test_initial_stats_are_zero(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()
        status = plugin.get_status()
        assert status["msgs_received"] == 0
        assert status["msgs_sent"] == 0
        assert status["connect_count"] == 0
        assert status["connected"] is False
        plugin.stop()

    def test_stop_contains_unexpected_disconnect_failure(self, mock_app, gw_config):
        plugin = _make_serial_ownership_plugin(mock_app, gw_config)
        plugin._active = True
        plugin._disconnect_device = MagicMock(side_effect=RuntimeError("SDK teardown failed"))
        plugin._join_threads = MagicMock()

        plugin.stop()

        assert plugin._active is False
        plugin._join_threads.assert_called_once_with()


# ---------------------------------------------------------------------------
# TestConnectionManagement
# ---------------------------------------------------------------------------


class TestConnectionManagement:
    def test_run_async_without_loop_closes_awaitable(self, mock_app, gw_config):
        plugin = _make_serial_ownership_plugin(mock_app, gw_config)
        awaitable = MagicMock()

        with pytest.raises(RuntimeError, match="async loop not running"):
            plugin._run_async(awaitable)

        awaitable.close.assert_called_once_with()

    def test_run_async_timeout_cancels_submitted_future(self, mock_app, gw_config):
        plugin = _make_serial_ownership_plugin(mock_app, gw_config)
        plugin._loop = MagicMock()
        plugin._loop.is_running.return_value = True
        future = MagicMock()
        future.result.side_effect = TimeoutError("command timed out")
        awaitable = MagicMock()

        with patch(
            "reticulumpi.builtin_plugins.meshcore_gateway.asyncio.run_coroutine_threadsafe",
            return_value=future,
        ):
            with pytest.raises(TimeoutError, match="command timed out"):
                plugin._run_async(awaitable, timeout=0.01)

        future.cancel.assert_called_once_with()

    def test_tracked_open_submission_failure_clears_attempt(self, mock_app, gw_config):
        plugin = _make_serial_ownership_plugin(mock_app, gw_config)
        plugin._active = True
        plugin._loop = MagicMock()
        plugin._loop.is_running.return_value = True
        generation = plugin._begin_connection_generation()

        async def open_client():
            return MagicMock()

        with (
            patch(
                "reticulumpi.builtin_plugins.meshcore_gateway.asyncio.run_coroutine_threadsafe",
                side_effect=RuntimeError("loop rejected task"),
            ),
            pytest.raises(RuntimeError, match="loop rejected task"),
        ):
            plugin._run_tracked_open(open_client(), generation, timeout=1)

        assert plugin._open_attempt is None
        assert plugin._serial_reopen_blocked is False

    def test_async_close_unpublished_client_reports_disconnect_failure(
        self,
        mock_app,
        gw_config,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, gw_config)
        mc = MagicMock()
        mc.disconnect = AsyncMock(side_effect=RuntimeError("USB disappeared"))

        assert asyncio.run(plugin._async_close_unpublished_mc(mc)) is False

    def test_claims_and_revalidates_exclusively_before_serial_open(
        self,
        mock_app,
        gw_config,
        _patch_meshcore,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, gw_config)
        calls = []
        lease = MagicMock()
        _patch_meshcore.claim.side_effect = lambda path, owner: (
            calls.append(("claim", path, owner)) or lease
        )
        lease.revalidate.side_effect = lambda: calls.append(("revalidate",))
        plugin._run_async = MagicMock(side_effect=lambda awaitable, timeout=15: None)

        with patch("meshcore.MeshCore.create_serial") as create_serial:
            create_serial.side_effect = lambda *args, **kwargs: calls.append(("open",)) or object()
            with pytest.raises(ConnectionError, match="did not respond"):
                plugin._connect_device()

        assert calls[:3] == [
            ("claim", "/dev/meshcore", "meshcore_gateway"),
            ("revalidate",),
            ("open",),
        ]
        create_serial.assert_called_once_with(
            "/dev/meshcore",
            115200,
            auto_reconnect=False,
        )
        assert plugin._serial_device_lease is lease

    def test_serial_claim_conflict_prevents_open(
        self,
        mock_app,
        gw_config,
        _patch_meshcore,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, gw_config)
        busy = SerialDeviceBusyError(
            "/dev/meshcore",
            ("meshtastic_gateway",),
            MagicMock(),
            external=False,
        )
        _patch_meshcore.claim.side_effect = busy
        plugin._run_async = MagicMock()

        with patch("meshcore.MeshCore.create_serial") as create_serial:
            with pytest.raises(SerialDeviceBusyError, match="meshtastic_gateway"):
                plugin._connect_device()

        create_serial.assert_not_called()
        plugin._run_async.assert_not_called()
        assert plugin._serial_device_lease is None

    def test_reconnect_revalidates_existing_lease_without_reclaim(
        self,
        mock_app,
        gw_config,
        _patch_meshcore,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, gw_config)
        lease = MagicMock()
        plugin._serial_device_lease = lease

        assert plugin._ensure_serial_device_lease("/dev/meshcore") is lease

        lease.revalidate.assert_called_once_with()
        _patch_meshcore.claim.assert_not_called()

    def test_hotplug_identity_change_releases_and_reclaims(
        self,
        mock_app,
        gw_config,
        _patch_meshcore,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, gw_config)
        old_lease = MagicMock()
        old_lease.revalidate.side_effect = SerialDeviceChangedError(
            "/dev/meshcore",
            MagicMock(),
            MagicMock(),
        )
        new_lease = MagicMock()
        _patch_meshcore.claim.return_value = new_lease
        plugin._serial_device_lease = old_lease

        assert plugin._ensure_serial_device_lease("/dev/meshcore") is new_lease

        old_lease.release.assert_called_once_with()
        _patch_meshcore.claim.assert_called_once_with("/dev/meshcore", "meshcore_gateway")
        new_lease.revalidate.assert_called_once_with()
        assert plugin._serial_device_lease is new_lease

    def test_missing_hotplug_device_retains_existing_lease_and_does_not_open(
        self,
        mock_app,
        gw_config,
        _patch_meshcore,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, gw_config)
        lease = MagicMock()
        lease.revalidate.side_effect = SerialDeviceIdentityError("device absent")
        plugin._serial_device_lease = lease

        with patch("meshcore.MeshCore.create_serial") as create_serial:
            with pytest.raises(SerialDeviceIdentityError, match="device absent"):
                plugin._connect_device()

        lease.release.assert_not_called()
        _patch_meshcore.claim.assert_not_called()
        create_serial.assert_not_called()
        assert plugin._serial_device_lease is lease

    def test_stop_releases_serial_lease(self, mock_app, gw_config):
        plugin = _make_serial_ownership_plugin(mock_app, gw_config)
        lease = MagicMock()
        plugin._serial_device_lease = lease
        plugin._active = True
        plugin._loop = None
        plugin._mc = None
        plugin._join_threads = MagicMock()

        plugin.stop()

        lease.release.assert_called_once_with()
        assert plugin._serial_device_lease is None

    def test_stop_retains_lease_when_disconnect_cannot_be_proven(self, mock_app, gw_config):
        plugin = _make_serial_ownership_plugin(mock_app, gw_config)
        lease = MagicMock()
        plugin._serial_device_lease = lease
        plugin._active = True
        plugin._loop = None
        plugin._mc = object()
        plugin._join_threads = MagicMock()

        plugin.stop()

        lease.release.assert_not_called()
        assert plugin._serial_device_lease is lease

    def test_stop_retains_lease_while_managed_worker_is_alive(self, mock_app, gw_config):
        plugin = _make_serial_ownership_plugin(mock_app, gw_config)
        lease = MagicMock()
        live_thread = MagicMock()
        live_thread.name = "meshcore-connect"
        live_thread.is_alive.return_value = True
        plugin._serial_device_lease = lease
        plugin._active = True
        plugin._loop = None
        plugin._mc = None
        plugin._threads = [live_thread]
        plugin._join_threads = MagicMock()

        plugin.stop()

        lease.release.assert_not_called()
        assert plugin._serial_device_lease is lease

    def test_uncertain_disconnect_retains_handle_and_blocks_reopen(self, mock_app, gw_config):
        plugin = _make_serial_ownership_plugin(mock_app, gw_config)
        mc = MagicMock()
        plugin._mc = mc
        plugin._connected = False
        plugin._loop = MagicMock()
        plugin._loop.is_running.return_value = True

        def timeout_disconnect(coro, timeout=15):
            coro.close()
            raise TimeoutError("disconnect timed out")

        plugin._run_async = MagicMock(side_effect=timeout_disconnect)

        assert plugin._disconnect_device() is False

        assert plugin._mc is mc
        assert plugin._serial_reopen_blocked is True

    def test_connection_loop_never_opens_behind_uncertain_stale_handle(
        self,
        mock_app,
        gw_config,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, gw_config)
        plugin._mc = MagicMock()
        plugin._connected = False
        plugin._active = True
        plugin._reconnect_failures = 0
        plugin._disconnect_device = MagicMock(return_value=False)
        plugin._connect_device = MagicMock()
        plugin._sleep_while_active = MagicMock(
            side_effect=lambda _delay: setattr(plugin, "_active", False)
        )

        plugin._connection_loop()

        plugin._disconnect_device.assert_called_once_with()
        plugin._connect_device.assert_not_called()

    def test_failed_initialization_keeps_candidate_for_teardown(
        self,
        mock_app,
        gw_config,
        _patch_meshcore,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, gw_config)
        mc = _make_mock_meshcore_device()
        mc.commands.set_time = MagicMock(return_value=object())
        plugin._active = True
        plugin._health_query_timeout = 5
        plugin._ensure_serial_device_lease = MagicMock()
        plugin._run_async = MagicMock(side_effect=[mc, RuntimeError("initialization failed")])

        with patch("meshcore.MeshCore.create_serial", return_value=object()):
            with pytest.raises(RuntimeError, match="initialization failed"):
                plugin._connect_device()

        assert plugin._mc is mc
        assert plugin._connected is False

    def test_stop_generation_fences_in_progress_setup(
        self,
        mock_app,
        gw_config,
    ):
        plugin = _make_serial_ownership_plugin(mock_app, gw_config)
        plugin._active = True
        plugin._health_query_timeout = 5
        plugin._ensure_serial_device_lease = MagicMock()
        mc = _make_mock_meshcore_device()
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
        assert plugin._connected is False
        mc.subscribe.assert_not_called()

    def test_connect_device_sets_state(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        with patch.object(plugin, "_connection_loop"):
            plugin.start()

        mc = _make_mock_meshcore_device()

        async def mock_create_serial(*args, **kwargs):
            return mc

        with patch("meshcore.MeshCore.create_serial", side_effect=mock_create_serial):
            plugin._connect_device()

        assert plugin._connected is True
        assert plugin._mc is mc
        assert plugin._device_info.get("ver") == "v1.14.1"
        assert plugin._device_info.get("model") == "RAK 4631"

        # Verify events were published
        mock_app.event_bus.publish.assert_any_call(
            "meshcore.connected",
            {
                "firmware": "v1.14.1",
                "model": "RAK 4631",
                "serial_port": "/dev/meshcore",
            },
        )

        plugin.stop()

    def test_old_generation_callbacks_are_fenced_after_disconnect(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        with patch.object(plugin, "_connection_loop"):
            plugin.start()

        mc = _make_mock_meshcore_device()

        async def mock_create_serial(*args, **kwargs):
            return mc

        with patch("meshcore.MeshCore.create_serial", side_effect=mock_create_serial):
            plugin._connect_device()

        callbacks = [call.args[1] for call in mc.subscribe.call_args_list]
        direct, channel, disconnected, ack, new_contact = callbacks
        plugin._handle_incoming_message = MagicMock()
        plugin._handle_ack_event = MagicMock()
        plugin._handle_new_contact = MagicMock()

        asyncio.run(disconnected(_MockEvent(_MockEventType.DISCONNECTED, {})))
        asyncio.run(direct(_MockEvent(_MockEventType.CONTACT_MSG_RECV, {})))
        asyncio.run(channel(_MockEvent(_MockEventType.CHANNEL_MSG_RECV, {})))
        asyncio.run(ack(_MockEvent(_MockEventType.ACK, {})))
        asyncio.run(new_contact(_MockEvent(_MockEventType.NEW_CONTACT, {})))

        assert plugin._connected is False
        plugin._handle_incoming_message.assert_not_called()
        plugin._handle_ack_event.assert_not_called()
        plugin._handle_new_contact.assert_not_called()
        plugin.stop()

    def test_disconnect_clears_state(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        with patch.object(plugin, "_connection_loop"):
            plugin.start()

        mc = _make_mock_meshcore_device()

        async def mock_create_serial(*args, **kwargs):
            return mc

        with patch("meshcore.MeshCore.create_serial", side_effect=mock_create_serial):
            plugin._connect_device()

        assert plugin._connected is True
        plugin._disconnect_device()

        assert plugin._connected is False
        assert plugin._mc is None

        plugin.stop()

    def test_check_health_queries_device_instead_of_cached_connection(self, mock_app, gw_config):
        mc = _make_mock_meshcore_device()
        mc.is_connected = False
        plugin = _make_health_plugin(mock_app, gw_config, mc)

        assert plugin._check_health() is True
        mc.commands.send_device_query.assert_awaited_once()
        assert plugin._device_info["ver"] == "v1.14.1"
        assert plugin._last_device_response_monotonic > 0
        assert plugin._last_device_response_time is not None

    def test_check_health_false_when_no_mc(self, mock_app, gw_config):
        plugin = _make_health_plugin(mock_app, gw_config)
        assert plugin._check_health() is False

    def test_recent_response_tolerates_one_bounded_query_failure(self, mock_app, gw_config):
        gw_config["health_response_max_age"] = 60
        mc = _make_mock_meshcore_device()
        mc.is_connected = True
        mc.commands.send_device_query = AsyncMock(side_effect=TimeoutError("query timed out"))
        plugin = _make_health_plugin(mock_app, gw_config, mc)
        plugin._last_device_response_monotonic = time.monotonic() - 10

        assert plugin._check_health() is True
        assert plugin._health_query_failures == 1
        plugin._run_async.assert_called_once()
        assert plugin._run_async.call_args.kwargs["timeout"] == 5

    def test_cached_connected_flag_cannot_mask_stale_device(self, mock_app, gw_config):
        gw_config["health_response_max_age"] = 30
        mc = _make_mock_meshcore_device()
        mc.is_connected = True
        mc.commands.send_device_query = AsyncMock(side_effect=TimeoutError("query timed out"))
        plugin = _make_health_plugin(mock_app, gw_config, mc)
        plugin._last_device_response_monotonic = time.monotonic() - 31

        assert plugin._check_health() is False
        assert plugin._health_query_failures == 1

    def test_explicit_device_error_is_not_a_health_response(self, mock_app, gw_config):
        gw_config["health_response_max_age"] = 0
        mc = _make_mock_meshcore_device()
        mc.commands.send_device_query = AsyncMock(
            return_value=_MockEvent(_MockEventType.ERROR, {"reason": "radio busy"})
        )
        plugin = _make_health_plugin(mock_app, gw_config, mc)

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
        gw_config,
        response,
    ):
        gw_config["health_response_max_age"] = 0
        mc = _make_mock_meshcore_device()
        mc.commands.send_device_query = AsyncMock(return_value=response)
        plugin = _make_health_plugin(mock_app, gw_config, mc)

        assert plugin._check_health() is False
        assert plugin._last_device_response_monotonic == 0

    def test_recognizable_direct_device_mapping_proves_health(self, mock_app, gw_config):
        mc = _make_mock_meshcore_device()
        mc.commands.send_device_query = AsyncMock(
            return_value={"ver": "v1.15.0", "model": "RAK4631"}
        )
        plugin = _make_health_plugin(mock_app, gw_config, mc)

        assert plugin._check_health() is True
        assert plugin._device_info["ver"] == "v1.15.0"

    @pytest.mark.parametrize("event_wrapped", [False, True], ids=["direct", "event"])
    def test_sdk_fw_ver_only_device_info_proves_health(
        self,
        mock_app,
        gw_config,
        event_wrapped,
    ):
        """MeshCore 2.3.7 always reports ``fw ver``, including pre-v3 firmware."""
        mc = _make_mock_meshcore_device()
        payload = {"fw ver": 2}
        response = _MockEvent(_MockEventType.DEVICE_INFO, payload) if event_wrapped else payload
        mc.commands.send_device_query = AsyncMock(return_value=response)
        plugin = _make_health_plugin(mock_app, gw_config, mc)

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
        gw_config,
        payload,
    ):
        gw_config["health_response_max_age"] = 0
        mc = _make_mock_meshcore_device()
        mc.commands.send_device_query = AsyncMock(
            return_value=_MockEvent(_MockEventType.DEVICE_INFO, payload)
        )
        plugin = _make_health_plugin(mock_app, gw_config, mc)

        assert plugin._check_health() is False
        assert plugin._last_device_response_monotonic == 0

    @pytest.mark.parametrize(
        "event_type",
        [_MockEventType.DISCONNECTED, _MockEventType.ACK, _MockEventType.OK],
    )
    def test_unrelated_event_payload_cannot_prove_health(
        self,
        mock_app,
        gw_config,
        event_type,
    ):
        gw_config["health_response_max_age"] = 0
        mc = _make_mock_meshcore_device()
        mc.commands.send_device_query = AsyncMock(
            return_value=_MockEvent(event_type, {"ver": "looks-valid"})
        )
        plugin = _make_health_plugin(mock_app, gw_config, mc)

        assert plugin._check_health() is False
        assert plugin._last_device_response_monotonic == 0

    def test_empty_device_info_cannot_prove_health(self, mock_app, gw_config):
        gw_config["health_response_max_age"] = 0
        mc = _make_mock_meshcore_device()
        mc.commands.send_device_query = AsyncMock(
            return_value=_MockEvent(_MockEventType.DEVICE_INFO, {})
        )
        plugin = _make_health_plugin(mock_app, gw_config, mc)

        assert plugin._check_health() is False

    def test_string_device_info_type_remains_compatible(self, mock_app, gw_config):
        mc = _make_mock_meshcore_device()
        mc.commands.send_device_query = AsyncMock(
            return_value=_MockEvent("device_info", {"ver": "v1.14.1"})
        )
        plugin = _make_health_plugin(mock_app, gw_config, mc)

        assert plugin._check_health() is True

    def test_cancellation_resistant_open_is_closed_before_reopen(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        with patch.object(plugin, "_connection_loop"):
            plugin.start()

        late_mc = _make_mock_meshcore_device()
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
        attempt = plugin._open_attempt
        assert plugin._serial_reopen_blocked is True
        with pytest.raises(RuntimeError, match="not quiescent"):
            plugin._begin_connection_generation()

        allow_return.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with plugin._lock:
                quiescent = plugin._open_attempt is None and plugin._mc is None
            if quiescent:
                break
            time.sleep(0.01)

        assert quiescent is True
        assert attempt["done"].wait(timeout=2)
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), plugin._loop).result(timeout=2)
        late_mc.disconnect.assert_awaited_once()
        plugin.stop()

    def test_hung_unsubscribe_blocks_reopen_until_teardown_finishes(
        self,
        mock_app,
        gw_config,
    ):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        with patch.object(plugin, "_connection_loop"):
            plugin.start()

        mc = _make_mock_meshcore_device()
        sub = MagicMock()
        release = threading.Event()
        mc.unsubscribe.side_effect = lambda _sub: release.wait()
        with plugin._lock:
            plugin._mc = mc
            plugin._connected = True
            plugin._subscriptions = [sub]
        plugin._device_teardown_timeout = 0.03

        assert plugin._disconnect_device() is False
        assert plugin._mc is mc
        assert plugin._serial_reopen_blocked is True
        with pytest.raises(RuntimeError, match="not quiescent"):
            plugin._begin_connection_generation()

        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and plugin._mc is not None:
            time.sleep(0.01)

        assert plugin._mc is None
        mc.disconnect.assert_awaited_once()
        plugin.stop()

    def test_old_generation_disconnect_callback_cannot_drop_new_client(
        self,
        mock_app,
        gw_config,
    ):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        with patch.object(plugin, "_connection_loop"):
            plugin.start()

        first = _make_mock_meshcore_device()
        second = _make_mock_meshcore_device()

        async def create_first(*args, **kwargs):
            return first

        async def create_second(*args, **kwargs):
            return second

        with patch("meshcore.MeshCore.create_serial", side_effect=create_first):
            plugin._connect_device()
        first_disconnect_cb = next(
            call.args[1]
            for call in first.subscribe.call_args_list
            if call.args[0] == _MockEventType.DISCONNECTED
        )
        assert plugin._disconnect_device() is True

        with patch("meshcore.MeshCore.create_serial", side_effect=create_second):
            plugin._connect_device()
        mock_app.event_bus.publish.reset_mock()

        asyncio.run(
            first_disconnect_cb(_MockEvent(_MockEventType.DISCONNECTED, {"reason": "late"}))
        )

        assert plugin._mc is second
        assert plugin._connected is True
        assert not any(
            call.args[0] == "meshcore.disconnected"
            for call in mock_app.event_bus.publish.call_args_list
        )
        plugin.stop()

    def test_connection_loop_reconnects_only_after_configured_failures(
        self,
        mock_app,
        gw_config,
    ):
        gw_config["health_failure_threshold"] = 3
        plugin = _make_health_plugin(mock_app, gw_config, _make_mock_meshcore_device())
        plugin._reconnect_failures = 0
        plugin._advert_interval = 0
        plugin._contact_refresh_interval = 0
        plugin._active = True
        plugin._check_health = MagicMock(side_effect=[False, False, False])
        plugin._sleep_while_active = MagicMock()

        def disconnect_meshcore_only():
            plugin._mc = None
            plugin._connected = False
            plugin._active = False

        plugin._disconnect_device = MagicMock(side_effect=disconnect_meshcore_only)

        plugin._connection_loop()

        assert plugin._check_health.call_count == 3
        plugin._disconnect_device.assert_called_once_with()
        mock_app.event_bus.publish.assert_any_call(
            "meshcore.disconnected",
            {"reason": "health_check_failed"},
        )

    def test_successful_probe_clears_failure_streak_without_reconnect(
        self,
        mock_app,
        gw_config,
    ):
        plugin = _make_health_plugin(mock_app, gw_config, _make_mock_meshcore_device())
        plugin._reconnect_failures = 0
        plugin._advert_interval = 0
        plugin._contact_refresh_interval = 0
        plugin._active = True
        plugin._check_health = MagicMock(side_effect=[False, True])
        plugin._disconnect_device = MagicMock()
        sleep_count = 0

        def finish_after_recovery(_seconds):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count == 2:
                plugin._active = False

        plugin._sleep_while_active = MagicMock(side_effect=finish_after_recovery)

        plugin._connection_loop()

        assert plugin._check_health.call_count == 2
        assert plugin._health_consecutive_failures == 0
        plugin._disconnect_device.assert_not_called()

    def test_offline_hotplug_retry_path_is_unchanged(self, mock_app, gw_config):
        plugin = _make_health_plugin(mock_app, gw_config)
        plugin._reconnect_failures = 0
        plugin._advert_interval = 0
        plugin._contact_refresh_interval = 0
        plugin._internet_available = False
        plugin._active = True
        mc = _make_mock_meshcore_device()
        attempts = 0

        def connect_after_hotplug():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("device absent")
            plugin._mc = mc
            plugin._connected = True
            plugin._active = False

        plugin._connect_device = MagicMock(side_effect=connect_after_hotplug)
        plugin._check_health = MagicMock(return_value=True)
        plugin._sleep_while_active = MagicMock()

        plugin._connection_loop()

        assert attempts == 2
        mock_app.event_bus.publish.assert_any_call(
            "meshcore.connect_failed",
            {"error": "device absent", "attempt": 1},
        )


# ---------------------------------------------------------------------------
# TestMessageHandling
# ---------------------------------------------------------------------------


class TestMessageHandling:
    def test_handle_direct_message_dict_payload(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()

        # Pre-populate cache so prefix can resolve to full key + name
        full_key = "aabb" * 8
        plugin._contact_cache[full_key] = {"adv_name": "TestNode1"}

        event = _MockEvent(
            _MockEventType.CONTACT_MSG_RECV,
            {
                "text": "Hello from MeshCore!",
                "pubkey_prefix": full_key[:12],
            },
        )

        plugin._handle_incoming_message(event, msg_type="direct")

        assert plugin._msgs_received == 1
        mock_app.event_bus.publish.assert_any_call(
            "meshcore.message_received",
            {
                "from_key": full_key,
                "from_name": "TestNode1",
                "text": "Hello from MeshCore!",
                "msg_type": "direct",
                "channel": None,
                "path_len": None,
            },
        )

        plugin.stop()

    def test_handle_channel_message(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()

        event = _MockEvent(
            _MockEventType.CHANNEL_MSG_RECV,
            {"text": "Hello channel!", "channel_idx": 0},
        )

        plugin._handle_incoming_message(event, msg_type="broadcast")

        assert plugin._msgs_received == 1
        mock_app.event_bus.publish.assert_any_call(
            "meshcore.message_received",
            {
                "from_key": "",
                "from_name": "",
                "text": "Hello channel!",
                "msg_type": "broadcast",
                "channel": 0,
                "path_len": None,
            },
        )

        plugin.stop()

    def test_handle_empty_message_ignored(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()

        event = _MockEvent(
            _MockEventType.CONTACT_MSG_RECV,
            {"text": "   ", "pubkey_prefix": "aabb" * 8},
        )
        plugin._handle_incoming_message(event, msg_type="direct")

        assert plugin._msgs_received == 0
        plugin.stop()

    def test_handle_message_resolves_name_from_cache(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()

        # Pre-populate cache — prefix lookup should match
        full_key = "aabb" * 8
        plugin._contact_cache[full_key] = {"adv_name": "CachedName"}

        event = _MockEvent(
            _MockEventType.CONTACT_MSG_RECV,
            {"text": "Test msg", "pubkey_prefix": full_key[:12]},
        )
        plugin._handle_incoming_message(event, msg_type="direct")

        # Should resolve name from cache via prefix match
        call_args = mock_app.event_bus.publish.call_args_list
        msg_events = [c for c in call_args if c[0][0] == "meshcore.message_received"]
        assert len(msg_events) == 1
        assert msg_events[0][0][1]["from_name"] == "CachedName"
        assert msg_events[0][0][1]["from_key"] == full_key

        plugin.stop()


# ---------------------------------------------------------------------------
# TestRateLimiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    def test_rate_limit_disabled_by_default(self, mock_app, gw_config):
        gw_config["max_messages_per_minute"] = 0
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()
        assert plugin._check_send_rate_limit() is True
        assert plugin._check_send_rate_limit() is True
        plugin.stop()

    def test_rate_limit_enforced(self, mock_app, gw_config):
        gw_config["max_messages_per_minute"] = 60  # 1 per second
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()

        # First message should be allowed
        assert plugin._check_send_rate_limit() is True
        # Immediate second should be blocked
        assert plugin._check_send_rate_limit() is False
        assert plugin._msgs_rate_limited == 1

        plugin.stop()


# ---------------------------------------------------------------------------
# TestSendMessage
# ---------------------------------------------------------------------------


class TestSendMessage:
    def test_send_fails_when_not_connected(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()

        result = plugin.send_message("hello")
        assert result["sent"] is False
        assert result["reason"] == "not_connected"

        plugin.stop()

    def test_send_fails_when_rate_limited(self, mock_app, gw_config):
        gw_config["max_messages_per_minute"] = 60
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()

        mc = _make_mock_meshcore_device()
        plugin._mc = mc
        plugin._connected = True

        # First send succeeds
        plugin._check_send_rate_limit()  # consume the allowance
        result = plugin.send_message("hello")
        assert result["sent"] is False
        assert result["reason"] == "rate_limited"

        plugin.stop()

    def test_send_direct_message(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()

        mc = _make_mock_meshcore_device()
        plugin._mc = mc
        plugin._connected = True

        try:
            result = plugin.send_message("hello", destination="aabb" * 8)
            assert result["sent"] is True
            assert plugin._msgs_sent == 1
        finally:
            plugin.stop()

    def test_send_channel_message(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()

        mc = _make_mock_meshcore_device()
        plugin._mc = mc
        plugin._connected = True

        try:
            result = plugin.send_message("hello channel", channel=0)
            assert result["sent"] is True
            assert plugin._msgs_sent == 1
        finally:
            plugin.stop()

    def test_reconnect_during_send_reports_delivery_uncertain_without_retry(
        self,
        mock_app,
        gw_config,
    ):
        old_mc = MagicMock()
        old_mc.commands.send_msg.return_value = object()
        new_mc = MagicMock()
        plugin = _make_health_plugin(mock_app, gw_config, old_mc)
        plugin._send_min_interval = 0
        plugin._msgs_sent = 0

        def reconnect_before_completion(_awaitable, timeout=15):
            assert timeout == 10
            with plugin._lock:
                plugin._connection_generation += 1
                plugin._mc = new_mc
                plugin._connected = True
            return _MockEvent(
                _MockEventType.MSG_SENT,
                {"expected_ack": b"\x01\x02", "suggested_timeout": 5000},
            )

        plugin._run_async = MagicMock(side_effect=reconnect_before_completion)

        result = plugin.send_message("hello", destination="aabb" * 8)

        assert result == {
            "sent": False,
            "reason": "delivery_uncertain_connection_changed",
        }
        old_mc.commands.send_msg.assert_called_once_with("aabb" * 8, "hello")
        new_mc.commands.send_msg.assert_not_called()
        assert plugin._msgs_sent == 0
        sent_events = [
            call
            for call in mock_app.event_bus.publish.call_args_list
            if call.args and call.args[0] == "meshcore.message_sent"
        ]
        assert sent_events == []

    def test_stop_while_send_is_inflight_reports_delivery_uncertain(
        self,
        mock_app,
        gw_config,
    ):
        entered_sdk = threading.Event()
        release_sdk = threading.Event()
        mc = MagicMock()
        mc.commands.send_chan_msg.return_value = object()
        plugin = _make_health_plugin(mock_app, gw_config, mc)
        plugin._send_min_interval = 0
        plugin._msgs_sent = 0
        plugin._loop = None
        plugin._serial_reopen_blocked = False
        plugin._open_attempt = None
        plugin._teardown_attempt = None
        plugin._serial_device_lease = None
        plugin._join_threads = MagicMock()

        def blocked_send(_awaitable, timeout=15):
            assert timeout == 10
            entered_sdk.set()
            assert release_sdk.wait(timeout=3)
            return _MockEvent(_MockEventType.OK, {})

        def fence_connection() -> bool:
            plugin._invalidate_connection_generation()
            with plugin._lock:
                plugin._mc = None
            return True

        plugin._run_async = MagicMock(side_effect=blocked_send)
        plugin._disconnect_device = MagicMock(side_effect=fence_connection)
        outcome: dict[str, Any] = {}

        thread = threading.Thread(
            target=lambda: outcome.update(plugin.send_message("field update", channel=2)),
            daemon=True,
        )
        thread.start()
        assert entered_sdk.wait(timeout=2)

        plugin.stop()
        release_sdk.set()
        thread.join(timeout=2)

        assert not thread.is_alive()
        assert outcome == {
            "sent": False,
            "reason": "delivery_uncertain_connection_changed",
        }
        mc.commands.send_chan_msg.assert_called_once_with(2, "field update")
        assert plugin._msgs_sent == 0
        sent_events = [
            call
            for call in mock_app.event_bus.publish.call_args_list
            if call.args and call.args[0] == "meshcore.message_sent"
        ]
        assert sent_events == []


# ---------------------------------------------------------------------------
# TestStatusAndContacts
# ---------------------------------------------------------------------------


class TestStatusAndContacts:
    def test_get_status(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()

        status = plugin.get_status()
        assert status["active"] is True
        assert status["connected"] is False
        assert status["serial_port"] == "/dev/meshcore"
        assert status["msgs_received"] == 0
        assert status["msgs_sent"] == 0

        plugin.stop()

    def test_get_device_info(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()

        plugin._device_info = {"ver": "v1.14.1", "model": "RAK 4631"}
        plugin._connected = True

        info = plugin.get_device_info()
        assert info["ver"] == "v1.14.1"
        assert info["model"] == "RAK 4631"
        assert info["connected"] is True

        plugin.stop()

    def test_get_contacts_from_cache(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()

        plugin._contact_cache = {
            "aabb" * 8: {
                "public_key": "aabb" * 8,
                "adv_name": "TestNode1",
                "type": 0,
                "last_advert": int(time.time()) - 3600,
                "adv_lat": 30.0,
                "adv_lon": -97.0,
                "flags": 0,
                "out_path_len": 2,
            },
        }

        contacts = plugin.get_contacts()
        assert len(contacts) == 1
        assert contacts[0]["public_key"] == "aabb" * 8
        assert contacts[0]["name"] == "TestNode1"

        plugin.stop()

    def test_get_contacts_from_live_device(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()

        mc = _make_mock_meshcore_device()
        plugin._mc = mc
        plugin._connected = True

        contacts = plugin.get_contacts()
        assert len(contacts) == 2

        names = {c["name"] for c in contacts}
        assert "TestNode1" in names
        assert "TestNode2" in names

        plugin.stop()

    def test_get_meshcore_nodes(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()

        mc = _make_mock_meshcore_device()
        plugin._mc = mc
        plugin._connected = True

        nodes = plugin.get_meshcore_nodes()
        assert len(nodes) == 2
        assert all("id" in n and "name" in n for n in nodes)

        plugin.stop()


# ---------------------------------------------------------------------------
# TestContactCache
# ---------------------------------------------------------------------------


class TestContactCache:
    def test_save_and_load_cache(self, mock_app, gw_config, tmp_path):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()

        plugin._contact_cache = {"key1": {"adv_name": "Node1"}}
        plugin._save_contact_cache()

        # Load into a new plugin
        plugin2 = _make_plugin_no_start(mock_app, gw_config)
        plugin2.start()
        assert plugin2._contact_cache.get("key1", {}).get("adv_name") == "Node1"

        plugin.stop()
        plugin2.stop()

    def test_load_cache_missing_file(self, mock_app, gw_config, tmp_path):
        gw_config["storage_path"] = str(tmp_path / "nonexistent_dir")
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()
        assert plugin._contact_cache == {}
        plugin.stop()


# ---------------------------------------------------------------------------
# TestAdvertisements
# ---------------------------------------------------------------------------


class TestAdvertisements:
    def test_advert_interval_validation_too_low(self, mock_app, gw_config):
        gw_config["advert_interval"] = 30
        with pytest.raises(ValueError, match="advert_interval"):
            _make_plugin_no_start(mock_app, gw_config)

    def test_advert_interval_validation_non_numeric(self, mock_app, gw_config):
        gw_config["advert_interval"] = "fast"
        with pytest.raises(ValueError, match="advert_interval"):
            _make_plugin_no_start(mock_app, gw_config)

    def test_advert_interval_default(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()
        assert plugin._advert_interval == 900
        plugin.stop()

    def test_advert_interval_custom(self, mock_app, gw_config):
        gw_config["advert_interval"] = 300
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()
        assert plugin._advert_interval == 300
        plugin.stop()

    def test_connect_sends_initial_advert(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        with patch.object(plugin, "_connection_loop"):
            plugin.start()

        mc = _make_mock_meshcore_device()
        mc.commands.send_advert = AsyncMock()

        async def mock_create_serial(*args, **kwargs):
            return mc

        with patch("meshcore.MeshCore.create_serial", side_effect=mock_create_serial):
            plugin._connect_device()

        mc.commands.send_advert.assert_awaited_once()
        assert plugin._last_advert_time > 0

        plugin.stop()

    def test_send_periodic_advert(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()

        mc = _make_mock_meshcore_device()
        mc.commands.send_advert = AsyncMock()
        plugin._mc = mc
        plugin._connected = True

        plugin._send_periodic_advert()

        mc.commands.send_advert.assert_awaited_once()
        assert plugin._last_advert_time > 0

        plugin.stop()

    def test_send_periodic_advert_skips_when_no_mc(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()

        # _mc is None by default
        plugin._send_periodic_advert()
        # Should not raise
        assert plugin._last_advert_time == 0

        plugin.stop()


class TestBroadcastCache:
    def test_returns_cached_within_ttl(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()

        result1 = plugin.broadcast_snapshot()
        assert result1 is not None

        result2 = plugin.broadcast_snapshot()
        assert result2 is result1
        plugin.stop()

    def test_refreshes_after_ttl(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()

        result1 = plugin.broadcast_snapshot()
        assert result1 is not None

        plugin._broadcast_cache = (
            plugin._broadcast_cache[0] - plugin._broadcast_cache_ttl - 1,
            plugin._broadcast_cache[1],
        )

        result2 = plugin.broadcast_snapshot()
        assert result2 is not result1
        plugin.stop()
