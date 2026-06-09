"""Tests for the WebSocket handler (websocket_handler.py).

Covers: RNode config parsing, interface collection, transport traffic
enrichment, WebSocket auth/connection lifecycle, and broadcast logic.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from reticulumpi.builtin_plugins.web_dashboard import websocket_handler as wsh_module
from reticulumpi.builtin_plugins.web_dashboard.websocket_handler import (
    _broadcast_metrics,
    _check_ws_origin,
    _collect_broadcast_data,
    _collect_interfaces,
    _enrich_transport_traffic,
    _extract_radio,
    _handle_ws_command,
    _heartbeat_interval,
    _lookup_message_row,
    _on_alert_event,
    _on_firmware_event,
    _on_internet_event,
    _on_message_event,
    _on_position_recorded_event,
    _on_status_event,
    _parse_rnode_config,
    _push_to_clients,
    _send_with_timeout,
    _start_broadcast_task,
    _stop_broadcast_task,
    _ws_clients,
    websocket_metrics,
    websocket_spectrum,
)


async def _empty_async_iter():
    """Async iterator that yields nothing — simulates an idle WebSocket."""
    return
    yield  # makes this an async generator


@pytest.fixture(autouse=True)
def _reset_ws_clients():
    # _ws_clients is a module-level set; without reset, a failing test leaves
    # entries behind and poisons later tests. Required for safe re-ordering
    # and parallel runs.
    _ws_clients.clear()
    wsh_module._warm_cache_data = {}
    wsh_module._warm_cache_ts = 0.0
    wsh_module._last_heartbeat_ts = 0.0
    wsh_module._hb_count = 0
    wsh_module._hb_fail = 0
    wsh_module._cache_hits = 0
    wsh_module._last_hb_summary_ts = 0.0
    yield
    _ws_clients.clear()
    wsh_module._warm_cache_data = {}
    wsh_module._warm_cache_ts = 0.0
    wsh_module._last_heartbeat_ts = 0.0
    wsh_module._hb_count = 0
    wsh_module._hb_fail = 0
    wsh_module._cache_hits = 0
    wsh_module._last_hb_summary_ts = 0.0


# ── _extract_radio tests ───────────────────────────────────────────


class TestExtractRadio:
    def test_extracts_known_keys(self):
        data = {
            "frequency": "867200000",
            "bandwidth": "125000",
            "txpower": "17",
            "spreadingfactor": "8",
            "codingrate": "5",
        }
        radio = _extract_radio(data)
        assert radio["frequency"] == 867200000
        assert radio["bandwidth"] == 125000
        assert radio["txpower"] == 17
        assert radio["spreadingfactor"] == 8
        assert radio["codingrate"] == 5

    def test_ignores_unknown_keys(self):
        data = {"frequency": "867200000", "port": "/dev/ttyUSB0", "flow_control": "False"}
        radio = _extract_radio(data)
        assert "port" not in radio
        assert "flow_control" not in radio
        assert radio["frequency"] == 867200000

    def test_empty_data(self):
        assert _extract_radio({}) == {}

    def test_float_values(self):
        data = {"frequency": "867.2"}
        radio = _extract_radio(data)
        assert radio["frequency"] == 867.2

    def test_string_fallback(self):
        data = {"frequency": "not_a_number"}
        radio = _extract_radio(data)
        assert radio["frequency"] == "not_a_number"


# ── _parse_rnode_config tests ──────────────────────────────────────


class TestParseRnodeConfig:
    def _write_config(self, content: str) -> str:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False)
        f.write(content)
        f.close()
        return f.name

    def test_parses_rnode_section(self):
        cfg = self._write_config(
            "[interfaces]\n"
            "  [[RNode LoRa]]\n"
            "    type = RNodeInterface\n"
            "    frequency = 867200000\n"
            "    bandwidth = 125000\n"
            "    txpower = 17\n"
            "    spreadingfactor = 8\n"
            "    codingrate = 5\n"
        )
        try:
            with patch(
                "reticulumpi.builtin_plugins.web_dashboard.websocket_handler._RETICULUM_CONFIG_PATHS",
                [cfg],
            ):
                # Clear cache
                import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

                wsh._rnode_config_cache = None
                wsh._rnode_config_mtime = 0

                result = _parse_rnode_config()
                assert "RNode LoRa" in result
                assert result["RNode LoRa"]["frequency"] == 867200000
                assert result["RNode LoRa"]["bandwidth"] == 125000
        finally:
            os.unlink(cfg)

    def test_skips_non_rnode_sections(self):
        cfg = self._write_config(
            "[interfaces]\n"
            "  [[TCP Client]]\n"
            "    type = TCPClientInterface\n"
            "    target_host = example.com\n"
            "  [[RNode LoRa]]\n"
            "    type = RNodeInterface\n"
            "    frequency = 867200000\n"
        )
        try:
            with patch(
                "reticulumpi.builtin_plugins.web_dashboard.websocket_handler._RETICULUM_CONFIG_PATHS",
                [cfg],
            ):
                import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

                wsh._rnode_config_cache = None
                wsh._rnode_config_mtime = 0

                result = _parse_rnode_config()
                assert "TCP Client" not in result
                assert "RNode LoRa" in result
        finally:
            os.unlink(cfg)

    def test_multiple_rnode_sections(self):
        cfg = self._write_config(
            "[interfaces]\n"
            "  [[RNode 868]]\n"
            "    type = RNodeInterface\n"
            "    frequency = 868000000\n"
            "  [[RNode 915]]\n"
            "    type = RNodeInterface\n"
            "    frequency = 915000000\n"
        )
        try:
            with patch(
                "reticulumpi.builtin_plugins.web_dashboard.websocket_handler._RETICULUM_CONFIG_PATHS",
                [cfg],
            ):
                import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

                wsh._rnode_config_cache = None
                wsh._rnode_config_mtime = 0

                result = _parse_rnode_config()
                assert len(result) == 2
                assert result["RNode 868"]["frequency"] == 868000000
                assert result["RNode 915"]["frequency"] == 915000000
        finally:
            os.unlink(cfg)

    def test_caching(self):
        cfg = self._write_config(
            "[interfaces]\n  [[RNode LoRa]]\n    type = RNodeInterface\n    frequency = 867200000\n"
        )
        try:
            with patch(
                "reticulumpi.builtin_plugins.web_dashboard.websocket_handler._RETICULUM_CONFIG_PATHS",
                [cfg],
            ):
                import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

                wsh._rnode_config_cache = None
                wsh._rnode_config_mtime = 0

                result1 = _parse_rnode_config()
                result2 = _parse_rnode_config()
                # Should return same cached object
                assert result1 is result2
        finally:
            os.unlink(cfg)

    def test_no_config_file(self):
        with patch(
            "reticulumpi.builtin_plugins.web_dashboard.websocket_handler._RETICULUM_CONFIG_PATHS",
            ["/nonexistent/path"],
        ):
            import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

            wsh._rnode_config_cache = None
            wsh._rnode_config_mtime = 0

            result = _parse_rnode_config()
            assert result == {}

    def test_returns_cached_on_missing_file(self):
        """If file disappears, return last cached result."""
        with patch(
            "reticulumpi.builtin_plugins.web_dashboard.websocket_handler._RETICULUM_CONFIG_PATHS",
            ["/nonexistent/path"],
        ):
            import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

            cached = {"RNode LoRa": {"frequency": 867200000}}
            wsh._rnode_config_cache = cached
            wsh._rnode_config_mtime = 0

            result = _parse_rnode_config()
            assert result is cached


# ── _collect_interfaces tests ──────────────────────────────────────


class TestCollectInterfaces:
    def test_shared_instance_mode(self):
        """Collects from get_interface_stats in shared-instance mode."""
        rns = MagicMock()
        rns.get_interface_stats.return_value = {
            "interfaces": [
                {
                    "name": "RNodeInterface[RNode LoRa]",
                    "type": "RNodeInterface",
                    "status": True,
                    "bitrate": 5000,
                    "rxb": 1234,
                    "txb": 5678,
                    "airtime_short": 0.05,
                    "noise_floor": -110,
                },
                {
                    "name": "TCPInterface[TCP Client hub/example.com:4242]",
                    "type": "TCPClientInterface",
                    "status": True,
                    "rxb": 9999,
                    "txb": 8888,
                },
            ]
        }

        with patch(
            "reticulumpi.builtin_plugins.web_dashboard.websocket_handler._parse_rnode_config",
            return_value={"RNode LoRa": {"frequency": 867200000}},
        ):
            result = _collect_interfaces(rns)

        assert len(result) == 2
        rnode = result[0]
        assert rnode["type"] == "RNodeInterface"
        assert rnode["online"] is True
        assert rnode["rxb"] == 1234
        assert rnode["airtime_short"] == 0.05
        assert rnode["noise_floor"] == -110
        assert rnode["radio"]["frequency"] == 867200000

        tcp = result[1]
        assert tcp["type"] == "TCPClientInterface"
        assert tcp["rxb"] == 9999

    def test_skips_local_interfaces(self):
        """LocalClientInterface and LocalServerInterface are filtered out."""
        rns = MagicMock()
        rns.get_interface_stats.return_value = {
            "interfaces": [
                {"name": "Local", "type": "LocalClientInterface", "status": True},
                {"name": "Server", "type": "LocalServerInterface", "status": True},
                {"name": "Auto", "type": "AutoInterface", "status": True, "rxb": 0, "txb": 0},
            ]
        }

        with patch(
            "reticulumpi.builtin_plugins.web_dashboard.websocket_handler._parse_rnode_config",
            return_value={},
        ):
            result = _collect_interfaces(rns)

        assert len(result) == 1
        assert result[0]["type"] == "AutoInterface"

    def test_fallback_standalone_mode(self):
        """Falls back to RNS.Transport.interfaces when no reticulum instance."""
        iface = MagicMock()
        iface.__class__ = type("TCPClientInterface", (), {})
        iface.__str__ = lambda self: "TCPInterface[example:4242]"
        iface.online = True
        iface.bitrate = 10000
        iface.rxb = 100
        iface.txb = 200
        iface.__class__.__name__ = "TCPClientInterface"

        mock_rns = MagicMock()
        mock_rns.Transport.interfaces = [iface]

        with patch.dict("sys.modules", {"RNS": mock_rns}):
            with patch(
                "reticulumpi.builtin_plugins.web_dashboard.websocket_handler._parse_rnode_config",
                return_value={},
            ):
                result = _collect_interfaces(None)

        assert len(result) == 1
        assert result[0]["online"] is True
        assert result[0]["rxb"] == 100

    def test_no_reticulum_returns_empty(self):
        """Returns empty list when collection fails entirely."""
        result = _collect_interfaces(None)
        # May return [] or raise — either is fine as long as no crash
        assert isinstance(result, list)

    def test_rnode_radio_config_matching(self):
        """Radio config is matched by section name appearing in interface name."""
        rns = MagicMock()
        rns.get_interface_stats.return_value = {
            "interfaces": [
                {
                    "name": "RNodeInterface[RNode 868MHz]",
                    "type": "RNodeInterface",
                    "status": True,
                    "rxb": 0,
                    "txb": 0,
                },
            ]
        }

        with patch(
            "reticulumpi.builtin_plugins.web_dashboard.websocket_handler._parse_rnode_config",
            return_value={
                "RNode 868MHz": {"frequency": 868000000, "bandwidth": 125000},
                "RNode 915MHz": {"frequency": 915000000},
            },
        ):
            result = _collect_interfaces(rns)

        assert result[0]["radio"]["frequency"] == 868000000
        assert result[0]["radio"]["bandwidth"] == 125000


# ── _enrich_transport_traffic tests ────────────────────────────────


class TestEnrichTransportTraffic:
    def test_enriches_primaries(self):
        transport = {
            "primaries": [
                {"target_host": "hub.example.com", "target_port": 4242},
            ],
            "active_fallbacks": [],
            "auto_discovery": {"connected": []},
        }
        interfaces = [
            {
                "name": "TCPInterface[TCP Client hub/hub.example.com:4242]",
                "type": "TCPClientInterface",
                "rxb": 1000,
                "txb": 2000,
            }
        ]

        _enrich_transport_traffic(transport, interfaces)

        assert transport["primaries"][0]["rxb"] == 1000
        assert transport["primaries"][0]["txb"] == 2000

    def test_enriches_fallbacks(self):
        transport = {
            "primaries": [],
            "active_fallbacks": [
                {"target_host": "backup.example.com", "target_port": 4242},
            ],
            "auto_discovery": {"connected": []},
        }
        interfaces = [
            {
                "name": "TCPInterface[TCP Client backup/backup.example.com:4242]",
                "type": "TCPClientInterface",
                "rxb": 500,
                "txb": 600,
            }
        ]

        _enrich_transport_traffic(transport, interfaces)

        assert transport["active_fallbacks"][0]["rxb"] == 500

    def test_enriches_auto_discovery(self):
        transport = {
            "primaries": [],
            "active_fallbacks": [],
            "auto_discovery": {
                "connected": [
                    {"target_host": "auto.example.com", "target_port": 4242},
                ]
            },
        }
        interfaces = [
            {
                "name": "TCPInterface[TCP Client auto/auto.example.com:4242]",
                "type": "TCPClientInterface",
                "rxb": 300,
                "txb": 400,
            }
        ]

        _enrich_transport_traffic(transport, interfaces)

        assert transport["auto_discovery"]["connected"][0]["rxb"] == 300

    def test_no_matching_interface(self):
        transport = {
            "primaries": [
                {"target_host": "unknown.example.com", "target_port": 4242},
            ],
            "active_fallbacks": [],
            "auto_discovery": {"connected": []},
        }
        _enrich_transport_traffic(transport, [])

        assert "rxb" not in transport["primaries"][0]

    def test_skips_non_tcp_interfaces(self):
        transport = {
            "primaries": [
                {"target_host": "example.com", "target_port": 4242},
            ],
            "active_fallbacks": [],
            "auto_discovery": {"connected": []},
        }
        interfaces = [
            {
                "name": "AutoInterface[Default]",
                "type": "AutoInterface",
                "rxb": 9999,
                "txb": 9999,
            }
        ]

        _enrich_transport_traffic(transport, interfaces)

        assert "rxb" not in transport["primaries"][0]

    def test_empty_transport_data(self):
        """Gracefully handles missing keys."""
        _enrich_transport_traffic({}, [])
        _enrich_transport_traffic({"primaries": []}, [])


# ── WebSocket auth & connection tests ──────────────────────────────


def _make_ws_request(
    token=None, cookie_token=None, bearer_token=None, max_clients=10, auth_valid=True
):
    """Create a mock aiohttp request for WebSocket tests."""
    request = MagicMock()
    request.query = {}
    if token:
        request.query["token"] = token

    request.cookies = {}
    if cookie_token:
        request.cookies["session"] = cookie_token

    headers = {}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    request.headers = headers

    plugin = MagicMock()
    plugin.config = {"max_websocket_clients": max_clients}
    plugin._auth.validate_token.return_value = auth_valid

    request.app = {"plugin": plugin}
    return request


class TestWebsocketAuth:
    def test_rejects_no_token(self):
        """Closes with 4001 if no token provided."""
        request = _make_ws_request(token=None, cookie_token=None)

        ws_mock = MagicMock()
        ws_mock.prepare = AsyncMock()
        ws_mock.close = AsyncMock()

        with patch("aiohttp.web.WebSocketResponse", return_value=ws_mock):
            asyncio.run(websocket_metrics(request))

        ws_mock.close.assert_called_once()
        call_kwargs = ws_mock.close.call_args
        assert call_kwargs[1].get("code") == 4001 or call_kwargs[0][0] == 4001

    def test_rejects_invalid_token(self):
        """Closes with 4001 if token is invalid."""
        request = _make_ws_request(token="bad_token", auth_valid=False)

        ws_mock = MagicMock()
        ws_mock.prepare = AsyncMock()
        ws_mock.close = AsyncMock()

        with patch("aiohttp.web.WebSocketResponse", return_value=ws_mock):
            asyncio.run(websocket_metrics(request))

        ws_mock.close.assert_called_once()

    def test_rejects_query_token(self):
        """Query param tokens are no longer accepted (security hardening)."""
        request = _make_ws_request(token="valid_token", auth_valid=True)

        ws_mock = MagicMock()
        ws_mock.prepare = AsyncMock()
        ws_mock.close = AsyncMock()

        with patch("aiohttp.web.WebSocketResponse", return_value=ws_mock):
            asyncio.run(websocket_metrics(request))

        ws_mock.close.assert_called_once()

    def test_accepts_cookie_token(self):
        """Falls back to cookie session token."""
        request = _make_ws_request(cookie_token="cookie_token", auth_valid=True)

        ws_mock = MagicMock()
        ws_mock.prepare = AsyncMock()
        ws_mock.__aiter__ = lambda self: _empty_async_iter()

        with patch("aiohttp.web.WebSocketResponse", return_value=ws_mock):
            asyncio.run(websocket_metrics(request))

        # Should have validated the cookie token
        request.app["plugin"]._auth.validate_token.assert_called_with("cookie_token")
        ws_mock.close.assert_not_called()

    def test_accepts_bearer_token(self):
        """Authenticates via Authorization: Bearer header."""
        request = _make_ws_request(bearer_token="bearer_token", auth_valid=True)

        ws_mock = MagicMock()
        ws_mock.prepare = AsyncMock()
        ws_mock.__aiter__ = lambda self: _empty_async_iter()

        with patch("aiohttp.web.WebSocketResponse", return_value=ws_mock):
            asyncio.run(websocket_metrics(request))

        request.app["plugin"]._auth.validate_token.assert_called_with("bearer_token")
        ws_mock.close.assert_not_called()

    def test_rejects_when_max_clients_reached(self):
        """Closes with 4002 when max clients exceeded."""
        request = _make_ws_request(cookie_token="valid", auth_valid=True, max_clients=2)

        ws_mock = MagicMock()
        ws_mock.prepare = AsyncMock()
        ws_mock.close = AsyncMock()

        # Pre-fill _ws_clients to capacity
        dummy1 = MagicMock()
        dummy2 = MagicMock()
        _ws_clients.clear()
        _ws_clients.add(dummy1)
        _ws_clients.add(dummy2)

        try:
            with patch("aiohttp.web.WebSocketResponse", return_value=ws_mock):
                asyncio.run(websocket_metrics(request))

            ws_mock.close.assert_called_once()
            call_args = ws_mock.close.call_args
            assert call_args[1].get("code") == 4002 or call_args[0][0] == 4002
        finally:
            _ws_clients.clear()

    def test_client_added_and_removed(self):
        """Client is tracked in _ws_clients during connection and removed after."""
        request = _make_ws_request(cookie_token="valid", auth_valid=True)
        _ws_clients.clear()

        ws_mock = MagicMock()
        ws_mock.prepare = AsyncMock()
        ws_mock.__aiter__ = lambda self: _empty_async_iter()

        with patch("aiohttp.web.WebSocketResponse", return_value=ws_mock):
            asyncio.run(websocket_metrics(request))

        # After disconnect, client should be removed
        assert ws_mock not in _ws_clients

    def test_heartbeat_configured(self):
        """Authenticated WebSocket gets 60s heartbeat."""
        request = _make_ws_request(cookie_token="valid", auth_valid=True)
        _ws_clients.clear()

        ws_instances = []

        def capture_ws(*args, **kwargs):
            ws = MagicMock()
            ws.prepare = AsyncMock()
            ws.__aiter__ = lambda self: _empty_async_iter()
            ws_instances.append(kwargs)
            return ws

        with patch("aiohttp.web.WebSocketResponse", side_effect=capture_ws):
            asyncio.run(websocket_metrics(request))

        # The authenticated WS (second call) should have heartbeat=60.0
        # First call is for auth failure/max clients, second for success
        assert any(kw.get("heartbeat") == 60.0 for kw in ws_instances)

        _ws_clients.clear()


# ── Broadcast lifecycle tests ──────────────────────────────────────


class TestBroadcastLifecycle:
    def test_start_creates_task(self):
        """_start_broadcast_task creates an asyncio task."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        app = MagicMock()
        app.__getitem__ = lambda self, key: MagicMock()

        async def run():
            wsh._broadcast_task = None
            await _start_broadcast_task(app)
            assert wsh._broadcast_task is not None
            wsh._broadcast_task.cancel()
            try:
                await wsh._broadcast_task
            except asyncio.CancelledError:
                pass

        asyncio.run(run())

    def test_stop_cancels_task(self):
        """_stop_broadcast_task cancels the running task."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        async def run():
            app = MagicMock()
            app.__getitem__ = lambda self, key: MagicMock()

            # Create a task first
            await _start_broadcast_task(app)
            assert wsh._broadcast_task is not None

            # Stop it
            await _stop_broadcast_task(app)
            assert wsh._broadcast_task is None

        asyncio.run(run())

    def test_stop_closes_all_clients(self):
        """_stop_broadcast_task closes all connected WebSocket clients."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        _ws_clients.clear()
        ws1 = MagicMock()
        ws1.close = AsyncMock()
        ws2 = MagicMock()
        ws2.close = AsyncMock()
        _ws_clients.add(ws1)
        _ws_clients.add(ws2)

        async def run():
            wsh._broadcast_task = None  # No task to cancel
            await _stop_broadcast_task(MagicMock())

        asyncio.run(run())

        ws1.close.assert_called_once()
        ws2.close.assert_called_once()
        assert len(_ws_clients) == 0

    def test_stop_noop_when_no_task(self):
        """_stop_broadcast_task is safe when no task exists."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        _ws_clients.clear()
        wsh._broadcast_task = None

        async def run():
            await _stop_broadcast_task(MagicMock())

        asyncio.run(run())  # Should not raise


# ── Broadcast data collection tests ────────────────────────────────


class TestBroadcastDataCollection:
    @staticmethod
    def _broadcast_plugin(snapshot, *, tier=1, keys=None):
        """Wrap a MagicMock so the broadcast registry sees it."""
        m = MagicMock()
        m.broadcast_tier = tier
        m.broadcast_keys = keys
        m.broadcast_snapshot = MagicMock(return_value=snapshot)
        m.get_status.return_value = {"active": True}
        return m

    def _make_plugin(self, plugins=None):
        """Create a mock plugin with configurable sub-plugins."""
        plugin = MagicMock()
        plugin.config = {"metrics_interval": 0.1}
        plugin.app.reticulum = MagicMock()
        plugin.app.reticulum.get_interface_stats.return_value = {"interfaces": []}
        plugin.app.plugins = plugins or {}
        plugin.app.internet_probe = None
        return plugin

    def test_broadcasts_to_clients(self):
        """Broadcast sends JSON messages to connected clients."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        plugin = self._make_plugin()
        _ws_clients.clear()

        ws = MagicMock()
        ws.send_str = AsyncMock()
        _ws_clients.add(ws)

        app = MagicMock()
        app.__getitem__ = lambda self, key: plugin

        messages_sent = []

        async def capture_send(msg):
            messages_sent.append(msg)
            # Cancel after first broadcast to stop the loop
            raise asyncio.CancelledError()

        ws.send_str = capture_send

        async def run():
            try:
                await wsh._broadcast_metrics(app)
            except asyncio.CancelledError:
                pass

        asyncio.run(run())

        assert len(messages_sent) == 1
        data = json.loads(messages_sent[0])
        assert data["type"] == "update"
        assert "data" in data
        assert "timestamp" in data
        assert "interfaces" in data["data"]
        assert "plugins" in data["data"]
        assert "mesh" in data["data"]

        _ws_clients.clear()

    def test_removes_dead_clients(self):
        """Dead clients are removed from the set after failed send."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        plugin = self._make_plugin()
        _ws_clients.clear()

        dead_ws = MagicMock()
        dead_ws.send_str = AsyncMock(side_effect=ConnectionResetError("gone"))

        live_ws = MagicMock()
        live_ws.send_str = AsyncMock()

        _ws_clients.add(dead_ws)
        _ws_clients.add(live_ws)

        app = MagicMock()
        app.__getitem__ = lambda self, key: plugin

        cycle = 0

        async def run():
            nonlocal cycle
            original_sleep = asyncio.sleep

            async def cancel_after_one(secs):
                nonlocal cycle
                cycle += 1
                if cycle >= 2:
                    raise asyncio.CancelledError()
                await original_sleep(0)

            with patch("asyncio.sleep", cancel_after_one):
                try:
                    await wsh._broadcast_metrics(app)
                except asyncio.CancelledError:
                    pass

        asyncio.run(run())

        assert dead_ws not in _ws_clients
        assert live_ws in _ws_clients
        _ws_clients.clear()

    def test_skips_broadcast_when_no_clients(self):
        """No client-facing work is done when _ws_clients is empty and load is high."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        plugin = self._make_plugin()
        _ws_clients.clear()

        app = MagicMock()
        app.__getitem__ = lambda self, key: plugin

        cycle_count = 0

        async def run():
            nonlocal cycle_count

            original_sleep = asyncio.sleep

            async def counted_sleep(secs):
                nonlocal cycle_count
                cycle_count += 1
                if cycle_count >= 3:
                    raise asyncio.CancelledError()
                await original_sleep(0)

            with (
                patch("asyncio.sleep", counted_sleep),
                patch.object(wsh, "_heartbeat_interval", return_value=None),
            ):
                try:
                    await wsh._broadcast_metrics(app)
                except asyncio.CancelledError:
                    pass

        asyncio.run(run())

        # Should have looped without calling get_plugin for monitor etc.
        # (the continue statement fires before plugin collection)
        assert cycle_count >= 2

    def test_includes_mesh_data(self):
        """Mesh data from network_map is included in broadcast."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        mesh_snapshot = {
            "known_nodes": 42,
            "recent_announces": [
                {"hash": "aa" * 16, "last_seen": time.time()},
            ],
        }
        network_map = self._broadcast_plugin(
            mesh_snapshot,
            tier=1,
            keys="mesh",
        )

        plugin = self._make_plugin({"network_map": network_map})
        _ws_clients.clear()

        ws = MagicMock()
        messages = []

        async def capture_and_stop(msg):
            messages.append(msg)
            raise asyncio.CancelledError()

        ws.send_str = capture_and_stop
        _ws_clients.add(ws)

        app = MagicMock()
        app.__getitem__ = lambda self, key: plugin

        async def run():
            # Reset mesh tracking state
            wsh._last_mesh_announce_ts = 0
            wsh._mesh_version = 0
            try:
                await wsh._broadcast_metrics(app)
            except asyncio.CancelledError:
                pass

        asyncio.run(run())

        data = json.loads(messages[0])
        assert "mesh" in data["data"]
        assert data["data"]["mesh"]["known_nodes"] == 42
        assert len(data["data"]["mesh"]["recent_announces"]) == 1

        _ws_clients.clear()

    def test_includes_sensor_data(self):
        """Sensor data is included when sensor_framework plugin is available."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        sensor_snapshot = {
            "temperature": {"value": 22.5, "unit": "C"},
        }
        sensor_fw = self._broadcast_plugin(
            sensor_snapshot,
            tier=2,
            keys="sensors",
        )

        plugin = self._make_plugin({"sensor_framework": sensor_fw})
        _ws_clients.clear()

        ws = MagicMock()
        messages = []

        async def capture_and_stop(msg):
            messages.append(msg)
            raise asyncio.CancelledError()

        ws.send_str = capture_and_stop
        _ws_clients.add(ws)

        app = MagicMock()
        app.__getitem__ = lambda self, key: plugin

        async def run():
            try:
                await wsh._broadcast_metrics(app)
            except asyncio.CancelledError:
                pass

        asyncio.run(run())

        data = json.loads(messages[0])
        assert data["data"]["sensors"]["temperature"]["value"] == 22.5

        _ws_clients.clear()

    def test_includes_transport_data_with_enrichment(self):
        """Transport data is enriched with interface traffic stats."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        transport_snapshot = {
            "primaries": [
                {"target_host": "hub.example.com", "target_port": 4242},
            ],
            "active_fallbacks": [],
            "auto_discovery": {"connected": []},
        }
        transport_mon = self._broadcast_plugin(
            transport_snapshot,
            tier=0,
            keys="transport",
        )

        plugin = self._make_plugin({"transport_monitor": transport_mon})
        # Set up interface stats to have matching TCP client
        plugin.app.reticulum.get_interface_stats.return_value = {
            "interfaces": [
                {
                    "name": "TCPInterface[TCP Client hub/hub.example.com:4242]",
                    "type": "TCPClientInterface",
                    "status": True,
                    "rxb": 5000,
                    "txb": 6000,
                },
            ]
        }

        _ws_clients.clear()
        ws = MagicMock()
        messages = []

        async def capture_and_stop(msg):
            messages.append(msg)
            raise asyncio.CancelledError()

        ws.send_str = capture_and_stop
        _ws_clients.add(ws)

        app = MagicMock()
        app.__getitem__ = lambda self, key: plugin

        async def run():
            with patch(
                "reticulumpi.builtin_plugins.web_dashboard.websocket_handler._parse_rnode_config",
                return_value={},
            ):
                try:
                    await wsh._broadcast_metrics(app)
                except asyncio.CancelledError:
                    pass

        asyncio.run(run())

        data = json.loads(messages[0])
        primary = data["data"]["transport"]["primaries"][0]
        assert primary["rxb"] == 5000
        assert primary["txb"] == 6000

        _ws_clients.clear()

    def test_includes_messaging_data(self):
        """Slow-moving messaging state (transports, unread) rides the tick."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        messaging_snapshot = {
            "transports": ["lxmf"],
            "unread": {"lxmf": {"abc": 2}},
        }
        msg_hub = self._broadcast_plugin(
            messaging_snapshot,
            tier=1,
            keys="messaging",
        )

        plugin = self._make_plugin({"messaging_hub": msg_hub})
        _ws_clients.clear()

        ws = MagicMock()
        messages = []

        async def capture_and_stop(msg):
            messages.append(msg)
            raise asyncio.CancelledError()

        ws.send_str = capture_and_stop
        _ws_clients.add(ws)

        app = MagicMock()
        app.__getitem__ = lambda self, key: plugin

        async def run():
            try:
                await wsh._broadcast_metrics(app)
            except asyncio.CancelledError:
                pass

        asyncio.run(run())

        data = json.loads(messages[0])
        assert "messaging" in data["data"]
        assert "messages" not in data["data"]["messaging"]
        assert data["data"]["messaging"]["transports"] == ["lxmf"]
        assert data["data"]["messaging"]["unread"] == {"lxmf": {"abc": 2}}

        _ws_clients.clear()

    def test_error_recovery(self):
        """Broadcast loop recovers from exceptions in data collection."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        plugin = self._make_plugin()
        _ws_clients.clear()

        ws = MagicMock()
        messages = []

        async def capture(msg):
            messages.append(msg)
            if len(messages) >= 1:
                raise asyncio.CancelledError()

        ws.send_str = capture
        _ws_clients.add(ws)

        app = MagicMock()
        app.__getitem__ = lambda self, key: plugin

        call_count = 0
        original_collect = wsh._collect_broadcast_data

        def failing_then_ok(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise RuntimeError("transient failure")
            return original_collect(*args, **kwargs)

        async def run():
            with patch.object(wsh, "_collect_broadcast_data", side_effect=failing_then_ok):
                try:
                    await wsh._broadcast_metrics(app)
                except asyncio.CancelledError:
                    pass

        asyncio.run(run())

        assert call_count >= 2
        assert len(messages) >= 1

        _ws_clients.clear()


# ── Route setup test ───────────────────────────────────────────────


class TestSetupWebsocketRoutes:
    def test_registers_routes_and_hooks(self):
        """setup_websocket_routes registers /ws/metrics and lifecycle hooks."""
        from reticulumpi.builtin_plugins.web_dashboard.websocket_handler import (
            setup_websocket_routes,
        )

        app = MagicMock()
        app.router.add_get = MagicMock()
        app.on_startup = []
        app.on_shutdown = []

        setup_websocket_routes(app)

        assert app.router.add_get.call_count == 2
        assert len(app.on_startup) == 1
        assert len(app.on_shutdown) == 1


# ── _lookup_message_row tests ──────────────────────────────────────


class TestLookupMessageRow:
    """Enrichment path: event-bus payload id → full hub row."""

    def test_returns_none_when_plugin_not_initialised(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        with patch.object(wsh, "_ws_plugin", None):
            assert _lookup_message_row(42) is None

    def test_returns_none_when_id_missing(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        plugin = MagicMock()
        with patch.object(wsh, "_ws_plugin", plugin):
            assert _lookup_message_row(None) is None

    def test_returns_none_when_hub_unavailable(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = None
        with patch.object(wsh, "_ws_plugin", plugin):
            assert _lookup_message_row(1) is None

    def test_returns_none_when_hub_lacks_get_message(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        plugin = MagicMock()
        hub = MagicMock(spec=[])  # no attributes at all
        plugin.app.get_plugin.return_value = hub
        with patch.object(wsh, "_ws_plugin", plugin):
            assert _lookup_message_row(1) is None

    def test_returns_none_on_get_plugin_exception(self):
        """Plugin lookup faults must not propagate to the event-bus caller."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        plugin = MagicMock()
        plugin.app.get_plugin.side_effect = RuntimeError("boom")
        with patch.object(wsh, "_ws_plugin", plugin):
            assert _lookup_message_row(1) is None

    def test_returns_none_on_get_message_exception(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        plugin = MagicMock()
        hub = MagicMock()
        hub.get_message.side_effect = RuntimeError("db down")
        plugin.app.get_plugin.return_value = hub
        with patch.object(wsh, "_ws_plugin", plugin):
            assert _lookup_message_row(1) is None

    def test_returns_row_on_success(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        plugin = MagicMock()
        hub = MagicMock()
        row = {
            "id": 7,
            "contact_id": "abc",
            "transport": "lxmf",
            "direction": "inbound",
            "text": "hi",
        }
        hub.get_message.return_value = row
        plugin.app.get_plugin.return_value = hub
        with patch.object(wsh, "_ws_plugin", plugin):
            assert _lookup_message_row(7) == row
        hub.get_message.assert_called_once_with(7)


# ── _on_message_event tests ────────────────────────────────────────


class TestOnMessageEvent:
    """Event-bus callback → scheduled WS push."""

    def test_noop_when_loop_missing(self):
        """Pre-startup events (no running loop) must not crash the publisher."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        with patch.object(wsh, "_ws_loop", None):
            _on_message_event("message_received", {"id": 1})  # no raise

    def test_noop_when_no_clients(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        loop = MagicMock()
        with patch.object(wsh, "_ws_loop", loop):
            # _ws_clients already cleared by the autouse fixture.
            _on_message_event("message_received", {"id": 1})
        loop.call_soon_threadsafe.assert_not_called()

    def test_schedules_enriched_push_for_message(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        loop = MagicMock()
        _ws_clients.add(MagicMock())
        with patch.object(wsh, "_ws_loop", loop):
            _on_message_event("message_received", {"id": 9, "transport": "lxmf"})
        loop.call_soon_threadsafe.assert_called_once()
        args = loop.call_soon_threadsafe.call_args.args
        assert args[0] is wsh._schedule_enriched_push
        assert args[1] == "message"
        assert args[2] == "message_received"
        assert args[3] == {"id": 9, "transport": "lxmf"}

    def test_schedules_enriched_push_for_sent_message(self):
        """Sent messages also use the enriched push path."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        loop = MagicMock()
        _ws_clients.add(MagicMock())
        with patch.object(wsh, "_ws_loop", loop):
            _on_message_event("message_sent", {"id": 9, "transport": "lxmf"})
        args = loop.call_soon_threadsafe.call_args.args
        assert args[0] is wsh._schedule_enriched_push
        assert args[1] == "message"
        assert args[2] == "message_sent"
        assert args[3] == {"id": 9, "transport": "lxmf"}

    def test_swallows_runtime_error_from_closed_loop(self):
        """Shutdown race — loop closed between the check and the submission."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        loop = MagicMock()
        loop.call_soon_threadsafe.side_effect = RuntimeError("loop closed")
        _ws_clients.add(MagicMock())
        with patch.object(wsh, "_ws_loop", loop):
            _on_message_event("message_received", {"id": 1})  # no raise


# ── _on_status_event tests ─────────────────────────────────────────


class TestOnStatusEvent:
    """Status-change callback → minimal WS payload, enriched if row known."""

    def test_noop_when_loop_missing(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        with patch.object(wsh, "_ws_loop", None):
            _on_status_event("message_status_changed", {"id": 1, "status": "sent"})

    def test_noop_when_no_clients(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        loop = MagicMock()
        with patch.object(wsh, "_ws_loop", loop):
            _on_status_event("message_status_changed", {"id": 1, "status": "sent"})
        loop.call_soon_threadsafe.assert_not_called()

    def test_schedules_enriched_push_for_status(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        loop = MagicMock()
        _ws_clients.add(MagicMock())
        data = {
            "id": 5,
            "status": "delivered",
            "timestamp": 1234.5,
            "transport": "meshtastic",
        }
        with patch.object(wsh, "_ws_loop", loop):
            _on_status_event("message_status_changed", data)
        args = loop.call_soon_threadsafe.call_args.args
        assert args[0] is wsh._schedule_enriched_push
        assert args[1] == "message_status"
        assert args[2] == "message_status_changed"
        assert args[3] == data

    def test_schedules_enriched_push_for_failed_status(self):
        """Failed statuses also use the enriched push path."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        loop = MagicMock()
        _ws_clients.add(MagicMock())
        data = {"id": 3, "status": "failed"}
        with patch.object(wsh, "_ws_loop", loop):
            _on_status_event("message_status_changed", data)
        args = loop.call_soon_threadsafe.call_args.args
        assert args[0] is wsh._schedule_enriched_push
        assert args[1] == "message_status"
        assert args[2] == "message_status_changed"
        assert args[3] == data

    def test_swallows_runtime_error_from_closed_loop(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        loop = MagicMock()
        loop.call_soon_threadsafe.side_effect = RuntimeError("loop closed")
        _ws_clients.add(MagicMock())
        with patch.object(wsh, "_ws_loop", loop):
            _on_status_event("message_status_changed", {"id": 1, "status": "x"})


# ── _on_alert_event tests ──────────────────────────────────────────


class TestOnAlertEvent:
    """ALERT_TRIGGERED callback → scheduled WS push."""

    def test_noop_when_loop_missing(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        with patch.object(wsh, "_ws_loop", None):
            _on_alert_event("alert.triggered", {"message": "CPU high"})

    def test_noop_when_no_clients(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        loop = MagicMock()
        with patch.object(wsh, "_ws_loop", loop):
            _on_alert_event("alert.triggered", {"message": "CPU high"})
        loop.call_soon_threadsafe.assert_not_called()

    def test_schedules_push_with_alert_type(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        loop = MagicMock()
        _ws_clients.add(MagicMock())
        data = {"message": "CPU > 90%", "rule_key": "rule:cpu:>:90", "time": 12345.0}
        with patch.object(wsh, "_ws_loop", loop):
            _on_alert_event("alert.triggered", data)
        loop.call_soon_threadsafe.assert_called_once()
        args = loop.call_soon_threadsafe.call_args.args
        assert args[0] is wsh._schedule_push
        assert args[1] == "alert"
        assert args[2] == data

    def test_swallows_runtime_error_on_closed_loop(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        loop = MagicMock()
        loop.call_soon_threadsafe.side_effect = RuntimeError("loop closed")
        _ws_clients.add(MagicMock())
        with patch.object(wsh, "_ws_loop", loop):
            _on_alert_event("alert.triggered", {"message": "test"})


# ── _push_to_clients tests ─────────────────────────────────────────


class TestPushToClients:
    """Async fan-out: every client gets the same envelope; bad clients are dropped."""

    def test_noop_when_no_clients(self):
        async def run():
            await _push_to_clients("message", {"id": 1})

        asyncio.run(run())  # completes without error

    def test_broadcasts_to_all_clients(self):
        ws1 = MagicMock()
        ws1.send_str = AsyncMock()
        ws2 = MagicMock()
        ws2.send_str = AsyncMock()
        _ws_clients.add(ws1)
        _ws_clients.add(ws2)

        async def run():
            await _push_to_clients("message", {"id": 1, "text": "hi"})

        asyncio.run(run())

        ws1.send_str.assert_awaited_once()
        ws2.send_str.assert_awaited_once()
        # Envelope shape.
        payload = json.loads(ws1.send_str.call_args.args[0])
        assert payload["type"] == "message"
        assert payload["data"] == {"id": 1, "text": "hi"}
        assert "timestamp" in payload

    def test_drops_failed_clients_but_keeps_healthy_ones(self):
        good = MagicMock()
        good.send_str = AsyncMock()
        bad = MagicMock()
        bad.send_str = AsyncMock(side_effect=ConnectionResetError("peer gone"))
        _ws_clients.add(good)
        _ws_clients.add(bad)

        async def run():
            await _push_to_clients("message_status", {"id": 1, "status": "sent"})

        asyncio.run(run())

        assert good in _ws_clients
        assert bad not in _ws_clients

    def test_concurrent_send_isolates_slow_client(self):
        """A single slow peer must not serialize delivery to other clients."""
        order = []

        async def slow(_msg):
            await asyncio.sleep(0.05)
            order.append("slow")

        async def fast(_msg):
            order.append("fast")

        ws_slow = MagicMock()
        ws_slow.send_str = slow
        ws_fast = MagicMock()
        ws_fast.send_str = fast
        _ws_clients.add(ws_slow)
        _ws_clients.add(ws_fast)

        async def run():
            await _push_to_clients("message", {"id": 1})

        asyncio.run(run())

        # With gather, the fast send completes before the slow one wakes up.
        assert order == ["fast", "slow"]

    def test_json_dumps_runs_in_executor(self):
        """json.dumps must run via run_in_executor, not on the event loop."""
        ws = MagicMock()
        ws.send_str = AsyncMock()
        _ws_clients.add(ws)

        # Set a sentinel so the executor-identity assertion is non-vacuous.
        sentinel_executor = object()
        original_executor = wsh_module._broadcast_executor

        executor_calls = []

        async def fake_run_in_executor(executor, fn, *args):
            executor_calls.append((executor, fn))
            return fn(*args) if args else fn()

        async def run():
            loop = asyncio.get_running_loop()
            original_run = loop.run_in_executor
            loop.run_in_executor = fake_run_in_executor
            try:
                await _push_to_clients("message", {"id": 1})
            finally:
                loop.run_in_executor = original_run

        wsh_module._broadcast_executor = sentinel_executor
        try:
            asyncio.run(run())
        finally:
            wsh_module._broadcast_executor = original_executor

        # run_in_executor was called exactly once (for json.dumps).
        assert len(executor_calls) == 1
        executor_arg, fn_arg = executor_calls[0]
        # The executor passed should be _broadcast_executor.
        assert executor_arg is sentinel_executor
        # The callable should be a functools.partial wrapping json.dumps.
        assert isinstance(fn_arg, functools.partial)
        assert fn_arg.func is json.dumps


# ── _enrich_and_push tests ────────────────────────────────────────


class TestEnrichAndPush:
    def test_message_enriched_with_row(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        row = {"id": 9, "contact_id": "peer", "text": "hi"}
        ws = MagicMock()
        ws.send_str = AsyncMock()
        _ws_clients.add(ws)

        async def run():
            with (
                patch.object(wsh, "_lookup_message_row", return_value=row),
                patch.object(wsh, "_push_sem", asyncio.Semaphore(8)),
            ):
                await wsh._enrich_and_push(
                    "message", "message_received", {"id": 9, "transport": "lxmf"}
                )

        asyncio.run(run())
        sent = json.loads(ws.send_str.call_args.args[0])
        assert sent["type"] == "message"
        assert sent["data"]["contact_id"] == "peer"

    def test_message_fallback_when_row_missing(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        ws = MagicMock()
        ws.send_str = AsyncMock()
        _ws_clients.add(ws)

        async def run():
            with (
                patch.object(wsh, "_lookup_message_row", return_value=None),
                patch.object(wsh, "_push_sem", asyncio.Semaphore(8)),
            ):
                await wsh._enrich_and_push(
                    "message", "message_sent", {"id": 9, "transport": "lxmf"}
                )

        asyncio.run(run())
        sent = json.loads(ws.send_str.call_args.args[0])
        assert sent["data"]["event"] == "message_sent"
        assert sent["data"]["id"] == 9

    def test_status_enriched_with_contact_id(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        row = {"id": 5, "contact_id": "peer-1", "sub_transport": "mqtt"}
        ws = MagicMock()
        ws.send_str = AsyncMock()
        _ws_clients.add(ws)

        async def run():
            with (
                patch.object(wsh, "_lookup_message_row", return_value=row),
                patch.object(wsh, "_push_sem", asyncio.Semaphore(8)),
            ):
                await wsh._enrich_and_push(
                    "message_status",
                    "message_status_changed",
                    {
                        "id": 5,
                        "status": "delivered",
                        "timestamp": 1234.5,
                        "transport": "meshtastic",
                    },
                )

        asyncio.run(run())
        sent = json.loads(ws.send_str.call_args.args[0])
        assert sent["data"]["contact_id"] == "peer-1"
        assert sent["data"]["sub_transport"] == "mqtt"

    def test_status_fallback_without_row(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        ws = MagicMock()
        ws.send_str = AsyncMock()
        _ws_clients.add(ws)

        async def run():
            with (
                patch.object(wsh, "_lookup_message_row", return_value=None),
                patch.object(wsh, "_push_sem", asyncio.Semaphore(8)),
            ):
                await wsh._enrich_and_push(
                    "message_status",
                    "message_status_changed",
                    {"id": 3, "status": "failed", "contact_id": "peer-2", "sub_transport": "irc"},
                )

        asyncio.run(run())
        sent = json.loads(ws.send_str.call_args.args[0])
        assert sent["data"]["contact_id"] == "peer-2"
        assert sent["data"]["sub_transport"] == "irc"

    def test_lookup_exception_uses_fallback(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        ws = MagicMock()
        ws.send_str = AsyncMock()
        _ws_clients.add(ws)

        async def run():
            with (
                patch.object(wsh, "_lookup_message_row", side_effect=RuntimeError("db locked")),
                patch.object(wsh, "_push_sem", asyncio.Semaphore(8)),
            ):
                await wsh._enrich_and_push("message", "message_received", {"id": 1})

        asyncio.run(run())
        sent = json.loads(ws.send_str.call_args.args[0])
        assert sent["data"]["event"] == "message_received"


# ── _send_with_timeout tests ──────────────────────────────────────


class TestSendWithTimeout:
    def test_succeeds_within_timeout(self):
        ws = MagicMock()
        ws.send_str = AsyncMock()

        async def run():
            result = await _send_with_timeout(ws, "hello", timeout=1.0)
            assert result is True
            ws.send_str.assert_called_once_with("hello")

        asyncio.run(run())

    def test_returns_false_on_timeout(self):
        async def stall(_msg):
            await asyncio.sleep(10)

        ws = MagicMock()
        ws.send_str = stall

        async def run():
            result = await _send_with_timeout(ws, "hello", timeout=0.05)
            assert result is False

        asyncio.run(run())

    def test_returns_false_on_connection_error(self):
        ws = MagicMock()
        ws.send_str = AsyncMock(side_effect=ConnectionResetError("gone"))

        async def run():
            result = await _send_with_timeout(ws, "hello", timeout=1.0)
            assert result is False

        asyncio.run(run())

    def test_returns_false_on_cancelled(self):
        ws = MagicMock()
        ws.send_str = AsyncMock(side_effect=asyncio.CancelledError())

        async def run():
            try:
                result = await _send_with_timeout(ws, "hello", timeout=1.0)
                assert result is False
            except asyncio.CancelledError:
                pass  # CancelledError may propagate through wait_for

        asyncio.run(run())


# ── _collect_broadcast_data tests ─────────────────────────────────


class TestCollectBroadcastDataDirect:
    """Direct synchronous tests for the extracted data collection function."""

    def _make_mock_plugin(self, broadcast_plugins=None, interval=5):
        plugin = MagicMock()
        plugin.config.get.side_effect = lambda key, default=None: {
            "metrics_interval": interval,
        }.get(key, default)
        plugin.app.reticulum = MagicMock()
        plugin.app.reticulum.get_interface_stats.return_value = {"interfaces": []}
        plugin.app.plugins = broadcast_plugins or {}
        return plugin

    @staticmethod
    def _mock_broadcast_plugin(tier, keys, snapshot_return):
        p = MagicMock()
        p.broadcast_tier = tier
        p.broadcast_keys = keys
        p.broadcast_snapshot.return_value = snapshot_return
        p.get_status.return_value = {"active": True}
        return p

    def test_returns_base_keys(self):
        plugin = self._make_mock_plugin()
        data, ts, ver = _collect_broadcast_data(plugin, 1, 0, 0)
        assert "plugins" in data
        assert "interfaces" in data
        assert "mesh" in data

    def test_mesh_version_bump_on_new_announces(self):
        network_map = self._mock_broadcast_plugin(
            1,
            "mesh",
            {"node_count": 10, "recent_announces": [{"last_seen": 100.0}]},
        )
        plugin = self._make_mock_plugin({"network_map": network_map})
        data, new_ts, new_ver = _collect_broadcast_data(plugin, 1, 0, 0)
        assert new_ts == 100.0
        assert new_ver == 1
        assert data["mesh"]["recent_announces"][0]["last_seen"] == 100.0

    def test_mesh_version_stable_on_stale_announces(self):
        network_map = self._mock_broadcast_plugin(
            1,
            "mesh",
            {"node_count": 5, "recent_announces": [{"last_seen": 50.0}]},
        )
        plugin = self._make_mock_plugin({"network_map": network_map})
        data, new_ts, new_ver = _collect_broadcast_data(plugin, 1, 50.0, 3)
        assert new_ts == 50.0
        assert new_ver == 3

    def test_mesh_peers_merged(self):
        network_map = self._mock_broadcast_plugin(1, "mesh", {"node_count": 2})
        mesh_tel = self._mock_broadcast_plugin(1, "mesh_peers", [{"dest": "aa"}])
        plugin = self._make_mock_plugin(
            {
                "network_map": network_map,
                "mesh_telemetry": mesh_tel,
            }
        )
        data, _, _ = _collect_broadcast_data(plugin, 1, 0, 0)
        assert data["mesh"]["peers"] == [{"dest": "aa"}]
        assert data["mesh"]["peer_count"] == 1
        assert "mesh_peers" not in data

    def test_alerts_merged_into_mesh(self):
        alert_sys = self._mock_broadcast_plugin(
            1,
            "alerts",
            {"alerts_sent": 3, "last_alert": "fire"},
        )
        plugin = self._make_mock_plugin({"alert_system": alert_sys})
        data, _, _ = _collect_broadcast_data(plugin, 1, 0, 0)
        assert data["mesh"]["alerts_sent"] == 3
        assert "alerts" not in data


# ── Budget / timeout tests ───────────────────────────────────────


class TestCollectBroadcastBudget:
    """Test that _collect_broadcast_data respects the time budget."""

    def setup_method(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        wsh._broadcast_registry = None

    @staticmethod
    def _mock_broadcast_plugin(tier, keys, snapshot_return):
        p = MagicMock()
        p.broadcast_tier = tier
        p.broadcast_keys = keys
        p.broadcast_snapshot.return_value = snapshot_return
        p.get_status.return_value = {"active": True}
        return p

    def _make_mock_plugin(self, broadcast_plugins=None, interval=5):
        plugin = MagicMock()
        plugin.config.get.side_effect = lambda key, default=None: {
            "metrics_interval": interval,
        }.get(key, default)
        plugin.app.reticulum = MagicMock()
        plugin.app.reticulum.get_interface_stats.return_value = {"interfaces": []}
        plugin.app.plugins = broadcast_plugins or {}
        return plugin

    def test_skips_expensive_plugins_when_budget_exceeded(self):
        """When the budget is exhausted, expensive-tier plugins are skipped."""
        space = self._mock_broadcast_plugin(2, "space", {"tle_groups": {}})
        spectrum = self._mock_broadcast_plugin(2, "spectrum", {"power": [1, 2, 3]})

        plugin = self._make_mock_plugin(
            {"space_tracker": space, "spectrum_scanner": spectrum},
            interval=0,
        )
        data, _, _ = _collect_broadcast_data(plugin, 1, 0, 0)

        assert "plugins" in data
        assert "interfaces" in data
        assert "space" not in data
        assert "spectrum" not in data
        space.broadcast_snapshot.assert_not_called()
        spectrum.broadcast_snapshot.assert_not_called()

    def test_collects_all_plugins_within_budget(self):
        """With a generous budget, all plugins are collected."""
        space = self._mock_broadcast_plugin(
            2,
            "space",
            {
                "tle_groups": {"amateur": 5},
                "positions": {"objects": [{"name": "ISS", "el": 45}]},
                "observer": {"lat": 30, "lon": -85},
            },
        )

        plugin = self._make_mock_plugin(
            {"space_tracker": space},
            interval=60,
        )
        data, _, _ = _collect_broadcast_data(plugin, 1, 0, 0)
        assert "space" in data
        space.broadcast_snapshot.assert_called_once()

    def test_slow_plugin_logged(self):
        """A plugin exceeding the slow threshold triggers a warning."""
        from reticulumpi.builtin_plugins.web_dashboard import broadcast_registry as br

        conn_mon = MagicMock()
        conn_mon.broadcast_tier = 0
        conn_mon.broadcast_keys = "connectivity"
        conn_mon.get_status.return_value = {"active": True}

        def slow_snapshot(**kwargs):
            time.sleep(0.25)
            return {"status": "ok"}

        conn_mon.broadcast_snapshot = slow_snapshot

        plugin = self._make_mock_plugin(
            {"connectivity_monitor": conn_mon},
            interval=5,
        )

        with patch.object(br.log, "warning") as mock_warn:
            _collect_broadcast_data(plugin, 1, 0, 0)
            assert mock_warn.called
            call_args = mock_warn.call_args[0]
            assert "connectivity_monitor" in call_args[1]


# ── Overlap guard tests ──────────────────────────────────────────


class TestBroadcastOverlapGuard:
    """Test that the overlap guard in _broadcast_metrics skips cycles."""

    @pytest.mark.asyncio
    async def test_skips_when_collection_running(self):
        """If _collection_running is set, the broadcast cycle is skipped."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        # Simulate a long-running collection by setting the event
        wsh._collection_running.set()
        try:
            plugin = MagicMock()
            plugin.config.get.return_value = 0.01  # tiny interval

            app = MagicMock()
            app.__getitem__ = lambda self, key: plugin if key == "plugin" else None

            ws_mock = AsyncMock()
            wsh._ws_clients.add(ws_mock)

            task = asyncio.create_task(_broadcast_metrics(app))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            # No data should have been sent because collection was "running"
            ws_mock.send_str.assert_not_called()
        finally:
            wsh._collection_running.clear()
            wsh._ws_clients.discard(ws_mock)


class TestHandleWsCommandGuards:
    def test_none_action_returns_none(self):
        plugin = MagicMock()
        result = _handle_ws_command({"no_action": True}, plugin)
        assert result is None

    def test_empty_action_returns_none(self):
        plugin = MagicMock()
        result = _handle_ws_command({"action": ""}, plugin)
        assert result is None

    def test_missing_action_via_raw_string(self):
        plugin = MagicMock()
        result = _handle_ws_command('{"data": "x"}', plugin)
        assert result is None


class TestHandleWsSpectrumPreset:
    def _make_plugin(self, scanner=None):
        plugin = MagicMock()
        plugin.app.plugins.get.return_value = scanner
        return plugin

    def test_success_returns_switched_type(self):
        scanner = MagicMock()
        scanner.switch_preset.return_value = {
            "preset": "aviation",
            "freq_start_mhz": 108.0,
            "freq_stop_mhz": 137.0,
            "has_analysis": False,
        }
        plugin = self._make_plugin(scanner)
        raw = json.dumps({"action": "spectrum_switch_preset", "preset": "aviation"})
        result = _handle_ws_command(raw, plugin)
        assert result["type"] == "spectrum_preset_switched"
        assert result["preset"] == "aviation"

    def test_value_error_returns_preset_error(self):
        scanner = MagicMock()
        scanner.switch_preset.side_effect = ValueError("Unknown preset")
        plugin = self._make_plugin(scanner)
        raw = json.dumps({"action": "spectrum_switch_preset", "preset": "bad"})
        result = _handle_ws_command(raw, plugin)
        assert result["type"] == "spectrum_preset_error"
        assert "Unknown preset" in result["error"]

    def test_generic_exception_returns_preset_error(self):
        scanner = MagicMock()
        scanner.switch_preset.side_effect = RuntimeError("boom")
        plugin = self._make_plugin(scanner)
        raw = json.dumps({"action": "spectrum_switch_preset", "preset": "x"})
        result = _handle_ws_command(raw, plugin)
        assert result is not None
        assert result["type"] == "spectrum_preset_error"
        assert "boom" in result["error"]


class TestHandleWsPingPong:
    def test_ping_returns_pong_with_timestamp(self):
        plugin = MagicMock()
        raw = json.dumps({"action": "ping", "ts": 1700000000000})
        result = _handle_ws_command(raw, plugin)
        assert result == {"type": "pong", "ts": 1700000000000}

    def test_ping_without_ts_defaults_to_zero(self):
        plugin = MagicMock()
        raw = json.dumps({"action": "ping"})
        result = _handle_ws_command(raw, plugin)
        assert result == {"type": "pong", "ts": 0}

    def test_ping_does_not_touch_plugin(self):
        plugin = MagicMock()
        _handle_ws_command(json.dumps({"action": "ping", "ts": 42}), plugin)
        plugin.app.plugins.get.assert_not_called()


class TestWebSocketCompression:
    def test_compress_enabled_by_default(self):
        request = _make_ws_request(cookie_token="valid", auth_valid=True)
        _ws_clients.clear()

        ws_kwargs = []

        def capture_ws(*args, **kwargs):
            ws = MagicMock()
            ws.prepare = AsyncMock()
            ws.__aiter__ = lambda self: _empty_async_iter()
            ws_kwargs.append(kwargs)
            return ws

        with patch("aiohttp.web.WebSocketResponse", side_effect=capture_ws):
            asyncio.run(websocket_metrics(request))

        assert any(kw.get("compress") == 15 for kw in ws_kwargs)
        _ws_clients.clear()

    def test_compress_disabled_via_config(self):
        request = _make_ws_request(cookie_token="valid", auth_valid=True)
        request.app["ws_compress"] = False
        _ws_clients.clear()

        ws_kwargs = []

        def capture_ws(*args, **kwargs):
            ws = MagicMock()
            ws.prepare = AsyncMock()
            ws.__aiter__ = lambda self: _empty_async_iter()
            ws_kwargs.append(kwargs)
            return ws

        with patch("aiohttp.web.WebSocketResponse", side_effect=capture_ws):
            asyncio.run(websocket_metrics(request))

        assert any(kw.get("compress") == 0 for kw in ws_kwargs)
        _ws_clients.clear()


class TestPrevPayloadBytes:
    def _make_plugin(self):
        plugin = MagicMock()
        plugin.config = {"metrics_interval": 0.05}
        plugin.app.reticulum = MagicMock()
        plugin.app.reticulum.get_interface_stats.return_value = {"interfaces": []}
        plugin.app.plugins = {}
        plugin.app.internet_probe = None
        return plugin

    def test_first_cycle_zero_second_cycle_has_previous_length(self):
        """prev_payload_bytes is 0 on first cycle, then reflects prior cycle's size."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        plugin = self._make_plugin()
        _ws_clients.clear()

        ws = MagicMock()
        messages = []

        async def capture_send(msg):
            messages.append(msg)
            if len(messages) >= 2:
                raise asyncio.CancelledError()

        ws.send_str = capture_send
        _ws_clients.add(ws)

        app = MagicMock()
        app.__getitem__ = lambda self, key: plugin

        async def run():
            try:
                await wsh._broadcast_metrics(app)
            except asyncio.CancelledError:
                pass

        asyncio.run(run())

        assert len(messages) >= 2

        first = json.loads(messages[0])
        assert first["data"]["ws_stats"]["prev_payload_bytes"] == 0

        second = json.loads(messages[1])
        assert second["data"]["ws_stats"]["prev_payload_bytes"] == len(messages[0])

        _ws_clients.clear()


class TestNoBareWsSendStr:
    """Guard against bare ws.send_str() calls that can block the event loop."""

    def test_send_str_only_inside_send_with_timeout(self):
        source = Path(wsh_module.__file__).read_text()
        violations = []
        for i, line in enumerate(source.splitlines(), 1):
            if ".send_str(" not in line:
                continue
            if "wait_for(" in line:
                continue
            violations.append(f"  line {i}: {line.strip()}")
        assert not violations, (
            "Bare ws.send_str() found — use _send_with_timeout() instead:\n" + "\n".join(violations)
        )


# ── Warm Cache Heartbeat tests ────────────────────────────────────


class TestHeartbeatInterval:
    """Unit tests for _heartbeat_interval() adaptive load scaling."""

    def test_idle_returns_min_interval(self):
        with patch("os.getloadavg", return_value=(0.5, 0.3, 0.2)):
            assert _heartbeat_interval() == 30.0

    def test_moderate_load_interpolates(self):
        with patch("os.getloadavg", return_value=(2.2, 1.0, 0.5)):
            result = _heartbeat_interval()
            assert 74.0 < result < 76.0

    def test_high_load_returns_none(self):
        with patch("os.getloadavg", return_value=(4.0, 3.5, 3.0)):
            assert _heartbeat_interval() is None

    def test_boundary_low(self):
        with patch("os.getloadavg", return_value=(1.2, 1.0, 0.8)):
            assert _heartbeat_interval() == 30.0

    def test_boundary_high(self):
        with patch("os.getloadavg", return_value=(3.2, 3.0, 2.8)):
            assert _heartbeat_interval() is None

    def test_os_error_fallback(self):
        with patch("os.getloadavg", side_effect=OSError("not available")):
            assert _heartbeat_interval() == 30.0


class TestWarmCacheHeartbeat:
    """Test warm-cache heartbeat in the broadcast loop idle path."""

    @staticmethod
    def _make_plugin():
        plugin = MagicMock()
        plugin.config = {"metrics_interval": 0.05}
        plugin.app.reticulum = MagicMock()
        plugin.app.reticulum.get_interface_stats.return_value = {"interfaces": []}
        plugin.app.plugins = {}
        plugin.app.internet_probe = None
        return plugin

    def test_heartbeat_populates_warm_cache_when_idle(self):
        """No clients + interval elapsed -> cache populated."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        plugin = self._make_plugin()
        _ws_clients.clear()
        app = MagicMock()
        app.__getitem__ = lambda self, key: plugin
        wsh._last_heartbeat_ts = 0.0

        cycle_count = 0

        async def run():
            nonlocal cycle_count
            original_sleep = asyncio.sleep

            async def counted_sleep(secs):
                nonlocal cycle_count
                cycle_count += 1
                if cycle_count >= 2:
                    raise asyncio.CancelledError()
                await original_sleep(0)

            with (
                patch("asyncio.sleep", counted_sleep),
                patch.object(wsh, "_heartbeat_interval", return_value=0.0),
            ):
                try:
                    await wsh._broadcast_metrics(app)
                except asyncio.CancelledError:
                    pass

        asyncio.run(run())

        assert wsh._warm_cache_data
        assert "plugins" in wsh._warm_cache_data
        assert wsh._warm_cache_ts > 0.0
        assert wsh._hb_count == 1

    def test_heartbeat_failure_increments_hb_fail(self):
        """Collection exception -> _hb_fail incremented."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        plugin = self._make_plugin()
        _ws_clients.clear()
        app = MagicMock()
        app.__getitem__ = lambda self, key: plugin
        wsh._last_heartbeat_ts = 0.0

        cycle_count = 0

        async def run():
            nonlocal cycle_count
            original_sleep = asyncio.sleep

            async def counted_sleep(secs):
                nonlocal cycle_count
                cycle_count += 1
                if cycle_count >= 2:
                    raise asyncio.CancelledError()
                await original_sleep(0)

            def boom(*a, **kw):
                raise RuntimeError("boom")

            with (
                patch("asyncio.sleep", counted_sleep),
                patch.object(wsh, "_heartbeat_interval", return_value=0.0),
                patch.object(wsh, "_collect_broadcast_data", side_effect=boom),
            ):
                try:
                    await wsh._broadcast_metrics(app)
                except asyncio.CancelledError:
                    pass

        asyncio.run(run())

        assert wsh._hb_count == 0
        assert wsh._hb_fail == 1

    def test_heartbeat_skipped_under_high_load(self):
        """_heartbeat_interval returns None -> no collection."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        plugin = self._make_plugin()
        _ws_clients.clear()
        app = MagicMock()
        app.__getitem__ = lambda self, key: plugin
        wsh._last_heartbeat_ts = 0.0

        cycle_count = 0

        async def run():
            nonlocal cycle_count
            original_sleep = asyncio.sleep

            async def counted_sleep(secs):
                nonlocal cycle_count
                cycle_count += 1
                if cycle_count >= 3:
                    raise asyncio.CancelledError()
                await original_sleep(0)

            with (
                patch("asyncio.sleep", counted_sleep),
                patch.object(wsh, "_heartbeat_interval", return_value=None),
            ):
                try:
                    await wsh._broadcast_metrics(app)
                except asyncio.CancelledError:
                    pass

        asyncio.run(run())

        assert wsh._warm_cache_data == {}
        assert wsh._warm_cache_ts == 0.0

    def test_heartbeat_respects_interval_spacing(self):
        """Recent _last_heartbeat_ts -> no collection."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        plugin = self._make_plugin()
        _ws_clients.clear()
        app = MagicMock()
        app.__getitem__ = lambda self, key: plugin
        wsh._last_heartbeat_ts = time.monotonic()

        cycle_count = 0

        async def run():
            nonlocal cycle_count
            original_sleep = asyncio.sleep

            async def counted_sleep(secs):
                nonlocal cycle_count
                cycle_count += 1
                if cycle_count >= 3:
                    raise asyncio.CancelledError()
                await original_sleep(0)

            with (
                patch("asyncio.sleep", counted_sleep),
                patch.object(wsh, "_heartbeat_interval", return_value=30.0),
            ):
                try:
                    await wsh._broadcast_metrics(app)
                except asyncio.CancelledError:
                    pass

        asyncio.run(run())

        assert wsh._warm_cache_data == {}


class TestWarmCacheServing:
    """Test warm cache is served to connecting clients."""

    def test_initial_connect_uses_warm_cache(self):
        """Fresh warm cache -> _collect_broadcast_data NOT called."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        wsh._warm_cache_data = {
            "plugins": {"test": {"active": True}},
            "interfaces": [],
            "mesh": {"version": 1},
        }
        wsh._warm_cache_ts = time.monotonic()

        sent = []

        async def capture_send(ws, msg):
            sent.append(msg)
            return True

        request = _make_ws_request(cookie_token="valid", auth_valid=True)
        _ws_clients.clear()

        ws_mock = MagicMock()
        ws_mock.prepare = AsyncMock()
        ws_mock.__aiter__ = lambda self: _empty_async_iter()

        async def run():
            with (
                patch("aiohttp.web.WebSocketResponse", return_value=ws_mock),
                patch.object(wsh, "_send_with_timeout", side_effect=capture_send),
                patch.object(wsh, "_collect_broadcast_data") as mock_collect,
            ):
                await wsh.websocket_metrics(request)
                mock_collect.assert_not_called()

        asyncio.run(run())

        assert len(sent) >= 1
        data = json.loads(sent[0])
        assert data["type"] == "update"
        assert "plugins" in data["data"]
        assert wsh._cache_hits == 1

    def test_initial_connect_falls_back_when_cache_stale(self):
        """Cache >90s old -> cold collection path used."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        wsh._warm_cache_data = {"plugins": {}, "interfaces": []}
        wsh._warm_cache_ts = time.monotonic() - 100.0

        request = _make_ws_request(cookie_token="valid", auth_valid=True)
        _ws_clients.clear()

        ws_mock = MagicMock()
        ws_mock.prepare = AsyncMock()
        ws_mock.__aiter__ = lambda self: _empty_async_iter()

        async def run():
            with (
                patch("aiohttp.web.WebSocketResponse", return_value=ws_mock),
                patch.object(wsh, "_send_with_timeout", new_callable=AsyncMock, return_value=True),
            ):
                await wsh.websocket_metrics(request)

        asyncio.run(run())

    def test_initial_connect_falls_back_when_cache_empty(self):
        """Empty warm cache -> cold collection path used."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        wsh._warm_cache_data = {}
        wsh._warm_cache_ts = 0.0

        request = _make_ws_request(cookie_token="valid", auth_valid=True)
        _ws_clients.clear()

        ws_mock = MagicMock()
        ws_mock.prepare = AsyncMock()
        ws_mock.__aiter__ = lambda self: _empty_async_iter()

        async def run():
            with (
                patch("aiohttp.web.WebSocketResponse", return_value=ws_mock),
                patch.object(wsh, "_send_with_timeout", new_callable=AsyncMock, return_value=True),
            ):
                await wsh.websocket_metrics(request)

        asyncio.run(run())


class TestWarmCacheLifecycle:
    """Test warm cache seeding at startup and clearing at shutdown."""

    def test_startup_seeds_warm_cache(self):
        """_start_broadcast_task stores collection result in warm cache."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        plugin = MagicMock()
        plugin.config = MagicMock()
        plugin.config.get = MagicMock(return_value=5)
        plugin.app.reticulum = MagicMock()
        plugin.app.reticulum.get_interface_stats.return_value = {"interfaces": []}
        plugin.app.plugins = {}
        plugin.app.internet_probe = None
        plugin.app.event_bus = MagicMock()

        app = MagicMock()
        app.__getitem__ = lambda self, key: plugin
        app.on_startup = []
        app.on_shutdown = []

        async def run():
            await wsh._start_broadcast_task(app)
            assert wsh._warm_cache_data
            assert wsh._warm_cache_ts > 0.0
            assert wsh._last_heartbeat_ts > 0.0
            # Clean up
            if wsh._broadcast_executor:
                wsh._broadcast_executor.shutdown(wait=False)
                wsh._broadcast_executor = None
            if wsh._command_executor:
                wsh._command_executor.shutdown(wait=False)
                wsh._command_executor = None
            if wsh._broadcast_task:
                wsh._broadcast_task.cancel()
                try:
                    await wsh._broadcast_task
                except (asyncio.CancelledError, Exception):
                    pass
                wsh._broadcast_task = None
            if hasattr(wsh, "_spectrum_task") and wsh._spectrum_task:
                wsh._spectrum_task.cancel()
                try:
                    await wsh._spectrum_task
                except (asyncio.CancelledError, Exception):
                    pass
                wsh._spectrum_task = None

        asyncio.run(run())

    def test_shutdown_clears_warm_cache(self):
        """_stop_broadcast_task resets warm cache globals."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        wsh._warm_cache_data = {"some": "data"}
        wsh._warm_cache_ts = 12345.0
        wsh._last_heartbeat_ts = 12345.0
        wsh._hb_count = 42
        wsh._hb_fail = 3
        wsh._cache_hits = 10
        wsh._last_hb_summary_ts = 99999.0
        wsh._broadcast_task = None
        wsh._spectrum_task = None
        wsh._ws_plugin = None
        wsh._broadcast_executor = None
        wsh._command_executor = None

        app = MagicMock()

        async def run():
            await wsh._stop_broadcast_task(app)

        asyncio.run(run())

        assert wsh._warm_cache_data == {}
        assert wsh._warm_cache_ts == 0.0
        assert wsh._last_heartbeat_ts == 0.0
        assert wsh._hb_count == 0
        assert wsh._hb_fail == 0
        assert wsh._cache_hits == 0
        assert wsh._last_hb_summary_ts == 0.0

    def test_broadcast_executor_created_with_two_workers(self):
        """_start_broadcast_task creates ThreadPoolExecutor with max_workers=2."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        plugin = MagicMock()
        plugin.config = MagicMock()
        plugin.config.get = MagicMock(return_value=5)
        plugin.app.reticulum = MagicMock()
        plugin.app.reticulum.get_interface_stats.return_value = {"interfaces": []}
        plugin.app.plugins = {}
        plugin.app.internet_probe = None
        plugin.app.event_bus = MagicMock()

        app = MagicMock()
        app.__getitem__ = lambda self, key: plugin
        app.on_startup = []
        app.on_shutdown = []

        async def run():
            await wsh._start_broadcast_task(app)
            assert wsh._broadcast_executor is not None
            assert wsh._broadcast_executor._max_workers == 2
            # Clean up
            if wsh._broadcast_executor:
                wsh._broadcast_executor.shutdown(wait=False)
                wsh._broadcast_executor = None
            if wsh._command_executor:
                wsh._command_executor.shutdown(wait=False)
                wsh._command_executor = None
            if wsh._broadcast_task:
                wsh._broadcast_task.cancel()
                try:
                    await wsh._broadcast_task
                except (asyncio.CancelledError, Exception):
                    pass
                wsh._broadcast_task = None
            if hasattr(wsh, "_spectrum_task") and wsh._spectrum_task:
                wsh._spectrum_task.cancel()
                try:
                    await wsh._spectrum_task
                except (asyncio.CancelledError, Exception):
                    pass
                wsh._spectrum_task = None

        asyncio.run(run())


class TestWarmCacheSummaryLog:
    """Test the periodic INFO-level warm cache summary."""

    @staticmethod
    def _make_plugin():
        plugin = MagicMock()
        plugin.config = {"metrics_interval": 0.05}
        plugin.app.reticulum = MagicMock()
        plugin.app.reticulum.get_interface_stats.return_value = {"interfaces": []}
        plugin.app.plugins = {}
        plugin.app.internet_probe = None
        return plugin

    def test_hourly_summary_emitted(self):
        """After _HB_SUMMARY_INTERVAL elapses, an INFO log is produced."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        plugin = self._make_plugin()
        _ws_clients.clear()
        app = MagicMock()
        app.__getitem__ = lambda self, key: plugin
        wsh._last_heartbeat_ts = 0.0
        wsh._last_hb_summary_ts = time.monotonic() - wsh._HB_SUMMARY_INTERVAL - 1

        cycle_count = 0

        async def run():
            nonlocal cycle_count
            original_sleep = asyncio.sleep

            async def counted_sleep(secs):
                nonlocal cycle_count
                cycle_count += 1
                if cycle_count >= 2:
                    raise asyncio.CancelledError()
                await original_sleep(0)

            with (
                patch("asyncio.sleep", counted_sleep),
                patch.object(wsh, "_heartbeat_interval", return_value=0.0),
                patch.object(wsh, "log") as mock_log,
            ):
                try:
                    await wsh._broadcast_metrics(app)
                except asyncio.CancelledError:
                    pass

                info_calls = [c for c in mock_log.info.call_args_list if "Warm cache:" in str(c)]
                assert len(info_calls) == 1
                msg = info_calls[0][0][0]
                assert "heartbeats" in msg
                assert "failures" in msg
                assert "cache hits" in msg

        asyncio.run(run())

    def test_summary_not_emitted_before_interval(self):
        """Within _HB_SUMMARY_INTERVAL, no summary log is produced."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        plugin = self._make_plugin()
        _ws_clients.clear()
        app = MagicMock()
        app.__getitem__ = lambda self, key: plugin
        wsh._last_heartbeat_ts = 0.0
        # Set recent summary timestamp so interval hasn't elapsed
        wsh._last_hb_summary_ts = time.monotonic()

        cycle_count = 0

        async def run():
            nonlocal cycle_count
            original_sleep = asyncio.sleep

            async def counted_sleep(secs):
                nonlocal cycle_count
                cycle_count += 1
                if cycle_count >= 2:
                    raise asyncio.CancelledError()
                await original_sleep(0)

            with (
                patch("asyncio.sleep", counted_sleep),
                patch.object(wsh, "_heartbeat_interval", return_value=0.0),
                patch.object(wsh, "log") as mock_log,
            ):
                try:
                    await wsh._broadcast_metrics(app)
                except asyncio.CancelledError:
                    pass

                info_calls = [c for c in mock_log.info.call_args_list if "Warm cache:" in str(c)]
                assert len(info_calls) == 0

        asyncio.run(run())


class TestOffgridWsCommand:
    def setup_method(self):
        from reticulumpi.builtin_plugins.web_dashboard.shared_state import offgrid_rate_limiter

        offgrid_rate_limiter._last_toggle = 0.0

    def test_set_offgrid_mode(self):
        plugin = MagicMock()
        plugin.app.set_offgrid_mode.return_value = {"enabled": True, "persisted": True}
        result = _handle_ws_command(
            {"action": "set_offgrid_mode", "enabled": True},
            plugin,
        )
        assert result is not None
        assert result["type"] == "offgrid_mode_set"
        assert result["enabled"] is True
        assert result["persisted"] is True
        plugin.app.set_offgrid_mode.assert_called_once_with(True)

    def test_set_offgrid_mode_false(self):
        plugin = MagicMock()
        plugin.app.set_offgrid_mode.return_value = {"enabled": False, "persisted": True}
        result = _handle_ws_command(
            {"action": "set_offgrid_mode", "enabled": False},
            plugin,
        )
        assert result is not None
        assert result["type"] == "offgrid_mode_set"
        assert result["enabled"] is False

    def test_set_offgrid_mode_missing_enabled(self):
        plugin = MagicMock()
        result = _handle_ws_command(
            {"action": "set_offgrid_mode"},
            plugin,
        )
        assert result is not None
        assert result["type"] == "offgrid_error"
        assert "required" in result["error"]
        plugin.app.set_offgrid_mode.assert_not_called()

    def test_set_offgrid_mode_non_boolean(self):
        plugin = MagicMock()
        result = _handle_ws_command(
            {"action": "set_offgrid_mode", "enabled": "true"},
            plugin,
        )
        assert result is not None
        assert result["type"] == "offgrid_error"
        assert "boolean" in result["error"]
        plugin.app.set_offgrid_mode.assert_not_called()


class TestRadioCommands:
    def _make_plugin(self, fm=None):
        plugin = MagicMock()
        plugin.app.plugins = {"fm_receiver": fm} if fm else {}
        return plugin

    def test_no_fm_receiver_returns_none(self):
        plugin = self._make_plugin(fm=None)
        result = _handle_ws_command({"action": "radio_tune", "frequency_mhz": 99.5}, plugin)
        assert result is None

    def test_tune_valid_frequency(self):
        fm = MagicMock()
        fm.tune.return_value = {"frequency_hz": 99_500_000, "mode": "wbfm"}
        plugin = self._make_plugin(fm)
        result = _handle_ws_command(
            {"action": "radio_tune", "frequency_mhz": 99.5}, plugin
        )
        assert result["type"] == "radio_tuned"
        assert result["frequency_hz"] == 99_500_000
        fm.tune.assert_called_once_with(99_500_000, mode=None)

    def test_tune_with_mode(self):
        fm = MagicMock()
        fm.tune.return_value = {"frequency_hz": 99_500_000, "mode": "nbfm"}
        plugin = self._make_plugin(fm)
        result = _handle_ws_command(
            {"action": "radio_tune", "frequency_mhz": 99.5, "mode": "nbfm"}, plugin
        )
        assert result["type"] == "radio_tuned"
        fm.tune.assert_called_once_with(99_500_000, mode="nbfm")

    def test_tune_missing_frequency(self):
        fm = MagicMock()
        plugin = self._make_plugin(fm)
        result = _handle_ws_command({"action": "radio_tune"}, plugin)
        assert result["type"] == "radio_error"
        assert "frequency_mhz required" in result["error"]
        fm.tune.assert_not_called()

    def test_tune_invalid_frequency(self):
        fm = MagicMock()
        plugin = self._make_plugin(fm)
        result = _handle_ws_command(
            {"action": "radio_tune", "frequency_mhz": "not_a_number"}, plugin
        )
        assert result["type"] == "radio_error"
        assert "invalid frequency_mhz" in result["error"]
        fm.tune.assert_not_called()

    def test_tune_non_string_mode_rejected(self):
        fm = MagicMock()
        plugin = self._make_plugin(fm)
        result = _handle_ws_command(
            {"action": "radio_tune", "frequency_mhz": 99.5, "mode": 123}, plugin
        )
        assert result["type"] == "radio_error"
        assert "mode must be a string" in result["error"]

    def test_tune_raises_value_error(self):
        fm = MagicMock()
        fm.tune.side_effect = ValueError("out of range")
        plugin = self._make_plugin(fm)
        result = _handle_ws_command(
            {"action": "radio_tune", "frequency_mhz": 99.5}, plugin
        )
        assert result["type"] == "radio_error"
        assert "out of range" in result["error"]

    def test_stop(self):
        fm = MagicMock()
        fm.stop_playback.return_value = {"stopped": True}
        plugin = self._make_plugin(fm)
        result = _handle_ws_command({"action": "radio_stop"}, plugin)
        assert result["type"] == "radio_stop"
        assert result["stopped"] is True
        fm.stop_playback.assert_called_once()

    def test_play(self):
        fm = MagicMock()
        fm.play.return_value = {"playing": True}
        plugin = self._make_plugin(fm)
        result = _handle_ws_command({"action": "radio_play"}, plugin)
        assert result["type"] == "radio_play"
        assert result["playing"] is True

    def test_volume_valid(self):
        fm = MagicMock()
        fm.set_volume.return_value = {"volume": 0.5}
        plugin = self._make_plugin(fm)
        result = _handle_ws_command({"action": "radio_volume", "volume": 0.5}, plugin)
        assert result["type"] == "radio_volume"
        assert result["volume"] == 0.5
        fm.set_volume.assert_called_once_with(0.5)

    def test_volume_out_of_range(self):
        fm = MagicMock()
        plugin = self._make_plugin(fm)
        result = _handle_ws_command({"action": "radio_volume", "volume": 1.5}, plugin)
        assert result["type"] == "radio_error"
        assert "0.0-1.0" in result["error"]
        fm.set_volume.assert_not_called()

    def test_volume_negative(self):
        fm = MagicMock()
        plugin = self._make_plugin(fm)
        result = _handle_ws_command({"action": "radio_volume", "volume": -0.1}, plugin)
        assert result["type"] == "radio_error"
        assert "0.0-1.0" in result["error"]

    def test_gain(self):
        fm = MagicMock()
        fm.set_gain.return_value = {"gain_db": 20}
        plugin = self._make_plugin(fm)
        result = _handle_ws_command({"action": "radio_gain", "gain_db": 20}, plugin)
        assert result["type"] == "radio_gain"
        assert result["gain_db"] == 20
        fm.set_gain.assert_called_once_with(20.0)

    def test_squelch(self):
        fm = MagicMock()
        fm.set_squelch.return_value = {"level": 5}
        plugin = self._make_plugin(fm)
        result = _handle_ws_command({"action": "radio_squelch", "level": 5}, plugin)
        assert result["type"] == "radio_squelch"
        fm.set_squelch.assert_called_once_with(5)

    def test_lock(self):
        fm = MagicMock()
        fm.lock_dongle.return_value = {"locked": True}
        plugin = self._make_plugin(fm)
        result = _handle_ws_command({"action": "radio_lock"}, plugin)
        assert result["type"] == "radio_lock"
        assert result["locked"] is True

    def test_unlock(self):
        fm = MagicMock()
        fm.unlock_dongle.return_value = {"unlocked": True}
        plugin = self._make_plugin(fm)
        result = _handle_ws_command({"action": "radio_unlock"}, plugin)
        assert result["type"] == "radio_unlock"
        assert result["unlocked"] is True

    def test_add_favorite(self):
        fm = MagicMock()
        fm.add_favorite.return_value = {"id": "fav1", "label": "Rock FM"}
        plugin = self._make_plugin(fm)
        result = _handle_ws_command(
            {
                "action": "radio_add_favorite",
                "label": "Rock FM",
                "frequency_mhz": 99.5,
                "mode": "wbfm",
            },
            plugin,
        )
        assert result["type"] == "radio_favorite_added"
        assert result["label"] == "Rock FM"
        fm.add_favorite.assert_called_once_with(
            label="Rock FM", frequency_mhz=99.5, mode="wbfm", gain_db=None
        )

    def test_remove_favorite_success(self):
        fm = MagicMock()
        fm.remove_favorite.return_value = True
        plugin = self._make_plugin(fm)
        result = _handle_ws_command(
            {"action": "radio_remove_favorite", "favorite_id": "fav1"}, plugin
        )
        assert result["type"] == "radio_favorite_removed"
        assert result["id"] == "fav1"

    def test_remove_favorite_not_found(self):
        fm = MagicMock()
        fm.remove_favorite.return_value = False
        plugin = self._make_plugin(fm)
        result = _handle_ws_command(
            {"action": "radio_remove_favorite", "favorite_id": "nope"}, plugin
        )
        assert result["type"] == "radio_error"
        assert "not found" in result["error"].lower()

    def test_tune_favorite(self):
        fm = MagicMock()
        fm.tune_favorite.return_value = {"frequency_hz": 99_500_000}
        plugin = self._make_plugin(fm)
        result = _handle_ws_command(
            {"action": "radio_tune_favorite", "favorite_id": "fav1"}, plugin
        )
        assert result["type"] == "radio_tuned"
        fm.tune_favorite.assert_called_once_with("fav1")

    def test_tune_favorite_not_found(self):
        fm = MagicMock()
        fm.tune_favorite.side_effect = ValueError("Favorite not found")
        plugin = self._make_plugin(fm)
        result = _handle_ws_command(
            {"action": "radio_tune_favorite", "favorite_id": "nope"}, plugin
        )
        assert result["type"] == "radio_error"
        assert "not found" in result["error"].lower()

    def test_record_start(self):
        fm = MagicMock()
        fm.start_recording.return_value = {"recording": True, "file": "rec_001.wav"}
        plugin = self._make_plugin(fm)
        result = _handle_ws_command(
            {"action": "radio_record_start", "label": "test"}, plugin
        )
        assert result["type"] == "radio_record_started"
        assert result["recording"] is True
        fm.start_recording.assert_called_once_with(label="test")

    def test_record_start_error(self):
        fm = MagicMock()
        fm.start_recording.return_value = {"error": "Already recording"}
        plugin = self._make_plugin(fm)
        result = _handle_ws_command({"action": "radio_record_start"}, plugin)
        assert result["type"] == "radio_error"
        assert "Already recording" in result["error"]

    def test_record_stop(self):
        fm = MagicMock()
        fm.stop_recording.return_value = {"recording": False, "file": "rec_001.wav"}
        plugin = self._make_plugin(fm)
        result = _handle_ws_command({"action": "radio_record_stop"}, plugin)
        assert result["type"] == "radio_record_stopped"
        assert result["recording"] is False
        fm.stop_recording.assert_called_once()

    def test_unknown_radio_action_with_fm(self):
        """Unknown radio_* sub-action returns None when fm_receiver exists."""
        fm = MagicMock()
        plugin = self._make_plugin(fm)
        result = _handle_ws_command({"action": "radio_nonexistent"}, plugin)
        assert result is None


class TestOnInternetEvent:
    def test_includes_force_offline_true(self):
        probe = MagicMock()
        probe.force_offline = True
        plugin = MagicMock()
        plugin.app.internet_probe = probe

        loop = MagicMock()
        captured = []
        loop.call_soon_threadsafe.side_effect = lambda fn, *a: captured.append(a)
        ws = MagicMock()

        wsh_module._ws_loop = loop
        wsh_module._ws_plugin = plugin
        _ws_clients.add(ws)
        try:
            _on_internet_event("internet.offline", {"wan_ip": None, "lan_ip": "10.0.0.1"})
            assert len(captured) == 1
            push_type, payload = captured[0]
            assert push_type == "internet_status"
            assert payload["force_offline"] is True
            assert payload["online"] is False
        finally:
            _ws_clients.discard(ws)
            wsh_module._ws_loop = None
            wsh_module._ws_plugin = None

    def test_includes_force_offline_false(self):
        probe = MagicMock()
        probe.force_offline = False
        plugin = MagicMock()
        plugin.app.internet_probe = probe

        loop = MagicMock()
        captured = []
        loop.call_soon_threadsafe.side_effect = lambda fn, *a: captured.append(a)
        ws = MagicMock()

        wsh_module._ws_loop = loop
        wsh_module._ws_plugin = plugin
        _ws_clients.add(ws)
        try:
            _on_internet_event("internet.online", {"wan_ip": "1.2.3.4", "lan_ip": "10.0.0.1"})
            assert len(captured) == 1
            _, payload = captured[0]
            assert payload["force_offline"] is False
            assert payload["online"] is True
        finally:
            _ws_clients.discard(ws)
            wsh_module._ws_loop = None
            wsh_module._ws_plugin = None

    def test_defaults_false_when_no_probe(self):
        plugin = MagicMock()
        plugin.app.internet_probe = None

        loop = MagicMock()
        captured = []
        loop.call_soon_threadsafe.side_effect = lambda fn, *a: captured.append(a)
        ws = MagicMock()

        wsh_module._ws_loop = loop
        wsh_module._ws_plugin = plugin
        _ws_clients.add(ws)
        try:
            _on_internet_event("internet.offline", {"wan_ip": None, "lan_ip": None})
            assert len(captured) == 1
            _, payload = captured[0]
            assert payload["force_offline"] is False
        finally:
            _ws_clients.discard(ws)
            wsh_module._ws_loop = None
            wsh_module._ws_plugin = None


# ── Firmware event tests ─────────────────────────────────────────────


class TestOnFirmwareEvent:
    def test_hang_event_payload(self):
        loop = MagicMock()
        captured = []
        loop.call_soon_threadsafe.side_effect = lambda fn, *a: captured.append(a)
        ws = MagicMock()

        wsh_module._ws_loop = loop
        _ws_clients.add(ws)
        try:
            _on_firmware_event(
                "meshtastic.firmware_hang",
                {"reason": "usb_disappeared", "silence_seconds": 320, "total_hangs": 2},
            )
            assert len(captured) == 1
            push_type, payload = captured[0]
            assert push_type == "firmware_status"
            assert payload["hang"] is True
            assert payload["reason"] == "usb_disappeared"
            assert payload["silence_seconds"] == 320
            assert payload["total_hangs"] == 2
        finally:
            _ws_clients.discard(ws)
            wsh_module._ws_loop = None

    def test_recovered_event_payload(self):
        loop = MagicMock()
        captured = []
        loop.call_soon_threadsafe.side_effect = lambda fn, *a: captured.append(a)
        ws = MagicMock()

        wsh_module._ws_loop = loop
        _ws_clients.add(ws)
        try:
            _on_firmware_event(
                "meshtastic.firmware_recovered",
                {"total_resets": 3},
            )
            assert len(captured) == 1
            push_type, payload = captured[0]
            assert push_type == "firmware_status"
            assert payload["hang"] is False
            assert payload["total_resets"] == 3
        finally:
            _ws_clients.discard(ws)
            wsh_module._ws_loop = None

    def test_noop_when_no_clients(self):
        loop = MagicMock()
        wsh_module._ws_loop = loop
        try:
            _on_firmware_event("meshtastic.firmware_hang", {"reason": "probe_timeout"})
            loop.call_soon_threadsafe.assert_not_called()
        finally:
            wsh_module._ws_loop = None

    def test_noop_when_loop_missing(self):
        wsh_module._ws_loop = None
        ws = MagicMock()
        _ws_clients.add(ws)
        try:
            _on_firmware_event("meshtastic.firmware_hang", {"reason": "probe_timeout"})
        finally:
            _ws_clients.discard(ws)


# ── _on_position_recorded_event tests ────────────────────────────────


class TestOnPositionRecordedEvent:
    """NODE_POSITION_RECORDED callback → scheduled WS push with trail_update type."""

    def test_noop_when_loop_missing(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        with patch.object(wsh, "_ws_loop", None):
            _on_position_recorded_event("node.position_recorded", {"count": 3})

    def test_noop_when_no_clients(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        loop = MagicMock()
        with patch.object(wsh, "_ws_loop", loop):
            _on_position_recorded_event("node.position_recorded", {"count": 3})
        loop.call_soon_threadsafe.assert_not_called()

    def test_schedules_push_with_trail_update_type(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        loop = MagicMock()
        _ws_clients.add(MagicMock())
        data = {"count": 5}
        with patch.object(wsh, "_ws_loop", loop):
            _on_position_recorded_event("node.position_recorded", data)
        loop.call_soon_threadsafe.assert_called_once()
        args = loop.call_soon_threadsafe.call_args.args
        assert args[0] is wsh._schedule_push
        assert args[1] == "trail_update"
        assert args[2] == {"count": 5}

    def test_defaults_count_to_zero(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        loop = MagicMock()
        _ws_clients.add(MagicMock())
        with patch.object(wsh, "_ws_loop", loop):
            _on_position_recorded_event("node.position_recorded", {})
        args = loop.call_soon_threadsafe.call_args.args
        assert args[2] == {"count": 0}

    def test_swallows_runtime_error_on_closed_loop(self):
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        loop = MagicMock()
        loop.call_soon_threadsafe.side_effect = RuntimeError("loop closed")
        _ws_clients.add(MagicMock())
        with patch.object(wsh, "_ws_loop", loop):
            _on_position_recorded_event("node.position_recorded", {"count": 1})


# ── Origin validation tests ──────────────────────────────────────────


class TestCheckWsOrigin:
    def _make_request(self, origin=None, host="localhost:8080"):
        req = MagicMock()
        headers = {}
        if origin is not None:
            headers["Origin"] = origin
        req.headers = headers
        req.host = host
        return req

    def test_no_origin_header_allows(self):
        req = self._make_request(origin=None)
        assert _check_ws_origin(req) is True

    def test_same_origin_allows(self):
        req = self._make_request(origin="http://localhost:8080", host="localhost:8080")
        assert _check_ws_origin(req) is True

    def test_cross_origin_rejects(self):
        req = self._make_request(origin="http://evil.com", host="localhost:8080")
        assert _check_ws_origin(req) is False

    def test_same_host_different_scheme(self):
        req = self._make_request(origin="https://myhost:8080", host="myhost:8080")
        assert _check_ws_origin(req) is True

    def test_different_port_rejects(self):
        req = self._make_request(origin="http://localhost:9999", host="localhost:8080")
        assert _check_ws_origin(req) is False

    def test_empty_origin_string_rejects(self):
        req = self._make_request(origin="", host="localhost:8080")
        assert _check_ws_origin(req) is True

    def test_malformed_origin_rejects(self):
        req = self._make_request(origin="not-a-url", host="localhost:8080")
        assert _check_ws_origin(req) is False


class TestWsOriginIntegration:
    @pytest.mark.asyncio
    async def test_metrics_rejects_cross_origin(self):
        req = MagicMock()
        req.headers = {"Origin": "http://evil.com"}
        req.host = "localhost:8080"
        req.app = {"plugin": MagicMock()}
        with patch("aiohttp.web.WebSocketResponse") as MockWS:
            mock_ws = AsyncMock()
            MockWS.return_value = mock_ws
            await websocket_metrics(req)
            mock_ws.prepare.assert_awaited_once_with(req)
            mock_ws.close.assert_awaited_once()
            assert mock_ws.close.call_args[1]["code"] == 4003

    @pytest.mark.asyncio
    async def test_spectrum_rejects_cross_origin(self):
        req = MagicMock()
        req.headers = {"Origin": "http://evil.com"}
        req.host = "localhost:8080"
        req.app = {"plugin": MagicMock()}
        with patch("aiohttp.web.WebSocketResponse") as MockWS:
            mock_ws = AsyncMock()
            MockWS.return_value = mock_ws
            await websocket_spectrum(req)
            mock_ws.prepare.assert_awaited_once_with(req)
            mock_ws.close.assert_awaited_once()
            assert mock_ws.close.call_args[1]["code"] == 4003
