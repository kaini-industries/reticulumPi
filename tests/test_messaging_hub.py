"""Tests for the messaging_hub plugin."""

import time
from unittest.mock import ANY, MagicMock, patch

import pytest
import RNS as _RNS

from reticulumpi.builtin_plugins.messaging_hub import (
    LXMFAdapter,
    MeshCoreAdapter,
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
        "meshcore": {"enabled": False},
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
        "meshcore": {"enabled": False},
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

    def test_update_status_unless_terminal_blocks_overwrite(self, store):
        msg_id = store.store(
            transport="lxmf", direction="sent", msg_type="direct",
            text="Test", status="delivered",
        )
        terminal = frozenset({"delivered", "delivery_failed", "failed"})
        updated = store.update_status_unless_terminal(msg_id, "timeout", terminal)
        assert updated is False
        assert store.get_message(msg_id)["status"] == "delivered"

    def test_update_status_unless_terminal_allows_non_terminal(self, store):
        msg_id = store.store(
            transport="lxmf", direction="sent", msg_type="direct",
            text="Test", status="sent",
        )
        terminal = frozenset({"delivered", "delivery_failed", "failed"})
        updated = store.update_status_unless_terminal(msg_id, "timeout", terminal)
        assert updated is True
        assert store.get_message(msg_id)["status"] == "timeout"

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

    def test_prune_is_per_transport(self, store):
        # Regression: a single global cap let a chatty transport (mqtt
        # broadcasts) starve a sparse one (lxmf) by evicting it entirely.
        for i in range(10):
            store.store(
                transport="meshtastic", direction="received",
                msg_type="broadcast", text=f"mt {i}",
            )
        for i in range(2):
            store.store(
                transport="lxmf", direction="received",
                msg_type="direct", text=f"lx {i}",
            )
        deleted = store.prune(5)
        assert deleted == 5  # only the 5 oldest meshtastic rows
        msgs = store.get_messages(limit=100)
        by_t = {}
        for m in msgs:
            by_t[m["transport"]] = by_t.get(m["transport"], 0) + 1
        assert by_t.get("lxmf") == 2     # untouched
        assert by_t.get("meshtastic") == 5

    def test_prune_is_per_sub_transport(self, store):
        # Regression: bucketing by transport alone let chatty Meshtastic
        # MQTT broadcasts starve sparse LoRa DMs inside the same 500-row
        # meshtastic bucket. Prune must bucket per (transport, sub_transport).
        for i in range(10):
            store.store(
                transport="meshtastic", direction="received",
                sub_transport="mqtt", msg_type="broadcast", text=f"mq {i}",
            )
        for i in range(2):
            store.store(
                transport="meshtastic", direction="received",
                sub_transport="lora", msg_type="direct", text=f"lr {i}",
            )
        deleted = store.prune(5)
        assert deleted == 5  # only the 5 oldest mqtt rows
        msgs = store.get_messages(limit=100)
        by_sub: dict[str, int] = {}
        for m in msgs:
            by_sub[m["sub_transport"]] = by_sub.get(m["sub_transport"], 0) + 1
        assert by_sub.get("lora") == 2     # untouched
        assert by_sub.get("mqtt") == 5

    def test_delete_conversation_removes_only_that_thread(self, store):
        store.store(
            transport="lxmf", direction="received", msg_type="direct",
            text="keep A", from_id="aaaa", to_id="zzzz",
        )
        store.store(
            transport="lxmf", direction="received", msg_type="direct",
            text="delete me 1", from_id="bbbb", to_id="zzzz",
        )
        store.store(
            transport="lxmf", direction="sent", msg_type="direct",
            text="delete me 2", from_id="zzzz", to_id="bbbb",
        )
        deleted = store.delete_conversation("bbbb")
        assert deleted == 2
        remaining = [c["contact_id"] for c in store.get_conversations()]
        assert "bbbb" not in remaining
        assert "aaaa" in remaining

    def test_delete_conversation_missing_contact(self, store):
        deleted = store.delete_conversation("nobody")
        assert deleted == 0

    def test_metadata_round_trip(self, store):
        meta = {"key": "value", "num": 42}
        msg_id = store.store(
            transport="lxmf", direction="received", msg_type="direct",
            text="Test", metadata=meta,
        )
        msg = store.get_message(msg_id)
        assert msg["metadata"] == meta

    def test_dm_contact_id_splits_by_sub_transport(self, store):
        """DMs from the same Meshtastic peer split by MQTT vs LoRa source."""
        store.store(
            transport="meshtastic", direction="received", msg_type="direct",
            text="Hello via LoRa", from_id="!aabb1122", to_id="!ccdd3344",
            sub_transport="lora",
        )
        store.store(
            transport="meshtastic", direction="received", msg_type="direct",
            text="Hello via MQTT", from_id="!aabb1122", to_id="!ccdd3344",
            sub_transport="mqtt",
        )
        convos = store.get_conversations()
        # Each source produces its own conversation
        contact_ids = [c["contact_id"] for c in convos]
        assert "!aabb1122__lora" in contact_ids
        assert "!aabb1122__mqtt" in contact_ids

    def test_dm_same_sub_transport_groups(self, store):
        """Two DMs from the same peer via the same sub_transport group."""
        store.store(
            transport="meshtastic", direction="received", msg_type="direct",
            text="First via LoRa", from_id="!aabb1122", to_id="!ccdd3344",
            sub_transport="lora",
        )
        store.store(
            transport="meshtastic", direction="received", msg_type="direct",
            text="Second via LoRa", from_id="!aabb1122", to_id="!ccdd3344",
            sub_transport="lora",
        )
        convos = store.get_conversations()
        lora_dm = [c for c in convos if c["contact_id"] == "!aabb1122__lora"]
        assert len(lora_dm) == 1

    def test_broadcast_and_dm_are_separate_conversations(self, store):
        """Broadcast and DM from the same peer create separate conversations."""
        store.store(
            transport="meshtastic", direction="received", msg_type="broadcast",
            text="Broadcast msg", from_id="!aabb1122",
            sub_transport="lora",
        )
        store.store(
            transport="meshtastic", direction="received", msg_type="direct",
            text="Direct msg", from_id="!aabb1122", to_id="!ccdd3344",
            sub_transport="lora",
        )
        convos = store.get_conversations()
        contact_ids = [c["contact_id"] for c in convos]
        assert "__broadcast_meshtastic_lora__" in contact_ids
        assert "!aabb1122__lora" in contact_ids
        assert len(convos) == 2

    def test_sub_transport_filter_on_conversations(self, store):
        """get_conversations(sub_transport=...) filters by source channel."""
        store.store(
            transport="meshtastic", direction="received", msg_type="broadcast",
            text="MQTT broadcast", from_id="!aabb1122",
            sub_transport="mqtt",
        )
        store.store(
            transport="meshtastic", direction="received", msg_type="broadcast",
            text="LoRa broadcast", from_id="!ccdd3344",
            sub_transport="lora",
        )
        mqtt_convos = store.get_conversations(
            transport="meshtastic", sub_transport="mqtt",
        )
        lora_convos = store.get_conversations(
            transport="meshtastic", sub_transport="lora",
        )
        assert len(mqtt_convos) == 1
        assert len(lora_convos) == 1
        assert mqtt_convos[0]["sub_transport"] == "mqtt"
        assert lora_convos[0]["sub_transport"] == "lora"

    def test_sub_transport_filter_on_messages(self, store):
        """get_messages(sub_transport=...) filters by source channel."""
        store.store(
            transport="meshtastic", direction="received", msg_type="broadcast",
            text="MQTT msg", from_id="!aa", sub_transport="mqtt",
        )
        store.store(
            transport="meshtastic", direction="received", msg_type="broadcast",
            text="LoRa msg", from_id="!aa", sub_transport="lora",
        )
        mqtt = store.get_messages(sub_transport="mqtt")
        lora = store.get_messages(sub_transport="lora")
        assert len(mqtt) == 1 and mqtt[0]["text"] == "MQTT msg"
        assert len(lora) == 1 and lora[0]["text"] == "LoRa msg"

    def test_broadcast_contact_id_splits_by_channel(self, store):
        """Broadcasts on different Meshtastic channels get their own threads."""
        store.store(
            transport="meshtastic", direction="received", msg_type="broadcast",
            text="Primary", from_id="!aabb1122",
            sub_transport="lora", channel=0,
        )
        store.store(
            transport="meshtastic", direction="received", msg_type="broadcast",
            text="Private channel", from_id="!aabb1122",
            sub_transport="lora", channel=1,
        )
        contact_ids = [c["contact_id"] for c in store.get_conversations()]
        assert "__broadcast_meshtastic_lora_ch0__" in contact_ids
        assert "__broadcast_meshtastic_lora_ch1__" in contact_ids
        assert len(contact_ids) == 2

    def test_dm_contact_id_ignores_channel(self, store):
        """DMs from the same peer stay in one thread regardless of channel."""
        store.store(
            transport="meshtastic", direction="received", msg_type="direct",
            text="via ch0", from_id="!aabb1122", to_id="!ccdd3344",
            sub_transport="lora", channel=0,
        )
        store.store(
            transport="meshtastic", direction="received", msg_type="direct",
            text="via ch3", from_id="!aabb1122", to_id="!ccdd3344",
            sub_transport="lora", channel=3,
        )
        contact_ids = [c["contact_id"] for c in store.get_conversations()]
        assert contact_ids == ["!aabb1122__lora"]

    def test_store_records_channel_in_metadata(self, store):
        """channel kwarg mirrors into metadata['channel'] for display/query."""
        msg_id = store.store(
            transport="meshtastic", direction="received", msg_type="broadcast",
            text="hi", from_id="!aa", sub_transport="lora", channel=2,
        )
        msg = store.get_message(msg_id)
        assert msg["metadata"]["channel"] == 2

    def test_broadcast_channel_accepts_string(self, store):
        """String channel (MQTT peer on an unmapped channel) creates its own thread."""
        store.store(
            transport="meshtastic", direction="received", msg_type="broadcast",
            text="from LongFast peer", from_id="!aa",
            sub_transport="mqtt", channel="LongFast",
        )
        contact_ids = [c["contact_id"] for c in store.get_conversations()]
        assert "__broadcast_meshtastic_mqtt_chLongFast__" in contact_ids

    def test_broadcast_int_and_str_channels_are_distinct(self, store):
        """int 0 and str 'LongFast' create separate conversations even if they semantically overlap."""
        store.store(
            transport="meshtastic", direction="received", msg_type="broadcast",
            text="via index 0", from_id="!aa",
            sub_transport="mqtt", channel=0,
        )
        store.store(
            transport="meshtastic", direction="received", msg_type="broadcast",
            text="via name", from_id="!bb",
            sub_transport="mqtt", channel="LongFast",
        )
        contact_ids = [c["contact_id"] for c in store.get_conversations()]
        assert "__broadcast_meshtastic_mqtt_ch0__" in contact_ids
        assert "__broadcast_meshtastic_mqtt_chLongFast__" in contact_ids
        assert len(contact_ids) == 2

    def test_legacy_broadcast_rows_cleaned_on_init(self, tmp_path):
        """Pre-channel-split Meshtastic broadcast rows are purged at init."""
        db = str(tmp_path / "legacy.db")
        # Seed with a legacy-format Meshtastic broadcast row plus an LXMF
        # broadcast (no channels -> shouldn't be purged) and a DM.
        s1 = MessageStore(db)
        import sqlite3
        with sqlite3.connect(db) as conn:
            # Insert a legacy-format broadcast row directly to simulate
            # pre-upgrade data that the new code can't bucket by channel.
            conn.execute(
                "INSERT INTO messages "
                "(timestamp, transport, direction, msg_type, "
                " from_id, text, contact_id, sub_transport) "
                "VALUES (?, 'meshtastic', 'received', 'broadcast', "
                " '!aa', 'legacy', '__broadcast_meshtastic_lora__', 'lora')",
                (time.time(),),
            )
            conn.execute(
                "INSERT INTO messages "
                "(timestamp, transport, direction, msg_type, "
                " from_id, text, contact_id, sub_transport) "
                "VALUES (?, 'lxmf', 'received', 'broadcast', "
                " 'peer', 'lxmf_bcast', '__broadcast_lxmf__', '')",
                (time.time(),),
            )
            conn.commit()
        s1.close()

        # Re-open — the cleanup runs in __init__ via _migrate_schema.
        s2 = MessageStore(db)
        try:
            contact_ids = [c["contact_id"] for c in s2.get_conversations()]
            # Legacy Meshtastic broadcast gone
            assert "__broadcast_meshtastic_lora__" not in contact_ids
            # LXMF broadcast untouched (no channel concept on LXMF)
            assert "__broadcast_lxmf__" in contact_ids
        finally:
            s2.close()


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
                    "sub_transport": "",
                    "contact_id": "dest123",
                    "direction": "sent",
                    "status": "sent",
                    "destination": "dest123",
                    "text": "Hello!",
                    "msg_type": "direct",
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

    def test_send_broadcast_defaults_channel_to_zero(self, hub_plugin):
        """send_message(broadcast) with no channel lands on channel 0."""
        adapter = MagicMock(spec=TransportAdapter)
        adapter.transport_name = "meshtastic"
        adapter.display_name = "Meshtastic"
        adapter.is_available.return_value = True
        adapter.send.return_value = {"sent": True}
        adapter.get_contacts.return_value = []
        hub_plugin.register_adapter(adapter)

        hub_plugin.send_message(
            "meshtastic", "hello", "broadcast",
            msg_type="broadcast", sub_transport="lora",
        )
        msgs = hub_plugin.get_messages()
        assert len(msgs) == 1
        assert msgs[0]["contact_id"] == "__broadcast_meshtastic_lora_ch0__"

    def test_adapter_message_propagates_channel(self, hub_plugin):
        """Inbound broadcast with channel kwarg lands in per-channel thread."""
        hub_plugin._on_adapter_message({
            "transport": "meshtastic",
            "sub_transport": "lora",
            "from_id": "!aabb1122",
            "from_name": "Alice",
            "to_id": None,
            "to_name": None,
            "text": "private chat",
            "msg_type": "broadcast",
            "channel": 4,
        })
        msgs = hub_plugin.get_messages()
        assert len(msgs) == 1
        assert msgs[0]["contact_id"] == "__broadcast_meshtastic_lora_ch4__"
        assert msgs[0]["metadata"]["channel"] == 4

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

    def test_get_contacts_search_by_name(self, hub_plugin):
        a = MagicMock(spec=TransportAdapter)
        a.transport_name = "meshtastic"
        a.display_name = "Meshtastic"
        a.get_contacts.return_value = [
            {"id": "!aabb1122", "name": "Trashman", "transport": "meshtastic"},
            {"id": "!ccdd3344", "name": "Hilltop Relay", "transport": "meshtastic"},
            {"id": "!eeff5566", "name": "Base Camp", "transport": "meshtastic"},
        ]
        hub_plugin.register_adapter(a)

        # Search by partial name (case-insensitive)
        contacts = hub_plugin.get_contacts(query="trash")
        assert len(contacts) == 1
        assert contacts[0]["id"] == "!aabb1122"

    def test_get_contacts_search_by_id(self, hub_plugin):
        a = MagicMock(spec=TransportAdapter)
        a.transport_name = "meshtastic"
        a.display_name = "Meshtastic"
        a.get_contacts.return_value = [
            {"id": "!aabb1122", "name": "Node A", "transport": "meshtastic"},
            {"id": "!ccdd3344", "name": "Node B", "transport": "meshtastic"},
        ]
        hub_plugin.register_adapter(a)

        contacts = hub_plugin.get_contacts(query="ccdd")
        assert len(contacts) == 1
        assert contacts[0]["id"] == "!ccdd3344"

    def test_get_contacts_search_no_match(self, hub_plugin):
        a = MagicMock(spec=TransportAdapter)
        a.transport_name = "meshtastic"
        a.display_name = "Meshtastic"
        a.get_contacts.return_value = [
            {"id": "!aabb1122", "name": "Node A", "transport": "meshtastic"},
        ]
        hub_plugin.register_adapter(a)

        contacts = hub_plugin.get_contacts(query="zzz_no_match")
        assert len(contacts) == 0

    def test_get_contacts_sorted_by_last_heard(self, hub_plugin):
        a = MagicMock(spec=TransportAdapter)
        a.transport_name = "meshtastic"
        a.display_name = "Meshtastic"
        a.get_contacts.return_value = [
            {"id": "!old", "name": "Old", "transport": "meshtastic", "last_heard": 1000},
            {"id": "!new", "name": "New", "transport": "meshtastic", "last_heard": 3000},
            {"id": "!mid", "name": "Mid", "transport": "meshtastic", "last_heard": 2000},
            {"id": "!none", "name": "Never", "transport": "meshtastic"},
        ]
        hub_plugin.register_adapter(a)

        contacts = hub_plugin.get_contacts()
        assert contacts[0]["id"] == "!new"
        assert contacts[1]["id"] == "!mid"
        assert contacts[2]["id"] == "!old"
        # Node without last_heard sorts last
        assert contacts[3]["id"] == "!none"

    def test_get_status(self, hub_plugin):
        status = hub_plugin.get_status()
        assert status["active"] is True
        assert "total_messages" in status
        assert "transports" in status

    def test_history_limit_prunes(self, hub_plugin):
        hub_plugin._history_limit = 5
        for i in range(10):
            # Bypass the 30s prune throttle so every store triggers a prune
            # and we can observe the history limit taking effect deterministically.
            hub_plugin._last_prune_ts = 0.0
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

    def test_broadcast_snapshot_includes_conversations(self, hub_plugin):
        adapter = MagicMock(spec=TransportAdapter)
        adapter.transport_name = "test"
        adapter.display_name = "Test"
        adapter.is_available.return_value = True
        adapter.send.return_value = {"sent": True}
        adapter.get_contacts.return_value = []
        hub_plugin.register_adapter(adapter)

        hub_plugin._store.store(
            transport="test", direction="received", msg_type="direct",
            text="hello", from_id="alice1", from_name="Alice",
        )

        snapshot = hub_plugin.broadcast_snapshot(cycle_count=0)
        assert snapshot is not None
        assert "transports" in snapshot
        assert "unread" in snapshot
        assert "conversations" in snapshot
        assert len(snapshot["conversations"]) == 1
        assert snapshot["conversations"][0]["contact_name"] == "Alice"


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


class TestLXMFNameResolution:
    """LXMFAdapter should look up announced display names via network_map."""

    def _build_adapter(self, mock_app, tmp_path):
        hub = MagicMock()
        hub.app = mock_app
        hub.config = {"lxmf": {"storage_path": str(tmp_path / "lxmf")}}
        hub.log = MagicMock()

        mock_identity = MagicMock()
        mock_identity.hash = b"\x02" * 16

        patches = [
            patch("LXMF.LXMRouter"),
            patch("RNS.Identity", return_value=mock_identity),
            patch.object(_RNS.Transport, "register_announce_handler"),
        ]
        for p in patches:
            p.start()

        mock_router = MagicMock()
        mock_dest = MagicMock()
        mock_dest.hash = b"\x02" * 16
        mock_router.register_delivery_identity.return_value = mock_dest
        __import__("LXMF").LXMRouter.return_value = mock_router

        adapter = LXMFAdapter(hub)
        adapter.start()
        return adapter, hub, patches

    def _teardown(self, patches):
        for p in patches:
            p.stop()

    def test_inbound_uses_network_map_name(self, mock_app, tmp_path):
        adapter, hub, patches = self._build_adapter(mock_app, tmp_path)
        try:
            nm = MagicMock()
            nm.get_node_name.return_value = "Alice"
            mock_app.get_plugin.return_value = nm

            cb = MagicMock()
            adapter.on_message_received(cb)

            fake_msg = MagicMock()
            fake_msg.source_hash = b"\xaa" * 16
            fake_msg.content_as_string.return_value = "hi"
            adapter._on_lxmf_message(fake_msg)

            payload = cb.call_args[0][0]
            assert payload["from_name"] == "Alice"
            nm.get_node_name.assert_called_once_with((b"\xaa" * 16).hex())
        finally:
            self._teardown(patches)

    def test_inbound_falls_back_to_pretty_hash_when_plugin_absent(
        self, mock_app, tmp_path
    ):
        adapter, hub, patches = self._build_adapter(mock_app, tmp_path)
        try:
            mock_app.get_plugin.return_value = None

            cb = MagicMock()
            adapter.on_message_received(cb)

            fake_msg = MagicMock()
            fake_msg.source_hash = b"\xaa" * 16
            fake_msg.content_as_string.return_value = "hi"
            adapter._on_lxmf_message(fake_msg)

            payload = cb.call_args[0][0]
            assert payload["from_name"] == _RNS.prettyhexrep(b"\xaa" * 16)
        finally:
            self._teardown(patches)

    def test_inbound_falls_back_when_plugin_returns_none(
        self, mock_app, tmp_path
    ):
        adapter, hub, patches = self._build_adapter(mock_app, tmp_path)
        try:
            nm = MagicMock()
            nm.get_node_name.return_value = None
            mock_app.get_plugin.return_value = nm

            cb = MagicMock()
            adapter.on_message_received(cb)

            fake_msg = MagicMock()
            fake_msg.source_hash = b"\xbb" * 16
            fake_msg.content_as_string.return_value = "hi"
            adapter._on_lxmf_message(fake_msg)

            payload = cb.call_args[0][0]
            assert payload["from_name"] == _RNS.prettyhexrep(b"\xbb" * 16)
        finally:
            self._teardown(patches)

    def test_inbound_falls_back_when_plugin_raises(self, mock_app, tmp_path):
        adapter, hub, patches = self._build_adapter(mock_app, tmp_path)
        try:
            nm = MagicMock()
            nm.get_node_name.side_effect = RuntimeError("boom")
            mock_app.get_plugin.return_value = nm

            cb = MagicMock()
            adapter.on_message_received(cb)

            fake_msg = MagicMock()
            fake_msg.source_hash = b"\xcc" * 16
            fake_msg.content_as_string.return_value = "hi"
            adapter._on_lxmf_message(fake_msg)

            payload = cb.call_args[0][0]
            assert payload["from_name"] == _RNS.prettyhexrep(b"\xcc" * 16)
        finally:
            self._teardown(patches)

    def test_finalize_send_lxmf_uses_network_map_for_to_name(
        self, hub_plugin
    ):
        """Outbound LXMF without contact match should resolve to_name via network_map."""
        adapter = MagicMock(spec=TransportAdapter)
        adapter.transport_name = "lxmf"
        adapter.display_name = "LXMF"
        adapter.is_available.return_value = True
        adapter.get_contacts.return_value = []
        adapter.send.return_value = {"sent": True}
        hub_plugin.register_adapter(adapter)

        nm = MagicMock()
        nm.get_node_name.return_value = "Bob"
        hub_plugin.app.get_plugin = MagicMock(return_value=nm)

        hub_plugin.send_message("lxmf", "hi", "deadbeef" * 4)

        stored = hub_plugin.get_messages()[-1]
        assert stored["to_name"] == "Bob"
        nm.get_node_name.assert_called_once_with("deadbeef" * 4)

    def test_finalize_send_non_lxmf_does_not_consult_network_map(
        self, hub_plugin
    ):
        """Regression guard: Meshtastic/MeshCore paths must not hit network_map."""
        adapter = MagicMock(spec=TransportAdapter)
        adapter.transport_name = "meshtastic"
        adapter.display_name = "Meshtastic"
        adapter.is_available.return_value = True
        adapter.get_contacts.return_value = []
        adapter.send.return_value = {"sent": True}
        hub_plugin.register_adapter(adapter)

        nm = MagicMock()
        hub_plugin.app.get_plugin = MagicMock(return_value=nm)

        hub_plugin.send_message("meshtastic", "hi", "!abcd1234")

        nm.get_node_name.assert_not_called()

    def test_finalize_send_contact_match_skips_network_map(self, hub_plugin):
        """If contacts provide a name, network_map should not be consulted."""
        adapter = MagicMock(spec=TransportAdapter)
        adapter.transport_name = "lxmf"
        adapter.is_available.return_value = True
        adapter.get_contacts.return_value = [
            {"id": "deadbeef" * 4, "name": "Charlie"},
        ]
        adapter.send.return_value = {"sent": True}
        hub_plugin.register_adapter(adapter)

        nm = MagicMock()
        hub_plugin.app.get_plugin = MagicMock(return_value=nm)

        hub_plugin.send_message("lxmf", "hi", "deadbeef" * 4)

        stored = hub_plugin.get_messages()[-1]
        assert stored["to_name"] == "Charlie"
        nm.get_node_name.assert_not_called()


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

        gw.send_message.assert_called_once_with(
            "Hello mesh!", destination_id="!abcd1234", channel=None,
            via="lora", on_ack=ANY,
        )
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

        gw.send_message.assert_called_once_with(
            "Hello all!", destination_id=None, channel=None, via="",
            on_ack=ANY,
        )
        assert result["sent"] is True

    def test_send_via_lora(self, mock_app):
        hub = MagicMock()
        hub.app = mock_app
        hub.event_bus = mock_app.event_bus

        gw = MagicMock()
        gw.send_message.return_value = {"sent": True}
        mock_app.get_plugin.return_value = gw

        adapter = MeshtasticAdapter(hub)
        result = adapter.send("Local msg!", "broadcast", sub_transport="lora")

        gw.send_message.assert_called_once_with(
            "Local msg!", destination_id=None, channel=None, via="lora",
            on_ack=ANY,
        )
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
            {"id": "!aabb1122", "long_name": "Node A", "is_self": False, "last_heard": 1000},
            {"id": "!ccdd3344", "long_name": "Gateway", "is_self": True},
            {"id": "!eeff5566", "short_name": "NB", "is_self": False, "last_heard": 2000},
        ]
        mock_app.get_plugin.return_value = gw

        adapter = MeshtasticAdapter(hub)
        contacts = adapter.get_contacts()

        # Should exclude is_self
        assert len(contacts) == 2
        assert contacts[0]["id"] == "!aabb1122"
        assert contacts[0]["name"] == "Node A"
        assert contacts[0]["last_heard"] == 1000
        assert contacts[1]["id"] == "!eeff5566"
        assert contacts[1]["name"] == "NB"
        assert contacts[1]["last_heard"] == 2000

    def test_is_available_checks_gateway(self, mock_app):
        hub = MagicMock()
        hub.app = mock_app
        hub.event_bus = mock_app.event_bus

        gw = MagicMock()
        gw.get_status.return_value = {"connected": True, "serial_available": False}
        mock_app.get_plugin.return_value = gw

        adapter = MeshtasticAdapter(hub)
        assert adapter.is_available() is True

        gw.get_status.return_value = {"connected": False, "serial_available": False}
        assert adapter.is_available() is False

    def test_is_available_serial_fallback(self, mock_app):
        hub = MagicMock()
        hub.app = mock_app
        hub.event_bus = mock_app.event_bus

        gw = MagicMock()
        gw.get_status.return_value = {"connected": False, "serial_available": True}
        mock_app.get_plugin.return_value = gw

        adapter = MeshtasticAdapter(hub)
        assert adapter.is_available() is True

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

        # Simulate broadcast event from gateway
        adapter._on_mesh_event(events.MESHTASTIC_MESSAGE_RECEIVED, {
            "from_id": "!aabb1122",
            "text": "Hello from mesh",
            "forwarded_to": 1,
            "is_broadcast": True,
            "source": "LoRa",
        })

        callback.assert_called_once()
        call_args = callback.call_args[0][0]
        assert call_args["transport"] == "meshtastic"
        assert call_args["text"] == "Hello from mesh"
        assert call_args["from_id"] == "!aabb1122"
        assert call_args["msg_type"] == "broadcast"
        assert call_args["sub_transport"] == "lora"

    def test_dm_event_routes_as_direct(self, mock_app):
        hub = MagicMock()
        hub.app = mock_app
        hub.event_bus = mock_app.event_bus

        adapter = MeshtasticAdapter(hub)
        callback = MagicMock()
        adapter.on_message_received(callback)

        adapter._on_mesh_event(events.MESHTASTIC_MESSAGE_RECEIVED, {
            "from_id": "!aabb1122",
            "from_name": "Solar Node",
            "to_id": "!ccdd3344",
            "is_broadcast": False,
            "text": "Hello direct",
            "source": "LoRa",
        })

        callback.assert_called_once()
        call_args = callback.call_args[0][0]
        assert call_args["transport"] == "meshtastic"
        assert call_args["msg_type"] == "direct"
        assert call_args["from_id"] == "!aabb1122"
        assert call_args["from_name"] == "Solar Node"
        assert call_args["to_id"] == "!ccdd3344"
        # DMs now carry sub_transport so MQTT vs LoRa panels stay separate
        assert call_args["sub_transport"] == "lora"

    def test_mqtt_dm_event_tags_sub_transport(self, mock_app):
        """DMs arriving via MQTT are tagged sub_transport='mqtt'."""
        hub = MagicMock()
        hub.app = mock_app
        hub.event_bus = mock_app.event_bus

        adapter = MeshtasticAdapter(hub)
        callback = MagicMock()
        adapter.on_message_received(callback)

        adapter._on_mesh_event(events.MESHTASTIC_MESSAGE_RECEIVED, {
            "from_id": "!aabb1122",
            "to_id": "!ccdd3344",
            "is_broadcast": False,
            "text": "Hello via MQTT",
            "source": "MQTT",
        })

        call_args = callback.call_args[0][0]
        assert call_args["msg_type"] == "direct"
        assert call_args["sub_transport"] == "mqtt"

    def test_legacy_event_without_source_defaults_lora(self, mock_app):
        """Events that predate the source field fall back to 'lora'."""
        hub = MagicMock()
        hub.app = mock_app
        hub.event_bus = mock_app.event_bus

        adapter = MeshtasticAdapter(hub)
        callback = MagicMock()
        adapter.on_message_received(callback)

        adapter._on_mesh_event(events.MESHTASTIC_MESSAGE_RECEIVED, {
            "from_id": "!aabb1122",
            "text": "No source field",
            "is_broadcast": True,
        })

        assert callback.call_args[0][0]["sub_transport"] == "lora"

    def test_missing_is_broadcast_defaults_to_broadcast(self, mock_app):
        hub = MagicMock()
        hub.app = mock_app
        hub.event_bus = mock_app.event_bus

        adapter = MeshtasticAdapter(hub)
        callback = MagicMock()
        adapter.on_message_received(callback)

        # Old-format event without is_broadcast field
        adapter._on_mesh_event(events.MESHTASTIC_MESSAGE_RECEIVED, {
            "from_id": "!aabb1122",
            "text": "Legacy format",
            "source": "LoRa",
        })

        callback.assert_called_once()
        call_args = callback.call_args[0][0]
        assert call_args["msg_type"] == "broadcast"


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


