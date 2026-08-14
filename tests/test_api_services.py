"""Tests for web_dashboard api_services REST handlers.

Currently covers ``handle_node_tracker_history``.  The handler is wrapped in
``@api_cache(...)`` which uses ``functools.wraps``; the tests exercise the
unwrapped handler via ``__wrapped__`` so cached responses never bleed across
test cases.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock

import pytest

from reticulumpi.builtin_plugins.web_dashboard.api_services import (
    _check_send_rate_limit,
    handle_delete_conversation,
    handle_link_tester_start,
    handle_meshtastic_channel_delete,
    handle_meshtastic_channel_join,
    handle_meshtastic_device_reset,
    handle_node_tracker_history,
)

# Unwrap the @api_cache decorator so caching does not bleed across tests.
_history_handler = handle_node_tracker_history.__wrapped__


# ── Test helpers ───────────────────────────────────────────────────────


def _make_request(query_string="", plugin_mock=None):
    """Create a mock aiohttp.web.Request.

    Mirrors the harness in ``test_api_write_endpoints.py``: query strings are
    parsed by splitting on ``&`` and ``=`` into a plain dict so handler calls
    such as ``request.query.get("nodes", "")`` behave realistically.
    """
    request = MagicMock()

    # Parse query string into a plain dict.
    query = {}
    if query_string:
        for part in query_string.lstrip("?").split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                query[k] = v
    request.query = query
    request.remote = "127.0.0.1"

    if plugin_mock is None:
        plugin_mock = MagicMock()
    request.app = {"plugin": plugin_mock}
    return request


def _parse_response(resp) -> dict:
    """Parse the JSON text from an aiohttp.web.Response."""
    return json.loads(resp.text)


def _make_plugin(tracker):
    """Build a plugin mock whose ``app.get_plugin`` returns ``tracker``."""
    plugin_mock = MagicMock()
    plugin_mock.app.get_plugin.return_value = tracker
    return plugin_mock


def _make_link_tester_request(body, plugin_mock):
    request = _make_request(plugin_mock=plugin_mock)

    async def _json():
        return body

    request.json = _json
    return request


# ── handle_link_tester_start ───────────────────────────────────────────


class TestHandleLinkTesterStart:
    @pytest.mark.parametrize("body", [[], 1, "invalid"])
    def test_rejects_non_object_json_body(self, body):
        link_tester = MagicMock()
        request = _make_link_tester_request(body, _make_plugin(link_tester))

        response = asyncio.run(handle_link_tester_start(request))

        assert response.status == 400
        assert _parse_response(response)["error"] == "request body must be a JSON object"
        link_tester.start_test.assert_not_called()

    @pytest.mark.parametrize("count", [True, False, -1, "-2", "2", 1.5])
    def test_rejects_noninteger_or_negative_count(self, count):
        link_tester = MagicMock()
        request = _make_link_tester_request(
            {"target": "!11223344", "count": count},
            _make_plugin(link_tester),
        )

        response = asyncio.run(handle_link_tester_start(request))

        assert response.status == 400
        assert _parse_response(response)["error"] == (
            "count must be a non-negative integer (0 = unlimited)"
        )
        link_tester.start_test.assert_not_called()

    def test_positive_integer_is_forwarded_without_coercion(self):
        link_tester = MagicMock()
        link_tester.start_test.return_value = {
            "ok": True,
            "target": "!11223344",
            "count": 2,
        }
        request = _make_link_tester_request(
            {"target": "!11223344", "count": 2},
            _make_plugin(link_tester),
        )

        response = asyncio.run(handle_link_tester_start(request))

        assert response.status == 200
        link_tester.start_test.assert_called_once_with(
            target="!11223344",
            count=2,
        )

    def test_zero_is_forwarded_as_unlimited_count(self):
        link_tester = MagicMock()
        link_tester.start_test.return_value = {
            "ok": True,
            "target": "!11223344",
            "count": 0,
        }
        request = _make_link_tester_request(
            {"target": "!11223344", "count": 0},
            _make_plugin(link_tester),
        )

        response = asyncio.run(handle_link_tester_start(request))

        assert response.status == 200
        link_tester.start_test.assert_called_once_with(
            target="!11223344",
            count=0,
        )


# ── handle_node_tracker_history ──────────────────────────────────────────


class TestHandleNodeTrackerHistory:
    """Unit tests for the node location history endpoint."""

    def test_happy_path_returns_history_and_calls_get_history(self):
        history = {"msh:!aa": [{"lat": 1.0, "lon": 2.0, "ts": 123.0}], "mc:bb": []}
        tracker = MagicMock()
        tracker.get_history.return_value = history
        plugin_mock = _make_plugin(tracker)
        request = _make_request(query_string="nodes=msh:!aa,mc:bb", plugin_mock=plugin_mock)

        before = time.time()
        resp = asyncio.run(_history_handler(request))
        after = time.time()

        assert resp.status == 200
        data = _parse_response(resp)
        assert data["ok"] is True
        assert data["data"]["history"] == history

        tracker.get_history.assert_called_once()
        args, kwargs = tracker.get_history.call_args
        keys, since, until, limit = args
        assert keys == ["msh:!aa", "mc:bb"]
        # Default 24h window -> since ~= now - 24*3600, within a few seconds.
        expected_since_low = before - 24 * 3600 - 5
        expected_since_high = after - 24 * 3600 + 5
        assert expected_since_low <= since <= expected_since_high
        assert until is None
        assert limit == 500

    def test_missing_nodes_param_returns_400(self):
        tracker = MagicMock()
        plugin_mock = _make_plugin(tracker)
        request = _make_request(query_string="", plugin_mock=plugin_mock)

        resp = asyncio.run(_history_handler(request))

        assert resp.status == 400
        data = _parse_response(resp)
        assert data["ok"] is False
        assert "nodes" in data["error"]
        tracker.get_history.assert_not_called()

    def test_more_than_ten_nodes_returns_400(self):
        tracker = MagicMock()
        plugin_mock = _make_plugin(tracker)
        nodes = ",".join(f"n{i}" for i in range(11))
        request = _make_request(query_string=f"nodes={nodes}", plugin_mock=plugin_mock)

        resp = asyncio.run(_history_handler(request))

        assert resp.status == 400
        data = _parse_response(resp)
        assert data["ok"] is False
        assert "10" in data["error"]
        tracker.get_history.assert_not_called()

    def test_invalid_hours_returns_400(self):
        tracker = MagicMock()
        plugin_mock = _make_plugin(tracker)
        request = _make_request(query_string="nodes=a&hours=abc", plugin_mock=plugin_mock)

        resp = asyncio.run(_history_handler(request))

        assert resp.status == 400
        data = _parse_response(resp)
        assert data["ok"] is False
        tracker.get_history.assert_not_called()

    def test_invalid_limit_returns_400(self):
        tracker = MagicMock()
        plugin_mock = _make_plugin(tracker)
        request = _make_request(query_string="nodes=a&limit=abc", plugin_mock=plugin_mock)

        resp = asyncio.run(_history_handler(request))

        assert resp.status == 400
        data = _parse_response(resp)
        assert data["ok"] is False
        tracker.get_history.assert_not_called()

    def test_negative_bounds_return_400_instead_of_unlimited_query(self):
        tracker = MagicMock()
        plugin_mock = _make_plugin(tracker)

        for query in ("nodes=a&limit=-1", "nodes=a&hours=-1"):
            response = asyncio.run(
                _history_handler(_make_request(query_string=query, plugin_mock=plugin_mock))
            )
            assert response.status == 400

        tracker.get_history.assert_not_called()

    def test_hours_capped_at_720(self):
        tracker = MagicMock()
        tracker.get_history.return_value = {}
        plugin_mock = _make_plugin(tracker)
        request = _make_request(query_string="nodes=a&hours=10000", plugin_mock=plugin_mock)

        before = time.time()
        resp = asyncio.run(_history_handler(request))
        after = time.time()

        assert resp.status == 200
        args, _ = tracker.get_history.call_args
        _keys, since, _until, _limit = args
        # hours capped at 720 -> since ~= now - 720*3600.
        assert before - 720 * 3600 - 5 <= since <= after - 720 * 3600 + 5

    def test_limit_capped_at_2000(self):
        tracker = MagicMock()
        tracker.get_history.return_value = {}
        plugin_mock = _make_plugin(tracker)
        request = _make_request(query_string="nodes=a&limit=99999", plugin_mock=plugin_mock)

        resp = asyncio.run(_history_handler(request))

        assert resp.status == 200
        args, _ = tracker.get_history.call_args
        _keys, _since, _until, limit = args
        assert limit == 2000

    def test_csv_strips_spaces_and_empty_segments(self):
        tracker = MagicMock()
        tracker.get_history.return_value = {}
        plugin_mock = _make_plugin(tracker)
        request = _make_request(query_string="nodes=a, b ,,c", plugin_mock=plugin_mock)

        resp = asyncio.run(_history_handler(request))

        assert resp.status == 200
        args, _ = tracker.get_history.call_args
        keys, _since, _until, _limit = args
        assert keys == ["a", "b", "c"]

    def test_tracker_unavailable_returns_empty_history(self):
        plugin_mock = _make_plugin(None)
        request = _make_request(query_string="nodes=a", plugin_mock=plugin_mock)

        resp = asyncio.run(_history_handler(request))

        assert resp.status == 200
        data = _parse_response(resp)
        assert data["ok"] is True
        assert data["data"]["history"] == {}
        assert "message" in data["data"]

    def test_get_history_raises_returns_500(self):
        tracker = MagicMock()
        tracker.get_history.side_effect = RuntimeError("boom")
        plugin_mock = _make_plugin(tracker)
        request = _make_request(query_string="nodes=a", plugin_mock=plugin_mock)

        resp = asyncio.run(_history_handler(request))

        assert resp.status == 500
        data = _parse_response(resp)
        assert data["ok"] is False


# ── gap-003: State-changing handler tests ──────────────────────────────


def _make_json_request(body, match_info=None, plugin_mock=None):
    """Create a mock aiohttp.web.Request with a JSON body."""
    request = MagicMock()
    request.query = {}
    request.match_info = match_info or {}
    request.remote = "127.0.0.1"

    async def _json():
        return body

    request.json = _json
    if plugin_mock is None:
        plugin_mock = MagicMock()
    request.app = {"plugin": plugin_mock}
    return request


class TestHandleMeshtasticChannelJoin:
    """Tests for POST /api/meshtastic/channels/join."""

    def test_join_by_name_and_psk(self):
        gw = MagicMock()
        gw.join_channel.return_value = {"ok": True, "index": 1, "name": "test-ch"}
        plugin_mock = _make_plugin(gw)
        request = _make_json_request(
            {"name": "test-ch", "psk": "default"},
            plugin_mock=plugin_mock,
        )

        resp = asyncio.run(handle_meshtastic_channel_join(request))

        assert resp.status == 200
        data = _parse_response(resp)
        assert data["ok"] is True
        gw.join_channel.assert_called_once()

    def test_join_missing_name_returns_400(self):
        gw = MagicMock()
        plugin_mock = _make_plugin(gw)
        request = _make_json_request({"psk": "default"}, plugin_mock=plugin_mock)

        resp = asyncio.run(handle_meshtastic_channel_join(request))

        assert resp.status == 400
        data = _parse_response(resp)
        assert "name" in data["error"]

    def test_join_by_url(self):
        gw = MagicMock()
        gw.join_channel_url.return_value = {"ok": True}
        plugin_mock = _make_plugin(gw)
        request = _make_json_request(
            {"url": "https://meshtastic.org/e/#test"},
            plugin_mock=plugin_mock,
        )

        resp = asyncio.run(handle_meshtastic_channel_join(request))

        assert resp.status == 200
        gw.join_channel_url.assert_called_once_with("https://meshtastic.org/e/#test")

    def test_join_gateway_unavailable_returns_503(self):
        plugin_mock = _make_plugin(None)
        request = _make_json_request(
            {"name": "test"},
            plugin_mock=plugin_mock,
        )

        resp = asyncio.run(handle_meshtastic_channel_join(request))

        assert resp.status == 503

    def test_join_failure_returns_400(self):
        gw = MagicMock()
        gw.join_channel.return_value = {"ok": False, "reason": "Radio busy"}
        plugin_mock = _make_plugin(gw)
        request = _make_json_request(
            {"name": "test-ch", "psk": "default"},
            plugin_mock=plugin_mock,
        )

        resp = asyncio.run(handle_meshtastic_channel_join(request))

        assert resp.status == 400
        data = _parse_response(resp)
        assert "Radio busy" in data["error"]


class TestHandleMeshtasticChannelDelete:
    """Tests for DELETE /api/meshtastic/channels/{index}."""

    def test_delete_channel_success(self):
        gw = MagicMock()
        gw.delete_channel.return_value = {"ok": True, "index": 3}
        plugin_mock = _make_plugin(gw)
        request = _make_request(plugin_mock=plugin_mock)
        request.match_info = {"index": "3"}

        resp = asyncio.run(handle_meshtastic_channel_delete(request))

        assert resp.status == 200
        data = _parse_response(resp)
        assert data["ok"] is True
        gw.delete_channel.assert_called_once_with(3)

    def test_delete_channel_invalid_index_returns_400(self):
        gw = MagicMock()
        plugin_mock = _make_plugin(gw)
        request = _make_request(plugin_mock=plugin_mock)
        request.match_info = {"index": "abc"}

        resp = asyncio.run(handle_meshtastic_channel_delete(request))

        assert resp.status == 400

    def test_delete_channel_out_of_range_returns_400(self):
        gw = MagicMock()
        plugin_mock = _make_plugin(gw)
        request = _make_request(plugin_mock=plugin_mock)
        request.match_info = {"index": "0"}

        resp = asyncio.run(handle_meshtastic_channel_delete(request))

        assert resp.status == 400
        data = _parse_response(resp)
        assert "1-7" in data["error"]

    def test_delete_channel_gateway_unavailable_returns_503(self):
        plugin_mock = _make_plugin(None)
        request = _make_request(plugin_mock=plugin_mock)
        request.match_info = {"index": "1"}

        resp = asyncio.run(handle_meshtastic_channel_delete(request))

        assert resp.status == 503


class TestHandleMeshtasticDeviceReset:
    """Tests for POST /api/meshtastic/device/reset."""

    def test_reset_success(self):
        gw = MagicMock()
        gw.reset_device.return_value = {"ok": True, "method": "usb"}
        plugin_mock = _make_plugin(gw)
        request = _make_request(plugin_mock=plugin_mock)

        resp = asyncio.run(handle_meshtastic_device_reset(request))

        assert resp.status == 200
        data = _parse_response(resp)
        assert data["ok"] is True
        gw.reset_device.assert_called_once()

    def test_reset_failure_returns_400(self):
        gw = MagicMock()
        gw.reset_device.return_value = {"ok": False, "reason": "Device offline"}
        plugin_mock = _make_plugin(gw)
        request = _make_request(plugin_mock=plugin_mock)

        resp = asyncio.run(handle_meshtastic_device_reset(request))

        assert resp.status == 400
        data = _parse_response(resp)
        assert "Device offline" in data["error"]

    def test_reset_gateway_unavailable_returns_503(self):
        plugin_mock = _make_plugin(None)
        request = _make_request(plugin_mock=plugin_mock)

        resp = asyncio.run(handle_meshtastic_device_reset(request))

        assert resp.status == 503


class TestHandleDeleteConversation:
    """Tests for DELETE /api/messages/conversation/{contact_id}."""

    def test_delete_success(self):
        hub = MagicMock()
        hub.delete_conversation.return_value = 5
        plugin_mock = MagicMock()
        plugin_mock.app.get_plugin.return_value = hub
        request = _make_request(plugin_mock=plugin_mock)
        request.match_info = {"contact_id": "peer123"}

        resp = asyncio.run(handle_delete_conversation(request))

        assert resp.status == 200
        data = _parse_response(resp)
        assert data["ok"] is True
        assert data["data"]["deleted"] == 5
        hub.delete_conversation.assert_called_once()
        call_args = hub.delete_conversation.call_args
        assert call_args[0][0] == "peer123"

    def test_delete_hub_unavailable_returns_503(self):
        plugin_mock = MagicMock()
        plugin_mock.app.get_plugin.return_value = None
        request = _make_request(plugin_mock=plugin_mock)
        request.match_info = {"contact_id": "peer123"}

        resp = asyncio.run(handle_delete_conversation(request))

        assert resp.status == 503
        data = _parse_response(resp)
        assert data["ok"] is False


# ── gap-009: Rate limiter tests ────────────────────────────────────────


class TestCheckSendRateLimit:
    """Unit tests for _check_send_rate_limit sliding window."""

    def test_first_request_allowed(self):
        plugin = MagicMock(spec=[])  # empty spec so no auto-vivified attrs
        allowed, retry_after = _check_send_rate_limit(
            plugin, "test-key", max_per_window=5, window_seconds=60
        )
        assert allowed is True
        assert retry_after == 0.0

    def test_second_request_blocked_at_limit_1(self):
        plugin = MagicMock(spec=[])
        # First request: allowed
        ok1, _ = _check_send_rate_limit(plugin, "key-a", max_per_window=1, window_seconds=60)
        assert ok1 is True

        # Second request: blocked
        ok2, retry_after = _check_send_rate_limit(
            plugin, "key-a", max_per_window=1, window_seconds=60
        )
        assert ok2 is False
        assert retry_after > 0

    def test_different_keys_independent(self):
        plugin = MagicMock(spec=[])
        ok1, _ = _check_send_rate_limit(plugin, "key-x", max_per_window=1, window_seconds=60)
        ok2, _ = _check_send_rate_limit(plugin, "key-y", max_per_window=1, window_seconds=60)
        assert ok1 is True
        assert ok2 is True

    def test_429_response_has_retry_after_header(self):
        """Integration-style: send 2 messages with max_per_window=1,
        verify the handler returns 429 with a Retry-After header."""
        hub = MagicMock()
        hub.send_message.return_value = {"sent": True}
        plugin_mock = MagicMock()
        plugin_mock.app.get_plugin.return_value = hub
        plugin_mock.config = {}

        # Clear any stale rate state from previous test runs
        if hasattr(plugin_mock, "_send_rate_state"):
            del plugin_mock._send_rate_state

        from reticulumpi.builtin_plugins.web_dashboard.api_services import (
            handle_send_message,
        )

        def _make_send_request():
            req = MagicMock()
            req.query = {}
            req.match_info = {}
            req.remote = "127.0.0.1"
            req.headers = {"User-Agent": "test-agent"}
            req.get = lambda k, default=None: "authenticated-token"

            async def _json():
                return {
                    "transport": "lxmf",
                    "text": "hello",
                    "destination": "aabb",
                }

            req.json = _json
            req.app = {"plugin": plugin_mock}
            return req

        # Override rate limit config to max_per_window=1
        plugin_mock.config["send_rate_limit"] = {
            "max_per_window": 1,
            "window_seconds": 60,
        }

        # First send: should succeed
        resp1 = asyncio.run(handle_send_message(_make_send_request()))
        assert resp1.status == 200

        # Second send: should be rate-limited (429)
        resp2 = asyncio.run(handle_send_message(_make_send_request()))
        assert resp2.status == 429
        assert "Retry-After" in resp2.headers
