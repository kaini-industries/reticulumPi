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

from reticulumpi.builtin_plugins.web_dashboard.api_services import (
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
