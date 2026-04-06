"""Tests for the messaging_hub plugin."""

import time
from unittest.mock import MagicMock, patch

import pytest
import RNS as _RNS

from reticulumpi.builtin_plugins.messaging_hub import (
    LXMFAdapter,
    MeshtasticAdapter,
    MessageStore,
    MessagingHubPlugin,
    TransportAdapter,
)
from reticulumpi import events


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def store(tmp_path):
    """Create a MessageStore backed by a temp SQLite file."""
    s = MessageStore(str(tmp_path / "test_messages.db"))
    yield s
    s.close()


@pytest.fixture
def hub_plugin(mock_app, tmp_path):
    """Create a MessagingHubPlugin with LXMF and Meshtastic disabled."""
    config = {
        "db_path": str(tmp_path / "hub.db"),
        "message_history_limit": 100,
        "lxmf": {"enabled": False},
        "meshtastic": {"enabled": False},
    }
    plugin = MessagingHubPlugin(mock_app, config)
    plugin.start()
    yield plugin
    plugin.stop()


@pytest.fixture
def hub_with_lxmf(mock_app, tmp_path):
    """Create a MessagingHubPlugin with a mocked LXMF adapter."""
    config = {
        "db_path": str(tmp_path / "hub.db"),
        "message_history_limit": 100,
        "lxmf": {
            "enabled": True,
            "storage_path": str(tmp_path / "lxmf"),
            "display_name": "Test Messages",
        },
        "meshtastic": {"enabled": False},
    }
    mock_identity = MagicMock()
    mock_identity.hash = b"\x01" * 16

    with (
        patch("LXMF.LXMRouter") as mock_router_cls,
        patch("RNS.Identity", return_value=mock_identity),
        patch.object(_RNS.Transport, "register_announce_handler"),
        patch.object(_RNS.Transport, "deregister_announce_handler"),
    ):
        mock_router = MagicMock()
        mock_dest = MagicMock()
        mock_dest.hash = b"\x01" * 16
        mock_router.register_delivery_identity.return_value = mock_dest
        mock_router_cls.return_value = mock_router

        plugin = MessagingHubPlugin(mock_app, config)
        plugin.start()
        yield plugin
        plugin.stop()


# ═══════════════════════════════════════════════════════════════════
# TransportAdapter base class
# ═══════════════════════════════════════════════════════════════════


class TestTransportAdapter:
    def test_send_raises_not_implemented(self):
        adapter = TransportAdapter()
        with pytest.raises(NotImplementedError):
            adapter.send("hello", "dest123")

    def test_defaults(self):
        adapter = TransportAdapter()
        assert adapter.get_contacts() == []
        assert adapter.is_available() is False
        assert adapter.transport_name == ""

    def test_on_message_received_sets_callback(self):
        adapter = TransportAdapter()
        cb = MagicMock()
        adapter.on_message_received(cb)
        assert adapter._hub_callback is cb


# ═══════════════════════════════════════════════════════════════════
# MessageStore
# ═══════════════════════════════════════════════════════════════════


