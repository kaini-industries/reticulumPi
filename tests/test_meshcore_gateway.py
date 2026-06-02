"""Tests for the MeshCore Gateway plugin."""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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
    """Ensure meshcore is always available as a mock."""
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
        yield


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

    def test_raises_for_bad_baudrate(self, mock_app, gw_config):
        gw_config["baudrate"] = -1
        with pytest.raises(ValueError, match="baudrate"):
            _make_plugin_no_start(mock_app, gw_config)

    def test_raises_for_bad_health_check_interval(self, mock_app, gw_config):
        gw_config["health_check_interval"] = 2
        with pytest.raises(ValueError, match="health_check_interval"):
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


# ---------------------------------------------------------------------------
# TestConnectionManagement
# ---------------------------------------------------------------------------


class TestConnectionManagement:
    def test_connect_device_sets_state(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
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

    def test_disconnect_clears_state(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
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

    def test_check_health_true_when_connected(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()

        mc = _make_mock_meshcore_device()
        mc.is_connected = True
        plugin._mc = mc
        plugin._connected = True

        assert plugin._check_health() is True
        plugin.stop()

    def test_check_health_false_when_no_mc(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()
        assert plugin._check_health() is False
        plugin.stop()


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
        plugin._loop = asyncio.new_event_loop()
        plugin._loop_ready.set()

        # Run the loop in another thread briefly
        import threading

        t = threading.Thread(target=plugin._loop.run_forever, daemon=True)
        t.start()

        try:
            result = plugin.send_message("hello", destination="aabb" * 8)
            assert result["sent"] is True
            assert plugin._msgs_sent == 1
        finally:
            plugin._loop.call_soon_threadsafe(plugin._loop.stop)
            t.join(timeout=5)
            plugin._loop.close()
            plugin._loop = None
            plugin.stop()

    def test_send_channel_message(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        plugin.start()

        mc = _make_mock_meshcore_device()
        plugin._mc = mc
        plugin._connected = True
        plugin._loop = asyncio.new_event_loop()
        plugin._loop_ready.set()

        import threading

        t = threading.Thread(target=plugin._loop.run_forever, daemon=True)
        t.start()

        try:
            result = plugin.send_message("hello channel", channel=0)
            assert result["sent"] is True
            assert plugin._msgs_sent == 1
        finally:
            plugin._loop.call_soon_threadsafe(plugin._loop.stop)
            t.join(timeout=5)
            plugin._loop.close()
            plugin._loop = None
            plugin.stop()


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
