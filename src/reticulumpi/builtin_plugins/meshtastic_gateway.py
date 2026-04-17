"""Meshtastic Gateway plugin — bridges Meshtastic text with LXMF over Reticulum.

Connects to a Meshtastic network via serial device or MQTT and translates
text messages bidirectionally between the Meshtastic mesh and Reticulum/LXMF.

Modes:
  serial — connects to a Meshtastic-firmware LoRa device over USB serial.
  mqtt   — connects to a Meshtastic MQTT broker (no hardware required).
           WARNING: messages published via MQTT may be rebroadcast over LoRa
           by any Meshtastic node with uplink enabled.  Use a private channel
           or keep the rate limit low to avoid flooding the regional mesh.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

import RNS
import RNS.vendor.umsgpack as umsgpack

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

# Meshtastic payload limit (bytes).  Messages longer than this are truncated.
MESHTASTIC_MTU = 237

# Default message prefixes
DEFAULT_MESH_PREFIX = "[Mesh]"
DEFAULT_LXMF_PREFIX = "[LXMF]"

# Connection modes
MODE_SERIAL = "serial"
MODE_MQTT = "mqtt"

# Regex for 32-char hex hash
_HEX_HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")

# Regex for Meshtastic node ID  e.g. !abcd1234
_MESH_NODE_ID_RE = re.compile(r"^![0-9a-fA-F]{8}$")

# MQTT default configuration
_MQTT_DEFAULTS: dict[str, Any] = {
    "broker": "mqtt.meshtastic.org",
    "port": 1883,
    "username": "meshdev",
    "password": "large4cats",
    "root_topic": "msh/US/2/e/LongFast",
    "channel_key": "AQ==",
}

# The well-known Meshtastic default channel key (PSK byte 0x01 expands to this).
_MESHTASTIC_DEFAULT_KEY = bytes([
    0xd4, 0xf1, 0xbb, 0x3a, 0x20, 0x29, 0x07, 0x59,
    0xf0, 0xbc, 0xff, 0xab, 0xcf, 0x4e, 0x69, 0x01,
])

# Broadcast address for all nodes
_MESH_BROADCAST = 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Native Meshtastic MQTT client (replaces non-existent MQTTInterface)
# ---------------------------------------------------------------------------


class _MeshtasticMQTTClient:
    """Lightweight Meshtastic MQTT client using paho-mqtt + protobufs.

    Provides a compatible interface for the gateway plugin:
    - ``sendText(text, channelIndex)`` to publish text messages
    - ``nodes`` dict for discovered mesh nodes
    - ``client.is_connected()`` for health checks
    - ``close()`` for teardown

    Incoming text messages are delivered via pypubsub with the same
    topic names that the meshtastic library uses:
    ``meshtastic.receive.text``, ``meshtastic.connection.established``,
    ``meshtastic.connection.lost``.
    """

    def __init__(
        self,
        broker: str,
        port: int,
        username: str,
        password: str,
        root_topic: str,
        ch_index: int,
        ch_key_b64: str,
        logger: Any = None,
        node_num: int | None = None,
        long_name: str = "",
        short_name: str = "",
    ):
        import base64
        import hashlib
        import random

        import paho.mqtt.client as mqtt_client

        self._broker = broker
        self._port = port
        self._root_topic = root_topic.rstrip("/")
        self._ch_index = ch_index
        self._logger = logger
        self._lock = threading.Lock()

        # Derive the AES key from the base64-encoded channel PSK.
        raw_key = base64.b64decode(ch_key_b64)
        if len(raw_key) == 0:
            self._aes_key = None  # No encryption
        elif len(raw_key) == 1:
            # Single-byte PSK: 0x01 = default key, others expand via SHA-256.
            if raw_key[0] == 1:
                self._aes_key = _MESHTASTIC_DEFAULT_KEY
            else:
                self._aes_key = hashlib.sha256(raw_key).digest()[:16]
        elif len(raw_key) in (16, 32):
            self._aes_key = raw_key
        else:
            # Truncate/pad to 32 bytes via hash
            self._aes_key = hashlib.sha256(raw_key).digest()

        # Persistent identity — use provided node_num or generate random.
        if node_num is not None:
            self._my_node_num = node_num
        else:
            self._my_node_num = random.randint(0x10000000, 0x7FFFFFFF)
        self._long_name = long_name
        self._short_name = short_name

        # Derive initial packet_id from node_num to reduce collision risk
        # across restarts without persisting the counter.
        seed = hashlib.sha256(self._my_node_num.to_bytes(4, "big")).digest()
        self._next_packet_id = int.from_bytes(seed[:4], "big") | 1  # Ensure non-zero

        # Node database (populated from received NODEINFO_APP messages)
        self.nodes: dict[str, dict] = {}

        # Mimic myInfo for _get_own_node_id()
        class _MyInfo:
            my_node_num = self._my_node_num
        self.myInfo = _MyInfo()

        # NODEINFO announcement tracking
        self._last_nodeinfo_time: float = 0
        self._nodeinfo_interval: float = 900  # 15 minutes, matches Meshtastic firmware

        # Register ourselves in the node database
        self._register_self_in_nodes()

        # Set up paho-mqtt client
        client_id = f"reticulumpi-gw-{self._my_node_num:08x}"
        self.client = mqtt_client.Client(
            mqtt_client.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )
        if username:
            self.client.username_pw_set(username, password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        # Set connect timeout to avoid hanging on unresponsive brokers
        self.client.connect_timeout = 10.0
        self.client.connect(broker, port, keepalive=60)
        self.client.loop_start()

    def close(self) -> None:
        """Disconnect and clean up."""
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass  # Best-effort cleanup

    # ── paho callbacks ──────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        """Subscribe to the root topic on connect and announce our identity."""
        topic = f"{self._root_topic}/#"
        client.subscribe(topic, qos=0)
        if self._logger:
            self._logger.debug("MQTT subscribed to %s", topic)
        try:
            self.sendNodeInfo()
        except Exception:
            if self._logger:
                self._logger.debug("Error sending NODEINFO on connect", exc_info=True)
        try:
            from pubsub import pub
            pub.sendMessage("meshtastic.connection.established", interface=self)
        except Exception:
            pass  # pubsub is optional

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        """Notify on disconnect."""
        try:
            from pubsub import pub
            pub.sendMessage("meshtastic.connection.lost", interface=self)
        except Exception:
            pass  # pubsub is optional

    def _on_message(self, client, userdata, msg):
        """Decode incoming MQTT message and dispatch via pubsub."""
        try:
            self._process_mqtt_message(msg.topic, msg.payload)
        except Exception as exc:
            if self._logger:
                from google.protobuf.message import DecodeError
                if isinstance(exc, DecodeError):
                    self._logger.debug(
                        "Could not decode MQTT message (expected for encrypted/incompatible packets)"
                    )
                else:
                    self._logger.warning("Error processing MQTT message", exc_info=True)

    # ── Message decoding ────────────────────────────────────────────

    def _process_mqtt_message(self, topic: str, payload: bytes) -> None:
        """Parse a ServiceEnvelope and dispatch text messages via pubsub."""
        from meshtastic.protobuf.mesh_pb2 import Data, MeshPacket
        from meshtastic.protobuf.mqtt_pb2 import ServiceEnvelope
        from meshtastic.protobuf.portnums_pb2 import PortNum

        NODEINFO_APP = PortNum.NODEINFO_APP
        TEXT_MESSAGE_APP = PortNum.TEXT_MESSAGE_APP

        envelope = ServiceEnvelope()
        envelope.ParseFromString(payload)

        if not envelope.HasField("packet"):
            return

        packet: MeshPacket = envelope.packet
        from_num = packet.__getattribute__("from")  # 'from' is a Python keyword
        to_num = packet.to
        packet_id = packet.id

        # Ignore our own packets echoed back by the MQTT broker
        if from_num == self._my_node_num:
            return

        # Try to get the decoded payload (unencrypted packets)
        data: Data | None = None
        if packet.HasField("decoded"):
            data = packet.decoded
        elif packet.encrypted and self._aes_key:
            data = self._decrypt_packet(packet.encrypted, from_num, packet_id)

        if data is None:
            return

        # Update SNR for any node we hear from
        rx_snr = packet.rx_snr if packet.rx_snr else None
        if rx_snr is not None:
            node_id = f"!{from_num:08x}"
            with self._lock:
                if node_id not in self.nodes:
                    self.nodes[node_id] = {}
                self.nodes[node_id]["snr"] = rx_snr

        # Track nodes from NODEINFO_APP
        if data.portnum == NODEINFO_APP:
            self._handle_nodeinfo(from_num, data.payload)

        # Dispatch text messages
        if data.portnum == TEXT_MESSAGE_APP:
            text = data.payload.decode("utf-8", errors="replace")
            from_id = f"!{from_num:08x}"

            # Build a packet dict compatible with the serial pubsub format
            fake_packet = {
                "from": from_num,
                "to": to_num,
                "fromId": from_id,
                "toId": f"!{to_num:08x}",
                "id": packet_id,
                "decoded": {
                    "portnum": "TEXT_MESSAGE_APP",
                    "payload": data.payload,
                    "text": text,
                },
                "rxSnr": packet.rx_snr if packet.rx_snr else None,
                "rxTime": packet.rx_time if packet.rx_time else None,
            }

            try:
                from pubsub import pub
                pub.sendMessage("meshtastic.receive.text", packet=fake_packet, interface=self)
            except Exception:
                if self._logger:
                    self._logger.debug("Error dispatching text via pubsub", exc_info=True)

    def _decrypt_packet(self, encrypted: bytes, from_num: int, packet_id: int) -> Any:
        """Decrypt a MeshPacket payload using AES-CTR."""
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        from meshtastic.protobuf.mesh_pb2 import Data

        # Nonce: 8 bytes packet_id (little-endian) + 8 bytes from_num (little-endian)
        nonce = packet_id.to_bytes(8, "little") + from_num.to_bytes(8, "little")

        try:
            cipher = Cipher(algorithms.AES(self._aes_key), modes.CTR(nonce))
            decryptor = cipher.decryptor()
            decrypted = decryptor.update(encrypted) + decryptor.finalize()

            data = Data()
            data.ParseFromString(decrypted)
            return data
        except Exception:
            # Decryption failures are expected for packets using different keys
            return None

    def _handle_nodeinfo(self, from_num: int, payload: bytes) -> None:
        """Update node database from NODEINFO_APP payloads."""
        from meshtastic.protobuf.mesh_pb2 import HardwareModel, User

        try:
            user = User()
            user.ParseFromString(payload)
            node_id = f"!{from_num:08x}"

            # Resolve hardware model enum to friendly name
            hw_name = None
            if user.hw_model:
                try:
                    hw_name = HardwareModel.Name(user.hw_model)
                except ValueError:
                    hw_name = str(user.hw_model)

            with self._lock:
                if node_id not in self.nodes:
                    self.nodes[node_id] = {}
                self.nodes[node_id]["user"] = {
                    "longName": user.long_name,
                    "shortName": user.short_name,
                    "hwModel": hw_name,
                    "id": node_id,
                }
                self.nodes[node_id]["lastHeard"] = int(time.time())
        except Exception:
            if self._logger:
                self._logger.debug("Error parsing NODEINFO payload", exc_info=True)

    # ── Identity management ───────────────────────────────────────────

    def _register_self_in_nodes(self) -> None:
        """Add the gateway to its own node database."""
        node_id = f"!{self._my_node_num:08x}"
        with self._lock:
            self.nodes[node_id] = {
                "user": {
                    "longName": self._long_name,
                    "shortName": self._short_name,
                    "hwModel": "PRIVATE_HW",
                    "id": node_id,
                },
                "lastHeard": int(time.time()),
                "isSelf": True,
            }

    def sendNodeInfo(self) -> None:
        """Broadcast a NODEINFO_APP packet with our identity to the mesh."""
        from meshtastic.protobuf.mesh_pb2 import Data, MeshPacket, User
        from meshtastic.protobuf.mqtt_pb2 import ServiceEnvelope
        from meshtastic.protobuf.portnums_pb2 import PortNum

        NODEINFO_APP = PortNum.NODEINFO_APP

        user = User()
        user.id = f"!{self._my_node_num:08x}"
        user.long_name = self._long_name
        user.short_name = self._short_name
        user.hw_model = 255  # PRIVATE_HW

        data = Data()
        data.portnum = NODEINFO_APP
        data.payload = user.SerializeToString()

        packet = MeshPacket()
        packet_id = self._next_packet_id
        self._next_packet_id = (self._next_packet_id + 1) & 0xFFFFFFFF

        setattr(packet, "from", self._my_node_num)
        packet.to = _MESH_BROADCAST
        packet.id = packet_id
        packet.hop_limit = 3
        packet.want_ack = False

        if self._aes_key:
            packet.encrypted = self._encrypt_data(data, self._my_node_num, packet_id)
        else:
            packet.decoded.CopyFrom(data)

        envelope = ServiceEnvelope()
        envelope.packet.CopyFrom(packet)
        envelope.channel_id = self._root_topic.split("/")[-1]
        envelope.gateway_id = f"!{self._my_node_num:08x}"

        topic = f"{self._root_topic}/!{self._my_node_num:08x}"
        self.client.publish(topic, envelope.SerializeToString(), qos=0)

        self._last_nodeinfo_time = time.time()
        if self._logger:
            self._logger.debug(
                "Sent NODEINFO: %s (%s) as !%08x",
                self._long_name,
                self._short_name,
                self._my_node_num,
            )

    def maybe_send_nodeinfo(self) -> None:
        """Send NODEINFO if enough time has elapsed since the last one."""
        now = time.time()
        if now - self._last_nodeinfo_time >= self._nodeinfo_interval:
            self.sendNodeInfo()

    # ── Message sending ─────────────────────────────────────────────

    def sendText(
        self, text: str, channelIndex: int = 0, destinationId: Any = None, **kwargs: Any
    ) -> None:
        """Publish a text message to the Meshtastic MQTT topic.

        Args:
            text: Message text (truncated to MTU).
            channelIndex: Meshtastic channel index (0-7).
            destinationId: Target node (int, or ``"!abcd1234"`` hex string).
                ``None`` sends a broadcast to all nodes.
        """
        from meshtastic.protobuf.mesh_pb2 import Data, MeshPacket
        from meshtastic.protobuf.mqtt_pb2 import ServiceEnvelope
        from meshtastic.protobuf.portnums_pb2 import PortNum

        TEXT_MESSAGE_APP = PortNum.TEXT_MESSAGE_APP

        # Build the Data payload
        data = Data()
        data.portnum = TEXT_MESSAGE_APP
        data.payload = text.encode("utf-8")[:MESHTASTIC_MTU]

        # Build the MeshPacket
        packet = MeshPacket()
        packet_id = self._next_packet_id
        self._next_packet_id = (self._next_packet_id + 1) & 0xFFFFFFFF

        # Use setattr for 'from' since it's a Python keyword
        setattr(packet, "from", self._my_node_num)

        # Set destination — broadcast or direct message
        if destinationId is not None:
            if isinstance(destinationId, str) and destinationId.startswith("!"):
                packet.to = int(destinationId[1:], 16)
            else:
                packet.to = int(destinationId)
            packet.want_ack = True
        else:
            packet.to = _MESH_BROADCAST
            packet.want_ack = False

        packet.id = packet_id
        packet.channel = channelIndex
        packet.hop_limit = 3

        if self._aes_key:
            # Encrypt the payload
            packet.encrypted = self._encrypt_data(data, self._my_node_num, packet_id)
        else:
            packet.decoded.CopyFrom(data)

        # Wrap in ServiceEnvelope
        envelope = ServiceEnvelope()
        envelope.packet.CopyFrom(packet)
        envelope.channel_id = self._root_topic.split("/")[-1]  # e.g. "LongFast"
        envelope.gateway_id = f"!{self._my_node_num:08x}"

        # Publish
        topic = f"{self._root_topic}/!{self._my_node_num:08x}"
        self.client.publish(topic, envelope.SerializeToString(), qos=0)

    def _encrypt_data(self, data: Any, from_num: int, packet_id: int) -> bytes:
        """Encrypt a Data protobuf using AES-CTR."""
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        nonce = packet_id.to_bytes(8, "little") + from_num.to_bytes(8, "little")
        plaintext = data.SerializeToString()

        cipher = Cipher(algorithms.AES(self._aes_key), modes.CTR(nonce))
        encryptor = cipher.encryptor()
        return encryptor.update(plaintext) + encryptor.finalize()


# ---------------------------------------------------------------------------
# Propagation node discovery (same pattern as message_echo.py)
# ---------------------------------------------------------------------------


class _PropagationAnnounceHandler:
    """RNS announce handler that auto-selects the nearest LXMF propagation node."""

    def __init__(self, plugin: "MeshtasticGateway"):
        self.aspect_filter = "lxmf.propagation"
        self._plugin = plugin

    def received_announce(self, destination_hash, announced_identity, app_data):
        self._plugin._handle_propagation_announce(destination_hash, announced_identity, app_data)


# ---------------------------------------------------------------------------
# Main plugin
# ---------------------------------------------------------------------------


class MeshtasticGateway(PluginBase):
    """Bridges Meshtastic text messages with LXMF over Reticulum."""

    plugin_name = "meshtastic_gateway"
    plugin_description = "Bridges Meshtastic text messages with LXMF over Reticulum"
    plugin_version = "1.2.0"

    # ── Configuration validation ────────────────────────────────────

    def validate_config(self) -> None:  # noqa: C901 — validation is inherently branchy
        # Check the meshtastic package is installed (needed for both modes)
        try:
            import meshtastic  # noqa: F401
        except ImportError:
            raise ValueError(
                "meshtastic package not found. "
                "Install with: pip install meshtastic  "
                "(or: pip install reticulumpi[meshtastic])"
            )

        # Validate mode
        mode = self.config.get("mode", MODE_SERIAL)
        if mode not in (MODE_SERIAL, MODE_MQTT):
            raise ValueError(f"mode must be '{MODE_SERIAL}' or '{MODE_MQTT}', got {mode!r}")

        if mode == MODE_SERIAL:
            serial_port = self.config.get("serial_port", "auto")
            if not isinstance(serial_port, str):
                raise ValueError("serial_port must be a string (device path or 'auto')")

        elif mode == MODE_MQTT:
            # MQTT mode requires paho-mqtt and cryptography
            try:
                import paho.mqtt.client  # noqa: F401
            except ImportError:
                raise ValueError(
                    "paho-mqtt package not found (required for MQTT mode). "
                    "Install with: pip install paho-mqtt"
                )
            try:
                from cryptography.hazmat.primitives.ciphers import (  # noqa: F401
                    Cipher,
                )
            except ImportError:
                raise ValueError(
                    "cryptography package not found (required for MQTT mode). "
                    "Install with: pip install cryptography"
                )

            mqtt_cfg = self.config.get("mqtt", {})
            if not isinstance(mqtt_cfg, dict):
                raise ValueError("mqtt config must be a dictionary")

            broker = mqtt_cfg.get("broker", _MQTT_DEFAULTS["broker"])
            if not isinstance(broker, str) or not broker:
                raise ValueError("mqtt.broker must be a non-empty string")

            port = mqtt_cfg.get("port", _MQTT_DEFAULTS["port"])
            if not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError("mqtt.port must be an integer 1-65535")

        channel = self.config.get("meshtastic_channel", 0)
        if not isinstance(channel, int) or not 0 <= channel <= 7:
            raise ValueError("meshtastic_channel must be an integer 0-7")

        hci = self.config.get("health_check_interval", 15)
        if not isinstance(hci, (int, float)) or hci < 5:
            raise ValueError("health_check_interval must be >= 5 seconds")

        rd = self.config.get("reconnect_delay", 10)
        if not isinstance(rd, (int, float)) or rd < 1:
            raise ValueError("reconnect_delay must be >= 1 second")

        mra = self.config.get("max_reconnect_attempts", 10)
        if not isinstance(mra, int) or mra < 0:
            raise ValueError("max_reconnect_attempts must be a non-negative integer")

        # Rate limiting
        mpm = self.config.get("max_messages_per_minute", 0)
        if not isinstance(mpm, (int, float)) or mpm < 0:
            raise ValueError("max_messages_per_minute must be >= 0 (0 = unlimited)")

        # Validate device_probe_port (optional, for MQTT mode device telemetry)
        dpp = self.config.get("device_probe_port", "")
        if dpp and not isinstance(dpp, str):
            raise ValueError("device_probe_port must be a string (device path)")

        dpi = self.config.get("device_probe_interval", 300)
        if not isinstance(dpi, (int, float)) or dpi < 60:
            raise ValueError("device_probe_interval must be >= 60 seconds")

        # Validate short_name (optional, max 4 chars)
        short_name = self.config.get("short_name", "")
        if short_name and (not isinstance(short_name, str) or len(short_name) > 4):
            raise ValueError("short_name must be a string of at most 4 characters")

        # Validate LXMF recipient hashes
        for h in self.config.get("lxmf_recipients", []):
            if not isinstance(h, str) or not _HEX_HASH_RE.match(h):
                raise ValueError(f"Invalid LXMF recipient hash: {h!r} (must be 32 hex chars)")

        # Validate allow lists
        for h in self.config.get("lxmf_allow_list", []):
            if not isinstance(h, str) or not _HEX_HASH_RE.match(h):
                raise ValueError(f"Invalid LXMF allow list hash: {h!r} (must be 32 hex chars)")

        for nid in self.config.get("meshtastic_allow_list", []):
            if not isinstance(nid, str) or not _MESH_NODE_ID_RE.match(nid):
                raise ValueError(
                    f"Invalid Meshtastic node ID: {nid!r} (must be !XXXXXXXX, 8 hex chars)"
                )

    # ── Lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        import LXMF

        self._lock = threading.Lock()
        self._mode = self.config.get("mode", MODE_SERIAL)

        # Stats
        self._msgs_mesh_to_lxmf = 0
        self._msgs_lxmf_to_mesh = 0
        self._msgs_hub_to_mesh = 0
        self._msgs_rate_limited = 0
        self._connect_count = 0
        self._reconnect_failures = 0
        self._last_mesh_msg_time: float | None = None
        self._last_lxmf_msg_time: float | None = None

        # Rate limiting for outbound (LXMF -> Meshtastic) direction.
        # Critical for MQTT mode where messages may be rebroadcast over LoRa.
        max_per_min = self.config.get("max_messages_per_minute", 0)
        if max_per_min > 0:
            self._send_min_interval = 60.0 / max_per_min
        else:
            self._send_min_interval = 0  # No limit
        self._last_send_time = 0.0

        # Meshtastic state
        self._mesh_interface: Any = None
        self._connected = False
        self._last_disconnect_time: float = 0.0

        # Device info probe (for dashboard device card & LoRa neighbors)
        self._cached_device_info: dict[str, Any] | None = None
        self._cached_lora_neighbors: list[dict[str, Any]] = []
        self._node_name_cache: dict[str, dict[str, str]] = {}  # {id: {longName, shortName, hwModel}}
        self._name_cache_path: str = ""  # set after storage_path is known
        self._device_info_cache_time: float = 0
        self._device_probe_port: str = self.config.get("device_probe_port", "")
        self._device_probe_interval: float = self.config.get(
            "device_probe_interval", 300
        )

        # Persistent serial listener for LoRa message reception (MQTT mode)
        self._serial_listener: Any = None

        # Packet dedup — same message can arrive via both MQTT and serial
        self._seen_packet_ids: dict[int, float] = {}  # packet_id → timestamp

        # ── LXMF setup (same pattern as message_echo.py) ───────────
        default_storage = "~/.local/share/reticulumpi/meshtastic_gw_lxmf"
        storage_path = os.path.expanduser(self.config.get("storage_path", default_storage))
        os.makedirs(storage_path, exist_ok=True)

        # Load persisted node name cache (survives restarts)
        self._name_cache_path = os.path.join(storage_path, "node_name_cache.json")
        self._load_name_cache()

        identity_path = os.path.join(storage_path, "identity")
        if os.path.isfile(identity_path):
            self._gw_identity = RNS.Identity.from_file(identity_path)
            self.log.debug("Loaded gateway identity from %s", identity_path)
        else:
            self._gw_identity = RNS.Identity()
            self._gw_identity.to_file(identity_path)
            self.log.info("Created new gateway identity at %s", identity_path)

        self.lxmf_router = LXMF.LXMRouter(storagepath=storage_path)
        display_name = self.config.get("display_name") or f"{self.app.node_name} Mesh Gateway"
        self.local_lxmf_destination = self.lxmf_router.register_delivery_identity(
            self._gw_identity,
            display_name=display_name,
        )
        self.lxmf_router.register_delivery_callback(self._handle_lxmf_message)

        # Propagation node auto-selection
        self._best_propagation_hops = RNS.Transport.PATHFINDER_M + 1
        self._propagation_handler = _PropagationAnnounceHandler(self)
        RNS.Transport.register_announce_handler(self._propagation_handler)

        # ── Meshtastic MQTT identity persistence ────────────────────
        if self._mode == MODE_MQTT:
            node_num_path = os.path.join(storage_path, "meshtastic_node_num")
            self._mqtt_node_num = _load_or_create_node_num(node_num_path, self.log)
            self._mqtt_long_name = (
                self.config.get("display_name") or f"{self.app.node_name} Mesh Gateway"
            )
            self._mqtt_short_name = (
                self.config.get("short_name") or _derive_short_name(self._mqtt_long_name)
            )
        else:
            self._mqtt_node_num = None
            self._mqtt_long_name = None
            self._mqtt_short_name = None

        # ── Parse recipient and allow lists ─────────────────────────
        self._recipient_hashes: list[bytes] = []
        for h in self.config.get("lxmf_recipients", []):
            try:
                self._recipient_hashes.append(bytes.fromhex(h))
            except ValueError:
                self.log.warning("Skipping invalid LXMF recipient hash: %s", h)

        self._lxmf_allow_set: set[str] = {
            h.lower() for h in self.config.get("lxmf_allow_list", [])
        }
        self._mesh_allow_set: set[str] = {
            nid.lower() for nid in self.config.get("meshtastic_allow_list", [])
        }

        # ── Start device connection thread ──────────────────────────
        self._active = True
        self._start_thread(self._connection_loop, "meshtastic-connect")

        # Start device probe thread (MQTT mode only — reads HW info from
        # the physical Meshtastic device via a brief serial connection).
        if self._mode == MODE_MQTT and self._device_probe_port:
            self._start_thread(self._device_probe_loop, "meshtastic-device-probe")

        self.log.info(
            "Meshtastic Gateway started (mode=%s, LXMF address: %s)",
            self._mode,
            RNS.prettyhexrep(self.local_lxmf_destination.hash),
        )

    def stop(self) -> None:
        self._active = False
        # Close persistent serial listener (if any) before joining threads
        try:
            listener = self._serial_listener
            self._serial_listener = None
            if listener is not None:
                listener.close()
        except Exception:
            pass
        try:
            RNS.Transport.deregister_announce_handler(self._propagation_handler)
        except Exception:
            pass  # Best-effort cleanup
        try:
            self.lxmf_router.register_delivery_callback(None)
        except Exception:
            pass  # Best-effort cleanup
        self._close_mesh_interface()
        self._join_threads()

    # ── Device connection management ────────────────────────────────

    def _connection_loop(self) -> None:
        """Background thread: connect to Meshtastic device/broker and monitor health.

        Reconnection strategy:
        1. When paho detects a disconnect it auto-reconnects with exponential
           backoff (1 s → 120 s).  We give it a **grace period** before tearing
           down the client and creating a new one.
        2. If the grace period expires without reconnection, we close the old
           client cleanly (preventing paho loop-thread leaks) and create a fresh
           connection with exponential backoff at the plugin layer.
        """
        reconnect_delay = self.config.get("reconnect_delay", 10)
        health_check_interval = self.config.get("health_check_interval", 30)
        max_attempts = self.config.get("max_reconnect_attempts", 10)
        # Grace period: let paho auto-reconnect before we tear down and rebuild.
        # Default 90 s covers paho's full backoff range (1 → 2 → … → 120 s).
        grace_period = self.config.get("reconnect_grace_period", 90)

        while self._active:
            if not self._connected:
                # ── Grace period: paho may be auto-reconnecting ────────
                with self._lock:
                    has_interface = self._mesh_interface is not None
                    since_disconnect = (
                        time.monotonic() - self._last_disconnect_time
                        if self._last_disconnect_time > 0
                        else float("inf")
                    )

                if has_interface and since_disconnect < grace_period:
                    remaining = grace_period - since_disconnect
                    self.log.debug(
                        "Waiting for auto-reconnect (%.0fs of %ds grace remaining)",
                        remaining,
                        grace_period,
                    )
                    self._sleep_while_active(min(health_check_interval, remaining))
                    if self._connected:
                        self.log.info("Auto-reconnect succeeded")
                        self._reconnect_failures = 0
                        continue
                    # Still disconnected — loop will re-check grace timer
                    continue

                # Grace period expired or no interface — tear down and rebuild
                if has_interface:
                    self.log.warning(
                        "Auto-reconnect failed after %ds, creating new connection",
                        grace_period,
                    )
                self._close_mesh_interface()

                try:
                    self._connect_mesh_device()
                    self._reconnect_failures = 0
                except Exception as exc:
                    self._reconnect_failures += 1
                    self.log.warning(
                        "Meshtastic connect failed (%d): %s",
                        self._reconnect_failures,
                        exc,
                    )
                    self.event_bus.publish(events.MESHTASTIC_CONNECT_FAILED, {
                        "error": str(exc),
                        "attempt": self._reconnect_failures,
                    })
                    if max_attempts > 0 and self._reconnect_failures >= max_attempts:
                        self.log.error(
                            "Max reconnect attempts (%d) reached, giving up", max_attempts
                        )
                        self._active = False
                        break
                    # Exponential backoff: 10 → 20 → 40 → 80 → 160 → cap 300 s
                    backoff = min(
                        reconnect_delay * (2 ** min(self._reconnect_failures - 1, 5)),
                        300,
                    )
                    self.log.debug("Reconnect backoff: %ds", backoff)
                    self._sleep_while_active(backoff)
                    continue

            # Health check
            self._sleep_while_active(health_check_interval)
            if self._connected and not self._check_mesh_health():
                self.log.warning("Meshtastic health check failed, reconnecting")
                self._close_mesh_interface()
                self.event_bus.publish(events.MESHTASTIC_DISCONNECTED, {
                    "reason": "health_check_failed",
                })
                continue

            # Periodic NODEINFO re-announcement (MQTT mode only)
            if self._connected and self._mode == MODE_MQTT:
                try:
                    with self._lock:
                        iface = self._mesh_interface
                    if iface and hasattr(iface, "maybe_send_nodeinfo"):
                        iface.maybe_send_nodeinfo()
                except Exception:
                    self.log.debug("Error sending periodic NODEINFO", exc_info=True)

    def _connect_mesh_device(self) -> None:
        """Open connection to the Meshtastic network (serial or MQTT)."""
        from pubsub import pub

        if self._mode == MODE_MQTT:
            iface = self._create_mqtt_interface()
        else:
            iface = self._create_serial_interface()

        # Assign interface before subscribing so callbacks can see it immediately
        with self._lock:
            self._mesh_interface = iface
            self._connected = True
            self._connect_count += 1

        # Register pubsub callbacks (after interface is assigned)
        pub.subscribe(self._on_mesh_text, "meshtastic.receive.text")
        pub.subscribe(self._on_mesh_connect, "meshtastic.connection.established")
        pub.subscribe(self._on_mesh_disconnect, "meshtastic.connection.lost")

        # Identify ourselves
        node_id = self._get_own_node_id(iface)
        conn_detail = self._get_connection_detail()

        self.log.info(
            "Meshtastic connected (mode=%s, node=%s, %s)",
            self._mode,
            node_id,
            conn_detail,
        )
        self.event_bus.publish(events.MESHTASTIC_CONNECTED, {
            "mode": self._mode,
            "node_id": node_id,
            "detail": conn_detail,
        })

    def _create_serial_interface(self) -> Any:
        """Create a Meshtastic SerialInterface (USB device)."""
        import meshtastic.serial_interface

        serial_port = self.config.get("serial_port", "auto")
        self.log.info("Connecting to Meshtastic device (port=%s)...", serial_port)

        if serial_port == "auto":
            return meshtastic.serial_interface.SerialInterface()
        return meshtastic.serial_interface.SerialInterface(devPath=serial_port)

    def _create_mqtt_interface(self) -> Any:
        """Create a native Meshtastic MQTT client (no hardware required).

        Uses paho-mqtt + meshtastic protobufs + AES-CTR encryption to talk
        directly to a Meshtastic MQTT broker.  Provides the same pubsub
        callbacks and ``sendText()`` API as SerialInterface.

        .. warning::
           Messages published to MQTT may be rebroadcast over LoRa by any
           Meshtastic node with uplink enabled.  Use a private channel and
           keep the rate limit low.
        """
        mqtt_cfg = self.config.get("mqtt", {})
        broker = mqtt_cfg.get("broker", _MQTT_DEFAULTS["broker"])
        port = mqtt_cfg.get("port", _MQTT_DEFAULTS["port"])
        username = mqtt_cfg.get("username", _MQTT_DEFAULTS["username"])
        password = mqtt_cfg.get("password", _MQTT_DEFAULTS["password"])
        root_topic = mqtt_cfg.get("root_topic", _MQTT_DEFAULTS["root_topic"])
        channel_key = mqtt_cfg.get("channel_key", _MQTT_DEFAULTS["channel_key"])
        channel = self.config.get("meshtastic_channel", 0)

        self.log.info(
            "Connecting to Meshtastic MQTT (broker=%s:%d, topic=%s)...",
            broker,
            port,
            root_topic,
        )

        return _MeshtasticMQTTClient(
            broker=broker,
            port=port,
            username=username,
            password=password,
            root_topic=root_topic,
            ch_index=channel,
            ch_key_b64=channel_key,
            logger=self.log,
            node_num=self._mqtt_node_num,
            long_name=self._mqtt_long_name or "",
            short_name=self._mqtt_short_name or "",
        )

    def _get_own_node_id(self, iface: Any) -> str:
        """Extract our Meshtastic node ID from the interface."""
        try:
            my_info = getattr(iface, "myInfo", None)
            if my_info:
                return f"!{my_info.my_node_num:08x}"
        except Exception:
            if self.log:
                self.log.debug("Error reading own node ID from interface", exc_info=True)
        return "unknown"

    def _get_connection_detail(self) -> str:
        """Return human-readable connection detail for logging."""
        if self._mode == MODE_MQTT:
            mqtt_cfg = self.config.get("mqtt", {})
            broker = mqtt_cfg.get("broker", _MQTT_DEFAULTS["broker"])
            topic = mqtt_cfg.get("root_topic", _MQTT_DEFAULTS["root_topic"])
            return f"broker={broker}, topic={topic}"
        return f"port={self.config.get('serial_port', 'auto')}"

    def _close_mesh_interface(self) -> None:
        """Tear down the Meshtastic connection and unsubscribe from pubsub."""
        with self._lock:
            if self._mesh_interface is None:
                return
            try:
                from pubsub import pub

                pub.unsubscribe(self._on_mesh_text, "meshtastic.receive.text")
                pub.unsubscribe(self._on_mesh_connect, "meshtastic.connection.established")
                pub.unsubscribe(self._on_mesh_disconnect, "meshtastic.connection.lost")
            except Exception:
                self.log.debug("Error unsubscribing from Meshtastic pubsub", exc_info=True)
            try:
                self._mesh_interface.close()
            except Exception:
                self.log.debug("Error closing Meshtastic interface", exc_info=True)
            self._mesh_interface = None
            self._connected = False
            # Invalidate serial-mode caches on disconnect
            if self._mode == MODE_SERIAL:
                self._cached_device_info = None
                self._cached_lora_neighbors = []

    # ── Device info probe (for dashboard device card) ────────────

    def _device_probe_loop(self) -> None:
        """Background thread: probe the physical device and listen for LoRa messages.

        Only runs in MQTT mode when ``device_probe_port`` is configured.
        Opens a SerialInterface, reads hardware/radio/telemetry data, then
        **keeps the connection open** between probes so the meshtastic library
        can receive local LoRa text messages via pubsub.  The existing
        ``_on_mesh_text`` callback fires for both MQTT and serial sources;
        packet-level dedup prevents double-processing.
        """
        import meshtastic.serial_interface

        # Small initial delay so the gateway MQTT connection settles first
        self._sleep_while_active(10)

        while self._active:
            iface = None
            try:
                iface = meshtastic.serial_interface.SerialInterface(
                    devPath=self._device_probe_port
                )

                # ── Read device info (same as before) ────────────────
                info = self._read_device_info_from_interface(iface)
                neighbors = self._extract_lora_neighbors(iface)
                self._request_missing_nodeinfo(iface, neighbors)

                if info:
                    with self._lock:
                        self._cached_device_info = info
                        self._device_info_cache_time = time.monotonic()
                        self._cached_lora_neighbors = neighbors
                    self._save_name_cache()
                    self.log.debug(
                        "Device probe OK: %s %s (%s), %d LoRa neighbors",
                        info.get("hw_model"),
                        info.get("firmware_version"),
                        info.get("node_id"),
                        len(neighbors),
                    )

                # ── Keep serial open to receive local LoRa messages ──
                with self._lock:
                    self._serial_listener = iface
                self.log.info(
                    "Serial listener active on %s — receiving LoRa messages",
                    self._device_probe_port,
                )

                # Sleep until next probe.  The meshtastic library's
                # internal reader thread delivers packets via pubsub
                # while we wait — _on_mesh_text handles them.
                self._sleep_while_active(self._device_probe_interval)

            except Exception:
                self.log.debug("Device probe / serial listener failed", exc_info=True)
                # Wait before retrying on failure
                self._sleep_while_active(self._device_probe_interval)
            finally:
                # Close serial so next iteration can reopen cleanly
                with self._lock:
                    self._serial_listener = None
                if iface is not None:
                    try:
                        iface.close()
                    except Exception:
                        pass

    def _request_missing_nodeinfo(
        self, iface: Any, neighbors: list[dict[str, Any]],
    ) -> None:
        """Send NodeInfo requests for LoRa neighbors with generic names.

        Fire-and-forget: the device receives the response over LoRa and
        stores it in its NodeDB.  The next probe will pick up the name.

        Rate-limited to at most 3 requests per probe to conserve airtime.
        Only targets nodes heard within the last 2 hours with generic names.
        """
        from meshtastic.protobuf.portnums_pb2 import PortNum

        now = time.time()
        cutoff = now - 7200  # 2 hours
        requests_sent = 0
        max_requests = 3

        for n in neighbors:
            if requests_sent >= max_requests:
                break
            # Only request for generic-named, recently-heard nodes
            name = n.get("long_name") or ""
            last_heard = n.get("last_heard") or 0
            if not (name.startswith("Meshtastic ") and len(name) <= 20):
                continue
            if last_heard < cutoff:
                continue
            node_id = n.get("id", "")
            if not node_id:
                continue
            try:
                iface.sendData(
                    bytes(),
                    destinationId=node_id,
                    portNum=PortNum.NODEINFO_APP,
                    wantAck=False,
                    wantResponse=True,
                )
                requests_sent += 1
                self.log.debug(
                    "Requested NodeInfo from %s (generic name)", node_id,
                )
            except Exception:
                self.log.debug(
                    "Failed to request NodeInfo from %s", node_id,
                    exc_info=True,
                )

    @staticmethod
    def _read_device_info_from_interface(iface: Any) -> dict[str, Any]:
        """Extract hardware, radio, and telemetry info from a Meshtastic interface.

        Works with both ``SerialInterface`` (full data) and the custom
        ``_MeshtasticMQTTClient`` (limited data).
        """
        from meshtastic import mesh_pb2, config_pb2

        info: dict[str, Any] = {"available": True}

        # ── Node identity ────────────────────────────────────────
        my_info = getattr(iface, "myInfo", None)
        if my_info:
            try:
                node_num = my_info.my_node_num
                info["node_id"] = f"!{node_num:08x}"
            except Exception:
                info["node_id"] = None
        else:
            info["node_id"] = None

        # ── Metadata (firmware, role, hw_model) ──────────────────
        metadata = getattr(iface, "metadata", None)
        if metadata:
            info["firmware_version"] = getattr(metadata, "firmware_version", None)
            # Hardware model enum
            hw_val = getattr(metadata, "hwModel", None) or getattr(
                metadata, "hw_model", None
            )
            if hw_val is not None:
                try:
                    info["hw_model"] = mesh_pb2.HardwareModel.Name(hw_val)
                except (ValueError, AttributeError):
                    info["hw_model"] = str(hw_val)
            else:
                info["hw_model"] = None
            # Role from metadata
            role_val = getattr(metadata, "role", None)
            if role_val is not None:
                try:
                    info["role"] = config_pb2.Config.DeviceConfig.Role.Name(role_val)
                except (ValueError, AttributeError):
                    info["role"] = str(role_val)
            else:
                info["role"] = None
        else:
            info["firmware_version"] = None
            info["hw_model"] = None
            info["role"] = None

        # ── Local config (LoRa settings) ─────────────────────────
        local_node = getattr(iface, "localNode", None)
        local_config = getattr(local_node, "localConfig", None) if local_node else None

        if local_config:
            lora = getattr(local_config, "lora", None)
            if lora:
                # Region
                region_val = getattr(lora, "region", None)
                if region_val is not None:
                    try:
                        info["region"] = config_pb2.Config.LoRaConfig.RegionCode.Name(
                            region_val
                        )
                    except (ValueError, AttributeError):
                        info["region"] = str(region_val)
                else:
                    info["region"] = None
                # Modem preset
                preset_val = getattr(lora, "modem_preset", None)
                if preset_val is not None:
                    try:
                        info["modem_preset"] = (
                            config_pb2.Config.LoRaConfig.ModemPreset.Name(preset_val)
                        )
                    except (ValueError, AttributeError):
                        info["modem_preset"] = str(preset_val)
                else:
                    info["modem_preset"] = None
                info["hop_limit"] = getattr(lora, "hop_limit", None)
                info["tx_power"] = getattr(lora, "tx_power", None)
                info["tx_enabled"] = getattr(lora, "tx_enabled", None)
            else:
                for k in ("region", "modem_preset", "hop_limit", "tx_power", "tx_enabled"):
                    info[k] = None

            device_cfg = getattr(local_config, "device", None)
            if device_cfg:
                info["node_info_broadcast_secs"] = getattr(
                    device_cfg, "node_info_broadcast_secs", None
                )
                # Fallback role from device config
                if info.get("role") is None:
                    role_val = getattr(device_cfg, "role", None)
                    if role_val is not None:
                        try:
                            info["role"] = config_pb2.Config.DeviceConfig.Role.Name(
                                role_val
                            )
                        except (ValueError, AttributeError):
                            info["role"] = str(role_val)
            else:
                info.setdefault("node_info_broadcast_secs", None)
        else:
            for k in (
                "region", "modem_preset", "hop_limit", "tx_power",
                "tx_enabled", "node_info_broadcast_secs",
            ):
                info[k] = None

        # ── Self-node user info (long/short name) ────────────────
        info["long_name"] = None
        info["short_name"] = None
        node_num = None
        if my_info:
            try:
                node_num = my_info.my_node_num
            except Exception:
                pass
        nodes_by_num = getattr(iface, "nodesByNum", None) or {}
        self_node = nodes_by_num.get(node_num, {}) if node_num else {}

        user = self_node.get("user", {})
        if user:
            info["long_name"] = user.get("longName")
            info["short_name"] = user.get("shortName")

        # ── Device telemetry (battery, utilization) ──────────────
        device_metrics = self_node.get("deviceMetrics", {})
        info["battery_level"] = device_metrics.get("batteryLevel")
        info["voltage"] = device_metrics.get("voltage")
        info["channel_utilization"] = device_metrics.get("channelUtilization")
        info["air_util_tx"] = device_metrics.get("airUtilTx")

        return info

    def _load_name_cache(self) -> None:
        """Load the persisted node name cache from disk."""
        if not self._name_cache_path:
            return
        try:
            with open(self._name_cache_path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._node_name_cache = data
                self.log.debug(
                    "Loaded %d entries from node name cache", len(data),
                )
        except FileNotFoundError:
            pass
        except Exception:
            self.log.debug("Error loading node name cache", exc_info=True)

    def _save_name_cache(self) -> None:
        """Persist the node name cache to disk."""
        if not self._name_cache_path:
            return
        try:
            with open(self._name_cache_path, "w") as f:
                json.dump(self._node_name_cache, f)
        except Exception:
            self.log.debug("Error saving node name cache", exc_info=True)

    def _extract_lora_neighbors(
        self, iface: Any,
    ) -> list[dict[str, Any]]:
        """Extract LoRa-only neighbors from a Meshtastic interface's NodeDB.

        Filters to nodes where ``via_mqtt`` is False/absent, excluding the
        self node.  Returns a list of dicts suitable for the dashboard API.

        Names are enriched from ``_node_name_cache`` which accumulates across
        probes, since NodeInfo packets arrive infrequently (default 3 h).
        """
        raw_nodes = getattr(iface, "nodes", None) or {}
        # Determine own node number to filter self even if isSelf is absent
        my_node_num: int | None = None
        my_info = getattr(iface, "myInfo", None)
        if my_info:
            try:
                my_node_num = my_info.my_node_num
            except Exception:
                pass

        # First pass: update the name cache from ALL nodes in the NodeDB
        # (including MQTT nodes — their names help enrich LoRa neighbors)
        for node_id, node_data in raw_nodes.items():
            user = node_data.get("user", {})
            long_name = user.get("longName") or ""
            short_name = user.get("shortName") or ""
            hw_model = user.get("hwModel") or ""
            # Cache if we have any useful user data (skip bare IDs)
            if long_name and long_name != node_id:
                self._node_name_cache[node_id] = {
                    "longName": long_name,
                    "shortName": short_name,
                    "hwModel": hw_model,
                }
            elif short_name and node_id not in self._node_name_cache:
                self._node_name_cache[node_id] = {
                    "longName": long_name,
                    "shortName": short_name,
                    "hwModel": hw_model,
                }

        # Second pass: build the filtered LoRa-only neighbor list
        neighbors: list[dict[str, Any]] = []
        for node_id, node_data in raw_nodes.items():
            # Skip self node (check isSelf flag and own node number)
            if node_data.get("isSelf"):
                continue
            if my_node_num is not None and node_data.get("num") == my_node_num:
                continue
            # Skip MQTT-relayed nodes — we only want LoRa-heard
            # Check both snake_case (protobuf) and camelCase (library) variants
            if node_data.get("via_mqtt") or node_data.get("viaMqtt"):
                continue
            user = node_data.get("user", {})
            cached = self._node_name_cache.get(node_id, {})
            position = node_data.get("position", {})

            # Prefer cached names over generic auto-names.
            # The serial NodeDB auto-generates "Meshtastic XXXX" as a
            # placeholder until a NodeInfo packet arrives (every 3 h).
            # The name cache may already have a real name from MQTT.
            local_long = user.get("longName") or ""
            cached_long = cached.get("longName") or ""
            local_short = user.get("shortName") or ""
            cached_short = cached.get("shortName") or ""
            local_hw = user.get("hwModel") or ""
            cached_hw = cached.get("hwModel") or ""

            is_generic = (
                local_long.startswith("Meshtastic ")
                and len(local_long) <= 20
            )
            long_name = (
                (cached_long if is_generic and cached_long
                 and not cached_long.startswith("Meshtastic ")
                 else local_long) or None
            )
            short_name = (
                (cached_short if is_generic and cached_short
                 else local_short) or None
            )
            hw_model = (
                (cached_hw if (not local_hw or local_hw == "UNSET")
                 and cached_hw and cached_hw != "UNSET"
                 else local_hw) or None
            )

            entry: dict[str, Any] = {
                "id": node_id,
                "long_name": long_name,
                "short_name": short_name,
                "hw_model": hw_model,
                "hops_away": node_data.get("hops_away", node_data.get("hopsAway")),
                "snr": node_data.get("snr"),
                "last_heard": node_data.get("lastHeard"),
                "latitude": position.get("latitude"),
                "longitude": position.get("longitude"),
            }
            neighbors.append(entry)
        return neighbors

    def get_device_info(self) -> dict[str, Any]:
        """Return Meshtastic device hardware and radio info for the dashboard.

        In SERIAL mode, reads directly from the live interface.
        In MQTT mode, returns cached data from the background device probe.
        """
        with self._lock:
            mode = self._mode
            connected = self._connected
            iface = self._mesh_interface

        result: dict[str, Any] = {"mode": mode, "connected": connected}

        if mode == MODE_SERIAL and connected and iface is not None:
            # Serial mode — read live from the connected interface
            try:
                info = self._read_device_info_from_interface(iface)
                info["mode"] = mode
                info["connected"] = connected
                return info
            except Exception:
                if self.log:
                    self.log.debug("Error reading device info from serial interface",
                                  exc_info=True)
                result["available"] = False
                result["message"] = "Error reading device info"
                return result

        # MQTT mode — return cached probe data
        with self._lock:
            cached = self._cached_device_info

        if cached:
            cached_copy = dict(cached)
            cached_copy["mode"] = mode
            cached_copy["connected"] = connected
            return cached_copy

        # No cache yet
        if self._device_probe_port:
            result["available"] = False
            result["message"] = "Device probe pending — first reading in a few seconds"
        else:
            result["available"] = False
            result["message"] = (
                "No device_probe_port configured — "
                "set device_probe_port in meshtastic_gateway config "
                "to enable hardware telemetry"
            )
            # Return what we know from MQTT identity
            if self._mqtt_node_num:
                result["node_id"] = f"!{self._mqtt_node_num:08x}"
                result["long_name"] = self._mqtt_long_name
                result["short_name"] = self._mqtt_short_name
        return result

    def _check_mesh_health(self) -> bool:
        """Return True if the Meshtastic connection appears healthy."""
        with self._lock:
            if self._mesh_interface is None:
                return False
            try:
                if self._mode == MODE_MQTT:
                    # For MQTT, check the underlying paho client
                    client = getattr(self._mesh_interface, "client", None)
                    if client and hasattr(client, "is_connected"):
                        return client.is_connected()
                    # Fallback: interface exists and is not None
                    return True
                else:
                    # For serial, check the serial stream
                    stream = getattr(self._mesh_interface, "stream", None)
                    if stream and hasattr(stream, "is_open"):
                        return stream.is_open
                    return self._mesh_interface is not None
            except Exception:
                if self.log:
                    self.log.debug("Error during mesh health check", exc_info=True)
                return False

    # ── Rate limiting ───────────────────────────────────────────────

    def _check_send_rate_limit(self) -> bool:
        """Check if we're allowed to send a message to Meshtastic.

        Returns True if the message should be allowed, False if rate-limited.
        Especially important in MQTT mode where outbound messages may be
        rebroadcast over LoRa by uplinked nodes.
        """
        if self._send_min_interval <= 0:
            return True
        now = time.time()
        with self._lock:
            if now - self._last_send_time >= self._send_min_interval:
                self._last_send_time = now
                return True
            self._msgs_rate_limited += 1
            return False

    # ── Meshtastic pubsub callbacks ─────────────────────────────────

    def _on_mesh_text(self, packet: dict, interface: Any = None) -> None:
        """Handle incoming Meshtastic text message (pubsub callback).

        Fires for messages from both the MQTT client and the persistent
        serial listener.  Packet-level dedup ensures a message arriving
        via both sources is only processed once.
        """
        with self._lock:
            if not self._active:
                return
            try:
                # ── Packet dedup ─────────────────────────────────────
                packet_id = packet.get("id", 0)
                now = time.time()
                if packet_id:
                    if packet_id in self._seen_packet_ids:
                        return  # Already processed via the other source
                    self._seen_packet_ids[packet_id] = now
                    # Prune old entries every 64 packets
                    if len(self._seen_packet_ids) > 512:
                        cutoff = now - 120
                        self._seen_packet_ids = {
                            k: v for k, v in self._seen_packet_ids.items()
                            if v > cutoff
                        }

                from_id = packet.get("fromId", "?")
                from_num = packet.get("from", 0)
                to_num = packet.get("to", 0)
                to_id = packet.get("toId", "")
                is_broadcast = (to_num == _MESH_BROADCAST)

                decoded = packet.get("decoded", {})
                payload = decoded.get("payload", b"")
                text = (
                    payload.decode("utf-8", errors="replace")
                    if isinstance(payload, bytes)
                    else str(payload)
                )

                if not text.strip():
                    return

                # Filter by allow list
                if self._mesh_allow_set and from_id.lower() not in self._mesh_allow_set:
                    self.log.debug("Ignoring Meshtastic msg from %s (not in allow list)", from_id)
                    return

                # Resolve node name (checks MQTT nodes, serial nodes, name cache)
                node_name = self._resolve_mesh_node_name(from_num)
                sender_label = f"{node_name} ({from_id})" if node_name else from_id

                # Identify source for logging
                is_serial = (interface is not None
                             and interface is self._serial_listener)
                source_tag = "LoRa" if is_serial else "MQTT"

                # Build formatted message
                prefix = self.config.get("meshtastic_prefix", DEFAULT_MESH_PREFIX)
                formatted = f"{prefix} {sender_label}:\n{text}"

                self.log.info(
                    "Meshtastic msg [%s] from %s: %s",
                    source_tag, sender_label, text[:80],
                )

                self._msgs_mesh_to_lxmf += 1
                self._last_mesh_msg_time = time.time()

            except Exception:
                self.log.exception("Error parsing Meshtastic text message")
                return

        # Forward outside the lock to avoid holding it during LXMF send
        try:
            self._forward_to_lxmf(formatted)
        except Exception:
            self.log.exception("Error forwarding Meshtastic message to LXMF")

        self.event_bus.publish(events.MESHTASTIC_MESSAGE_RECEIVED, {
            "from_id": from_id,
            "from_name": node_name,
            "to_id": to_id,
            "is_broadcast": is_broadcast,
            "text": text[:100],
            "forwarded_to": len(self._recipient_hashes),
            "source": source_tag,  # "LoRa" or "MQTT"
        })

    def _on_mesh_connect(self, interface: Any = None, topic: Any = None) -> None:
        """Pubsub callback when Meshtastic connection is (re-)established.

        Paho-mqtt auto-reconnects with exponential backoff.  When it succeeds,
        this callback fires — set ``_connected = True`` so the connection loop
        knows the link is healthy again and does NOT tear everything down.

        Ignores connect events from the serial listener — those track the
        LoRa message receiver, not the primary MQTT connection.
        """
        # Ignore serial listener connection events — they must not affect
        # the MQTT connection state that the dashboard reports.
        if interface is not None and interface is self._serial_listener:
            self.log.debug("Serial listener connected (ignored for MQTT state)")
            return

        was_disconnected = False
        with self._lock:
            if not self._connected:
                was_disconnected = True
                self._connected = True
        if was_disconnected:
            self.log.info("Meshtastic connection re-established (auto-reconnect)")
            self.event_bus.publish(events.MESHTASTIC_CONNECTED, {
                "mode": self._mode,
                "detail": "auto-reconnect",
            })
        else:
            self.log.debug("Meshtastic connection.established event")

    def _on_mesh_disconnect(self, interface: Any = None, topic: Any = None) -> None:
        """Pubsub callback when Meshtastic connection is lost.

        Ignores disconnect events from the serial listener.
        """
        # Ignore serial listener disconnects
        if interface is not None and interface is self._serial_listener:
            self.log.debug("Serial listener disconnected (ignored for MQTT state)")
            return

        self.log.warning("Meshtastic connection lost")
        with self._lock:
            self._connected = False
            self._last_disconnect_time = time.monotonic()
        self.event_bus.publish(events.MESHTASTIC_DISCONNECTED, {"reason": "connection_lost"})

    # ── LXMF delivery callback ──────────────────────────────────────

    def _handle_lxmf_message(self, message: Any) -> None:
        """Handle incoming LXMF message and forward to Meshtastic."""
        # Rate limit check BEFORE acquiring main lock (avoids nesting)
        if not self._check_send_rate_limit():
            self.log.info("LXMF->Meshtastic message rate-limited, dropping")
            return

        with self._lock:
            if not self._active or not self._connected or self._mesh_interface is None:
                return
            try:
                sender_hash = RNS.prettyhexrep(message.source_hash)
                content = message.content_as_string()
                sender_hex = message.source_hash.hex()

                # Filter by allow list
                if self._lxmf_allow_set and sender_hex.lower() not in self._lxmf_allow_set:
                    self.log.debug("Ignoring LXMF msg from %s (not in allow list)", sender_hash)
                    return

                # Build formatted message with MTU handling
                prefix = self.config.get("lxmf_prefix", DEFAULT_LXMF_PREFIX)
                header = f"{prefix} {sender_hash}:\n"
                formatted = _truncate_for_mtu(header, content, MESHTASTIC_MTU)
                channel = self.config.get("meshtastic_channel", 0)

                # Capture interface reference — sendText runs outside lock
                iface = self._mesh_interface

            except Exception:
                self.log.exception("Error processing LXMF message for Meshtastic")
                return

        # Send outside the lock to avoid blocking during I/O
        try:
            iface.sendText(formatted, channelIndex=channel)
        except Exception:
            self.log.exception("Error sending text to Meshtastic")
            return

        with self._lock:
            self._msgs_lxmf_to_mesh += 1
            self._last_lxmf_msg_time = time.time()

        self.log.info("Forwarded LXMF msg from %s to Meshtastic ch%d", sender_hash, channel)
        self.event_bus.publish(events.MESHTASTIC_MESSAGE_SENT, {
            "from_lxmf": sender_hash,
            "text": content[:100],
            "channel": channel,
        })

    # ── Helpers ──────────────────────────────────────────────────────

    def _resolve_mesh_node_name(self, node_num: int) -> str | None:
        """Look up the long name for a Meshtastic node by its node number.

        Checks three sources in order:
        1. MQTT interface node list
        2. Serial listener node list (LoRa-heard nodes)
        3. Persistent name cache
        Prefers a real name over generic "Meshtastic XXXX" auto-names.
        """
        node_id = f"!{node_num:08x}"
        best_name: str | None = None

        # 1. MQTT interface nodes
        if self._mesh_interface and hasattr(self._mesh_interface, "nodes"):
            node_info = (getattr(self._mesh_interface, "nodes", None) or {}).get(node_id)
            if node_info:
                user = node_info.get("user", {})
                name = user.get("longName") or user.get("shortName")
                if name:
                    best_name = name

        # 2. Serial listener nodes (may have names MQTT doesn't)
        listener = self._serial_listener
        if listener is not None and hasattr(listener, "nodes"):
            node_info = (getattr(listener, "nodes", None) or {}).get(node_id)
            if node_info:
                user = node_info.get("user", {})
                name = user.get("longName") or user.get("shortName")
                if name:
                    # Prefer non-generic name from either source
                    if best_name and best_name.startswith("Meshtastic ") and not name.startswith("Meshtastic "):
                        best_name = name
                    elif not best_name:
                        best_name = name

        # 3. Persistent name cache (accumulated over time)
        if (not best_name or (best_name.startswith("Meshtastic ") and len(best_name) <= 20)):
            cached = self._node_name_cache.get(node_id, {})
            cached_name = cached.get("longName") or cached.get("shortName")
            if cached_name and not cached_name.startswith("Meshtastic "):
                best_name = cached_name

        return best_name

    def _forward_to_lxmf(self, text: str) -> None:
        """Send formatted text to each configured LXMF recipient."""
        import LXMF

        if not self._recipient_hashes:
            self.log.debug("No LXMF recipients configured, Meshtastic message not forwarded")
            return

        for recipient_hash in self._recipient_hashes:
            try:
                dest_identity = RNS.Identity.recall(recipient_hash)
                if dest_identity is None:
                    RNS.Transport.request_path(recipient_hash)
                    self.log.debug(
                        "Path requested for %s, message deferred",
                        RNS.prettyhexrep(recipient_hash),
                    )
                    continue

                dest = RNS.Destination(
                    dest_identity,
                    RNS.Destination.OUT,
                    RNS.Destination.SINGLE,
                    "lxmf",
                    "delivery",
                )
                msg = LXMF.LXMessage(
                    dest,
                    self.local_lxmf_destination,
                    text,
                    desired_method=LXMF.LXMessage.OPPORTUNISTIC,
                )
                self.lxmf_router.handle_outbound(msg)
                self.log.debug("Forwarded to LXMF %s", RNS.prettyhexrep(recipient_hash))
            except Exception:
                self.log.exception(
                    "Failed to forward to LXMF recipient %s",
                    RNS.prettyhexrep(recipient_hash),
                )

    def _handle_propagation_announce(
        self, destination_hash: bytes, announced_identity: Any, app_data: bytes
    ) -> None:
        """Auto-select the nearest active propagation node."""
        try:
            if not app_data:
                return

            from LXMF import pn_announce_data_is_valid

            if not pn_announce_data_is_valid(app_data):
                return

            data = umsgpack.unpackb(app_data)
            if not (len(data) >= 3 and data[2] is True):
                return

            hops = RNS.Transport.hops_to(destination_hash)
            with self._lock:
                if hops < self._best_propagation_hops:
                    self._best_propagation_hops = hops
                    self.lxmf_router.set_outbound_propagation_node(destination_hash)
                    self.log.info(
                        "Auto-selected propagation node %s (%d hops)",
                        RNS.prettyhexrep(destination_hash),
                        hops,
                    )
        except Exception:
            self.log.exception("Error handling propagation node announce")

    # ── Public query methods (for dashboard / info bot) ─────────────

    def get_status(self) -> dict[str, Any]:
        """Return current gateway status for monitoring and API."""
        with self._lock:
            # During auto-reconnect grace period, report as connected so
            # the dashboard doesn't flash "(off)" on brief MQTT drops.
            connected = self._connected
            if (
                not connected
                and self._active
                and self._mesh_interface is not None
                and self._last_disconnect_time > 0
            ):
                grace = self.config.get("reconnect_grace_period", 90)
                elapsed = time.monotonic() - self._last_disconnect_time
                if elapsed < grace:
                    connected = True

            status: dict[str, Any] = {
                "active": self._active,
                "mode": self._mode,
                "connected": connected,
                "meshtastic_channel": self.config.get("meshtastic_channel", 0),
                "msgs_mesh_to_lxmf": self._msgs_mesh_to_lxmf,
                "msgs_lxmf_to_mesh": self._msgs_lxmf_to_mesh,
                "msgs_hub_to_mesh": self._msgs_hub_to_mesh,
                "msgs_rate_limited": self._msgs_rate_limited,
                "connect_count": self._connect_count,
                "reconnect_failures": self._reconnect_failures,
                "last_mesh_msg_time": self._last_mesh_msg_time,
                "last_lxmf_msg_time": self._last_lxmf_msg_time,
                "lxmf_recipients": len(self._recipient_hashes),
            }
            # Mode-specific connection details
            if self._mode == MODE_SERIAL:
                status["serial_port"] = self.config.get("serial_port", "auto")
            else:
                mqtt_cfg = self.config.get("mqtt", {})
                status["mqtt_broker"] = mqtt_cfg.get("broker", _MQTT_DEFAULTS["broker"])
                status["mqtt_topic"] = mqtt_cfg.get(
                    "root_topic", _MQTT_DEFAULTS["root_topic"]
                )
                if self._mqtt_node_num:
                    status["node_id"] = f"!{self._mqtt_node_num:08x}"
                    status["long_name"] = self._mqtt_long_name
                    status["short_name"] = self._mqtt_short_name

            # Rate limit info
            if self._send_min_interval > 0:
                status["rate_limit_per_min"] = round(60.0 / self._send_min_interval, 1)

            if self._connected and self._mesh_interface:
                try:
                    nodes = getattr(self._mesh_interface, "nodes", None) or {}
                    status["meshtastic_nodes"] = len(nodes)
                except Exception:
                    if self.log:
                        self.log.debug("Error reading node count for status", exc_info=True)
            return status

    def get_meshtastic_nodes(self) -> list[dict[str, Any]]:
        """Return list of known Meshtastic mesh nodes.

        Merges nodes from the MQTT interface and the serial listener so
        contacts from both sources appear in the Messages panel.
        """
        with self._lock:
            seen: dict[str, dict[str, Any]] = {}

            # 1. MQTT interface nodes
            if self._connected and self._mesh_interface:
                raw_nodes = getattr(self._mesh_interface, "nodes", None) or {}
                for node_id, node_data in raw_nodes.items():
                    user = node_data.get("user", {})
                    position = node_data.get("position", {})
                    long_name = user.get("longName") or ""
                    short_name = user.get("shortName") or ""
                    hw_model = user.get("hwModel") or ""
                    if long_name and long_name != node_id:
                        self._node_name_cache[node_id] = {
                            "longName": long_name,
                            "shortName": short_name,
                            "hwModel": hw_model,
                        }
                    entry: dict[str, Any] = {
                        "id": node_id,
                        "long_name": long_name or None,
                        "short_name": short_name or None,
                        "hw_model": hw_model or None,
                        "snr": node_data.get("snr"),
                        "last_heard": node_data.get("lastHeard"),
                        "latitude": position.get("latitude"),
                        "longitude": position.get("longitude"),
                    }
                    if node_data.get("isSelf"):
                        entry["is_self"] = True
                    seen[node_id] = entry

            # 2. Serial listener nodes (LoRa-heard, may not be on MQTT)
            listener = self._serial_listener
            if listener is not None:
                serial_nodes = getattr(listener, "nodes", None) or {}
                for node_id, node_data in serial_nodes.items():
                    user = node_data.get("user", {})
                    position = node_data.get("position", {})
                    long_name = user.get("longName") or ""
                    short_name = user.get("shortName") or ""
                    hw_model = user.get("hwModel") or ""
                    if long_name and long_name != node_id:
                        self._node_name_cache[node_id] = {
                            "longName": long_name,
                            "shortName": short_name,
                            "hwModel": hw_model,
                        }
                    if node_id in seen:
                        # Prefer non-generic name from serial over MQTT
                        existing = seen[node_id]
                        ex_name = existing.get("long_name") or ""
                        if (ex_name.startswith("Meshtastic ")
                                and long_name
                                and not long_name.startswith("Meshtastic ")):
                            existing["long_name"] = long_name
                            existing["short_name"] = short_name or existing["short_name"]
                            existing["hw_model"] = hw_model or existing["hw_model"]
                    else:
                        entry = {
                            "id": node_id,
                            "long_name": long_name or None,
                            "short_name": short_name or None,
                            "hw_model": hw_model or None,
                            "snr": node_data.get("snr"),
                            "last_heard": node_data.get("lastHeard"),
                            "latitude": position.get("latitude"),
                            "longitude": position.get("longitude"),
                        }
                        if node_data.get("isSelf"):
                            entry["is_self"] = True
                        seen[node_id] = entry

            return list(seen.values())

    def get_lora_neighbors(self) -> list[dict[str, Any]]:
        """Return LoRa-only neighbors from the physical radio's NodeDB.

        In SERIAL mode: filters the live interface's nodes (always fresh).
        In MQTT mode: returns cached data from the background device probe.
        Returns an empty list if no data is available.
        """
        with self._lock:
            mode = self._mode
            connected = self._connected
            iface = self._mesh_interface

        if mode == MODE_SERIAL and connected and iface is not None:
            try:
                return self._extract_lora_neighbors(iface)
            except Exception:
                if self.log:
                    self.log.debug(
                        "Error extracting LoRa neighbors from serial",
                        exc_info=True,
                    )
                return []

        # MQTT mode — return cached probe data
        with self._lock:
            return list(self._cached_lora_neighbors)

    # ── Channel management (serial mode only) ──────────────────────

    def _get_serial_node(self) -> Any | None:
        """Return the localNode for a serial interface, or ``None``.

        In serial mode, uses the main mesh interface.
        In MQTT mode, falls back to the serial listener (device probe) if present.
        """
        with self._lock:
            if self._mode == MODE_SERIAL:
                iface = self._mesh_interface
            else:
                iface = self._serial_listener
        if iface is None:
            return None
        return getattr(iface, "localNode", None)

    def get_channels(self) -> list[dict[str, Any]]:
        """Return the radio's configured channels (serial mode only).

        Each entry: ``{index, name, role, psk_label, active}``.
        ``psk_label`` is a privacy-safe description (e.g. "default", "secret").
        """
        node = self._get_serial_node()
        if node is None:
            return []

        from meshtastic import channel_pb2
        from meshtastic.util import pskToString

        result: list[dict[str, Any]] = []
        channels = getattr(node, "channels", None) or []
        for ch in channels:
            role_name = channel_pb2.Channel.Role.Name(ch.role)
            name = ""
            psk_label = "unencrypted"
            if ch.settings:
                name = ch.settings.name or ""
                psk_label = pskToString(ch.settings.psk) if ch.settings.psk else "unencrypted"
            result.append({
                "index": ch.index,
                "name": name,
                "role": role_name,
                "psk_label": psk_label,
                "active": role_name in ("PRIMARY", "SECONDARY"),
            })
        return result

    def join_channel(
        self, name: str, psk: str, index: int | None = None,
    ) -> dict[str, Any]:
        """Configure a channel slot on the radio (serial mode only).

        Args:
            name: Channel name (displayed on the radio).
            psk: PSK value — "none", "default", "random", "simple1"-"simple254",
                 or a base64-encoded key.
            index: Channel slot (1-7).  If ``None``, uses the first DISABLED slot.

        Returns:
            ``{"ok": True, "index": N}`` on success,
            ``{"ok": False, "reason": str}`` on failure.
        """
        node = self._get_serial_node()
        if node is None:
            return {"ok": False, "reason": "Not connected in serial mode"}

        from meshtastic import channel_pb2
        from meshtastic.util import fromPSK

        channels = getattr(node, "channels", None) or []
        if not channels:
            return {"ok": False, "reason": "Channel list not loaded from device"}

        # Find target slot
        if index is not None:
            if not isinstance(index, int) or not 1 <= index <= 7:
                return {"ok": False, "reason": "index must be 1-7 (0 is PRIMARY)"}
            ch = node.getChannelByChannelIndex(index)
            if ch is None:
                return {"ok": False, "reason": f"Channel slot {index} not found"}
            if ch.role == channel_pb2.Channel.Role.PRIMARY:
                return {"ok": False, "reason": "Cannot overwrite PRIMARY channel"}
        else:
            ch = node.getDisabledChannel()
            if ch is None:
                return {"ok": False, "reason": "No available channel slots (all 8 in use)"}
            index = ch.index

        # Parse PSK — strict mode: accept only the well-known labels
        # ("none", "default", "random", "simple1"-"simple254") and
        # base64-encoded raw AES keys (16 or 32 bytes).  Anything else
        # is rejected so we stay interoperable with stock Meshtastic
        # clients that don't derive a key from a free-form passphrase.
        import base64
        import binascii
        import re

        label_pattern = re.compile(r"^simple([0-9]{1,3})$")
        if psk in ("none", "default", "random") or label_pattern.match(psk):
            try:
                psk_bytes = fromPSK(psk)
            except Exception as exc:
                return {"ok": False, "reason": f"Invalid PSK label: {exc}"}
            if not isinstance(psk_bytes, (bytes, bytearray)):
                return {
                    "ok": False,
                    "reason": f"fromPSK returned {type(psk_bytes).__name__}, expected bytes",
                }
        else:
            try:
                psk_bytes = base64.b64decode(psk, validate=True)
            except (binascii.Error, ValueError):
                return {
                    "ok": False,
                    "reason": (
                        "PSK must be a base64-encoded 16 or 32 byte key, "
                        "or one of: none, default, random, simple1-simple254"
                    ),
                }
            if len(psk_bytes) not in (16, 32):
                return {
                    "ok": False,
                    "reason": (
                        f"PSK key length is {len(psk_bytes)} bytes; "
                        "Meshtastic requires 16 (AES-128) or 32 (AES-256) bytes"
                    ),
                }

        # Configure the channel
        ch.role = channel_pb2.Channel.Role.SECONDARY
        ch.settings.name = name
        ch.settings.psk = bytes(psk_bytes)

        try:
            node.writeChannel(index)
        except Exception as exc:
            self.log.exception("Error writing channel %d to device", index)
            return {"ok": False, "reason": f"Device write failed: {exc}"}

        self.log.info("Joined channel %d: %r", index, name)
        return {"ok": True, "index": index}

    def join_channel_url(self, url: str) -> dict[str, Any]:
        """Join channel(s) from a Meshtastic URL (serial mode only).

        Args:
            url: Meshtastic channel URL (e.g. ``https://meshtastic.org/e/#...``).

        Returns:
            ``{"ok": True}`` on success,
            ``{"ok": False, "reason": str}`` on failure.
        """
        node = self._get_serial_node()
        if node is None:
            return {"ok": False, "reason": "Not connected in serial mode"}

        try:
            node.setURL(url, addOnly=True)
        except Exception as exc:
            self.log.exception("Error applying channel URL")
            return {"ok": False, "reason": str(exc)}

        self.log.info("Applied channel URL: %s", url[:60])
        return {"ok": True}

    def delete_channel(self, index: int) -> dict[str, Any]:
        """Remove a SECONDARY channel from the radio (serial mode only).

        Args:
            index: Channel slot (1-7).  PRIMARY (0) cannot be deleted.

        Returns:
            ``{"ok": True}`` on success,
            ``{"ok": False, "reason": str}`` on failure.
        """
        node = self._get_serial_node()
        if node is None:
            return {"ok": False, "reason": "Not connected in serial mode"}

        from meshtastic import channel_pb2

        if not isinstance(index, int) or not 1 <= index <= 7:
            return {"ok": False, "reason": "index must be 1-7 (0 is PRIMARY)"}

        ch = node.getChannelByChannelIndex(index)
        if ch is None:
            return {"ok": False, "reason": f"Channel slot {index} not found"}
        if ch.role != channel_pb2.Channel.Role.SECONDARY:
            return {"ok": False, "reason": f"Channel {index} is {channel_pb2.Channel.Role.Name(ch.role)}, only SECONDARY can be deleted"}

        try:
            node.deleteChannel(index)
        except Exception as exc:
            self.log.exception("Error deleting channel %d", index)
            return {"ok": False, "reason": f"Device write failed: {exc}"}

        self.log.info("Deleted channel %d", index)
        return {"ok": True}

    # ── Public send API (for messaging hub / dashboard) ────────────

    def send_message(
        self,
        text: str,
        destination_id: str | None = None,
        channel: int | None = None,
        via: str = "",
        on_ack: Any = None,
    ) -> dict[str, Any]:
        """Send a text message to the Meshtastic mesh.

        Args:
            text: Message text (truncated to MTU if needed).
            destination_id: Target node ID (e.g. ``"!abcd1234"``) or ``None``
                for broadcast.
            channel: Channel index override.  Uses the configured default
                if ``None``.
            via: Send route — ``"lora"`` to send via the local serial radio
                instead of the MQTT client.  Falls back to MQTT if the
                serial listener is unavailable.
            on_ack: Optional callback ``(acked: bool) -> None`` invoked when
                a delivery ack or nak is received.  Only works for direct
                messages sent via the serial (LoRa) interface.

        Returns:
            ``{"sent": True, "truncated": bool, "ack_tracking": str|None}``
            on success, or ``{"sent": False, "reason": str}`` on failure.
            ``ack_tracking`` is ``"serial"`` when ack callbacks are active,
            ``None`` when acks are not available (MQTT or broadcast).
        """
        if not self._check_send_rate_limit():
            return {"sent": False, "reason": "rate_limited"}

        with self._lock:
            # Prefer serial listener when via="lora" and it's available
            if via == "lora" and self._serial_listener is not None:
                iface = self._serial_listener
            elif self._active and self._connected and self._mesh_interface is not None:
                iface = self._mesh_interface
            else:
                return {"sent": False, "reason": "not_connected"}

        ch = channel if channel is not None else self.config.get("meshtastic_channel", 0)
        truncated = len(text.encode("utf-8")) > MESHTASTIC_MTU
        is_serial = iface is self._serial_listener
        via_label = "LoRa" if is_serial else "MQTT"

        # Ack tracking is only possible for direct messages via serial
        can_ack = bool(is_serial and destination_id and on_ack)

        try:
            if destination_id:
                if can_ack:
                    # Serial interface supports wantAck + onResponse
                    iface.sendText(
                        text[:MESHTASTIC_MTU], channelIndex=ch,
                        destinationId=destination_id,
                        wantAck=True,
                        onResponse=self._make_ack_handler(on_ack),
                    )
                else:
                    iface.sendText(
                        text[:MESHTASTIC_MTU], channelIndex=ch,
                        destinationId=destination_id,
                    )
            else:
                iface.sendText(text[:MESHTASTIC_MTU], channelIndex=ch)
        except Exception as exc:
            self.log.exception("Error sending Meshtastic message via %s", via_label)
            return {"sent": False, "reason": str(exc)}

        dest_label = destination_id or "broadcast"
        self.log.info(
            "Sent Meshtastic message to %s on ch%d via %s",
            dest_label, ch, via_label,
        )

        with self._lock:
            self._msgs_hub_to_mesh += 1

        self.event_bus.publish(events.MESHTASTIC_MESSAGE_SENT, {
            "text": text[:100],
            "destination": dest_label,
            "channel": ch,
            "source": "hub",
        })

        return {
            "sent": True,
            "truncated": truncated,
            "ack_tracking": "serial" if can_ack else None,
        }

    @staticmethod
    def _make_ack_handler(on_ack: Any) -> Any:
        """Create a Meshtastic onResponse callback that calls *on_ack(bool)*.

        The meshtastic library invokes ``onResponse(packet_dict)`` when an
        ack or nak arrives.  We inspect the routing field to determine
        whether it's an ack (no error) or a nak.
        """
        def _handler(packet: dict) -> None:
            try:
                routing = (packet or {}).get("decoded", {}).get("routing", {})
                error = routing.get("errorReason", "NONE")
                acked = (error == "NONE")
                on_ack(acked)
            except Exception:
                pass
        return _handler


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _load_or_create_node_num(path: str, logger: Any = None) -> int:
    """Load a persistent Meshtastic node number from *path*, or generate one.

    The file contains a single line with an 8-character hex node number.
    If the file is missing or corrupt a new number is generated and saved.
    """
    import random

    if os.path.isfile(path):
        try:
            with open(path, "r") as f:
                node_num = int(f.read().strip(), 16)
            if logger:
                logger.debug("Loaded Meshtastic node number !%08x from %s", node_num, path)
            return node_num
        except (ValueError, OSError):
            if logger:
                logger.warning("Corrupt meshtastic_node_num file, regenerating")

    node_num = random.randint(0x10000000, 0x7FFFFFFF)
    try:
        with open(path, "w") as f:
            f.write(f"{node_num:08x}\n")
        if logger:
            logger.info("Generated Meshtastic node number !%08x, saved to %s", node_num, path)
    except OSError:
        if logger:
            logger.warning("Could not save meshtastic_node_num to %s", path)
    return node_num


def _derive_short_name(long_name: str) -> str:
    """Derive a 4-character Meshtastic short name from a long name.

    Takes the first letter of each word (up to 4).  If fewer than 4 words,
    pads with characters from the first word.
    """
    words = long_name.split()
    if not words:
        return "NODE"
    if len(words) >= 4:
        return "".join(w[0] for w in words[:4]).upper()
    initials = "".join(w[0] for w in words).upper()
    padding = long_name.replace(" ", "").upper()
    return (initials + padding)[:4]


def _truncate_for_mtu(header: str, body: str, mtu: int) -> str:
    """Build header + body string, truncating body to fit within *mtu* bytes.

    Truncation is UTF-8 safe — it never splits a multi-byte character.
    """
    header_bytes = len(header.encode("utf-8"))
    max_body_bytes = mtu - header_bytes
    if max_body_bytes <= 0:
        # Header alone exceeds MTU — just return header (will be truncated by radio)
        return header

    body_encoded = body.encode("utf-8")
    if len(body_encoded) <= max_body_bytes:
        return header + body

    # Truncate with "..." indicator
    ellipsis = " ..."
    target = max_body_bytes - len(ellipsis.encode("utf-8"))
    if target <= 0:
        return header + ellipsis

    # Remove characters from the end until it fits
    truncated = body
    while len(truncated.encode("utf-8")) > target:
        truncated = truncated[:-1]

    return header + truncated + ellipsis
