"""Tests for API endpoints that write to disk or modify system state.

Covers: interface toggle, interface add, LoRa announce mode,
NomadNet auth add/remove, send message, and the _validate_interface_config
helper.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

from reticulumpi.builtin_plugins.web_dashboard.api import (
    handle_offgrid_get,
    handle_offgrid_set,
    handle_services_restart,
    handle_spectrum_switch_preset,
)
from reticulumpi.builtin_plugins.web_dashboard.api_interfaces import (
    _validate_interface_config,
    handle_interface_add,
    handle_interface_toggle,
)
from reticulumpi.builtin_plugins.web_dashboard.api_services import (
    handle_lora_announce_mode,
    handle_nomadnet_auth_add,
    handle_nomadnet_auth_remove,
    handle_send_message,
)
from reticulumpi.rns_config import InterfaceEntry


# ── Test helpers ───────────────────────────────────────────────────────


def _make_request(
    body=None, query_string="", plugin_mock=None, match_info=None, token="valid-test-token"
):
    """Create a mock aiohttp.web.Request.

    Uses a real dict for item access so ``request.get("token")`` returns a
    realistic value instead of an always-truthy MagicMock.
    """
    request = MagicMock()

    _store: dict = {}
    if token is not None:
        _store["token"] = token
    request.__getitem__ = lambda _self, key: _store[key]
    request.__setitem__ = lambda _self, key, val: _store.__setitem__(key, val)
    request.__contains__ = lambda _self, key: key in _store
    request.get = lambda key, default=None: _store.get(key, default)

    # Parse query string
    query = {}
    if query_string:
        for part in query_string.lstrip("?").split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                query[k] = v
    request.query = query
    request.remote = "127.0.0.1"

    # match_info for path params
    if match_info is None:
        match_info = {}
    request.match_info = match_info

    # json() coroutine
    if body is not None:

        async def _json():
            return body

        request.json = _json
    else:

        async def _json():
            raise ValueError("no body")

        request.json = _json

    if plugin_mock is None:
        plugin_mock = MagicMock()
    request.app = {"plugin": plugin_mock}
    return request


def _parse_response(resp) -> dict:
    """Parse the JSON text from an aiohttp.web.Response."""
    return json.loads(resp.text)


# ── _validate_interface_config ──────────────────────────────────────────


class TestValidateInterfaceConfig:
    """Unit tests for the pure validation function."""

    def test_valid_rnode_config_passes(self):
        props = {
            "port": "/dev/ttyUSB0",
            "frequency": 867200000,
            "bandwidth": 125000,
            "txpower": 7,
            "spreadingfactor": 8,
            "codingrate": 5,
        }
        assert _validate_interface_config("RNodeInterface", props) is None

    def test_unknown_type_fails(self):
        err = _validate_interface_config("MadeUpInterface", {})
        assert err is not None
        assert "Unknown interface type" in err
        assert "MadeUpInterface" in err

    def test_missing_required_field_fails(self):
        # RNodeInterface requires "port"
        props = {
            "frequency": 867200000,
            "bandwidth": 125000,
            "txpower": 7,
            "spreadingfactor": 8,
            "codingrate": 5,
            # "port" is missing
        }
        err = _validate_interface_config("RNodeInterface", props)
        assert err is not None
        assert "port" in err

    def test_rnode_frequency_out_of_range_low(self):
        props = {
            "port": "/dev/ttyUSB0",
            "frequency": 50_000_000,  # below 100 MHz
            "bandwidth": 125000,
            "txpower": 7,
            "spreadingfactor": 8,
            "codingrate": 5,
        }
        err = _validate_interface_config("RNodeInterface", props)
        assert err is not None
        assert "frequency" in err

    def test_rnode_frequency_out_of_range_high(self):
        props = {
            "port": "/dev/ttyUSB0",
            "frequency": 2_000_000_000,  # above 1 GHz
            "bandwidth": 125000,
            "txpower": 7,
            "spreadingfactor": 8,
            "codingrate": 5,
        }
        err = _validate_interface_config("RNodeInterface", props)
        assert err is not None
        assert "frequency" in err

    def test_rnode_txpower_out_of_range(self):
        props = {
            "port": "/dev/ttyUSB0",
            "frequency": 867200000,
            "bandwidth": 125000,
            "txpower": 30,  # max is 22
            "spreadingfactor": 8,
            "codingrate": 5,
        }
        err = _validate_interface_config("RNodeInterface", props)
        assert err is not None
        assert "txpower" in err

    def test_rnode_spreading_factor_boundary_valid(self):
        """SF 7 and 12 are valid boundary values."""
        base = {
            "port": "/dev/ttyUSB0",
            "frequency": 867200000,
            "bandwidth": 125000,
            "txpower": 7,
            "codingrate": 5,
        }
        assert _validate_interface_config("RNodeInterface", {**base, "spreadingfactor": 7}) is None
        assert _validate_interface_config("RNodeInterface", {**base, "spreadingfactor": 12}) is None

    def test_rnode_spreading_factor_out_of_range(self):
        props = {
            "port": "/dev/ttyUSB0",
            "frequency": 867200000,
            "bandwidth": 125000,
            "txpower": 7,
            "spreadingfactor": 13,  # max is 12
            "codingrate": 5,
        }
        err = _validate_interface_config("RNodeInterface", props)
        assert err is not None
        assert "spreadingfactor" in err

    def test_boolean_parsing_works(self):
        """Boolean values like 'yes', 'no', 'true', 'false' are accepted."""
        props = {"kiss_framing": "true", "target_host": "localhost", "target_port": 4242}
        assert _validate_interface_config("TCPClientInterface", props) is None

        props2 = {"kiss_framing": "yes", "target_host": "localhost", "target_port": 4242}
        assert _validate_interface_config("TCPClientInterface", props2) is None

        props3 = {"kiss_framing": "off", "target_host": "localhost", "target_port": 4242}
        assert _validate_interface_config("TCPClientInterface", props3) is None

    def test_tcp_client_valid(self):
        props = {"target_host": "10.0.0.1", "target_port": 4242}
        assert _validate_interface_config("TCPClientInterface", props) is None

    def test_tcp_client_missing_target_host(self):
        props = {"target_port": 4242}
        err = _validate_interface_config("TCPClientInterface", props)
        assert err is not None
        assert "target_host" in err

    def test_tcp_client_missing_target_port(self):
        props = {"target_host": "10.0.0.1"}
        err = _validate_interface_config("TCPClientInterface", props)
        assert err is not None
        assert "target_port" in err

    def test_auto_interface_no_required_props(self):
        """AutoInterface has no required properties."""
        assert _validate_interface_config("AutoInterface", {}) is None

    def test_tcp_server_no_required_props(self):
        """TCPServerInterface has no required properties."""
        assert _validate_interface_config("TCPServerInterface", {}) is None

    def test_serial_interface_requires_port(self):
        err = _validate_interface_config("SerialInterface", {})
        assert err is not None
        assert "port" in err

    def test_type_and_enabled_keys_skipped_in_optional_check(self):
        """Properties named 'type', 'enabled', 'mode' are not type-checked."""
        props = {
            "target_host": "localhost",
            "target_port": 4242,
            "type": "anything",
            "enabled": "whatever",
        }
        assert _validate_interface_config("TCPClientInterface", props) is None


# ── handle_interface_toggle ─────────────────────────────────────────────


class TestHandleInterfaceToggle:
    """Tests for POST /api/interfaces/{name}/toggle."""

    @patch("reticulumpi.rns_config.write_rns_config")
    @patch("reticulumpi.rns_config.set_interface_enabled")
    @patch("reticulumpi.rns_config.parse_rns_config")
    def test_toggle_success(self, mock_parse, mock_set, mock_write):
        entry = InterfaceEntry(
            name="TCP Client", iface_type="TCPClientInterface", enabled=True, start_line=5
        )
        mock_parse.return_value = (["line1\n", "line2\n"], [entry])
        mock_set.return_value = ["line1\n", "line2_modified\n"]

        plugin = MagicMock()
        plugin.app._reticulum_config_dir = "/tmp/test_rns"
        request = _make_request(plugin_mock=plugin, match_info={"name": "TCP Client"})

        resp = asyncio.run(handle_interface_toggle(request))
        data = _parse_response(resp)

        assert data["ok"] is True
        assert data["data"]["name"] == "TCP Client"
        assert data["data"]["enabled"] is False  # toggled from True
        assert data["data"]["restart_required"] is True
        mock_write.assert_called_once()

    @patch("reticulumpi.rns_config.write_rns_config")
    @patch("reticulumpi.rns_config.set_interface_enabled")
    @patch("reticulumpi.rns_config.parse_rns_config")
    def test_toggle_disabled_to_enabled(self, mock_parse, mock_set, mock_write):
        entry = InterfaceEntry(
            name="My RNode", iface_type="RNodeInterface", enabled=False, start_line=10
        )
        mock_parse.return_value = (["line\n"], [entry])
        mock_set.return_value = ["line_modified\n"]

        plugin = MagicMock()
        plugin.app._reticulum_config_dir = "/tmp/test_rns"
        request = _make_request(plugin_mock=plugin, match_info={"name": "My RNode"})

        resp = asyncio.run(handle_interface_toggle(request))
        data = _parse_response(resp)

        assert data["ok"] is True
        assert data["data"]["enabled"] is True  # toggled from False

    @patch("reticulumpi.rns_config.parse_rns_config")
    def test_toggle_interface_not_found(self, mock_parse):
        entry = InterfaceEntry(name="TCP Client", iface_type="TCPClientInterface", enabled=True)
        mock_parse.return_value = ([], [entry])

        plugin = MagicMock()
        plugin.app._reticulum_config_dir = "/tmp/test_rns"
        request = _make_request(plugin_mock=plugin, match_info={"name": "Nonexistent"})

        resp = asyncio.run(handle_interface_toggle(request))
        data = _parse_response(resp)

        assert resp.status == 404
        assert data["ok"] is False
        assert "not found" in data["error"]

    @patch("reticulumpi.rns_config.parse_rns_config")
    def test_toggle_parse_error(self, mock_parse):
        mock_parse.side_effect = RuntimeError("corrupt config")

        plugin = MagicMock()
        plugin.app._reticulum_config_dir = "/tmp/test_rns"
        request = _make_request(plugin_mock=plugin, match_info={"name": "TCP Client"})

        resp = asyncio.run(handle_interface_toggle(request))
        data = _parse_response(resp)

        assert resp.status == 500
        assert data["ok"] is False
        assert "parse" in data["error"].lower() or "corrupt" in data["error"].lower()

    @patch("reticulumpi.rns_config.write_rns_config")
    @patch("reticulumpi.rns_config.set_interface_enabled")
    @patch("reticulumpi.rns_config.parse_rns_config")
    def test_toggle_write_error(self, mock_parse, mock_set, mock_write):
        entry = InterfaceEntry(
            name="TCP Client", iface_type="TCPClientInterface", enabled=True, start_line=5
        )
        mock_parse.return_value = (["line\n"], [entry])
        mock_set.return_value = ["modified\n"]
        mock_write.side_effect = PermissionError("read-only filesystem")

        plugin = MagicMock()
        plugin.app._reticulum_config_dir = "/tmp/test_rns"
        request = _make_request(plugin_mock=plugin, match_info={"name": "TCP Client"})

        resp = asyncio.run(handle_interface_toggle(request))
        data = _parse_response(resp)

        assert resp.status == 500
        assert data["ok"] is False
        assert "write" in data["error"].lower() or "read-only" in data["error"].lower()


# ── handle_interface_add ────────────────────────────────────────────────


class TestHandleInterfaceAdd:
    """Tests for POST /api/interfaces/add."""

    @patch("reticulumpi.rns_config.write_rns_config")
    @patch("reticulumpi.rns_config.add_interface_section")
    @patch("reticulumpi.rns_config.parse_rns_config")
    def test_add_success(self, mock_parse, mock_add, mock_write):
        mock_parse.return_value = (["[interfaces]\n"], [])
        mock_add.return_value = ["[interfaces]\n", "[[New TCP]]\n"]

        plugin = MagicMock()
        plugin.app._reticulum_config_dir = "/tmp/test_rns"
        body = {
            "name": "New TCP",
            "type": "TCPClientInterface",
            "properties": {"target_host": "10.0.0.1", "target_port": 4242},
        }
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_interface_add(request))
        data = _parse_response(resp)

        assert data["ok"] is True
        assert data["data"]["name"] == "New TCP"
        assert data["data"]["type"] == "TCPClientInterface"
        assert data["data"]["restart_required"] is True
        mock_write.assert_called_once()

    def test_add_missing_name(self):
        plugin = MagicMock()
        plugin.app._reticulum_config_dir = "/tmp/test_rns"
        body = {"type": "TCPClientInterface", "properties": {"target_host": "x", "target_port": 1}}
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_interface_add(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert data["ok"] is False
        assert "name" in data["error"].lower()

    def test_add_empty_name(self):
        plugin = MagicMock()
        plugin.app._reticulum_config_dir = "/tmp/test_rns"
        body = {
            "name": "   ",
            "type": "TCPClientInterface",
            "properties": {"target_host": "x", "target_port": 1},
        }
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_interface_add(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert "name" in data["error"].lower()

    def test_add_missing_type(self):
        plugin = MagicMock()
        plugin.app._reticulum_config_dir = "/tmp/test_rns"
        body = {"name": "Test", "properties": {}}
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_interface_add(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert "type" in data["error"].lower()

    def test_add_unknown_type(self):
        plugin = MagicMock()
        plugin.app._reticulum_config_dir = "/tmp/test_rns"
        body = {"name": "Test", "type": "FakeInterface", "properties": {}}
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_interface_add(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert "Unknown interface type" in data["error"]

    @patch("reticulumpi.rns_config.parse_rns_config")
    def test_add_duplicate_name(self, mock_parse):
        existing = InterfaceEntry(name="TCP Client", iface_type="TCPClientInterface", enabled=True)
        mock_parse.return_value = ([], [existing])

        plugin = MagicMock()
        plugin.app._reticulum_config_dir = "/tmp/test_rns"
        body = {
            "name": "TCP Client",
            "type": "TCPClientInterface",
            "properties": {"target_host": "x", "target_port": 1},
        }
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_interface_add(request))
        data = _parse_response(resp)

        assert resp.status == 409
        assert data["ok"] is False
        assert "already exists" in data["error"]

    def test_add_rnode_validation_error(self):
        """RNode with out-of-range frequency is rejected."""
        plugin = MagicMock()
        plugin.app._reticulum_config_dir = "/tmp/test_rns"
        body = {
            "name": "Bad RNode",
            "type": "RNodeInterface",
            "properties": {
                "port": "/dev/ttyUSB0",
                "frequency": 50_000_000,  # below minimum
                "bandwidth": 125000,
                "txpower": 7,
                "spreadingfactor": 8,
                "codingrate": 5,
            },
        }
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_interface_add(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert "frequency" in data["error"]

    def test_add_invalid_json_body(self):
        plugin = MagicMock()
        plugin.app._reticulum_config_dir = "/tmp/test_rns"
        # No body triggers ValueError in json()
        request = _make_request(body=None, plugin_mock=plugin)

        resp = asyncio.run(handle_interface_add(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert "Invalid JSON" in data["error"]

    def test_add_name_too_long(self):
        plugin = MagicMock()
        plugin.app._reticulum_config_dir = "/tmp/test_rns"
        body = {
            "name": "A" * 101,
            "type": "TCPClientInterface",
            "properties": {"target_host": "x", "target_port": 1},
        }
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_interface_add(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert "too long" in data["error"]

    @patch("reticulumpi.rns_config.write_rns_config")
    @patch("reticulumpi.rns_config.add_interface_section")
    @patch("reticulumpi.rns_config.parse_rns_config")
    def test_add_write_error(self, mock_parse, mock_add, mock_write):
        mock_parse.return_value = (["[interfaces]\n"], [])
        mock_add.return_value = ["[interfaces]\n", "[[New]]\n"]
        mock_write.side_effect = OSError("disk full")

        plugin = MagicMock()
        plugin.app._reticulum_config_dir = "/tmp/test_rns"
        body = {
            "name": "New",
            "type": "AutoInterface",
            "properties": {},
        }
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_interface_add(request))
        data = _parse_response(resp)

        assert resp.status == 500
        assert data["ok"] is False
        assert "write" in data["error"].lower() or "disk" in data["error"].lower()

    @patch("reticulumpi.rns_config.parse_rns_config")
    def test_add_parse_error(self, mock_parse):
        mock_parse.side_effect = RuntimeError("bad config")

        plugin = MagicMock()
        plugin.app._reticulum_config_dir = "/tmp/test_rns"
        body = {
            "name": "New",
            "type": "AutoInterface",
            "properties": {},
        }
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_interface_add(request))
        data = _parse_response(resp)

        assert resp.status == 500
        assert data["ok"] is False


# ── handle_lora_announce_mode ───────────────────────────────────────────


class TestHandleLoraAnnounceMode:
    """Tests for POST /api/lora/announce_mode."""

    def test_success(self):
        lora = MagicMock()
        lora.set_announce_mode.return_value = {"mode": "silent", "applied": True}

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = lora
        request = _make_request(body={"mode": "silent"}, plugin_mock=plugin)

        resp = asyncio.run(handle_lora_announce_mode(request))
        data = _parse_response(resp)

        assert data["ok"] is True
        assert data["data"]["mode"] == "silent"
        lora.set_announce_mode.assert_called_once_with("silent")

    def test_all_mode(self):
        lora = MagicMock()
        lora.set_announce_mode.return_value = {"mode": "all", "applied": True}

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = lora
        request = _make_request(body={"mode": "all"}, plugin_mock=plugin)

        resp = asyncio.run(handle_lora_announce_mode(request))
        data = _parse_response(resp)

        assert data["ok"] is True
        assert data["data"]["mode"] == "all"

    def test_missing_mode(self):
        lora = MagicMock()

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = lora
        request = _make_request(body={}, plugin_mock=plugin)

        resp = asyncio.run(handle_lora_announce_mode(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert data["ok"] is False
        assert "mode" in data["error"].lower()

    def test_invalid_mode_value_error(self):
        lora = MagicMock()
        lora.set_announce_mode.side_effect = ValueError("Invalid mode 'bogus'")

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = lora
        request = _make_request(body={"mode": "bogus"}, plugin_mock=plugin)

        resp = asyncio.run(handle_lora_announce_mode(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert data["ok"] is False

    def test_runtime_error(self):
        lora = MagicMock()
        lora.set_announce_mode.side_effect = RuntimeError("rnsd restart failed")

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = lora
        request = _make_request(body={"mode": "silent"}, plugin_mock=plugin)

        resp = asyncio.run(handle_lora_announce_mode(request))
        data = _parse_response(resp)

        assert resp.status == 500
        assert data["ok"] is False
        assert "rnsd" in data["error"].lower() or "restart" in data["error"].lower()

    def test_plugin_not_available(self):
        """When lora_diagnostics plugin is not loaded, returns 503."""
        plugin = MagicMock()
        plugin.app.get_plugin.return_value = None
        request = _make_request(body={"mode": "all"}, plugin_mock=plugin)

        resp = asyncio.run(handle_lora_announce_mode(request))
        data = _parse_response(resp)

        assert resp.status == 503
        assert data["ok"] is False
        assert "not available" in data["error"]

    def test_plugin_missing_method(self):
        """Plugin exists but lacks set_announce_mode attribute."""
        lora = MagicMock(spec=[])  # no attributes
        plugin = MagicMock()
        plugin.app.get_plugin.return_value = lora
        request = _make_request(body={"mode": "all"}, plugin_mock=plugin)

        resp = asyncio.run(handle_lora_announce_mode(request))
        data = _parse_response(resp)

        assert resp.status == 503
        assert data["ok"] is False
        assert "not available" in data["error"]

    def test_invalid_json_body(self):
        """When request body is not valid JSON."""
        lora = MagicMock()

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = lora
        # No body causes json() to raise
        request = _make_request(body=None, plugin_mock=plugin)

        resp = asyncio.run(handle_lora_announce_mode(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert data["ok"] is False
        assert "Invalid JSON" in data["error"]


# ── handle_nomadnet_auth_add ────────────────────────────────────────────


class TestHandleNomadnetAuthAdd:
    """Tests for POST /api/nomadnet/auth/add."""

    def test_success(self):
        nn = MagicMock()
        nn.add_allowed_identity.return_value = True

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = nn
        request = _make_request(body={"identity": "abcdef1234567890"}, plugin_mock=plugin)

        resp = asyncio.run(handle_nomadnet_auth_add(request))
        data = _parse_response(resp)

        assert data["ok"] is True
        assert data["data"]["added"] is True
        nn.add_allowed_identity.assert_called_once_with("abcdef1234567890")

    def test_missing_identity(self):
        nn = MagicMock()

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = nn
        request = _make_request(body={}, plugin_mock=plugin)

        resp = asyncio.run(handle_nomadnet_auth_add(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert data["ok"] is False
        assert "identity" in data["error"].lower()

    def test_empty_identity(self):
        nn = MagicMock()

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = nn
        request = _make_request(body={"identity": ""}, plugin_mock=plugin)

        resp = asyncio.run(handle_nomadnet_auth_add(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert "identity" in data["error"].lower()

    def test_identity_too_long(self):
        nn = MagicMock()

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = nn
        request = _make_request(body={"identity": "a" * 129}, plugin_mock=plugin)

        resp = asyncio.run(handle_nomadnet_auth_add(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert "too long" in data["error"]

    def test_identity_at_max_length_accepted(self):
        """128 characters is the boundary -- should be accepted."""
        nn = MagicMock()
        nn.add_allowed_identity.return_value = True

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = nn
        request = _make_request(body={"identity": "a" * 128}, plugin_mock=plugin)

        resp = asyncio.run(handle_nomadnet_auth_add(request))
        data = _parse_response(resp)

        assert data["ok"] is True

    def test_plugin_not_available(self):
        plugin = MagicMock()
        plugin.app.get_plugin.return_value = None
        request = _make_request(body={"identity": "abc123"}, plugin_mock=plugin)

        resp = asyncio.run(handle_nomadnet_auth_add(request))
        data = _parse_response(resp)

        assert resp.status == 503
        assert data["ok"] is False
        assert "not available" in data["error"]

    def test_plugin_missing_method(self):
        """Plugin exists but lacks add_allowed_identity attribute."""
        nn = MagicMock(spec=[])  # no attributes
        plugin = MagicMock()
        plugin.app.get_plugin.return_value = nn
        request = _make_request(body={"identity": "abc123"}, plugin_mock=plugin)

        resp = asyncio.run(handle_nomadnet_auth_add(request))
        _parse_response(resp)

        assert resp.status == 503

    def test_value_error_from_plugin(self):
        nn = MagicMock()
        nn.add_allowed_identity.side_effect = ValueError("invalid hex hash")

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = nn
        request = _make_request(body={"identity": "not_a_valid_hash"}, plugin_mock=plugin)

        resp = asyncio.run(handle_nomadnet_auth_add(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert data["ok"] is False
        assert "invalid hex hash" in data["error"]

    def test_invalid_request_body(self):
        nn = MagicMock()

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = nn
        # No body triggers json() ValueError
        request = _make_request(body=None, plugin_mock=plugin)

        resp = asyncio.run(handle_nomadnet_auth_add(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert data["ok"] is False


# ── handle_nomadnet_auth_remove ─────────────────────────────────────────


class TestHandleNomadnetAuthRemove:
    """Tests for POST /api/nomadnet/auth/remove."""

    def test_success(self):
        nn = MagicMock()
        nn.remove_allowed_identity.return_value = True

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = nn
        request = _make_request(body={"identity": "abcdef1234567890"}, plugin_mock=plugin)

        resp = asyncio.run(handle_nomadnet_auth_remove(request))
        data = _parse_response(resp)

        assert data["ok"] is True
        assert data["data"]["removed"] is True
        nn.remove_allowed_identity.assert_called_once_with("abcdef1234567890")

    def test_missing_identity(self):
        nn = MagicMock()

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = nn
        request = _make_request(body={}, plugin_mock=plugin)

        resp = asyncio.run(handle_nomadnet_auth_remove(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert "identity" in data["error"].lower()

    def test_identity_too_long(self):
        nn = MagicMock()

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = nn
        request = _make_request(body={"identity": "x" * 129}, plugin_mock=plugin)

        resp = asyncio.run(handle_nomadnet_auth_remove(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert "too long" in data["error"]

    def test_plugin_not_available(self):
        plugin = MagicMock()
        plugin.app.get_plugin.return_value = None
        request = _make_request(body={"identity": "abc123"}, plugin_mock=plugin)

        resp = asyncio.run(handle_nomadnet_auth_remove(request))
        data = _parse_response(resp)

        assert resp.status == 503
        assert "not available" in data["error"]

    def test_remove_nonexistent_identity(self):
        """Removing an identity that doesn't exist returns removed=False gracefully."""
        nn = MagicMock()
        nn.remove_allowed_identity.return_value = False

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = nn
        request = _make_request(body={"identity": "notfound"}, plugin_mock=plugin)

        resp = asyncio.run(handle_nomadnet_auth_remove(request))
        data = _parse_response(resp)

        assert data["ok"] is True
        assert data["data"]["removed"] is False

    def test_invalid_request_body(self):
        nn = MagicMock()

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = nn
        request = _make_request(body=None, plugin_mock=plugin)

        resp = asyncio.run(handle_nomadnet_auth_remove(request))
        _parse_response(resp)

        assert resp.status == 400


