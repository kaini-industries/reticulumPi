"""Tests for the Meshtastic Gateway plugin."""

from __future__ import annotations

import sys
import time
from unittest.mock import MagicMock, patch

import pytest
import RNS as _RNS


# ---------------------------------------------------------------------------
# Mock the meshtastic and pubsub packages before any plugin imports
# ---------------------------------------------------------------------------

_mock_meshtastic = MagicMock()
_mock_meshtastic_serial = MagicMock()
_mock_meshtastic.serial_interface = _mock_meshtastic_serial
_mock_meshtastic_protobuf = MagicMock()
_mock_meshtastic_mesh_pb2 = MagicMock()
_mock_meshtastic_mqtt_pb2 = MagicMock()
_mock_meshtastic_portnums_pb2 = MagicMock()
_mock_meshtastic.protobuf = _mock_meshtastic_protobuf
_mock_meshtastic_protobuf.mesh_pb2 = _mock_meshtastic_mesh_pb2
_mock_meshtastic_protobuf.mqtt_pb2 = _mock_meshtastic_mqtt_pb2
_mock_meshtastic_protobuf.portnums_pb2 = _mock_meshtastic_portnums_pb2
_mock_pubsub = MagicMock()
_mock_pub = MagicMock()
_mock_pubsub.pub = _mock_pub
_mock_paho = MagicMock()
_mock_paho_client = MagicMock()
_mock_paho.client = _mock_paho_client
_mock_crypto = MagicMock()
_mock_crypto_ciphers = MagicMock()


@pytest.fixture(autouse=True)
def _patch_meshtastic():
    """Ensure meshtastic and pubsub are always available as mocks."""
    with patch.dict(sys.modules, {
        "meshtastic": _mock_meshtastic,
        "meshtastic.serial_interface": _mock_meshtastic_serial,
        "meshtastic.protobuf": _mock_meshtastic_protobuf,
        "meshtastic.protobuf.mesh_pb2": _mock_meshtastic_mesh_pb2,
        "meshtastic.protobuf.mqtt_pb2": _mock_meshtastic_mqtt_pb2,
        "meshtastic.protobuf.portnums_pb2": _mock_meshtastic_portnums_pb2,
        "meshtastic.mesh_pb2": _mock_meshtastic_mesh_pb2,
        "meshtastic.mqtt_pb2": _mock_meshtastic_mqtt_pb2,
        "meshtastic.portnums_pb2": _mock_meshtastic_portnums_pb2,
        "pubsub": _mock_pubsub,
        "pubsub.pub": _mock_pub,
        "paho": _mock_paho,
        "paho.mqtt": _mock_paho,
        "paho.mqtt.client": _mock_paho_client,
        "cryptography": _mock_crypto,
        "cryptography.hazmat": MagicMock(),
        "cryptography.hazmat.primitives": MagicMock(),
        "cryptography.hazmat.primitives.ciphers": _mock_crypto_ciphers,
    }):
        yield


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def gw_config(tmp_path):
    """Base config dict for the meshtastic_gateway plugin (serial mode)."""
    return {
        "enabled": True,
        "mode": "serial",
        "serial_port": "/dev/ttyUSB0",
        "meshtastic_channel": 0,
        "lxmf_recipients": [],
        "meshtastic_allow_list": [],
        "lxmf_allow_list": [],
        "storage_path": str(tmp_path / "mesh_gw_lxmf"),
        "health_check_interval": 15,
        "reconnect_delay": 5,
        "max_reconnect_attempts": 3,
        "max_messages_per_minute": 0,
    }


@pytest.fixture
def mqtt_gw_config(tmp_path):
    """Config dict for MQTT mode."""
    return {
        "enabled": True,
        "mode": "mqtt",
        "mqtt": {
            "broker": "mqtt.meshtastic.org",
            "port": 1883,
            "username": "meshdev",
            "password": "large4cats",
            "root_topic": "msh/US/2/e/LongFast",
            "channel_key": "AQ==",
        },
        "meshtastic_channel": 0,
        "lxmf_recipients": [],
        "meshtastic_allow_list": [],
        "lxmf_allow_list": [],
        "storage_path": str(tmp_path / "mqtt_gw_lxmf"),
        "health_check_interval": 15,
        "reconnect_delay": 5,
        "max_reconnect_attempts": 3,
        "max_messages_per_minute": 0,
    }


def _make_mock_mesh_interface():
    """Create a mock Meshtastic interface (works for both serial and MQTT)."""
    iface = MagicMock()
    iface.nodes = {
        "!abcd1234": {
            "user": {"longName": "TestNode1", "shortName": "TN1", "hwModel": "RAK4631"},
            "snr": 5.5,
            "lastHeard": 1700000000,
            "position": {"latitude": 30.0, "longitude": -97.0},
        },
        "!beef5678": {
            "user": {"longName": "TestNode2", "shortName": "TN2", "hwModel": "HELTEC_V3"},
            "snr": -2.0,
            "lastHeard": 1700001000,
            "position": {},
        },
    }
    iface.myInfo = MagicMock()
    iface.myInfo.my_node_num = 0x12345678
    iface.stream = MagicMock()
    iface.stream.is_open = True
    # MQTT client mock
    iface.client = MagicMock()
    iface.client.is_connected.return_value = True
    return iface


def _create_started_plugin(mock_app, config):
    """Create and start a MeshtasticGateway with all mocks active."""
    with (
        patch("LXMF.LXMRouter") as mock_router_cls,
        patch("RNS.Identity") as mock_identity_cls,
        patch.object(_RNS.Transport, "register_announce_handler"),
        patch.object(_RNS.Transport, "deregister_announce_handler"),
    ):
        mock_router = MagicMock()
        mock_dest = MagicMock()
        mock_dest.hash = b"\x03" * 16
        mock_router.register_delivery_identity.return_value = mock_dest
        mock_router_cls.return_value = mock_router

        mock_identity = MagicMock()
        mock_identity.hash = b"\x04" * 16
        mock_identity_cls.return_value = mock_identity

        from reticulumpi.builtin_plugins.meshtastic_gateway import MeshtasticGateway

        plugin = MeshtasticGateway(mock_app, config)
        plugin.start()
        yield plugin
        plugin._active = False
        plugin._join_threads()


@pytest.fixture
def gateway_plugin(mock_app, gw_config):
    """Create a serial-mode MeshtasticGateway plugin, started and stopped."""
    yield from _create_started_plugin(mock_app, gw_config)