class TestMessageStore:
    def test_store_and_retrieve(self, store):
        msg_id = store.store(
            transport="lxmf",
            direction="received",
            msg_type="direct",
            text="Hello world",
            from_id="sender123",
            from_name="Alice",
        )
        assert msg_id > 0

        msgs = store.get_messages()
        assert len(msgs) == 1
        assert msgs[0]["text"] == "Hello world"
        assert msgs[0]["transport"] == "lxmf"
        assert msgs[0]["from_id"] == "sender123"
        assert msgs[0]["from_name"] == "Alice"
        assert msgs[0]["direction"] == "received"

    def test_get_message_by_id(self, store):
        msg_id = store.store(
            transport="meshtastic", direction="sent", msg_type="broadcast",
            text="Test msg",
        )
        msg = store.get_message(msg_id)
        assert msg is not None
        assert msg["text"] == "Test msg"
        assert msg["transport"] == "meshtastic"

    def test_get_message_missing(self, store):
        assert store.get_message(9999) is None

    def test_filter_by_transport(self, store):
        store.store(transport="lxmf", direction="received", msg_type="direct", text="A")
        store.store(transport="meshtastic", direction="received", msg_type="broadcast", text="B")
        store.store(transport="lxmf", direction="sent", msg_type="direct", text="C")

        lxmf_msgs = store.get_messages(transport="lxmf")
        assert len(lxmf_msgs) == 2
        assert all(m["transport"] == "lxmf" for m in lxmf_msgs)

    def test_filter_by_direction(self, store):
        store.store(transport="lxmf", direction="received", msg_type="direct", text="A")
        store.store(transport="lxmf", direction="sent", msg_type="direct", text="B")

        sent = store.get_messages(direction="sent")
        assert len(sent) == 1
        assert sent[0]["text"] == "B"

    def test_filter_by_since(self, store):
        store.store(transport="lxmf", direction="received", msg_type="direct", text="Old")
        cutoff = time.time()
        time.sleep(0.01)
        store.store(transport="lxmf", direction="received", msg_type="direct", text="New")

        msgs = store.get_messages(since=cutoff)
        assert len(msgs) == 1
        assert msgs[0]["text"] == "New"

    def test_pagination(self, store):
        for i in range(10):
            store.store(
                transport="lxmf", direction="received", msg_type="direct",
                text=f"Msg {i}",
            )
        page1 = store.get_messages(limit=3, offset=0)
        page2 = store.get_messages(limit=3, offset=3)
        assert len(page1) == 3
        assert len(page2) == 3
        # Newest first
        assert page1[0]["text"] != page2[0]["text"]

    def test_update_status(self, store):
        msg_id = store.store(
            transport="lxmf", direction="sent", msg_type="direct",
            text="Test", status="pending",
        )
        store.update_status(msg_id, "delivered")
        msg = store.get_message(msg_id)
        assert msg["status"] == "delivered"

    def test_get_stats(self, store):
        store.store(transport="lxmf", direction="received", msg_type="direct", text="A")
        store.store(transport="lxmf", direction="sent", msg_type="direct", text="B")
        store.store(transport="meshtastic", direction="received", msg_type="broadcast", text="C")

        stats = store.get_stats()
        assert stats["total"] == 3
        assert stats["by_transport"]["lxmf"] == 2
        assert stats["by_transport"]["meshtastic"] == 1
        assert stats["by_direction"]["received"] == 2
        assert stats["by_direction"]["sent"] == 1

    def test_prune(self, store):
        for i in range(10):
            store.store(
                transport="lxmf", direction="received", msg_type="direct",
                text=f"Msg {i}",
            )
        deleted = store.prune(5)
        assert deleted == 5
        remaining = store.get_messages(limit=100)
        assert len(remaining) == 5

    def test_prune_noop_when_under_limit(self, store):
        store.store(transport="lxmf", direction="received", msg_type="direct", text="A")
        deleted = store.prune(100)
        assert deleted == 0

    def test_metadata_round_trip(self, store):
        meta = {"key": "value", "num": 42}
        msg_id = store.store(
            transport="lxmf", direction="received", msg_type="direct",
            text="Test", metadata=meta,
        )
        msg = store.get_message(msg_id)
        assert msg["metadata"] == meta


# ═══════════════════════════════════════════════════════════════════
# MessagingHubPlugin
# ═══════════════════════════════════════════════════════════════════


