"""Tests for the /api/routing endpoint."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

from reticulumpi.builtin_plugins.web_dashboard.api import handle_routing


def _make_request(query_string="", conn_mon=None):
    """Create a mock aiohttp.web.Request."""
    request = MagicMock()
    # Parse query string into a dict
    query = {}
    if query_string:
        for part in query_string.lstrip("?").split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                query[k] = v
    request.query = query

    plugin = MagicMock()
    plugin.app.get_plugin.return_value = conn_mon
    request.app = {"plugin": plugin}
    return request


class TestRoutingEndpoint:
    def test_routing_returns_summary(self):
        """The routing endpoint returns summary data."""
        conn_mon = MagicMock()
        conn_mon.get_routing_data.return_value = {
            "summary": {
                "path_count": 5,
                "hop_distribution": {1: 2, 2: 3},
                "diagnostics": [],
            },
            "paths": [],
            "total_paths": 5,
            "page": 1,
            "per_page": 0,
            "pages": 0,
            "rate_table": [],
            "blackholed": {},
        }

        request = _make_request("per_page=0", conn_mon)
        resp = asyncio.run(handle_routing(request))

        data = json.loads(resp.text)
        assert data["ok"] is True
        assert data["data"]["summary"]["path_count"] == 5
        assert data["data"]["paths"] == []

    def test_routing_passes_params(self):
        """Query parameters are passed through to get_routing_data."""
        conn_mon = MagicMock()
        conn_mon.get_routing_data.return_value = {
            "summary": {},
            "paths": [{"hash": "aa" * 16}],
            "total_paths": 1,
            "page": 2,
            "per_page": 50,
            "pages": 3,
            "rate_table": [],
            "blackholed": {},
        }

        request = _make_request("page=2&per_page=50&sort=timestamp&order=desc&interface=TCP&min_hops=2&max_hops=4&search=aa", conn_mon)
        resp = asyncio.run(handle_routing(request))

        # Verify get_routing_data was called with parsed params
        conn_mon.get_routing_data.assert_called_once_with(
            page=2,
            per_page=50,
            sort="timestamp",
            order="desc",
            iface_filter="TCP",
            min_hops=2,
            max_hops=4,
            search="aa",
        )

        data = json.loads(resp.text)
        assert data["ok"] is True
        assert data["data"]["page"] == 2

    def test_routing_missing_plugin(self):
        """Returns graceful error when connectivity_monitor not available."""
        request = _make_request("", conn_mon=None)
        resp = asyncio.run(handle_routing(request))

        data = json.loads(resp.text)
        assert data["ok"] is True
        assert data["data"]["total_paths"] == 0
        assert "message" in data["data"]