# ═══════════════════════════════════════════════════════════════════
# Delivery Tracking
# ═══════════════════════════════════════════════════════════════════


class TestLXMFDeliveryTracking:
    def test_lxmf_delivery_callback_updates_status(self, mock_app, tmp_path):
        """LXMF delivery callback updates the message status in the store."""
        hub = MagicMock()
        hub.app = mock_app
        hub.config = {"lxmf": {"storage_path": str(tmp_path / "lxmf")}}
        hub.log = MagicMock()

        adapter = LXMFAdapter(hub)
        # Track a pending message
        adapter.track_pending(42, "aabbccdd")
        assert "aabbccdd" in adapter.get_pending_delivery()

        # Simulate delivery
        import LXMF

        fake_msg = MagicMock()
        fake_msg.hash = bytes.fromhex("aabbccdd")
        fake_msg.state = LXMF.LXMessage.DELIVERED

        adapter._on_lxmf_delivery(fake_msg)

        hub._on_delivery_status_update.assert_called_once_with(
            42, "lxmf", "delivered",
        )
        assert "aabbccdd" not in adapter.get_pending_delivery()

    def test_lxmf_propagated_callback(self, mock_app, tmp_path):
        """LXMF propagated state sets status to 'propagated'."""
        hub = MagicMock()
        hub.app = mock_app
        hub.config = {"lxmf": {"storage_path": str(tmp_path / "lxmf")}}
        hub.log = MagicMock()

        adapter = LXMFAdapter(hub)
        adapter.track_pending(43, "11223344")

        import LXMF

        fake_msg = MagicMock()
        fake_msg.hash = bytes.fromhex("11223344")
        fake_msg.state = LXMF.LXMessage.SENT  # propagated, not delivered

        adapter._on_lxmf_delivery(fake_msg)
        hub._on_delivery_status_update.assert_called_once_with(
            43, "lxmf", "propagated",
        )

    def test_lxmf_failed_callback(self, mock_app, tmp_path):
        """LXMF failed callback updates status to delivery_failed."""
        hub = MagicMock()
        hub.app = mock_app
        hub.config = {"lxmf": {"storage_path": str(tmp_path / "lxmf")}}
        hub.log = MagicMock()

        adapter = LXMFAdapter(hub)
        adapter.track_pending(44, "deadbeef")

        fake_msg = MagicMock()
        fake_msg.hash = bytes.fromhex("deadbeef")

        adapter._on_lxmf_failed(fake_msg)
        hub._on_delivery_status_update.assert_called_once_with(
            44, "lxmf", "delivery_failed",
        )

    def test_lxmf_callback_ignores_unknown_hash(self, mock_app, tmp_path):
        """Callbacks for messages we aren't tracking are silently ignored."""
        hub = MagicMock()
        hub.app = mock_app
        hub.config = {"lxmf": {"storage_path": str(tmp_path / "lxmf")}}
        hub.log = MagicMock()

        import LXMF

        adapter = LXMFAdapter(hub)
        fake_msg = MagicMock()
        fake_msg.hash = bytes.fromhex("00000000")
        fake_msg.state = LXMF.LXMessage.DELIVERED

        adapter._on_lxmf_delivery(fake_msg)
        hub._on_delivery_status_update.assert_not_called()


