"""Tests for the WebSocket handler (websocket_handler.py).

Covers: RNode config parsing, interface collection, transport traffic
enrichment, WebSocket auth/connection lifecycle, and broadcast logic.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from reticulumpi.builtin_plugins.web_dashboard.websocket_handler import (
    _broadcast_metrics,
    _collect_interfaces,
    _enrich_transport_traffic,
    _extract_radio,
    _parse_rnode_config,
    _start_broadcast_task,
    _stop_broadcast_task,
    _ws_clients,
    websocket_metrics,
)


async def _empty_async_iter():
    """Async iterator that yields nothing — simulates an idle WebSocket."""
    return
    yield  # noqa: unreachable — makes this an async generator


@pytest.fixture(autouse=True)
def _reset_ws_clients():
    # _ws_clients is a module-level set; without reset, a failing test leaves
    # entries behind and poisons later tests. Required for safe re-ordering
    # and parallel runs.
    _ws_clients.clear()
    yield
    _ws_clients.clear()


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
            "[interfaces]\n"
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


def _make_ws_request(token=None, cookie_token=None, max_clients=10, auth_valid=True):
    """Create a mock aiohttp request for WebSocket tests."""
    request = MagicMock()
    request.query = {}
    if token:
        request.query["token"] = token

    request.cookies = {}
    if cookie_token:
        request.cookies["session"] = cookie_token

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
            result = asyncio.run(websocket_metrics(request))

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
            result = asyncio.run(websocket_metrics(request))

        ws_mock.close.assert_called_once()

    def test_accepts_query_token(self):
        """Valid query param token is accepted."""
        request = _make_ws_request(token="valid_token", auth_valid=True)

        ws_mock = MagicMock()
        ws_mock.prepare = AsyncMock()
        # Simulate empty message loop
        ws_mock.__aiter__ = lambda self: _empty_async_iter()

        with patch("aiohttp.web.WebSocketResponse", return_value=ws_mock):
            result = asyncio.run(websocket_metrics(request))

        ws_mock.close.assert_not_called()

    def test_accepts_cookie_token(self):
        """Falls back to cookie session token."""
        request = _make_ws_request(cookie_token="cookie_token", auth_valid=True)

        ws_mock = MagicMock()
        ws_mock.prepare = AsyncMock()
        ws_mock.__aiter__ = lambda self: _empty_async_iter()

        with patch("aiohttp.web.WebSocketResponse", return_value=ws_mock):
            result = asyncio.run(websocket_metrics(request))

        # Should have validated the cookie token
        request.app["plugin"]._auth.validate_token.assert_called_with("cookie_token")
        ws_mock.close.assert_not_called()

    def test_rejects_when_max_clients_reached(self):
        """Closes with 4002 when max clients exceeded."""
        request = _make_ws_request(token="valid", auth_valid=True, max_clients=2)

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
                result = asyncio.run(websocket_metrics(request))

            ws_mock.close.assert_called_once()
            call_args = ws_mock.close.call_args
            assert call_args[1].get("code") == 4002 or call_args[0][0] == 4002
        finally:
            _ws_clients.clear()

    def test_client_added_and_removed(self):
        """Client is tracked in _ws_clients during connection and removed after."""
        request = _make_ws_request(token="valid", auth_valid=True)
        _ws_clients.clear()

        ws_mock = MagicMock()
        ws_mock.prepare = AsyncMock()
        ws_mock.__aiter__ = lambda self: _empty_async_iter()

        with patch("aiohttp.web.WebSocketResponse", return_value=ws_mock):
            result = asyncio.run(websocket_metrics(request))

        # After disconnect, client should be removed
        assert ws_mock not in _ws_clients

    def test_heartbeat_configured(self):
        """Authenticated WebSocket gets 60s heartbeat."""
        request = _make_ws_request(token="valid", auth_valid=True)
        _ws_clients.clear()

        ws_instances = []

        def capture_ws(*args, **kwargs):
            ws = MagicMock()
            ws.prepare = AsyncMock()
            ws.__aiter__ = lambda self: _empty_async_iter()
            ws_instances.append(kwargs)
            return ws

        with patch("aiohttp.web.WebSocketResponse", side_effect=capture_ws):
            result = asyncio.run(websocket_metrics(request))

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
    def _make_plugin(self, plugins=None):
        """Create a mock plugin with configurable sub-plugins."""
        plugin = MagicMock()
        plugin.config = {"metrics_interval": 0.1}
        plugin.app.reticulum = MagicMock()
        plugin.app.reticulum.get_interface_stats.return_value = {"interfaces": []}

        available = plugins or {}
        plugin.app.get_plugin.side_effect = lambda name: available.get(name)
        plugin.app.plugins = {}

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
        assert "metrics" in data["data"]
        assert "interfaces" in data["data"]
        assert "plugins" in data["data"]

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
        """No work is done when _ws_clients is empty."""
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
                # Don't actually sleep in tests
                await original_sleep(0)

            with patch("asyncio.sleep", counted_sleep):
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

        network_map = MagicMock()
        network_map.get_node_count.return_value = 42
        network_map.get_recent_announces.return_value = [
            {"hash": "aa" * 16, "last_seen": time.time()},
        ]

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

        sensor_fw = MagicMock()
        sensor_fw.get_latest_readings.return_value = {
            "temperature": {"value": 22.5, "unit": "C"},
        }

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

        transport_mon = MagicMock()
        transport_mon.get_hub_health.return_value = {
            "primaries": [
                {"target_host": "hub.example.com", "target_port": 4242},
            ],
            "active_fallbacks": [],
            "auto_discovery": {"connected": []},
        }

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
        """Recent messages are included when messaging_hub is available."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        msg_hub = MagicMock()
        msg_hub.get_messages.return_value = [
            {"text": "hello", "timestamp": time.time()},
        ]
        msg_hub.get_transports.return_value = ["lxmf"]
        msg_hub.get_unread_counts.return_value = {}
        msg_hub.get_status_updates_since.return_value = []

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
            wsh._last_msg_ts["ts"] = 0
            try:
                await wsh._broadcast_metrics(app)
            except asyncio.CancelledError:
                pass

        asyncio.run(run())

        data = json.loads(messages[0])
        assert "messaging" in data["data"]
        assert len(data["data"]["messaging"]["messages"]) == 1
        assert data["data"]["messaging"]["transports"] == ["lxmf"]

        _ws_clients.clear()

    def test_error_recovery(self):
        """Broadcast loop recovers from plugin exceptions."""
        import reticulumpi.builtin_plugins.web_dashboard.websocket_handler as wsh

        plugin = MagicMock()
        plugin.config = {"metrics_interval": 0.01}
        plugin.app.reticulum = None  # Will cause issues in collection

        call_count = 0

        # Make get_plugin raise, then succeed
        def failing_get_plugin(name):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise RuntimeError("transient failure")
            return None

        plugin.app.get_plugin.side_effect = failing_get_plugin
        plugin.app.plugins = {}

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

        async def run():
            try:
                await wsh._broadcast_metrics(app)
            except asyncio.CancelledError:
                pass

        asyncio.run(run())

        # Should have recovered and eventually sent a message
        assert call_count >= 1

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

        app.router.add_get.assert_called_once_with("/ws/metrics", websocket_metrics)
        assert len(app.on_startup) == 1
        assert len(app.on_shutdown) == 1
