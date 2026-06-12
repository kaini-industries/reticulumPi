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

import collections
import json
import os
import re
import threading
import time
from typing import Any

import RNS
import RNS.vendor.umsgpack as umsgpack

from reticulumpi import events
from reticulumpi.mtu import MESHTASTIC_MTU, truncate_bytes, truncate_for_mtu
from reticulumpi.plugin_base import PluginBase

# Meshtastic hop_limit is a 3-bit field; 7 is the protocol maximum.
MESHTASTIC_HOP_LIMIT = 7

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
_MESHTASTIC_DEFAULT_KEY = bytes(
    [
        0xD4,
        0xF1,
        0xBB,
        0x3A,
        0x20,
        0x29,
        0x07,
        0x59,
        0xF0,
        0xBC,
        0xFF,
        0xAB,
        0xCF,
        0x4E,
        0x69,
        0x01,
    ]
)

# Broadcast address for all nodes
_MESH_BROADCAST = 0xFFFFFFFF

# Characters allowed in a channel-name suffix of contact_id (conservative set
# to avoid any surprises in URLs, SQL-ish identifiers, regex matchers).
_CHANNEL_TAG_SANITIZER = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize_channel_tag(name: str) -> str:
    """Make a Meshtastic channel name safe for use inside a contact_id."""
    s = _CHANNEL_TAG_SANITIZER.sub("_", (name or "").strip())
    return s or "unknown"


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
        tls: dict[str, Any] | None = None,
        max_nodes: int = 1024,
        node_ttl_seconds: float = 86400.0,
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
        self._max_nodes: int = max_nodes
        self._node_ttl_seconds: float = node_ttl_seconds
        self._node_inserts_since_eviction: int = 0

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

        # Enable TLS if configured. Without TLS, username/password are sent
        # in cleartext and messages can be tampered with on-path.
        if tls and tls.get("enabled"):
            import ssl as _ssl

            ca_cert = tls.get("ca_cert") or None
            certfile = tls.get("certfile") or None
            keyfile = tls.get("keyfile") or None
            self.client.tls_set(
                ca_certs=ca_cert,
                certfile=certfile,
                keyfile=keyfile,
                cert_reqs=_ssl.CERT_NONE if tls.get("insecure") else _ssl.CERT_REQUIRED,
                tls_version=_ssl.PROTOCOL_TLS_CLIENT,
            )
            if tls.get("insecure"):
                self.client.tls_insecure_set(True)
                if logger:
                    logger.warning(
                        "MQTT TLS certificate verification DISABLED "
                        "(mqtt.tls.insecure=true) — connection is encrypted "
                        "but not authenticated; on-path attacker can MITM.",
                    )

        # Set connect timeout to avoid hanging on unresponsive brokers
        self.client.connect_timeout = 10.0
        self.client.connect(broker, port, keepalive=60)
        self.client.loop_start()
        # Register a finalizer so the paho loop thread is reliably stopped
        # if ``close()`` is never called — e.g., if the plugin is hot-
        # reloaded and the old instance is garbage-collected without
        # going through its normal shutdown path. Without this the old
        # paho thread outlives the plugin and keeps publishing NODEINFOs.
        import weakref

        self._finalizer = weakref.finalize(
            self,
            _MeshtasticMQTTClient._safe_shutdown_client,
            self.client,
            self._logger,
        )
        self._closed = False

    @staticmethod
    def _safe_shutdown_client(client: Any, logger: Any) -> None:
        """Finalizer callback — stop the paho thread without touching self."""
        try:
            client.loop_stop()
        except Exception:
            if logger:
                logger.debug(
                    "Error stopping MQTT loop during finalize",
                    exc_info=True,
                )
        try:
            client.disconnect()
        except Exception:
            if logger:
                logger.debug(
                    "Error disconnecting MQTT client during finalize",
                    exc_info=True,
                )

    def close(self) -> None:
        """Disconnect and clean up. Safe to call multiple times."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            if self._logger:
                self._logger.debug(
                    "Error during MQTT client close (ignored)",
                    exc_info=True,
                )
        # Normal close — detach the finalizer so it doesn't re-run later.
        fin = getattr(self, "_finalizer", None)
        if fin is not None:
            fin.detach()

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
        # pypubsub IS required by the meshtastic library — failures here
        # indicate a broken install, not an optional feature. Log so we can
        # see it if connection-state callbacks stop firing.
        try:
            from pubsub import pub

            pub.sendMessage("meshtastic.connection.established", interface=self)
        except Exception:
            if self._logger:
                self._logger.warning(
                    "Failed to dispatch meshtastic.connection.established via pubsub",
                    exc_info=True,
                )

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        """Notify on disconnect."""
        try:
            from pubsub import pub

            pub.sendMessage("meshtastic.connection.lost", interface=self)
        except Exception:
            if self._logger:
                self._logger.warning(
                    "Failed to dispatch meshtastic.connection.lost via pubsub",
                    exc_info=True,
                )

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
        POSITION_APP = PortNum.POSITION_APP

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
            self._maybe_evict_nodes()

        # Track nodes from NODEINFO_APP
        if data.portnum == NODEINFO_APP:
            self._handle_nodeinfo(from_num, data.payload)

        # Track position from POSITION_APP
        if data.portnum == POSITION_APP:
            self._handle_position(from_num, data.payload)

        # Dispatch text messages
        if data.portnum == TEXT_MESSAGE_APP:
            text = data.payload.decode("utf-8", errors="replace")
            from_id = f"!{from_num:08x}"

            # Build a packet dict compatible with the serial pubsub format
            decoded_dict: dict[str, Any] = {
                "portnum": "TEXT_MESSAGE_APP",
                "payload": data.payload,
                "text": text,
            }
            if data.emoji:
                decoded_dict["emoji"] = data.emoji
            if data.reply_id:
                decoded_dict["replyId"] = data.reply_id
            fake_packet = {
                "from": from_num,
                "to": to_num,
                "fromId": from_id,
                "toId": f"!{to_num:08x}",
                "id": packet_id,
                "decoded": decoded_dict,
                "rxSnr": packet.rx_snr if packet.rx_snr else None,
                "rxTime": packet.rx_time if packet.rx_time else None,
                # Over-the-air MeshPacket.channel is the channel hash byte,
                # not a local index.  Pass channel_id (the MQTT topic tail,
                # e.g. "LongFast") as the authoritative channel identity;
                # _on_mesh_text resolves it against local channels.
                "channel": packet.channel,
                "channelName": envelope.channel_id or None,
            }

            try:
                from pubsub import pub

                pub.sendMessage("meshtastic.receive.text", packet=fake_packet, interface=self)
            except Exception:
                if self._logger:
                    self._logger.error("Error dispatching text via pubsub", exc_info=True)

        # Dispatch PRIVATE_APP packets (read receipts, etc.)
        if data.portnum == PortNum.PRIVATE_APP:
            fake_data_packet = {
                "from": from_num,
                "fromId": f"!{from_num:08x}",
                "to": to_num,
                "toId": f"!{to_num:08x}",
                "id": packet_id,
                "decoded": {
                    "portnum": "PRIVATE_APP",
                    "payload": bytes(data.payload),
                },
            }
            try:
                from pubsub import pub

                pub.sendMessage(
                    "meshtastic.receive.data",
                    packet=fake_data_packet,
                    interface=self,
                )
            except Exception:
                if self._logger:
                    self._logger.error("Error dispatching PRIVATE_APP via pubsub", exc_info=True)

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
            self._maybe_evict_nodes()
        except Exception:
            if self._logger:
                self._logger.debug("Error parsing NODEINFO payload", exc_info=True)

    def _handle_position(self, from_num: int, payload: bytes) -> None:
        """Update node database from POSITION_APP payloads."""
        from meshtastic.protobuf.mesh_pb2 import Position

        try:
            pos = Position()
            pos.ParseFromString(payload)
            lat = pos.latitude_i / 1e7 if pos.latitude_i else None
            lon = pos.longitude_i / 1e7 if pos.longitude_i else None
            if lat is None or lon is None:
                return
            if lat == 0.0 and lon == 0.0:
                return

            node_id = f"!{from_num:08x}"
            with self._lock:
                if node_id not in self.nodes:
                    self.nodes[node_id] = {}
                self.nodes[node_id]["position"] = {
                    "latitude": lat,
                    "longitude": lon,
                }
                self.nodes[node_id]["lastHeard"] = int(time.time())
            self._maybe_evict_nodes()
            if self._logger:
                self._logger.debug("Position update for %s: %.6f, %.6f", node_id, lat, lon)
        except Exception:
            if self._logger:
                self._logger.debug("Error parsing POSITION payload", exc_info=True)

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

    def _maybe_evict_nodes(self) -> None:
        """Evict stale or excess nodes from the in-memory database.

        Called periodically (every 64 inserts) to keep memory bounded.
        Must be called while self._lock is NOT held.
        """
        with self._lock:
            self._node_inserts_since_eviction += 1
            if self._node_inserts_since_eviction < 64:
                return
            self._node_inserts_since_eviction = 0

        now = time.time()
        cutoff = now - self._node_ttl_seconds
        with self._lock:
            self.nodes = {
                nid: data
                for nid, data in self.nodes.items()
                if data.get("isSelf") or data.get("lastHeard", 0) > cutoff
            }
            if len(self.nodes) > self._max_nodes:
                keep = sorted(
                    self.nodes.items(),
                    key=lambda kv: (
                        kv[1].get("isSelf", False),
                        kv[1].get("lastHeard", 0),
                    ),
                    reverse=True,
                )[: int(self._max_nodes * 0.75)]
                self.nodes = dict(keep)

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
        with self._lock:
            packet_id = self._next_packet_id
            self._next_packet_id = (self._next_packet_id + 1) & 0xFFFFFFFF

        setattr(packet, "from", self._my_node_num)
        packet.to = _MESH_BROADCAST
        packet.id = packet_id
        packet.hop_limit = MESHTASTIC_HOP_LIMIT
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
        with self._lock:
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
        hop_limit = kwargs.get("hopLimit")
        packet.hop_limit = hop_limit if hop_limit is not None else MESHTASTIC_HOP_LIMIT

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


# ---------------------------------------------------------------------------
# Main plugin
# ---------------------------------------------------------------------------


class MeshtasticGateway(PluginBase):
    """Bridges Meshtastic text messages with LXMF over Reticulum."""

    plugin_name = "meshtastic_gateway"
    plugin_description = "Bridges Meshtastic text messages with LXMF over Reticulum"
    plugin_version = "1.2.0"
    broadcast_tier = 1
    broadcast_keys = [
        "meshtastic_device",
        "meshtastic_status",
        "meshtastic_nodes",
        "meshtastic_lora_neighbors",
    ]

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

        sri = self.config.get("serial_retry_interval", 30)
        if not isinstance(sri, (int, float)) or sri < 5:
            raise ValueError("serial_retry_interval must be >= 5 seconds")

        dpsd = self.config.get("device_probe_startup_delay", 20)
        if not isinstance(dpsd, (int, float)) or dpsd < 5:
            raise ValueError("device_probe_startup_delay must be >= 5 seconds")

        sqs = self.config.get("send_queue_size", 10)
        if not isinstance(sqs, int) or sqs < 0:
            raise ValueError("send_queue_size must be a non-negative integer")

        sqt = self.config.get("send_queue_ttl", 60)
        if not isinstance(sqt, (int, float)) or sqt <= 0:
            raise ValueError("send_queue_ttl must be > 0 seconds")

        pls = self.config.get("pending_lxmf_size", 20)
        if not isinstance(pls, int) or pls < 0:
            raise ValueError("pending_lxmf_size must be a non-negative integer")

        plt = self.config.get("pending_lxmf_ttl", 120)
        if not isinstance(plt, (int, float)) or plt <= 0:
            raise ValueError("pending_lxmf_ttl must be > 0 seconds")

        # Firmware watchdog
        fw_wd = self.config.get("firmware_watchdog", {})
        if not isinstance(fw_wd, dict):
            raise ValueError("firmware_watchdog must be a dictionary")
        fw_silence = fw_wd.get("silence_timeout", 300)
        if not isinstance(fw_silence, (int, float)) or fw_silence < 30:
            raise ValueError("firmware_watchdog.silence_timeout must be >= 30 seconds")
        fw_probe_to = fw_wd.get("probe_timeout", 15)
        if not isinstance(fw_probe_to, (int, float)) or fw_probe_to < 5:
            raise ValueError("firmware_watchdog.probe_timeout must be >= 5 seconds")
        fw_probe_iv = fw_wd.get("probe_interval", 0)
        if not isinstance(fw_probe_iv, (int, float)) or fw_probe_iv < 0:
            raise ValueError("firmware_watchdog.probe_interval must be >= 0")
        fw_max_resets = fw_wd.get("max_resets_per_hour", 3)
        if not isinstance(fw_max_resets, int) or fw_max_resets < 0:
            raise ValueError("firmware_watchdog.max_resets_per_hour must be >= 0")
        fw_open_thresh = fw_wd.get("open_failure_threshold", 3)
        if not isinstance(fw_open_thresh, int) or fw_open_thresh < 1:
            raise ValueError("firmware_watchdog.open_failure_threshold must be >= 1")

        rdos = self.config.get("reboot_device_on_stop", False)
        if not isinstance(rdos, bool):
            raise ValueError("reboot_device_on_stop must be a boolean")

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

        # Bounded outbound queue — rate-limited LXMF messages are queued
        # instead of dropped, then drained at the rate-limit interval.
        self._send_queue_max = int(self.config.get("send_queue_size", 10))
        self._send_queue_ttl: float = float(self.config.get("send_queue_ttl", 60))
        self._send_queue: collections.deque[tuple[float, str, int]] = collections.deque()
        self._send_queue_dropped = 0

        # Meshtastic state
        self._mesh_interface: Any = None
        self._connected = False
        self._mqtt_suspended = False
        self._last_disconnect_time: float = 0.0

        # Firmware watchdog — monitors the physical USB device for hangs.
        # Activates when device_probe_port is set (MQTT mode with serial
        # listener) OR when the gateway itself is in serial mode.
        fw_wd_cfg = self.config.get("firmware_watchdog", {})
        has_serial_device = bool(self.config.get("device_probe_port") or self._mode == MODE_SERIAL)
        self._fw_watchdog_enabled = fw_wd_cfg.get("enabled", True) and has_serial_device
        self._fw_silence_timeout: float = fw_wd_cfg.get("silence_timeout", 300)
        self._fw_probe_timeout: float = fw_wd_cfg.get("probe_timeout", 15)
        self._fw_probe_interval: float = fw_wd_cfg.get("probe_interval", 0)
        self._fw_auto_reset: bool = fw_wd_cfg.get("auto_reset", True)
        self._fw_usb_power_cycle: bool = fw_wd_cfg.get("usb_power_cycle", False)
        self._fw_max_resets_per_hour: int = fw_wd_cfg.get("max_resets_per_hour", 3)
        self._last_device_activity: float = 0.0
        self._last_fw_probe_time: float = 0.0
        self._fw_reset_timestamps: list[float] = []
        self._fw_hang_detected: bool = False
        self._fw_hang_reason: str | None = None
        self._fw_total_hangs: int = 0
        self._fw_total_resets: int = 0
        self._fw_open_failure_threshold: int = fw_wd_cfg.get("open_failure_threshold", 3)
        self._fw_consecutive_open_failures: int = 0
        self._fw_first_open_failure_time: float = 0.0

        # Device info probe (for dashboard device card & LoRa neighbors)
        self._cached_device_info: dict[str, Any] | None = None
        self._cached_lora_neighbors: list[dict[str, Any]] = []
        # CPython GIL: dict.__setitem__ on str keys is atomic; writes are unsynchronized
        self._node_name_cache: dict[
            str, dict[str, str]
        ] = {}  # {id: {longName, shortName, hwModel}}
        self._name_cache_path: str = ""  # set after storage_path is known
        self._device_info_cache_time: float = 0
        self._device_probe_port: str = self.config.get("device_probe_port", "")
        self._device_probe_interval: float = self.config.get("device_probe_interval", 300)
        self._serial_retry_interval: float = self.config.get("serial_retry_interval", 30)
        self._device_probe_startup_delay: float = self.config.get("device_probe_startup_delay", 20)
        self._reboot_device_on_stop: bool = self.config.get("reboot_device_on_stop", False)
        # Bounded wait on the blocking SerialInterface() constructor.  If the
        # radio never returns config_complete_id the probe thread would
        # otherwise hang forever with no log.
        self._device_probe_open_timeout: float = self.config.get("device_probe_open_timeout", 30)

        # Persistent serial listener for LoRa message reception (MQTT mode)
        self._serial_listener: Any = None

        # Channel-list cache — returned by get_channels() when the serial
        # listener is unavailable, so the dashboard doesn't flip to "no
        # channels" every time the probe stalls or the USB device hiccups.
        self._cached_channels: list[dict[str, Any]] = []
        self._channels_cache_time: float = 0.0  # monotonic; 0 = never populated
        self._cache_ttl: float = float(self.config.get("cache_ttl", 600))

        self._broadcast_cache: tuple[float, dict] | None = None
        self._broadcast_cache_ttl: float = 10.0
        self._nodes_cache: tuple[float, list] | None = None
        # Node tables change slowly; a longer TTL cuts the dict-copy/rebuild
        # frequency (and the periodic disk save) ~3x on the broadcast thread.
        self._nodes_cache_ttl: float = 30.0

        # Packet dedup — same message can arrive via both MQTT and serial,
        # and MQTT bridges can replay packets many minutes apart. Keep a
        # bounded TTL cache of seen packet IDs.
        self._seen_packet_ids: dict[int, float] = {}  # packet_id → timestamp
        self._dedup_ttl_seconds: float = max(
            30.0,
            float(self.config.get("dedup_ttl_seconds", 300.0)),
        )
        self._dedup_max_entries: int = max(
            64,
            int(self.config.get("dedup_max_entries", 2048)),
        )
        # Amortized cleanup: scan for stale entries every N inserts, not
        # every packet (was O(n) per packet via min()).
        self._dedup_cleanup_interval: int = max(
            32,
            self._dedup_max_entries // 8,
        )
        self._dedup_inserts_since_cleanup: int = 0

        # ── LXMF setup (same pattern as message_echo.py) ───────────
        default_storage = "~/.local/share/reticulumpi/meshtastic_gw_lxmf"
        storage_path = os.path.expanduser(self.config.get("storage_path", default_storage))
        os.makedirs(storage_path, exist_ok=True)

        # Load persisted caches (survive restarts)
        self._name_cache_path = os.path.join(storage_path, "node_name_cache.json")
        self._load_name_cache()
        self._node_data_cache_path = os.path.join(storage_path, "node_data_cache.json")
        self._persisted_nodes: dict[str, dict[str, Any]] = {}
        self._node_data_save_counter: int = 0
        # Guards the background node-data save so the periodic dispatch from the
        # broadcast thread never starts a second concurrent disk write.
        self._node_data_save_inflight: bool = False
        self._mqtt_node_ttl: float = float(self.config.get("mqtt_node_ttl_seconds", 86400))
        self._load_node_data_cache()

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
        self._announce_sub = self.announce_dispatcher.subscribe(
            "lxmf.propagation",
            self._handle_propagation_announce,
        )

        # ── Meshtastic MQTT identity persistence ────────────────────
        if self._mode == MODE_MQTT:
            node_num_path = os.path.join(storage_path, "meshtastic_node_num")
            self._mqtt_node_num = _load_or_create_node_num(node_num_path, self.log)
            self._mqtt_long_name = (
                self.config.get("display_name") or f"{self.app.node_name} Mesh Gateway"
            )
            self._mqtt_short_name = self.config.get("short_name") or _derive_short_name(
                self._mqtt_long_name
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

        self._pending_lxmf: collections.deque[tuple[float, bytes, str]] = collections.deque()
        self._pending_lxmf_max = int(self.config.get("pending_lxmf_size", 20))
        self._pending_lxmf_ttl: float = float(self.config.get("pending_lxmf_ttl", 120))

        self._lxmf_allow_set: set[str] = {h.lower() for h in self.config.get("lxmf_allow_list", [])}
        self._mesh_allow_set: set[str] = {
            nid.lower() for nid in self.config.get("meshtastic_allow_list", [])
        }

        # ── Subscribe to Meshtastic pubsub ONCE at plugin start ─────
        # Owned by plugin lifecycle (start/stop), NOT by MQTT connection
        # lifecycle, so the serial LoRa listener keeps delivering messages
        # even when MQTT is reconnecting or suspended.
        from pubsub import pub

        pub.subscribe(self._on_mesh_text, "meshtastic.receive.text")
        pub.subscribe(self._on_mesh_data, "meshtastic.receive.data")
        pub.subscribe(self._on_mesh_connect, "meshtastic.connection.established")
        pub.subscribe(self._on_mesh_disconnect, "meshtastic.connection.lost")

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

    def _graceful_device_shutdown(self) -> None:
        """Send reboot command so the device flushes state to flash."""
        if not self._reboot_device_on_stop:
            return
        with self._lock:
            iface = self._serial_listener or (
                self._mesh_interface if self._mode == MODE_SERIAL else None
            )
        if iface is None:
            return
        local_node = getattr(iface, "localNode", None)
        if local_node is None:
            return
        try:
            local_node.reboot(secs=2)
            self.log.info("Sent reboot to device for graceful flash flush")
            time.sleep(0.3)
        except Exception:
            self.log.debug("Could not send device reboot on stop", exc_info=True)

    def stop(self) -> None:
        # Send device reboot BEFORE setting _active=False — the probe loop's
        # finally block clears _serial_listener once _active is False.
        self._graceful_device_shutdown()
        self._active = False
        self._save_node_data_cache()
        self._save_name_cache()
        # Close persistent serial listener (if any) before joining threads
        try:
            with self._lock:
                listener = self._serial_listener
                self._serial_listener = None
            if listener is not None:
                listener.close()
        except Exception:
            self.log.debug("Error closing serial listener", exc_info=True)
        try:
            self.announce_dispatcher.unsubscribe(self._announce_sub)
        except Exception:
            self.log.debug("Error unsubscribing announce handler", exc_info=True)
        try:
            self.lxmf_router.register_delivery_callback(None)
        except Exception:
            self.log.debug("Error clearing LXMF delivery callback", exc_info=True)
        # Unsubscribe from Meshtastic pubsub (owned by plugin lifecycle)
        try:
            from pubsub import pub

            pub.unsubscribe(self._on_mesh_text, "meshtastic.receive.text")
            pub.unsubscribe(self._on_mesh_data, "meshtastic.receive.data")
            pub.unsubscribe(self._on_mesh_connect, "meshtastic.connection.established")
            pub.unsubscribe(self._on_mesh_disconnect, "meshtastic.connection.lost")
        except Exception:
            self.log.debug("Error unsubscribing from Meshtastic pubsub", exc_info=True)
        self._close_mesh_interface()
        self._join_threads()

    def on_internet_available(self) -> None:
        if self._mode != MODE_MQTT:
            return
        with self._lock:
            if self._mqtt_suspended:
                self._mqtt_suspended = False
                self._reconnect_failures = 0
        self.log.info("Internet restored — MQTT reconnection enabled")

    def on_internet_lost(self) -> None:
        if self._mode != MODE_MQTT:
            return
        self.log.warning("Internet lost — MQTT reconnection will pause")

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
            if self._mode == MODE_MQTT and not self.internet_available:
                self._sleep_while_active(30)
                continue

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
                    self.event_bus.publish(
                        events.MESHTASTIC_CONNECT_FAILED,
                        {
                            "error": str(exc),
                            "attempt": self._reconnect_failures,
                        },
                    )
                    if max_attempts > 0 and self._reconnect_failures >= max_attempts:
                        self.log.error(
                            "MQTT suspended after %d failures — LoRa reception continues",
                            max_attempts,
                        )
                        with self._lock:
                            self._mqtt_suspended = True
                        self.event_bus.publish(
                            events.MESHTASTIC_DISCONNECTED,
                            {
                                "reason": "mqtt_suspended",
                            },
                        )
                        self._sleep_while_active(600)
                        with self._lock:
                            self._reconnect_failures = 0
                        continue
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
            self._drain_send_queue()
            self._retry_pending_lxmf()
            if self._connected and not self._check_mesh_health():
                self.log.warning("Meshtastic health check failed, reconnecting")
                self._close_mesh_interface()
                self.event_bus.publish(
                    events.MESHTASTIC_DISCONNECTED,
                    {
                        "reason": "health_check_failed",
                    },
                )
                continue

            # Firmware watchdog — only in pure serial mode (no device_probe_port).
            # When device_probe_port is set, _device_probe_loop owns the watchdog.
            if (
                self._connected
                and self._mode == MODE_SERIAL
                and not self._device_probe_port
                and not self._check_firmware_watchdog()
            ):
                self.event_bus.publish(
                    events.MESHTASTIC_DISCONNECTED,
                    {
                        "reason": "firmware_hang",
                    },
                )
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
        if self._mode == MODE_MQTT:
            iface = self._create_mqtt_interface()
        else:
            iface = self._create_serial_interface()

        with self._lock:
            self._mesh_interface = iface
            self._connected = True
            self._mqtt_suspended = False
            self._connect_count += 1
            self._last_device_activity = time.monotonic()
            self._fw_hang_detected = False
            self._fw_hang_reason = None

        # Identify ourselves
        node_id = self._get_own_node_id(iface)
        conn_detail = self._get_connection_detail()

        self.log.info(
            "Meshtastic connected (mode=%s, node=%s, %s)",
            self._mode,
            node_id,
            conn_detail,
        )
        self.event_bus.publish(
            events.MESHTASTIC_CONNECTED,
            {
                "mode": self._mode,
                "node_id": node_id,
                "detail": conn_detail,
            },
        )

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
        # Credentials: env vars take precedence over config so that secrets
        # stay out of config.yaml on disk. Fall back to the public Meshtastic
        # community broker defaults only when the broker is unchanged.
        username = (
            os.environ.get(mqtt_cfg.get("username_env", ""))
            if mqtt_cfg.get("username_env")
            else None
        ) or mqtt_cfg.get("username")
        password = (
            os.environ.get(mqtt_cfg.get("password_env", ""))
            if mqtt_cfg.get("password_env")
            else None
        ) or mqtt_cfg.get("password")
        if username is None:
            username = _MQTT_DEFAULTS["username"] if broker == _MQTT_DEFAULTS["broker"] else ""
        if password is None:
            password = _MQTT_DEFAULTS["password"] if broker == _MQTT_DEFAULTS["broker"] else ""
        root_topic = mqtt_cfg.get("root_topic", _MQTT_DEFAULTS["root_topic"])
        channel_key = mqtt_cfg.get("channel_key", _MQTT_DEFAULTS["channel_key"])
        channel = self.config.get("meshtastic_channel", 0)
        tls_cfg = mqtt_cfg.get("tls") or {}

        # Security warning: non-public broker over plaintext sends creds in
        # cleartext on every reconnect and allows on-path message tampering.
        if not tls_cfg.get("enabled") and broker != _MQTT_DEFAULTS["broker"] and username:
            self.log.warning(
                "MQTT connection to %s:%d is NOT using TLS. Credentials are "
                "sent in cleartext. Set mqtt.tls.enabled=true in config.",
                broker,
                port,
            )

        self.log.info(
            "Connecting to Meshtastic MQTT (broker=%s:%d, topic=%s, tls=%s)...",
            broker,
            port,
            root_topic,
            "yes" if tls_cfg.get("enabled") else "no",
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
            tls=tls_cfg if tls_cfg.get("enabled") else None,
            max_nodes=int(self.config.get("mqtt_max_nodes", 1024)),
            node_ttl_seconds=float(self.config.get("mqtt_node_ttl_seconds", 86400)),
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
        """Tear down the Meshtastic MQTT/serial connection.

        Pubsub subscriptions are NOT touched here — they are owned by the
        plugin lifecycle (start/stop) so the serial LoRa listener keeps
        delivering messages even while MQTT is reconnecting.
        """
        with self._lock:
            if self._mesh_interface is None:
                return
            try:
                self._mesh_interface.close()
            except Exception:
                self.log.debug("Error closing Meshtastic interface", exc_info=True)
            self._mesh_interface = None
            self._connected = False
            self._nodes_cache = None
            self._broadcast_cache = None
            if self._mode == MODE_SERIAL:
                self._cached_device_info = None
                self._cached_lora_neighbors = []

    # ── Device reset ────────────────────────────────────────────

    def _resolve_usb_device_path(self) -> str | None:
        """Resolve the USB bus device path from the serial port sysfs tree.

        Returns e.g. '/dev/bus/usb/004/016' or None if resolution fails.
        """
        try:
            tty = os.path.realpath(self._device_probe_port)
            tty_name = os.path.basename(tty)
            sysfs = f"/sys/class/tty/{tty_name}/device/.."
            busnum = int(open(f"{sysfs}/busnum").read().strip())
            devnum = int(open(f"{sysfs}/devnum").read().strip())
            return f"/dev/bus/usb/{busnum:03d}/{devnum:03d}"
        except Exception:
            return None

    def _usb_bus_reset(self, usb_path: str) -> dict[str, Any]:
        """Issue a USBDEVFS_RESET ioctl on the USB bus device."""
        import fcntl

        USBDEVFS_RESET = 0x5514
        fd = None
        try:
            fd = os.open(usb_path, os.O_WRONLY)
            fcntl.ioctl(fd, USBDEVFS_RESET, 0)
            return {"ok": True, "method": "usb_reset"}
        except PermissionError:
            return {
                "ok": False,
                "reason": f"Permission denied on {usb_path} — check udev rules",
            }
        except OSError as exc:
            return {"ok": False, "reason": str(exc)}
        finally:
            if fd is not None:
                os.close(fd)

    def reset_device(self) -> dict[str, Any]:
        """Reset the Meshtastic device using escalating strategies."""
        if not self._device_probe_port:
            return {"ok": False, "reason": "No device_probe_port configured"}

        # Step 1: Try library reboot via serial listener
        with self._lock:
            listener = self._serial_listener

        if listener is not None:
            local_node = getattr(listener, "localNode", None)
            if local_node is not None:
                try:
                    local_node.reboot(secs=5)
                    self.log.info("Meshtastic device reboot command sent")
                    self._cleanup_after_reset()
                    return {"ok": True, "method": "reboot_command"}
                except Exception:
                    self.log.debug("Library reboot failed, trying USB reset", exc_info=True)

        # Step 2: Try USB bus reset
        usb_path = self._resolve_usb_device_path()
        if usb_path is not None:
            result = self._usb_bus_reset(usb_path)
            if result.get("ok"):
                self.log.info("Meshtastic device USB bus reset sent (%s)", usb_path)
                self._cleanup_after_reset()
                return result
            self.log.warning("USB bus reset failed: %s", result.get("reason"))

        return {"ok": False, "reason": "All reset methods failed"}

    def _cleanup_after_reset(self) -> None:
        """Close stale serial listener and clear caches after a device reset."""
        with self._lock:
            listener = self._serial_listener
            self._serial_listener = None
            self._cached_device_info = None
            self._cached_lora_neighbors = []
        if listener is not None:
            try:
                listener.close()
            except (OSError, RuntimeError):
                self.log.debug("Error closing serial listener after reset", exc_info=True)

    # ── Device info probe (for dashboard device card) ────────────

    def _close_leaked_serial_fd(self) -> None:
        """Force-close any leaked fd to the device probe port.

        The meshtastic SerialInterface constructor starts a reader thread
        before waitForConfig().  If the constructor raises, neither the
        stream nor the reader thread are cleaned up, permanently locking
        the serial port.  This scans /proc/self/fd for an open handle to
        our device and closes it so the next open attempt can succeed.
        """
        import os

        try:
            port = os.path.realpath(self._device_probe_port)
            for fd_name in os.listdir("/proc/self/fd"):
                try:
                    if os.readlink(f"/proc/self/fd/{fd_name}") == port:
                        os.close(int(fd_name))
                        self.log.info(
                            "Force-closed leaked fd %s to %s",
                            fd_name,
                            port,
                        )
                        return
                except (OSError, ValueError):
                    continue
        except (OSError, RuntimeError):
            self.log.debug("Error scanning for leaked serial fd", exc_info=True)

    def _open_serial_interface_with_timeout(self) -> Any | None:
        """Open a Meshtastic SerialInterface with a bounded timeout.

        ``SerialInterface()`` blocks until the radio sends a config_complete
        reply.  If the device is unresponsive this hangs indefinitely with
        no log.  Run the constructor on a daemon worker and give up after
        ``_device_probe_open_timeout`` seconds.  On timeout the worker is
        signalled via a ``cancelled`` event so it closes the interface when
        the constructor eventually completes.  If the constructor fails,
        ``_close_leaked_serial_fd`` cleans up the leaked fd on the next
        attempt.
        """
        import meshtastic.serial_interface

        self._close_leaked_serial_fd()

        timeout_s = self._device_probe_open_timeout
        result: dict[str, Any] = {"iface": None, "error": None}
        cancelled = threading.Event()

        def worker() -> None:
            iface = None
            try:
                iface = meshtastic.serial_interface.SerialInterface(devPath=self._device_probe_port)
                result["iface"] = iface
            except Exception as exc:
                result["error"] = exc

            if cancelled.is_set() and iface is not None:
                try:
                    iface.close()
                except (OSError, RuntimeError):
                    self.log.debug("Error closing abandoned serial interface", exc_info=True)
                self.log.info(
                    "Abandoned serial worker closed interface on %s",
                    self._device_probe_port,
                )

        t = threading.Thread(
            target=worker,
            name="meshtastic-serial-open",
            daemon=True,
        )
        t.start()
        t.join(timeout=timeout_s)

        if t.is_alive():
            cancelled.set()
            self.log.warning(
                "Meshtastic serial open on %s timed out after %.0fs — "
                "abandoning worker (will auto-close), retrying in %.0fs",
                self._device_probe_port,
                timeout_s,
                self._serial_retry_interval,
            )
            return None
        if result["error"] is not None:
            self.log.warning(
                "Meshtastic serial open on %s failed: %s",
                self._device_probe_port,
                result["error"],
            )
            return None
        return result["iface"]

    def _device_probe_loop(self) -> None:
        """Background thread: keep a persistent serial listener for LoRa reception.

        Only runs in MQTT mode when ``device_probe_port`` is configured.
        Opens a ``SerialInterface`` **once** and leaves it open — the
        meshtastic library's internal reader delivers local LoRa packets
        via pubsub continuously while we periodically re-read device
        info/neighbors from the live interface.  The earlier design
        closed and reopened every probe interval, which produced a
        multi-second reception gap every cycle; this keeps LoRa
        reception uninterrupted except during startup or error recovery.
        """
        # Initial delay: let the gateway MQTT connection settle and give
        # the nRF52 firmware time to fully initialize its USB CDC stack
        # after a cold boot (can take 10-15s on RAK4631).
        self.log.info(
            "Waiting %.0fs for device USB initialization (device_probe_startup_delay)...",
            self._device_probe_startup_delay,
        )
        self._sleep_while_active(self._device_probe_startup_delay)

        # Outer loop: manage the serial interface lifecycle.  Each
        # iteration opens the device once, runs the inner probe loop
        # until the interface looks broken, then closes & retries.
        while self._active:
            iface = self._open_serial_interface_with_timeout()
            if iface is None:
                with self._lock:
                    self._fw_consecutive_open_failures += 1
                    if self._fw_consecutive_open_failures == 1:
                        self._fw_first_open_failure_time = time.monotonic()
                    consecutive_failures = self._fw_consecutive_open_failures
                self.log.warning(
                    "Serial open failed (%d/%d consecutive)",
                    consecutive_failures,
                    self._fw_open_failure_threshold,
                )
                if (
                    self._fw_watchdog_enabled
                    and consecutive_failures >= self._fw_open_failure_threshold
                ):
                    self._handle_startup_firmware_hang()
                    with self._lock:
                        self._fw_consecutive_open_failures = 0
                        self._fw_first_open_failure_time = 0.0
                    self._sleep_while_active(self._serial_retry_interval * 2)
                else:
                    self._sleep_while_active(self._serial_retry_interval)
                continue

            # Publish the listener reference before the first probe so
            # _on_mesh_text tags packets correctly from the very first
            # arrival (interface is self._serial_listener → LoRa tag).
            with self._lock:
                self._serial_listener = iface
                self._fw_consecutive_open_failures = 0
                self._fw_first_open_failure_time = 0.0
            self.log.info(
                "Serial listener active on %s — receiving LoRa messages",
                self._device_probe_port,
            )

            consecutive_failures = 0
            with self._lock:
                self._last_device_activity = time.monotonic()
                self._fw_hang_detected = False
                self._fw_hang_reason = None
            try:
                # Inner loop: refresh device info from the LIVE interface
                # without closing it.  The meshtastic library's reader
                # thread updates iface.nodesByNum / iface.myInfo as
                # packets arrive, so each read sees fresh state.
                while self._active:
                    try:
                        info = self._read_device_info_from_interface(iface)
                        neighbors = self._extract_lora_neighbors(iface)
                        self._request_missing_nodeinfo(iface, neighbors)

                        if info:
                            with self._lock:
                                self._cached_device_info = info
                                self._device_info_cache_time = time.monotonic()
                                self._cached_lora_neighbors = neighbors
                                self._last_device_activity = time.monotonic()
                            self._save_name_cache()
                            self.log.debug(
                                "Device probe OK: %s %s (%s), %d LoRa neighbors",
                                info.get("hw_model"),
                                info.get("firmware_version"),
                                info.get("node_id"),
                                len(neighbors),
                            )

                        self._refresh_channel_cache(iface)
                        consecutive_failures = 0
                    except Exception:
                        consecutive_failures += 1
                        self.log.warning(
                            "Device probe read failed (%d consecutive)",
                            consecutive_failures,
                            exc_info=True,
                        )
                        # Two consecutive read failures likely mean the
                        # interface is unusable — bail to outer loop so
                        # we close + reopen.  A single failure is
                        # tolerated (transient library glitch).
                        if consecutive_failures >= 2:
                            self.log.warning(
                                "Serial listener appears broken — reopening",
                            )
                            break

                    # Firmware watchdog: check if the device is hung
                    if not self._check_firmware_watchdog():
                        self.log.warning(
                            "Firmware watchdog triggered — reopening serial listener",
                        )
                        break

                    self._sleep_while_active(self._device_probe_interval)
            finally:
                # Close BEFORE clearing _serial_listener so the identity
                # check in _on_mesh_disconnect (interface is not
                # self._mesh_interface) correctly suppresses the pubsub
                # event fired by close().
                try:
                    iface.close()
                except (OSError, RuntimeError):
                    self.log.debug("Error closing serial listener in probe loop", exc_info=True)
                with self._lock:
                    self._serial_listener = None

    def _request_missing_nodeinfo(
        self,
        iface: Any,
        neighbors: list[dict[str, Any]],
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
                    "Requested NodeInfo from %s (generic name)",
                    node_id,
                )
            except Exception:
                self.log.debug(
                    "Failed to request NodeInfo from %s",
                    node_id,
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
            hw_val = getattr(metadata, "hwModel", None) or getattr(metadata, "hw_model", None)
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
                        info["region"] = config_pb2.Config.LoRaConfig.RegionCode.Name(region_val)
                    except (ValueError, AttributeError):
                        info["region"] = str(region_val)
                else:
                    info["region"] = None
                # Modem preset
                preset_val = getattr(lora, "modem_preset", None)
                if preset_val is not None:
                    try:
                        info["modem_preset"] = config_pb2.Config.LoRaConfig.ModemPreset.Name(
                            preset_val
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
                            info["role"] = config_pb2.Config.DeviceConfig.Role.Name(role_val)
                        except (ValueError, AttributeError):
                            info["role"] = str(role_val)
            else:
                info.setdefault("node_info_broadcast_secs", None)
        else:
            for k in (
                "region",
                "modem_preset",
                "hop_limit",
                "tx_power",
                "tx_enabled",
                "node_info_broadcast_secs",
            ):
                info[k] = None

        # ── Self-node user info (long/short name) ────────────────
        info["long_name"] = None
        info["short_name"] = None
        node_num = None
        if my_info:
            try:
                node_num = my_info.my_node_num
            except (AttributeError, TypeError):
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
                    "Loaded %d entries from node name cache",
                    len(data),
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

    def _load_node_data_cache(self) -> None:
        """Load persisted node data from disk (survives restarts)."""
        try:
            with open(self._node_data_cache_path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._persisted_nodes = data
                self.log.debug(
                    "Loaded %d entries from node data cache",
                    len(data),
                )
        except FileNotFoundError:
            pass
        except Exception:
            self.log.debug("Error loading node data cache", exc_info=True)

    def _save_node_data_cache(self) -> None:
        """Persist current node data to disk."""
        snapshot = dict(self._persisted_nodes)
        if not snapshot:
            return
        try:
            with open(self._node_data_cache_path, "w") as f:
                json.dump(snapshot, f)
        except Exception:
            self.log.debug("Error saving node data cache", exc_info=True)

    def _save_node_data_cache_async(self) -> None:
        """Dispatch the node-data disk save to a background thread.

        The periodic save fires from ``get_meshtastic_nodes`` on the broadcast
        thread; the json.dump to the SD card can spike, so push it off-thread.
        The in-flight flag is lock-free: dispatch only ever originates from the
        single broadcast thread, and a raced flag at worst causes one redundant
        save.
        """
        if self._node_data_save_inflight:
            return
        self._node_data_save_inflight = True

        def _run() -> None:
            try:
                self._save_node_data_cache()
            finally:
                self._node_data_save_inflight = False
                self._remove_thread(threading.current_thread())

        try:
            self._start_thread(_run, "meshtastic-node-save")
        except Exception:
            # If the thread couldn't be started, fall back to inline save so we
            # don't silently skip persistence (and clear the flag).
            self._node_data_save_inflight = False
            self._save_node_data_cache()

    def _extract_lora_neighbors(
        self,
        iface: Any,
    ) -> list[dict[str, Any]]:
        """Extract LoRa-only neighbors from a Meshtastic interface's NodeDB.

        Filters to nodes where ``via_mqtt`` is False/absent, excluding the
        self node.  Returns a list of dicts suitable for the dashboard API.

        Names are enriched from ``_node_name_cache`` which accumulates across
        probes, since NodeInfo packets arrive infrequently (default 3 h).
        """
        # Snapshot: the library mutates this dict from its own thread, so
        # iterating live races ("dictionary changed size during iteration").
        try:
            raw_nodes = dict(getattr(iface, "nodes", None) or {})
        except RuntimeError:
            raw_nodes = {}
        # Determine own node number to filter self even if isSelf is absent
        my_node_num: int | None = None
        my_info = getattr(iface, "myInfo", None)
        if my_info:
            try:
                my_node_num = my_info.my_node_num
            except (AttributeError, TypeError):
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

            is_generic = local_long.startswith("Meshtastic ") and len(local_long) <= 20
            long_name = (
                cached_long
                if is_generic and cached_long and not cached_long.startswith("Meshtastic ")
                else local_long
            ) or None
            short_name = (cached_short if is_generic and cached_short else local_short) or None
            hw_model = (
                cached_hw
                if (not local_hw or local_hw == "UNSET") and cached_hw and cached_hw != "UNSET"
                else local_hw
            ) or None

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
                    self.log.debug("Error reading device info from serial interface", exc_info=True)
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
            iface = self._mesh_interface
            mode = self._mode
        if iface is None:
            return False
        try:
            if mode == MODE_MQTT:
                # For MQTT, check the underlying paho client
                client = getattr(iface, "client", None)
                if client and hasattr(client, "is_connected"):
                    return client.is_connected()
                # Fallback: interface exists and is not None
                return True
            else:
                # For serial, check the serial stream
                stream = getattr(iface, "stream", None)
                if stream and hasattr(stream, "is_open"):
                    return stream.is_open
                return True
        except Exception:
            if self.log:
                self.log.debug("Error during mesh health check", exc_info=True)
            return False

    # ── Firmware watchdog ─────────────────────────────────────────

    def _check_firmware_watchdog(self) -> bool:
        """Layer 1+2+3 firmware hang detection.  Returns True if healthy."""
        if not self._fw_watchdog_enabled:
            return True

        now = time.monotonic()

        # Layer 3: USB enumeration — device vanished from bus entirely
        if not self._check_usb_present():
            self.log.warning("Firmware watchdog: USB device no longer enumerated")
            self._handle_firmware_hang("usb_disappeared")
            return False

        # Layer 1: silence detection — no data from device for too long
        silence = now - self._last_device_activity if self._last_device_activity else 0
        if self._last_device_activity and silence < self._fw_silence_timeout:
            return True

        # Proactive probe even without silence (belt-and-suspenders)
        if (
            self._fw_probe_interval > 0
            and self._last_fw_probe_time > 0
            and now - self._last_fw_probe_time < self._fw_probe_interval
        ):
            return True

        # Layer 2: active probe — ask the device for nodeinfo
        if self._last_device_activity:
            self.log.info(
                "Firmware watchdog: %ds silence, sending probe",
                int(silence),
            )
        else:
            self.log.info("Firmware watchdog: no activity since connect, sending probe")

        if self._probe_device_responsive():
            self._last_fw_probe_time = now
            self._last_device_activity = now
            return True

        self.log.warning(
            "Firmware watchdog: device unresponsive after %ds silence + probe",
            int(silence),
        )
        self._handle_firmware_hang("probe_timeout")
        return False

    def _probe_device_responsive(self) -> bool:
        """Send a lightweight command and verify the device responds."""
        with self._lock:
            iface = self._serial_listener or self._mesh_interface
        if iface is None:
            return False

        result: dict[str, Any] = {"ok": False}
        timeout = self._fw_probe_timeout

        def _do_probe() -> None:
            try:
                my_info = iface.getMyNodeInfo()
                if my_info:
                    result["ok"] = True
            except Exception:
                self.log.debug("Firmware watchdog probe failed", exc_info=True)

        t = threading.Thread(target=_do_probe, name="fw-watchdog-probe", daemon=True)
        t.start()
        t.join(timeout=timeout)

        if t.is_alive():
            self.log.debug("Firmware probe timed out after %ds", int(timeout))
            return False
        return result["ok"]

    def _check_usb_present(self) -> bool:
        """Check if the serial device path still exists on the filesystem."""
        port = self._device_probe_port or self.config.get("serial_port", "auto")
        if not port or port == "auto":
            return True
        try:
            return os.path.exists(port)
        except Exception:
            return True

    def _handle_firmware_hang(self, reason: str) -> None:
        """Record a firmware hang and attempt recovery if configured."""
        now = time.monotonic()
        with self._lock:
            self._fw_hang_detected = True
            self._fw_hang_reason = reason
            self._fw_total_hangs += 1

        self.event_bus.publish(
            events.MESHTASTIC_FIRMWARE_HANG,
            {
                "reason": reason,
                "silence_seconds": (
                    int(now - self._last_device_activity) if self._last_device_activity else None
                ),
                "total_hangs": self._fw_total_hangs,
            },
        )

        if not self._fw_auto_reset:
            self.log.warning("Firmware hang detected but auto_reset is disabled")
            return

        if not self._fw_reset_allowed():
            self.log.warning(
                "Firmware hang detected but max_resets_per_hour (%d) reached",
                self._fw_max_resets_per_hour,
            )
            return

        self._attempt_firmware_recovery(reason)

    def _fw_reset_allowed(self) -> bool:
        """Circuit breaker: check if we're under the hourly reset limit."""
        if self._fw_max_resets_per_hour <= 0:
            return True
        now = time.monotonic()
        cutoff = now - 3600
        with self._lock:
            self._fw_reset_timestamps = [t for t in self._fw_reset_timestamps if t > cutoff]
            return len(self._fw_reset_timestamps) < self._fw_max_resets_per_hour

    def _attempt_firmware_recovery(self, reason: str) -> None:
        """Escalating recovery: soft reset -> USB bus reset."""
        self.log.info("Attempting firmware recovery (trigger: %s)", reason)

        # Step 1: try soft reboot via meshtastic library on whichever
        # serial interface owns the physical device
        with self._lock:
            iface = self._serial_listener or self._mesh_interface
        if iface is not None:
            try:
                local_node = getattr(iface, "localNode", None)
                if local_node is not None:
                    local_node.reboot(secs=5)
                    self.log.info("Firmware recovery: reboot command sent")
                    self._record_reset("reboot_command")
                    self._post_recovery_wait()
                    return
            except Exception:
                self.log.debug("Soft reboot failed, escalating", exc_info=True)

        # Step 2: USB bus reset (if enabled)
        if self._fw_usb_power_cycle:
            port = self._device_probe_port or self.config.get("serial_port", "")
            if port and port != "auto":
                try:
                    saved = self._device_probe_port
                    self._device_probe_port = port
                    usb_path = self._resolve_usb_device_path()
                    self._device_probe_port = saved
                    if usb_path:
                        result = self._usb_bus_reset(usb_path)
                        if result.get("ok"):
                            self.log.info(
                                "Firmware recovery: USB bus reset sent (%s)",
                                usb_path,
                            )
                            self._record_reset("usb_bus_reset")
                            self._post_recovery_wait()
                            return
                        self.log.warning(
                            "USB bus reset failed: %s",
                            result.get("reason"),
                        )
                except Exception:
                    self.log.debug("USB bus reset failed", exc_info=True)

        self.log.error("Firmware recovery: all methods exhausted")

    def _record_reset(self, method: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._fw_reset_timestamps.append(now)
            self._fw_total_resets += 1

    def _post_recovery_wait(self) -> None:
        """Publish recovery event.  The caller (probe loop or connection loop)
        is responsible for closing and reopening the affected interface."""
        self.event_bus.publish(
            events.MESHTASTIC_FIRMWARE_RECOVERED,
            {
                "total_resets": self._fw_total_resets,
            },
        )

    def _handle_startup_firmware_hang(self) -> None:
        """Handle a firmware hang detected during serial open (pre-connection).

        Unlike mid-session hangs, there is no localNode for soft reboot —
        recovery goes straight to USB bus reset.
        """
        now = time.monotonic()
        duration = (
            int(now - self._fw_first_open_failure_time)
            if self._fw_first_open_failure_time
            else None
        )

        with self._lock:
            self._fw_hang_detected = True
            self._fw_hang_reason = "serial_open_timeout"
            self._fw_total_hangs += 1

        self.log.error(
            "Firmware hang detected: %d consecutive serial open failures "
            "over %ss — attempting recovery",
            self._fw_consecutive_open_failures,
            duration,
        )

        self.event_bus.publish(
            events.MESHTASTIC_FIRMWARE_HANG,
            {
                "reason": "serial_open_timeout",
                "consecutive_failures": self._fw_consecutive_open_failures,
                "duration_seconds": duration,
                "total_hangs": self._fw_total_hangs,
            },
        )

        if not self._fw_auto_reset:
            self.log.warning("Firmware hang detected but auto_reset is disabled")
            return

        if not self._fw_reset_allowed():
            self.log.warning(
                "Firmware hang detected but max_resets_per_hour (%d) reached",
                self._fw_max_resets_per_hour,
            )
            return

        self._attempt_startup_recovery()

    def _attempt_startup_recovery(self) -> None:
        """Recovery for startup hangs — USB bus reset only (no localNode)."""
        self.log.info(
            "Attempting startup recovery via USB bus reset (no localNode available for soft reboot)"
        )

        self._close_leaked_serial_fd()

        if not self._check_usb_present():
            self.log.error("Startup recovery: USB device not present")
            return

        usb_path = self._resolve_usb_device_path()
        if usb_path is None:
            self.log.error("Startup recovery: USB device path not found in sysfs")
            return

        result = self._usb_bus_reset(usb_path)
        if result.get("ok"):
            self.log.info("Startup recovery: USB bus reset sent (%s)", usb_path)
            self._record_reset("usb_bus_reset_startup")
            self._post_recovery_wait()
        else:
            self.log.error(
                "Startup recovery: USB bus reset failed: %s",
                result.get("reason"),
            )

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

    def _enqueue_lxmf_send(self, message: Any) -> None:
        """Enqueue a rate-limited LXMF message for later delivery."""
        try:
            sender_hash = RNS.prettyhexrep(message.source_hash)
            content = message.content_as_string()
            sender_hex = message.source_hash.hex()

            if self._lxmf_allow_set and sender_hex.lower() not in self._lxmf_allow_set:
                return

            prefix = self.config.get("lxmf_prefix", DEFAULT_LXMF_PREFIX)
            header = f"{prefix} {sender_hash}:\n"
            formatted = truncate_for_mtu(header, content, MESHTASTIC_MTU)
            channel = self.config.get("meshtastic_channel", 0)
        except Exception:
            self.log.exception("Error formatting LXMF message for queue")
            return
        with self._lock:
            if len(self._send_queue) >= self._send_queue_max:
                self._send_queue_dropped += 1
                self.log.info(
                    "LXMF->Meshtastic send queue full (%d), dropping oldest",
                    self._send_queue_max,
                )
                self._send_queue.popleft()
            self._send_queue.append((time.time(), formatted, channel))
            self.log.debug(
                "LXMF message queued (%d pending)",
                len(self._send_queue),
            )

    def _drain_send_queue(self) -> None:
        """Send queued messages, skipping expired ones, until rate-limited."""
        while True:
            with self._lock:
                while self._send_queue:
                    enqueued_at, formatted, channel = self._send_queue[0]
                    if time.time() - enqueued_at > self._send_queue_ttl:
                        self._send_queue.popleft()
                        self.log.debug("Queued message expired (%.0fs TTL)", self._send_queue_ttl)
                    else:
                        break
                if not self._send_queue:
                    return
                enqueued_at, formatted, channel = self._send_queue.popleft()

            if not self._check_send_rate_limit():
                with self._lock:
                    self._send_queue.appendleft((enqueued_at, formatted, channel))
                return

            with self._lock:
                if self._connected and self._mesh_interface is not None:
                    iface = self._mesh_interface
                elif self._serial_listener is not None:
                    iface = self._serial_listener
                else:
                    self._send_queue.appendleft((enqueued_at, formatted, channel))
                    return

            try:
                iface.sendText(formatted, channelIndex=channel, hopLimit=MESHTASTIC_HOP_LIMIT)
                with self._lock:
                    self._msgs_lxmf_to_mesh += 1
                    self._last_lxmf_msg_time = time.time()
                self.log.debug("Sent queued LXMF message on ch%d", channel)
            except Exception:
                self.log.exception("Error sending queued message to Meshtastic")
                with self._lock:
                    self._send_queue.appendleft((enqueued_at, formatted, channel))
                return

    # ── Meshtastic pubsub callbacks ─────────────────────────────────

    def _on_mesh_text(self, packet: dict, interface: Any = None) -> None:
        """Handle incoming Meshtastic text message (pubsub callback).

        Fires for messages from both the MQTT client and the persistent
        serial listener.  Packet-level dedup ensures a message arriving
        via both sources is only processed once.

        The lock is held only for the dedup check and counter updates so
        that MQTT message processing cannot block LoRa reception.
        """
        # ── Lock: dedup + capture shared refs ──────────────────────
        with self._lock:
            if not self._active:
                return
            packet_id = packet.get("id", 0)
            now = time.time()
            if packet_id:
                cutoff = now - self._dedup_ttl_seconds
                if packet_id in self._seen_packet_ids:
                    return
                self._seen_packet_ids[packet_id] = now
                self._dedup_inserts_since_cleanup += 1
                if (
                    self._dedup_inserts_since_cleanup >= self._dedup_cleanup_interval
                    or len(self._seen_packet_ids) > self._dedup_max_entries
                ):
                    self._dedup_inserts_since_cleanup = 0
                    self._seen_packet_ids = {
                        k: v for k, v in self._seen_packet_ids.items() if v > cutoff
                    }
                    if len(self._seen_packet_ids) > self._dedup_max_entries:
                        items = sorted(
                            self._seen_packet_ids.items(),
                            key=lambda kv: kv[1],
                        )
                        self._seen_packet_ids = dict(items[self._dedup_max_entries // 2 :])
            serial_listener = self._serial_listener
        # ── Lock released — all parsing runs without contention ────

        try:
            from_id = packet.get("fromId", "?")
            from_num = packet.get("from", 0)
            to_num = packet.get("to", 0)
            to_id = packet.get("toId", "")
            is_broadcast = to_num == _MESH_BROADCAST
            channel_name = packet.get("channelName")
            if channel_name:
                channel_idx: Any = channel_name
            else:
                channel_idx = packet.get("channel", 0)

            decoded = packet.get("decoded", {})
            is_serial = interface is not None and interface is serial_listener
            source_tag = "LoRa" if is_serial else "MQTT"

            # ── Emoji reaction detection ───────────────────
            emoji_codepoint = decoded.get("emoji")
            if emoji_codepoint:
                reply_to = decoded.get("replyId", 0)
                node_name = self._resolve_mesh_node_name(from_num)
                text_payload = decoded.get("text", "")
                if text_payload and text_payload.strip():
                    emoji_char = text_payload.strip()[:16]
                else:
                    try:
                        emoji_char = chr(emoji_codepoint)
                        if not emoji_char.isprintable():
                            emoji_char = "\U0001f44d"
                    except (ValueError, OverflowError):
                        emoji_char = "\U0001f44d"
                self.log.info(
                    "Meshtastic reaction [%s] from %s: %s (target=%d)",
                    source_tag,
                    from_id,
                    emoji_char,
                    reply_to,
                )
                with self._lock:
                    self._last_device_activity = time.monotonic()
                self.event_bus.publish(
                    events.MESHTASTIC_REACTION_RECEIVED,
                    {
                        "from_id": from_id,
                        "from_name": node_name,
                        "emoji": emoji_char,
                        "reply_to_packet_id": reply_to,
                        "source": source_tag,
                        "channel": channel_idx,
                    },
                )
                return

            # ── Regular text message ───────────────────────
            payload = decoded.get("payload", b"")
            text = (
                payload.decode("utf-8", errors="replace")
                if isinstance(payload, bytes)
                else str(payload)
            )

            if not text.strip():
                return

            if self._mesh_allow_set and from_id.lower() not in self._mesh_allow_set:
                self.log.debug("Ignoring Meshtastic msg from %s (not in allow list)", from_id)
                return

            node_name = self._resolve_mesh_node_name(from_num)
            sender_label = f"{node_name} ({from_id})" if node_name else from_id

            prefix = self.config.get("meshtastic_prefix", DEFAULT_MESH_PREFIX)
            formatted = f"{prefix} {sender_label}:\n{text}"

            self.log.info(
                "Meshtastic msg [%s] from %s: %s",
                source_tag,
                sender_label,
                text[:80],
            )

            with self._lock:
                self._msgs_mesh_to_lxmf += 1
                self._last_mesh_msg_time = time.time()
                self._last_device_activity = time.monotonic()

        except Exception:
            self.log.exception("Error parsing Meshtastic text message")
            return

        try:
            self._forward_to_lxmf(formatted)
        except Exception:
            self.log.exception("Error forwarding Meshtastic message to LXMF")

        self.event_bus.publish(
            events.MESHTASTIC_MESSAGE_RECEIVED,
            {
                "from_id": from_id,
                "from_name": node_name,
                "to_id": to_id,
                "is_broadcast": is_broadcast,
                "text": truncate_bytes(text, MESHTASTIC_MTU),
                "forwarded_to": len(self._recipient_hashes),
                "source": source_tag,
                "channel": channel_idx,
                "packet_id": packet_id,
                "snr": packet.get("rxSnr"),
            },
        )

    def _on_mesh_connect(self, interface: Any = None, topic: Any = None) -> None:
        """Pubsub callback when Meshtastic connection is (re-)established.

        Only reacts to connect events from the main mesh interface.
        Ignores serial listener connections AND any interface events
        that fire before _mesh_interface is assigned.
        """
        was_disconnected = False
        with self._lock:
            if interface is None or interface is not self._mesh_interface:
                self.log.debug("Connect from non-primary interface ignored")
                return
            if not self._connected:
                was_disconnected = True
                self._connected = True
                self._mqtt_suspended = False
                self._last_device_activity = time.monotonic()
                self._fw_hang_detected = False
            self._fw_hang_reason = None
        if was_disconnected:
            self.log.info("Meshtastic connection re-established (auto-reconnect)")
            self.event_bus.publish(
                events.MESHTASTIC_CONNECTED,
                {
                    "mode": self._mode,
                    "detail": "auto-reconnect",
                },
            )
        else:
            self.log.debug("Meshtastic connection.established event")

    def _on_mesh_disconnect(self, interface: Any = None, topic: Any = None) -> None:
        """Pubsub callback when Meshtastic connection is lost.

        Only reacts to disconnects from the main mesh interface.
        Ignores serial listener disconnects AND failed serial opens
        that fire pubsub events before _serial_listener is assigned.
        """
        with self._lock:
            if interface is None or interface is not self._mesh_interface:
                self.log.debug("Disconnect from non-primary interface ignored")
                return
            self._connected = False
            self._last_disconnect_time = time.monotonic()

        self.log.warning("Meshtastic connection lost")
        self.event_bus.publish(events.MESHTASTIC_DISCONNECTED, {"reason": "connection_lost"})

    # ── LXMF delivery callback ──────────────────────────────────────

    def _handle_lxmf_message(self, message: Any) -> None:
        """Handle incoming LXMF message and forward to Meshtastic."""
        if not self._check_send_rate_limit():
            self._enqueue_lxmf_send(message)
            return

        with self._lock:
            if not self._active:
                return
            lxmf_allow_set = self._lxmf_allow_set
            connected = self._connected
            mesh_iface = self._mesh_interface
            serial_listener = self._serial_listener

        try:
            sender_hash = RNS.prettyhexrep(message.source_hash)
            content = message.content_as_string()
            sender_hex = message.source_hash.hex()

            if lxmf_allow_set and sender_hex.lower() not in lxmf_allow_set:
                self.log.debug("Ignoring LXMF msg from %s (not in allow list)", sender_hash)
                return

            prefix = self.config.get("lxmf_prefix", DEFAULT_LXMF_PREFIX)
            header = f"{prefix} {sender_hash}:\n"
            formatted = truncate_for_mtu(header, content, MESHTASTIC_MTU)
            channel = self.config.get("meshtastic_channel", 0)

            # Prefer MQTT interface; fall back to serial listener
            # so outbound messages still reach the mesh when MQTT
            # is down but the LoRa radio is available.
            if connected and mesh_iface is not None:
                iface = mesh_iface
            elif serial_listener is not None:
                iface = serial_listener
            else:
                self.log.debug("LXMF message dropped — no Meshtastic interface available")
                return

        except Exception:
            self.log.exception("Error processing LXMF message for Meshtastic")
            return

        try:
            iface.sendText(formatted, channelIndex=channel, hopLimit=MESHTASTIC_HOP_LIMIT)
        except Exception:
            self.log.exception("Error sending text to Meshtastic")
            return

        with self._lock:
            self._msgs_lxmf_to_mesh += 1
            self._last_lxmf_msg_time = time.time()

        self.log.info("Forwarded LXMF msg from %s to Meshtastic ch%d", sender_hash, channel)
        self.event_bus.publish(
            events.MESHTASTIC_MESSAGE_SENT,
            {
                "from_lxmf": sender_hash,
                "text": content[:100],
                "channel": channel,
            },
        )

    # ── Helpers ──────────────────────────────────────────────────────

    def _resolve_mesh_node_name(self, node_num: int) -> str | None:
        """Look up the long name for a Meshtastic node by its node number.

        Checks three sources in order:
        1. MQTT interface node list
        2. Serial listener node list (LoRa-heard nodes)
        3. Persistent name cache
        Prefers a real name over generic "Meshtastic XXXX" auto-names.
        """
        with self._lock:
            mesh_iface = self._mesh_interface
            listener = self._serial_listener

        node_id = f"!{node_num:08x}"
        best_name: str | None = None

        # 1. MQTT interface nodes
        if mesh_iface and hasattr(mesh_iface, "nodes"):
            node_info = (getattr(mesh_iface, "nodes", None) or {}).get(node_id)
            if node_info:
                user = node_info.get("user", {})
                name = user.get("longName") or user.get("shortName")
                if name:
                    best_name = name

        # 2. Serial listener nodes (may have names MQTT doesn't)
        if listener is not None and hasattr(listener, "nodes"):
            node_info = (getattr(listener, "nodes", None) or {}).get(node_id)
            if node_info:
                user = node_info.get("user", {})
                name = user.get("longName") or user.get("shortName")
                if name:
                    # Prefer non-generic name from either source
                    if (
                        best_name
                        and best_name.startswith("Meshtastic ")
                        and not name.startswith("Meshtastic ")
                    ):
                        best_name = name
                    elif not best_name:
                        best_name = name

        # 3. Persistent name cache (accumulated over time)
        if not best_name or (best_name.startswith("Meshtastic ") and len(best_name) <= 20):
            cached = self._node_name_cache.get(node_id, {})
            cached_name = cached.get("longName") or cached.get("shortName")
            if cached_name and not cached_name.startswith("Meshtastic "):
                best_name = cached_name

        return best_name

    def _forward_to_lxmf(self, text: str) -> None:
        """Send formatted text to each configured LXMF recipient."""
        if not self._recipient_hashes:
            self.log.debug("No LXMF recipients configured, Meshtastic message not forwarded")
            return

        for recipient_hash in self._recipient_hashes:
            if not self._try_send_lxmf(recipient_hash, text):
                with self._lock:
                    if len(self._pending_lxmf) >= self._pending_lxmf_max:
                        self._pending_lxmf.popleft()
                    self._pending_lxmf.append((time.time(), recipient_hash, text))

    def _try_send_lxmf(self, recipient_hash: bytes, text: str) -> bool:
        """Attempt to send an LXMF message. Returns True on success."""
        import LXMF

        try:
            dest_identity = RNS.Identity.recall(recipient_hash)
            if dest_identity is None:
                RNS.Transport.request_path(recipient_hash)
                self.log.debug(
                    "Path requested for %s, message deferred",
                    RNS.prettyhexrep(recipient_hash),
                )
                return False

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
            return True
        except Exception:
            self.log.exception(
                "Failed to forward to LXMF recipient %s",
                RNS.prettyhexrep(recipient_hash),
            )
            return False

    def _retry_pending_lxmf(self) -> None:
        """Retry deferred LXMF messages whose paths may now be resolved."""
        with self._lock:
            retries = len(self._pending_lxmf)
        now = time.time()
        for _ in range(retries):
            with self._lock:
                if not self._pending_lxmf:
                    break
                enqueued_at, recipient_hash, text = self._pending_lxmf.popleft()
            if now - enqueued_at > self._pending_lxmf_ttl:
                self.log.debug(
                    "Pending LXMF to %s expired",
                    RNS.prettyhexrep(recipient_hash),
                )
                continue
            if not self._try_send_lxmf(recipient_hash, text):
                with self._lock:
                    self._pending_lxmf.append((enqueued_at, recipient_hash, text))

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
            active = self._active
            mode = self._mode
            connected = self._connected
            iface = self._mesh_interface
            mqtt_suspended = self._mqtt_suspended
            msgs_mesh_to_lxmf = self._msgs_mesh_to_lxmf
            msgs_lxmf_to_mesh = self._msgs_lxmf_to_mesh
            msgs_hub_to_mesh = self._msgs_hub_to_mesh
            msgs_rate_limited = self._msgs_rate_limited
            connect_count = self._connect_count
            reconnect_failures = self._reconnect_failures
            last_mesh_msg_time = self._last_mesh_msg_time
            last_lxmf_msg_time = self._last_lxmf_msg_time
            lxmf_recipients = len(self._recipient_hashes)
            send_min_interval = self._send_min_interval
            mqtt_node_num = self._mqtt_node_num
            mqtt_long_name = self._mqtt_long_name
            mqtt_short_name = self._mqtt_short_name
            last_disconnect_time = self._last_disconnect_time
            fw_watchdog_enabled = self._fw_watchdog_enabled
            fw_hang_detected = self._fw_hang_detected
            fw_hang_reason = self._fw_hang_reason
            last_device_activity = self._last_device_activity
            fw_silence_timeout = self._fw_silence_timeout
            fw_total_hangs = self._fw_total_hangs
            fw_total_resets = self._fw_total_resets
            fw_auto_reset = self._fw_auto_reset
            fw_reset_timestamps = list(self._fw_reset_timestamps)
            fw_max_resets_per_hour = self._fw_max_resets_per_hour
            fw_consecutive_open_failures = self._fw_consecutive_open_failures
            fw_open_failure_threshold = self._fw_open_failure_threshold
            fw_first_open_failure_time = self._fw_first_open_failure_time
            serial_listener = self._serial_listener

        if not connected and active and iface is not None and last_disconnect_time > 0:
            grace = self.config.get("reconnect_grace_period", 90)
            elapsed = time.monotonic() - last_disconnect_time
            if elapsed < grace:
                connected = True

        status: dict[str, Any] = {
            "active": active,
            "mode": mode,
            "connected": connected,
            "serial_available": serial_listener is not None,
            "mqtt_suspended": mqtt_suspended,
            "meshtastic_channel": self.config.get("meshtastic_channel", 0),
            "msgs_mesh_to_lxmf": msgs_mesh_to_lxmf,
            "msgs_lxmf_to_mesh": msgs_lxmf_to_mesh,
            "msgs_hub_to_mesh": msgs_hub_to_mesh,
            "msgs_rate_limited": msgs_rate_limited,
            "connect_count": connect_count,
            "reconnect_failures": reconnect_failures,
            "last_mesh_msg_time": last_mesh_msg_time,
            "last_lxmf_msg_time": last_lxmf_msg_time,
            "lxmf_recipients": lxmf_recipients,
        }
        if mode == MODE_SERIAL:
            status["serial_port"] = self.config.get("serial_port", "auto")
        else:
            mqtt_cfg = self.config.get("mqtt", {})
            status["mqtt_broker"] = mqtt_cfg.get("broker", _MQTT_DEFAULTS["broker"])
            status["mqtt_topic"] = mqtt_cfg.get("root_topic", _MQTT_DEFAULTS["root_topic"])
            if mqtt_node_num:
                status["node_id"] = f"!{mqtt_node_num:08x}"
                status["long_name"] = mqtt_long_name
                status["short_name"] = mqtt_short_name

        if send_min_interval > 0:
            status["rate_limit_per_min"] = round(60.0 / send_min_interval, 1)

        if connected and iface:
            try:
                nodes = getattr(iface, "nodes", None) or {}
                status["meshtastic_nodes"] = len(nodes)
            except Exception:
                if self.log:
                    self.log.debug("Error reading node count for status", exc_info=True)

        if fw_watchdog_enabled:
            now_mono = time.monotonic()
            silence = int(now_mono - last_device_activity) if last_device_activity else None
            status["firmware_watchdog"] = {
                "enabled": True,
                "hang_detected": fw_hang_detected,
                "hang_reason": fw_hang_reason,
                "silence_seconds": silence,
                "silence_timeout": int(fw_silence_timeout),
                "total_hangs": fw_total_hangs,
                "total_resets": fw_total_resets,
                "auto_reset": fw_auto_reset,
                "resets_last_hour": len([t for t in fw_reset_timestamps if t > now_mono - 3600]),
                "max_resets_per_hour": fw_max_resets_per_hour,
                "consecutive_open_failures": fw_consecutive_open_failures,
                "open_failure_threshold": fw_open_failure_threshold,
                "open_failure_duration_seconds": (
                    int(now_mono - fw_first_open_failure_time)
                    if fw_first_open_failure_time > 0
                    else None
                ),
            }

        return status

    def broadcast_snapshot(self, cycle_count: int = 0) -> dict | None:
        now = time.monotonic()
        cached = self._broadcast_cache
        if cached is not None and (now - cached[0]) < self._broadcast_cache_ttl:
            return cached[1]

        result = {}
        if hasattr(self, "get_device_info"):
            d = self.get_device_info()
            if d:
                result["meshtastic_device"] = d
        if hasattr(self, "get_status"):
            s = self.get_status()
            if s:
                result["meshtastic_status"] = s
        if hasattr(self, "get_meshtastic_nodes"):
            n = self.get_meshtastic_nodes()
            if n:
                result["meshtastic_nodes"] = n
        if hasattr(self, "get_lora_neighbors"):
            ln = self.get_lora_neighbors()
            if ln:
                result["meshtastic_lora_neighbors"] = ln
        snapshot = result or None
        self._broadcast_cache = (now, snapshot)
        return snapshot

    def get_meshtastic_nodes(self) -> list[dict[str, Any]]:
        """Return list of known Meshtastic mesh nodes.

        Merges nodes from the MQTT interface and the serial listener so
        contacts from both sources appear in the Messages panel.
        """
        nc = self._nodes_cache
        if nc is not None and (time.monotonic() - nc[0]) < self._nodes_cache_ttl:
            return nc[1]

        with self._lock:
            connected = self._connected
            iface = self._mesh_interface
            listener = self._serial_listener

        raw_mqtt: dict = {}
        if connected and iface:
            try:
                raw_mqtt = dict(getattr(iface, "nodes", None) or {})
            except (TypeError, ValueError):
                pass

        raw_serial: dict = {}
        if listener is not None:
            try:
                raw_serial = dict(getattr(listener, "nodes", None) or {})
            except (TypeError, ValueError):
                pass

        now_ts = time.time()
        persist_cutoff = now_ts - self._mqtt_node_ttl
        seen: dict[str, dict[str, Any]] = {
            nid: entry
            for nid, entry in self._persisted_nodes.items()
            if (entry.get("last_heard") or 0) > persist_cutoff
        }

        for node_id, node_data in raw_mqtt.items():
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
                "via_mqtt": True,
                "via_lora": False,
            }
            if node_data.get("isSelf"):
                entry["is_self"] = True
                entry["via_mqtt"] = False
                entry["via_lora"] = True
            seen[node_id] = entry

        for node_id, node_data in raw_serial.items():
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
            serial_via_mqtt = bool(node_data.get("via_mqtt") or node_data.get("viaMqtt"))
            serial_via_lora = not serial_via_mqtt
            if node_data.get("isSelf"):
                serial_via_mqtt = False
                serial_via_lora = True
            if node_id in seen:
                existing = seen[node_id]
                existing["via_mqtt"] = existing.get("via_mqtt", False) or serial_via_mqtt
                existing["via_lora"] = existing.get("via_lora", False) or serial_via_lora
                ex_name = existing.get("long_name") or ""
                if (
                    ex_name.startswith("Meshtastic ")
                    and long_name
                    and not long_name.startswith("Meshtastic ")
                ):
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
                    "via_mqtt": serial_via_mqtt,
                    "via_lora": serial_via_lora,
                }
                if node_data.get("isSelf"):
                    entry["is_self"] = True
                seen[node_id] = entry

        result = list(seen.values())
        self._nodes_cache = (time.monotonic(), result)

        self._persisted_nodes = {e["id"]: e for e in result if not e.get("is_self")}
        self._node_data_save_counter += 1
        if self._node_data_save_counter >= 20:
            self._node_data_save_counter = 0
            # Off-thread: keep the json.dump to SD off the broadcast thread.
            self._save_node_data_cache_async()

        return result

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

        # MQTT mode — return cached probe data, clearing if stale
        with self._lock:
            if self._cached_lora_neighbors and self._device_info_cache_time > 0:
                age = time.monotonic() - self._device_info_cache_time
                if age > self._cache_ttl:
                    self._cached_lora_neighbors = []
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

    def _channels_from_node(self, node: Any) -> list[dict[str, Any]]:
        """Build the channel list from a Meshtastic localNode object."""
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
            result.append(
                {
                    "index": ch.index,
                    "name": name,
                    "role": role_name,
                    "psk_label": psk_label,
                    "active": role_name in ("PRIMARY", "SECONDARY"),
                }
            )
        return result

    def _refresh_channel_cache(self, iface: Any) -> None:
        """Read channels from ``iface.localNode`` and update the in-memory cache."""
        node = getattr(iface, "localNode", None)
        if node is None:
            return
        try:
            channels = self._channels_from_node(node)
        except Exception:
            self.log.debug("Error reading channels from localNode", exc_info=True)
            return
        if not channels:
            # localNode not yet populated — don't clobber a known-good cache
            return
        with self._lock:
            self._cached_channels = channels
            self._channels_cache_time = time.monotonic()

    def get_channels(self) -> list[dict[str, Any]]:
        """Return the radio's configured channels.

        Serves live from the serial listener when available; otherwise
        returns the most recent cached list.  Returns ``[]`` only if the
        cache has never been populated.  Check ``channels_cache_age_seconds``
        for staleness.

        Each entry: ``{index, name, role, psk_label, active}``.
        ``psk_label`` is a privacy-safe description (e.g. "default", "secret").
        """
        node = self._get_serial_node()
        if node is not None:
            try:
                channels = self._channels_from_node(node)
                if channels:
                    with self._lock:
                        self._cached_channels = channels
                        self._channels_cache_time = time.monotonic()
                    return channels
            except Exception:
                self.log.debug(
                    "Error reading live channels from localNode",
                    exc_info=True,
                )
                # fall through to cached value
        with self._lock:
            if self._cached_channels and self._channels_cache_time > 0:
                age = time.monotonic() - self._channels_cache_time
                if age > self._cache_ttl:
                    self._cached_channels = []
                    self._channels_cache_time = 0.0
            return list(self._cached_channels)

    @property
    def channels_live(self) -> bool:
        """True when the serial listener is currently connected."""
        return self._get_serial_node() is not None

    @property
    def channels_cache_age_seconds(self) -> float | None:
        """Seconds since the channel cache was last populated.

        Returns ``None`` if the cache has never been populated.
        """
        with self._lock:
            if self._channels_cache_time <= 0:
                return None
            return time.monotonic() - self._channels_cache_time

    def join_channel(
        self,
        name: str,
        psk: str,
        index: int | None = None,
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
        if len(name) > 11:
            return {
                "ok": False,
                "reason": "Channel name must be 11 characters or fewer",
            }

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
        if not url.startswith(("https://meshtastic.org/e/#", "http://meshtastic.org/e/#")):
            return {
                "ok": False,
                "reason": "URL must be a Meshtastic channel URL (https://meshtastic.org/e/#...)",
            }

        node = self._get_serial_node()
        if node is None:
            return {"ok": False, "reason": "Not connected in serial mode"}

        try:
            node.setURL(url, addOnly=True)
        except Exception as exc:
            self.log.exception("Error applying channel URL")
            return {"ok": False, "reason": f"Channel URL failed: {exc}"}

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
            return {
                "ok": False,
                "reason": f"Channel {index} is {channel_pb2.Channel.Role.Name(ch.role)}, only SECONDARY can be deleted",
            }

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
        # Validate destination_id format if provided
        if destination_id is not None and not _MESH_NODE_ID_RE.match(destination_id):
            return {
                "sent": False,
                "reason": f"Invalid destination_id: {destination_id!r} "
                "(must be !XXXXXXXX, 8 hex chars)",
            }

        if not self._check_send_rate_limit():
            return {"sent": False, "reason": "rate_limited"}

        with self._lock:
            if via == "lora" and self._serial_listener is not None:
                iface = self._serial_listener
            elif self._active and self._connected and self._mesh_interface is not None:
                iface = self._mesh_interface
            elif self._serial_listener is not None:
                iface = self._serial_listener
            else:
                return {"sent": False, "reason": "not_connected"}

        ch = channel if channel is not None else self.config.get("meshtastic_channel", 0)
        truncated = len(text.encode("utf-8")) > MESHTASTIC_MTU
        is_serial = iface is self._serial_listener
        via_label = "LoRa" if is_serial else "MQTT"

        # Ack tracking is only possible for direct messages via serial
        can_ack = bool(is_serial and destination_id and on_ack)

        sent_packet = None
        try:
            if destination_id:
                if can_ack:
                    # Serial interface supports wantAck + onResponse
                    sent_packet = iface.sendText(
                        truncate_bytes(text, MESHTASTIC_MTU),
                        channelIndex=ch,
                        destinationId=destination_id,
                        wantAck=True,
                        onResponse=self._make_ack_handler(on_ack),
                        hopLimit=MESHTASTIC_HOP_LIMIT,
                    )
                else:
                    sent_packet = iface.sendText(
                        truncate_bytes(text, MESHTASTIC_MTU),
                        channelIndex=ch,
                        destinationId=destination_id,
                        hopLimit=MESHTASTIC_HOP_LIMIT,
                    )
            else:
                sent_packet = iface.sendText(
                    truncate_bytes(text, MESHTASTIC_MTU),
                    channelIndex=ch,
                    hopLimit=MESHTASTIC_HOP_LIMIT,
                )
        except Exception as exc:
            self.log.exception("Error sending Meshtastic message via %s", via_label)
            return {"sent": False, "reason": str(exc)}

        dest_label = destination_id or "broadcast"
        self.log.info(
            "Sent Meshtastic message to %s on ch%d via %s",
            dest_label,
            ch,
            via_label,
        )

        with self._lock:
            self._msgs_hub_to_mesh += 1

        self.event_bus.publish(
            events.MESHTASTIC_MESSAGE_SENT,
            {
                "text": text[:100],
                "destination": dest_label,
                "channel": ch,
                "source": "hub",
            },
        )

        packet_id = None
        if sent_packet is not None:
            if isinstance(sent_packet, dict):
                packet_id = sent_packet.get("id")
            elif hasattr(sent_packet, "id"):
                packet_id = sent_packet.id

        return {
            "sent": True,
            "truncated": truncated,
            "ack_tracking": "serial" if can_ack else None,
            "packet_id": packet_id,
        }

    def _make_ack_handler(self, on_ack: Any) -> Any:
        """Create a Meshtastic onResponse callback that calls *on_ack(bool)*.

        The meshtastic library invokes ``onResponse(packet_dict)`` when an
        ack or nak arrives.  We inspect the routing field to determine
        whether it's an ack (no error) or a nak.
        """
        log = self.log

        def _handler(packet: dict) -> None:
            try:
                routing = (packet or {}).get("decoded", {}).get("routing", {})
                error = routing.get("errorReason", "NONE")
                acked = error == "NONE"
                on_ack(acked)
            except Exception:
                # Log so delivery-tracking failures aren't invisible; still
                # swallow since raising would kill the meshtastic library's
                # response dispatcher thread.
                log.warning(
                    "Error in Meshtastic ACK handler",
                    exc_info=True,
                )

        return _handler

    # ── Read receipts (PRIVATE_APP portnum) ──────────────────────────

    _READ_RECEIPT_TAG = 0x01

    def send_read_receipt(
        self,
        packet_id: int,
        destination_id: str,
    ) -> dict[str, Any]:
        """Send a read-receipt data packet to a Meshtastic peer.

        Uses PRIVATE_APP portnum (256) with a compact binary payload:
          byte 0:    type tag (0x01 = read receipt)
          bytes 1-4: packet_id (big-endian uint32)
        """
        # Validate destination_id format
        if not _MESH_NODE_ID_RE.match(destination_id):
            return {
                "sent": False,
                "reason": f"Invalid destination_id: {destination_id!r} "
                "(must be !XXXXXXXX, 8 hex chars)",
            }

        if not self._check_send_rate_limit():
            return {"sent": False, "reason": "rate_limited"}

        with self._lock:
            iface = self._serial_listener
        if iface is None or not hasattr(iface, "sendData"):
            return {"sent": False, "reason": "serial_interface_unavailable"}

        payload = bytes([self._READ_RECEIPT_TAG]) + (packet_id & 0xFFFFFFFF).to_bytes(4, "big")

        try:
            from meshtastic.protobuf.portnums_pb2 import PortNum

            iface.sendData(
                payload,
                destinationId=destination_id,
                portNum=PortNum.PRIVATE_APP,
                wantAck=False,
            )
        except Exception as exc:
            self.log.debug("Failed to send read receipt to %s: %s", destination_id, exc)
            return {"sent": False, "reason": str(exc)}

        self.log.debug("Sent read receipt to %s (packet_id=%d)", destination_id, packet_id)
        return {"sent": True}

    def _on_mesh_data(self, packet: dict, interface: Any = None) -> None:
        """Handle incoming Meshtastic data packets (non-text portnums)."""
        with self._lock:
            if not self._active:
                return
        try:
            decoded = packet.get("decoded", {})
            portnum = decoded.get("portnum")
            if portnum in ("PRIVATE_APP", 256):
                self._handle_private_app(packet)
        except Exception:
            self.log.debug("Error handling data packet", exc_info=True)

    def _handle_private_app(self, packet: dict) -> None:
        """Parse PRIVATE_APP packets for read receipts."""
        decoded = packet.get("decoded", {})
        payload = decoded.get("payload", b"")
        if isinstance(payload, (bytes, bytearray)) and len(payload) >= 5:
            if payload[0] == self._READ_RECEIPT_TAG:
                read_packet_id = int.from_bytes(payload[1:5], "big")
                from_id = packet.get("fromId", "")
                from_num = packet.get("from", 0)
                node_name = self._resolve_mesh_node_name(from_num)
                self.log.info(
                    "Read receipt from %s for packet_id=%d",
                    from_id or f"!{from_num:08x}",
                    read_packet_id,
                )
                self.event_bus.publish(
                    events.MESHTASTIC_READ_RECEIPT_RECEIVED,
                    {
                        "from_id": from_id,
                        "from_name": node_name,
                        "packet_id": read_packet_id,
                    },
                )


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