class TestMeshtasticDeliveryTracking:
    def test_ack_callback_updates_status(self, mock_app):
        """Meshtastic ack callback triggers a delivery status update."""
        hub = MagicMock()
        hub.app = mock_app
        hub.event_bus = mock_app.event_bus

        adapter = MeshtasticAdapter(hub)
        adapter.track_pending(50, "serial")
        assert "50" in adapter.get_pending_delivery()

        # Simulate the on_ack closure being called with acked=True
        # We need to go through the actual send flow to get the closure
        gw = MagicMock()
        gw.send_message.return_value = {"sent": True, "ack_tracking": "serial"}
        mock_app.get_plugin.return_value = gw

        result = adapter.send("Test!", "!abcd1234")

        # Extract the on_ack callback from the gateway call
        call_kwargs = gw.send_message.call_args[1]
        on_ack = call_kwargs["on_ack"]

        # Bind msg_id via the _ack_holder mechanism
        ack_holder = result.get("_ack_holder")
        assert ack_holder is not None
        ack_holder["msg_id"] = 55

        # Simulate ack
        on_ack(True)
        hub._on_delivery_status_update.assert_called_once_with(
            55, "meshtastic", "delivered",
        )

    def test_nak_callback_sets_delivery_failed(self, mock_app):
        """Meshtastic nak sets delivery_failed."""
        hub = MagicMock()
        hub.app = mock_app
        hub.event_bus = mock_app.event_bus

        gw = MagicMock()
        gw.send_message.return_value = {"sent": True, "ack_tracking": "serial"}
        mock_app.get_plugin.return_value = gw

        adapter = MeshtasticAdapter(hub)
        result = adapter.send("Test!", "!abcd1234")

        call_kwargs = gw.send_message.call_args[1]
        on_ack = call_kwargs["on_ack"]

        ack_holder = result.get("_ack_holder")
        ack_holder["msg_id"] = 56

        on_ack(False)
        hub._on_delivery_status_update.assert_called_once_with(
            56, "meshtastic", "delivery_failed",
        )


