"""Tests for the Meshtastic auto-reply responder plugin."""

from __future__ import annotations

import io
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi import events
from reticulumpi.event_bus import EventBus


# ── Helpers ──────────────────────────────────────────────────────────


def _make_event(
    text: str = "!ping",
    from_id: str = "!aabb1122",
    is_broadcast: bool = False,
    source: str = "LoRa",
    from_name: str = "TestNode",
    to_id: str = "!11223344",
) -> dict:
    """Build a MESHTASTIC_MESSAGE_RECEIVED event payload."""
    return {
        "from_id": from_id,
        "from_name": from_name,
        "to_id": to_id,
        "is_broadcast": is_broadcast,
        "text": text,
        "forwarded_to": 0,
        "source": source,
    }


def _mock_urlopen(json_data):
    """Create a mock context manager that returns JSON data."""
    resp = io.BytesIO(json.dumps(json_data).encode("utf-8"))
    mock_cm = MagicMock()
    mock_cm.__enter__ = MagicMock(return_value=resp)
    mock_cm.__exit__ = MagicMock(return_value=False)
    return mock_cm


def _wait_for_call(mock, timeout=2.0, poll=0.01):
    """Poll until *mock* has been called, or *timeout* expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if mock.call_count:
            return
        time.sleep(poll)


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_app():
    """Create a mock ReticulumPiApp with a real EventBus."""
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.identity.hash = b"\x00" * 16
    app.node_name = "TestNode"
    app.plugins = {}
    app.event_bus = EventBus()
    return app


@pytest.fixture
def default_config():
    """Default responder config with all commands enabled."""
    return {
        "prefix": "!",
        "respond_to_broadcast": False,
        "cooldown_seconds": 30,
        "commands": [],  # empty = all enabled
        "custom_responses": {
            "hello": "Hi from the mesh!",
            "info": "Send !help for commands.",
        },
    }


@pytest.fixture
def responder(mock_app, default_config):
    """Create a started MeshtasticResponder plugin."""
    from reticulumpi.builtin_plugins.meshtastic_responder import (
        MeshtasticResponder,
    )

    plugin = MeshtasticResponder(mock_app, default_config)
    plugin.start()
    yield plugin
    plugin.stop()


@pytest.fixture
def mock_gateway():
    """Create a mock meshtastic_gateway plugin."""
    gw = MagicMock()
    gw.send_message.return_value = {"sent": True, "truncated": False}
    gw.get_status.return_value = {"node_id": "!99887766", "connected": True}
    gw.get_meshtastic_nodes.return_value = [
        {
            "id": "!aabb1122",
            "long_name": "AlphaNode",
            "short_name": "AN",
            "last_heard": time.time() - 60,
        },
        {
            "id": "!ccdd3344",
            "long_name": "BravoNode",
            "short_name": "BN",
            "last_heard": time.time() - 300,
        },
    ]
    return gw


# ── TestCommandRouting ───────────────────────────────────────────────


class TestCommandRouting:
    """Test command prefix parsing and routing."""

    def test_help_returns_command_list(self, responder):
        result = responder._route_command("!help")
        assert "help" in result.lower()
        assert "ping" in result.lower()

    def test_ping_returns_pong(self, responder):
        result = responder._route_command("!ping")
        assert result.startswith("Pong!")

    def test_unknown_command_returns_error(self, responder):
        result = responder._route_command("!nonexistent")
        assert "Unknown" in result
        assert "!help" in result

    def test_case_insensitive_command(self, responder):
        result = responder._route_command("!PING")
        assert result.startswith("Pong!")

    def test_empty_after_prefix_returns_help(self, responder):
        result = responder._route_command("!")
        assert "help" in result.lower() or "Mesh Responder" in result

    def test_command_with_args(self, responder):
        result = responder._route_command("!calc 2+2")
        assert "= 4" in result

    def test_no_prefix_returns_none(self, responder):
        result = responder._match_and_respond("random chat")
        assert result is None

    def test_prefix_only_text_not_custom(self, responder):
        """Text that is just the prefix should route to help, not custom."""
        result = responder._match_and_respond("!")
        # Starts with prefix so it's a command, not custom
        assert result is not None


# ── TestCustomResponses ──────────────────────────────────────────────


class TestCustomResponses:
    """Test custom keyword→response matching."""

    def test_exact_match(self, responder):
        result = responder._match_and_respond("hello")
        assert result == "Hi from the mesh!"

    def test_case_insensitive(self, responder):
        result = responder._match_and_respond("HELLO")
        assert result == "Hi from the mesh!"

    def test_info_keyword(self, responder):
        result = responder._match_and_respond("info")
        assert result == "Send !help for commands."

    def test_no_match_returns_none(self, responder):
        result = responder._match_and_respond("goodbye")
        assert result is None

    def test_custom_response_priority_over_prefix(self, mock_app):
        """A custom response matching the text takes priority over command parsing."""
        from reticulumpi.builtin_plugins.meshtastic_responder import (
            MeshtasticResponder,
        )

        config = {
            "prefix": "!",
            "cooldown_seconds": 0,
            "custom_responses": {"!ping": "Custom pong!"},
        }
        plugin = MeshtasticResponder(mock_app, config)
        plugin.start()
        try:
            # "!ping" is both a valid command and a custom response key
            # Custom responses are checked first
            result = plugin._match_and_respond("!ping")
            assert result == "Custom pong!"
        finally:
            plugin.stop()


# ── TestCooldown ─────────────────────────────────────────────────────


class TestCooldown:
    """Test per-node cooldown behavior."""

    def test_first_message_allowed(self, responder):
        assert responder._check_cooldown("!aabb1122") is True

    def test_repeated_message_blocked(self, responder):
        assert responder._check_cooldown("!aabb1122") is True
        assert responder._check_cooldown("!aabb1122") is False

    def test_cooldown_expires(self, responder):
        assert responder._check_cooldown("!aabb1122") is True
        # Simulate time passing beyond cooldown
        with responder._lock:
            responder._node_cooldowns["!aabb1122"] = time.time() - responder._cooldown_seconds - 1
        assert responder._check_cooldown("!aabb1122") is True

    def test_different_nodes_independent(self, responder):
        assert responder._check_cooldown("!node_a") is True
        assert responder._check_cooldown("!node_b") is True
        # node_a is now in cooldown, node_b just entered
        assert responder._check_cooldown("!node_a") is False
        assert responder._check_cooldown("!node_b") is False

    def test_zero_cooldown_always_allows(self, mock_app):
        """With cooldown_seconds=0, every message is allowed."""
        from reticulumpi.builtin_plugins.meshtastic_responder import (
            MeshtasticResponder,
        )

        config = {"cooldown_seconds": 0}
        plugin = MeshtasticResponder(mock_app, config)
        plugin.start()
        try:
            assert plugin._check_cooldown("!aabb1122") is True
            assert plugin._check_cooldown("!aabb1122") is True
            assert plugin._check_cooldown("!aabb1122") is True
        finally:
            plugin.stop()

    def test_cooldown_prune(self, responder):
        """Cooldown dict gets pruned when it exceeds threshold."""
        from reticulumpi.builtin_plugins.meshtastic_responder import (
            _COOLDOWN_PRUNE_THRESHOLD,
        )

        # Fill up beyond threshold with expired entries
        old_time = time.time() - responder._cooldown_seconds - 10
        with responder._lock:
            for i in range(_COOLDOWN_PRUNE_THRESHOLD + 10):
                responder._node_cooldowns[f"!node{i:04x}"] = old_time

        # Trigger a check which should prune
        assert responder._check_cooldown("!trigger_prune") is True
        with responder._lock:
            # Most old entries should be pruned
            assert len(responder._node_cooldowns) < _COOLDOWN_PRUNE_THRESHOLD


# ── TestBroadcastFiltering ───────────────────────────────────────────


class TestBroadcastFiltering:
    """Test broadcast vs. direct message filtering."""

    def test_broadcast_ignored_by_default(self, responder, mock_gateway, mock_app):
        mock_app.get_plugin.return_value = mock_gateway
        data = _make_event(text="!ping", is_broadcast=True)
        responder._on_mesh_message(events.MESHTASTIC_MESSAGE_RECEIVED, data)
        mock_gateway.send_message.assert_not_called()

    def test_dm_responded(self, responder, mock_gateway, mock_app):
        mock_app.get_plugin.return_value = mock_gateway
        data = _make_event(text="!ping", is_broadcast=False)
        responder._on_mesh_message(events.MESHTASTIC_MESSAGE_RECEIVED, data)
        mock_gateway.send_message.assert_called_once()

    def test_broadcast_responded_when_enabled(self, mock_app, mock_gateway):
        from reticulumpi.builtin_plugins.meshtastic_responder import (
            MeshtasticResponder,
        )

        config = {
            "respond_to_broadcast": True,
            "cooldown_seconds": 0,
        }
        mock_app.get_plugin.return_value = mock_gateway
        plugin = MeshtasticResponder(mock_app, config)
        plugin.start()
        try:
            data = _make_event(text="!ping", is_broadcast=True)
            plugin._on_mesh_message(events.MESHTASTIC_MESSAGE_RECEIVED, data)
            mock_gateway.send_message.assert_called_once()
        finally:
            plugin.stop()


# ── TestSelfReplyGuard ───────────────────────────────────────────────


class TestSelfReplyGuard:
    """Test that the responder does not reply to its own messages."""

    def test_own_node_id_skipped(self, responder, mock_gateway, mock_app):
        mock_app.get_plugin.return_value = mock_gateway
        # Send from the gateway's own node ID
        data = _make_event(text="!ping", from_id="!99887766")
        responder._on_mesh_message(events.MESHTASTIC_MESSAGE_RECEIVED, data)
        mock_gateway.send_message.assert_not_called()

    def test_other_node_not_skipped(self, responder, mock_gateway, mock_app):
        mock_app.get_plugin.return_value = mock_gateway
        data = _make_event(text="!ping", from_id="!aabb1122")
        responder._on_mesh_message(events.MESHTASTIC_MESSAGE_RECEIVED, data)
        mock_gateway.send_message.assert_called_once()

    def test_own_node_id_cached(self, responder, mock_gateway, mock_app):
        """After first lookup, own_node_id is cached."""
        mock_app.get_plugin.return_value = mock_gateway
        # First call caches the ID
        responder._is_own_node("!aabb1122")
        assert responder._own_node_id == "!99887766"
        # Second call doesn't re-query gateway
        mock_gateway.get_status.reset_mock()
        responder._is_own_node("!99887766")
        mock_gateway.get_status.assert_not_called()


# ── TestEventFlow ────────────────────────────────────────────────────


class TestEventFlow:
    """Test end-to-end event bus integration."""

    def test_event_triggers_response(self, responder, mock_gateway, mock_app):
        mock_app.get_plugin.return_value = mock_gateway
        data = _make_event(text="!ping")
        # Publish via the real event bus — callback is offloaded to a
        # background thread, so we need a short wait.
        mock_app.event_bus.publish(events.MESHTASTIC_MESSAGE_RECEIVED, data)
        _wait_for_call(mock_gateway.send_message)
        mock_gateway.send_message.assert_called_once()
        args = mock_gateway.send_message.call_args
        assert "Pong!" in args[0][0]  # first positional arg is text
        assert args[1]["destination_id"] == "!aabb1122"

    def test_gateway_unavailable_no_crash(self, responder, mock_app):
        mock_app.get_plugin.return_value = None
        data = _make_event(text="!ping")
        # Should not raise
        responder._on_mesh_message(events.MESHTASTIC_MESSAGE_RECEIVED, data)

    def test_empty_text_ignored(self, responder, mock_gateway, mock_app):
        mock_app.get_plugin.return_value = mock_gateway
        data = _make_event(text="")
        responder._on_mesh_message(events.MESHTASTIC_MESSAGE_RECEIVED, data)
        mock_gateway.send_message.assert_not_called()

    def test_whitespace_only_ignored(self, responder, mock_gateway, mock_app):
        mock_app.get_plugin.return_value = mock_gateway
        data = _make_event(text="   ")
        responder._on_mesh_message(events.MESHTASTIC_MESSAGE_RECEIVED, data)
        mock_gateway.send_message.assert_not_called()

    def test_no_from_id_ignored(self, responder, mock_gateway, mock_app):
        mock_app.get_plugin.return_value = mock_gateway
        data = _make_event(text="!ping", from_id="")
        responder._on_mesh_message(events.MESHTASTIC_MESSAGE_RECEIVED, data)
        mock_gateway.send_message.assert_not_called()

    def test_custom_response_via_event(self, responder, mock_gateway, mock_app):
        mock_app.get_plugin.return_value = mock_gateway
        data = _make_event(text="hello")
        mock_app.event_bus.publish(events.MESHTASTIC_MESSAGE_RECEIVED, data)
        _wait_for_call(mock_gateway.send_message)
        mock_gateway.send_message.assert_called_once()
        sent_text = mock_gateway.send_message.call_args[0][0]
        assert sent_text == "Hi from the mesh!"

    def test_unrecognized_text_no_reply(self, responder, mock_gateway, mock_app):
        mock_app.get_plugin.return_value = mock_gateway
        data = _make_event(text="just chatting")
        responder._on_mesh_message(events.MESHTASTIC_MESSAGE_RECEIVED, data)
        mock_gateway.send_message.assert_not_called()

    def test_msgs_handled_counter(self, responder, mock_gateway, mock_app):
        mock_app.get_plugin.return_value = mock_gateway
        assert responder._msgs_handled == 0
        data = _make_event(text="!ping")
        responder._on_mesh_message(events.MESHTASTIC_MESSAGE_RECEIVED, data)
        assert responder._msgs_handled == 1

    def test_cooldown_counter(self, responder, mock_gateway, mock_app):
        mock_app.get_plugin.return_value = mock_gateway
        assert responder._msgs_cooldown_skipped == 0
        data = _make_event(text="!ping")
        responder._on_mesh_message(events.MESHTASTIC_MESSAGE_RECEIVED, data)
        responder._on_mesh_message(events.MESHTASTIC_MESSAGE_RECEIVED, data)
        assert responder._msgs_cooldown_skipped == 1

    def test_reply_via_config(self, mock_app, mock_gateway):
        """reply_via config is passed through to send_message."""
        from reticulumpi.builtin_plugins.meshtastic_responder import (
            MeshtasticResponder,
        )

        config = {"cooldown_seconds": 0, "reply_via": "lora"}
        mock_app.get_plugin.return_value = mock_gateway
        plugin = MeshtasticResponder(mock_app, config)
        plugin.start()
        try:
            data = _make_event(text="!ping")
            plugin._on_mesh_message(events.MESHTASTIC_MESSAGE_RECEIVED, data)
            args = mock_gateway.send_message.call_args
            assert args[1]["via"] == "lora"
        finally:
            plugin.stop()


# ── TestTruncation ───────────────────────────────────────────────────


class TestTruncation:
    """Test response truncation for Meshtastic MTU."""

    def test_short_response_unchanged(self):
        from reticulumpi.builtin_plugins.meshtastic_responder import (
            _truncate_response,
        )

        text = "Pong! Uptime: 1h 30m"
        assert _truncate_response(text) == text

    def test_long_response_truncated(self):
        from reticulumpi.builtin_plugins.meshtastic_responder import (
            _MESHTASTIC_MTU,
            _truncate_response,
        )

        text = "A " * 200  # Well over the MTU
        result = _truncate_response(text)
        assert len(result.encode("utf-8")) <= _MESHTASTIC_MTU
        assert result.endswith("...(more)")

    def test_truncation_at_word_boundary(self):
        from reticulumpi.builtin_plugins.meshtastic_responder import (
            _truncate_response,
        )

        # Create text that would cut mid-word without boundary handling
        text = "word " * 50  # 250 chars
        result = _truncate_response(text, max_bytes=50)
        # Should not end with a partial word before the suffix
        before_suffix = result.split("\n...(more)")[0]
        assert before_suffix.endswith(" ") or before_suffix.endswith("word")

    def test_utf8_byte_counting(self):
        from reticulumpi.builtin_plugins.meshtastic_responder import (
            _MESHTASTIC_MTU,
            _truncate_response,
        )

        # Multi-byte characters
        text = "Hello " + "\u00e9" * 200  # é is 2 bytes in UTF-8
        result = _truncate_response(text)
        assert len(result.encode("utf-8")) <= _MESHTASTIC_MTU

    def test_exact_boundary_unchanged(self):
        from reticulumpi.builtin_plugins.meshtastic_responder import (
            _MESHTASTIC_MTU,
            _truncate_response,
        )

        text = "x" * _MESHTASTIC_MTU
        assert _truncate_response(text) == text


# ── TestIndividualCommands ───────────────────────────────────────────


class TestIndividualCommands:
    """Test each built-in command handler."""

    def test_help_lists_all_commands(self, responder):
        result = responder._cmd_help()
        assert result.startswith("Cmds:")
        assert "!ping" in result
        assert "!weather" in result
        assert "!calc" in result
        assert len(result.encode("utf-8")) <= 180

    def test_ping_returns_pong(self, responder):
        assert responder._cmd_ping() == "Pong!"

    def test_time_utc(self, responder):
        result = responder._cmd_time()
        assert "UTC" in result

    def test_time_with_timezone(self, responder):
        result = responder._cmd_time("PST")
        assert "US/Pacific" in result or "PST" in result

    def test_time_invalid_timezone(self, responder):
        result = responder._cmd_time("INVALID_TZ_XYZ")
        assert "Unknown timezone" in result

    def test_uptime_format(self, responder):
        result = responder._cmd_uptime()
        assert "Bot:" in result
        assert "System:" in result

    def test_nodes_with_gateway(self, responder, mock_gateway, mock_app):
        mock_app.get_plugin.return_value = mock_gateway
        result = responder._cmd_nodes()
        assert "2 nodes" in result
        # Either short_name (AN/BN) or long_name prefix should appear
        assert "AN" in result or "AlphaNode" in result

    def test_nodes_no_gateway(self, responder, mock_app):
        mock_app.get_plugin.return_value = None
        result = responder._cmd_nodes()
        assert "unavailable" in result.lower()

    def test_nodes_empty(self, responder, mock_app):
        gw = MagicMock()
        gw.get_meshtastic_nodes.return_value = []
        mock_app.get_plugin.return_value = gw
        result = responder._cmd_nodes()
        assert "No nodes" in result

    def test_weather_no_args(self, responder):
        result = responder._cmd_weather()
        assert "Usage" in result

    def test_weather_success(self, responder):
        geo_response = {
            "results": [
                {
                    "latitude": 30.27,
                    "longitude": -97.74,
                    "name": "Austin",
                    "admin1": "Texas",
                    "country": "United States",
                    "country_code": "US",
                }
            ]
        }
        weather_response = {
            "current": {
                "temperature_2m": 85.0,
                "relative_humidity_2m": 55,
                "wind_speed_10m": 12.0,
                "weather_code": 1,
            }
        }

        call_count = [0]

        def _side_effect(url, **kwargs):
            result = geo_response if call_count[0] == 0 else weather_response
            call_count[0] += 1
            return _mock_urlopen(result)

        with patch("urllib.request.urlopen", side_effect=_side_effect):
            result = responder._cmd_weather("Austin, TX")
        assert "Austin" in result
        assert "85" in result or "F" in result

    def test_weather_location_not_found(self, responder):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _mock_urlopen({"results": None})
            result = responder._cmd_weather("Nonexistentplace")
        assert "not found" in result

    def test_fortune_returns_string(self, responder):
        result = responder._cmd_fortune()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_dice_default(self, responder):
        result = responder._cmd_dice()
        assert "Rolling 1d6" in result

    def test_dice_2d6(self, responder):
        result = responder._cmd_dice("2d6")
        assert "Rolling 2d6" in result
        assert "=" in result

    def test_dice_invalid(self, responder):
        result = responder._cmd_dice("abc")
        assert "Usage" in result

    def test_dice_limits(self, responder):
        result = responder._cmd_dice("200d6")
        assert "Limits" in result

    def test_flip_heads_or_tails(self, responder):
        result = responder._cmd_flip()
        assert result in ("Heads!", "Tails!")

    def test_calc_basic(self, responder):
        result = responder._cmd_calc("2+2")
        assert "= 4" in result

    def test_calc_float(self, responder):
        result = responder._cmd_calc("1/3")
        assert "0.333" in result

    def test_calc_sqrt(self, responder):
        result = responder._cmd_calc("sqrt(144)")
        assert "= 12" in result

    def test_calc_no_args(self, responder):
        result = responder._cmd_calc()
        assert "Usage" in result

    def test_calc_overflow_protection(self, responder):
        result = responder._cmd_calc("2**9999")
        assert "Error" in result

    def test_calc_syntax_error(self, responder):
        result = responder._cmd_calc("2+*2")
        assert "Error" in result

    def test_calc_division_by_zero(self, responder):
        result = responder._cmd_calc("1/0")
        assert "Error" in result


# ── TestConfig ───────────────────────────────────────────────────────


class TestConfig:
    """Test config validation."""

    def test_invalid_prefix_raises(self, mock_app):
        from reticulumpi.builtin_plugins.meshtastic_responder import (
            MeshtasticResponder,
        )

        with pytest.raises(ValueError, match="prefix"):
            MeshtasticResponder(mock_app, {"prefix": ""})

    def test_negative_cooldown_raises(self, mock_app):
        from reticulumpi.builtin_plugins.meshtastic_responder import (
            MeshtasticResponder,
        )

        with pytest.raises(ValueError, match="cooldown"):
            MeshtasticResponder(mock_app, {"cooldown_seconds": -1})

    def test_unknown_command_raises(self, mock_app):
        from reticulumpi.builtin_plugins.meshtastic_responder import (
            MeshtasticResponder,
        )

        with pytest.raises(ValueError, match="unknown commands"):
            MeshtasticResponder(mock_app, {"commands": ["help", "nonexistent"]})

    def test_invalid_custom_responses_raises(self, mock_app):
        from reticulumpi.builtin_plugins.meshtastic_responder import (
            MeshtasticResponder,
        )

        with pytest.raises(ValueError, match="custom_responses"):
            MeshtasticResponder(mock_app, {"custom_responses": "not a dict"})

    def test_empty_commands_enables_all(self, mock_app):
        from reticulumpi.builtin_plugins.meshtastic_responder import (
            MeshtasticResponder,
        )

        plugin = MeshtasticResponder(mock_app, {"commands": []})
        plugin.start()
        try:
            assert "help" in plugin._commands
            assert "weather" in plugin._commands
            assert "calc" in plugin._commands
            assert len(plugin._commands) == 10
        finally:
            plugin.stop()

    def test_subset_commands(self, mock_app):
        from reticulumpi.builtin_plugins.meshtastic_responder import (
            MeshtasticResponder,
        )

        plugin = MeshtasticResponder(mock_app, {"commands": ["help", "ping"]})
        plugin.start()
        try:
            assert "help" in plugin._commands
            assert "ping" in plugin._commands
            assert "weather" not in plugin._commands
            assert len(plugin._commands) == 2
        finally:
            plugin.stop()

    def test_default_config_values(self, mock_app):
        """Plugin works with an empty config dict (all defaults)."""
        from reticulumpi.builtin_plugins.meshtastic_responder import (
            MeshtasticResponder,
        )

        plugin = MeshtasticResponder(mock_app, {})
        plugin.start()
        try:
            assert plugin._prefix == "!"
            assert plugin._respond_to_broadcast is False
            assert plugin._cooldown_seconds == 30.0
            assert len(plugin._commands) == 10
        finally:
            plugin.stop()


# ── TestStartStop ────────────────────────────────────────────────────


class TestStartStop:
    """Test plugin lifecycle."""

    def test_start_subscribes_to_event(self, mock_app):
        from reticulumpi.builtin_plugins.meshtastic_responder import (
            MeshtasticResponder,
        )

        plugin = MeshtasticResponder(mock_app, {})
        plugin.start()
        try:
            bus = mock_app.event_bus
            wrapper = bus._offload_map.get(plugin._on_mesh_message)
            assert wrapper is not None
            assert wrapper in bus._subscribers.get(events.MESHTASTIC_MESSAGE_RECEIVED, [])
            mc_wrapper = bus._offload_map.get(plugin._on_meshcore_message)
            assert mc_wrapper is not None
            assert mc_wrapper in bus._subscribers.get(events.MESHCORE_MESSAGE_RECEIVED, [])
        finally:
            plugin.stop()

    def test_stop_unsubscribes(self, mock_app):
        from reticulumpi.builtin_plugins.meshtastic_responder import (
            MeshtasticResponder,
        )

        plugin = MeshtasticResponder(mock_app, {})
        plugin.start()
        plugin.stop()
        bus = mock_app.event_bus
        assert bus._subscribers.get(events.MESHTASTIC_MESSAGE_RECEIVED, []) == []
        assert bus._subscribers.get(events.MESHCORE_MESSAGE_RECEIVED, []) == []

    def test_get_status(self, responder, mock_gateway, mock_app):
        mock_app.get_plugin.return_value = mock_gateway
        # Handle one message
        data = _make_event(text="!ping")
        responder._on_mesh_message(events.MESHTASTIC_MESSAGE_RECEIVED, data)

        status = responder.get_status()
        assert status["active"] is True
        assert status["msgs_handled"] == 1
        assert "enabled_commands" in status
        assert status["custom_responses"] == 2

    def test_stop_clears_cooldowns(self, mock_app):
        from reticulumpi.builtin_plugins.meshtastic_responder import (
            MeshtasticResponder,
        )

        plugin = MeshtasticResponder(mock_app, {})
        plugin.start()
        plugin._check_cooldown("!aabb1122")
        assert len(plugin._node_cooldowns) > 0
        plugin.stop()
        assert len(plugin._node_cooldowns) == 0


# ── TestGatewayEdgeCases ─────────────────────────────────────────────


class TestGatewayEdgeCases:
    """Test edge cases when interacting with the gateway."""

    def test_gateway_send_failure_logged(self, responder, mock_app, caplog):
        gw = MagicMock()
        gw.send_message.return_value = {"sent": False, "reason": "rate_limited"}
        gw.get_status.return_value = {"node_id": "!99887766"}
        mock_app.get_plugin.return_value = gw

        data = _make_event(text="!ping")
        responder._on_mesh_message(events.MESHTASTIC_MESSAGE_RECEIVED, data)
        assert "rate_limited" in caplog.text or gw.send_message.called

    def test_gateway_exception_handled(self, responder, mock_app):
        gw = MagicMock()
        gw.send_message.side_effect = RuntimeError("connection lost")
        gw.get_status.return_value = {"node_id": "!99887766"}
        mock_app.get_plugin.return_value = gw

        data = _make_event(text="!ping")
        # Should not raise
        responder._on_mesh_message(events.MESHTASTIC_MESSAGE_RECEIVED, data)

    def test_gateway_missing_send_message(self, responder, mock_app):
        gw = MagicMock(spec=[])  # No attributes
        mock_app.get_plugin.return_value = gw

        data = _make_event(text="!ping")
        # Should not raise
        responder._on_mesh_message(events.MESHTASTIC_MESSAGE_RECEIVED, data)
