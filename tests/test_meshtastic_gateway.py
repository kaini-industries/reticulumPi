"""Tests for the Meshtastic Gateway plugin."""

from __future__ import annotations

import json
import errno
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

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
_mock_paho.MQTT_ERR_SUCCESS = 0
_mock_paho_client.MQTT_ERR_SUCCESS = 0
_mock_paho.mqtt = _mock_paho
_mock_paho.client = _mock_paho_client
_mock_crypto = MagicMock()
_mock_crypto_ciphers = MagicMock()


@pytest.fixture(autouse=True)
def _patch_meshtastic():
    """Ensure meshtastic and pubsub are always available as mocks."""
    mqtt_client = MagicMock()
    mqtt_client.connect.return_value = 0
    mqtt_client.subscribe.return_value = (0, 1)
    mqtt_client.publish.return_value.rc = 0

    def complete_connack():
        mqtt_client.on_connect(mqtt_client, None, None, 0, None)

    mqtt_client.loop_start.side_effect = complete_connack
    _mock_paho_client.Client.side_effect = None
    _mock_paho_client.Client.return_value = mqtt_client
    with patch.dict(
        sys.modules,
        {
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
        },
    ):
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
        "serial_port": "/dev/meshtastic",
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


def _live_meshtastic_connect_threads() -> set[threading.Thread]:
    """Return the currently live gateway connection workers."""

    return {
        thread
        for thread in threading.enumerate()
        if thread.name == "meshtastic-connect" and thread.is_alive()
    }


@contextmanager
def _create_started_plugin(mock_app, config):
    """Create and start a MeshtasticGateway with all mocks active."""
    with (
        patch(
            "reticulumpi.builtin_plugins.meshtastic_gateway.create_lxm_router"
        ) as mock_router_cls,
        patch("RNS.Identity") as mock_identity_cls,
        patch(
            "reticulumpi.builtin_plugins.meshtastic_gateway."
            "MeshtasticHealthAdapter.compatibility_error",
            return_value=None,
        ),
        patch.object(_RNS.Transport, "register_announce_handler"),
        patch.object(_RNS.Transport, "deregister_announce_handler"),
    ):
        baseline_connect_threads = _live_meshtastic_connect_threads()
        plugin = None
        try:
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
        finally:
            try:
                if plugin is not None:
                    plugin._active = False
                    plugin._join_threads()
            finally:
                leaked_connect_threads = (
                    _live_meshtastic_connect_threads() - baseline_connect_threads
                )
                assert not leaked_connect_threads, (
                    "started-plugin fixture leaked Meshtastic connection thread(s): "
                    f"{[(thread.name, thread.ident) for thread in leaked_connect_threads]}"
                )


def _quiesce_plugin_workers(plugin):
    """Stop real background probes before asserting synthetic callback state."""

    plugin._active = False
    plugin._join_threads()
    plugin._active = True
    with plugin._lock:
        plugin._serial_open_generation = plugin._serial_active_generation


@pytest.fixture
def gateway_plugin(mock_app, gw_config):
    """Create a serial-mode MeshtasticGateway plugin, started and stopped."""
    with _create_started_plugin(mock_app, gw_config) as plugin:
        _quiesce_plugin_workers(plugin)
        yield plugin


@pytest.fixture
def mqtt_gateway_plugin(mock_app, mqtt_gw_config):
    """Create an MQTT-mode MeshtasticGateway plugin, started and stopped."""
    with _create_started_plugin(mock_app, mqtt_gw_config) as plugin:
        _quiesce_plugin_workers(plugin)
        yield plugin