# ── handle_send_message ─────────────────────────────────────────────────


class TestHandleSendMessage:
    """Tests for POST /api/messages/send."""

    def test_success(self):
        hub = MagicMock()
        hub.send_message.return_value = {"sent": True, "id": "msg001"}

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = hub
        body = {"transport": "lxmf", "text": "Hello mesh!", "destination": "abcd1234"}
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_send_message(request))
        data = _parse_response(resp)

        assert data["ok"] is True
        assert data["data"]["sent"] is True
        hub.send_message.assert_called_once_with("lxmf", "Hello mesh!", "abcd1234")

    def test_missing_transport(self):
        hub = MagicMock()

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = hub
        body = {"text": "Hello", "destination": "abcd1234"}
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_send_message(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert "transport" in data["error"].lower()

    def test_missing_text(self):
        hub = MagicMock()

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = hub
        body = {"transport": "lxmf", "destination": "abcd1234"}
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_send_message(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert "text" in data["error"].lower()

    def test_empty_text_after_strip(self):
        hub = MagicMock()

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = hub
        body = {"transport": "lxmf", "text": "   ", "destination": "abcd1234"}
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_send_message(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert "text" in data["error"].lower()

    def test_missing_destination(self):
        hub = MagicMock()

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = hub
        body = {"transport": "lxmf", "text": "Hello"}
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_send_message(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert "destination" in data["error"].lower()

    def test_text_too_long(self):
        hub = MagicMock()

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = hub
        body = {"transport": "lxmf", "text": "x" * 5001, "destination": "abcd1234"}
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_send_message(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert "maximum length" in data["error"] or "5000" in data["error"]

    def test_text_at_max_length_accepted(self):
        """5000 characters is the boundary -- should be accepted."""
        hub = MagicMock()
        hub.send_message.return_value = {"sent": True}

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = hub
        body = {"transport": "lxmf", "text": "x" * 5000, "destination": "abcd1234"}
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_send_message(request))
        data = _parse_response(resp)

        assert data["ok"] is True

    def test_hub_not_available(self):
        plugin = MagicMock()
        plugin.app.get_plugin.return_value = None
        body = {"transport": "lxmf", "text": "Hello", "destination": "abcd1234"}
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_send_message(request))
        data = _parse_response(resp)

        assert resp.status == 503
        assert data["ok"] is False
        assert "not enabled" in data["error"].lower()

    def test_hub_missing_send_method(self):
        hub = MagicMock(spec=[])  # no attributes
        plugin = MagicMock()
        plugin.app.get_plugin.return_value = hub
        body = {"transport": "lxmf", "text": "Hello", "destination": "abcd1234"}
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_send_message(request))
        _parse_response(resp)

        assert resp.status == 503

    def test_send_failure(self):
        """When hub.send_message returns sent=False with a reason."""
        hub = MagicMock()
        hub.send_message.return_value = {"sent": False, "reason": "No route to destination"}

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = hub
        body = {"transport": "lxmf", "text": "Hello", "destination": "deadbeef"}
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_send_message(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert data["ok"] is False
        assert "No route" in data["error"]

    def test_send_failure_no_reason(self):
        """When hub.send_message returns sent=False without a reason field."""
        hub = MagicMock()
        hub.send_message.return_value = {"sent": False}

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = hub
        body = {"transport": "lxmf", "text": "Hello", "destination": "deadbeef"}
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_send_message(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert data["ok"] is False
        assert "Send failed" in data["error"]

    def test_invalid_json_body(self):
        hub = MagicMock()

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = hub
        request = _make_request(body=None, plugin_mock=plugin)

        resp = asyncio.run(handle_send_message(request))
        data = _parse_response(resp)

        assert resp.status == 400
        assert "Invalid JSON" in data["error"]

    def test_meshtastic_transport(self):
        """Non-lxmf transport should also work."""
        hub = MagicMock()
        hub.send_message.return_value = {"sent": True, "transport": "meshtastic"}

        plugin = MagicMock()
        plugin.app.get_plugin.return_value = hub
        body = {"transport": "meshtastic", "text": "Mesh message", "destination": "!aabbccdd"}
        request = _make_request(body=body, plugin_mock=plugin)

        resp = asyncio.run(handle_send_message(request))
        data = _parse_response(resp)

        assert data["ok"] is True
        hub.send_message.assert_called_once_with("meshtastic", "Mesh message", "!aabbccdd")


# ── Off-grid endpoints ────────────────────────────────────────────────


class TestOffgridEndpoints:
    def test_offgrid_get(self):
        plugin = MagicMock()
        plugin.app.offgrid_mode = False
        request = _make_request(plugin_mock=plugin)
        resp = asyncio.run(handle_offgrid_get(request))
        data = _parse_response(resp)
        assert data["ok"] is True
        assert data["data"]["enabled"] is False

    def test_offgrid_set_valid(self):
        plugin = MagicMock()
        plugin.app.set_offgrid_mode.return_value = {"enabled": True}
        request = _make_request(body={"enabled": True}, plugin_mock=plugin)
        resp = asyncio.run(handle_offgrid_set(request))
        data = _parse_response(resp)
        assert data["ok"] is True
        assert data["data"]["enabled"] is True

    def test_offgrid_set_non_boolean(self):
        plugin = MagicMock()
        request = _make_request(body={"enabled": "true"}, plugin_mock=plugin)
        resp = asyncio.run(handle_offgrid_set(request))
        data = _parse_response(resp)
        assert data["ok"] is False
        assert "boolean" in data["error"]
        plugin.app.set_offgrid_mode.assert_not_called()

    def test_offgrid_set_missing_field(self):
        plugin = MagicMock()
        request = _make_request(body={}, plugin_mock=plugin)
        resp = asyncio.run(handle_offgrid_set(request))
        data = _parse_response(resp)
        assert data["ok"] is False
        assert "required" in data["error"]


# ── Auth gate negative tests ──────────────────────────────────────────────


class TestAuthGateNegative:
    """Verify that write endpoints reject requests without a valid token.

    The _make_request helper now uses a real dict for item access, so
    token=None genuinely produces a falsy ``request.get("token")``.
    """

    def test_services_restart_requires_token(self):
        request = _make_request(token=None)
        resp = asyncio.run(handle_services_restart(request))
        data = _parse_response(resp)
        assert resp.status == 401
        assert data["ok"] is False
        assert "Authentication required" in data["error"]

    def test_spectrum_switch_preset_requires_token(self):
        request = _make_request(body={"preset": "aviation"}, token=None)
        resp = asyncio.run(handle_spectrum_switch_preset(request))
        data = _parse_response(resp)
        assert resp.status == 401
        assert data["ok"] is False
        assert "Authentication required" in data["error"]

    def test_send_message_requires_token_by_default(self):
        """handle_send_message rejects unauthenticated callers unless
        allow_localhost_send is explicitly enabled."""
        plugin = MagicMock()
        plugin.app.get_plugin.return_value = MagicMock()
        plugin.config = {"allow_localhost_send": False}
        request = _make_request(
            body={"transport": "lxmf", "text": "hi", "destination": "abc123"},
            plugin_mock=plugin,
            token=None,
        )
        resp = asyncio.run(handle_send_message(request))
        data = _parse_response(resp)
        assert resp.status == 401
        assert data["ok"] is False