class TestMeshCoreDeliveryTracking:
    def test_ack_event_updates_status(self, mock_app):
        """MeshCore ACK event triggers delivery status update."""
        hub = MagicMock()
        hub.app = mock_app
        hub.event_bus = mock_app.event_bus

        adapter = MeshCoreAdapter(hub)
        adapter.track_pending(60, "abcd1234")
        assert "abcd1234" in adapter.get_pending_delivery()

        # Simulate ACK event
        adapter._on_meshcore_ack(
            events.MESHCORE_MESSAGE_ACKED,
            {"ack_code": "abcd1234"},
        )

        hub._on_delivery_status_update.assert_called_once_with(
            60, "meshcore", "delivered",
        )
        assert "abcd1234" not in adapter.get_pending_delivery()

    def test_ack_event_ignores_unknown_code(self, mock_app):
        """ACK for unknown ack_code is silently ignored."""
        hub = MagicMock()
        hub.app = mock_app
        hub.event_bus = mock_app.event_bus

        adapter = MeshCoreAdapter(hub)
        adapter._on_meshcore_ack(
            events.MESHCORE_MESSAGE_ACKED,
            {"ack_code": "unknown"},
        )
        hub._on_delivery_status_update.assert_not_called()


class TestHubDeliveryStatus:
    def test_on_delivery_status_update(self, hub_plugin):
        """_on_delivery_status_update persists status and publishes event."""
        msg_id = hub_plugin._store.store(
            transport="lxmf", direction="sent", msg_type="direct",
            text="Test", status="sent",
        )
        hub_plugin._on_delivery_status_update(msg_id, "lxmf", "delivered")

        msg = hub_plugin._store.get_message(msg_id)
        assert msg["status"] == "delivered"
        hub_plugin.event_bus.publish.assert_any_call(
            events.MESSAGE_STATUS_CHANGED,
            pytest.approx({
                "id": msg_id,
                "transport": "lxmf",
                "status": "delivered",
                "timestamp": pytest.approx(time.time(), abs=2),
                "contact_id": "__unknown__",
                "sub_transport": "",
            }),
        )

    def test_timeout_does_not_overwrite_delivered(self, hub_plugin):
        msg_id = hub_plugin._store.store(
            transport="lxmf", direction="sent", msg_type="direct",
            text="Test", status="sent",
        )
        hub_plugin._on_delivery_status_update(msg_id, "lxmf", "delivered")
        hub_plugin._on_delivery_status_update(msg_id, "lxmf", "timeout")
        assert hub_plugin._store.get_message(msg_id)["status"] == "delivered"

    def test_timeout_does_not_overwrite_delivery_failed(self, hub_plugin):
        msg_id = hub_plugin._store.store(
            transport="lxmf", direction="sent", msg_type="direct",
            text="Test", status="sent",
        )
        hub_plugin._on_delivery_status_update(msg_id, "lxmf", "delivery_failed")
        hub_plugin._on_delivery_status_update(msg_id, "lxmf", "timeout")
        assert hub_plugin._store.get_message(msg_id)["status"] == "delivery_failed"

    def test_expired_does_not_overwrite_delivered(self, hub_plugin):
        msg_id = hub_plugin._store.store(
            transport="lxmf", direction="sent", msg_type="direct",
            text="Test", status="sent",
        )
        hub_plugin._on_delivery_status_update(msg_id, "lxmf", "delivered")
        hub_plugin._on_delivery_status_update(msg_id, "lxmf", "expired")
        assert hub_plugin._store.get_message(msg_id)["status"] == "delivered"

    def test_timeout_does_not_overwrite_failed(self, hub_plugin):
        msg_id = hub_plugin._store.store(
            transport="lxmf", direction="sent", msg_type="direct",
            text="Test", status="sent",
        )
        hub_plugin._on_delivery_status_update(msg_id, "lxmf", "failed")
        hub_plugin._on_delivery_status_update(msg_id, "lxmf", "timeout")
        assert hub_plugin._store.get_message(msg_id)["status"] == "failed"

    def test_timeout_can_overwrite_sent(self, hub_plugin):
        msg_id = hub_plugin._store.store(
            transport="lxmf", direction="sent", msg_type="direct",
            text="Test", status="sent",
        )
        hub_plugin._on_delivery_status_update(msg_id, "lxmf", "timeout")
        assert hub_plugin._store.get_message(msg_id)["status"] == "timeout"

    def test_expire_stale_pending(self, hub_plugin):
        """expire_stale_pending marks old entries as timeout."""
        # Store a message first so we have a valid msg_id
        msg_id = hub_plugin._store.store(
            transport="test_lxmf", direction="sent", msg_type="direct",
            text="Stale", status="sent",
        )

        # Register a mock adapter with a stale pending entry using the real msg_id
        adapter = MagicMock(spec=LXMFAdapter)
        adapter.transport_name = "test_lxmf"
        adapter.display_name = "Test LXMF"
        adapter._pending_lock = __import__("threading").Lock()
        adapter._pending_delivery = {
            "stale_hash": {"msg_id": msg_id, "timestamp": time.time() - 600},
        }
        adapter.get_pending_delivery.return_value = dict(adapter._pending_delivery)
        adapter.track_pending = MagicMock()
        hub_plugin.register_adapter(adapter)

        expired = hub_plugin.expire_stale_pending(max_age=300)
        assert expired == 1

        # Verify the DB row was actually updated
        msg = hub_plugin._store.get_message(msg_id)
        assert msg["status"] == "timeout"