class TestMessagingHubPlugin:
    def test_register_adapter(self, hub_plugin):
        adapter = MagicMock(spec=TransportAdapter)
        adapter.transport_name = "test_transport"
        adapter.display_name = "Test Transport"

        hub_plugin.register_adapter(adapter)

        adapter.on_message_received.assert_called_once()
        adapter.start.assert_called_once()
        assert "test_transport" in hub_plugin._adapters

    def test_register_adapter_start_failure(self, hub_plugin):
        adapter = MagicMock(spec=TransportAdapter)
        adapter.transport_name = "bad"
        adapter.display_name = "Bad"
        adapter.start.side_effect = RuntimeError("fail")

        hub_plugin.register_adapter(adapter)

        assert "bad" not in hub_plugin._adapters

    def test_send_message_stores_and_publishes(self, hub_plugin):
        adapter = MagicMock(spec=TransportAdapter)
        adapter.transport_name = "test"
        adapter.display_name = "Test"
        adapter.is_available.return_value = True
        adapter.send.return_value = {"sent": True}
        adapter.get_contacts.return_value = []
        hub_plugin.register_adapter(adapter)

        result = hub_plugin.send_message("test", "Hello!", "dest123")

        assert result["sent"] is True
        assert "msg_id" in result
        msgs = hub_plugin.get_messages()
        assert len(msgs) == 1
        assert msgs[0]["text"] == "Hello!"
        assert msgs[0]["direction"] == "sent"
        hub_plugin.event_bus.publish.assert_any_call(
            events.MESSAGE_SENT,
            pytest.approx(
                {
                    "id": result["msg_id"],
                    "transport": "test",
                    "destination": "dest123",
                    "text": "Hello!",
                    "timestamp": pytest.approx(time.time(), abs=2),
                }
            ),
        )

    def test_send_message_transport_not_registered(self, hub_plugin):
        result = hub_plugin.send_message("nonexistent", "hi", "dest")
        assert result["sent"] is False
        assert "not registered" in result["reason"]

    def test_send_message_transport_unavailable(self, hub_plugin):
        adapter = MagicMock(spec=TransportAdapter)
        adapter.transport_name = "test"
        adapter.display_name = "Test"
        adapter.is_available.return_value = False
        hub_plugin.register_adapter(adapter)

        result = hub_plugin.send_message("test", "hi", "dest")
        assert result["sent"] is False
        assert "not available" in result["reason"]

    def test_send_message_failure_stores_as_failed(self, hub_plugin):
        adapter = MagicMock(spec=TransportAdapter)
        adapter.transport_name = "test"
        adapter.display_name = "Test"
        adapter.is_available.return_value = True
        adapter.send.return_value = {"sent": False, "reason": "timeout"}
        adapter.get_contacts.return_value = []
        hub_plugin.register_adapter(adapter)

        result = hub_plugin.send_message("test", "Hello!", "dest123")
        assert result["sent"] is False
        msgs = hub_plugin.get_messages()
        assert msgs[0]["status"] == "failed"

    def test_on_adapter_message_stores_and_publishes(self, hub_plugin):
        msg_data = {
            "transport": "lxmf",
            "from_id": "abc123",
            "from_name": "Alice",
            "to_id": None,
            "to_name": None,
            "text": "Hello from Alice",
            "msg_type": "direct",
        }
        hub_plugin._on_adapter_message(msg_data)

        msgs = hub_plugin.get_messages()
        assert len(msgs) == 1
        assert msgs[0]["text"] == "Hello from Alice"
        assert msgs[0]["direction"] == "received"
        assert msgs[0]["status"] == "received"
        hub_plugin.event_bus.publish.assert_called()

    def test_get_transports(self, hub_plugin):
        adapter = MagicMock(spec=TransportAdapter)
        adapter.transport_name = "test"
        adapter.display_name = "Test"
        adapter.is_available.return_value = True
        hub_plugin.register_adapter(adapter)

        transports = hub_plugin.get_transports()
        assert len(transports) == 1
        assert transports[0]["name"] == "test"
        assert transports[0]["available"] is True

    def test_get_contacts_all(self, hub_plugin):
        a1 = MagicMock(spec=TransportAdapter)
        a1.transport_name = "t1"
        a1.display_name = "T1"
        a1.get_contacts.return_value = [{"id": "1", "name": "One", "transport": "t1"}]

        a2 = MagicMock(spec=TransportAdapter)
        a2.transport_name = "t2"
        a2.display_name = "T2"
        a2.get_contacts.return_value = [{"id": "2", "name": "Two", "transport": "t2"}]

        hub_plugin.register_adapter(a1)
        hub_plugin.register_adapter(a2)

        contacts = hub_plugin.get_contacts()
        assert len(contacts) == 2

    def test_get_contacts_filtered(self, hub_plugin):
        a1 = MagicMock(spec=TransportAdapter)
        a1.transport_name = "t1"
        a1.display_name = "T1"
        a1.get_contacts.return_value = [{"id": "1", "name": "One", "transport": "t1"}]

        a2 = MagicMock(spec=TransportAdapter)
        a2.transport_name = "t2"
        a2.display_name = "T2"
        a2.get_contacts.return_value = [{"id": "2", "name": "Two", "transport": "t2"}]

        hub_plugin.register_adapter(a1)
        hub_plugin.register_adapter(a2)

        contacts = hub_plugin.get_contacts(transport="t1")
        assert len(contacts) == 1
        assert contacts[0]["id"] == "1"

    def test_get_status(self, hub_plugin):
        status = hub_plugin.get_status()
        assert status["active"] is True
        assert "total_messages" in status
        assert "transports" in status

    def test_history_limit_prunes(self, hub_plugin):
        hub_plugin._history_limit = 5
        for i in range(10):
            hub_plugin._on_adapter_message({
                "transport": "lxmf",
                "text": f"msg {i}",
                "msg_type": "direct",
            })
        msgs = hub_plugin.get_messages(limit=100)
        assert len(msgs) == 5

    def test_validate_config_bad_limit(self, mock_app, tmp_path):
        config = {
            "db_path": str(tmp_path / "hub.db"),
            "message_history_limit": -1,
            "lxmf": {"enabled": False},
            "meshtastic": {"enabled": False},
        }
        with pytest.raises(ValueError, match="message_history_limit"):
            MessagingHubPlugin(mock_app, config)