@pytest.fixture
def mqtt_gateway_plugin(mock_app, mqtt_gw_config):
    """Create an MQTT-mode MeshtasticGateway plugin, started and stopped."""
    yield from _create_started_plugin(mock_app, mqtt_gw_config)


def _make_plugin_no_start(mock_app, config):
    """Construct a MeshtasticGateway without calling start() — for config validation tests."""
    with (
        patch("LXMF.LXMRouter"),
        patch("RNS.Identity"),
        patch.object(_RNS.Transport, "register_announce_handler"),
        patch.object(_RNS.Transport, "deregister_announce_handler"),
    ):
        from reticulumpi.builtin_plugins.meshtastic_gateway import MeshtasticGateway

        return MeshtasticGateway(mock_app, config)


# ---------------------------------------------------------------------------
# TestValidateConfig
# ---------------------------------------------------------------------------


class TestValidateConfig:
    def test_raises_when_meshtastic_not_installed(self, mock_app, gw_config):
        """ValueError with install instructions when meshtastic is missing."""
        with patch.dict(sys.modules, {"meshtastic": None}):
            import importlib

            from reticulumpi.builtin_plugins import meshtastic_gateway as mg

            importlib.reload(mg)

            with (
                patch("LXMF.LXMRouter"),
                patch("RNS.Identity"),
                patch.object(_RNS.Transport, "register_announce_handler"),
                patch.object(_RNS.Transport, "deregister_announce_handler"),
            ):
                with pytest.raises(ValueError, match="meshtastic package not found"):
                    mg.MeshtasticGateway(mock_app, gw_config)

    def test_raises_on_invalid_mode(self, mock_app, gw_config):
        gw_config["mode"] = "bluetooth"
        with pytest.raises(ValueError, match="mode must be"):
            _make_plugin_no_start(mock_app, gw_config)

    def test_raises_on_invalid_channel(self, mock_app, gw_config):
        gw_config["meshtastic_channel"] = 8
        with pytest.raises(ValueError, match="meshtastic_channel"):
            _make_plugin_no_start(mock_app, gw_config)

    def test_raises_on_negative_channel(self, mock_app, gw_config):
        gw_config["meshtastic_channel"] = -1
        with pytest.raises(ValueError, match="meshtastic_channel"):
            _make_plugin_no_start(mock_app, gw_config)

    def test_raises_on_invalid_health_check(self, mock_app, gw_config):
        gw_config["health_check_interval"] = 2
        with pytest.raises(ValueError, match="health_check_interval"):
            _make_plugin_no_start(mock_app, gw_config)

    def test_raises_on_invalid_reconnect_delay(self, mock_app, gw_config):
        gw_config["reconnect_delay"] = 0
        with pytest.raises(ValueError, match="reconnect_delay"):
            _make_plugin_no_start(mock_app, gw_config)

    def test_raises_on_invalid_lxmf_recipient(self, mock_app, gw_config):
        gw_config["lxmf_recipients"] = ["not-hex"]
        with pytest.raises(ValueError, match="Invalid LXMF recipient hash"):
            _make_plugin_no_start(mock_app, gw_config)

    def test_raises_on_invalid_mesh_node_id(self, mock_app, gw_config):
        gw_config["meshtastic_allow_list"] = ["abcd1234"]  # Missing !
        with pytest.raises(ValueError, match="Invalid Meshtastic node ID"):
            _make_plugin_no_start(mock_app, gw_config)

    def test_raises_on_invalid_rate_limit(self, mock_app, gw_config):
        gw_config["max_messages_per_minute"] = -1
        with pytest.raises(ValueError, match="max_messages_per_minute"):
            _make_plugin_no_start(mock_app, gw_config)

    def test_valid_config_passes(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        assert plugin.plugin_name == "meshtastic_gateway"

    def test_auto_serial_port_accepted(self, mock_app, gw_config):
        gw_config["serial_port"] = "auto"
        plugin = _make_plugin_no_start(mock_app, gw_config)
        assert plugin is not None


class TestValidateConfigMqtt:
    """MQTT-specific configuration validation."""

    def test_mqtt_mode_valid_config(self, mock_app, mqtt_gw_config):
        plugin = _make_plugin_no_start(mock_app, mqtt_gw_config)
        assert plugin is not None

    def test_mqtt_mode_empty_broker(self, mock_app, mqtt_gw_config):
        mqtt_gw_config["mqtt"]["broker"] = ""
        with pytest.raises(ValueError, match="mqtt.broker"):
            _make_plugin_no_start(mock_app, mqtt_gw_config)

    def test_mqtt_mode_invalid_port(self, mock_app, mqtt_gw_config):
        mqtt_gw_config["mqtt"]["port"] = 0
        with pytest.raises(ValueError, match="mqtt.port"):
            _make_plugin_no_start(mock_app, mqtt_gw_config)

    def test_mqtt_mode_port_too_high(self, mock_app, mqtt_gw_config):
        mqtt_gw_config["mqtt"]["port"] = 99999
        with pytest.raises(ValueError, match="mqtt.port"):
            _make_plugin_no_start(mock_app, mqtt_gw_config)

    def test_mqtt_mode_missing_mqtt_section_uses_defaults(self, mock_app, mqtt_gw_config):
        """mqtt sub-section is optional — defaults are used."""
        del mqtt_gw_config["mqtt"]
        plugin = _make_plugin_no_start(mock_app, mqtt_gw_config)
        assert plugin is not None

    def test_mqtt_mode_rejects_missing_paho(self, mock_app, mqtt_gw_config):
        """ValueError when paho-mqtt is not importable."""
        with patch.dict(sys.modules, {"paho": None, "paho.mqtt": None, "paho.mqtt.client": None}):
            with (
                patch("LXMF.LXMRouter"),
                patch("RNS.Identity"),
                patch.object(_RNS.Transport, "register_announce_handler"),
                patch.object(_RNS.Transport, "deregister_announce_handler"),
            ):
                from reticulumpi.builtin_plugins.meshtastic_gateway import MeshtasticGateway

                with pytest.raises(ValueError, match="paho-mqtt"):
                    MeshtasticGateway(mock_app, mqtt_gw_config)


# ---------------------------------------------------------------------------
# TestStart
# ---------------------------------------------------------------------------


class TestStart:
    def test_start_initializes_state(self, gateway_plugin):
        assert gateway_plugin._active is True
        # Connection thread may or may not have connected yet (mock succeeds instantly)
        assert gateway_plugin._msgs_mesh_to_lxmf == 0
        assert gateway_plugin._msgs_lxmf_to_mesh == 0
        assert gateway_plugin._msgs_rate_limited == 0

    def test_start_stores_mode(self, gateway_plugin):
        assert gateway_plugin._mode == "serial"

    def test_mqtt_mode_stored(self, mqtt_gateway_plugin):
        assert mqtt_gateway_plugin._mode == "mqtt"

    def test_start_creates_lxmf_router(self, gateway_plugin):
        assert gateway_plugin.lxmf_router is not None
        assert gateway_plugin.local_lxmf_destination is not None

    def test_start_parses_recipients(self, mock_app, gw_config):
        gw_config["lxmf_recipients"] = ["aa" * 16, "bb" * 16]
        with (
            patch("LXMF.LXMRouter") as mock_router_cls,
            patch("RNS.Identity") as mock_id_cls,
            patch.object(_RNS.Transport, "register_announce_handler"),
            patch.object(_RNS.Transport, "deregister_announce_handler"),
        ):
            mock_router = MagicMock()
            mock_dest = MagicMock()
            mock_dest.hash = b"\x03" * 16
            mock_router.register_delivery_identity.return_value = mock_dest
            mock_router_cls.return_value = mock_router
            mock_id = MagicMock()
            mock_id.hash = b"\x04" * 16
            mock_id_cls.return_value = mock_id

            from reticulumpi.builtin_plugins.meshtastic_gateway import MeshtasticGateway

            plugin = MeshtasticGateway(mock_app, gw_config)
            plugin.start()
            try:
                assert len(plugin._recipient_hashes) == 2
                assert plugin._recipient_hashes[0] == bytes.fromhex("aa" * 16)
            finally:
                plugin._active = False
                plugin._join_threads()

    def test_rate_limit_interval_set(self, mock_app, gw_config):
        gw_config["max_messages_per_minute"] = 6
        for plugin in _create_started_plugin(mock_app, gw_config):
            assert plugin._send_min_interval == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# TestMeshToLxmf
# ---------------------------------------------------------------------------


class TestMeshToLxmf:
    def _make_packet(self, from_id="!abcd1234", from_num=0xABCD1234, text="Hello mesh"):
        return {
            "from": from_num,
            "fromId": from_id,
            "to": 0xFFFFFFFF,
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "payload": text.encode("utf-8"),
            },
        }

    def test_text_message_increments_stats(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        gateway_plugin._on_mesh_text(self._make_packet())
        assert gateway_plugin._msgs_mesh_to_lxmf == 1
        assert gateway_plugin._last_mesh_msg_time is not None

    def test_message_includes_sender_info(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()

        forwarded = []
        gateway_plugin._forward_to_lxmf = lambda text: forwarded.append(text)

        gateway_plugin._on_mesh_text(self._make_packet())
        assert len(forwarded) == 1
        assert "[Mesh]" in forwarded[0]
        assert "TestNode1" in forwarded[0]
        assert "!abcd1234" in forwarded[0]
        assert "Hello mesh" in forwarded[0]

    def test_allow_list_blocks_unknown_sender(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        gateway_plugin._mesh_allow_set = {"!deadbeef"}
        gateway_plugin._on_mesh_text(self._make_packet(from_id="!abcd1234"))
        assert gateway_plugin._msgs_mesh_to_lxmf == 0

    def test_allow_list_permits_listed_sender(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        gateway_plugin._mesh_allow_set = {"!abcd1234"}
        gateway_plugin._on_mesh_text(self._make_packet(from_id="!abcd1234"))
        assert gateway_plugin._msgs_mesh_to_lxmf == 1

    def test_empty_allow_list_accepts_all(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        gateway_plugin._mesh_allow_set = set()
        gateway_plugin._on_mesh_text(self._make_packet(from_id="!anything0"))
        assert gateway_plugin._msgs_mesh_to_lxmf == 1

    def test_empty_text_ignored(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        gateway_plugin._on_mesh_text(self._make_packet(text="   "))
        assert gateway_plugin._msgs_mesh_to_lxmf == 0

    def test_event_published(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        gateway_plugin._on_mesh_text(self._make_packet())
        calls = [c for c in gateway_plugin.event_bus.publish.call_args_list
                 if c[0][0] == "meshtastic.message_received"]
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# TestLxmfToMesh
# ---------------------------------------------------------------------------


class TestLxmfToMesh:
    def _make_lxmf_message(self, content="Hello from Reticulum", source_hash=b"\xaa" * 16):
        msg = MagicMock()
        msg.source_hash = source_hash
        msg.content_as_string.return_value = content
        msg.source = MagicMock()
        return msg

    def test_lxmf_message_forwarded_to_meshtastic(self, gateway_plugin):
        mock_iface = _make_mock_mesh_interface()
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = mock_iface
        gateway_plugin._handle_lxmf_message(self._make_lxmf_message())
        mock_iface.sendText.assert_called_once()
        assert gateway_plugin._msgs_lxmf_to_mesh == 1

    def test_message_not_sent_when_disconnected(self, gateway_plugin):
        mock_iface = _make_mock_mesh_interface()
        gateway_plugin._connected = False
        gateway_plugin._mesh_interface = mock_iface
        gateway_plugin._handle_lxmf_message(self._make_lxmf_message())
        mock_iface.sendText.assert_not_called()
        assert gateway_plugin._msgs_lxmf_to_mesh == 0

    def test_lxmf_allow_list_blocks_unauthorized(self, gateway_plugin):
        mock_iface = _make_mock_mesh_interface()
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = mock_iface
        gateway_plugin._lxmf_allow_set = {"bb" * 16}
        gateway_plugin._handle_lxmf_message(self._make_lxmf_message(source_hash=b"\xaa" * 16))
        mock_iface.sendText.assert_not_called()

    def test_lxmf_allow_list_permits_authorized(self, gateway_plugin):
        mock_iface = _make_mock_mesh_interface()
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = mock_iface
        gateway_plugin._lxmf_allow_set = {"aa" * 16}
        gateway_plugin._handle_lxmf_message(self._make_lxmf_message(source_hash=b"\xaa" * 16))
        mock_iface.sendText.assert_called_once()

    def test_stats_incremented(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        gateway_plugin._handle_lxmf_message(self._make_lxmf_message())
        gateway_plugin._handle_lxmf_message(self._make_lxmf_message())
        assert gateway_plugin._msgs_lxmf_to_mesh == 2
        assert gateway_plugin._last_lxmf_msg_time is not None

    def test_channel_from_config(self, mock_app, gw_config):
        gw_config["meshtastic_channel"] = 3
        with (
            patch("LXMF.LXMRouter") as mock_router_cls,
            patch("RNS.Identity") as mock_id_cls,
            patch.object(_RNS.Transport, "register_announce_handler"),
            patch.object(_RNS.Transport, "deregister_announce_handler"),
        ):
            mock_router = MagicMock()
            mock_dest = MagicMock()
            mock_dest.hash = b"\x03" * 16
            mock_router.register_delivery_identity.return_value = mock_dest
            mock_router_cls.return_value = mock_router
            mock_id = MagicMock()
            mock_id.hash = b"\x04" * 16
            mock_id_cls.return_value = mock_id

            from reticulumpi.builtin_plugins.meshtastic_gateway import MeshtasticGateway

            plugin = MeshtasticGateway(mock_app, gw_config)
            plugin.start()
            try:
                mock_iface = _make_mock_mesh_interface()
                plugin._connected = True
                plugin._mesh_interface = mock_iface
                msg = MagicMock()
                msg.source_hash = b"\xaa" * 16
                msg.content_as_string.return_value = "Test"
                plugin._handle_lxmf_message(msg)
                call_kwargs = mock_iface.sendText.call_args
                assert call_kwargs[1]["channelIndex"] == 3
            finally:
                plugin._active = False
                plugin._join_threads()


# ---------------------------------------------------------------------------
# TestNodeResolution
# ---------------------------------------------------------------------------


class TestNodeResolution:
    def test_resolves_known_node(self, gateway_plugin):
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        name = gateway_plugin._resolve_mesh_node_name(0xABCD1234)
        assert name == "TestNode1"

    def test_returns_none_for_unknown_node(self, gateway_plugin):
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        name = gateway_plugin._resolve_mesh_node_name(0x99999999)
        assert name is None

    def test_handles_no_interface(self, gateway_plugin):
        gateway_plugin._mesh_interface = None
        name = gateway_plugin._resolve_mesh_node_name(0xABCD1234)
        assert name is None

    def test_handles_missing_nodes_dict(self, gateway_plugin):
        gateway_plugin._mesh_interface = MagicMock(spec=[])  # No 'nodes' attribute
        name = gateway_plugin._resolve_mesh_node_name(0xABCD1234)
        assert name is None


# ---------------------------------------------------------------------------
# TestGetStatus
# ---------------------------------------------------------------------------


class TestGetStatus:
    def test_status_when_connected(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        status = gateway_plugin.get_status()
        assert status["active"] is True
        assert status["connected"] is True
        assert status["mode"] == "serial"
        assert status["serial_port"] == "/dev/ttyUSB0"
        assert status["meshtastic_channel"] == 0
        assert status["meshtastic_nodes"] == 2

    def test_status_when_disconnected(self, gateway_plugin):
        # Force disconnected state (connection thread may have auto-connected)
        gateway_plugin._connected = False
        gateway_plugin._mesh_interface = None
        status = gateway_plugin.get_status()
        assert status["active"] is True
        assert status["connected"] is False
        assert "meshtastic_nodes" not in status

    def test_status_includes_message_counts(self, gateway_plugin):
        gateway_plugin._msgs_mesh_to_lxmf = 5
        gateway_plugin._msgs_lxmf_to_mesh = 3
        gateway_plugin._msgs_rate_limited = 1
        status = gateway_plugin.get_status()
        assert status["msgs_mesh_to_lxmf"] == 5
        assert status["msgs_lxmf_to_mesh"] == 3
        assert status["msgs_rate_limited"] == 1

    def test_mqtt_status_fields(self, mqtt_gateway_plugin):
        status = mqtt_gateway_plugin.get_status()
        assert status["mode"] == "mqtt"
        assert status["mqtt_broker"] == "mqtt.meshtastic.org"
        assert status["mqtt_topic"] == "msh/US/2/e/LongFast"
        assert "serial_port" not in status

    def test_rate_limit_in_status(self, mock_app, gw_config):
        gw_config["max_messages_per_minute"] = 4
        for plugin in _create_started_plugin(mock_app, gw_config):
            status = plugin.get_status()
            assert status["rate_limit_per_min"] == 4.0

    def test_no_rate_limit_field_when_unlimited(self, gateway_plugin):
        status = gateway_plugin.get_status()
        assert "rate_limit_per_min" not in status


# ---------------------------------------------------------------------------
# TestGetMeshtasticNodes
# ---------------------------------------------------------------------------


class TestGetMeshtasticNodes:
    def test_returns_node_list(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        nodes = gateway_plugin.get_meshtastic_nodes()
        assert len(nodes) == 2
        ids = {n["id"] for n in nodes}
        assert "!abcd1234" in ids
        assert "!beef5678" in ids

    def test_returns_empty_when_disconnected(self, gateway_plugin):
        gateway_plugin._connected = False
        gateway_plugin._mesh_interface = None
        assert gateway_plugin.get_meshtastic_nodes() == []

    def test_node_fields_present(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        nodes = gateway_plugin.get_meshtastic_nodes()
        node = next(n for n in nodes if n["id"] == "!abcd1234")
        assert node["long_name"] == "TestNode1"
        assert node["short_name"] == "TN1"
        assert node["hw_model"] == "RAK4631"
        assert node["snr"] == 5.5
        assert node["last_heard"] == 1700000000
        assert node["latitude"] == 30.0
        assert node["longitude"] == -97.0


# ---------------------------------------------------------------------------
# TestConnectionManagement
# ---------------------------------------------------------------------------


class TestConnectionManagement:
    def test_close_sets_disconnected(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        gateway_plugin._close_mesh_interface()
        assert gateway_plugin._connected is False
        assert gateway_plugin._mesh_interface is None

    def test_close_calls_interface_close(self, gateway_plugin):
        mock_iface = _make_mock_mesh_interface()
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = mock_iface
        gateway_plugin._close_mesh_interface()
        mock_iface.close.assert_called_once()

    def test_close_idempotent(self, gateway_plugin):
        gateway_plugin._connected = False
        gateway_plugin._mesh_interface = None
        gateway_plugin._close_mesh_interface()  # Should not raise
        assert gateway_plugin._connected is False

    def test_on_mesh_disconnect_sets_flag(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._on_mesh_disconnect()
        assert gateway_plugin._connected is False

    def test_on_mesh_disconnect_records_time(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._last_disconnect_time = 0.0
        gateway_plugin._on_mesh_disconnect()
        assert gateway_plugin._last_disconnect_time > 0

    def test_on_mesh_connect_restores_connected(self, gateway_plugin):
        """When paho auto-reconnects, _on_mesh_connect must set _connected = True."""
        gateway_plugin._connected = False
        gateway_plugin._on_mesh_connect()
        assert gateway_plugin._connected is True

    def test_on_mesh_connect_publishes_event_on_reconnect(self, gateway_plugin):
        """Auto-reconnect fires MESHTASTIC_CONNECTED event."""
        gateway_plugin._connected = False
        gateway_plugin._on_mesh_connect()
        gateway_plugin.event_bus.publish.assert_called()
        # Find the MESHTASTIC_CONNECTED call
        from reticulumpi import events
        found = any(
            call.args[0] == events.MESHTASTIC_CONNECTED
            for call in gateway_plugin.event_bus.publish.call_args_list
        )
        assert found, "Expected MESHTASTIC_CONNECTED event on auto-reconnect"

    def test_on_mesh_connect_no_event_when_already_connected(self, gateway_plugin):
        """No duplicate event when _on_mesh_connect fires while already connected."""
        gateway_plugin._connected = True
        gateway_plugin.event_bus.publish.reset_mock()
        gateway_plugin._on_mesh_connect()
        # Should NOT publish MESHTASTIC_CONNECTED
        from reticulumpi import events
        connected_calls = [
            c for c in gateway_plugin.event_bus.publish.call_args_list
            if c.args[0] == events.MESHTASTIC_CONNECTED
        ]
        assert len(connected_calls) == 0


# ---------------------------------------------------------------------------
# TestMqttMode
# ---------------------------------------------------------------------------


class TestMqttMode:
    def test_mqtt_plugin_starts(self, mqtt_gateway_plugin):
        assert mqtt_gateway_plugin._active is True
        assert mqtt_gateway_plugin._mode == "mqtt"

    def test_mqtt_status_connected(self, mqtt_gateway_plugin):
        mqtt_gateway_plugin._connected = True
        mqtt_gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        status = mqtt_gateway_plugin.get_status()
        assert status["mode"] == "mqtt"
        assert status["mqtt_broker"] == "mqtt.meshtastic.org"
        assert status["mqtt_topic"] == "msh/US/2/e/LongFast"

    def test_mqtt_text_message_works(self, mqtt_gateway_plugin):
        """MQTT mode receives text via same pubsub callbacks."""
        mqtt_gateway_plugin._connected = True
        mqtt_gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        packet = {
            "from": 0xABCD1234,
            "fromId": "!abcd1234",
            "to": 0xFFFFFFFF,
            "decoded": {"payload": b"Hello from MQTT"},
        }
        mqtt_gateway_plugin._on_mesh_text(packet)
        assert mqtt_gateway_plugin._msgs_mesh_to_lxmf == 1

    def test_mqtt_lxmf_to_mesh(self, mqtt_gateway_plugin):
        """LXMF messages are sent to Meshtastic via MQTT interface."""
        mock_iface = _make_mock_mesh_interface()
        mqtt_gateway_plugin._connected = True
        mqtt_gateway_plugin._mesh_interface = mock_iface
        msg = MagicMock()
        msg.source_hash = b"\xaa" * 16
        msg.content_as_string.return_value = "Test via MQTT"
        mqtt_gateway_plugin._handle_lxmf_message(msg)
        mock_iface.sendText.assert_called_once()
        assert mqtt_gateway_plugin._msgs_lxmf_to_mesh == 1

    def test_mqtt_health_check_uses_client(self, mqtt_gateway_plugin):
        """MQTT health check queries the paho client."""
        mock_iface = _make_mock_mesh_interface()
        mock_iface.client.is_connected.return_value = True
        mqtt_gateway_plugin._connected = True
        mqtt_gateway_plugin._mesh_interface = mock_iface
        assert mqtt_gateway_plugin._check_mesh_health() is True

    def test_mqtt_health_check_detects_disconnect(self, mqtt_gateway_plugin):
        """MQTT health check detects paho client disconnect."""
        mock_iface = _make_mock_mesh_interface()
        mock_iface.client.is_connected.return_value = False
        mqtt_gateway_plugin._connected = True
        mqtt_gateway_plugin._mesh_interface = mock_iface
        assert mqtt_gateway_plugin._check_mesh_health() is False


# ---------------------------------------------------------------------------
# TestRateLimiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    def test_rate_limit_blocks_fast_messages(self, mock_app, gw_config):
        """Messages faster than the rate limit are dropped."""
        gw_config["max_messages_per_minute"] = 6  # 1 per 10 seconds
        for plugin in _create_started_plugin(mock_app, gw_config):
            mock_iface = _make_mock_mesh_interface()
            plugin._connected = True
            plugin._mesh_interface = mock_iface

            msg = MagicMock()
            msg.source_hash = b"\xaa" * 16
            msg.content_as_string.return_value = "Test"

            # First message should go through
            plugin._handle_lxmf_message(msg)
            assert mock_iface.sendText.call_count == 1

            # Second message immediately after should be rate-limited
            plugin._handle_lxmf_message(msg)
            assert mock_iface.sendText.call_count == 1  # Still 1
            assert plugin._msgs_rate_limited == 1

    def test_rate_limit_allows_after_interval(self, mock_app, gw_config):
        """Messages are allowed after the rate interval passes."""
        gw_config["max_messages_per_minute"] = 60  # 1 per second
        for plugin in _create_started_plugin(mock_app, gw_config):
            mock_iface = _make_mock_mesh_interface()
            plugin._connected = True
            plugin._mesh_interface = mock_iface

            msg = MagicMock()
            msg.source_hash = b"\xaa" * 16
            msg.content_as_string.return_value = "Test"

            # First message goes through
            plugin._handle_lxmf_message(msg)
            assert mock_iface.sendText.call_count == 1

            # Simulate time passing (beyond 1-second interval)
            plugin._last_send_time = time.time() - 2.0

            # Second message should now go through
            plugin._handle_lxmf_message(msg)
            assert mock_iface.sendText.call_count == 2
            assert plugin._msgs_rate_limited == 0

    def test_no_rate_limit_when_zero(self, gateway_plugin):
        """max_messages_per_minute=0 means unlimited."""
        mock_iface = _make_mock_mesh_interface()
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = mock_iface

        msg = MagicMock()
        msg.source_hash = b"\xaa" * 16
        msg.content_as_string.return_value = "Test"

        # Rapid-fire messages should all go through
        for _ in range(5):
            gateway_plugin._handle_lxmf_message(msg)
        assert mock_iface.sendText.call_count == 5
        assert gateway_plugin._msgs_rate_limited == 0

    def test_rate_limit_count_in_status(self, mock_app, gw_config):
        """Rate-limited count appears in get_status()."""
        gw_config["max_messages_per_minute"] = 6
        for plugin in _create_started_plugin(mock_app, gw_config):
            mock_iface = _make_mock_mesh_interface()
            plugin._connected = True
            plugin._mesh_interface = mock_iface

            msg = MagicMock()
            msg.source_hash = b"\xaa" * 16
            msg.content_as_string.return_value = "Test"

            plugin._handle_lxmf_message(msg)
            plugin._handle_lxmf_message(msg)  # Rate-limited

            status = plugin.get_status()
            assert status["msgs_rate_limited"] == 1
            assert status["rate_limit_per_min"] == 6.0


# ---------------------------------------------------------------------------
# TestMtuHandling
# ---------------------------------------------------------------------------


class TestMtuHandling:
    def test_short_message_not_truncated(self):
        from reticulumpi.builtin_plugins.meshtastic_gateway import _truncate_for_mtu

        result = _truncate_for_mtu("[LXMF] sender:\n", "Hi", 237)
        assert result == "[LXMF] sender:\nHi"
        assert " ..." not in result

    def test_exact_boundary_not_truncated(self):
        from reticulumpi.builtin_plugins.meshtastic_gateway import _truncate_for_mtu

        header = "H:"
        body = "x" * (237 - len(header.encode("utf-8")))
        result = _truncate_for_mtu(header, body, 237)
        assert len(result.encode("utf-8")) == 237
        assert " ..." not in result

    def test_long_message_truncated_with_ellipsis(self):
        from reticulumpi.builtin_plugins.meshtastic_gateway import _truncate_for_mtu

        header = "H:\n"
        body = "A" * 300
        result = _truncate_for_mtu(header, body, 237)
        assert len(result.encode("utf-8")) <= 237
        assert result.endswith(" ...")

    def test_truncation_respects_utf8(self):
        from reticulumpi.builtin_plugins.meshtastic_gateway import _truncate_for_mtu

        header = "H:"
        body = "\u00e9" * 200  # 2 bytes each in UTF-8
        result = _truncate_for_mtu(header, body, 237)
        result.encode("utf-8")  # Should not raise
        assert len(result.encode("utf-8")) <= 237

    def test_header_exceeds_mtu(self):
        from reticulumpi.builtin_plugins.meshtastic_gateway import _truncate_for_mtu

        header = "X" * 250
        result = _truncate_for_mtu(header, "body", 237)
        assert result == header


# ---------------------------------------------------------------------------
# TestForwardToLxmf
# ---------------------------------------------------------------------------


class TestForwardToLxmf:
    def test_no_recipients_does_nothing(self, gateway_plugin):
        gateway_plugin._recipient_hashes = []
        gateway_plugin._forward_to_lxmf("test message")  # Should not raise

    def test_forward_creates_lxmf_message(self, gateway_plugin):
        gateway_plugin._recipient_hashes = [bytes.fromhex("aa" * 16)]
        with patch.object(_RNS.Identity, "recall") as mock_recall:
            mock_identity = MagicMock()
            mock_recall.return_value = mock_identity

            with (
                patch("RNS.Destination") as mock_dest_cls,
                patch("LXMF.LXMessage") as mock_msg_cls,
            ):
                gateway_plugin._forward_to_lxmf("test message")
                mock_dest_cls.assert_called_once()
                mock_msg_cls.assert_called_once()
                gateway_plugin.lxmf_router.handle_outbound.assert_called()

    def test_path_request_on_unknown_identity(self, gateway_plugin):
        gateway_plugin._recipient_hashes = [bytes.fromhex("aa" * 16)]
        with (
            patch.object(_RNS.Identity, "recall", return_value=None),
            patch.object(_RNS.Transport, "request_path") as mock_request,
        ):
            gateway_plugin._forward_to_lxmf("test message")
            mock_request.assert_called_once()


# ---------------------------------------------------------------------------
# TestPersistentIdentity
# ---------------------------------------------------------------------------


class TestPersistentIdentity:
    """Test persistent Meshtastic node number and identity fields."""

    def test_mqtt_node_num_file_created_on_first_start(self, mqtt_gateway_plugin, tmp_path):
        """First start should create meshtastic_node_num file."""
        node_file = tmp_path / "mqtt_gw_lxmf" / "meshtastic_node_num"
        assert node_file.exists()
        content = node_file.read_text().strip()
        assert len(content) == 8  # 8 hex chars
        assert int(content, 16) == mqtt_gateway_plugin._mqtt_node_num

    def test_mqtt_node_num_loaded_on_restart(self, mock_app, mqtt_gw_config, tmp_path):
        """Second start should load existing node number from file."""
        # Write a known node number
        storage = tmp_path / "mqtt_gw_lxmf"
        storage.mkdir(parents=True, exist_ok=True)
        node_file = storage / "meshtastic_node_num"
        node_file.write_text("1a2b3c4d\n")

        for plugin in _create_started_plugin(mock_app, mqtt_gw_config):
            assert plugin._mqtt_node_num == 0x1A2B3C4D
            break

    def test_mqtt_node_num_stable_across_restarts(self, mock_app, mqtt_gw_config):
        """Node number should persist between start/stop cycles."""
        first_num = None
        for plugin in _create_started_plugin(mock_app, mqtt_gw_config):
            first_num = plugin._mqtt_node_num
            break

        for plugin in _create_started_plugin(mock_app, mqtt_gw_config):
            assert plugin._mqtt_node_num == first_num
            break

    def test_serial_mode_skips_persistence(self, gateway_plugin, tmp_path):
        """Serial mode should not create meshtastic_node_num file."""
        node_file = tmp_path / "mesh_gw_lxmf" / "meshtastic_node_num"
        assert not node_file.exists()
        assert gateway_plugin._mqtt_node_num is None

    def test_short_name_from_config(self, mock_app, mqtt_gw_config):
        """Explicit short_name in config should be used."""
        mqtt_gw_config["short_name"] = "RPGW"
        for plugin in _create_started_plugin(mock_app, mqtt_gw_config):
            assert plugin._mqtt_short_name == "RPGW"
            break

    def test_short_name_derived_from_display_name(self, mock_app, mqtt_gw_config):
        """If short_name not set, derive from display_name."""
        mqtt_gw_config["display_name"] = "ReticulumPi Mesh Gateway"
        for plugin in _create_started_plugin(mock_app, mqtt_gw_config):
            assert len(plugin._mqtt_short_name) == 4
            # Initials "RMG" + pad from "ReticulumPiMeshGateway" → "RMGR"
            assert plugin._mqtt_short_name == "RMGR"
            break

    def test_short_name_validation_rejects_too_long(self, mock_app, mqtt_gw_config):
        """short_name > 4 chars should raise ValueError."""
        mqtt_gw_config["short_name"] = "TOOLONG"
        with pytest.raises(ValueError, match="short_name"):
            _make_plugin_no_start(mock_app, mqtt_gw_config)

    def test_display_name_used_as_long_name(self, mock_app, mqtt_gw_config):
        """display_name should map to _mqtt_long_name."""
        mqtt_gw_config["display_name"] = "My Custom Gateway"
        for plugin in _create_started_plugin(mock_app, mqtt_gw_config):
            assert plugin._mqtt_long_name == "My Custom Gateway"
            break

    def test_corrupt_node_num_file_regenerates(self, mock_app, mqtt_gw_config, tmp_path):
        """Corrupt file should be replaced with a valid node number."""
        storage = tmp_path / "mqtt_gw_lxmf"
        storage.mkdir(parents=True, exist_ok=True)
        node_file = storage / "meshtastic_node_num"
        node_file.write_text("not-hex-at-all\n")

        for plugin in _create_started_plugin(mock_app, mqtt_gw_config):
            assert plugin._mqtt_node_num is not None
            assert 0x10000000 <= plugin._mqtt_node_num <= 0x7FFFFFFF
            # File should have been overwritten with valid hex
            content = node_file.read_text().strip()
            assert int(content, 16) == plugin._mqtt_node_num
            break


# ---------------------------------------------------------------------------
# TestNodeInfoAnnouncement
# ---------------------------------------------------------------------------


class TestNodeInfoAnnouncement:
    """Test NODEINFO_APP broadcasting and periodic re-announcement."""

    def test_send_nodeinfo_calls_mqtt_publish(self, mqtt_gateway_plugin):
        """sendNodeInfo on MQTT interface should publish a packet."""
        from reticulumpi.builtin_plugins.meshtastic_gateway import _MeshtasticMQTTClient

        mock_iface = MagicMock()
        mock_iface._my_node_num = 0x12345678
        mock_iface._long_name = "Test GW"
        mock_iface._short_name = "TSGW"
        mock_iface._aes_key = None
        mock_iface._root_topic = "msh/US/2/e/LongFast"
        mock_iface._next_packet_id = 42
        mock_iface._last_nodeinfo_time = 0
        mock_iface._logger = None

        _MeshtasticMQTTClient.sendNodeInfo(mock_iface)
        mock_iface.client.publish.assert_called_once()

        call_args = mock_iface.client.publish.call_args
        topic = call_args[0][0]
        assert topic == "msh/US/2/e/LongFast/!12345678"

    def test_nodeinfo_sent_on_connect(self, mqtt_gateway_plugin):
        """_on_connect callback should trigger sendNodeInfo."""
        mock_iface = MagicMock()
        mock_iface._root_topic = "msh/US/2/e/LongFast"
        mock_iface._logger = None

        from reticulumpi.builtin_plugins.meshtastic_gateway import _MeshtasticMQTTClient
        _MeshtasticMQTTClient._on_connect(mock_iface, MagicMock(), None, None, None)
        mock_iface.sendNodeInfo.assert_called_once()

    def test_periodic_nodeinfo_throttled(self, mqtt_gateway_plugin):
        """maybe_send_nodeinfo should respect the interval."""
        mock_iface = MagicMock()
        mock_iface._last_nodeinfo_time = time.time()
        mock_iface._nodeinfo_interval = 900

        from reticulumpi.builtin_plugins.meshtastic_gateway import _MeshtasticMQTTClient
        _MeshtasticMQTTClient.maybe_send_nodeinfo(mock_iface)
        mock_iface.sendNodeInfo.assert_not_called()

    def test_periodic_nodeinfo_fires_after_interval(self, mqtt_gateway_plugin):
        """maybe_send_nodeinfo should fire after enough time passes."""
        mock_iface = MagicMock()
        mock_iface._last_nodeinfo_time = time.time() - 1000  # 1000s ago
        mock_iface._nodeinfo_interval = 900

        from reticulumpi.builtin_plugins.meshtastic_gateway import _MeshtasticMQTTClient
        _MeshtasticMQTTClient.maybe_send_nodeinfo(mock_iface)
        mock_iface.sendNodeInfo.assert_called_once()

    def test_self_registered_in_nodes(self, mqtt_gateway_plugin):
        """Gateway should appear in its own nodes dict with isSelf marker."""
        mock_iface = _make_mock_mesh_interface()

        # Assign the mock interface to the plugin
        mqtt_gateway_plugin._mesh_interface = mock_iface
        mqtt_gateway_plugin._connected = True

        # Check that start() set up the identity fields
        assert mqtt_gateway_plugin._mqtt_node_num is not None

        # The MQTT client's _register_self_in_nodes is called during __init__
        # In test context the mock interface doesn't have real nodes, so test
        # the module-level helper directly
        from reticulumpi.builtin_plugins.meshtastic_gateway import _MeshtasticMQTTClient
        client = MagicMock(spec=_MeshtasticMQTTClient)
        client._my_node_num = 0xAABBCCDD
        client._long_name = "Test Gateway"
        client._short_name = "TSGW"
        client._lock = __import__("threading").Lock()
        client.nodes = {}

        _MeshtasticMQTTClient._register_self_in_nodes(client)
        assert "!aabbccdd" in client.nodes
        assert client.nodes["!aabbccdd"]["isSelf"] is True
        assert client.nodes["!aabbccdd"]["user"]["longName"] == "Test Gateway"
        assert client.nodes["!aabbccdd"]["user"]["shortName"] == "TSGW"
        assert client.nodes["!aabbccdd"]["user"]["hwModel"] == "PRIVATE_HW"


# ---------------------------------------------------------------------------
# TestSelfFiltering
# ---------------------------------------------------------------------------


class TestSelfFiltering:
    """Test that the MQTT client ignores its own echoed packets."""

    def test_own_text_message_ignored(self, mqtt_gateway_plugin):
        """Text message from our own node_num should be dropped."""
        from reticulumpi.builtin_plugins.meshtastic_gateway import _MeshtasticMQTTClient

        my_node = 0x12345678

        mock_client = MagicMock(spec=_MeshtasticMQTTClient)
        mock_client._my_node_num = my_node
        mock_client._aes_key = None
        mock_client._lock = __import__("threading").Lock()
        mock_client.nodes = {}
        mock_client._logger = None

        # Build a mock ServiceEnvelope where from == our own node number
        mock_packet = MagicMock()
        mock_packet.__getattribute__ = MagicMock(return_value=my_node)
        mock_packet.to = 0xFFFFFFFF
        mock_packet.id = 1

        mock_envelope = MagicMock()
        mock_envelope.HasField.return_value = True
        mock_envelope.packet = mock_packet

        _mock_meshtastic_mqtt_pb2.ServiceEnvelope.return_value = mock_envelope

        # Call should return early due to self-filter — _handle_nodeinfo never called
        _MeshtasticMQTTClient._process_mqtt_message(mock_client, "msh/test", b"fake")
        mock_client._handle_nodeinfo.assert_not_called()
        mock_client._decrypt_packet.assert_not_called()

    def test_other_messages_still_processed(self, mqtt_gateway_plugin):
        """Messages from other node_nums should be processed normally."""
        mock_iface = _make_mock_mesh_interface()
        mqtt_gateway_plugin._mesh_interface = mock_iface
        mqtt_gateway_plugin._connected = True

        # Simulate a text message from another node
        packet = {
            "from": 0xAAAABBBB,  # Different from gateway's node
            "fromId": "!aaaabbbb",
            "to": 0xFFFFFFFF,
            "decoded": {"payload": b"Hello from other", "text": "Hello from other"},
        }
        mqtt_gateway_plugin._on_mesh_text(packet, interface=mock_iface)
        assert mqtt_gateway_plugin._msgs_mesh_to_lxmf == 1


# ---------------------------------------------------------------------------
# TestGetStatusIdentity
# ---------------------------------------------------------------------------


class TestGetStatusIdentity:
    """Test get_status() includes identity fields in MQTT mode."""

    def test_mqtt_status_includes_identity(self, mqtt_gateway_plugin):
        """MQTT mode status should include node_id, long_name, short_name."""
        mock_iface = _make_mock_mesh_interface()
        mqtt_gateway_plugin._mesh_interface = mock_iface
        mqtt_gateway_plugin._connected = True

        status = mqtt_gateway_plugin.get_status()
        assert "node_id" in status
        assert status["node_id"].startswith("!")
        assert len(status["node_id"]) == 9  # !XXXXXXXX
        assert "long_name" in status
        assert "short_name" in status

    def test_serial_status_excludes_mqtt_identity(self, gateway_plugin):
        """Serial mode status should not include MQTT identity fields."""
        mock_iface = _make_mock_mesh_interface()
        gateway_plugin._mesh_interface = mock_iface
        gateway_plugin._connected = True

        status = gateway_plugin.get_status()
        assert "node_id" not in status
        assert "long_name" not in status
        assert "short_name" not in status


# ---------------------------------------------------------------------------
# TestDeriveShortName
# ---------------------------------------------------------------------------


class TestDeriveShortName:
    """Test the _derive_short_name helper."""

    def test_four_words(self):
        from reticulumpi.builtin_plugins.meshtastic_gateway import _derive_short_name
        assert _derive_short_name("ReticulumPi Mesh Gateway Node") == "RMGN"

    def test_three_words(self):
        from reticulumpi.builtin_plugins.meshtastic_gateway import _derive_short_name
        result = _derive_short_name("ReticulumPi Mesh Gateway")
        assert len(result) == 4
        assert result == "RMGR"  # initials "RMG" + pad from "ReticulumPiMeshGateway"

    def test_single_word(self):
        from reticulumpi.builtin_plugins.meshtastic_gateway import _derive_short_name
        result = _derive_short_name("Gateway")
        assert len(result) == 4
        assert result == "GGAT"  # initial "G" + "Gateway"[:3]

    def test_empty_string(self):
        from reticulumpi.builtin_plugins.meshtastic_gateway import _derive_short_name
        assert _derive_short_name("") == "NODE"


# ---------------------------------------------------------------------------
# TestLoadOrCreateNodeNum
# ---------------------------------------------------------------------------


class TestLoadOrCreateNodeNum:
    """Test the _load_or_create_node_num helper."""

    def test_creates_new_file(self, tmp_path):
        from reticulumpi.builtin_plugins.meshtastic_gateway import _load_or_create_node_num
        path = str(tmp_path / "node_num")
        num = _load_or_create_node_num(path)
        assert 0x10000000 <= num <= 0x7FFFFFFF
        # File should exist
        with open(path) as f:
            assert int(f.read().strip(), 16) == num

    def test_loads_existing_file(self, tmp_path):
        from reticulumpi.builtin_plugins.meshtastic_gateway import _load_or_create_node_num
        path = str(tmp_path / "node_num")
        with open(path, "w") as f:
            f.write("deadbeef\n")
        num = _load_or_create_node_num(path)
        assert num == 0xDEADBEEF

    def test_regenerates_on_corrupt_file(self, tmp_path):
        from reticulumpi.builtin_plugins.meshtastic_gateway import _load_or_create_node_num
        path = str(tmp_path / "node_num")
        with open(path, "w") as f:
            f.write("not-valid-hex\n")
        num = _load_or_create_node_num(path)
        assert 0x10000000 <= num <= 0x7FFFFFFF