class TestOutboundQueueEvents:
    """Dashboard visibility for queued sends and drain-time status flips.

    Gap being closed: a send that could not go out (adapter reports
    not_connected) used to be silently written to the retry queue with
    no event, so the dashboard had no bubble until a later full refresh.
    Drain-time success then updated the row in the store but did not
    publish MESSAGE_STATUS_CHANGED, so any client that DID have the row
    saw it pinned at "queued" forever.
    """

    @staticmethod
    def _queueable_adapter(transport: str = "test"):
        """Return a mock adapter that reports unavailable at send time."""
        adapter = MagicMock(spec=TransportAdapter)
        adapter.transport_name = transport
        adapter.display_name = transport.title()
        # is_available gate lets send_message through; send() returning
        # not_connected is what actually triggers queueing.
        adapter.is_available.return_value = True
        adapter.send.return_value = {"sent": False, "reason": "not_connected"}
        adapter.get_contacts.return_value = []
        return adapter

    def test_queue_publishes_message_sent_with_row_id(self, hub_plugin):
        adapter = self._queueable_adapter()
        hub_plugin.register_adapter(adapter)

        result = hub_plugin.send_message("test", "hi", "dest-x")

        assert result["queued"] is True
        msg_id = result["msg_id"]
        hub_plugin.event_bus.publish.assert_any_call(
            events.MESSAGE_SENT,
            pytest.approx({
                "id": msg_id,
                "transport": "test",
                "sub_transport": "",
                "contact_id": "dest-x",
                "direction": "sent",
                "status": "queued",
                "destination": "dest-x",
                "text": "hi",
                "msg_type": "direct",
                "timestamp": pytest.approx(time.time(), abs=2),
            }),
        )

    def test_queue_stores_row_with_queued_status(self, hub_plugin):
        adapter = self._queueable_adapter()
        hub_plugin.register_adapter(adapter)

        result = hub_plugin.send_message("test", "hi", "dest-x")
        stored = hub_plugin._store.get_message(result["msg_id"])

        assert stored["status"] == "queued"
        assert stored["direction"] == "sent"

    def test_drain_success_publishes_status_changed(self, hub_plugin):
        """The queued→sent transition must broadcast MESSAGE_STATUS_CHANGED.

        Without this event the dashboard bubble appears as "queued" when
        the row is first created, and never updates — the status change
        would otherwise only land in the store.
        """
        adapter = self._queueable_adapter()
        hub_plugin.register_adapter(adapter)

        # First send queues.
        result = hub_plugin.send_message("test", "hi", "dest-x")
        msg_id = result["msg_id"]

        # Transport comes back up and the retry succeeds.
        adapter.send.return_value = {"sent": True}
        hub_plugin.event_bus.publish.reset_mock()
        sent, requeued, expired = hub_plugin._drain_outbound_queue("test")

        assert (sent, requeued, expired) == (1, 0, 0)
        hub_plugin.event_bus.publish.assert_any_call(
            events.MESSAGE_STATUS_CHANGED,
            pytest.approx({
                "id": msg_id,
                "transport": "test",
                "status": "sent",
                "timestamp": pytest.approx(time.time(), abs=2),
                "contact_id": "dest-x",
                "sub_transport": "",
            }),
        )

    def test_drain_success_still_publishes_message_sent(self, hub_plugin):
        """MESSAGE_SENT kept for pure-transmit subscribers — verify it still fires."""
        adapter = self._queueable_adapter()
        hub_plugin.register_adapter(adapter)
        result = hub_plugin.send_message("test", "hi", "dest-x")
        msg_id = result["msg_id"]

        adapter.send.return_value = {"sent": True}
        hub_plugin.event_bus.publish.reset_mock()
        hub_plugin._drain_outbound_queue("test")

        sent_calls = [
            call for call in hub_plugin.event_bus.publish.call_args_list
            if call.args[0] == events.MESSAGE_SENT
            and call.args[1].get("id") == msg_id
        ]
        assert len(sent_calls) == 1

    def test_drain_success_updates_store_status(self, hub_plugin):
        adapter = self._queueable_adapter()
        hub_plugin.register_adapter(adapter)
        result = hub_plugin.send_message("test", "hi", "dest-x")
        msg_id = result["msg_id"]

        adapter.send.return_value = {"sent": True}
        hub_plugin._drain_outbound_queue("test")

        assert hub_plugin._store.get_message(msg_id)["status"] == "sent"

    def test_drain_hard_failure_publishes_status_changed_failed(self, hub_plugin):
        """Non-queueable error on retry → status "failed" broadcast (pre-existing path)."""
        adapter = self._queueable_adapter()
        hub_plugin.register_adapter(adapter)
        result = hub_plugin.send_message("test", "hi", "dest-x")
        msg_id = result["msg_id"]

        adapter.send.return_value = {"sent": False, "reason": "invalid_dest"}
        hub_plugin.event_bus.publish.reset_mock()
        sent, requeued, expired = hub_plugin._drain_outbound_queue("test")

        assert (sent, requeued, expired) == (0, 0, 1)
        hub_plugin.event_bus.publish.assert_any_call(
            events.MESSAGE_STATUS_CHANGED,
            pytest.approx({
                "id": msg_id,
                "transport": "test",
                "status": "failed",
                "timestamp": pytest.approx(time.time(), abs=2),
                "contact_id": "dest-x",
                "sub_transport": "",
            }),
        )

    def test_drain_respects_time_budget(self, hub_plugin):
        """Drain stops and requeues remaining items when time budget expires."""
        adapter = self._queueable_adapter()
        hub_plugin.register_adapter(adapter)

        # Queue 5 messages.
        for i in range(5):
            hub_plugin.send_message("test", f"msg-{i}", "dest-x")

        # Make adapter available so drain attempts sends, but each send
        # is slow enough that only one fits in the budget.
        call_count = 0
        def slow_send(text, destination, **kw):
            nonlocal call_count
            call_count += 1
            return {"sent": True}
        adapter.send.side_effect = slow_send
        adapter.is_available.return_value = True

        # Set a tiny budget so the first send exceeds it.
        hub_plugin._drain_time_budget_s = 0.0

        sent, requeued, expired = hub_plugin._drain_outbound_queue("test")

        # At most 1 send should have gone through before budget hit.
        assert sent <= 1
        assert requeued >= 4
        assert sent + requeued + expired == 5