# ═══════════════════════════════════════════════════════════════════
# LXMFAdapter
# ═══════════════════════════════════════════════════════════════════


class TestLXMFAdapter:
    def test_is_available_before_start(self):
        hub = MagicMock()
        adapter = LXMFAdapter(hub)
        assert adapter.is_available() is False
        assert adapter.address is None

    def test_start_creates_identity_and_router(self, mock_app, tmp_path):
        hub = MagicMock()
        hub.app = mock_app
        hub.config = {
            "lxmf": {
                "storage_path": str(tmp_path / "lxmf"),
                "display_name": "Test Hub",
            }
        }
        hub.log = MagicMock()

        mock_identity = MagicMock()
        mock_identity.hash = b"\x02" * 16

        with (
            patch("LXMF.LXMRouter") as mock_router_cls,
            patch("RNS.Identity", return_value=mock_identity),
            patch.object(_RNS.Transport, "register_announce_handler"),
        ):
            mock_router = MagicMock()
            mock_dest = MagicMock()
            mock_dest.hash = b"\x02" * 16
            mock_router.register_delivery_identity.return_value = mock_dest
            mock_router_cls.return_value = mock_router

            adapter = LXMFAdapter(hub)
            adapter.start()

            assert adapter.is_available() is True
            assert adapter.address == (b"\x02" * 16).hex()
            mock_router.register_delivery_callback.assert_called_once()

    def test_send_with_mocked_lxmf(self, mock_app, tmp_path):
        hub = MagicMock()
        hub.app = mock_app
        hub.config = {
            "lxmf": {
                "storage_path": str(tmp_path / "lxmf"),
                "display_name": "Test Hub",
            }
        }
        hub.log = MagicMock()
        mock_app.get_plugin.return_value = None  # No path_warmer

        mock_identity = MagicMock()
        mock_identity.hash = b"\x02" * 16

        with (
            patch("LXMF.LXMRouter") as mock_router_cls,
            patch("LXMF.LXMessage"),
            patch("RNS.Identity", return_value=mock_identity),
            patch.object(_RNS.Identity, "recall") as mock_recall,
            patch("RNS.Destination"),
            patch.object(_RNS.Transport, "register_announce_handler"),
        ):
            mock_router = MagicMock()
            mock_dest = MagicMock()
            mock_dest.hash = b"\x02" * 16
            mock_router.register_delivery_identity.return_value = mock_dest
            mock_router_cls.return_value = mock_router

            # Recall returns a destination identity
            mock_recall.return_value = MagicMock()

            adapter = LXMFAdapter(hub)
            adapter.start()

            result = adapter.send("Hello!", "aa" * 16)

            assert result["sent"] is True
            mock_router.handle_outbound.assert_called_once()

    def test_send_path_not_found(self, mock_app, tmp_path):
        hub = MagicMock()
        hub.app = mock_app
        hub.config = {
            "lxmf": {
                "storage_path": str(tmp_path / "lxmf"),
            }
        }
        hub.log = MagicMock()
        mock_app.get_plugin.return_value = None

        mock_identity = MagicMock()
        mock_identity.hash = b"\x02" * 16

        with (
            patch("LXMF.LXMRouter") as mock_router_cls,
            patch("RNS.Identity", return_value=mock_identity),
            patch.object(_RNS.Identity, "recall", return_value=None),
            patch.object(_RNS.Transport, "request_path"),
            patch.object(_RNS.Transport, "register_announce_handler"),
        ):
            mock_router = MagicMock()
            mock_dest = MagicMock()
            mock_dest.hash = b"\x02" * 16
            mock_router.register_delivery_identity.return_value = mock_dest
            mock_router_cls.return_value = mock_router

            adapter = LXMFAdapter(hub)
            adapter.start()

            result = adapter.send("Hello!", "bb" * 16)
            assert result["sent"] is False
            assert "not found" in result["reason"].lower()

    def test_send_invalid_destination(self, mock_app, tmp_path):
        hub = MagicMock()
        hub.app = mock_app
        hub.config = {"lxmf": {"storage_path": str(tmp_path / "lxmf")}}
        hub.log = MagicMock()

        mock_identity = MagicMock()
        mock_identity.hash = b"\x02" * 16

        with (
            patch("LXMF.LXMRouter") as mock_router_cls,
            patch("RNS.Identity", return_value=mock_identity),
            patch.object(_RNS.Transport, "register_announce_handler"),
        ):
            mock_router = MagicMock()
            mock_dest = MagicMock()
            mock_dest.hash = b"\x02" * 16
            mock_router.register_delivery_identity.return_value = mock_dest
            mock_router_cls.return_value = mock_router

            adapter = LXMFAdapter(hub)
            adapter.start()

            result = adapter.send("Hello!", "not-hex")
            assert result["sent"] is False
            assert "invalid" in result["reason"].lower()

    def test_inbound_callback(self, mock_app, tmp_path):
        hub = MagicMock()
        hub.app = mock_app
        hub.config = {
            "lxmf": {
                "storage_path": str(tmp_path / "lxmf"),
            }
        }
        hub.log = MagicMock()

        mock_identity = MagicMock()
        mock_identity.hash = b"\x02" * 16

        with (
            patch("LXMF.LXMRouter") as mock_router_cls,
            patch("RNS.Identity", return_value=mock_identity),
            patch.object(_RNS.Transport, "register_announce_handler"),
        ):
            mock_router = MagicMock()
            mock_dest = MagicMock()
            mock_dest.hash = b"\x02" * 16
            mock_router.register_delivery_identity.return_value = mock_dest
            mock_router_cls.return_value = mock_router

            adapter = LXMFAdapter(hub)
            adapter.start()

            callback = MagicMock()
            adapter.on_message_received(callback)

            # Simulate incoming LXMF message
            fake_msg = MagicMock()
            fake_msg.source_hash = b"\xaa" * 16
            fake_msg.content_as_string.return_value = "Test message"

            adapter._on_lxmf_message(fake_msg)

            callback.assert_called_once()
            call_args = callback.call_args[0][0]
            assert call_args["transport"] == "lxmf"
            assert call_args["text"] == "Test message"
            assert call_args["from_id"] == (b"\xaa" * 16).hex()


