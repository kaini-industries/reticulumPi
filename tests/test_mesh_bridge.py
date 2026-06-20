"""Tests for the MeshBridge plugin."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

from reticulumpi import events
from reticulumpi.builtin_plugins.mesh_bridge import MeshBridge
from reticulumpi.event_bus import EventBus

# Bridge subscriptions are offloaded (run in a thread pool). Give the
# executor time to dispatch the callback before asserting results.
_OFFLOAD_SETTLE = 0.15


@pytest.fixture
def bridge_app(mock_app, tmp_path):
    """mock_app with a real EventBus and state file under tmp_path."""
    mock_app.event_bus = EventBus()
    mock_app.state_dir = str(tmp_path)
    return mock_app


@pytest.fixture
def hub_mock(bridge_app):
    """Mock messaging_hub plugin registered on bridge_app."""
    hub = MagicMock()
    hub.send_message.return_value = {"sent": True, "msg_id": 42}
    bridge_app.plugins["messaging_hub"] = hub
    bridge_app.get_plugin.side_effect = lambda name: bridge_app.plugins.get(name)
    return hub


@pytest.fixture
def bridge_config(tmp_path):
    state_path = str(tmp_path / "mesh_bridge_state.json")
    return {
        "enabled": True,
        "channel_pairs": [
            {"meshtastic": 0, "meshcore": 0, "enabled": True},
        ],
        "state_path": state_path,
        # Default to 0 in tests so publishes don't hit the grace window.
        # Grace-period behavior is covered by dedicated tests.
        "startup_grace_seconds": 0,
    }


def _make(bridge_app, config):
    return MeshBridge(bridge_app, config)


def _publish_mesh(bus: EventBus, **data) -> None:
    defaults = {
        "from_id": "!abcd1234",
        "from_name": "Alice",
        "to_id": "",
        "is_broadcast": True,
        "text": "hello",
        "source": "LoRa",
        "channel": 0,
    }
    defaults.update(data)
    bus.publish(events.MESHTASTIC_MESSAGE_RECEIVED, defaults)
    time.sleep(_OFFLOAD_SETTLE)


def _publish_core(bus: EventBus, **data) -> None:
    defaults = {
        "from_key": "3c5a1f8e9012",
        "from_name": "Bob",
        "text": "hi",
        "msg_type": "broadcast",
        "channel": 0,
    }
    defaults.update(data)
    bus.publish(events.MESHCORE_MESSAGE_RECEIVED, defaults)
    time.sleep(_OFFLOAD_SETTLE)


# ── Subscription / lifecycle ────────────────────────────────────


def test_start_subscribes_to_both_events(bridge_app, hub_mock, bridge_config):
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(bridge_app.event_bus)
    _publish_core(bridge_app.event_bus)
    # Each inbound event should have triggered a hub send on the opposite
    assert hub_mock.send_message.call_count == 2


def test_stop_unsubscribes_from_event_bus(bridge_app, hub_mock, bridge_config):
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    plugin.stop()
    _publish_mesh(bridge_app.event_bus)
    assert hub_mock.send_message.call_count == 0


# ── Broadcast relay ─────────────────────────────────────────────


def test_mesh_broadcast_relays_to_core(bridge_app, hub_mock, bridge_config):
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(
        bridge_app.event_bus,
        from_name="Alice",
        text="hello world",
        channel=0,
        is_broadcast=True,
    )
    hub_mock.send_message.assert_called_once()
    args, kwargs = hub_mock.send_message.call_args
    assert args[0] == "meshcore"
    assert args[1] == "[via Mesh] Alice: hello world"
    assert args[2] == "broadcast"
    assert kwargs["channel"] == 0
    assert kwargs["msg_type"] == "broadcast"
    assert kwargs["metadata"]["bridge_origin"]["transport"] == "meshtastic"


def test_core_broadcast_relays_to_mesh(bridge_app, hub_mock, bridge_config):
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_core(
        bridge_app.event_bus,
        from_name="Bob",
        text="hey mesh",
        channel=0,
        msg_type="broadcast",
    )
    hub_mock.send_message.assert_called_once()
    args, kwargs = hub_mock.send_message.call_args
    assert args[0] == "meshtastic"
    assert args[1] == "[via Core] Bob: hey mesh"
    assert kwargs["sub_transport"] == "lora"
    assert kwargs["channel"] == 0


# ── Loop prevention ─────────────────────────────────────────────


def test_tag_prefixed_message_not_relayed(bridge_app, hub_mock, bridge_config):
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(bridge_app.event_bus, text="[via Core] Eve: echo")
    assert hub_mock.send_message.call_count == 0
    assert plugin.get_status()["stats"]["msgs_dropped_loop"] == 1


def test_dedup_blocks_immediate_repeat(bridge_app, hub_mock, bridge_config):
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(bridge_app.event_bus, text="ping")
    _publish_mesh(bridge_app.event_bus, text="ping")
    assert hub_mock.send_message.call_count == 1
    assert plugin.get_status()["stats"]["msgs_dropped_dedup"] == 1


def test_dedup_entry_expires_after_ttl(bridge_app, hub_mock, bridge_config, monkeypatch):
    import reticulumpi.builtin_plugins.mesh_bridge as mb

    fake_now = [1000.0]
    monkeypatch.setattr(mb.time, "monotonic", lambda: fake_now[0])
    plugin = _make(bridge_app, {**bridge_config, "dedup_ttl_seconds": 60})
    plugin.start()
    _publish_mesh(bridge_app.event_bus, text="ping")
    fake_now[0] += 90  # past TTL
    _publish_mesh(bridge_app.event_bus, text="ping")
    assert hub_mock.send_message.call_count == 2


def test_opposite_side_prestocked_blocks_return_trip(
    bridge_app,
    hub_mock,
    bridge_config,
):
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    # Mesh→Core relay happens
    _publish_mesh(
        bridge_app.event_bus,
        from_name="Alice",
        from_id="!abcd1234",
        text="greetings",
    )
    assert hub_mock.send_message.call_count == 1
    # Now simulate the same (sender, text) bouncing back as a Core event
    _publish_core(
        bridge_app.event_bus,
        from_name="Alice",
        from_key="!abcd1234",
        text="greetings",
    )
    # Second call would be a Core→Mesh relay — must be blocked
    assert hub_mock.send_message.call_count == 1
    assert plugin.get_status()["stats"]["msgs_dropped_dedup"] == 1


# ── Target resolution + filtering ──────────────────────────────


def test_unmapped_channel_is_dropped(bridge_app, hub_mock, bridge_config):
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(bridge_app.event_bus, channel=5)
    assert hub_mock.send_message.call_count == 0
    assert plugin.get_status()["stats"]["msgs_dropped_no_pair"] == 1


def test_allow_regex_filter(bridge_app, hub_mock, bridge_config):
    bridge_config["channel_pairs"][0]["allow_regex"] = r"^weather"
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(bridge_app.event_bus, text="hello")
    _publish_mesh(bridge_app.event_bus, text="weather SF")
    assert hub_mock.send_message.call_count == 1
    assert plugin.get_status()["stats"]["msgs_dropped_filter"] == 1


def test_deny_regex_filter(bridge_app, hub_mock, bridge_config):
    bridge_config["channel_pairs"][0]["deny_regex"] = r"badword"
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(bridge_app.event_bus, text="says badword here")
    assert hub_mock.send_message.call_count == 0
    assert plugin.get_status()["stats"]["msgs_dropped_filter"] == 1


def test_direction_mesh_to_core_only(bridge_app, hub_mock, bridge_config):
    bridge_config["channel_pairs"][0]["direction"] = "mesh_to_core"
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_core(bridge_app.event_bus)
    assert hub_mock.send_message.call_count == 0
    _publish_mesh(bridge_app.event_bus)
    assert hub_mock.send_message.call_count == 1


# ── MTU ─────────────────────────────────────────────────────────


def test_mtu_truncation_preserves_prefix(bridge_app, hub_mock, bridge_config):
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    body = "x" * 500
    _publish_mesh(bridge_app.event_bus, text=body, from_name="Alice")
    args, _ = hub_mock.send_message.call_args
    sent = args[1]
    # MeshCore default MTU is 240; sent must fit
    assert len(sent.encode("utf-8")) <= 240
    assert sent.startswith("[via Mesh] Alice: ")
    assert sent.endswith(" ...")


# ── DM bridging ─────────────────────────────────────────────────


def test_dm_bridging_off_by_default(bridge_app, hub_mock, bridge_config):
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(
        bridge_app.event_bus,
        is_broadcast=False,
        to_id="!deadbeef",
        channel=None,
    )
    assert hub_mock.send_message.call_count == 0
    assert plugin.get_status()["stats"]["msgs_dropped_no_pair"] == 1


def test_dm_bridging_with_mapping(bridge_app, hub_mock, bridge_config):
    bridge_config["bridge_dms"] = True
    bridge_config["dm_pairs"] = [
        {"meshtastic": "!abcd1234", "meshcore": "3c5a1f8e9012"},
    ]
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(
        bridge_app.event_bus,
        from_id="!ffffffff",
        from_name="Alice",
        is_broadcast=False,
        to_id="!abcd1234",
        channel=None,
        text="hi bob",
    )
    hub_mock.send_message.assert_called_once()
    args, kwargs = hub_mock.send_message.call_args
    assert args[0] == "meshcore"
    assert args[2] == "3c5a1f8e9012"
    assert kwargs["msg_type"] == "direct"


def test_unmapped_dm_dropped(bridge_app, hub_mock, bridge_config):
    bridge_config["bridge_dms"] = True
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(
        bridge_app.event_bus,
        is_broadcast=False,
        to_id="!deadbeef",
        channel=None,
    )
    assert hub_mock.send_message.call_count == 0
    assert plugin.get_status()["stats"]["msgs_dropped_no_pair"] == 1


def test_core_dm_always_drops_due_to_missing_to_key(
    bridge_app,
    hub_mock,
    bridge_config,
):
    """MeshCore gateway events don't include a to_key field, so even with
    bridge_dms=true and a dm_pair, core-origin DMs cannot be routed to a
    Meshtastic recipient.  This test documents that asymmetry."""
    bridge_config["bridge_dms"] = True
    bridge_config["dm_pairs"] = [
        {"meshtastic": "!abcd1234", "meshcore": "3c5a1f8e9012"},
    ]
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_core(
        bridge_app.event_bus,
        from_key="3c5a1f8e9012",
        from_name="Bob",
        msg_type="direct",
        text="private",
    )
    assert hub_mock.send_message.call_count == 0
    assert plugin.get_status()["stats"]["msgs_dropped_no_pair"] == 1


# ── Fallback + error handling ──────────────────────────────────


def test_hub_unavailable_falls_back_to_direct_gateway(
    bridge_app,
    bridge_config,
):
    gw = MagicMock()
    gw.send_message.return_value = {"sent": True}
    bridge_app.plugins = {"meshcore_gateway": gw}
    bridge_app.get_plugin.side_effect = lambda n: bridge_app.plugins.get(n)
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(bridge_app.event_bus, text="hello")
    gw.send_message.assert_called_once()


def test_gateway_not_connected_drops_gracefully(bridge_app, hub_mock, bridge_config):
    hub_mock.send_message.return_value = {"sent": False, "reason": "not_connected"}
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(bridge_app.event_bus, text="hello")
    # No crash; counter incremented
    assert plugin.get_status()["stats"]["msgs_dropped_send_failed"] == 1


def test_rate_limited_handled(bridge_app, hub_mock, bridge_config):
    hub_mock.send_message.return_value = {"sent": False, "reason": "rate_limited"}
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(bridge_app.event_bus, text="hello")
    assert plugin.get_status()["stats"]["msgs_dropped_send_failed"] == 1


# ── Config validation ──────────────────────────────────────────


def test_invalid_config_raises_on_init(bridge_app, tmp_path):
    state_path = str(tmp_path / "state.json")
    with pytest.raises(ValueError, match="channel_pair.meshtastic"):
        MeshBridge(
            bridge_app,
            {
                "channel_pairs": [{"meshtastic": 99, "meshcore": 0}],
                "state_path": state_path,
            },
        )
    with pytest.raises(ValueError, match="loop_detect_regex"):
        MeshBridge(
            bridge_app,
            {
                "loop_detect_regex": "[",
                "state_path": state_path,
            },
        )
    with pytest.raises(ValueError, match="dm_pair.meshtastic"):
        MeshBridge(
            bridge_app,
            {
                "dm_pairs": [{"meshtastic": "notavalidid", "meshcore": "3c5a1f8e9012"}],
                "state_path": state_path,
            },
        )


# ── Pause / resume / persistence ───────────────────────────────


def test_paused_bridge_drops_everything(bridge_app, hub_mock, bridge_config):
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    plugin.set_running(False)
    _publish_mesh(bridge_app.event_bus)
    _publish_core(bridge_app.event_bus)
    assert hub_mock.send_message.call_count == 0
    assert plugin.get_status()["stats"]["msgs_dropped_paused"] == 2


def test_set_running_persists_to_state_file(bridge_app, hub_mock, bridge_config):
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    plugin.set_running(False, reason="manual")
    # Read state file directly
    with open(bridge_config["state_path"]) as f:
        saved = json.load(f)
    assert saved["running"] is False
    assert saved["auto_paused_reason"] == "manual"

    # Re-instantiate — should load paused state
    plugin2 = _make(bridge_app, bridge_config)
    plugin2.start()
    assert plugin2.get_status()["running"] is False


def test_circuit_breaker_trips_on_rate_spike(bridge_app, hub_mock, bridge_config):
    bridge_config["auto_pause_threshold"] = 3
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    for i in range(5):
        _publish_mesh(bridge_app.event_bus, text=f"msg {i}")
    status = plugin.get_status()
    assert status["running"] is False
    assert status["auto_paused_reason"] == "rate_limit"
    # After pause, further messages drop
    prior = hub_mock.send_message.call_count
    _publish_mesh(bridge_app.event_bus, text="after pause")
    assert hub_mock.send_message.call_count == prior


def test_manual_resume_clears_auto_paused_reason(
    bridge_app,
    hub_mock,
    bridge_config,
):
    bridge_config["auto_pause_threshold"] = 2
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    for i in range(4):
        _publish_mesh(bridge_app.event_bus, text=f"msg {i}")
    assert plugin.get_status()["auto_paused_reason"] == "rate_limit"
    plugin.set_running(True)
    status = plugin.get_status()
    assert status["running"] is True
    assert status["auto_paused_reason"] is None
    # Next relay goes through (new text to avoid dedup)
    _publish_mesh(bridge_app.event_bus, text="resumed!")
    assert any("resumed!" in str(call.args[1]) for call in hub_mock.send_message.call_args_list)


def test_manual_resume_clears_rate_window(bridge_app, hub_mock, bridge_config):
    """Regression: after the circuit breaker trips, a manual resume must
    reset _recent_relays so the breaker doesn't immediately re-trip on
    the very next message."""
    bridge_config["auto_pause_threshold"] = 2
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    for i in range(4):
        _publish_mesh(bridge_app.event_bus, text=f"msg {i}")
    assert plugin.get_status()["running"] is False
    plugin.set_running(True)
    # Send threshold messages — should NOT re-trip because the rate window
    # was cleared on resume.
    for i in range(2):
        _publish_mesh(bridge_app.event_bus, text=f"after-resume {i}")
    assert plugin.get_status()["running"] is True


def test_empty_text_increments_dropped_empty_stat(
    bridge_app,
    hub_mock,
    bridge_config,
):
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(bridge_app.event_bus, text="")
    assert hub_mock.send_message.call_count == 0
    assert plugin.get_status()["stats"]["msgs_dropped_empty"] == 1


# ── Startup grace period ───────────────────────────────────────


def test_startup_grace_drops_messages_during_window(
    bridge_app,
    hub_mock,
    bridge_config,
    monkeypatch,
):
    """Messages arriving in the first N seconds after start are dropped —
    prevents re-broadcasting queue-drained messages from MeshCore/MQTT
    after a service restart."""
    import reticulumpi.builtin_plugins.mesh_bridge as mb

    fake_now = [1000.0]
    monkeypatch.setattr(mb.time, "monotonic", lambda: fake_now[0])
    bridge_config["startup_grace_seconds"] = 30
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(bridge_app.event_bus, text="stale")
    assert hub_mock.send_message.call_count == 0
    assert plugin.get_status()["stats"]["msgs_dropped_startup_grace"] == 1
    # Advance past grace window
    fake_now[0] += 31
    _publish_mesh(bridge_app.event_bus, text="live")
    assert hub_mock.send_message.call_count == 1


def test_startup_grace_zero_disables_feature(
    bridge_app,
    hub_mock,
    bridge_config,
):
    bridge_config["startup_grace_seconds"] = 0
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(bridge_app.event_bus, text="hello")
    assert hub_mock.send_message.call_count == 1
    assert plugin.get_status()["stats"]["msgs_dropped_startup_grace"] == 0


# ── Content filters (position shares + tapbacks) ──────────────


def test_position_share_blocked_by_default(bridge_app, hub_mock, bridge_config):
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(
        bridge_app.event_bus,
        text="📍 Alice has shared their position with you.",
    )
    assert hub_mock.send_message.call_count == 0
    assert plugin.get_status()["stats"]["msgs_dropped_position_share"] == 1


def test_position_share_location_variant_blocked(
    bridge_app,
    hub_mock,
    bridge_config,
):
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(bridge_app.event_bus, text="Bob has shared their location")
    assert hub_mock.send_message.call_count == 0
    assert plugin.get_status()["stats"]["msgs_dropped_position_share"] == 1


def test_position_filter_can_be_disabled(bridge_app, hub_mock, bridge_config):
    bridge_config["filter_position_shares"] = False
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(
        bridge_app.event_bus,
        text="📍 Alice has shared their position with you.",
    )
    assert hub_mock.send_message.call_count == 1


@pytest.mark.parametrize(
    "tapback_text",
    [
        "👍",
        "❤️",
        "😂",
        "👎",
        "🔥",
        "!!",
        "  👍  ",  # whitespace-wrapped emoji
    ],
)
def test_tapback_blocked_by_default(
    bridge_app,
    hub_mock,
    bridge_config,
    tapback_text,
):
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(bridge_app.event_bus, text=tapback_text)
    assert hub_mock.send_message.call_count == 0
    assert plugin.get_status()["stats"]["msgs_dropped_tapback"] == 1


@pytest.mark.parametrize(
    "short_text",
    [
        "ok",
        "yes",
        "hi",
        "LOL",
        "42",
        "OMG",
        "LOL!!",
    ],
)
def test_short_alphanumeric_text_is_not_tapback(
    bridge_app,
    hub_mock,
    bridge_config,
    short_text,
):
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(bridge_app.event_bus, text=short_text)
    assert hub_mock.send_message.call_count == 1


def test_tapback_filter_can_be_disabled(bridge_app, hub_mock, bridge_config):
    bridge_config["filter_tapbacks"] = False
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(bridge_app.event_bus, text="👍")
    assert hub_mock.send_message.call_count == 1


def test_state_file_roundtrip_with_auto_pause(bridge_app, hub_mock, bridge_config):
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    plugin.set_running(False, reason="rate_limit")
    plugin2 = _make(bridge_app, bridge_config)
    plugin2.start()
    status = plugin2.get_status()
    assert status["running"] is False
    assert status["auto_paused_reason"] == "rate_limit"
    assert status["auto_paused_at"] is not None


# ── Dedup eviction ────────────────────────────────────────────


def test_dedup_eviction_at_max(bridge_app, hub_mock, bridge_config, monkeypatch):
    import reticulumpi.builtin_plugins.mesh_bridge as mb

    fake_now = [1000.0]
    monkeypatch.setattr(mb.time, "monotonic", lambda: fake_now[0])
    bridge_config["dedup_max_entries"] = 4
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    for i in range(6):
        fake_now[0] += 1.0
        _publish_mesh(bridge_app.event_bus, text=f"msg-{i}", from_id=f"!aaa{i:05d}")
    assert hub_mock.send_message.call_count == 6
    assert len(plugin._dedup_cache) <= 4


def test_dedup_eviction_same_timestamp(bridge_app, hub_mock, bridge_config, monkeypatch):
    """Even when all entries share the same timestamp, eviction reduces size."""
    import reticulumpi.builtin_plugins.mesh_bridge as mb

    fake_now = [1000.0]
    monkeypatch.setattr(mb.time, "monotonic", lambda: fake_now[0])
    bridge_config["dedup_max_entries"] = 4
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    for i in range(8):
        _publish_mesh(bridge_app.event_bus, text=f"burst-{i}", from_id=f"!bbb{i:05d}")
    assert len(plugin._dedup_cache) <= 4


def test_dedup_eviction_preserves_valid_ttl_entries(
    bridge_app,
    hub_mock,
    bridge_config,
    monkeypatch,
):
    """Expired entries are pruned first; TTL-valid entries survive eviction."""
    import reticulumpi.builtin_plugins.mesh_bridge as mb

    fake_now = [1000.0]
    monkeypatch.setattr(mb.time, "monotonic", lambda: fake_now[0])
    # Each relay records 2 dedup entries (inbound + outbound side), so
    # set max high enough that only expired entries trigger eviction.
    bridge_config["dedup_max_entries"] = 8
    bridge_config["dedup_ttl_seconds"] = 30
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    # Insert 3 messages at t=1000 (will expire by t=1035)
    for i in range(3):
        fake_now[0] += 1.0
        _publish_mesh(bridge_app.event_bus, text=f"old-{i}", from_id=f"!old{i:05d}")
    old_count = len(plugin._dedup_cache)
    assert old_count == 6  # 3 messages × 2 sides
    # Jump forward past TTL
    fake_now[0] = 1040.0
    # Insert enough new messages to trigger eviction
    for i in range(5):
        fake_now[0] += 1.0
        _publish_mesh(bridge_app.event_bus, text=f"new-{i}", from_id=f"!new{i:05d}")
    # Expired entries should have been pruned; cache bounded
    assert len(plugin._dedup_cache) <= 10
    # The newest entry should still be a dedup-hit
    newest_key = ("mesh", "!new00004", hash("new-4"))
    assert plugin._dedup_check_and_record(newest_key, fake_now[0])


def test_dedup_eviction_fifo_when_all_valid(
    bridge_app,
    hub_mock,
    bridge_config,
    monkeypatch,
):
    """When all entries are within TTL, oldest-by-insertion are evicted first."""
    import reticulumpi.builtin_plugins.mesh_bridge as mb

    fake_now = [1000.0]
    monkeypatch.setattr(mb.time, "monotonic", lambda: fake_now[0])
    # Each relay records 2 entries; set max=6 so 4 relays (8 entries) triggers FIFO
    bridge_config["dedup_max_entries"] = 6
    bridge_config["dedup_ttl_seconds"] = 600
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    for i in range(4):
        fake_now[0] += 1.0
        _publish_mesh(bridge_app.event_bus, text=f"msg-{i}", from_id=f"!fifo{i:05d}")
    assert len(plugin._dedup_cache) <= 6
    # Newest entry should survive FIFO eviction
    newest_key = ("mesh", "!fifo00003", hash("msg-3"))
    assert plugin._dedup_check_and_record(newest_key, fake_now[0])


# ── State file edge cases ─────────────────────────────────────


def test_corrupt_state_file_falls_back_to_default(bridge_app, hub_mock, bridge_config):
    with open(bridge_config["state_path"], "w") as f:
        f.write("{{{invalid json")
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    assert plugin.get_status()["running"] is True


# ── Sender label edge cases ───────────────────────────────────


def test_select_sender_label_none_inputs():
    from reticulumpi.builtin_plugins.mesh_bridge import _select_sender_label

    assert _select_sender_label(None, None) == "?"
    assert _select_sender_label("", None) == "?"
    assert _select_sender_label(None, "") == "?"
    assert _select_sender_label("", "") == "?"
    assert _select_sender_label("Alice", None) == "Alice"
    assert _select_sender_label(None, "!abcdef123456789") == "!abcdef12345"
    assert _select_sender_label(None, "!abcd1234") == "!abcd1234"


# ── Multi-pair direction routing ──────────────────────────────


def test_multiple_channel_pairs_directions(bridge_app, hub_mock, bridge_config):
    bridge_config["channel_pairs"] = [
        {"meshtastic": 0, "meshcore": 0, "enabled": True, "direction": "mesh_to_core"},
        {"meshtastic": 1, "meshcore": 1, "enabled": True, "direction": "core_to_mesh"},
    ]
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(bridge_app.event_bus, text="m2c", channel=0)
    assert hub_mock.send_message.call_count == 1
    _publish_core(bridge_app.event_bus, text="c2m-blocked", channel=0)
    assert hub_mock.send_message.call_count == 1
    _publish_core(bridge_app.event_bus, text="c2m", channel=1)
    assert hub_mock.send_message.call_count == 2
    _publish_mesh(bridge_app.event_bus, text="m2c-blocked", channel=1)
    assert hub_mock.send_message.call_count == 2


# ── UTF-8 MTU truncation ─────────────────────────────────────


def test_mtu_truncation_utf8_multibyte(bridge_app, hub_mock, bridge_config):
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    body = "\U0001f600" * 200
    _publish_mesh(bridge_app.event_bus, text=body, from_name="A")
    args, _ = hub_mock.send_message.call_args
    sent = args[1]
    sent_bytes = sent.encode("utf-8")
    assert len(sent_bytes) <= 240
    sent_bytes.decode("utf-8")


# ── Empty channel_pairs ──────────────────────────────────────


def test_empty_channel_pairs_disables_relaying(bridge_app, hub_mock, bridge_config):
    bridge_config["channel_pairs"] = []
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(bridge_app.event_bus, text="hello")
    assert hub_mock.send_message.call_count == 0
    assert plugin.get_status()["stats"]["msgs_dropped_no_pair"] == 1


# ── Dispatch exception handling ───────────────────────────────


def test_dispatch_exception_returns_failure(bridge_app, hub_mock, bridge_config):
    hub_mock.send_message.side_effect = RuntimeError("connection reset")
    plugin = _make(bridge_app, bridge_config)
    plugin.start()
    _publish_mesh(bridge_app.event_bus, text="hello")
    assert plugin.get_status()["stats"]["msgs_dropped_send_failed"] == 1