class TestRetryableReasons:
    """Verify _is_retryable_reason correctly identifies queueable failures."""

    @pytest.mark.parametrize(
        "reason",
        [
            "not_connected",
            "not connected",
            "Path not found, requested",
            "path not found",
            "Path not found, something else",
        ],
    )
    def test_retryable(self, reason):
        assert MessagingHubPlugin._is_retryable_reason(reason) is True

    @pytest.mark.parametrize(
        "reason",
        [
            "timeout",
            "invalid_dest",
            "rate_limited",
            "",
            "some other error",
        ],
    )
    def test_not_retryable(self, reason):
        assert MessagingHubPlugin._is_retryable_reason(reason) is False

    def test_send_message_queues_path_not_found(self, hub_plugin):
        adapter = MagicMock(spec=TransportAdapter)
        adapter.transport_name = "lxmf"
        adapter.display_name = "LXMF"
        adapter.is_available.return_value = True
        adapter.send.return_value = {
            "sent": False,
            "reason": "Path not found, requested",
        }
        adapter.get_contacts.return_value = []
        hub_plugin.register_adapter(adapter)

        result = hub_plugin.send_message("lxmf", "hello", "aabbccdd")

        assert result.get("queued") is True
        stored = hub_plugin._store.get_message(result["msg_id"])
        assert stored["status"] == "queued"

    def test_drain_requeues_path_not_found(self, hub_plugin):
        adapter = MagicMock(spec=TransportAdapter)
        adapter.transport_name = "lxmf"
        adapter.display_name = "LXMF"
        adapter.is_available.return_value = True
        adapter.send.return_value = {
            "sent": False,
            "reason": "Path not found, requested",
        }
        adapter.get_contacts.return_value = []
        hub_plugin.register_adapter(adapter)

        hub_plugin.send_message("lxmf", "hello", "aabbccdd")

        sent, requeued, expired = hub_plugin._drain_outbound_queue("lxmf")
        assert requeued == 1
        assert sent == 0