def _make_plugin_no_start(mock_app, config):
    """Construct a MeshtasticGateway without calling start() — for config validation tests."""
    with (
        patch("reticulumpi.builtin_plugins.meshtastic_gateway.create_lxm_router"),
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
                patch("reticulumpi.builtin_plugins.meshtastic_gateway.create_lxm_router"),
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

    def test_raises_on_invalid_startup_delay(self, mock_app, gw_config):
        gw_config["device_probe_startup_delay"] = 2
        with pytest.raises(ValueError, match="device_probe_startup_delay"):
            _make_plugin_no_start(mock_app, gw_config)

    def test_startup_delay_default_accepted(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        assert "device_probe_startup_delay" not in plugin.config

    def test_startup_delay_custom_accepted(self, mock_app, gw_config):
        gw_config["device_probe_startup_delay"] = 30
        plugin = _make_plugin_no_start(mock_app, gw_config)
        assert plugin.config["device_probe_startup_delay"] == 30

    def test_valid_config_passes(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        assert plugin.plugin_name == "meshtastic_gateway"

    def test_auto_serial_port_rejected_without_stable_recovery_identity(
        self,
        mock_app,
        gw_config,
    ):
        gw_config["serial_port"] = "auto"
        with pytest.raises(ValueError, match="explicit stable serial device path"):
            _make_plugin_no_start(mock_app, gw_config)

    @pytest.mark.parametrize(
        "serial_port",
        [
            None,
            False,
            0,
            "",
            " ",
            " /dev/meshtastic",
            "/dev/meshtastic ",
            "meshtastic",
            "/dev/ttyUSB0",
            "/dev/ttyACM12",
        ],
    )
    def test_serial_port_rejects_nonstable_or_malformed_values(
        self,
        mock_app,
        gw_config,
        serial_port,
    ):
        gw_config["serial_port"] = serial_port
        with pytest.raises(ValueError, match="serial_port requires an explicit stable"):
            _make_plugin_no_start(mock_app, gw_config)

    @pytest.mark.parametrize("key", ["enabled", "auto_reset", "usb_power_cycle"])
    def test_firmware_watchdog_booleans_are_strict(self, mock_app, gw_config, key):
        gw_config["firmware_watchdog"] = {key: 1}
        with pytest.raises(ValueError, match=rf"firmware_watchdog\.{key}"):
            _make_plugin_no_start(mock_app, gw_config)


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

    @pytest.mark.parametrize("timeout", [None, False, 0, -1, 30.1, "10"])
    def test_mqtt_mode_rejects_invalid_connack_timeout(
        self,
        mock_app,
        mqtt_gw_config,
        timeout,
    ):
        mqtt_gw_config["mqtt"]["connack_timeout_seconds"] = timeout
        with pytest.raises(ValueError, match="mqtt.connack_timeout_seconds"):
            _make_plugin_no_start(mock_app, mqtt_gw_config)

    def test_mqtt_mode_accepts_bounded_connack_timeout(self, mock_app, mqtt_gw_config):
        mqtt_gw_config["mqtt"]["connack_timeout_seconds"] = 12.5
        plugin = _make_plugin_no_start(mock_app, mqtt_gw_config)
        assert plugin.config["mqtt"]["connack_timeout_seconds"] == 12.5

    def test_mqtt_mode_missing_mqtt_section_uses_defaults(self, mock_app, mqtt_gw_config):
        """mqtt sub-section is optional — defaults are used."""
        del mqtt_gw_config["mqtt"]
        plugin = _make_plugin_no_start(mock_app, mqtt_gw_config)
        assert plugin is not None

    @pytest.mark.parametrize(
        "device_probe_port",
        [
            None,
            False,
            0,
            " ",
            "auto",
            "AUTO",
            " /dev/meshtastic",
            "/dev/meshtastic ",
            "meshtastic",
            "/dev/ttyUSB0",
            "/dev/ttyACM7",
        ],
    )
    def test_device_probe_port_rejects_nonstable_or_malformed_values(
        self,
        mock_app,
        mqtt_gw_config,
        device_probe_port,
    ):
        mqtt_gw_config["device_probe_port"] = device_probe_port
        with pytest.raises(ValueError, match="device_probe_port requires an explicit stable"):
            _make_plugin_no_start(mock_app, mqtt_gw_config)

    @pytest.mark.parametrize(
        "device_probe_port",
        ["", "/dev/meshtastic", "/dev/serial/by-id/usb-meshtastic-radio"],
    )
    def test_device_probe_port_accepts_disabled_or_stable_aliases(
        self,
        mock_app,
        mqtt_gw_config,
        device_probe_port,
    ):
        mqtt_gw_config["device_probe_port"] = device_probe_port
        plugin = _make_plugin_no_start(mock_app, mqtt_gw_config)
        assert plugin.config["device_probe_port"] == device_probe_port

    def test_mqtt_mode_rejects_missing_paho(self, mock_app, mqtt_gw_config):
        """ValueError when paho-mqtt is not importable."""
        with patch.dict(sys.modules, {"paho": None, "paho.mqtt": None, "paho.mqtt.client": None}):
            with (
                patch("reticulumpi.builtin_plugins.meshtastic_gateway.create_lxm_router"),
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
    def test_started_plugin_cleanup_is_unconditional(self, mock_app, gw_config):
        baseline_connect_threads = _live_meshtastic_connect_threads()

        with pytest.raises(RuntimeError, match="synthetic fixture-body failure"):
            with _create_started_plugin(mock_app, gw_config) as plugin:
                assert plugin._active is True
                raise RuntimeError("synthetic fixture-body failure")

        assert _live_meshtastic_connect_threads() == baseline_connect_threads

    def test_api_v2_waits_for_device_and_tracks_loss_recovery(self, gateway_plugin):
        from reticulumpi.plugin_base import PluginHealth, PluginState

        gateway_plugin._active = False
        gateway_plugin._join_threads()
        gateway_plugin.mark_starting()
        gateway_plugin._active = True
        gateway_plugin._connected = False
        gateway_plugin._mesh_interface = None
        iface = _make_mock_mesh_interface()

        assert gateway_plugin.plugin_lifecycle_api == 2
        assert gateway_plugin.plugin_start_timeout_seconds == 75.0
        assert gateway_plugin.plugin_state == PluginState.STARTING

        with gateway_plugin._lock:
            generation = gateway_plugin._serial_active_generation
            gateway_plugin._serial_open_generation = generation
            gateway_plugin._serial_probe_candidate = (iface, generation)
        with (
            patch.object(gateway_plugin, "_create_serial_interface", return_value=iface),
            patch.object(gateway_plugin, "_bind_or_validate_usb_identity", return_value=True),
        ):
            gateway_plugin._connect_mesh_device()

        assert gateway_plugin.plugin_state == PluginState.READY
        assert gateway_plugin.plugin_health == PluginHealth.HEALTHY

        gateway_plugin._on_mesh_disconnect(interface=iface)
        assert gateway_plugin.plugin_state == PluginState.READY
        assert gateway_plugin.plugin_health == PluginHealth.DEGRADED

        gateway_plugin._on_mesh_connect(interface=iface)
        assert gateway_plugin.plugin_state == PluginState.READY
        assert gateway_plugin.plugin_health == PluginHealth.HEALTHY

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
            patch(
                "reticulumpi.builtin_plugins.meshtastic_gateway.create_lxm_router"
            ) as mock_router_cls,
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
        with _create_started_plugin(mock_app, gw_config) as plugin:
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
        gateway_plugin._on_mesh_text(
            self._make_packet(),
            interface=gateway_plugin._mesh_interface,
        )
        assert gateway_plugin._msgs_mesh_to_lxmf == 1
        assert gateway_plugin._last_mesh_msg_time is not None

    def test_message_includes_sender_info(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()

        forwarded = []
        gateway_plugin._forward_to_lxmf = lambda text: forwarded.append(text)

        gateway_plugin._on_mesh_text(
            self._make_packet(),
            interface=gateway_plugin._mesh_interface,
        )
        assert len(forwarded) == 1
        assert "[Mesh]" in forwarded[0]
        assert "TestNode1" in forwarded[0]
        assert "!abcd1234" in forwarded[0]
        assert "Hello mesh" in forwarded[0]

    def test_allow_list_blocks_unknown_sender(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        gateway_plugin._mesh_allow_set = {"!deadbeef"}
        gateway_plugin._on_mesh_text(
            self._make_packet(from_id="!abcd1234"),
            interface=gateway_plugin._mesh_interface,
        )
        assert gateway_plugin._msgs_mesh_to_lxmf == 0

    def test_allow_list_permits_listed_sender(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        gateway_plugin._mesh_allow_set = {"!abcd1234"}
        gateway_plugin._on_mesh_text(
            self._make_packet(from_id="!abcd1234"),
            interface=gateway_plugin._mesh_interface,
        )
        assert gateway_plugin._msgs_mesh_to_lxmf == 1

    def test_empty_allow_list_accepts_all(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        gateway_plugin._mesh_allow_set = set()
        gateway_plugin._on_mesh_text(
            self._make_packet(from_id="!anything0"),
            interface=gateway_plugin._mesh_interface,
        )
        assert gateway_plugin._msgs_mesh_to_lxmf == 1

    def test_empty_text_ignored(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        gateway_plugin._on_mesh_text(
            self._make_packet(text="   "),
            interface=gateway_plugin._mesh_interface,
        )
        assert gateway_plugin._msgs_mesh_to_lxmf == 0

    def test_event_published(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        gateway_plugin._on_mesh_text(
            self._make_packet(),
            interface=gateway_plugin._mesh_interface,
        )
        calls = [
            c
            for c in gateway_plugin.event_bus.publish.call_args_list
            if c[0][0] == "meshtastic.message_received"
        ]
        assert len(calls) == 1

    def test_pure_serial_primary_packets_are_labelled_lora(self, gateway_plugin):
        iface = _make_mock_mesh_interface()
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = iface

        gateway_plugin._on_mesh_text(self._make_packet(), interface=iface)

        matching = [
            call.args[1]
            for call in gateway_plugin.event_bus.publish.call_args_list
            if call.args[0] == "meshtastic.message_received"
        ]
        assert matching[-1]["source"] == "LoRa"

    def test_global_pubsub_text_rejects_unowned_or_missing_interface(self, gateway_plugin):
        owned = _make_mock_mesh_interface()
        foreign = _make_mock_mesh_interface()
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = owned
        gateway_plugin._forward_to_lxmf = MagicMock()
        packet = self._make_packet()
        packet["id"] = 0x10203040
        gateway_plugin.event_bus.publish.reset_mock()

        gateway_plugin._on_mesh_text(packet, interface=foreign)
        gateway_plugin._on_mesh_text(packet, interface=None)

        gateway_plugin._forward_to_lxmf.assert_not_called()
        gateway_plugin.event_bus.publish.assert_not_called()
        assert gateway_plugin._msgs_mesh_to_lxmf == 0
        assert (0xABCD1234, 0x10203040) not in gateway_plugin._seen_packet_ids


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
            patch(
                "reticulumpi.builtin_plugins.meshtastic_gateway.create_lxm_router"
            ) as mock_router_cls,
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
                # The real connection/watchdog workers are irrelevant to this
                # unit assertion and can race the synthetic interface below by
                # observing that /dev/meshtastic is absent on the test host.
                plugin._active = False
                plugin._join_threads()
                plugin._active = True
                with plugin._lock:
                    plugin._serial_open_generation = plugin._serial_active_generation
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
        assert status["serial_port"] == "/dev/meshtastic"
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
        with _create_started_plugin(mock_app, gw_config) as plugin:
            status = plugin.get_status()
            assert status["rate_limit_per_min"] == 4.0

    def test_no_rate_limit_field_when_unlimited(self, gateway_plugin):
        status = gateway_plugin.get_status()
        assert "rate_limit_per_min" not in status

    def test_serial_available_in_status(self, gateway_plugin):
        gateway_plugin._serial_listener = MagicMock()
        status = gateway_plugin.get_status()
        assert status["serial_available"] is True

        gateway_plugin._serial_listener = None
        status = gateway_plugin.get_status()
        assert status["serial_available"] is False

    def test_pure_serial_primary_is_reported_available(self, gateway_plugin):
        gateway_plugin._serial_listener = None
        gateway_plugin._mesh_interface = MagicMock()
        gateway_plugin._connected = True

        status = gateway_plugin.get_status()

        assert status["serial_available"] is True

    def test_pure_serial_disconnect_is_not_hidden_by_mqtt_grace(self, gateway_plugin):
        gateway_plugin._mesh_interface = MagicMock()
        gateway_plugin._connected = False
        gateway_plugin._last_disconnect_time = time.monotonic()

        status = gateway_plugin.get_status()

        assert status["connected"] is False


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
        _quiesce_plugin_workers(gateway_plugin)
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

    def test_mqtt_nodes_have_via_mqtt_flag(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        nodes = gateway_plugin.get_meshtastic_nodes()
        for node in nodes:
            assert node["via_mqtt"] is True
            assert node["via_lora"] is False

    def test_serial_lora_node_has_via_lora_flag(self, gateway_plugin):
        gateway_plugin._connected = False
        gateway_plugin._mesh_interface = None
        listener = MagicMock()
        listener.nodes = {
            "!aaa11111": {
                "user": {"longName": "LoRaNode", "shortName": "LN", "hwModel": "RAK4631"},
                "snr": 3.0,
                "lastHeard": 1700002000,
                "position": {"latitude": 31.0, "longitude": -96.0},
            },
        }
        gateway_plugin._serial_listener = listener
        nodes = gateway_plugin.get_meshtastic_nodes()
        assert len(nodes) == 1
        assert nodes[0]["via_lora"] is True
        assert nodes[0]["via_mqtt"] is False

    def test_serial_via_mqtt_node_has_via_mqtt_flag(self, gateway_plugin):
        gateway_plugin._connected = False
        gateway_plugin._mesh_interface = None
        listener = MagicMock()
        listener.nodes = {
            "!bbb22222": {
                "user": {"longName": "RelayedNode", "shortName": "RN", "hwModel": "HELTEC_V3"},
                "snr": 1.0,
                "lastHeard": 1700003000,
                "position": {},
                "viaMqtt": True,
            },
        }
        gateway_plugin._serial_listener = listener
        nodes = gateway_plugin.get_meshtastic_nodes()
        assert len(nodes) == 1
        assert nodes[0]["via_mqtt"] is True
        assert nodes[0]["via_lora"] is False

    def test_merged_node_has_both_transport_flags(self, gateway_plugin):
        _quiesce_plugin_workers(gateway_plugin)
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        listener = MagicMock()
        listener.nodes = {
            "!abcd1234": {
                "user": {"longName": "TestNode1", "shortName": "TN1", "hwModel": "RAK4631"},
                "snr": 6.0,
                "lastHeard": 1700005000,
                "position": {"latitude": 30.0, "longitude": -97.0},
            },
        }
        gateway_plugin._serial_listener = listener
        nodes = gateway_plugin.get_meshtastic_nodes()
        merged = next(n for n in nodes if n["id"] == "!abcd1234")
        assert merged["via_mqtt"] is True
        assert merged["via_lora"] is True

    def test_self_node_always_via_lora(self, gateway_plugin):
        gateway_plugin._connected = True
        iface = MagicMock()
        iface.nodes = {
            "!12345678": {
                "user": {"longName": "MyNode", "shortName": "MN", "hwModel": "RAK4631"},
                "isSelf": True,
                "lastHeard": 1700000000,
                "position": {},
            },
        }
        iface.myInfo = MagicMock()
        iface.myInfo.my_node_num = 0x12345678
        gateway_plugin._mesh_interface = iface
        nodes = gateway_plugin.get_meshtastic_nodes()
        self_node = next(n for n in nodes if n.get("is_self"))
        assert self_node["via_lora"] is True
        assert self_node["via_mqtt"] is False


# ---------------------------------------------------------------------------
# TestNodeDataCache
# ---------------------------------------------------------------------------


class TestNodeDataCache:
    def test_persisted_nodes_loaded_on_startup(self, gateway_plugin, tmp_path):
        cache = {
            "!cached01": {
                "id": "!cached01",
                "long_name": "CachedNode",
                "short_name": "CN",
                "hw_model": "RAK4631",
                "snr": 4.0,
                "last_heard": 1700000000,
                "latitude": 30.0,
                "longitude": -97.0,
                "via_mqtt": True,
                "via_lora": False,
            },
        }
        cache_path = str(tmp_path / "node_data_cache.json")
        with open(cache_path, "w") as f:
            json.dump(cache, f)
        gateway_plugin._node_data_cache_path = cache_path
        gateway_plugin._load_node_data_cache()
        assert "!cached01" in gateway_plugin._persisted_nodes
        assert gateway_plugin._persisted_nodes["!cached01"]["long_name"] == "CachedNode"

    def test_persisted_nodes_appear_in_get_meshtastic_nodes(self, gateway_plugin):
        gateway_plugin._connected = False
        gateway_plugin._mesh_interface = None
        gateway_plugin._serial_listener = None
        gateway_plugin._persisted_nodes = {
            "!cached01": {
                "id": "!cached01",
                "long_name": "CachedNode",
                "short_name": "CN",
                "hw_model": "RAK4631",
                "snr": 4.0,
                "last_heard": int(time.time()) - 3600,
                "latitude": 30.0,
                "longitude": -97.0,
                "via_mqtt": True,
                "via_lora": False,
            },
        }
        nodes = gateway_plugin.get_meshtastic_nodes()
        assert len(nodes) == 1
        assert nodes[0]["id"] == "!cached01"
        assert nodes[0]["long_name"] == "CachedNode"

    def test_live_data_overwrites_persisted(self, gateway_plugin):
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        gateway_plugin._persisted_nodes = {
            "!abcd1234": {
                "id": "!abcd1234",
                "long_name": "OldName",
                "short_name": "ON",
                "hw_model": "RAK4631",
                "snr": 1.0,
                "last_heard": 1600000000,
                "latitude": 29.0,
                "longitude": -96.0,
                "via_mqtt": True,
                "via_lora": False,
            },
        }
        nodes = gateway_plugin.get_meshtastic_nodes()
        node = next(n for n in nodes if n["id"] == "!abcd1234")
        assert node["long_name"] == "TestNode1"
        assert node["snr"] == 5.5

    def test_save_and_reload_roundtrip(self, gateway_plugin, tmp_path):
        cache_path = str(tmp_path / "node_data_cache.json")
        gateway_plugin._node_data_cache_path = cache_path
        gateway_plugin._persisted_nodes = {
            "!round01": {
                "id": "!round01",
                "long_name": "RoundTrip",
                "short_name": "RT",
                "hw_model": "HELTEC_V3",
                "snr": 3.5,
                "last_heard": 1700001000,
                "latitude": 31.0,
                "longitude": -98.0,
                "via_mqtt": False,
                "via_lora": True,
            },
        }
        gateway_plugin._save_node_data_cache()
        gateway_plugin._persisted_nodes = {}
        gateway_plugin._load_node_data_cache()
        assert "!round01" in gateway_plugin._persisted_nodes
        assert gateway_plugin._persisted_nodes["!round01"]["long_name"] == "RoundTrip"

    def test_missing_cache_file_no_error(self, gateway_plugin, tmp_path):
        gateway_plugin._node_data_cache_path = str(tmp_path / "nonexistent.json")
        gateway_plugin._load_node_data_cache()
        assert gateway_plugin._persisted_nodes == {}


# ---------------------------------------------------------------------------
# TestNodeDataSaveDispatch (A5: off-thread periodic save + 30s TTL)
# ---------------------------------------------------------------------------


class TestNodeDataSaveDispatch:
    def test_periodic_save_dispatched_off_thread(self, gateway_plugin):
        """The 20th refresh dispatches the disk save to a thread, never inline."""
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        # Next get_meshtastic_nodes() crosses the 20-cycle threshold.
        gateway_plugin._node_data_save_counter = 19
        gateway_plugin._nodes_cache = None  # force a real rebuild

        dispatched = []

        def _fake_start_thread(target, name=None):
            dispatched.append((target, name))
            return MagicMock()  # do NOT run it — prove the caller returns first

        with (
            patch.object(gateway_plugin, "_start_thread", side_effect=_fake_start_thread),
            patch.object(gateway_plugin, "_save_node_data_cache") as mock_save,
        ):
            nodes = gateway_plugin.get_meshtastic_nodes()

            # The caller returned its node list without doing the disk write.
            assert nodes is not None
            mock_save.assert_not_called()
            # A background thread was scheduled for the save.
            assert len(dispatched) == 1
            target, name = dispatched[0]
            assert name == "meshtastic-node-save"

            # Running the dispatched target performs the actual save.
            target()
            mock_save.assert_called_once()

    def test_save_not_dispatched_before_threshold(self, gateway_plugin):
        """Refreshes below the 20-cycle threshold neither save nor dispatch."""
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        gateway_plugin._node_data_save_counter = 0
        gateway_plugin._nodes_cache = None

        with (
            patch.object(gateway_plugin, "_start_thread") as mock_start,
            patch.object(gateway_plugin, "_save_node_data_cache") as mock_save,
        ):
            gateway_plugin.get_meshtastic_nodes()
            mock_start.assert_not_called()
            mock_save.assert_not_called()

    def test_async_save_skips_when_inflight(self, gateway_plugin):
        """A save already in flight is not started a second time."""
        gateway_plugin._node_data_save_inflight = True
        with patch.object(gateway_plugin, "_start_thread") as mock_start:
            gateway_plugin._save_node_data_cache_async()
            mock_start.assert_not_called()

    def test_async_save_clears_inflight_after_run(self, gateway_plugin):
        """The dispatched target clears the in-flight flag when it finishes."""
        captured = {}

        def _fake_start_thread(target, name=None):
            captured["target"] = target
            return MagicMock()

        with (
            patch.object(gateway_plugin, "_start_thread", side_effect=_fake_start_thread),
            patch.object(gateway_plugin, "_save_node_data_cache"),
        ):
            gateway_plugin._save_node_data_cache_async()
            assert gateway_plugin._node_data_save_inflight is True
            captured["target"]()
            assert gateway_plugin._node_data_save_inflight is False

    def test_nodes_cache_ttl_is_30_seconds(self, gateway_plugin):
        """A5(ii): node cache TTL was raised from 10s to 30s."""
        assert gateway_plugin._nodes_cache_ttl == 30.0

    def test_nodes_cache_served_within_ttl(self, gateway_plugin):
        """A cached node list within the 30s TTL is returned without a rebuild."""
        sentinel = [{"id": "!cached", "long_name": "Cached"}]
        # Stamp the cache 20s ago — still inside the 30s window.
        gateway_plugin._nodes_cache = (time.monotonic() - 20.0, sentinel)
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()

        result = gateway_plugin.get_meshtastic_nodes()
        assert result is sentinel  # served from cache, no rebuild

    def test_nodes_cache_rebuilds_after_ttl(self, gateway_plugin):
        """Past the 30s TTL the node list is rebuilt from the live interface."""
        stale = [{"id": "!stale", "long_name": "Stale"}]
        gateway_plugin._nodes_cache = (time.monotonic() - 31.0, stale)
        gateway_plugin._connected = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()

        result = gateway_plugin.get_meshtastic_nodes()
        assert result is not stale
        assert {n["id"] for n in result} == {"!abcd1234", "!beef5678"}


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

    def test_pure_serial_open_failure_never_enters_mqtt_suspension(self, gateway_plugin):
        from reticulumpi.builtin_plugins.meshtastic_gateway import (
            SerialOpenError,
            SerialOpenOutcome,
            SerialOpenResult,
        )

        plugin = gateway_plugin
        plugin._connected = False
        plugin._mesh_interface = None
        sleeps = []

        def stop_after_backoff(seconds):
            sleeps.append(seconds)
            plugin._active = False

        failure = SerialOpenError(
            SerialOpenResult(SerialOpenOutcome.BUSY),
            plugin.config["serial_port"],
        )
        with (
            patch.object(plugin, "_close_mesh_interface"),
            patch.object(plugin, "_connect_mesh_device", side_effect=failure),
            patch.object(plugin, "_sleep_while_active", side_effect=stop_after_backoff),
        ):
            plugin._connection_loop()

        assert sleeps == [plugin._serial_retry_interval]
        assert plugin._mqtt_suspended is False

    def test_default_mqtt_reconnects_indefinitely_without_suspension(
        self,
        mqtt_gateway_plugin,
    ):
        plugin = mqtt_gateway_plugin
        plugin.config.pop("max_reconnect_attempts")
        with plugin._lock:
            plugin._connected = False
            plugin._mesh_interface = None
            plugin._mqtt_suspended = False
            plugin._reconnect_failures = 0
        plugin._active = True
        sleeps = []

        def stop_after_eleven_failures(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 11:
                plugin._active = False

        with (
            patch.object(plugin, "_close_mesh_interface"),
            patch.object(
                plugin, "_connect_mesh_device", side_effect=RuntimeError("offline")
            ) as connect,
            patch.object(
                plugin,
                "_sleep_while_active",
                side_effect=stop_after_eleven_failures,
            ),
        ):
            plugin._connection_loop()

        assert connect.call_count == 11
        assert 600 not in sleeps
        assert plugin._mqtt_suspended is False

    def test_on_mesh_disconnect_sets_flag(self, gateway_plugin):
        iface = _make_mock_mesh_interface()
        gateway_plugin._mesh_interface = iface
        gateway_plugin._connected = True
        gateway_plugin._on_mesh_disconnect(interface=iface)
        assert gateway_plugin._connected is False

    def test_on_mesh_disconnect_records_time(self, gateway_plugin):
        gateway_plugin._active = False
        gateway_plugin._join_threads()
        iface = _make_mock_mesh_interface()
        gateway_plugin._mesh_interface = iface
        gateway_plugin._connected = True
        gateway_plugin._last_disconnect_time = 0.0
        gateway_plugin._on_mesh_disconnect(interface=iface)
        assert gateway_plugin._last_disconnect_time > 0

    def test_on_mesh_disconnect_ignores_none_interface(self, gateway_plugin):
        """Pubsub event with interface=None must not falsely disconnect."""
        iface = _make_mock_mesh_interface()
        gateway_plugin._mesh_interface = iface
        gateway_plugin._connected = True
        gateway_plugin._on_mesh_disconnect(interface=None)
        assert gateway_plugin._connected is True

    def test_on_mesh_connect_restores_connected(self, gateway_plugin):
        """When paho auto-reconnects, _on_mesh_connect must set _connected = True."""
        iface = _make_mock_mesh_interface()
        gateway_plugin._mesh_interface = iface
        gateway_plugin._connected = False
        gateway_plugin._on_mesh_connect(interface=iface)
        assert gateway_plugin._connected is True

    def test_on_mesh_connect_ignores_none_interface(self, gateway_plugin):
        """Pubsub event with interface=None must not falsely connect."""
        _quiesce_plugin_workers(gateway_plugin)
        iface = _make_mock_mesh_interface()
        gateway_plugin._mesh_interface = iface
        gateway_plugin._connected = False
        gateway_plugin._on_mesh_connect(interface=None)
        assert gateway_plugin._connected is False

    def test_on_mesh_connect_publishes_event_on_reconnect(self, gateway_plugin):
        """Auto-reconnect fires MESHTASTIC_CONNECTED event."""
        iface = _make_mock_mesh_interface()
        gateway_plugin._mesh_interface = iface
        gateway_plugin._connected = False
        gateway_plugin._on_mesh_connect(interface=iface)
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
        iface = _make_mock_mesh_interface()
        gateway_plugin._mesh_interface = iface
        gateway_plugin._connected = True
        gateway_plugin.event_bus.publish.reset_mock()
        gateway_plugin._on_mesh_connect(interface=iface)
        # Should NOT publish MESHTASTIC_CONNECTED
        from reticulumpi import events

        connected_calls = [
            c
            for c in gateway_plugin.event_bus.publish.call_args_list
            if c.args[0] == events.MESHTASTIC_CONNECTED
        ]
        assert len(connected_calls) == 0


# ---------------------------------------------------------------------------
# TestGracefulDeviceShutdown
# ---------------------------------------------------------------------------


class TestGracefulDeviceShutdown:
    def test_reboot_called_with_serial_listener(self, gateway_plugin):
        mock_node = MagicMock()
        iface = _make_mock_mesh_interface()
        iface.localNode = mock_node
        gateway_plugin._serial_listener = iface
        gateway_plugin._reboot_device_on_stop = True
        with (
            patch.object(gateway_plugin, "_reserve_reset", return_value=True) as reserve,
            patch("reticulumpi.builtin_plugins.meshtastic_gateway.time.sleep") as mock_sleep,
        ):
            gateway_plugin._graceful_device_shutdown()
        mock_node.reboot.assert_called_once_with(secs=2)
        reserve.assert_called_once_with("shutdown_reboot")
        mock_sleep.assert_called_once_with(0.3)

    def test_reboot_called_with_serial_mode_interface(self, gateway_plugin):
        mock_node = MagicMock()
        iface = _make_mock_mesh_interface()
        iface.localNode = mock_node
        gateway_plugin._serial_listener = None
        gateway_plugin._mesh_interface = iface
        gateway_plugin._mode = "serial"
        gateway_plugin._reboot_device_on_stop = True
        with patch("reticulumpi.builtin_plugins.meshtastic_gateway.time.sleep") as mock_sleep:
            gateway_plugin._graceful_device_shutdown()
        mock_node.reboot.assert_called_once_with(secs=2)
        mock_sleep.assert_called_once_with(0.3)

    def test_skipped_when_disabled(self, gateway_plugin):
        mock_node = MagicMock()
        iface = _make_mock_mesh_interface()
        iface.localNode = mock_node
        gateway_plugin._serial_listener = iface
        gateway_plugin._reboot_device_on_stop = False
        gateway_plugin._graceful_device_shutdown()
        mock_node.reboot.assert_not_called()

    def test_skipped_in_mqtt_only_mode(self, gateway_plugin):
        mock_node = MagicMock()
        iface = _make_mock_mesh_interface()
        iface.localNode = mock_node
        gateway_plugin._serial_listener = None
        gateway_plugin._mesh_interface = iface
        gateway_plugin._mode = "mqtt"
        gateway_plugin._reboot_device_on_stop = True
        gateway_plugin._graceful_device_shutdown()
        mock_node.reboot.assert_not_called()

    def test_exception_does_not_block(self, gateway_plugin):
        mock_node = MagicMock()
        mock_node.reboot.side_effect = RuntimeError("serial gone")
        iface = _make_mock_mesh_interface()
        iface.localNode = mock_node
        gateway_plugin._serial_listener = iface
        gateway_plugin._reboot_device_on_stop = True
        gateway_plugin._graceful_device_shutdown()  # Should not raise

    def test_skipped_when_local_node_is_none(self, gateway_plugin):
        iface = _make_mock_mesh_interface()
        iface.localNode = None
        gateway_plugin._serial_listener = iface
        gateway_plugin._reboot_device_on_stop = True
        gateway_plugin._graceful_device_shutdown()  # Should not raise

    def test_config_validation_rejects_non_bool(self, mock_app, gw_config):
        gw_config["reboot_device_on_stop"] = "yes"
        with pytest.raises(ValueError, match="reboot_device_on_stop"):
            _make_plugin_no_start(mock_app, gw_config)

    def test_stop_calls_shutdown_before_deactivating(self, gateway_plugin):
        active_during_shutdown = []

        def capture_active():
            active_during_shutdown.append(gateway_plugin._active)

        gateway_plugin._reboot_device_on_stop = True
        with patch.object(gateway_plugin, "_graceful_device_shutdown", side_effect=capture_active):
            gateway_plugin.stop()
        assert active_during_shutdown == [True]

    def test_instance_vars_set_after_start(self, gateway_plugin):
        assert gateway_plugin._device_probe_startup_delay == 20
        assert gateway_plugin._reboot_device_on_stop is False


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
        mqtt_gateway_plugin._on_mesh_text(
            packet,
            interface=mqtt_gateway_plugin._mesh_interface,
        )
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
        with _create_started_plugin(mock_app, gw_config) as plugin:
            _quiesce_plugin_workers(plugin)
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
        with _create_started_plugin(mock_app, gw_config) as plugin:
            _quiesce_plugin_workers(plugin)
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
        _quiesce_plugin_workers(gateway_plugin)
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
        with _create_started_plugin(mock_app, gw_config) as plugin:
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

    def test_legacy_mqtt_node_num_rotates_once_on_packet_state_enrollment(
        self,
        mock_app,
        mqtt_gw_config,
        tmp_path,
    ):
        """A pre-allocator identity rotates once, then remains stable."""
        storage = tmp_path / "mqtt_gw_lxmf"
        storage.mkdir(parents=True, exist_ok=True)
        node_file = storage / "meshtastic_node_num"
        node_file.write_text("1a2b3c4d\n")

        with _create_started_plugin(mock_app, mqtt_gw_config) as plugin:
            enrolled_num = plugin._mqtt_node_num
            assert enrolled_num != 0x1A2B3C4D
            assert int(node_file.read_text().strip(), 16) == enrolled_num
            assert (storage / "meshtastic_packet_ids.json").exists()

        with _create_started_plugin(mock_app, mqtt_gw_config) as plugin:
            assert plugin._mqtt_node_num == enrolled_num

    def test_mqtt_node_num_stable_across_restarts(self, mock_app, mqtt_gw_config):
        """Node number should persist between start/stop cycles."""
        first_num = None
        with _create_started_plugin(mock_app, mqtt_gw_config) as plugin:
            first_num = plugin._mqtt_node_num

        with _create_started_plugin(mock_app, mqtt_gw_config) as plugin:
            assert plugin._mqtt_node_num == first_num

    def test_serial_mode_skips_persistence(self, gateway_plugin, tmp_path):
        """Serial mode should not create meshtastic_node_num file."""
        node_file = tmp_path / "mesh_gw_lxmf" / "meshtastic_node_num"
        assert not node_file.exists()
        assert gateway_plugin._mqtt_node_num is None

    def test_short_name_from_config(self, mock_app, mqtt_gw_config):
        """Explicit short_name in config should be used."""
        mqtt_gw_config["short_name"] = "RPGW"
        with _create_started_plugin(mock_app, mqtt_gw_config) as plugin:
            assert plugin._mqtt_short_name == "RPGW"

    def test_short_name_derived_from_display_name(self, mock_app, mqtt_gw_config):
        """If short_name not set, derive from display_name."""
        mqtt_gw_config["display_name"] = "ReticulumPi Mesh Gateway"
        with _create_started_plugin(mock_app, mqtt_gw_config) as plugin:
            assert len(plugin._mqtt_short_name) == 4
            # Initials "RMG" + pad from "ReticulumPiMeshGateway" → "RMGR"
            assert plugin._mqtt_short_name == "RMGR"

    def test_short_name_validation_rejects_too_long(self, mock_app, mqtt_gw_config):
        """short_name > 4 chars should raise ValueError."""
        mqtt_gw_config["short_name"] = "TOOLONG"
        with pytest.raises(ValueError, match="short_name"):
            _make_plugin_no_start(mock_app, mqtt_gw_config)

    def test_display_name_used_as_long_name(self, mock_app, mqtt_gw_config):
        """display_name should map to _mqtt_long_name."""
        mqtt_gw_config["display_name"] = "My Custom Gateway"
        with _create_started_plugin(mock_app, mqtt_gw_config) as plugin:
            assert plugin._mqtt_long_name == "My Custom Gateway"

    def test_corrupt_node_num_file_regenerates(self, mock_app, mqtt_gw_config, tmp_path):
        """Corrupt file should be replaced with a valid node number."""
        storage = tmp_path / "mqtt_gw_lxmf"
        storage.mkdir(parents=True, exist_ok=True)
        node_file = storage / "meshtastic_node_num"
        node_file.write_text("not-hex-at-all\n")

        with _create_started_plugin(mock_app, mqtt_gw_config) as plugin:
            assert plugin._mqtt_node_num is not None
            assert 0x10000000 <= plugin._mqtt_node_num <= 0x7FFFFFFF
            # File should have been overwritten with valid hex
            content = node_file.read_text().strip()
            assert int(content, 16) == plugin._mqtt_node_num


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
        mock_iface._packet_id_allocator = MagicMock()
        mock_iface._packet_id_allocator.take.return_value = 42
        mock_iface._last_nodeinfo_time = 0
        mock_iface._logger = None
        mock_iface.client.publish.return_value.rc = 0

        _MeshtasticMQTTClient.sendNodeInfo(mock_iface)
        mock_iface.client.publish.assert_called_once()

        call_args = mock_iface.client.publish.call_args
        topic = call_args[0][0]
        assert topic == "msh/US/2/e/LongFast/!12345678"

    def test_send_nodeinfo_rejects_non_success_publish_result(self):
        """A broker admission failure must not advance the NODEINFO timer."""
        from reticulumpi.builtin_plugins.meshtastic_gateway import _MeshtasticMQTTClient

        mock_iface = MagicMock()
        mock_iface._my_node_num = 0x12345678
        mock_iface._long_name = "Test GW"
        mock_iface._short_name = "TSGW"
        mock_iface._aes_key = None
        mock_iface._root_topic = "msh/US/2/e/LongFast"
        mock_iface._packet_id_allocator = MagicMock()
        mock_iface._packet_id_allocator.take.return_value = 42
        mock_iface._last_nodeinfo_time = 123.0
        mock_iface._logger = None
        mock_iface.client.publish.return_value.rc = 4

        with pytest.raises(ConnectionError, match=r"NODEINFO.*rc=4"):
            _MeshtasticMQTTClient.sendNodeInfo(mock_iface)

        assert mock_iface._last_nodeinfo_time == 123.0

    def test_nodeinfo_sent_on_connect(self, mqtt_gateway_plugin):
        """_on_connect callback should trigger sendNodeInfo."""
        mock_iface = MagicMock()
        mock_iface._root_topic = "msh/US/2/e/LongFast"
        mock_iface._logger = None
        mock_iface._lock = threading.Lock()
        mock_iface._closed = False
        mock_iface._connack_event = threading.Event()
        mock_iface._connack_succeeded = False
        mock_iface._connack_reason_code = None

        from reticulumpi.builtin_plugins.meshtastic_gateway import _MeshtasticMQTTClient

        mock_iface._connack_failed = _MeshtasticMQTTClient._connack_failed
        client = MagicMock()
        client.subscribe.return_value = (0, 1)
        _MeshtasticMQTTClient._on_connect(mock_iface, client, None, None, 0, None)
        mock_iface.sendNodeInfo.assert_called_once()
        assert mock_iface._connack_event.is_set()
        assert mock_iface._connack_succeeded is True

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


class TestMQTTConnackAdmission:
    @staticmethod
    def _plugin_waiting_for_mqtt(mock_app, mqtt_gw_config):
        plugin = _make_plugin_no_start(mock_app, mqtt_gw_config)
        plugin.mark_starting()
        mock_router = MagicMock()
        mock_destination = MagicMock()
        mock_destination.hash = b"\x03" * 16
        mock_router.register_delivery_identity.return_value = mock_destination
        with (
            patch.object(plugin, "_start_thread"),
            patch(
                "reticulumpi.builtin_plugins.meshtastic_gateway.create_lxm_router",
                return_value=mock_router,
            ),
            patch("RNS.Identity") as identity_cls,
        ):
            identity_cls.return_value.hash = b"\x04" * 16
            plugin.start()
        plugin.event_bus.publish.reset_mock()
        return plugin

    @staticmethod
    def _paho_client():
        client = MagicMock()
        client.connect.return_value = 0
        client.subscribe.return_value = (0, 1)
        client.publish.return_value.rc = 0
        client.is_connected.return_value = True
        return client

    def test_failed_connack_closes_client_without_connected_or_ready(
        self,
        mock_app,
        mqtt_gw_config,
    ):
        from reticulumpi import events
        from reticulumpi.plugin_base import PluginState

        plugin = self._plugin_waiting_for_mqtt(mock_app, mqtt_gw_config)
        client = self._paho_client()

        def reject_credentials():
            client.on_connect(client, None, None, 134, None)

        client.loop_start.side_effect = reject_credentials
        with patch.object(_mock_paho_client, "Client", return_value=client):
            with pytest.raises(ConnectionError, match="rejected connection"):
                plugin._connect_mesh_device()

        assert plugin._mesh_interface is None
        assert plugin._connected is False
        assert plugin.plugin_state == PluginState.STARTING
        client.subscribe.assert_not_called()
        client.publish.assert_not_called()
        client.loop_stop.assert_called_once_with()
        client.disconnect.assert_called_once_with()
        assert not any(
            call.args and call.args[0] == events.MESHTASTIC_CONNECTED
            for call in plugin.event_bus.publish.call_args_list
        )

    def test_rejected_subscription_is_not_advertised_as_connected(
        self,
        mock_app,
        mqtt_gw_config,
    ):
        from reticulumpi import events
        from reticulumpi.plugin_base import PluginState

        plugin = self._plugin_waiting_for_mqtt(mock_app, mqtt_gw_config)
        client = self._paho_client()
        client.subscribe.return_value = (4, None)
        client.loop_start.side_effect = lambda: client.on_connect(
            client,
            None,
            None,
            0,
            None,
        )

        with patch.object(_mock_paho_client, "Client", return_value=client):
            with pytest.raises(ConnectionError, match="subscribe 4"):
                plugin._connect_mesh_device()

        assert plugin._mesh_interface is None
        assert plugin._connected is False
        assert plugin.plugin_state == PluginState.STARTING
        client.publish.assert_not_called()
        client.loop_stop.assert_called_once_with()
        client.disconnect.assert_called_once_with()
        assert not any(
            call.args and call.args[0] == events.MESHTASTIC_CONNECTED
            for call in plugin.event_bus.publish.call_args_list
        )

    def test_connack_timeout_closes_client_without_connected_or_ready(
        self,
        mock_app,
        mqtt_gw_config,
    ):
        from reticulumpi import events
        from reticulumpi.plugin_base import PluginState

        mqtt_gw_config["mqtt"]["connack_timeout_seconds"] = 1.25
        plugin = self._plugin_waiting_for_mqtt(mock_app, mqtt_gw_config)
        client = self._paho_client()
        connack_event = MagicMock()
        connack_event.wait.return_value = False
        with (
            patch.object(_mock_paho_client, "Client", return_value=client),
            patch(
                "reticulumpi.builtin_plugins.meshtastic_gateway.threading.Event",
                return_value=connack_event,
            ),
        ):
            with pytest.raises(TimeoutError, match="did not return CONNACK"):
                plugin._connect_mesh_device()

        connack_event.wait.assert_called_once_with(1.25)
        assert plugin._mesh_interface is None
        assert plugin._connected is False
        assert plugin.plugin_state == PluginState.STARTING
        client.subscribe.assert_not_called()
        client.publish.assert_not_called()
        client.loop_stop.assert_called_once_with()
        client.disconnect.assert_called_once_with()
        assert not any(
            call.args and call.args[0] == events.MESHTASTIC_CONNECTED
            for call in plugin.event_bus.publish.call_args_list
        )

    def test_successful_connack_precedes_primary_publication_and_readiness(
        self,
        mock_app,
        mqtt_gw_config,
    ):
        from reticulumpi import events
        from reticulumpi.plugin_base import PluginState

        plugin = self._plugin_waiting_for_mqtt(mock_app, mqtt_gw_config)
        client = self._paho_client()

        def accept_connection():
            assert plugin._mesh_interface is None
            assert plugin._connected is False
            assert plugin.plugin_state == PluginState.STARTING
            client.on_connect(client, None, None, 0, None)
            assert plugin._mesh_interface is None
            assert plugin._connected is False
            assert plugin.plugin_state == PluginState.STARTING

        client.loop_start.side_effect = accept_connection
        with patch.object(_mock_paho_client, "Client", return_value=client):
            plugin._connect_mesh_device()

        try:
            assert plugin._mesh_interface is not None
            assert plugin._connected is True
            assert plugin.plugin_state == PluginState.READY
            client.subscribe.assert_called_once()
            client.publish.assert_called_once()
            assert (
                sum(
                    1
                    for call in plugin.event_bus.publish.call_args_list
                    if call.args and call.args[0] == events.MESHTASTIC_CONNECTED
                )
                == 1
            )
        finally:
            plugin._close_mesh_interface()

    def test_packet_id_admission_failure_prevents_mqtt_publish(self):
        from reticulumpi.builtin_plugins.meshtastic_gateway import (
            _MeshtasticMQTTClient,
            _PacketIdStateError,
        )

        iface = object.__new__(_MeshtasticMQTTClient)
        iface._lock = threading.Lock()
        iface._my_node_num = 0x12345678
        iface._aes_key = None
        iface._root_topic = "msh/US/2/e/LongFast"
        iface._packet_id_allocator = MagicMock()
        iface._packet_id_allocator.take.side_effect = _PacketIdStateError("unsafe state")
        iface.client = MagicMock()

        with pytest.raises(_PacketIdStateError, match="unsafe state"):
            iface.sendText("must not publish")

        iface.client.publish.assert_not_called()

    def test_close_attempts_disconnect_when_loop_stop_raises(self):
        from reticulumpi.builtin_plugins.meshtastic_gateway import _MeshtasticMQTTClient

        iface = object.__new__(_MeshtasticMQTTClient)
        iface._lock = threading.Lock()
        iface._closed = False
        iface._connack_event = threading.Event()
        iface._logger = None
        iface._finalizer = MagicMock()
        iface.client = MagicMock()
        iface.client.loop_stop.side_effect = RuntimeError("loop did not stop")

        iface.close()

        iface.client.loop_stop.assert_called_once_with()
        iface.client.disconnect.assert_called_once_with()
        iface._finalizer.detach.assert_not_called()
        assert iface._connack_event.is_set()


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


class TestDurablePacketIdAllocator:
    @staticmethod
    def _paths(tmp_path):
        return (
            str(tmp_path / "meshtastic_packet_ids.json"),
            str(tmp_path / "meshtastic_node_num"),
        )

    @staticmethod
    def _write_complete_state(state_path, node_path, node_num, high_watermark):
        from reticulumpi.builtin_plugins.meshtastic_gateway import (
            _PACKET_ID_ENROLLMENT_COMPLETE,
            _PACKET_ID_STATE_SCHEMA,
        )

        with open(node_path, "w") as stream:
            stream.write(f"{node_num:08x}\n")
        with open(state_path, "w") as stream:
            json.dump(
                {
                    "schema": _PACKET_ID_STATE_SCHEMA,
                    "node_num": node_num,
                    "high_watermark": high_watermark,
                    "enrollment": _PACKET_ID_ENROLLMENT_COMPLETE,
                },
                stream,
            )

    def test_restart_reservations_are_disjoint_even_if_first_block_is_unused(self, tmp_path):
        from reticulumpi.builtin_plugins.meshtastic_gateway import _DurablePacketIdAllocator

        state_path, node_path = self._paths(tmp_path)
        first = _DurablePacketIdAllocator(state_path, node_path, block_size=4)
        first_reserved_high = json.loads(Path(state_path).read_text())["high_watermark"]

        second = _DurablePacketIdAllocator(state_path, node_path, block_size=4)
        second_ids = [second.take() for _ in range(4)]

        assert second.node_num == first.node_num
        assert min(second_ids) > first_reserved_high
        assert len(set(second_ids)) == 4
        assert 0 not in second_ids

    def test_pending_enrollment_recovers_after_node_file_commit_failure(self, tmp_path):
        from reticulumpi.builtin_plugins import meshtastic_gateway as mg

        state_path, node_path = self._paths(tmp_path)
        legacy_node = 0x1A2B3C4D
        with open(node_path, "w") as stream:
            stream.write(f"{legacy_node:08x}\n")
        original_atomic_write = mg._atomic_write_private_text

        def fail_node_commit(path, content):
            if os.path.abspath(path) == os.path.abspath(node_path):
                raise OSError("simulated node-file commit failure")
            return original_atomic_write(path, content)

        with patch.object(mg, "_atomic_write_private_text", side_effect=fail_node_commit):
            with pytest.raises(OSError, match="node-file commit failure"):
                mg._DurablePacketIdAllocator(state_path, node_path, block_size=4)

        pending = json.loads(Path(state_path).read_text())
        assert pending["enrollment"] == mg._PACKET_ID_ENROLLMENT_PENDING
        assert pending["node_num"] != legacy_node
        assert int(Path(node_path).read_text().strip(), 16) == legacy_node

        recovered = mg._DurablePacketIdAllocator(state_path, node_path, block_size=4)
        complete = json.loads(Path(state_path).read_text())
        assert complete["enrollment"] == mg._PACKET_ID_ENROLLMENT_COMPLETE
        assert recovered.node_num == pending["node_num"]
        assert int(Path(node_path).read_text().strip(), 16) == recovered.node_num
        assert recovered.take() > pending["high_watermark"]

        restarted = mg._DurablePacketIdAllocator(state_path, node_path, block_size=4)
        assert restarted.node_num == recovered.node_num

    def test_lock_inode_is_stable_and_all_state_files_are_owner_only(self, tmp_path):
        import stat

        from reticulumpi.builtin_plugins.meshtastic_gateway import _DurablePacketIdAllocator

        state_path, node_path = self._paths(tmp_path)
        allocator = _DurablePacketIdAllocator(state_path, node_path, block_size=2)
        lock_path = f"{state_path}.lock"
        lock_inode = os.stat(lock_path).st_ino

        emitted = [allocator.take() for _ in range(3)]

        assert len(set(emitted)) == 3
        assert os.stat(lock_path).st_ino == lock_inode
        for path in (state_path, node_path, lock_path):
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_concurrent_allocators_reserve_disjoint_blocks(self, tmp_path):
        from reticulumpi.builtin_plugins.meshtastic_gateway import _DurablePacketIdAllocator

        state_path, node_path = self._paths(tmp_path)
        _DurablePacketIdAllocator(state_path, node_path, block_size=8)
        barrier = threading.Barrier(3)
        ranges = []
        errors = []

        def reserve_range():
            try:
                barrier.wait(timeout=2)
                allocator = _DurablePacketIdAllocator(
                    state_path,
                    node_path,
                    block_size=8,
                )
                ranges.append([allocator.take() for _ in range(8)])
            except BaseException as exc:
                errors.append(exc)

        workers = [threading.Thread(target=reserve_range) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=2)
        for worker in workers:
            worker.join(timeout=2)

        assert all(not worker.is_alive() for worker in workers)
        assert errors == []
        assert len(ranges) == 2
        assert set(ranges[0]).isdisjoint(ranges[1])
        assert all(packet_id != 0 for reserved in ranges for packet_id in reserved)

    def test_persistence_failure_does_not_issue_an_unreserved_id(self, tmp_path):
        from reticulumpi.builtin_plugins import meshtastic_gateway as mg

        state_path, node_path = self._paths(tmp_path)
        allocator = mg._DurablePacketIdAllocator(state_path, node_path, block_size=1)
        first = allocator.take()
        state_before = Path(state_path).read_text()

        with patch.object(
            mg,
            "_atomic_write_private_text",
            side_effect=OSError("simulated durable write failure"),
        ):
            with pytest.raises(OSError, match="durable write failure"):
                allocator.take()

        assert Path(state_path).read_text() == state_before
        next_id = allocator.take()
        assert next_id == first + 1

    def test_uint32_boundary_emits_max_then_fails_closed_before_zero(self, tmp_path):
        from reticulumpi.builtin_plugins.meshtastic_gateway import (
            _DurablePacketIdAllocator,
            _MAX_MESHTASTIC_PACKET_ID,
            _PacketIdExhaustedError,
        )

        state_path, node_path = self._paths(tmp_path)
        self._write_complete_state(
            state_path,
            node_path,
            0x12345678,
            _MAX_MESHTASTIC_PACKET_ID - 1,
        )
        allocator = _DurablePacketIdAllocator(state_path, node_path, block_size=4)

        assert allocator.take() == _MAX_MESHTASTIC_PACKET_ID
        with pytest.raises(_PacketIdExhaustedError, match="nonce domain is exhausted"):
            allocator.take()
        assert json.loads(Path(state_path).read_text())["high_watermark"] == 0xFFFFFFFF

    @pytest.mark.parametrize(
        "state_content",
        [
            "not-json\n",
            json.dumps(
                {
                    "schema": 1,
                    "node_num": 0x12345678,
                    "high_watermark": "wrong-type",
                    "enrollment": "complete",
                }
            ),
        ],
    )
    def test_corrupt_recognized_state_fails_closed(self, tmp_path, state_content):
        from reticulumpi.builtin_plugins.meshtastic_gateway import (
            _DurablePacketIdAllocator,
            _PacketIdStateError,
        )

        state_path, node_path = self._paths(tmp_path)
        with open(node_path, "w") as stream:
            stream.write("12345678\n")
        with open(state_path, "w") as stream:
            stream.write(state_content)

        with pytest.raises(_PacketIdStateError, match="packet-ID state"):
            _DurablePacketIdAllocator(state_path, node_path, block_size=4)
        assert Path(state_path).read_text() == state_content

    def test_complete_state_node_mismatch_fails_closed_without_repair(self, tmp_path):
        from reticulumpi.builtin_plugins.meshtastic_gateway import (
            _DurablePacketIdAllocator,
            _PacketIdStateError,
        )

        state_path, node_path = self._paths(tmp_path)
        self._write_complete_state(state_path, node_path, 0x12345678, 100)
        with open(node_path, "w") as stream:
            stream.write("23456789\n")

        with pytest.raises(_PacketIdStateError, match="does not match"):
            _DurablePacketIdAllocator(state_path, node_path, block_size=4)
        assert Path(node_path).read_text().strip() == "23456789"


# ---------------------------------------------------------------------------
# TestDrainSendQueue
# ---------------------------------------------------------------------------


def _make_mock_lxmf_message(source_hex="aabbccdd" + "0" * 24, content="hello"):
    msg = MagicMock()
    msg.source_hash = bytes.fromhex(source_hex)
    msg.content_as_string.return_value = content
    return msg


def _init_queue_state(plugin):
    """Set up the queue/lock attributes that start() normally initializes."""
    import collections
    import threading

    plugin._lock = threading.Lock()
    plugin._active = True
    plugin._mode = "serial"
    plugin._connected = True
    plugin._mesh_interface = MagicMock()
    plugin._serial_listener = None
    plugin._serial_open_generation = 1
    plugin._serial_active_generation = 1
    plugin._send_min_interval = 0
    plugin._last_send_time = 0.0
    plugin._send_queue_max = int(plugin.config.get("send_queue_size", 10))
    plugin._send_queue_ttl = float(plugin.config.get("send_queue_ttl", 60))
    plugin._send_queue = collections.deque()
    plugin._send_queue_dropped = 0
    plugin._msgs_rate_limited = 0
    plugin._msgs_lxmf_to_mesh = 0
    plugin._last_lxmf_msg_time = None
    plugin._pending_lxmf = collections.deque()
    plugin._pending_lxmf_max = int(plugin.config.get("pending_lxmf_size", 20))
    plugin._pending_lxmf_ttl = float(plugin.config.get("pending_lxmf_ttl", 120))
    plugin._lxmf_allow_set = set()
    return plugin


class TestDrainSendQueue:
    """Tests for _drain_send_queue — the rate-limited outbound queue."""

    def _setup_plugin(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        return _init_queue_state(plugin)

    def test_drains_multiple_messages(self, mock_app, gw_config):
        plugin = self._setup_plugin(mock_app, gw_config)
        now = time.time()
        for i in range(3):
            plugin._send_queue.append((now, f"msg{i}", 0))
        plugin._drain_send_queue()
        assert plugin._mesh_interface.sendText.call_count == 3
        assert len(plugin._send_queue) == 0

    def test_skips_expired_messages(self, mock_app, gw_config):
        plugin = self._setup_plugin(mock_app, gw_config)
        expired = time.time() - plugin._send_queue_ttl - 10
        now = time.time()
        plugin._send_queue.append((expired, "old", 0))
        plugin._send_queue.append((now, "fresh1", 0))
        plugin._send_queue.append((now, "fresh2", 0))
        plugin._drain_send_queue()
        assert plugin._mesh_interface.sendText.call_count == 2
        assert len(plugin._send_queue) == 0

    def test_uncertain_serial_send_is_not_retried_and_blocks_reopen(
        self,
        gateway_plugin,
    ):
        from reticulumpi.builtin_plugins.meshtastic_gateway import SerialOpenOutcome

        plugin = gateway_plugin
        iface = _make_mock_mesh_interface()
        entered = threading.Event()
        release = threading.Event()

        def blocking_send(*_args, **_kwargs):
            entered.set()
            assert release.wait(timeout=1)

        iface.sendText.side_effect = blocking_send
        with plugin._lock:
            plugin._mesh_interface = iface
            plugin._serial_listener = None
            plugin._connected = True
            plugin._serial_open_generation = 61
            plugin._serial_active_generation = 61
            plugin._send_queue.append((time.time(), "once", 0))
        plugin._serial_command_timeout = 0.02
        plugin._serial_close_timeout = 0.02

        plugin._drain_send_queue()

        assert entered.is_set()
        assert iface.sendText.call_count == 1
        assert list(plugin._send_queue) == []
        assert plugin._mesh_interface is None
        assert plugin._serial_open_generation == 62

        with (
            patch.object(plugin, "_ensure_serial_device_lease", return_value=True),
            patch.object(_mock_meshtastic_serial, "SerialInterface") as constructor,
        ):
            open_result = plugin._open_serial_interface_result(plugin.config["serial_port"])
        assert open_result.outcome is SerialOpenOutcome.TEARDOWN_UNPROVEN
        constructor.assert_not_called()

        release.set()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not plugin._prune_serial_workers():
            time.sleep(0.005)
        assert plugin._prune_serial_workers() is True

    def test_stops_when_rate_limited(self, mock_app, gw_config):
        plugin = self._setup_plugin(mock_app, gw_config)
        plugin._send_min_interval = 9999
        plugin._last_send_time = time.time()
        plugin._send_queue.append((time.time(), "msg", 0))
        plugin._drain_send_queue()
        plugin._mesh_interface.sendText.assert_not_called()
        assert len(plugin._send_queue) == 1

    def test_no_interface_requeues(self, mock_app, gw_config):
        plugin = self._setup_plugin(mock_app, gw_config)
        plugin._connected = False
        plugin._mesh_interface = None
        plugin._serial_listener = None
        plugin._send_queue.append((time.time(), "msg", 0))
        plugin._drain_send_queue()
        assert len(plugin._send_queue) == 1

    def test_empty_queue_no_op(self, mock_app, gw_config):
        plugin = self._setup_plugin(mock_app, gw_config)
        plugin._drain_send_queue()
        plugin._mesh_interface.sendText.assert_not_called()


# ---------------------------------------------------------------------------
# TestEnqueueLxmfSend
# ---------------------------------------------------------------------------


class TestEnqueueLxmfSend:
    """Tests for _enqueue_lxmf_send — queuing rate-limited LXMF messages."""

    def _setup_plugin(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        return _init_queue_state(plugin)

    def test_enqueues_message(self, mock_app, gw_config):
        plugin = self._setup_plugin(mock_app, gw_config)
        msg = _make_mock_lxmf_message()
        plugin._enqueue_lxmf_send(msg)
        assert len(plugin._send_queue) == 1

    def test_drops_oldest_when_full(self, mock_app, gw_config):
        plugin = self._setup_plugin(mock_app, gw_config)
        plugin._send_queue_max = 2
        for i in range(3):
            msg = _make_mock_lxmf_message(content=f"msg{i}")
            plugin._enqueue_lxmf_send(msg)
        assert len(plugin._send_queue) == 2
        assert plugin._send_queue_dropped == 1

    def test_allow_list_blocks(self, mock_app, gw_config):
        plugin = self._setup_plugin(mock_app, gw_config)
        plugin._lxmf_allow_set = {"aaaa" + "0" * 28}
        msg = _make_mock_lxmf_message(source_hex="bbbbccdd" + "0" * 24)
        plugin._enqueue_lxmf_send(msg)
        assert len(plugin._send_queue) == 0

    def test_allow_list_permits(self, mock_app, gw_config):
        source = "aabbccdd" + "0" * 24
        plugin = self._setup_plugin(mock_app, gw_config)
        plugin._lxmf_allow_set = {source}
        msg = _make_mock_lxmf_message(source_hex=source)
        plugin._enqueue_lxmf_send(msg)
        assert len(plugin._send_queue) == 1

    def test_formatting_error_no_crash(self, mock_app, gw_config):
        plugin = self._setup_plugin(mock_app, gw_config)
        msg = _make_mock_lxmf_message()
        msg.content_as_string.side_effect = RuntimeError("boom")
        plugin._enqueue_lxmf_send(msg)
        assert len(plugin._send_queue) == 0


# ---------------------------------------------------------------------------
# TestRetryPendingLxmf
# ---------------------------------------------------------------------------


class TestRetryPendingLxmf:
    """Tests for _retry_pending_lxmf — retrying deferred LXMF messages."""

    def _setup_plugin(self, mock_app, gw_config):
        plugin = _make_plugin_no_start(mock_app, gw_config)
        return _init_queue_state(plugin)

    def test_retries_all_items(self, mock_app, gw_config):
        plugin = self._setup_plugin(mock_app, gw_config)
        now = time.time()
        for i in range(3):
            plugin._pending_lxmf.append((now, bytes(16), f"text{i}"))
        with patch.object(plugin, "_try_send_lxmf", return_value=True) as mock_send:
            plugin._retry_pending_lxmf()
        assert mock_send.call_count == 3
        assert len(plugin._pending_lxmf) == 0

    def test_continues_past_failure(self, mock_app, gw_config):
        plugin = self._setup_plugin(mock_app, gw_config)
        now = time.time()
        hash_a, hash_b, hash_c = bytes(16), bytes(range(16)), bytes([0xFF] * 16)
        plugin._pending_lxmf.append((now, hash_a, "a"))
        plugin._pending_lxmf.append((now, hash_b, "b"))
        plugin._pending_lxmf.append((now, hash_c, "c"))

        def side_effect(h, _text):
            return h != hash_a

        with patch.object(plugin, "_try_send_lxmf", side_effect=side_effect) as mock_send:
            plugin._retry_pending_lxmf()
        assert mock_send.call_count == 3
        assert len(plugin._pending_lxmf) == 1
        assert plugin._pending_lxmf[0][1] == hash_a

    def test_expired_items_removed(self, mock_app, gw_config):
        plugin = self._setup_plugin(mock_app, gw_config)
        expired = time.time() - plugin._pending_lxmf_ttl - 10
        now = time.time()
        plugin._pending_lxmf.append((expired, bytes(16), "old"))
        plugin._pending_lxmf.append((now, bytes(range(16)), "fresh"))
        with patch.object(plugin, "_try_send_lxmf", return_value=True) as mock_send:
            plugin._retry_pending_lxmf()
        assert mock_send.call_count == 1
        assert len(plugin._pending_lxmf) == 0

    def test_empty_queue_no_op(self, mock_app, gw_config):
        plugin = self._setup_plugin(mock_app, gw_config)
        with patch.object(plugin, "_try_send_lxmf") as mock_send:
            plugin._retry_pending_lxmf()
        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# TestMqttNodeEviction
# ---------------------------------------------------------------------------


class TestMqttNodeEviction:
    """Test TTL and max-size eviction on _MeshtasticMQTTClient.nodes."""

    @staticmethod
    def _make_client():
        from reticulumpi.builtin_plugins.meshtastic_gateway import _MeshtasticMQTTClient

        client = MagicMock(spec=_MeshtasticMQTTClient)
        client._lock = __import__("threading").Lock()
        client._my_node_num = 0xAABBCCDD
        client._long_name = "Test"
        client._short_name = "TS"
        client.nodes = {}
        client._max_nodes = 10
        client._node_ttl_seconds = 3600.0
        client._node_inserts_since_eviction = 0
        return client

    def test_evicts_stale_nodes_by_ttl(self):
        from reticulumpi.builtin_plugins.meshtastic_gateway import _MeshtasticMQTTClient

        client = self._make_client()
        now = time.time()
        client.nodes = {
            "!self": {"isSelf": True, "lastHeard": now},
            "!old1": {"lastHeard": now - 7200},
            "!old2": {"lastHeard": now - 7200},
            "!new1": {"lastHeard": now - 100},
        }
        client._node_inserts_since_eviction = 63

        _MeshtasticMQTTClient._maybe_evict_nodes(client)

        assert "!self" in client.nodes
        assert "!new1" in client.nodes
        assert "!old1" not in client.nodes
        assert "!old2" not in client.nodes

    def test_evicts_excess_by_max_size(self):
        from reticulumpi.builtin_plugins.meshtastic_gateway import _MeshtasticMQTTClient

        client = self._make_client()
        client._max_nodes = 5
        now = time.time()
        client.nodes = {
            "!self": {"isSelf": True, "lastHeard": now},
        }
        for i in range(10):
            client.nodes[f"!node{i:04x}"] = {"lastHeard": now - (10 - i)}
        client._node_inserts_since_eviction = 63

        _MeshtasticMQTTClient._maybe_evict_nodes(client)

        assert "!self" in client.nodes
        assert len(client.nodes) <= int(5 * 0.75) + 1  # 75% of max + self

    def test_self_node_survives_eviction(self):
        from reticulumpi.builtin_plugins.meshtastic_gateway import _MeshtasticMQTTClient

        client = self._make_client()
        client._max_nodes = 2
        now = time.time()
        client.nodes = {
            "!self": {"isSelf": True, "lastHeard": now - 99999},
            "!a": {"lastHeard": now - 1},
            "!b": {"lastHeard": now - 2},
            "!c": {"lastHeard": now - 3},
        }
        client._node_inserts_since_eviction = 63

        _MeshtasticMQTTClient._maybe_evict_nodes(client)

        assert "!self" in client.nodes

    def test_skips_eviction_before_threshold(self):
        from reticulumpi.builtin_plugins.meshtastic_gateway import _MeshtasticMQTTClient

        client = self._make_client()
        now = time.time()
        client.nodes = {
            "!old": {"lastHeard": now - 99999},
        }
        client._node_inserts_since_eviction = 10  # below 64

        _MeshtasticMQTTClient._maybe_evict_nodes(client)

        assert "!old" in client.nodes  # not evicted yet


# ═══════════════════════════════════════════════════════════════════
# Read receipts
# ═══════════════════════════════════════════════════════════════════


class TestPureSerialPublicSend:
    def test_primary_serial_send_supports_ack_contract(self, gateway_plugin):
        iface = _make_mock_mesh_interface()
        callback = MagicMock()
        with gateway_plugin._lock:
            gateway_plugin._mesh_interface = iface
            gateway_plugin._serial_listener = None
            gateway_plugin._connected = True

        result = gateway_plugin.send_message(
            "hello",
            destination_id="!aabb1122",
            on_ack=callback,
        )

        assert result["sent"] is True
        assert result["ack_tracking"] == "serial"
        on_response = iface.sendText.call_args.kwargs["onResponse"]
        assert on_response.__name__ == "onAckNak"
        on_response({"decoded": {"routing": {"errorReason": "NONE"}}})
        callback.assert_called_once_with(True)


class TestReadReceipts:
    def test_send_read_receipt(self, gateway_plugin):
        mock_iface = MagicMock()
        mock_iface.sendData = MagicMock()
        gateway_plugin._serial_listener = mock_iface

        result = gateway_plugin.send_read_receipt(42, "!aabb1122")
        assert result["sent"] is True
        mock_iface.sendData.assert_called_once()
        call_kwargs = mock_iface.sendData.call_args
        payload = call_kwargs[0][0]
        assert payload[0] == 0x01
        assert int.from_bytes(payload[1:5], "big") == 42
        assert call_kwargs[1]["destinationId"] == "!aabb1122"
        assert call_kwargs[1]["wantAck"] is False

    def test_send_read_receipt_no_serial(self, gateway_plugin):
        gateway_plugin._serial_listener = None
        result = gateway_plugin.send_read_receipt(42, "!aabb1122")
        assert result["sent"] is False
        assert result["reason"] == "serial_interface_unavailable"

    def test_send_read_receipt_uses_pure_serial_primary(self, gateway_plugin):
        iface = _make_mock_mesh_interface()
        iface.sendData = MagicMock()
        gateway_plugin._serial_listener = None
        gateway_plugin._mesh_interface = iface
        gateway_plugin._connected = True

        result = gateway_plugin.send_read_receipt(7, "!aabb1122")

        assert result["sent"] is True
        iface.sendData.assert_called_once()

    def test_handle_private_app_read_receipt(self, gateway_plugin):
        packet = {
            "from": 0xAABB1122,
            "fromId": "!aabb1122",
            "decoded": {
                "portnum": "PRIVATE_APP",
                "payload": bytes([0x01, 0x00, 0x00, 0x00, 0x2A]),  # packet_id = 42
            },
        }
        gateway_plugin._active = True
        gateway_plugin._handle_private_app(packet)
        gateway_plugin.event_bus.publish.assert_called_with(
            "meshtastic.read_receipt_received",
            {
                "from_id": "!aabb1122",
                "from_name": ANY,
                "packet_id": 42,
            },
        )

    def test_on_mesh_data_dispatches_private_app(self, gateway_plugin):
        gateway_plugin._active = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        packet = {
            "from": 0x11223344,
            "fromId": "!11223344",
            "decoded": {
                "portnum": "PRIVATE_APP",
                "payload": bytes([0x01, 0x00, 0x00, 0x01, 0x00]),  # packet_id = 256
            },
        }
        gateway_plugin._on_mesh_data(
            packet,
            interface=gateway_plugin._mesh_interface,
        )
        gateway_plugin.event_bus.publish.assert_called()

    def test_on_mesh_data_ignores_inactive(self, gateway_plugin):
        gateway_plugin._active = False
        gateway_plugin.event_bus.publish.reset_mock()
        packet = {
            "decoded": {"portnum": "PRIVATE_APP", "payload": bytes([0x01, 0, 0, 0, 1])},
        }
        gateway_plugin._on_mesh_data(packet)
        gateway_plugin.event_bus.publish.assert_not_called()

    def test_global_pubsub_private_app_rejects_foreign_interface(self, gateway_plugin):
        gateway_plugin._active = True
        gateway_plugin._mesh_interface = _make_mock_mesh_interface()
        packet = {
            "from": 0x11223344,
            "fromId": "!11223344",
            "decoded": {
                "portnum": "PRIVATE_APP",
                "payload": bytes([0x01, 0x00, 0x00, 0x01, 0x00]),
            },
        }
        gateway_plugin.event_bus.publish.reset_mock()

        gateway_plugin._on_mesh_data(packet, interface=MagicMock())

        gateway_plugin.event_bus.publish.assert_not_called()

    def test_handle_private_app_ignores_short_payload(self, gateway_plugin):
        gateway_plugin.event_bus.publish.reset_mock()
        packet = {
            "from": 0xAABB1122,
            "fromId": "!aabb1122",
            "decoded": {"portnum": "PRIVATE_APP", "payload": bytes([0x01, 0x00])},
        }
        gateway_plugin._handle_private_app(packet)
        gateway_plugin.event_bus.publish.assert_not_called()

    def test_handle_private_app_ignores_unknown_tag(self, gateway_plugin):
        gateway_plugin.event_bus.publish.reset_mock()
        packet = {
            "from": 0xAABB1122,
            "fromId": "!aabb1122",
            "decoded": {
                "portnum": "PRIVATE_APP",
                "payload": bytes([0xFF, 0x00, 0x00, 0x00, 0x2A]),
            },
        }
        gateway_plugin._handle_private_app(packet)
        gateway_plugin.event_bus.publish.assert_not_called()


# ---------------------------------------------------------------------------
# TestSerialOpenTimeout — _open_serial_interface_with_timeout lock-leak fix
# ---------------------------------------------------------------------------


class TestSerialOpenTimeout:
    """Verify that abandoned serial-open workers release the port lock."""

    @pytest.fixture
    def probe_plugin(self, mock_app, mqtt_gw_config):
        mqtt_gw_config["device_probe_port"] = "/dev/meshtastic"
        mqtt_gw_config["device_probe_open_timeout"] = 0.3
        mqtt_gw_config["serial_retry_interval"] = 5
        plugin = _make_plugin_no_start(mock_app, mqtt_gw_config)
        plugin._device_probe_port = "/dev/meshtastic"
        plugin._device_probe_open_timeout = 0.3
        plugin._serial_retry_interval = 5.0
        plugin._device_probe_interval = 300.0
        plugin._lock = threading.Lock()
        plugin._serial_device_lease = None
        plugin._serial_open_generation = 0
        plugin._serial_active_generation = 0
        plugin._active = True
        plugin._ensure_serial_device_lease = MagicMock(return_value=True)
        return plugin

    def test_success_within_timeout(self, probe_plugin):
        """Constructor completes in time — returns interface, close() not called."""
        mock_iface = MagicMock()
        _mock_meshtastic_serial.SerialInterface.return_value = mock_iface
        _mock_meshtastic_serial.SerialInterface.side_effect = None

        result = probe_plugin._open_serial_interface_with_timeout()

        assert result is mock_iface
        mock_iface.close.assert_not_called()

    def test_timeout_closes_abandoned_interface(self, probe_plugin):
        """Constructor completes after timeout — worker closes the interface."""
        mock_iface = MagicMock()
        gate = threading.Event()

        def slow_constructor(**kwargs):
            gate.wait(timeout=5)
            return mock_iface

        _mock_meshtastic_serial.SerialInterface.side_effect = slow_constructor

        result = probe_plugin._open_serial_interface_with_timeout()
        assert result is None

        gate.set()
        time.sleep(0.2)

        mock_iface.close.assert_called_once()

    def test_timeout_constructor_error_no_crash(self, probe_plugin):
        """Constructor errors after timeout — no crash, no close attempt."""
        gate = threading.Event()

        def slow_error(**kwargs):
            gate.wait(timeout=5)
            raise OSError("device vanished")

        _mock_meshtastic_serial.SerialInterface.side_effect = slow_error

        result = probe_plugin._open_serial_interface_with_timeout()
        assert result is None

        gate.set()
        time.sleep(0.2)

    def test_constructor_error_within_timeout(self, probe_plugin):
        """Constructor errors within timeout — returns None, error logged."""
        _mock_meshtastic_serial.SerialInterface.side_effect = OSError(
            "[Errno 13] Permission denied"
        )

        result = probe_plugin._open_serial_interface_with_timeout()

        assert result is None

    def test_timeout_log_promises_no_reopen_before_worker_exit(self, probe_plugin, caplog):
        gate = threading.Event()

        def slow_constructor(**kwargs):
            gate.wait(timeout=5)
            return MagicMock()

        _mock_meshtastic_serial.SerialInterface.side_effect = slow_constructor

        import logging

        with caplog.at_level(logging.WARNING, logger="reticulumpi.plugin.meshtastic_gateway"):
            probe_plugin._open_serial_interface_with_timeout()

        gate.set()
        time.sleep(0.2)

        timeout_msgs = [
            record
            for record in caplog.records
            if record.name == "reticulumpi.plugin.meshtastic_gateway"
            and "timed out" in record.message
        ]
        assert len(timeout_msgs) == 1
        assert "lease remains held" in timeout_msgs[0].message
        assert "no replacement will start" in timeout_msgs[0].message

    def test_open_path_does_not_expose_raw_process_fd_cleanup(self, probe_plugin):
        """Serial teardown is owned by interface workers, never /proc fd scans."""
        _mock_meshtastic_serial.SerialInterface.return_value = MagicMock()
        _mock_meshtastic_serial.SerialInterface.side_effect = None

        assert not hasattr(probe_plugin, "_close_leaked_serial_fd")
        assert probe_plugin._open_serial_interface_with_timeout() is not None

    def test_gateway_has_no_automatic_missing_nodeinfo_sender(self, probe_plugin):
        assert not hasattr(probe_plugin, "_request_missing_nodeinfo")

    def test_abandoned_worker_cap_refuses_new_constructor(self, probe_plugin):
        abandoned = []
        worker = MagicMock(name="abandoned")
        worker.is_alive.return_value = True
        abandoned.append(worker)
        probe_plugin._abandoned_serial_threads = abandoned
        _mock_meshtastic_serial.SerialInterface.reset_mock()

        assert probe_plugin._open_serial_interface_with_timeout() is None
        _mock_meshtastic_serial.SerialInterface.assert_not_called()
        assert probe_plugin.plugin_health.value == "degraded"

    @pytest.mark.parametrize(
        ("error", "outcome"),
        [
            (PermissionError("denied"), "permission"),
            (FileNotFoundError("gone"), "missing"),
            (OSError(16, "Resource busy"), "busy"),
        ],
    )
    def test_immediate_open_failures_are_typed_and_not_reset_eligible(
        self,
        probe_plugin,
        error,
        outcome,
    ):
        _mock_meshtastic_serial.SerialInterface.side_effect = error

        result = probe_plugin._open_serial_interface_result()

        assert result.outcome.value == outcome
        assert result.reset_eligible is False

    def test_stop_retains_lease_until_abandoned_work_exits(self, gateway_plugin):
        plugin = gateway_plugin
        lease = MagicMock()
        alive = {"value": True}
        worker = MagicMock()
        worker.is_alive.side_effect = lambda: alive["value"]
        plugin._serial_device_lease = lease
        plugin._abandoned_serial_threads = [worker]
        plugin._active = False

        plugin._release_serial_device_lease()

        lease.release.assert_not_called()
        assert plugin._serial_device_lease is lease
        alive["value"] = False
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not lease.release.called:
            time.sleep(0.005)
        lease.release.assert_called_once_with()
        assert plugin._serial_device_lease is None

    def test_blocked_close_is_bounded_and_marks_teardown_unproven(self, gateway_plugin):
        plugin = gateway_plugin
        iface = MagicMock()
        entered = threading.Event()
        release = threading.Event()

        def blocking_close():
            entered.set()
            assert release.wait(timeout=1)

        iface.close.side_effect = blocking_close
        plugin._serial_close_timeout = 0.02

        started = time.monotonic()
        assert plugin._bounded_close_serial_interface(iface, 1, "test close") is False
        elapsed = time.monotonic() - started

        assert entered.is_set()
        assert elapsed < 0.5
        assert plugin._serial_teardown_unproven is True
        assert plugin._bounded_close_serial_interface(iface, 1, "duplicate") is False
        assert iface.close.call_count == 1
        release.set()

    def test_successful_exact_close_is_idempotent_across_lifecycle_races(self, gateway_plugin):
        plugin = gateway_plugin
        iface = MagicMock()

        assert plugin._bounded_close_serial_interface(iface, 9, "first close") is True
        assert plugin._bounded_close_serial_interface(iface, 9, "racing close") is True

        iface.close.assert_called_once_with()
        assert plugin._serial_unclosed_interfaces == {}

    def test_concurrent_constructor_is_refused_before_second_sdk_entry(self, probe_plugin):
        from reticulumpi.builtin_plugins.meshtastic_gateway import SerialOpenOutcome

        _mock_meshtastic_serial.SerialInterface.reset_mock()
        entered = threading.Event()
        release = threading.Event()
        iface = MagicMock()

        def blocking_constructor(**_kwargs):
            entered.set()
            assert release.wait(timeout=1)
            return iface

        _mock_meshtastic_serial.SerialInterface.side_effect = blocking_constructor
        first_results = []
        first = threading.Thread(
            target=lambda: first_results.append(probe_plugin._open_serial_interface_result())
        )
        first.start()
        assert entered.wait(timeout=1)

        second = probe_plugin._open_serial_interface_result()

        assert second.outcome is SerialOpenOutcome.TEARDOWN_UNPROVEN
        assert _mock_meshtastic_serial.SerialInterface.call_count == 1
        release.set()
        first.join(timeout=1)
        assert not first.is_alive()
        assert first_results[0].opened is True

    def test_lease_release_waits_for_constructor_and_late_cleanup(self, probe_plugin):
        entered = threading.Event()
        release = threading.Event()
        iface = MagicMock()
        lease = MagicMock()
        probe_plugin._serial_device_lease = lease
        probe_plugin._device_probe_open_timeout = 0.03
        probe_plugin._active = False

        def blocking_constructor(**_kwargs):
            entered.set()
            assert release.wait(timeout=1)
            return iface

        _mock_meshtastic_serial.SerialInterface.side_effect = blocking_constructor
        results = []
        opener = threading.Thread(
            target=lambda: results.append(probe_plugin._open_serial_interface_result())
        )
        opener.start()
        assert entered.wait(timeout=1)

        probe_plugin._release_serial_device_lease()

        lease.release.assert_not_called()
        opener.join(timeout=1)
        assert not opener.is_alive()
        assert results[0].outcome.value == "timeout"
        release.set()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not lease.release.called:
            time.sleep(0.005)
        iface.close.assert_called_once_with()
        lease.release.assert_called_once_with()

    def test_close_exception_quarantines_exact_handle_and_blocks_reopen(
        self,
        gateway_plugin,
    ):
        from reticulumpi.builtin_plugins.meshtastic_gateway import SerialOpenOutcome

        plugin = gateway_plugin
        iface = MagicMock()
        iface.close.side_effect = OSError("close failed")
        lease = MagicMock()
        plugin._serial_device_lease = lease
        plugin._active = False

        assert plugin._bounded_close_serial_interface(iface, 81, "failing close") is False

        key = (id(iface), 81)
        assert plugin._serial_unclosed_interfaces[key] is iface
        assert plugin._serial_teardown_unproven is True
        with patch.object(plugin, "_schedule_serial_lease_release") as schedule:
            plugin._release_serial_device_lease()
        lease.release.assert_not_called()
        schedule.assert_not_called()

        with (
            patch.object(plugin, "_ensure_serial_device_lease", return_value=True),
            patch.object(_mock_meshtastic_serial, "SerialInterface") as constructor,
        ):
            result = plugin._open_serial_interface_result(plugin.config["serial_port"])
        assert result.outcome is SerialOpenOutcome.TEARDOWN_UNPROVEN
        constructor.assert_not_called()

    def test_late_constructor_close_exception_remains_quarantined(self, probe_plugin):
        from reticulumpi.builtin_plugins.meshtastic_gateway import SerialOpenOutcome

        entered = threading.Event()
        release = threading.Event()
        iface = MagicMock()
        iface.close.side_effect = OSError("late close failed")
        probe_plugin._device_probe_open_timeout = 0.03

        def blocking_constructor(**_kwargs):
            entered.set()
            assert release.wait(timeout=1)
            return iface

        _mock_meshtastic_serial.SerialInterface.side_effect = blocking_constructor
        timed_out = probe_plugin._open_serial_interface_result()
        assert timed_out.outcome is SerialOpenOutcome.TIMEOUT
        assert entered.is_set()
        release.set()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not probe_plugin._serial_unclosed_interfaces:
            time.sleep(0.005)

        assert any(value is iface for value in probe_plugin._serial_unclosed_interfaces.values())
        _mock_meshtastic_serial.SerialInterface.reset_mock()
        blocked = probe_plugin._open_serial_interface_result()
        assert blocked.outcome is SerialOpenOutcome.TEARDOWN_UNPROVEN
        _mock_meshtastic_serial.SerialInterface.assert_not_called()

    def test_stale_unpublished_interface_is_closed_even_after_generation_moves(
        self,
        gateway_plugin,
    ):
        plugin = gateway_plugin
        iface = MagicMock()
        with plugin._lock:
            plugin._mesh_interface = None
            plugin._serial_listener = None
            plugin._serial_active_generation = 90
            plugin._serial_open_generation = 91
            plugin._serial_probe_candidate = None

        plugin._poison_serial_generation(iface, 90, "stale unpublished")

        iface.close.assert_called_once_with()


# ---------------------------------------------------------------------------
# TestStartupHangDetection — detect firmware hang during serial open
# ---------------------------------------------------------------------------


class TestStartupHangDetection:
    """Verify startup firmware hang detection and USB bus reset recovery."""

    @pytest.fixture
    def wd_plugin(self, mock_app, mqtt_gw_config):
        mqtt_gw_config["device_probe_port"] = "/dev/meshtastic"
        mqtt_gw_config["device_probe_open_timeout"] = 0.1
        mqtt_gw_config["serial_retry_interval"] = 5
        mqtt_gw_config["firmware_watchdog"] = {
            "enabled": True,
            "open_failure_threshold": 3,
            "auto_reset": True,
            "usb_power_cycle": True,
            "max_resets_per_hour": 3,
            "silence_timeout": 300,
            "probe_timeout": 15,
        }
        plugin = _make_plugin_no_start(mock_app, mqtt_gw_config)
        plugin._device_probe_port = "/dev/meshtastic"
        plugin._device_probe_open_timeout = 0.1
        plugin._serial_retry_interval = 5.0
        plugin._device_probe_interval = 300.0
        plugin._fw_watchdog_enabled = True
        plugin._fw_open_failure_threshold = 3
        plugin._fw_consecutive_open_failures = 0
        plugin._fw_first_open_failure_time = 0.0
        plugin._fw_auto_reset = True
        plugin._fw_usb_power_cycle = True
        plugin._fw_max_resets_per_hour = 3
        plugin._fw_reset_timestamps = []
        plugin._fw_hang_detected = False
        plugin._fw_hang_reason = None
        plugin._fw_total_hangs = 0
        plugin._fw_total_resets = 0
        plugin._fw_silence_timeout = 300.0
        plugin._fw_probe_timeout = 15.0
        plugin._last_serial_activity = 0.0
        plugin._last_mqtt_activity = 0.0
        plugin._serial_listener = None
        plugin._mesh_interface = None
        plugin._fw_reset_limiter = None
        plugin._fw_recovery_state = "healthy"
        plugin._fw_recovery_pending = False
        plugin._fw_recovery_method = None
        plugin._fw_recovery_started_at = None
        plugin._fw_recovery_not_before = 0.0
        plugin._fw_recovery_reopen_delay = 0.0
        plugin._fw_last_verified_at = None
        plugin._fw_last_recovery_error = None
        plugin._serial_device_lease = None
        plugin._lock = threading.Lock()
        return plugin

    def test_counter_increments_on_failure(self, wd_plugin):
        assert wd_plugin._fw_consecutive_open_failures == 0
        wd_plugin._fw_consecutive_open_failures += 1
        assert wd_plugin._fw_consecutive_open_failures == 1
        wd_plugin._fw_consecutive_open_failures += 1
        assert wd_plugin._fw_consecutive_open_failures == 2

    def test_counter_resets_on_success(self, wd_plugin):
        wd_plugin._fw_consecutive_open_failures = 2
        wd_plugin._fw_first_open_failure_time = time.monotonic() - 60
        wd_plugin._fw_consecutive_open_failures = 0
        wd_plugin._fw_first_open_failure_time = 0.0
        assert wd_plugin._fw_consecutive_open_failures == 0
        assert wd_plugin._fw_first_open_failure_time == 0.0

    def test_busy_open_never_counts_toward_firmware_reset(self, wd_plugin):
        from reticulumpi.builtin_plugins.meshtastic_gateway import (
            SerialOpenOutcome,
            SerialOpenResult,
        )

        wd_plugin._fw_consecutive_open_failures = 2
        with patch.object(wd_plugin, "_handle_startup_firmware_hang") as recover:
            wd_plugin._record_serial_open_failure(SerialOpenResult(SerialOpenOutcome.BUSY))

        assert wd_plugin._fw_consecutive_open_failures == 0
        recover.assert_not_called()

    def test_only_real_timeouts_reach_startup_recovery_threshold(self, wd_plugin):
        from reticulumpi.builtin_plugins.meshtastic_gateway import (
            SerialOpenOutcome,
            SerialOpenResult,
        )

        wd_plugin._fw_consecutive_open_failures = 2
        with patch.object(wd_plugin, "_handle_startup_firmware_hang") as recover:
            wd_plugin._record_serial_open_failure(SerialOpenResult(SerialOpenOutcome.TIMEOUT))

        recover.assert_called_once_with()

    def test_threshold_triggers_hang_event(self, wd_plugin):
        wd_plugin._fw_consecutive_open_failures = 3
        wd_plugin._fw_first_open_failure_time = time.monotonic() - 90

        with (
            patch.object(wd_plugin, "_attempt_startup_recovery"),
            patch.object(wd_plugin, "_fw_reset_allowed", return_value=True),
        ):
            wd_plugin._handle_startup_firmware_hang()

        wd_plugin.event_bus.publish.assert_called()
        call_args = wd_plugin.event_bus.publish.call_args
        assert call_args[0][0] == "meshtastic.firmware_hang"
        assert call_args[0][1]["reason"] == "serial_open_timeout"
        assert call_args[0][1]["consecutive_failures"] == 3

    def test_hang_sets_state_flags(self, wd_plugin):
        wd_plugin._fw_consecutive_open_failures = 3
        with (
            patch.object(wd_plugin, "_attempt_startup_recovery"),
            patch.object(wd_plugin, "_fw_reset_allowed", return_value=True),
        ):
            wd_plugin._handle_startup_firmware_hang()

        assert wd_plugin._fw_hang_detected is True
        assert wd_plugin._fw_total_hangs == 1

    def test_circuit_breaker_blocks_recovery(self, wd_plugin):
        wd_plugin._fw_consecutive_open_failures = 3
        wd_plugin._fw_reset_timestamps = [time.monotonic()] * 3

        with patch.object(wd_plugin, "_attempt_startup_recovery") as mock_recover:
            wd_plugin._handle_startup_firmware_hang()
            mock_recover.assert_not_called()

    def test_auto_reset_false_skips_recovery(self, wd_plugin):
        wd_plugin._fw_auto_reset = False
        wd_plugin._fw_consecutive_open_failures = 3

        with patch.object(wd_plugin, "_attempt_startup_recovery") as mock_recover:
            wd_plugin._handle_startup_firmware_hang()
            mock_recover.assert_not_called()

        wd_plugin.event_bus.publish.assert_called()

    def test_startup_recovery_calls_usb_reset(self, wd_plugin):
        with (
            patch.object(wd_plugin, "_check_usb_present", return_value=True),
            patch.object(
                wd_plugin, "_resolve_usb_device_path", return_value="/dev/bus/usb/004/011"
            ),
            patch.object(wd_plugin, "_usb_bus_reset", return_value={"ok": True}),
            patch.object(wd_plugin, "_reserve_reset", return_value=True) as mock_reserve,
        ):
            assert wd_plugin._attempt_startup_recovery() is True
            mock_reserve.assert_called_once_with("usb_bus_reset_startup")
            assert wd_plugin._fw_recovery_pending is True
            assert wd_plugin._fw_recovery_method == "usb_bus_reset_startup"

    def test_startup_recovery_honors_disabled_usb_reset(self, wd_plugin):
        wd_plugin._fw_usb_power_cycle = False
        with patch.object(wd_plugin, "_usb_bus_reset") as mock_reset:
            assert wd_plugin._attempt_startup_recovery() is False
        mock_reset.assert_not_called()

    def test_startup_recovery_skips_when_usb_gone(self, wd_plugin):
        with (
            patch.object(wd_plugin, "_check_usb_present", return_value=False),
            patch.object(wd_plugin, "_usb_bus_reset") as mock_reset,
        ):
            wd_plugin._attempt_startup_recovery()
            mock_reset.assert_not_called()

    def test_pending_soft_reset_open_timeouts_escalate_to_usb_exactly_once(
        self,
        wd_plugin,
    ):
        plugin = wd_plugin
        plugin._fw_consecutive_open_failures = 3
        plugin._fw_first_open_failure_time = time.monotonic() - 30
        plugin._fw_recovery_pending = True
        plugin._fw_recovery_epoch = 6
        plugin._fw_recovery_method = "soft_reboot"

        def hard_reset(epoch, method):
            assert epoch == 6
            assert method == "usb_bus_reset_open_timeout_escalation"
            return plugin._begin_pending_recovery(
                "hard_reset_issued",
                method,
                epoch=epoch,
            )

        with patch.object(plugin, "_attempt_usb_recovery", side_effect=hard_reset) as hard:
            plugin._handle_startup_firmware_hang()
            plugin._fw_consecutive_open_failures = 3
            plugin._handle_startup_firmware_hang()

        hard.assert_called_once_with(6, "usb_bus_reset_open_timeout_escalation")
        assert plugin._fw_recovery_hard_escalated is True
        assert plugin._fw_recovery_pending is False
        assert plugin._fw_recovery_state == "degraded"

    def test_failed_open_timeout_escalation_ends_pending_epoch(self, wd_plugin):
        plugin = wd_plugin
        plugin._fw_consecutive_open_failures = 3
        plugin._fw_recovery_pending = True
        plugin._fw_recovery_epoch = 7
        plugin._fw_recovery_method = "soft_reboot_uncertain"

        with patch.object(plugin, "_attempt_usb_recovery", return_value=False) as hard:
            plugin._handle_startup_firmware_hang()

        hard.assert_called_once_with(7, "usb_bus_reset_open_timeout_escalation")
        assert plugin._fw_recovery_hard_escalated is True
        assert plugin._fw_recovery_pending is False
        assert plugin._fw_recovery_state == "degraded"

    def test_status_includes_open_failure_fields(self, wd_plugin):
        wd_plugin._fw_consecutive_open_failures = 2
        wd_plugin._fw_first_open_failure_time = time.monotonic() - 45
        wd_plugin._active = True
        wd_plugin._connected = False
        wd_plugin._mqtt_suspended = False
        wd_plugin._msgs_mesh_to_lxmf = 0
        wd_plugin._msgs_lxmf_to_mesh = 0
        wd_plugin._msgs_hub_to_mesh = 0
        wd_plugin._msgs_rate_limited = 0
        wd_plugin._connect_count = 0
        wd_plugin._reconnect_failures = 0
        wd_plugin._last_mesh_msg_time = None
        wd_plugin._last_lxmf_msg_time = None
        wd_plugin._recipient_hashes = set()
        wd_plugin._send_min_interval = 0
        wd_plugin._mqtt_node_num = 0
        wd_plugin._mqtt_long_name = ""
        wd_plugin._mqtt_short_name = ""
        wd_plugin._last_disconnect_time = 0.0
        wd_plugin._mode = "mqtt"
        wd_plugin._last_fw_probe_time = 0.0
        wd_plugin._fw_probe_interval = 0

        status = wd_plugin.get_status()
        fw = status["firmware_watchdog"]
        assert fw["consecutive_open_failures"] == 2
        assert fw["open_failure_threshold"] == 3
        assert fw["open_failure_duration_seconds"] is not None
        assert fw["open_failure_duration_seconds"] >= 44

    def test_validate_config_rejects_zero_threshold(self, mock_app, mqtt_gw_config):
        mqtt_gw_config["firmware_watchdog"] = {"open_failure_threshold": 0}
        with pytest.raises(ValueError, match="open_failure_threshold"):
            _make_plugin_no_start(mock_app, mqtt_gw_config)


# ---------------------------------------------------------------------------
# TestVerifiedFirmwareRecovery — active liveness and recovery semantics
# ---------------------------------------------------------------------------


class TestUsbIdentityRebind:
    """Operator identity changes must be explicit and preserve reset history."""

    @staticmethod
    def _install_identity_state(plugin, limiter, *, bound, expected, current):
        lease = MagicMock()
        lease.revalidate.return_value = MagicMock()
        plugin._serial_device_lease = lease
        plugin._fw_reset_limiter = limiter
        plugin._fw_bound_usb_identity = bound
        plugin._fw_expected_usb_identity = expected
        return patch.object(plugin, "_usb_identity_mapping", return_value=current)

    def test_complete_expected_identity_authorizes_transactional_rebind(
        self,
        gateway_plugin,
        tmp_path,
    ):
        from reticulumpi.radio_recovery import PersistentResetLimiter

        state_path = tmp_path / "identity-rebind.json"
        limiter = PersistentResetLimiter(str(state_path), 3)
        old = {"vendor_id": "239a", "product_id": "0029", "serial": "old-radio"}
        current = {"vendor_id": "239a", "product_id": "0029", "serial": "new-radio"}
        assert limiter.set_metadata("device_identity", old)
        assert limiter.reserve("soft_reboot").allowed

        context = self._install_identity_state(
            gateway_plugin,
            limiter,
            bound=old,
            expected=current,
            current=current,
        )
        with context:
            assert gateway_plugin._bind_or_validate_usb_identity(already_claimed=True) is True

        restarted = PersistentResetLimiter(str(state_path), 3)
        assert restarted.metadata()["device_identity"] == current
        assert restarted.total_attempts == 1
        assert restarted.recent_attempts() == 1
        assert gateway_plugin._fw_bound_usb_identity == current

    def test_usb_serial_identity_remains_case_sensitive(self, gateway_plugin, tmp_path):
        from reticulumpi.radio_recovery import PersistentResetLimiter

        limiter = PersistentResetLimiter(str(tmp_path / "case-sensitive-rebind.json"), 3)
        old = {"vendor_id": "239a", "product_id": "0029", "serial": "old-radio"}
        current = {"vendor_id": "239a", "product_id": "0029", "serial": "Radio-ABC"}
        assert limiter.set_metadata("device_identity", old)
        context = self._install_identity_state(
            gateway_plugin,
            limiter,
            bound=old,
            expected={**current, "serial": "radio-abc"},
            current=current,
        )

        with context:
            assert gateway_plugin._bind_or_validate_usb_identity(already_claimed=True) is False

        assert limiter.metadata()["device_identity"] == old
        assert gateway_plugin._fw_bound_usb_identity == old

    def test_usb_identity_mapping_only_casefolds_hexadecimal_ids(self, gateway_plugin):
        lease = MagicMock()
        lease.identity.usb.vendor_id = "239A"
        lease.identity.usb.product_id = "ABCD"
        lease.identity.usb.serial_number = "Radio-ABC"
        lease.identity.usb.sysfs_path = "/sys/devices/USB-PORT"

        assert gateway_plugin._usb_identity_mapping(lease) == {
            "vendor_id": "239a",
            "product_id": "abcd",
            "serial": "Radio-ABC",
        }

    def test_partial_expected_identity_cannot_authorize_rebind(
        self,
        gateway_plugin,
        tmp_path,
    ):
        from reticulumpi.radio_recovery import PersistentResetLimiter

        limiter = PersistentResetLimiter(str(tmp_path / "partial-rebind.json"), 3)
        old = {"vendor_id": "239a", "product_id": "0029", "serial": "old-radio"}
        current = {"vendor_id": "239a", "product_id": "0029", "serial": "new-radio"}
        assert limiter.set_metadata("device_identity", old)
        context = self._install_identity_state(
            gateway_plugin,
            limiter,
            bound=old,
            expected={"vendor_id": "239a", "product_id": "0029"},
            current=current,
        )

        with context:
            assert gateway_plugin._bind_or_validate_usb_identity(already_claimed=True) is False

        assert limiter.metadata()["device_identity"] == old
        assert gateway_plugin._fw_bound_usb_identity == old

    def test_failed_rebind_persistence_keeps_old_in_memory_binding(
        self,
        gateway_plugin,
        tmp_path,
    ):
        from reticulumpi.radio_recovery import PersistentResetLimiter

        limiter = PersistentResetLimiter(str(tmp_path / "failed-rebind.json"), 3)
        old = {"vendor_id": "239a", "product_id": "0029", "serial": "old-radio"}
        current = {"vendor_id": "239a", "product_id": "0029", "serial": "new-radio"}
        assert limiter.set_metadata("device_identity", old)
        context = self._install_identity_state(
            gateway_plugin,
            limiter,
            bound=old,
            expected=current,
            current=current,
        )

        with context, patch.object(limiter, "set_metadata", return_value=False):
            assert gateway_plugin._bind_or_validate_usb_identity(already_claimed=True) is False

        assert gateway_plugin._fw_bound_usb_identity == old
        assert limiter.metadata()["device_identity"] == old

    def test_no_serial_identity_rebind_requires_explicit_sysfs_path(
        self,
        gateway_plugin,
        tmp_path,
    ):
        from reticulumpi.radio_recovery import PersistentResetLimiter

        limiter = PersistentResetLimiter(str(tmp_path / "sysfs-rebind.json"), 3)
        old = {
            "vendor_id": "239a",
            "product_id": "0029",
            "sysfs_path": "/sys/devices/usb1/1-1",
        }
        current = {
            "vendor_id": "239a",
            "product_id": "0029",
            "sysfs_path": "/sys/devices/usb1/1-2",
        }
        assert limiter.set_metadata("device_identity", old)
        context = self._install_identity_state(
            gateway_plugin,
            limiter,
            bound=old,
            expected=current,
            current=current,
        )

        with context:
            assert gateway_plugin._bind_or_validate_usb_identity(already_claimed=True) is True

        assert limiter.metadata()["device_identity"] == current


class TestVerifiedFirmwareRecovery:
    """Recovery must be driven by physical I/O and verified after reopen."""

    @staticmethod
    def _timeout_result():
        from reticulumpi.meshtastic_health import (
            MeshtasticHealthOutcome,
            MeshtasticHealthResult,
        )

        return MeshtasticHealthResult(
            MeshtasticHealthOutcome.TIMEOUT,
            "metadata_response_timeout",
        )

    def test_mqtt_activity_does_not_refresh_physical_clock(self, mqtt_gateway_plugin):
        plugin = mqtt_gateway_plugin
        _quiesce_plugin_workers(plugin)
        mqtt_iface = MagicMock()
        serial_iface = MagicMock()
        with plugin._lock:
            plugin._mesh_interface = mqtt_iface
            plugin._serial_listener = serial_iface
            plugin._last_serial_activity = 123.0
            plugin._last_mqtt_activity = 0.0

        plugin._on_mesh_text({}, interface=mqtt_iface)

        assert plugin._last_serial_activity == 123.0
        assert plugin._last_mqtt_activity > 0.0

    def test_busy_mqtt_cannot_mask_silent_radio(self, mqtt_gateway_plugin):
        plugin = mqtt_gateway_plugin
        _quiesce_plugin_workers(plugin)
        with plugin._lock:
            plugin._mesh_interface = MagicMock(name="mqtt")
            plugin._serial_listener = MagicMock(name="serial")
            plugin._serial_active_generation = 7
            plugin._last_serial_activity = time.monotonic() - 60
            plugin._last_mqtt_activity = time.monotonic()
            plugin._fw_silence_timeout = 30
            plugin._fw_probe_interval = 0
            plugin._fw_watchdog_enabled = True
            plugin._fw_recovery_pending = False

        with (
            patch.object(plugin, "_check_usb_present", return_value=True),
            patch.object(
                plugin,
                "_probe_device_health",
                return_value=self._timeout_result(),
            ),
            patch.object(plugin, "_handle_firmware_hang") as handle_hang,
        ):
            assert plugin._check_firmware_watchdog() is False

        handle_hang.assert_called_once_with("probe_timeout")

    def test_positive_probe_interval_is_proactive_despite_recent_rx(
        self,
        gateway_plugin,
    ):
        from reticulumpi.meshtastic_health import (
            MeshtasticHealthOutcome,
            MeshtasticHealthResult,
        )

        plugin = gateway_plugin
        now = time.monotonic()
        with plugin._lock:
            plugin._last_serial_activity = now
            plugin._last_fw_probe_time = now - 61
            plugin._fw_silence_timeout = 300
            plugin._fw_probe_interval = 60
            plugin._fw_watchdog_enabled = True
            plugin._fw_recovery_pending = False
        verified = MeshtasticHealthResult(
            MeshtasticHealthOutcome.VERIFIED,
            "metadata_response_verified",
        )

        with (
            patch.object(plugin, "_check_usb_present", return_value=True),
            patch.object(plugin, "_probe_device_health", return_value=verified) as probe,
        ):
            assert plugin._check_firmware_watchdog() is True

        probe.assert_called_once_with()
        assert plugin._fw_verified_serial_generation == plugin._serial_active_generation

    def test_positive_probe_interval_is_rate_limited_after_actual_attempt(
        self,
        gateway_plugin,
    ):
        plugin = gateway_plugin
        now = time.monotonic()
        with plugin._lock:
            plugin._last_serial_activity = now
            plugin._last_fw_probe_time = now - 10
            plugin._fw_silence_timeout = 300
            plugin._fw_probe_interval = 60
            plugin._fw_watchdog_enabled = True
            plugin._fw_recovery_pending = False

        with (
            patch.object(plugin, "_check_usb_present", return_value=True),
            patch.object(plugin, "_probe_device_health") as probe,
        ):
            assert plugin._check_firmware_watchdog() is True

        probe.assert_not_called()

    def test_mqtt_listener_loop_uses_shorter_proactive_probe_cadence(
        self,
        mqtt_gateway_plugin,
    ):
        plugin = mqtt_gateway_plugin
        plugin._fw_watchdog_enabled = True
        plugin._device_probe_interval = 300
        plugin._fw_probe_interval = 45

        assert plugin._device_probe_monitor_interval() == 45

        plugin._fw_probe_interval = 0
        plugin._fw_silence_timeout = 120
        assert plugin._device_probe_monitor_interval() == 120

        plugin._fw_watchdog_enabled = False
        assert plugin._device_probe_monitor_interval() == 300

    def test_serial_monitor_cadence_honors_watchdog_deadlines(self, gateway_plugin):
        plugin = gateway_plugin
        plugin._fw_watchdog_enabled = True
        plugin._fw_silence_timeout = 30
        plugin._fw_probe_interval = 0

        assert plugin._firmware_watchdog_monitor_interval(300) == 30

        plugin._fw_probe_interval = 12
        assert plugin._firmware_watchdog_monitor_interval(300) == 12

    def test_active_probe_never_uses_cached_node_info(self, gateway_plugin):
        from reticulumpi.meshtastic_health import (
            MeshtasticDeviceMetadata,
            MeshtasticHealthOutcome,
            MeshtasticHealthResult,
        )

        plugin = gateway_plugin
        _quiesce_plugin_workers(plugin)
        iface = MagicMock()
        verified = MeshtasticHealthResult(
            MeshtasticHealthOutcome.VERIFIED,
            "metadata_response_verified",
            metadata=MeshtasticDeviceMetadata("2.5.15", 11, 9),
        )
        plugin._meshtastic_health.probe = MagicMock(return_value=verified)
        with plugin._lock:
            plugin._mesh_interface = iface
            plugin._serial_listener = None
            plugin._serial_active_generation = 12
            plugin._serial_open_generation = 12

        assert plugin._probe_device_responsive(iface, generation=12) is True

        iface.getMyNodeInfo.assert_not_called()
        plugin._meshtastic_health.probe.assert_called_once()
        assert plugin._fw_device_firmware_version == "2.5.15"
        assert plugin._fw_device_hardware_model == 9

    def test_concurrent_unpublished_probe_cannot_replace_candidate(self, gateway_plugin):
        plugin = gateway_plugin
        first = MagicMock()
        second = MagicMock()
        with plugin._lock:
            plugin._mesh_interface = None
            plugin._serial_listener = None
            plugin._serial_open_generation = 70
            plugin._serial_active_generation = 70
            plugin._serial_probe_candidate = (first, 70)
        plugin._meshtastic_health.probe = MagicMock()

        result = plugin._probe_device_health(second, generation=70)

        assert result.outcome.value == "inconclusive"
        assert result.detail == "serial_probe_candidate_busy"
        assert plugin._serial_probe_candidate == (first, 70)
        plugin._meshtastic_health.probe.assert_not_called()

    def test_correlated_nak_proves_liveness_without_reset(self, gateway_plugin):
        from reticulumpi.meshtastic_health import (
            MeshtasticHealthOutcome,
            MeshtasticHealthResult,
        )

        plugin = gateway_plugin
        _quiesce_plugin_workers(plugin)
        with plugin._lock:
            plugin._last_serial_activity = time.monotonic() - 60
            plugin._fw_silence_timeout = 30
            plugin._fw_probe_interval = 0
            plugin._fw_watchdog_enabled = True
            plugin._fw_recovery_pending = False
        alive_nak = MeshtasticHealthResult(
            MeshtasticHealthOutcome.ALIVE_PROTOCOL_ERROR,
            "metadata_response_nak",
            protocol_error="NOT_AUTHORIZED",
        )

        with (
            patch.object(plugin, "_check_usb_present", return_value=True),
            patch.object(plugin, "_probe_device_health", return_value=alive_nak),
            patch.object(plugin, "_handle_firmware_hang") as handle_hang,
        ):
            assert plugin._check_firmware_watchdog() is True

        handle_hang.assert_not_called()
        assert plugin._fw_recovery_state == "degraded"
        assert plugin._last_serial_activity > 0

    def test_dependency_mismatch_is_visible_and_never_resets(self, gateway_plugin):
        from reticulumpi.meshtastic_health import (
            MeshtasticHealthOutcome,
            MeshtasticHealthResult,
        )

        plugin = gateway_plugin
        with plugin._lock:
            plugin._last_serial_activity = time.monotonic() - 60
            plugin._fw_silence_timeout = 30
            plugin._fw_probe_interval = 0
            plugin._fw_watchdog_enabled = True
            plugin._fw_recovery_pending = False
            plugin._fw_dependency_error = "meshtastic_version_unsupported:2.7.11"
        unsupported = MeshtasticHealthResult(
            MeshtasticHealthOutcome.UNSUPPORTED,
            "meshtastic_version_unsupported:2.7.11",
        )

        with (
            patch.object(plugin, "_check_usb_present", return_value=True),
            patch.object(plugin, "_probe_device_health", return_value=unsupported),
            patch.object(plugin, "_handle_firmware_hang") as handle_hang,
        ):
            assert plugin._check_firmware_watchdog() is True

        handle_hang.assert_not_called()
        watchdog = plugin.get_status()["firmware_watchdog"]
        assert watchdog["recovery_state"] == "degraded"
        assert watchdog["dependency_error"] == "meshtastic_version_unsupported:2.7.11"

        from reticulumpi.plugin_base import PluginHealth

        iface = MagicMock()
        plugin.mark_starting()
        with plugin._lock:
            plugin._active = True
            generation = plugin._serial_active_generation
            plugin._serial_open_generation = generation
            plugin._serial_probe_candidate = (iface, generation)
        with (
            patch.object(plugin, "_create_serial_interface", return_value=iface),
            patch.object(plugin, "_bind_or_validate_usb_identity", return_value=True),
        ):
            plugin._connect_mesh_device()
        assert plugin.plugin_health is PluginHealth.DEGRADED

    def test_soft_reboot_is_not_a_recovery_event(self, gateway_plugin):
        from reticulumpi import events

        plugin = gateway_plugin
        _quiesce_plugin_workers(plugin)
        iface = MagicMock()
        with plugin._lock:
            plugin._mesh_interface = iface
            plugin._serial_listener = None
            plugin._fw_recovery_pending = False
        plugin.event_bus.publish.reset_mock()

        with (
            patch.object(plugin, "_bind_or_validate_usb_identity", return_value=True),
            patch.object(plugin, "_reserve_reset", return_value=True),
        ):
            assert plugin._attempt_firmware_recovery("probe_timeout") is True

        iface.localNode.reboot.assert_called_once_with(secs=5)
        assert plugin._fw_recovery_pending is True
        assert plugin._fw_recovery_state == "soft_reset_issued"
        assert all(
            call.args[0] != events.MESHTASTIC_FIRMWARE_RECOVERED
            for call in plugin.event_bus.publish.call_args_list
        )

    def test_live_timed_out_health_probe_skips_soft_reboot_for_hard_recovery(
        self,
        gateway_plugin,
    ):
        plugin = gateway_plugin
        iface = MagicMock()
        with plugin._lock:
            plugin._mesh_interface = iface
            plugin._serial_listener = None
            plugin._serial_active_generation = 19
            plugin._serial_open_generation = 19
            plugin._fw_recovery_pending = False
        plugin._meshtastic_health.has_inflight = MagicMock(return_value=True)

        def hard_reset(epoch, method):
            assert method == "usb_bus_reset"
            return plugin._begin_pending_recovery(
                "hard_reset_issued",
                method,
                epoch=epoch,
            )

        with patch.object(plugin, "_attempt_usb_recovery", side_effect=hard_reset) as hard:
            assert plugin._attempt_firmware_recovery("probe_timeout") is True

        plugin._meshtastic_health.has_inflight.assert_called_once_with()
        iface.localNode.reboot.assert_not_called()
        hard.assert_called_once()
        assert plugin._fw_recovery_method == "usb_bus_reset"

    def test_live_timed_out_health_probe_blocks_other_serial_sdk_writes(
        self,
        gateway_plugin,
    ):
        from reticulumpi.builtin_plugins.meshtastic_gateway import _SerialCommandOutcome

        plugin = gateway_plugin
        iface = MagicMock()
        callback = MagicMock(return_value=True)
        with plugin._lock:
            plugin._mesh_interface = iface
            plugin._serial_listener = None
            plugin._serial_active_generation = 27
            plugin._serial_open_generation = 27
        plugin._meshtastic_health.has_inflight = MagicMock(return_value=True)

        blocked = plugin._run_serial_command(iface, 27, "dashboard-text-send", callback)

        assert blocked.outcome is _SerialCommandOutcome.BUSY
        callback.assert_not_called()

        plugin._meshtastic_health.has_inflight.return_value = False
        completed = plugin._run_serial_command(iface, 27, "dashboard-text-send", callback)
        assert completed.succeeded is True
        callback.assert_called_once_with()

    def test_live_timed_out_health_probe_degrades_when_hard_recovery_unavailable(
        self,
        gateway_plugin,
    ):
        plugin = gateway_plugin
        iface = MagicMock()
        with plugin._lock:
            plugin._mesh_interface = iface
            plugin._serial_listener = None
            plugin._fw_recovery_pending = False
            plugin._fw_recovery_attempting = False
            plugin._fw_auto_reset = True
            plugin._fw_max_resets_per_hour = 0
        plugin._meshtastic_health.has_inflight = MagicMock(return_value=True)

        with patch.object(plugin, "_attempt_usb_recovery", return_value=False):
            plugin._handle_firmware_hang("probe_timeout")

        iface.localNode.reboot.assert_not_called()
        assert plugin._fw_recovery_state == "degraded"
        assert plugin._fw_recovery_pending is False

    def test_reopen_without_active_verification_is_rejected(self, gateway_plugin):
        from reticulumpi import events

        plugin = gateway_plugin
        _quiesce_plugin_workers(plugin)
        recovered_iface = MagicMock()
        with plugin._lock:
            plugin._mesh_interface = None
            plugin._connected = False
            plugin._fw_recovery_pending = True
            plugin._fw_recovery_method = "usb_bus_reset"
            plugin._fw_recovery_epoch = 9
            plugin._serial_active_generation = 21
            plugin._serial_open_generation = 21
            plugin._serial_probe_candidate = (recovered_iface, 21)
        plugin.event_bus.publish.reset_mock()

        timeout = self._timeout_result()

        with (
            patch.object(plugin, "_create_serial_interface", return_value=recovered_iface),
            patch.object(plugin, "_bind_or_validate_usb_identity", return_value=True),
            patch.object(plugin, "_probe_device_health", return_value=timeout),
        ):
            with pytest.raises(RuntimeError, match="failed active verification"):
                plugin._connect_mesh_device()

        recovered_iface.close.assert_called_once()
        assert plugin._mesh_interface is None
        assert plugin._fw_recovery_pending is False
        assert plugin._fw_recovery_state == "degraded"
        assert all(
            call.args[0] != events.MESHTASTIC_FIRMWARE_RECOVERED
            for call in plugin.event_bus.publish.call_args_list
        )

    def test_verified_reopen_emits_recovery_once(self, gateway_plugin):
        from reticulumpi import events

        plugin = gateway_plugin
        _quiesce_plugin_workers(plugin)
        with plugin._lock:
            iface = MagicMock()
            plugin._mesh_interface = iface
            plugin._connected = True
            plugin._serial_open_generation = 13
            plugin._serial_active_generation = 13
            plugin._fw_recovery_pending = True
            plugin._fw_recovery_epoch = 4
            plugin._fw_recovery_method = "usb_bus_reset"
            plugin._fw_recovery_started_at = time.time() - 2
            plugin._fw_hang_detected = True
            plugin._fw_hang_reason = "probe_timeout"
        plugin.event_bus.publish.reset_mock()

        assert plugin._complete_firmware_recovery(4, iface, 13) is True

        plugin.event_bus.publish.assert_called_once()
        event_name, payload = plugin.event_bus.publish.call_args.args
        assert event_name == events.MESHTASTIC_FIRMWARE_RECOVERED
        assert payload["verified"] is True
        assert payload["method"] == "usb_bus_reset"
        assert plugin._fw_recovery_pending is False
        assert plugin._fw_hang_detected is False
        assert plugin._fw_recovery_state == "recovered"

    def test_failed_soft_verification_escalates_to_usb_only_once(self, gateway_plugin):
        plugin = gateway_plugin
        iface = MagicMock()
        timeout = self._timeout_result()
        with plugin._lock:
            plugin._mesh_interface = iface
            plugin._serial_open_generation = 31
            plugin._serial_active_generation = 31
            plugin._fw_recovery_pending = True
            plugin._fw_recovery_epoch = 8
            plugin._fw_recovery_method = "soft_reboot"
            plugin._fw_usb_power_cycle = True

        def hard_reset(epoch, method):
            assert epoch == 8
            assert method == "usb_bus_reset_escalation"
            plugin._begin_pending_recovery(
                "hard_reset_issued",
                method,
                epoch=epoch,
            )
            return True

        with patch.object(plugin, "_attempt_usb_recovery", side_effect=hard_reset) as hard:
            plugin._handle_recovery_verification_failure(iface, 31, 8, timeout)

        hard.assert_called_once()
        assert plugin._fw_recovery_pending is True
        assert plugin._fw_recovery_method == "usb_bus_reset_escalation"
        assert plugin._fw_recovery_hard_escalated is True

    def test_stale_epoch_cannot_complete_current_recovery(self, gateway_plugin):
        from reticulumpi import events

        plugin = gateway_plugin
        iface = MagicMock()
        with plugin._lock:
            plugin._mesh_interface = iface
            plugin._connected = True
            plugin._serial_open_generation = 44
            plugin._serial_active_generation = 44
            plugin._fw_recovery_pending = True
            plugin._fw_recovery_epoch = 12
            plugin._fw_recovery_method = "usb_bus_reset"
        plugin.event_bus.publish.reset_mock()

        assert plugin._complete_firmware_recovery(11, iface, 44) is False

        assert plugin._fw_recovery_pending is True
        assert all(
            call.args[0] != events.MESHTASTIC_FIRMWARE_RECOVERED
            for call in plugin.event_bus.publish.call_args_list
        )

    def test_parallel_recovery_requests_issue_one_reboot(self, gateway_plugin):
        plugin = gateway_plugin
        iface = MagicMock()
        entered = threading.Event()
        release = threading.Event()

        def blocking_reboot(*, secs):
            assert secs == 5
            entered.set()
            assert release.wait(timeout=1)

        iface.localNode.reboot.side_effect = blocking_reboot
        with plugin._lock:
            plugin._mesh_interface = iface
            plugin._serial_listener = None
            plugin._serial_open_generation = 51
            plugin._serial_active_generation = 51
            plugin._fw_recovery_pending = False
            plugin._fw_recovery_attempting = False
        plugin._serial_command_timeout = 1.0
        results = []

        with (
            patch.object(plugin, "_bind_or_validate_usb_identity", return_value=True),
            patch.object(plugin, "_reserve_reset", return_value=True),
        ):
            first = threading.Thread(
                target=lambda: results.append(plugin._attempt_firmware_recovery("first"))
            )
            first.start()
            assert entered.wait(timeout=1)
            results.append(plugin._attempt_firmware_recovery("second"))
            release.set()
            first.join(timeout=1)

        assert not first.is_alive()
        assert sorted(results) == [False, True]
        iface.localNode.reboot.assert_called_once_with(secs=5)
        assert plugin._fw_recovery_pending is True

    def test_queue_or_gate_probe_failure_is_inconclusive_and_never_resets(
        self,
        gateway_plugin,
    ):
        from reticulumpi.meshtastic_health import (
            MeshtasticHealthOutcome,
            MeshtasticHealthResult,
        )

        plugin = gateway_plugin
        with plugin._lock:
            plugin._last_serial_activity = time.monotonic() - 60
            plugin._fw_silence_timeout = 30
            plugin._fw_probe_interval = 0
            plugin._fw_watchdog_enabled = True
            plugin._fw_recovery_pending = False
        queue_failure = MeshtasticHealthResult(
            MeshtasticHealthOutcome.INCONCLUSIVE,
            "tx_queue_has_no_free_space",
        )

        with (
            patch.object(plugin, "_check_usb_present", return_value=True),
            patch.object(plugin, "_probe_device_health", return_value=queue_failure),
            patch.object(plugin, "_handle_firmware_hang") as handle_hang,
        ):
            assert plugin._check_firmware_watchdog() is True

        handle_hang.assert_not_called()
        assert plugin._fw_recovery_state == "suspect"

    def test_actual_serial_command_gate_busy_is_inconclusive(self, gateway_plugin):
        plugin = gateway_plugin
        iface = MagicMock()
        with plugin._lock:
            plugin._mesh_interface = iface
            plugin._serial_listener = None
            plugin._serial_active_generation = 73
            plugin._serial_open_generation = 73
        plugin._meshtastic_health.probe = MagicMock()
        assert plugin._serial_operation_lock.acquire(blocking=False)
        try:
            result = plugin._probe_device_health(iface, generation=73)
        finally:
            plugin._serial_operation_lock.release()

        assert result.outcome.value == "inconclusive"
        assert result.detail == "serial_command_gate_busy"
        plugin._meshtastic_health.probe.assert_not_called()

    @pytest.mark.parametrize(
        ("outcome", "detail"),
        [
            ("alive_protocol_error", "metadata_request_nak"),
            ("unsupported", "metadata_response_field_missing"),
        ],
    )
    def test_alive_or_incompatible_verification_never_escalates_to_hard_reset(
        self,
        gateway_plugin,
        outcome,
        detail,
    ):
        from reticulumpi.meshtastic_health import (
            MeshtasticHealthOutcome,
            MeshtasticHealthResult,
        )

        plugin = gateway_plugin
        iface = MagicMock()
        with plugin._lock:
            plugin._mesh_interface = None
            plugin._serial_listener = None
            plugin._serial_active_generation = 82
            plugin._serial_open_generation = 82
            plugin._serial_probe_candidate = (iface, 82)
            plugin._fw_recovery_pending = True
            plugin._fw_recovery_epoch = 17
            plugin._fw_recovery_method = "soft_reboot"
            plugin._fw_usb_power_cycle = True
        result = MeshtasticHealthResult(MeshtasticHealthOutcome(outcome), detail)

        with patch.object(plugin, "_attempt_usb_recovery") as hard_reset:
            plugin._handle_recovery_verification_failure(iface, 82, 17, result)

        hard_reset.assert_not_called()
        assert plugin._fw_recovery_pending is False
        assert plugin._fw_recovery_state == "degraded"
        assert plugin._fw_verification_failure_sticky is True

    def test_shutdown_does_not_issue_second_reboot_during_pending_recovery(
        self,
        gateway_plugin,
    ):
        plugin = gateway_plugin
        iface = MagicMock()
        plugin._reboot_device_on_stop = True
        with plugin._lock:
            plugin._mesh_interface = iface
            plugin._serial_listener = None
            plugin._serial_active_generation = 91
            plugin._serial_open_generation = 91
            plugin._fw_recovery_pending = True

        with patch.object(plugin, "_reserve_reset") as reserve:
            plugin._graceful_device_shutdown()

        reserve.assert_not_called()
        iface.localNode.reboot.assert_not_called()

    def test_serial_connection_loop_honors_recovery_delay_before_open(
        self,
        gateway_plugin,
    ):
        plugin = gateway_plugin
        with plugin._lock:
            plugin._connected = False
            plugin._mesh_interface = None
        plugin._active = True

        with (
            patch.object(plugin, "_close_mesh_interface"),
            patch.object(
                plugin,
                "_wait_for_recovery_reopen_delay",
                return_value=False,
            ) as wait_delay,
            patch.object(plugin, "_connect_mesh_device") as connect,
        ):
            plugin._connection_loop()

        wait_delay.assert_called_once_with()
        connect.assert_not_called()

    def test_physical_success_cannot_clear_unrelated_mqtt_degradation(
        self,
        mqtt_gateway_plugin,
    ):
        from reticulumpi.plugin_base import PluginHealth, PluginState

        plugin = mqtt_gateway_plugin
        with plugin._lock:
            plugin._connected = False
            plugin._fw_recovery_state = "healthy"
            plugin._fw_dependency_error = None
        plugin.mark_starting()
        plugin.mark_degraded("Meshtastic MQTT connection lost")
        plugin.mark_ready()

        assert plugin.plugin_state is PluginState.READY
        with patch.object(plugin, "mark_ready", wraps=plugin.mark_ready) as mark_ready:
            plugin._mark_ready_with_radio_guard()

        mark_ready.assert_not_called()
        assert plugin.plugin_health is PluginHealth.DEGRADED

    def test_mqtt_probe_port_blocks_initial_ready_until_listener_generation_verified(
        self,
        mqtt_gateway_plugin,
    ):
        from reticulumpi.plugin_base import PluginState

        plugin = mqtt_gateway_plugin
        plugin.mark_starting()
        with plugin._lock:
            plugin._active = True
            plugin._connected = True
            plugin._mesh_interface = MagicMock(name="mqtt")
            plugin._device_probe_port = "/dev/meshtastic"
            plugin._fw_watchdog_enabled = True
            plugin._fw_dependency_error = None
            plugin._fw_recovery_pending = False
            plugin._fw_recovery_attempting = False
            plugin._fw_recovery_state = "healthy"
            plugin._serial_active_generation = 101
            plugin._serial_open_generation = 101
            plugin._serial_listener = None
            plugin._fw_verified_serial_generation = None

        plugin._mark_ready_with_radio_guard()
        assert plugin.plugin_state is PluginState.STARTING

        with plugin._lock:
            plugin._serial_listener = MagicMock(name="physical")
        plugin._mark_ready_with_radio_guard()
        assert plugin.plugin_state is PluginState.STARTING

        with plugin._lock:
            plugin._fw_verified_serial_generation = 101
        plugin._mark_ready_with_radio_guard()
        assert plugin.plugin_state is PluginState.READY

    def test_initial_mqtt_physical_probe_failure_never_publishes_listener(
        self,
        mqtt_gateway_plugin,
    ):
        plugin = mqtt_gateway_plugin
        iface = MagicMock()
        from reticulumpi.builtin_plugins.meshtastic_gateway import (
            SerialOpenOutcome,
            SerialOpenResult,
        )

        plugin._device_probe_port = "/dev/meshtastic"
        plugin._device_probe_startup_delay = 5
        plugin._serial_retry_interval = 5
        plugin._fw_watchdog_enabled = True
        plugin._active = True
        with plugin._lock:
            plugin._serial_active_generation = 111
            plugin._serial_open_generation = 111
            plugin._serial_probe_candidate = (iface, 111)
            plugin._serial_listener = None

        sleep_calls = 0

        def stop_after_retry(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 1:
                plugin._active = False

        with (
            patch.object(plugin, "_sleep_while_active", side_effect=stop_after_retry),
            patch.object(plugin, "_wait_for_recovery_reopen_delay", return_value=True),
            patch.object(
                plugin,
                "_open_serial_interface_result",
                return_value=SerialOpenResult(
                    SerialOpenOutcome.OPENED,
                    interface=iface,
                    generation=111,
                ),
            ),
            patch.object(plugin, "_bind_or_validate_usb_identity", return_value=True),
            patch.object(plugin, "_probe_device_health", return_value=self._timeout_result()),
        ):
            plugin._device_probe_loop()

        assert plugin._serial_listener is None
        assert plugin._fw_verified_serial_generation is None
        assert plugin._fw_recovery_state == "degraded"

    def test_initial_mqtt_physical_listener_publishes_only_after_verified_probe(
        self,
        mqtt_gateway_plugin,
    ):
        from reticulumpi.builtin_plugins.meshtastic_gateway import (
            SerialOpenOutcome,
            SerialOpenResult,
        )
        from reticulumpi.meshtastic_health import (
            MeshtasticHealthOutcome,
            MeshtasticHealthResult,
        )

        plugin = mqtt_gateway_plugin
        iface = MagicMock()
        plugin._device_probe_port = "/dev/meshtastic"
        plugin._fw_watchdog_enabled = True
        plugin._fw_dependency_error = None
        plugin._active = True
        with plugin._lock:
            plugin._serial_active_generation = 112
            plugin._serial_open_generation = 112
            plugin._serial_probe_candidate = (iface, 112)
            plugin._serial_listener = None
            plugin._fw_recovery_pending = False
        verified = MeshtasticHealthResult(
            MeshtasticHealthOutcome.VERIFIED,
            "metadata_response_verified",
        )
        publication_snapshots = []

        def stop_after_observing_publication(_timeout):
            with plugin._lock:
                publication_snapshots.append(
                    (
                        plugin._serial_listener is iface,
                        plugin._fw_verified_serial_generation,
                    )
                )
            plugin._active = False
            return True

        with (
            patch.object(plugin, "_sleep_while_active"),
            patch.object(plugin, "_wait_for_recovery_reopen_delay", return_value=True),
            patch.object(
                plugin,
                "_open_serial_interface_result",
                return_value=SerialOpenResult(
                    SerialOpenOutcome.OPENED,
                    interface=iface,
                    generation=112,
                ),
            ),
            patch.object(plugin, "_bind_or_validate_usb_identity", return_value=True),
            patch.object(plugin, "_probe_device_health", return_value=verified) as probe,
            patch.object(plugin, "_read_device_info_from_interface", return_value={}),
            patch.object(plugin, "_extract_lora_neighbors", return_value=[]),
            patch.object(plugin, "_refresh_channel_cache"),
            patch.object(plugin, "_check_firmware_watchdog", return_value=True),
            patch.object(
                plugin,
                "_wait_for_serial_wake",
                side_effect=stop_after_observing_publication,
            ),
        ):
            plugin._device_probe_loop()

        probe.assert_called_once_with(iface, generation=112)
        assert publication_snapshots == [(True, 112)]
        assert plugin._serial_listener is None

    def test_manual_reset_reports_accepted_not_verified(self, gateway_plugin):
        plugin = gateway_plugin
        _quiesce_plugin_workers(plugin)
        with plugin._lock:
            plugin._fw_recovery_pending = False

        def issue_reset(_reason):
            with plugin._lock:
                plugin._fw_recovery_pending = True
                plugin._fw_recovery_state = "soft_reset_issued"
                plugin._fw_recovery_method = "soft_reboot"
            return True

        with (
            patch.object(plugin, "_attempt_firmware_recovery", side_effect=issue_reset),
            patch.object(plugin, "_cleanup_after_reset") as cleanup,
        ):
            result = plugin.reset_device()

        assert result == {
            "ok": True,
            "accepted": True,
            "verified": False,
            "state": "soft_reset_issued",
            "method": "soft_reboot",
        }
        cleanup.assert_called_once()

    def test_status_exposes_probe_and_radio_firmware_metadata(self, gateway_plugin):
        plugin = gateway_plugin
        _quiesce_plugin_workers(plugin)
        with plugin._lock:
            plugin._fw_watchdog_enabled = True
            plugin._fw_last_probe_outcome = "verified"
            plugin._fw_last_probe_detail = "metadata_response_verified"
            plugin._fw_device_firmware_version = "2.6.0"
            plugin._fw_device_hardware_model = 9

        watchdog = plugin.get_status()["firmware_watchdog"]

        assert watchdog["last_probe_outcome"] == "verified"
        assert watchdog["last_probe_detail"] == "metadata_response_verified"
        assert watchdog["device_firmware_version"] == "2.6.0"
        assert watchdog["device_hardware_model"] == 9

    def test_serial_mode_rejects_duplicate_probe_port(self, mock_app, gw_config):
        gw_config["device_probe_port"] = "/dev/meshtastic-probe"
        with pytest.raises(ValueError, match="only valid in MQTT mode"):
            _make_plugin_no_start(mock_app, gw_config)

    def test_usb_reset_rejects_auto_selected_device(self, mock_app, gw_config):
        gw_config["serial_port"] = "auto"
        gw_config["firmware_watchdog"] = {"usb_power_cycle": True}
        with pytest.raises(ValueError, match="explicit stable serial device path"):
            _make_plugin_no_start(mock_app, gw_config)


class TestMeshtasticChangedLineCoverage:
    """Focused branch tests for the safety-critical serial recovery paths."""

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("device_probe_open_timeout", 0, "device_probe_open_timeout"),
            ("serial_command_timeout", True, "serial_command_timeout"),
            ("serial_close_timeout", -1, "serial_close_timeout"),
            ("firmware_watchdog.recovery_reopen_delay", True, "recovery_reopen_delay"),
            (
                "firmware_watchdog.expected_usb_identity",
                "not-a-mapping",
                "expected_usb_identity",
            ),
            (
                "firmware_watchdog.expected_usb_identity",
                {"unknown": "value"},
                "Unknown firmware_watchdog",
            ),
            (
                "firmware_watchdog.expected_usb_identity",
                {"serial": ""},
                "must be a non-empty string",
            ),
        ],
    )
    def test_new_recovery_config_fields_fail_closed(
        self,
        mock_app,
        gw_config,
        field,
        value,
        message,
    ):
        if field.startswith("firmware_watchdog."):
            nested = field.split(".", 1)[1]
            gw_config["firmware_watchdog"] = {nested: value}
        else:
            gw_config[field] = value

        with pytest.raises(ValueError, match=message):
            _make_plugin_no_start(mock_app, gw_config)

    def test_physical_port_requires_explicit_configuration(self, gateway_plugin):
        gateway_plugin._device_probe_port = ""
        gateway_plugin.config["serial_port"] = "auto"

        assert gateway_plugin._physical_serial_port() is None

    def test_serial_lease_requires_configured_port(self, gateway_plugin):
        from reticulumpi.builtin_plugins.meshtastic_gateway import SerialOpenOutcome

        gateway_plugin._device_probe_port = ""
        gateway_plugin.config["serial_port"] = "auto"
        gateway_plugin._serial_device_lease = None

        assert gateway_plugin._ensure_serial_device_lease() is False
        assert gateway_plugin._serial_lease_failure is SerialOpenOutcome.IDENTITY

    def test_existing_serial_lease_revalidates_without_reclaim(self, gateway_plugin):
        lease = MagicMock()
        gateway_plugin._serial_device_lease = lease

        with patch(
            "reticulumpi.builtin_plugins.meshtastic_gateway.serial_device_registry.claim"
        ) as claim:
            assert gateway_plugin._ensure_serial_device_lease() is True

        lease.revalidate.assert_called_once_with()
        claim.assert_not_called()

    def test_changed_serial_lease_is_released_and_transactionally_reclaimed(
        self,
        gateway_plugin,
    ):
        from reticulumpi.serial_devices import SerialDeviceChangedError

        stale = MagicMock()
        stale.revalidate.side_effect = SerialDeviceChangedError(
            "/dev/meshtastic",
            MagicMock(),
            MagicMock(),
        )
        replacement = MagicMock()
        gateway_plugin._serial_device_lease = stale
        with gateway_plugin._lock:
            gateway_plugin._mesh_interface = None
            gateway_plugin._serial_listener = None

        with (
            patch.object(gateway_plugin, "_prune_serial_workers", return_value=True),
            patch(
                "reticulumpi.builtin_plugins.meshtastic_gateway.serial_device_registry.claim",
                return_value=replacement,
            ) as claim,
            patch.object(
                gateway_plugin,
                "_bind_or_validate_usb_identity",
                return_value=True,
            ) as bind,
        ):
            assert gateway_plugin._ensure_serial_device_lease("/dev/meshtastic") is True

        stale.release.assert_called_once_with()
        claim.assert_called_once_with("/dev/meshtastic", gateway_plugin.plugin_name)
        bind.assert_called_once_with(already_claimed=True)
        assert gateway_plugin._serial_device_lease is replacement

    @pytest.mark.parametrize("published", [True, False])
    def test_changed_lease_requires_proven_quiescence(self, gateway_plugin, published):
        from reticulumpi.builtin_plugins.meshtastic_gateway import SerialOpenOutcome
        from reticulumpi.serial_devices import SerialDeviceChangedError

        lease = MagicMock()
        lease.revalidate.side_effect = SerialDeviceChangedError(
            "/dev/meshtastic",
            MagicMock(),
            MagicMock(),
        )
        gateway_plugin._serial_device_lease = lease
        with gateway_plugin._lock:
            gateway_plugin._mesh_interface = MagicMock() if published else None
            gateway_plugin._serial_listener = None

        with patch.object(
            gateway_plugin,
            "_prune_serial_workers",
            return_value=published,
        ):
            assert gateway_plugin._ensure_serial_device_lease() is False

        assert gateway_plugin._serial_lease_failure is SerialOpenOutcome.TEARDOWN_UNPROVEN
        lease.release.assert_not_called()

    @pytest.mark.parametrize(
        ("error_factory", "expected"),
        [
            (
                lambda: __import__(
                    "reticulumpi.serial_devices",
                    fromlist=["SerialDeviceIdentityError"],
                ).SerialDeviceIdentityError("wrong device"),
                "identity",
            ),
            (lambda: RuntimeError("revalidate failed"), "error"),
        ],
    )
    def test_existing_lease_revalidation_failures_are_typed(
        self,
        gateway_plugin,
        error_factory,
        expected,
    ):
        lease = MagicMock()
        lease.revalidate.side_effect = error_factory()
        gateway_plugin._serial_device_lease = lease

        assert gateway_plugin._ensure_serial_device_lease() is False
        assert gateway_plugin._serial_lease_failure.value == expected

    def test_new_claim_is_blocked_until_old_workers_are_quiescent(self, gateway_plugin):
        from reticulumpi.builtin_plugins.meshtastic_gateway import SerialOpenOutcome

        gateway_plugin._serial_device_lease = None
        with (
            patch.object(gateway_plugin, "_prune_serial_workers", return_value=False),
            patch(
                "reticulumpi.builtin_plugins.meshtastic_gateway.serial_device_registry.claim"
            ) as claim,
        ):
            assert gateway_plugin._ensure_serial_device_lease() is False

        assert gateway_plugin._serial_lease_failure is SerialOpenOutcome.TEARDOWN_UNPROVEN
        claim.assert_not_called()

    @pytest.mark.parametrize(
        ("error_factory", "expected"),
        [
            (
                lambda: __import__(
                    "reticulumpi.serial_devices",
                    fromlist=["SerialDeviceBusyError"],
                ).SerialDeviceBusyError(
                    "/dev/meshtastic",
                    ("owner",),
                    None,
                    external=False,
                ),
                "busy",
            ),
            (
                lambda: __import__(
                    "reticulumpi.serial_devices",
                    fromlist=["SerialDeviceIdentityError"],
                ).SerialDeviceIdentityError("wrong device"),
                "identity",
            ),
            (lambda: PermissionError("denied"), "permission"),
            (lambda: FileNotFoundError("gone"), "missing"),
            (lambda: OSError(errno.EBUSY, "busy"), "busy"),
            (lambda: RuntimeError("registry failed"), "error"),
        ],
    )
    def test_new_serial_claim_failures_are_typed(
        self,
        gateway_plugin,
        error_factory,
        expected,
    ):
        gateway_plugin._serial_device_lease = None
        with (
            patch.object(gateway_plugin, "_prune_serial_workers", return_value=True),
            patch(
                "reticulumpi.builtin_plugins.meshtastic_gateway.serial_device_registry.claim",
                side_effect=error_factory(),
            ),
        ):
            assert gateway_plugin._ensure_serial_device_lease() is False

        assert gateway_plugin._serial_lease_failure.value == expected

    def test_failed_identity_binding_marks_claim_as_identity_failure(self, gateway_plugin):
        replacement = MagicMock()
        gateway_plugin._serial_device_lease = None
        with (
            patch.object(gateway_plugin, "_prune_serial_workers", return_value=True),
            patch(
                "reticulumpi.builtin_plugins.meshtastic_gateway.serial_device_registry.claim",
                return_value=replacement,
            ),
            patch.object(
                gateway_plugin,
                "_bind_or_validate_usb_identity",
                return_value=False,
            ),
        ):
            assert gateway_plugin._ensure_serial_device_lease() is False

        assert gateway_plugin._serial_lease_failure.value == "identity"

    def test_usb_identity_mapping_supports_serialless_and_non_usb_devices(
        self,
        gateway_plugin,
    ):
        non_usb = MagicMock()
        non_usb.identity.usb = None
        assert gateway_plugin._usb_identity_mapping(non_usb) is None

        serialless = MagicMock()
        serialless.identity.usb.vendor_id = "239A"
        serialless.identity.usb.product_id = "0029"
        serialless.identity.usb.serial_number = ""
        serialless.identity.usb.sysfs_path = "/sys/devices/usb1/1-1"
        assert gateway_plugin._usb_identity_mapping(serialless) == {
            "vendor_id": "239a",
            "product_id": "0029",
            "sysfs_path": "/sys/devices/usb1/1-1",
        }
        assert gateway_plugin._configured_identity_authorizes_rebind({}, None) is False

    def test_identity_binding_requires_a_claim_when_not_preclaimed(self, gateway_plugin):
        with patch.object(gateway_plugin, "_ensure_serial_device_lease", return_value=False):
            assert gateway_plugin._bind_or_validate_usb_identity() is False

    def test_identity_binding_rejects_missing_or_changed_claim(self, gateway_plugin):
        gateway_plugin._serial_device_lease = None
        assert gateway_plugin._bind_or_validate_usb_identity(already_claimed=True) is False

        lease = MagicMock()
        lease.revalidate.side_effect = RuntimeError("changed")
        gateway_plugin._serial_device_lease = lease
        assert gateway_plugin._bind_or_validate_usb_identity(already_claimed=True) is False

    @pytest.mark.parametrize("hard_reset", [False, True])
    def test_non_usb_identity_is_allowed_only_without_hard_reset(
        self,
        gateway_plugin,
        hard_reset,
    ):
        lease = MagicMock()
        lease.revalidate.return_value = object()
        gateway_plugin._serial_device_lease = lease
        gateway_plugin._fw_bound_usb_identity = None
        gateway_plugin._fw_expected_usb_identity = {}
        gateway_plugin._fw_usb_power_cycle = hard_reset

        with patch.object(gateway_plugin, "_usb_identity_mapping", return_value=None):
            assert (
                gateway_plugin._bind_or_validate_usb_identity(already_claimed=True)
                is not hard_reset
            )

    def test_matching_durable_usb_binding_is_accepted(self, gateway_plugin):
        current = {"vendor_id": "239a", "product_id": "0029", "serial": "radio"}
        lease = MagicMock()
        lease.revalidate.return_value = object()
        gateway_plugin._serial_device_lease = lease
        gateway_plugin._fw_bound_usb_identity = current
        gateway_plugin._fw_expected_usb_identity = {}

        with patch.object(gateway_plugin, "_usb_identity_mapping", return_value=current):
            assert gateway_plugin._bind_or_validate_usb_identity(already_claimed=True) is True

    @pytest.mark.parametrize("persisted", [False, True])
    def test_first_usb_binding_is_transactional(self, gateway_plugin, persisted):
        current = {"vendor_id": "239a", "product_id": "0029", "serial": "radio"}
        lease = MagicMock()
        lease.revalidate.return_value = object()
        limiter = MagicMock()
        limiter.set_metadata.return_value = persisted
        gateway_plugin._serial_device_lease = lease
        gateway_plugin._fw_bound_usb_identity = None
        gateway_plugin._fw_expected_usb_identity = {}
        gateway_plugin._fw_reset_limiter = limiter

        with patch.object(gateway_plugin, "_usb_identity_mapping", return_value=current):
            assert gateway_plugin._bind_or_validate_usb_identity(already_claimed=True) is persisted

        if persisted:
            assert gateway_plugin._fw_bound_usb_identity == current
        else:
            assert gateway_plugin._fw_bound_usb_identity is None

    def test_first_usb_binding_without_limiter_fails_closed(self, gateway_plugin):
        lease = MagicMock()
        lease.revalidate.return_value = object()
        gateway_plugin._serial_device_lease = lease
        gateway_plugin._fw_bound_usb_identity = None
        gateway_plugin._fw_expected_usb_identity = {}
        gateway_plugin._fw_reset_limiter = None

        with patch.object(
            gateway_plugin,
            "_usb_identity_mapping",
            return_value={"vendor_id": "1", "product_id": "2", "serial": "3"},
        ):
            assert gateway_plugin._bind_or_validate_usb_identity(already_claimed=True) is False

    def test_usb_bus_path_reads_current_kernel_address(self, gateway_plugin, tmp_path):
        (tmp_path / "busnum").write_text("4\n", encoding="utf-8")
        (tmp_path / "devnum").write_text("16\n", encoding="utf-8")
        lease = MagicMock()
        lease.identity.usb.sysfs_path = str(tmp_path)

        assert gateway_plugin._usb_bus_path_for_lease(lease) == "/dev/bus/usb/004/016"

        lease.identity.usb = None
        assert gateway_plugin._usb_bus_path_for_lease(lease) is None

    def test_usb_path_resolution_handles_ownership_and_identity_failures(self, gateway_plugin):
        with patch.object(gateway_plugin, "_bind_or_validate_usb_identity", return_value=False):
            assert gateway_plugin._resolve_usb_device_path() is None

        gateway_plugin._serial_device_lease = None
        with patch.object(gateway_plugin, "_bind_or_validate_usb_identity", return_value=True):
            assert gateway_plugin._resolve_usb_device_path() is None

        lease = MagicMock()
        lease.identity.usb = object()
        gateway_plugin._serial_device_lease = lease
        with (
            patch.object(gateway_plugin, "_bind_or_validate_usb_identity", return_value=True),
            patch.object(
                gateway_plugin,
                "_usb_bus_path_for_lease",
                return_value="/dev/bus/usb/004/016",
            ),
        ):
            assert gateway_plugin._resolve_usb_device_path() == "/dev/bus/usb/004/016"

        with (
            patch.object(gateway_plugin, "_bind_or_validate_usb_identity", return_value=True),
            patch.object(
                gateway_plugin,
                "_usb_bus_path_for_lease",
                side_effect=OSError("sysfs vanished"),
            ),
        ):
            assert gateway_plugin._resolve_usb_device_path() is None

    def test_usb_bus_reset_revalidates_exact_path_and_closes_fd(self, gateway_plugin):
        gateway_plugin._serial_device_lease = MagicMock()
        with (
            patch.object(gateway_plugin, "_bind_or_validate_usb_identity", return_value=True),
            patch.object(
                gateway_plugin,
                "_usb_bus_path_for_lease",
                return_value="/dev/bus/usb/004/016",
            ),
            patch("os.open", return_value=41) as open_fd,
            patch("fcntl.ioctl") as ioctl,
            patch("os.close") as close_fd,
        ):
            assert gateway_plugin._usb_bus_reset("/dev/bus/usb/004/016") == {
                "ok": True,
                "method": "usb_reset",
            }

        open_fd.assert_called_once_with("/dev/bus/usb/004/016", __import__("os").O_WRONLY)
        ioctl.assert_called_once_with(41, 0x5514, 0)
        close_fd.assert_called_once_with(41)

    @pytest.mark.parametrize(
        ("open_error", "reason_fragment"),
        [
            (PermissionError("denied"), "Permission denied"),
            (OSError("reset failed"), "reset failed"),
        ],
    )
    def test_usb_bus_reset_reports_open_failures(
        self,
        gateway_plugin,
        open_error,
        reason_fragment,
    ):
        gateway_plugin._serial_device_lease = MagicMock()
        with (
            patch.object(gateway_plugin, "_bind_or_validate_usb_identity", return_value=True),
            patch.object(
                gateway_plugin,
                "_usb_bus_path_for_lease",
                return_value="/dev/bus/usb/004/016",
            ),
            patch("os.open", side_effect=open_error),
        ):
            result = gateway_plugin._usb_bus_reset("/dev/bus/usb/004/016")

        assert result["ok"] is False
        assert reason_fragment in result["reason"]

    def test_usb_bus_reset_rejects_failed_identity_and_address_change(self, gateway_plugin):
        with patch.object(gateway_plugin, "_bind_or_validate_usb_identity", return_value=False):
            assert gateway_plugin._usb_bus_reset("/dev/bus/usb/004/016")["ok"] is False

        gateway_plugin._serial_device_lease = MagicMock()
        with (
            patch.object(gateway_plugin, "_bind_or_validate_usb_identity", return_value=True),
            patch.object(
                gateway_plugin,
                "_usb_bus_path_for_lease",
                side_effect=OSError("gone"),
            ),
        ):
            result = gateway_plugin._usb_bus_reset("/dev/bus/usb/004/016")
        assert result == {"ok": False, "reason": "USB bus address changed before reset"}

    def test_manual_reset_rejects_missing_port_pending_and_failed_actuator(
        self,
        gateway_plugin,
    ):
        with patch.object(gateway_plugin, "_physical_serial_port", return_value=None):
            assert gateway_plugin.reset_device()["reason"].startswith("No explicit")

        with gateway_plugin._lock:
            gateway_plugin._fw_recovery_pending = True
            gateway_plugin._fw_recovery_state = "waiting_for_reopen"
        assert gateway_plugin.reset_device()["reason"] == "Recovery already in progress"

        with gateway_plugin._lock:
            gateway_plugin._fw_recovery_pending = False
            gateway_plugin._fw_recovery_attempting = False
        with (
            patch.object(
                gateway_plugin,
                "_physical_serial_port",
                return_value="/dev/meshtastic",
            ),
            patch.object(gateway_plugin, "_attempt_firmware_recovery", return_value=False),
        ):
            assert gateway_plugin.reset_device() == {
                "ok": False,
                "reason": "All configured reset methods failed",
            }

    def test_cleanup_after_reset_closes_every_exact_handle_and_wakes_reconnect(
        self,
        gateway_plugin,
    ):
        listener = MagicMock(name="listener")
        candidate = MagicMock(name="candidate")
        with gateway_plugin._lock:
            gateway_plugin._mode = "serial"
            gateway_plugin._serial_listener = listener
            gateway_plugin._serial_probe_candidate = (candidate, 7)
            gateway_plugin._serial_active_generation = 6
            gateway_plugin._serial_open_generation = 6
            gateway_plugin._fw_verified_serial_generation = 6
            gateway_plugin._cached_device_info = {"old": True}
            gateway_plugin._cached_lora_neighbors = [{"old": True}]
        gateway_plugin._serial_reconnect_requested.clear()

        with (
            patch.object(gateway_plugin, "_bounded_close_serial_interface") as close,
            patch.object(gateway_plugin, "_close_mesh_interface") as close_mesh,
        ):
            gateway_plugin._cleanup_after_reset()

        assert close.call_args_list == [
            __import__("unittest.mock").mock.call(listener, 6, "post-reset close"),
            __import__("unittest.mock").mock.call(
                candidate,
                7,
                "post-reset unpublished-interface close",
            ),
        ]
        close_mesh.assert_called_once_with()
        assert gateway_plugin._fw_verified_serial_generation is None
        assert gateway_plugin._cached_device_info is None
        assert gateway_plugin._cached_lora_neighbors == []
        assert gateway_plugin._serial_reconnect_requested.is_set()

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (OSError(errno.EACCES, "denied"), "permission"),
            (OSError(errno.ENOENT, "gone"), "missing"),
            (RuntimeError("resource busy"), "busy"),
            (RuntimeError("no such device"), "missing"),
            (RuntimeError("unexpected"), "error"),
        ],
    )
    def test_serial_open_exception_classifier_is_conservative(
        self,
        error,
        expected,
    ):
        gateway_result = __import__(
            "reticulumpi.builtin_plugins.meshtastic_gateway",
            fromlist=["MeshtasticGateway"],
        ).MeshtasticGateway._classify_serial_open_exception(error)
        assert gateway_result.value == expected

    def test_serial_open_without_configured_endpoint_is_identity_failure(self, gateway_plugin):
        gateway_plugin._device_probe_port = ""
        gateway_plugin.config["serial_port"] = "auto"

        result = gateway_plugin._open_serial_interface_result()

        assert result.outcome.value == "identity"
        assert gateway_plugin._last_serial_open_result is result

    def test_serial_open_refuses_a_second_published_interface(self, gateway_plugin):
        published = MagicMock()
        with gateway_plugin._lock:
            gateway_plugin._mesh_interface = published
            gateway_plugin._serial_listener = None
            gateway_plugin._serial_probe_candidate = None
        with (
            patch.object(gateway_plugin, "_ensure_serial_device_lease", return_value=True),
            patch.object(gateway_plugin, "_prune_serial_workers", return_value=True),
            patch.object(_mock_meshtastic_serial, "SerialInterface") as constructor,
        ):
            result = gateway_plugin._open_serial_interface_result("/dev/meshtastic")

        assert result.outcome.value == "teardown_unproven"
        constructor.assert_not_called()

    def test_serial_open_worker_start_failure_is_typed(self, gateway_plugin):
        with gateway_plugin._lock:
            gateway_plugin._mesh_interface = None
            gateway_plugin._serial_listener = None
            gateway_plugin._serial_probe_candidate = None
        with (
            patch.object(gateway_plugin, "_ensure_serial_device_lease", return_value=True),
            patch.object(gateway_plugin, "_prune_serial_workers", return_value=True),
            patch.object(threading.Thread, "start", side_effect=RuntimeError("no thread")),
        ):
            result = gateway_plugin._open_serial_interface_result("/dev/meshtastic")

        assert result.outcome.value == "error"
        assert isinstance(result.error, RuntimeError)

    def test_serial_constructor_returning_none_is_never_published(self, gateway_plugin):
        with gateway_plugin._lock:
            gateway_plugin._mesh_interface = None
            gateway_plugin._serial_listener = None
            gateway_plugin._serial_probe_candidate = None
        with (
            patch.object(gateway_plugin, "_ensure_serial_device_lease", return_value=True),
            patch.object(gateway_plugin, "_prune_serial_workers", return_value=True),
            patch.object(_mock_meshtastic_serial, "SerialInterface", return_value=None),
        ):
            result = gateway_plugin._open_serial_interface_result("/dev/meshtastic")

        assert result.outcome.value == "teardown_unproven"
        assert result.interface is None

    def test_previously_quarantined_close_is_not_retried(self, gateway_plugin):
        iface = MagicMock()
        key = (id(iface), 5)
        gateway_plugin._ensure_serial_runtime_state()
        gateway_plugin._serial_unclosed_interfaces[key] = iface

        assert gateway_plugin._bounded_close_serial_interface(iface, 5, "retry") is False
        assert gateway_plugin._serial_teardown_unproven is True
        iface.close.assert_not_called()

    def test_close_worker_start_failure_is_bounded_and_quarantines_reuse(
        self,
        gateway_plugin,
    ):
        iface = MagicMock()
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("no thread")):
            assert gateway_plugin._bounded_close_serial_interface(iface, 8, "close") is False

        assert gateway_plugin._serial_teardown_unproven is True
        assert (id(iface), 8) not in gateway_plugin._serial_close_inflight

    def test_poison_detaches_listener_and_verified_generation(self, gateway_plugin):
        iface = MagicMock()
        with gateway_plugin._lock:
            gateway_plugin._mesh_interface = None
            gateway_plugin._serial_listener = iface
            gateway_plugin._serial_probe_candidate = None
            gateway_plugin._serial_active_generation = 12
            gateway_plugin._serial_open_generation = 12
            gateway_plugin._fw_verified_serial_generation = 12
        with patch.object(gateway_plugin, "_bounded_close_serial_interface"):
            gateway_plugin._poison_serial_generation(iface, 12, "test")

        assert gateway_plugin._serial_listener is None
        assert gateway_plugin._fw_verified_serial_generation is None
        assert gateway_plugin._serial_open_generation == 13

    def test_serial_command_rejects_stale_before_worker_start(self, gateway_plugin):
        with patch.object(
            gateway_plugin,
            "_serial_generation_is_current",
            return_value=False,
        ):
            result = gateway_plugin._run_serial_command(
                MagicMock(),
                1,
                "stale",
                lambda: True,
            )

        assert result.outcome.value == "stale"

    def test_serial_command_worker_can_become_stale_before_callback(self, gateway_plugin):
        callback = MagicMock()
        with patch.object(
            gateway_plugin,
            "_serial_generation_is_current",
            side_effect=[True, False],
        ):
            result = gateway_plugin._run_serial_command(
                MagicMock(),
                1,
                "stale-worker",
                callback,
            )

        assert result.outcome.value == "stale"
        callback.assert_not_called()

    def test_serial_command_worker_start_failure_releases_gate(self, gateway_plugin):
        with (
            patch.object(
                gateway_plugin,
                "_serial_generation_is_current",
                return_value=True,
            ),
            patch.object(threading.Thread, "start", side_effect=RuntimeError("no thread")),
        ):
            result = gateway_plugin._run_serial_command(
                MagicMock(),
                1,
                "start-failure",
                lambda: True,
            )

        assert result.outcome.value == "error"
        assert gateway_plugin._serial_operation_lock.acquire(blocking=False) is True
        gateway_plugin._serial_operation_lock.release()

    def test_interface_operation_rejects_foreign_and_contains_mqtt_errors(
        self,
        gateway_plugin,
    ):
        foreign = MagicMock()
        assert (
            gateway_plugin._invoke_interface_operation(
                foreign, "foreign", lambda: True
            ).outcome.value
            == "stale"
        )

        mqtt_iface = MagicMock()
        with gateway_plugin._lock:
            gateway_plugin._mode = "mqtt"
            gateway_plugin._mesh_interface = mqtt_iface
            gateway_plugin._connected = True
        result = gateway_plugin._invoke_interface_operation(
            mqtt_iface,
            "mqtt-error",
            lambda: (_ for _ in ()).throw(KeyboardInterrupt),
        )
        assert result.outcome.value == "error"
        assert isinstance(result.error, KeyboardInterrupt)

    def test_wait_helpers_cover_timeout_wake_shutdown_and_reenumeration_delay(
        self,
        gateway_plugin,
    ):
        gateway_plugin._active = True
        gateway_plugin._serial_reconnect_requested.clear()
        assert gateway_plugin._wait_for_serial_wake(0) is False

        gateway_plugin._serial_reconnect_requested.set()
        assert gateway_plugin._wait_for_serial_wake(1) is True
        assert not gateway_plugin._serial_reconnect_requested.is_set()

        gateway_plugin._active = False
        assert gateway_plugin._wait_for_serial_wake(1) is True

        gateway_plugin._active = True
        with gateway_plugin._lock:
            gateway_plugin._fw_recovery_not_before = 105.0
        with (
            patch("time.monotonic", return_value=100.0),
            patch.object(gateway_plugin, "_sleep_while_active") as sleep,
        ):
            assert gateway_plugin._wait_for_recovery_reopen_delay() is True
        sleep.assert_called_once_with(5.0)

    def test_ready_guard_reports_pending_teardown_and_unknown_state(self, gateway_plugin):
        gateway_plugin._fw_watchdog_enabled = True
        gateway_plugin._fw_dependency_error = None
        for updates, fragment in (
            ({"_fw_recovery_pending": True}, "still pending"),
            (
                {"_fw_recovery_pending": False, "_serial_teardown_unproven": True},
                "teardown is unproven",
            ),
            (
                {
                    "_serial_teardown_unproven": False,
                    "_fw_recovery_state": "suspect",
                },
                "suspect",
            ),
        ):
            with gateway_plugin._lock:
                for key, value in updates.items():
                    setattr(gateway_plugin, key, value)
            with patch.object(gateway_plugin, "mark_degraded") as degraded:
                gateway_plugin._mark_ready_with_radio_guard()
            assert fragment in degraded.call_args.args[0]

    def test_recovery_epoch_claim_and_begin_reject_existing_or_stale_work(
        self,
        gateway_plugin,
    ):
        with gateway_plugin._lock:
            gateway_plugin._fw_recovery_pending = True
            gateway_plugin._fw_recovery_attempting = False
        assert gateway_plugin._claim_recovery_attempt() is None
        assert not gateway_plugin._fw_recovery_operation_lock.locked()

        with gateway_plugin._lock:
            gateway_plugin._fw_recovery_pending = False
            gateway_plugin._fw_recovery_epoch = 10
        assert (
            gateway_plugin._begin_pending_recovery(
                "waiting_for_reopen",
                "soft_reboot",
                epoch=9,
            )
            is False
        )

    def test_reset_reservation_fallback_and_durable_denial(self, gateway_plugin):
        gateway_plugin._fw_reset_limiter = None
        with patch.object(gateway_plugin, "_fw_reset_allowed", return_value=False):
            assert gateway_plugin._reserve_reset("soft") is False

        gateway_plugin._fw_total_resets = 0
        gateway_plugin._fw_reset_timestamps = []
        with (
            patch.object(gateway_plugin, "_fw_reset_allowed", return_value=True),
            patch("time.monotonic", return_value=42.0),
        ):
            assert gateway_plugin._reserve_reset("soft") is True
        assert gateway_plugin._fw_reset_timestamps == [42.0]
        assert gateway_plugin._fw_total_resets == 1

        limiter = MagicMock()
        limiter.reserve.return_value.allowed = False
        limiter.reserve.return_value.reason = "rate_limited"
        limiter.reserve.return_value.recent_attempts = 3
        gateway_plugin._fw_reset_limiter = limiter
        with patch.object(gateway_plugin, "_set_fw_state") as state:
            assert gateway_plugin._reserve_reset("hard") is False
        state.assert_called_once()

    def test_hard_recovery_failure_paths_never_begin_pending_recovery(
        self,
        gateway_plugin,
    ):
        gateway_plugin._fw_usb_power_cycle = False
        assert gateway_plugin._attempt_usb_recovery(1, "hard") is False

        gateway_plugin._fw_usb_power_cycle = True
        with patch.object(gateway_plugin, "_physical_serial_port", return_value=None):
            assert gateway_plugin._attempt_usb_recovery(1, "hard") is False

        with (
            patch.object(
                gateway_plugin,
                "_physical_serial_port",
                return_value="/dev/meshtastic",
            ),
            patch.object(gateway_plugin, "_resolve_usb_device_path", return_value=None),
        ):
            assert gateway_plugin._attempt_usb_recovery(1, "hard") is False

        with (
            patch.object(
                gateway_plugin,
                "_physical_serial_port",
                return_value="/dev/meshtastic",
            ),
            patch.object(
                gateway_plugin,
                "_resolve_usb_device_path",
                return_value="/dev/bus/usb/001/002",
            ),
            patch.object(gateway_plugin, "_reserve_reset", return_value=False),
        ):
            assert gateway_plugin._attempt_usb_recovery(1, "hard") is False

        with (
            patch.object(
                gateway_plugin,
                "_physical_serial_port",
                return_value="/dev/meshtastic",
            ),
            patch.object(
                gateway_plugin,
                "_resolve_usb_device_path",
                return_value="/dev/bus/usb/001/002",
            ),
            patch.object(gateway_plugin, "_reserve_reset", return_value=True),
            patch.object(gateway_plugin, "_usb_bus_reset", side_effect=OSError("failed")),
        ):
            assert gateway_plugin._attempt_usb_recovery(1, "hard") is False

        with (
            patch.object(
                gateway_plugin,
                "_physical_serial_port",
                return_value="/dev/meshtastic",
            ),
            patch.object(
                gateway_plugin,
                "_resolve_usb_device_path",
                return_value="/dev/bus/usb/001/002",
            ),
            patch.object(gateway_plugin, "_reserve_reset", return_value=True),
            patch.object(
                gateway_plugin,
                "_usb_bus_reset",
                return_value={"ok": False, "reason": "device gone"},
            ),
        ):
            assert gateway_plugin._attempt_usb_recovery(1, "hard") is False

    def test_compatibility_recovery_hooks_delegate_to_state_machine(self, gateway_plugin):
        with (
            patch.object(gateway_plugin, "_reserve_reset") as reserve,
            patch.object(gateway_plugin, "_set_fw_state") as state,
        ):
            gateway_plugin._record_reset("manual")
            gateway_plugin._post_recovery_wait()

        reserve.assert_called_once_with("manual")
        state.assert_called_once_with("waiting_for_reopen")

    def test_serial_interface_helpers_cover_both_modes(self, gateway_plugin):
        serial_iface = MagicMock()
        mqtt_listener = MagicMock()
        with gateway_plugin._lock:
            gateway_plugin._mode = "serial"
            gateway_plugin._mesh_interface = serial_iface
        assert gateway_plugin._get_serial_interface() is serial_iface
        assert gateway_plugin._get_serial_node() is serial_iface.localNode

        with gateway_plugin._lock:
            gateway_plugin._mode = "mqtt"
            gateway_plugin._serial_listener = mqtt_listener
        assert gateway_plugin._get_serial_interface() is mqtt_listener
        assert gateway_plugin.get_channels() == []

    def test_channel_writes_require_a_local_serial_node(self, gateway_plugin):
        with patch.object(gateway_plugin, "_get_serial_interface", return_value=None):
            assert gateway_plugin.join_channel("test", "default")["ok"] is False
            assert (
                gateway_plugin.join_channel_url("https://meshtastic.org/e/#example")["ok"] is False
            )
            assert gateway_plugin.delete_channel(1)["ok"] is False

    def test_dashboard_send_reports_all_bounded_command_failures(self, gateway_plugin):
        from reticulumpi.builtin_plugins.meshtastic_gateway import (
            _SerialCommandOutcome,
            _SerialCommandResult,
        )

        iface = MagicMock()
        with gateway_plugin._lock:
            gateway_plugin._serial_listener = iface
            gateway_plugin._serial_active_generation = 20
            gateway_plugin._serial_open_generation = 20

        outcomes = [
            (
                _SerialCommandResult(_SerialCommandOutcome.TIMEOUT, started=True),
                "delivery_uncertain",
            ),
            (
                _SerialCommandResult(
                    _SerialCommandOutcome.ERROR,
                    error=RuntimeError("send failed"),
                    started=False,
                ),
                "send failed",
            ),
            (_SerialCommandResult(_SerialCommandOutcome.BUSY), "busy"),
        ]
        for command, reason in outcomes:
            with patch.object(
                gateway_plugin,
                "_invoke_interface_operation",
                return_value=command,
            ):
                result = gateway_plugin.send_message("hello", via="lora")
            assert result == {"sent": False, "reason": reason}

    def test_dashboard_send_executes_direct_and_broadcast_callbacks(self, gateway_plugin):
        from reticulumpi.builtin_plugins.meshtastic_gateway import (
            _SerialCommandOutcome,
            _SerialCommandResult,
        )

        iface = MagicMock()
        with gateway_plugin._lock:
            gateway_plugin._serial_listener = iface
            gateway_plugin._serial_active_generation = 21
            gateway_plugin._serial_open_generation = 21

        def invoke(_iface, _operation, callback):
            return _SerialCommandResult(
                _SerialCommandOutcome.SUCCESS,
                value=callback(),
                started=True,
            )

        with patch.object(gateway_plugin, "_invoke_interface_operation", side_effect=invoke):
            assert (
                gateway_plugin.send_message(
                    "direct",
                    destination_id="!1234abcd",
                    via="lora",
                )["sent"]
                is True
            )
            assert gateway_plugin.send_message("broadcast", via="lora")["sent"] is True

        assert iface.sendText.call_count == 2

    def test_read_receipt_and_mqtt_activity_failure_branches(self, gateway_plugin):
        from reticulumpi.builtin_plugins.meshtastic_gateway import (
            _SerialCommandOutcome,
            _SerialCommandResult,
        )

        iface = MagicMock()
        with gateway_plugin._lock:
            gateway_plugin._mode = "serial"
            gateway_plugin._mesh_interface = iface
        with patch.object(
            gateway_plugin,
            "_invoke_interface_operation",
            return_value=_SerialCommandResult(
                _SerialCommandOutcome.TIMEOUT,
                started=True,
            ),
        ):
            result = gateway_plugin.send_read_receipt(1, "!1234abcd")
        assert result == {"sent": False, "reason": "delivery_uncertain"}

        with gateway_plugin._lock:
            gateway_plugin._mode = "mqtt"
            gateway_plugin._mesh_interface = iface
            gateway_plugin._serial_listener = None
            gateway_plugin._active = True
            gateway_plugin._last_mqtt_activity = 0.0
        gateway_plugin._on_mesh_data({"decoded": {"portnum": "OTHER"}}, interface=iface)
        assert gateway_plugin._last_mqtt_activity > 0

    @pytest.mark.parametrize("outcome", ["stale", "error", "timeout"])
    def test_health_probe_maps_bounded_command_outcomes_and_restores_candidate(
        self,
        gateway_plugin,
        outcome,
    ):
        from reticulumpi.builtin_plugins.meshtastic_gateway import (
            _SerialCommandOutcome,
            _SerialCommandResult,
        )

        iface = MagicMock()
        with gateway_plugin._lock:
            gateway_plugin._mesh_interface = None
            gateway_plugin._serial_listener = None
            gateway_plugin._serial_probe_candidate = None
            gateway_plugin._serial_active_generation = 31
            gateway_plugin._serial_open_generation = 31
        command_outcome = _SerialCommandOutcome(outcome)
        command = _SerialCommandResult(
            command_outcome,
            error=RuntimeError("transport") if outcome == "error" else None,
            started=outcome != "stale",
        )

        with patch.object(gateway_plugin, "_run_serial_command", return_value=command):
            result = gateway_plugin._probe_device_health(iface, generation=31)

        assert (
            result.outcome.value
            == {
                "stale": "stale_generation",
                "error": "transport_error",
                "timeout": "timeout",
            }[outcome]
        )
        assert gateway_plugin._serial_probe_candidate is None

    def test_health_probe_without_any_serial_interface_is_stale(self, gateway_plugin):
        with gateway_plugin._lock:
            gateway_plugin._mesh_interface = None
            gateway_plugin._serial_listener = None
            gateway_plugin._serial_probe_candidate = None

        result = gateway_plugin._probe_device_health()

        assert result.outcome.value == "stale_generation"
        assert result.detail == "serial_interface_unavailable"

    @pytest.mark.parametrize(
        ("probe_outcome", "expected", "hang_reason"),
        [
            ("stale_generation", False, None),
            ("transport_error", False, "probe_transport_error"),
        ],
    )
    def test_watchdog_handles_stale_and_transport_probe_outcomes(
        self,
        gateway_plugin,
        probe_outcome,
        expected,
        hang_reason,
    ):
        from reticulumpi.meshtastic_health import (
            MeshtasticHealthOutcome,
            MeshtasticHealthResult,
        )

        gateway_plugin._fw_watchdog_enabled = True
        gateway_plugin._fw_recovery_pending = False
        gateway_plugin._fw_probe_interval = 0
        with gateway_plugin._lock:
            gateway_plugin._last_serial_activity = 0.0
            gateway_plugin._last_fw_probe_time = 0.0
        probe = MeshtasticHealthResult(
            MeshtasticHealthOutcome(probe_outcome),
            "test-result",
        )
        with (
            patch.object(gateway_plugin, "_check_usb_present", return_value=True),
            patch.object(gateway_plugin, "_probe_device_health", return_value=probe),
            patch.object(gateway_plugin, "_handle_firmware_hang") as hang,
        ):
            assert gateway_plugin._check_firmware_watchdog() is expected

        if hang_reason is None:
            hang.assert_not_called()
        else:
            hang.assert_called_once_with(hang_reason, allow_soft=False)

    def test_watchdog_pending_recovery_blocks_listener_reuse(self, gateway_plugin):
        gateway_plugin._fw_watchdog_enabled = True
        gateway_plugin._fw_recovery_pending = True

        assert gateway_plugin._check_firmware_watchdog() is False

    def test_startup_hang_ends_pending_epoch_when_auto_reset_is_disabled(
        self,
        gateway_plugin,
    ):
        with gateway_plugin._lock:
            gateway_plugin._fw_recovery_pending = True
            gateway_plugin._fw_recovery_method = "soft_reboot"
            gateway_plugin._fw_recovery_hard_escalated = False
            gateway_plugin._fw_recovery_not_before = 100.0
            gateway_plugin._fw_auto_reset = False
        with patch.object(gateway_plugin, "_set_fw_state") as state:
            gateway_plugin._handle_startup_firmware_hang()

        assert gateway_plugin._fw_recovery_pending is False
        assert gateway_plugin._fw_recovery_not_before == 0.0
        assert "automatic escalation is disabled" in state.call_args.args[1]

    def test_soft_recovery_identity_and_reservation_failures_do_not_hard_reset(
        self,
        gateway_plugin,
    ):
        from reticulumpi.builtin_plugins.meshtastic_gateway import (
            _SerialCommandOutcome,
            _SerialCommandResult,
        )

        iface = MagicMock()
        with gateway_plugin._lock:
            gateway_plugin._mesh_interface = iface
            gateway_plugin._serial_listener = None
            gateway_plugin._serial_active_generation = 41
            gateway_plugin._serial_open_generation = 41
            gateway_plugin._fw_recovery_pending = False
            gateway_plugin._fw_recovery_attempting = False
        gateway_plugin._fw_usb_power_cycle = False
        with (
            patch.object(gateway_plugin, "_bind_or_validate_usb_identity", return_value=False),
            patch.object(gateway_plugin, "_set_fw_state") as state,
        ):
            assert gateway_plugin._attempt_firmware_recovery("identity") is False
        assert "identity validation failed" in state.call_args.args[1]

        with gateway_plugin._lock:
            gateway_plugin._fw_recovery_pending = False
            gateway_plugin._fw_recovery_attempting = False

        def run_callback(_iface, _generation, _operation, callback):
            return _SerialCommandResult(
                _SerialCommandOutcome.SUCCESS,
                value=callback(),
                started=True,
            )

        with (
            patch.object(gateway_plugin, "_bind_or_validate_usb_identity", return_value=True),
            patch.object(gateway_plugin, "_reserve_reset", return_value=False) as reserve,
            patch.object(gateway_plugin, "_run_serial_command", side_effect=run_callback),
        ):
            assert gateway_plugin._attempt_firmware_recovery("reserve") is False
        reserve.assert_called_once_with("soft_reboot")

    def test_uncertain_soft_reboot_begins_verification_without_retry(self, gateway_plugin):
        from reticulumpi.builtin_plugins.meshtastic_gateway import (
            _SerialCommandOutcome,
            _SerialCommandResult,
        )

        iface = MagicMock()
        with gateway_plugin._lock:
            gateway_plugin._mesh_interface = iface
            gateway_plugin._serial_listener = None
            gateway_plugin._serial_active_generation = 42
            gateway_plugin._serial_open_generation = 42
            gateway_plugin._fw_recovery_pending = False
            gateway_plugin._fw_recovery_attempting = False
        with (
            patch.object(gateway_plugin, "_bind_or_validate_usb_identity", return_value=True),
            patch.object(
                gateway_plugin,
                "_run_serial_command",
                return_value=_SerialCommandResult(
                    _SerialCommandOutcome.TIMEOUT,
                    started=True,
                ),
            ),
        ):
            assert gateway_plugin._attempt_firmware_recovery("uncertain") is True

        assert gateway_plugin._fw_recovery_pending is True
        assert gateway_plugin._fw_recovery_method == "soft_reboot_uncertain"

    def test_channel_command_failures_surface_bounded_delivery_state(self, gateway_plugin):
        from types import SimpleNamespace

        from reticulumpi.builtin_plugins.meshtastic_gateway import (
            _SerialCommandOutcome,
            _SerialCommandResult,
        )

        role = SimpleNamespace(
            PRIMARY=1,
            SECONDARY=2,
            Name=lambda value: str(value),
        )
        channel_pb2 = SimpleNamespace(Channel=SimpleNamespace(Role=role))
        util = SimpleNamespace(fromPSK=lambda _label: b"\x01")
        iface = MagicMock()
        node = iface.localNode
        node.channels = [MagicMock()]
        channel = MagicMock()
        channel.index = 1
        channel.role = role.SECONDARY
        node.getDisabledChannel.return_value = channel
        node.getChannelByChannelIndex.return_value = channel
        timeout = _SerialCommandResult(_SerialCommandOutcome.TIMEOUT, started=True)

        with (
            patch.object(gateway_plugin, "_get_serial_interface", return_value=iface),
            patch.object(gateway_plugin, "_invoke_interface_operation", return_value=timeout),
            patch.object(_mock_meshtastic, "channel_pb2", channel_pb2),
            patch.dict(
                sys.modules,
                {
                    "meshtastic.channel_pb2": channel_pb2,
                    "meshtastic.util": util,
                },
            ),
        ):
            assert (
                "delivery uncertain"
                in gateway_plugin.join_channel(
                    "test",
                    "default",
                )["reason"]
            )
            assert (
                "delivery uncertain"
                in gateway_plugin.join_channel_url("https://meshtastic.org/e/#example")["reason"]
            )
            assert "delivery uncertain" in gateway_plugin.delete_channel(1)["reason"]


class TestMeshtasticOwnershipRaces:
    """Late third-party returns must never cross a stop or ownership swap."""

    @staticmethod
    def _wait_until(predicate, timeout=1.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return bool(predicate())

    @staticmethod
    def _run_capturing(callable_, errors):
        try:
            callable_()
        except BaseException as exc:
            errors.append(exc)

    def test_stop_during_primary_serial_constructor_closes_late_exact_handle(
        self,
        gateway_plugin,
    ):
        from reticulumpi import events

        plugin = gateway_plugin
        entered = threading.Event()
        release = threading.Event()
        late_iface = MagicMock(name="late-primary-serial")
        lease = MagicMock(name="serial-lease")
        errors = []

        def blocked_constructor(**_kwargs):
            entered.set()
            assert release.wait(timeout=2)
            return late_iface

        with plugin._lock:
            plugin._mode = "serial"
            plugin._active = True
            plugin._connected = False
            plugin._mesh_interface = None
            plugin._serial_listener = None
            plugin._serial_probe_candidate = None
            plugin._serial_open_generation = 50
            plugin._serial_active_generation = 50
            plugin._serial_device_lease = lease
        plugin._device_probe_open_timeout = 2.0
        plugin.event_bus.publish.reset_mock()

        with (
            patch.object(plugin, "_ensure_serial_device_lease", return_value=True),
            patch.object(
                _mock_meshtastic_serial,
                "SerialInterface",
                side_effect=blocked_constructor,
            ),
        ):
            connector = threading.Thread(
                target=self._run_capturing,
                args=(plugin._connect_mesh_device, errors),
            )
            connector.start()
            assert entered.wait(timeout=1)

            plugin.stop()

            assert plugin._serial_open_generation > 50
            assert plugin._serial_device_lease is lease
            lease.release.assert_not_called()
            release.set()
            connector.join(timeout=2)

        assert not connector.is_alive()
        assert errors
        assert self._wait_until(lambda: late_iface.close.call_count == 1)
        assert self._wait_until(lambda: lease.release.call_count == 1)
        assert plugin._mesh_interface is None
        assert plugin._serial_listener is None
        assert plugin._serial_probe_candidate is None
        assert plugin._connected is False
        assert not any(
            call.args and call.args[0] == events.MESHTASTIC_CONNECTED
            for call in plugin.event_bus.publish.call_args_list
        )

    def test_stop_during_primary_mqtt_constructor_closes_late_exact_client(
        self,
        mqtt_gateway_plugin,
    ):
        from reticulumpi import events

        plugin = mqtt_gateway_plugin
        entered = threading.Event()
        release = threading.Event()
        late_iface = MagicMock(name="late-primary-mqtt")
        errors = []

        def blocked_constructor():
            entered.set()
            assert release.wait(timeout=2)
            return late_iface

        with plugin._lock:
            plugin._mode = "mqtt"
            plugin._active = True
            plugin._connected = False
            plugin._mesh_interface = None
            plugin._serial_listener = None
            plugin._serial_probe_candidate = None
        plugin._internet_available = True
        plugin.event_bus.publish.reset_mock()

        with (
            patch.object(plugin, "_create_mqtt_interface", side_effect=blocked_constructor),
            patch.object(plugin, "_sleep_while_active"),
        ):
            connector = threading.Thread(
                target=self._run_capturing,
                args=(plugin._connection_loop, errors),
            )
            connector.start()
            assert entered.wait(timeout=1)

            plugin.stop()

            release.set()
            connector.join(timeout=2)

        assert not connector.is_alive()
        assert errors == []
        late_iface.close.assert_called_once_with()
        assert plugin._mesh_interface is None
        assert plugin._connected is False
        assert not any(
            call.args
            and call.args[0] in {events.MESHTASTIC_CONNECTED, events.MESHTASTIC_CONNECT_FAILED}
            for call in plugin.event_bus.publish.call_args_list
        )

    def test_stop_during_mqtt_listener_constructor_closes_late_handle_and_retains_lease(
        self,
        mqtt_gateway_plugin,
    ):
        plugin = mqtt_gateway_plugin
        entered = threading.Event()
        release = threading.Event()
        late_iface = MagicMock(name="late-mqtt-listener")
        lease = MagicMock(name="listener-lease")

        def blocked_constructor(**_kwargs):
            entered.set()
            assert release.wait(timeout=2)
            return late_iface

        with plugin._lock:
            plugin._mode = "mqtt"
            plugin._active = True
            plugin._connected = True
            plugin._mesh_interface = MagicMock(name="mqtt-primary")
            plugin._serial_listener = None
            plugin._serial_probe_candidate = None
            plugin._serial_open_generation = 70
            plugin._serial_active_generation = 70
            plugin._serial_device_lease = lease
        plugin._device_probe_port = "/dev/meshtastic"
        plugin._device_probe_startup_delay = 0.0
        plugin._device_probe_open_timeout = 2.0

        with (
            patch.object(plugin, "_ensure_serial_device_lease", return_value=True),
            patch.object(plugin, "_sleep_while_active"),
            patch.object(
                _mock_meshtastic_serial,
                "SerialInterface",
                side_effect=blocked_constructor,
            ),
        ):
            listener = threading.Thread(target=plugin._device_probe_loop)
            listener.start()
            assert entered.wait(timeout=1)

            plugin.stop()

            assert plugin._serial_device_lease is lease
            lease.release.assert_not_called()
            release.set()
            listener.join(timeout=2)

        assert not listener.is_alive()
        assert self._wait_until(lambda: late_iface.close.call_count == 1)
        assert self._wait_until(lambda: lease.release.call_count == 1)
        assert plugin._serial_listener is None
        assert plugin._serial_probe_candidate is None

    def test_serial_send_return_after_generation_swap_is_uncertain_not_success(
        self,
        gateway_plugin,
    ):
        from reticulumpi import events

        plugin = gateway_plugin
        iface = MagicMock(name="serial-send")
        entered = threading.Event()
        release = threading.Event()
        results = []

        def blocked_send(*_args, **_kwargs):
            entered.set()
            assert release.wait(timeout=2)
            return {"id": 123}

        iface.sendText.side_effect = blocked_send
        with plugin._lock:
            plugin._active = True
            plugin._connected = True
            plugin._mode = "serial"
            plugin._mesh_interface = iface
            plugin._serial_listener = None
            plugin._serial_active_generation = 80
            plugin._serial_open_generation = 80
            before = plugin._msgs_hub_to_mesh
        plugin.event_bus.publish.reset_mock()

        sender = threading.Thread(
            target=lambda: results.append(plugin.send_message("once")),
        )
        sender.start()
        assert entered.wait(timeout=1)
        with plugin._lock:
            plugin._serial_open_generation += 1
        release.set()
        sender.join(timeout=2)

        assert results == [{"sent": False, "reason": "delivery_uncertain"}]
        assert plugin._msgs_hub_to_mesh == before
        assert iface.sendText.call_count == 1
        assert not any(
            call.args and call.args[0] == events.MESHTASTIC_MESSAGE_SENT
            for call in plugin.event_bus.publish.call_args_list
        )

    def test_mqtt_send_return_after_interface_swap_is_uncertain_not_success(
        self,
        mqtt_gateway_plugin,
    ):
        from reticulumpi import events

        plugin = mqtt_gateway_plugin
        iface = MagicMock(name="mqtt-send")
        entered = threading.Event()
        release = threading.Event()
        results = []

        def blocked_send(*_args, **_kwargs):
            entered.set()
            assert release.wait(timeout=2)
            return {"id": 124}

        iface.sendText.side_effect = blocked_send
        with plugin._lock:
            plugin._active = True
            plugin._connected = True
            plugin._mode = "mqtt"
            plugin._mesh_interface = iface
            plugin._serial_listener = None
            before = plugin._msgs_hub_to_mesh
        plugin.event_bus.publish.reset_mock()

        sender = threading.Thread(
            target=lambda: results.append(plugin.send_message("once")),
        )
        sender.start()
        assert entered.wait(timeout=1)
        with plugin._lock:
            plugin._mesh_interface = MagicMock(name="replacement-mqtt")
        release.set()
        sender.join(timeout=2)

        assert results == [{"sent": False, "reason": "delivery_uncertain"}]
        assert plugin._msgs_hub_to_mesh == before
        assert iface.sendText.call_count == 1
        assert not any(
            call.args and call.args[0] == events.MESHTASTIC_MESSAGE_SENT
            for call in plugin.event_bus.publish.call_args_list
        )

    def test_mqtt_publish_admission_failure_is_not_recorded_as_sent(
        self,
        mqtt_gateway_plugin,
    ):
        from reticulumpi import events
        from reticulumpi.builtin_plugins.meshtastic_gateway import _MeshtasticMQTTClient

        plugin = mqtt_gateway_plugin
        iface = object.__new__(_MeshtasticMQTTClient)
        iface._lock = threading.Lock()
        iface._my_node_num = 0x12345678
        iface._packet_id_allocator = MagicMock()
        iface._packet_id_allocator.take.return_value = 42
        iface._aes_key = None
        iface._root_topic = "msh/US/2/e/LongFast"
        iface.client = MagicMock()
        iface.client.publish.return_value.rc = 4
        with plugin._lock:
            plugin._active = True
            plugin._connected = True
            plugin._mode = "mqtt"
            plugin._mesh_interface = iface
            plugin._serial_listener = None
            before = plugin._msgs_hub_to_mesh
        plugin.event_bus.publish.reset_mock()

        result = plugin.send_message("never admitted")

        assert result == {"sent": False, "reason": "delivery_uncertain"}
        assert plugin._msgs_hub_to_mesh == before
        iface.client.publish.assert_called_once()
        assert not any(
            call.args and call.args[0] == events.MESHTASTIC_MESSAGE_SENT
            for call in plugin.event_bus.publish.call_args_list
        )

    def test_device_write_return_after_generation_swap_is_not_committed_or_retried(
        self,
        gateway_plugin,
    ):
        plugin = gateway_plugin
        iface = MagicMock(name="serial-channel-write")
        node = iface.localNode
        entered = threading.Event()
        release = threading.Event()
        results = []

        def blocked_write(*_args, **_kwargs):
            entered.set()
            assert release.wait(timeout=2)

        node.setURL.side_effect = blocked_write
        with plugin._lock:
            plugin._active = True
            plugin._connected = True
            plugin._mode = "serial"
            plugin._mesh_interface = iface
            plugin._serial_listener = None
            plugin._serial_active_generation = 90
            plugin._serial_open_generation = 90

        writer = threading.Thread(
            target=lambda: results.append(
                plugin.join_channel_url("https://meshtastic.org/e/#exact-once")
            ),
        )
        writer.start()
        assert entered.wait(timeout=1)
        with plugin._lock:
            plugin._serial_open_generation += 1
        release.set()
        writer.join(timeout=2)

        assert results == [{"ok": False, "reason": "Channel URL failed: delivery uncertain"}]
        node.setURL.assert_called_once_with(
            "https://meshtastic.org/e/#exact-once",
            addOnly=True,
        )


class TestMeshtasticSenderScopedDedup:
    @staticmethod
    def _text_packet(sender, packet_id, text):
        return {
            "id": packet_id,
            "from": sender,
            "fromId": f"!{sender:08x}",
            "to": 0xFFFFFFFF,
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "payload": text.encode("utf-8"),
            },
        }

    @staticmethod
    def _receipt_packet(sender, packet_id, read_packet_id=42):
        return {
            "id": packet_id,
            "from": sender,
            "fromId": f"!{sender:08x}",
            "decoded": {
                "portnum": "PRIVATE_APP",
                "payload": bytes([0x01]) + read_packet_id.to_bytes(4, "big"),
            },
        }

    @staticmethod
    def _event_count(plugin, event_name):
        return sum(
            1
            for call in plugin.event_bus.publish.call_args_list
            if call.args and call.args[0] == event_name
        )

    def test_text_dedup_is_cross_path_but_scoped_to_sender(self, mqtt_gateway_plugin):
        from reticulumpi import events

        plugin = mqtt_gateway_plugin
        mqtt_iface = MagicMock(name="mqtt-owned")
        serial_iface = MagicMock(name="serial-owned")
        with plugin._lock:
            plugin._active = True
            plugin._connected = True
            plugin._mode = "mqtt"
            plugin._mesh_interface = mqtt_iface
            plugin._serial_listener = serial_iface
            plugin._seen_packet_ids.clear()
            plugin._msgs_mesh_to_lxmf = 0
        plugin._forward_to_lxmf = MagicMock()
        plugin.event_bus.publish.reset_mock()

        first = self._text_packet(0x11111111, 77, "first")
        different_sender = self._text_packet(0x22222222, 77, "second")
        plugin._on_mesh_text(first, interface=mqtt_iface)
        plugin._on_mesh_text(first, interface=serial_iface)
        plugin._on_mesh_text(different_sender, interface=serial_iface)

        assert plugin._msgs_mesh_to_lxmf == 2
        assert plugin._forward_to_lxmf.call_count == 2
        assert self._event_count(plugin, events.MESHTASTIC_MESSAGE_RECEIVED) == 2
        assert set(plugin._seen_packet_ids) == {
            (0x11111111, 77),
            (0x22222222, 77),
        }

    def test_private_receipt_dedup_is_cross_path_but_scoped_to_sender(
        self,
        mqtt_gateway_plugin,
    ):
        from reticulumpi import events

        plugin = mqtt_gateway_plugin
        mqtt_iface = MagicMock(name="mqtt-owned")
        serial_iface = MagicMock(name="serial-owned")
        with plugin._lock:
            plugin._active = True
            plugin._connected = True
            plugin._mode = "mqtt"
            plugin._mesh_interface = mqtt_iface
            plugin._serial_listener = serial_iface
            plugin._seen_packet_ids.clear()
        plugin.event_bus.publish.reset_mock()

        first = self._receipt_packet(0x33333333, 88)
        different_sender = self._receipt_packet(0x44444444, 88)
        plugin._on_mesh_data(first, interface=mqtt_iface)
        plugin._on_mesh_data(first, interface=serial_iface)
        plugin._on_mesh_data(different_sender, interface=serial_iface)

        assert self._event_count(plugin, events.MESHTASTIC_READ_RECEIPT_RECEIVED) == 2
        assert set(plugin._seen_packet_ids) == {
            (0x33333333, 88),
            (0x44444444, 88),
        }

    @pytest.mark.parametrize("sender", [True, -1, "not-numeric"])
    def test_dedup_key_uses_normalized_from_id_when_numeric_sender_is_invalid(
        self,
        sender,
    ):
        from reticulumpi.builtin_plugins.meshtastic_gateway import MeshtasticGateway

        assert MeshtasticGateway._packet_dedup_key(
            {"id": 91, "from": sender, "fromId": "  !AbCd1234  "}
        ) == ("!abcd1234", 91)
        assert (
            MeshtasticGateway._packet_dedup_key({"id": 91, "from": sender, "fromId": "   "}) is None
        )

    def test_dedup_cleanup_expires_old_entries_and_bounds_cache(
        self,
        gateway_plugin,
    ):
        plugin = gateway_plugin
        with plugin._lock:
            plugin._seen_packet_ids = {
                (1, 1): 0.0,
                (2, 2): 91.0,
                (3, 3): 92.0,
            }
            plugin._dedup_inserts_since_cleanup = 0
            plugin._dedup_cleanup_interval = 1
            plugin._dedup_ttl_seconds = 10.0
            plugin._dedup_max_entries = 2
            with patch(
                "reticulumpi.builtin_plugins.meshtastic_gateway.time.monotonic",
                return_value=100.0,
            ):
                assert not plugin._packet_is_duplicate_locked(
                    {"id": 4, "from": 4, "fromId": "!00000004"}
                )

        assert plugin._seen_packet_ids == {(3, 3): 92.0, (4, 4): 100.0}
        assert plugin._dedup_inserts_since_cleanup == 0

    def test_expired_exact_key_is_new_immediately_without_periodic_cleanup(
        self,
        gateway_plugin,
    ):
        plugin = gateway_plugin
        packet = {"id": 55, "from": 0x12345678, "fromId": "!12345678"}
        key = (0x12345678, 55)
        with plugin._lock:
            plugin._seen_packet_ids = {key: 50.0}
            plugin._dedup_ttl_seconds = 30.0
            plugin._dedup_cleanup_interval = 1000
            plugin._dedup_inserts_since_cleanup = 0
            with (
                patch(
                    "reticulumpi.builtin_plugins.meshtastic_gateway.time.monotonic",
                    return_value=100.0,
                ),
                patch(
                    "reticulumpi.builtin_plugins.meshtastic_gateway.time.time",
                    return_value=-1_000_000.0,
                ),
            ):
                assert plugin._packet_is_duplicate_locked(packet) is False
                assert plugin._seen_packet_ids[key] == 100.0
                assert plugin._packet_is_duplicate_locked(packet) is True

        assert plugin._dedup_inserts_since_cleanup == 1


def test_omitted_serial_port_uses_documented_stable_default(mock_app, gw_config):
    from reticulumpi.builtin_plugins.meshtastic_gateway import (
        SerialOpenOutcome,
        SerialOpenResult,
    )

    gw_config.pop("serial_port")
    plugin = _make_plugin_no_start(mock_app, gw_config)
    iface = MagicMock()
    with patch.object(
        plugin,
        "_open_serial_interface_result",
        return_value=SerialOpenResult(
            SerialOpenOutcome.OPENED,
            interface=iface,
            generation=1,
        ),
    ) as open_result:
        assert plugin._create_serial_interface() is iface

    open_result.assert_called_once_with("/dev/meshtastic")