# ═══════════════════════════════════════════════════════════════════
# MeshtasticAdapter
# ═══════════════════════════════════════════════════════════════════


class TestMeshtasticAdapter:
    def test_send_delegates_to_gateway(self, mock_app):
        hub = MagicMock()
        hub.app = mock_app
        hub.event_bus = mock_app.event_bus

        gw = MagicMock()
        gw.send_message.return_value = {"sent": True}
        mock_app.get_plugin.return_value = gw

        adapter = MeshtasticAdapter(hub)
        result = adapter.send("Hello mesh!", "!abcd1234")

        gw.send_message.assert_called_once_with("Hello mesh!", destination_id="!abcd1234")
        assert result["sent"] is True

    def test_send_broadcast(self, mock_app):
        hub = MagicMock()
        hub.app = mock_app
        hub.event_bus = mock_app.event_bus

        gw = MagicMock()
        gw.send_message.return_value = {"sent": True}
        mock_app.get_plugin.return_value = gw

        adapter = MeshtasticAdapter(hub)
        result = adapter.send("Hello all!", "broadcast")

        gw.send_message.assert_called_once_with("Hello all!", destination_id=None)
        assert result["sent"] is True

    def test_send_gateway_unavailable(self, mock_app):
        hub = MagicMock()
        hub.app = mock_app
        hub.event_bus = mock_app.event_bus
        mock_app.get_plugin.return_value = None

        adapter = MeshtasticAdapter(hub)
        result = adapter.send("Hello!", "!1234")
        assert result["sent"] is False
        assert "not available" in result["reason"]

    def test_get_contacts_maps_nodes(self, mock_app):
        hub = MagicMock()
        hub.app = mock_app
        hub.event_bus = mock_app.event_bus

        gw = MagicMock()
        gw.get_meshtastic_nodes.return_value = [
            {"id": "!aabb1122", "long_name": "Node A", "is_self": False},
            {"id": "!ccdd3344", "long_name": "Gateway", "is_self": True},
            {"id": "!eeff5566", "short_name": "NB", "is_self": False},
        ]
        mock_app.get_plugin.return_value = gw

        adapter = MeshtasticAdapter(hub)
        contacts = adapter.get_contacts()

        # Should exclude is_self
        assert len(contacts) == 2
        assert contacts[0]["id"] == "!aabb1122"
        assert contacts[0]["name"] == "Node A"
        assert contacts[1]["id"] == "!eeff5566"
        assert contacts[1]["name"] == "NB"

    def test_is_available_checks_gateway(self, mock_app):
        hub = MagicMock()
        hub.app = mock_app
        hub.event_bus = mock_app.event_bus

        gw = MagicMock()
        gw.get_status.return_value = {"connected": True}
        mock_app.get_plugin.return_value = gw

        adapter = MeshtasticAdapter(hub)
        assert adapter.is_available() is True

        gw.get_status.return_value = {"connected": False}
        assert adapter.is_available() is False

    def test_is_available_no_gateway(self, mock_app):
        hub = MagicMock()
        hub.app = mock_app
        hub.event_bus = mock_app.event_bus
        mock_app.get_plugin.return_value = None

        adapter = MeshtasticAdapter(hub)
        assert adapter.is_available() is False

    def test_event_flow_to_hub(self, mock_app):
        hub = MagicMock()
        hub.app = mock_app
        hub.event_bus = mock_app.event_bus

        adapter = MeshtasticAdapter(hub)
        callback = MagicMock()
        adapter.on_message_received(callback)

        # Simulate event from gateway
        adapter._on_mesh_event(events.MESHTASTIC_MESSAGE_RECEIVED, {
            "from_id": "!aabb1122",
            "text": "Hello from mesh",
            "forwarded_to": 1,
        })

        callback.assert_called_once()
        call_args = callback.call_args[0][0]
        assert call_args["transport"] == "meshtastic"
        assert call_args["text"] == "Hello from mesh"
        assert call_args["from_id"] == "!aabb1122"


# ═══════════════════════════════════════════════════════════════════
# Integration: Hub with LXMF adapter
# ═══════════════════════════════════════════════════════════════════


class TestHubWithLXMF:
    def test_lxmf_adapter_registered(self, hub_with_lxmf):
        assert "lxmf" in hub_with_lxmf._adapters
        transports = hub_with_lxmf.get_transports()
        assert any(t["name"] == "lxmf" for t in transports)

    def test_lxmf_address_in_transports(self, hub_with_lxmf):
        transports = hub_with_lxmf.get_transports()
        lxmf = next(t for t in transports if t["name"] == "lxmf")
        assert "address" in lxmf
        assert lxmf["address"] == (b"\x01" * 16).hex()