class TestQueueRecoveryOnRestart:
    """Verify queued messages are recovered from SQLite on hub restart."""

    def test_recover_queued_messages(self, mock_app, tmp_path):
        db_path = str(tmp_path / "hub.db")
        config = {
            "db_path": db_path,
            "message_history_limit": 100,
            "lxmf": {"enabled": False},
            "meshtastic": {"enabled": False},
            "meshcore": {"enabled": False},
        }

        # Start hub, queue a message, stop.
        hub1 = MessagingHubPlugin(mock_app, config)
        hub1.start()
        adapter = MagicMock(spec=TransportAdapter)
        adapter.transport_name = "test"
        adapter.display_name = "Test"
        adapter.is_available.return_value = True
        adapter.send.return_value = {"sent": False, "reason": "not_connected"}
        adapter.get_contacts.return_value = []
        hub1.register_adapter(adapter)
        result = hub1.send_message("test", "survive restart", "dest-1")
        assert result.get("queued") is True
        msg_id = result["msg_id"]
        hub1.stop()

        # Restart hub with same DB — message should be recovered.
        hub2 = MessagingHubPlugin(mock_app, config)
        hub2.start()
        from collections import deque
        q = hub2._outbound_queues.get("test", deque())
        assert len(q) == 1
        assert q[0]["msg_id"] == msg_id
        assert q[0]["text"] == "survive restart"
        assert q[0]["destination"] == "dest-1"
        hub2.stop()

    def test_recover_skips_expired(self, mock_app, tmp_path):
        db_path = str(tmp_path / "hub.db")
        config = {
            "db_path": db_path,
            "message_history_limit": 100,
            "outbound_queue_ttl_seconds": 1,
            "lxmf": {"enabled": False},
            "meshtastic": {"enabled": False},
            "meshcore": {"enabled": False},
        }

        hub1 = MessagingHubPlugin(mock_app, config)
        hub1.start()
        adapter = MagicMock(spec=TransportAdapter)
        adapter.transport_name = "test"
        adapter.display_name = "Test"
        adapter.is_available.return_value = True
        adapter.send.return_value = {"sent": False, "reason": "not_connected"}
        adapter.get_contacts.return_value = []
        hub1.register_adapter(adapter)
        hub1.send_message("test", "old message", "dest-1")
        hub1.stop()

        # Wait for TTL to expire.
        time.sleep(1.1)

        hub2 = MessagingHubPlugin(mock_app, config)
        hub2.start()
        from collections import deque
        q = hub2._outbound_queues.get("test", deque())
        assert len(q) == 0
        hub2.stop()

    def test_recover_respects_per_transport_limit(self, mock_app, tmp_path):
        db_path = str(tmp_path / "hub.db")
        config = {
            "db_path": db_path,
            "message_history_limit": 100,
            "outbound_queue_max": 2,
            "lxmf": {"enabled": False},
            "meshtastic": {"enabled": False},
            "meshcore": {"enabled": False},
        }

        hub1 = MessagingHubPlugin(mock_app, config)
        hub1.start()
        adapter = MagicMock(spec=TransportAdapter)
        adapter.transport_name = "test"
        adapter.display_name = "Test"
        adapter.is_available.return_value = True
        adapter.send.return_value = {"sent": False, "reason": "not_connected"}
        adapter.get_contacts.return_value = []
        hub1.register_adapter(adapter)
        for i in range(5):
            hub1.send_message("test", f"msg-{i}", "dest-1")
        hub1.stop()

        hub2 = MessagingHubPlugin(mock_app, config)
        hub2.start()
        from collections import deque
        q = hub2._outbound_queues.get("test", deque())
        assert len(q) <= 2
        hub2.stop()


class TestGetQueuedSent:
    """Verify MessageStore.get_queued_sent filtering."""

    def test_returns_only_queued_sent(self, store):
        store.store(transport="t", direction="sent", msg_type="direct",
                    text="a", status="queued")
        store.store(transport="t", direction="sent", msg_type="direct",
                    text="b", status="sent")
        store.store(transport="t", direction="received", msg_type="direct",
                    text="c", status="received")

        rows = store.get_queued_sent()
        assert len(rows) == 1
        assert rows[0]["text"] == "a"

    def test_max_age_filters_old(self, store):
        store.store(transport="t", direction="sent", msg_type="direct",
                    text="old", status="queued")
        # Backdate the row.
        store._conn.execute(
            "UPDATE messages SET timestamp = ? WHERE text = 'old'",
            (time.time() - 1000,),
        )
        store._conn.commit()

        rows = store.get_queued_sent(max_age_s=60)
        assert len(rows) == 0

        rows = store.get_queued_sent(max_age_s=2000)
        assert len(rows) == 1


# ═══════════════════════════════════════════════════════════════════
# Broadcast sent status
# ═══════════════════════════════════════════════════════════════════


class TestBroadcastSentStatus:
    def test_broadcast_send_transitions_to_broadcast_sent(self, hub_plugin):
        adapter = MagicMock(spec=TransportAdapter)
        adapter.transport_name = "meshtastic"
        adapter.display_name = "Meshtastic"
        adapter.is_available.return_value = True
        adapter.send.return_value = {"sent": True, "ack_tracking": None, "packet_id": 42}
        adapter.get_contacts.return_value = []
        hub_plugin.register_adapter(adapter)

        result = hub_plugin.send_message(
            "meshtastic", "Hello mesh!", "broadcast",
            msg_type="broadcast", sub_transport="lora",
        )
        assert result["sent"] is True
        msg = hub_plugin._store.get_message(result["msg_id"])
        assert msg["status"] == "broadcast_sent"

    def test_dm_send_stays_at_sent(self, hub_plugin):
        adapter = MagicMock(spec=TransportAdapter)
        adapter.transport_name = "meshtastic"
        adapter.display_name = "Meshtastic"
        adapter.is_available.return_value = True
        adapter.send.return_value = {"sent": True, "ack_tracking": "serial", "packet_id": 99}
        adapter.get_contacts.return_value = []
        hub_plugin.register_adapter(adapter)

        result = hub_plugin.send_message("meshtastic", "Hi!", "!abcd1234")
        msg = hub_plugin._store.get_message(result["msg_id"])
        assert msg["status"] == "sent"

    def test_broadcast_sent_is_terminal(self, store):
        msg_id = store.store(
            transport="meshtastic", direction="sent", msg_type="broadcast",
            text="bcast", status="broadcast_sent",
        )
        terminal = MessagingHubPlugin._TERMINAL_STATUSES
        updated = store.update_status_unless_terminal(msg_id, "timeout", terminal)
        assert updated is False
        assert store.get_status(msg_id) == "broadcast_sent"

    def test_broadcast_with_ack_tracking_not_promoted(self, hub_plugin):
        """If a future transport provides ack_tracking for broadcasts, don't promote."""
        adapter = MagicMock(spec=TransportAdapter)
        adapter.transport_name = "future"
        adapter.display_name = "Future"
        adapter.is_available.return_value = True
        adapter.send.return_value = {"sent": True, "ack_tracking": "radio"}
        adapter.get_contacts.return_value = []
        hub_plugin.register_adapter(adapter)

        result = hub_plugin.send_message(
            "future", "broadcast!", "broadcast", msg_type="broadcast",
        )
        msg = hub_plugin._store.get_message(result["msg_id"])
        assert msg["status"] == "sent"


# ═══════════════════════════════════════════════════════════════════
# Read receipts
# ═══════════════════════════════════════════════════════════════════


class TestFindSentByPacketId:
    def test_finds_matching_sent_message(self, store):
        msg_id = store.store(
            transport="meshtastic", direction="sent", msg_type="direct",
            text="hello", metadata={"packet_id": 12345},
        )
        found = store.find_sent_by_packet_id(12345)
        assert found == msg_id

    def test_ignores_received_messages(self, store):
        store.store(
            transport="meshtastic", direction="received", msg_type="direct",
            text="hi", metadata={"packet_id": 777},
        )
        assert store.find_sent_by_packet_id(777) is None

    def test_ignores_other_transports(self, store):
        store.store(
            transport="lxmf", direction="sent", msg_type="direct",
            text="hi", metadata={"packet_id": 888},
        )
        assert store.find_sent_by_packet_id(888) is None

    def test_returns_none_for_unknown(self, store):
        assert store.find_sent_by_packet_id(99999) is None


class TestReadReceipts:
    def test_inbound_read_receipt_updates_status(self, hub_plugin):
        msg_id = hub_plugin._store.store(
            transport="meshtastic", direction="sent", msg_type="direct",
            text="hello", status="delivered",
            metadata={"packet_id": 42},
        )

        adapter = MeshtasticAdapter(hub_plugin)
        adapter._hub = hub_plugin
        adapter._on_read_receipt("meshtastic.read_receipt_received", {
            "from_id": "!aabb1122",
            "packet_id": 42,
        })
        assert hub_plugin._store.get_status(msg_id) == "read"

    def test_inbound_read_receipt_unknown_packet_ignored(self, hub_plugin):
        adapter = MeshtasticAdapter(hub_plugin)
        adapter._hub = hub_plugin
        adapter._on_read_receipt("meshtastic.read_receipt_received", {
            "from_id": "!aabb1122",
            "packet_id": 99999,
        })

    def test_read_is_terminal(self, store):
        msg_id = store.store(
            transport="meshtastic", direction="sent", msg_type="direct",
            text="hi", status="read",
        )
        terminal = MessagingHubPlugin._TERMINAL_STATUSES
        updated = store.update_status_unless_terminal(msg_id, "timeout", terminal)
        assert updated is False
        assert store.get_status(msg_id) == "read"

    def test_outbound_receipt_sent_on_mark_read(self, hub_plugin):
        hub_plugin.config["meshtastic"] = {"enabled": False, "read_receipts": True}
        adapter = MeshtasticAdapter(hub_plugin)
        adapter._hub = hub_plugin
        hub_plugin._adapters["meshtastic"] = adapter

        hub_plugin._store.store(
            transport="meshtastic", direction="received", msg_type="direct",
            text="hi there", from_id="!aabb1122",
            metadata={"packet_id": 55},
            sub_transport="lora",
        )

        mock_gw = MagicMock()
        mock_gw.send_read_receipt.return_value = {"sent": True}
        hub_plugin.app.get_plugin.return_value = mock_gw

        hub_plugin.mark_read("!aabb1122__lora")
        mock_gw.send_read_receipt.assert_called_once_with(55, "!aabb1122")

    def test_outbound_receipt_not_sent_for_broadcast(self, hub_plugin):
        hub_plugin.config["meshtastic"] = {"enabled": False, "read_receipts": True}
        adapter = MeshtasticAdapter(hub_plugin)
        adapter._hub = hub_plugin
        hub_plugin._adapters["meshtastic"] = adapter

        mock_gw = MagicMock()
        hub_plugin.app.get_plugin.return_value = mock_gw

        hub_plugin._store.store(
            transport="meshtastic", direction="received", msg_type="broadcast",
            text="bcast", from_id="!aabb1122",
            sub_transport="lora", channel=0,
        )

        hub_plugin.mark_read("__broadcast_meshtastic_lora_ch0__")
        mock_gw.send_read_receipt.assert_not_called()

    def test_outbound_receipt_not_sent_when_disabled(self, hub_plugin):
        hub_plugin.config["meshtastic"] = {"enabled": False, "read_receipts": False}
        adapter = MeshtasticAdapter(hub_plugin)
        adapter._hub = hub_plugin
        hub_plugin._adapters["meshtastic"] = adapter

        mock_gw = MagicMock()
        hub_plugin.app.get_plugin.return_value = mock_gw

        hub_plugin._store.store(
            transport="meshtastic", direction="received", msg_type="direct",
            text="hi", from_id="!aabb1122",
            metadata={"packet_id": 77},
            sub_transport="lora",
        )

        hub_plugin.mark_read("!aabb1122__lora")
        mock_gw.send_read_receipt.assert_not_called()

    def test_outbound_receipt_rate_limited(self, hub_plugin):
        hub_plugin.config["meshtastic"] = {"enabled": False, "read_receipts": True}
        adapter = MeshtasticAdapter(hub_plugin)
        adapter._hub = hub_plugin
        adapter._read_receipt_cooldown = 30.0
        hub_plugin._adapters["meshtastic"] = adapter

        hub_plugin._store.store(
            transport="meshtastic", direction="received", msg_type="direct",
            text="hi", from_id="!aabb1122",
            metadata={"packet_id": 88},
            sub_transport="lora",
        )

        mock_gw = MagicMock()
        mock_gw.send_read_receipt.return_value = {"sent": True}
        hub_plugin.app.get_plugin.return_value = mock_gw

        hub_plugin.mark_read("!aabb1122__lora")
        assert mock_gw.send_read_receipt.call_count == 1

        # Second mark within cooldown — should be rate-limited
        hub_plugin._store.store(
            transport="meshtastic", direction="received", msg_type="direct",
            text="again", from_id="!aabb1122",
            metadata={"packet_id": 89},
            sub_transport="lora",
        )
        hub_plugin.mark_read("!aabb1122__lora")
        assert mock_gw.send_read_receipt.call_count == 1  # still 1, rate-limited
