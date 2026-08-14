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
import errno
import json
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

import RNS
import RNS.vendor.umsgpack as umsgpack

from reticulumpi import events
from reticulumpi.lxmf_compat import create_lxm_router
from reticulumpi.meshtastic_health import (
    MeshtasticHealthAdapter,
    MeshtasticHealthOutcome,
    MeshtasticHealthResult,
)
from reticulumpi.mtu import MESHTASTIC_MTU, truncate_bytes, truncate_for_mtu
from reticulumpi.plugin_base import PluginBase, PluginState
from reticulumpi.radio_recovery import PersistentResetLimiter
from reticulumpi.serial_devices import (
    SerialDeviceBusyError,
    SerialDeviceChangedError,
    SerialDeviceIdentityError,
    SerialDeviceLease,
    serial_device_registry,
    validate_stable_serial_path,
)

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
_MQTT_CONNACK_TIMEOUT_SECONDS = 10.0
_MQTT_MAX_CONNACK_TIMEOUT_SECONDS = 30.0

_PACKET_ID_STATE_SCHEMA = 1
_PACKET_ID_STATE_NAME = "meshtastic_packet_ids.json"
_PACKET_ID_BLOCK_SIZE = 256
_MAX_MESHTASTIC_PACKET_ID = 0xFFFFFFFF
_PACKET_ID_ENROLLMENT_PENDING = "pending_node_commit"
_PACKET_ID_ENROLLMENT_COMPLETE = "complete"
_PACKET_ID_PROCESS_LOCK = threading.Lock()

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

# Firmware recovery lifecycle.  A reset command is an attempted actuator,
# never evidence that the physical radio recovered.
FW_HEALTHY = "healthy"
FW_SUSPECT = "suspect"
FW_CONFIRMED_HUNG = "confirmed_hung"
FW_SOFT_RESET_ISSUED = "soft_reset_issued"
FW_HARD_RESET_ISSUED = "hard_reset_issued"
FW_WAITING_FOR_REOPEN = "waiting_for_reopen"
FW_VERIFYING = "verifying"
FW_RECOVERED = "recovered"
FW_DEGRADED = "degraded"


class SerialOpenOutcome(str, Enum):
    """Typed result for the bounded Meshtastic serial constructor."""

    OPENED = "opened"
    TIMEOUT = "timeout"
    BUSY = "busy"
    PERMISSION = "permission"
    MISSING = "missing"
    IDENTITY = "identity"
    TEARDOWN_UNPROVEN = "teardown_unproven"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SerialOpenResult:
    outcome: SerialOpenOutcome
    interface: Any = None
    error: BaseException | None = None
    generation: int | None = None

    @property
    def opened(self) -> bool:
        return self.outcome is SerialOpenOutcome.OPENED and self.interface is not None

    @property
    def reset_eligible(self) -> bool:
        # Only an actual config-handshake timeout is evidence that a present,
        # correctly owned device may be wedged.  Busy/permission/missing/
        # identity failures must never cause a reset.
        return self.outcome is SerialOpenOutcome.TIMEOUT


class SerialOpenError(RuntimeError):
    """Connection-loop exception retaining a typed serial-open failure."""

    def __init__(self, result: SerialOpenResult, port: str) -> None:
        self.result = result
        detail = f": {result.error}" if result.error is not None else ""
        super().__init__(f"Meshtastic serial open {result.outcome.value} on {port}{detail}")


class _SerialCommandOutcome(str, Enum):
    SUCCESS = "success"
    BUSY = "busy"
    STALE = "stale"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class _SerialCommandResult:
    outcome: _SerialCommandOutcome
    value: Any = None
    error: BaseException | None = None
    started: bool = False

    @property
    def succeeded(self) -> bool:
        return self.outcome is _SerialCommandOutcome.SUCCESS

    @property
    def uncertain(self) -> bool:
        return self.started and self.outcome in {
            _SerialCommandOutcome.STALE,
            _SerialCommandOutcome.TIMEOUT,
            _SerialCommandOutcome.ERROR,
        }


# Characters allowed in a channel-name suffix of contact_id (conservative set
# to avoid any surprises in URLs, SQL-ish identifiers, regex matchers).
_CHANNEL_TAG_SANITIZER = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize_channel_tag(name: str) -> str:
    """Make a Meshtastic channel name safe for use inside a contact_id."""
    s = _CHANNEL_TAG_SANITIZER.sub("_", (name or "").strip())
    return s or "unknown"


class _PacketIdStateError(RuntimeError):
    """Durable MQTT packet-ID state is unsafe or inconsistent."""


class _PacketIdExhaustedError(RuntimeError):
    """The nonzero uint32 packet-ID space for an MQTT identity is exhausted."""


def _atomic_write_private_text(path: str, content: str) -> None:
    """Atomically persist owner-only text and fsync its containing directory."""

    import tempfile

    directory = os.path.dirname(path) or "."
    prefix = f".{os.path.basename(path)}."
    fd, temporary_path = tempfile.mkstemp(prefix=prefix, dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = ""
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_fd = os.open(directory, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


class _DurablePacketIdAllocator:
    """Crash-safe nonzero uint32 ID allocator for one persistent MQTT node.

    The replaceable JSON state is protected by a separate stable flock inode.
    Ranges are persisted before use, so a crash can waste IDs but cannot cause
    a later process to reuse them. Exhaustion fails closed rather than wrapping
    an encrypted `(node number, packet ID)` AES-CTR nonce domain.
    """

    def __init__(
        self,
        state_path: str,
        node_num_path: str,
        logger: Any = None,
        *,
        block_size: int = _PACKET_ID_BLOCK_SIZE,
    ) -> None:
        if (
            not isinstance(block_size, int)
            or isinstance(block_size, bool)
            or not 1 <= block_size < _MAX_MESHTASTIC_PACKET_ID
        ):
            raise ValueError("packet-ID block_size must be between 1 and 4294967294")
        self._state_path = os.path.abspath(state_path)
        self._node_num_path = os.path.abspath(node_num_path)
        self._lock_path = f"{self._state_path}.lock"
        self._logger = logger
        self._block_size = block_size
        self._lock = threading.Lock()
        self._next_id = 0
        self._remaining = 0
        self.node_num = 0
        self._initialize()

    @staticmethod
    def _state_payload(node_num: int, high_watermark: int, enrollment: str) -> dict[str, Any]:
        return {
            "schema": _PACKET_ID_STATE_SCHEMA,
            "node_num": node_num,
            "high_watermark": high_watermark,
            "enrollment": enrollment,
        }

    @staticmethod
    def _private_read(path: str) -> str:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = -1
                return stream.read()
        finally:
            if fd >= 0:
                os.close(fd)

    def _load_state_locked(self) -> dict[str, Any]:
        try:
            raw = self._private_read(self._state_path)
            state = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise _PacketIdStateError(
                f"MQTT packet-ID state is unreadable; refusing nonce reuse: {exc}"
            ) from exc
        required = {"schema", "node_num", "high_watermark", "enrollment"}
        if not isinstance(state, dict) or set(state) != required:
            raise _PacketIdStateError("MQTT packet-ID state has an invalid schema")
        schema = state["schema"]
        node_num = state["node_num"]
        high_watermark = state["high_watermark"]
        enrollment = state["enrollment"]
        if schema != _PACKET_ID_STATE_SCHEMA or isinstance(schema, bool):
            raise _PacketIdStateError("MQTT packet-ID state version is unsupported")
        if (
            not isinstance(node_num, int)
            or isinstance(node_num, bool)
            or not 0x10000000 <= node_num <= 0x7FFFFFFF
        ):
            raise _PacketIdStateError("MQTT packet-ID state has an invalid node number")
        if (
            not isinstance(high_watermark, int)
            or isinstance(high_watermark, bool)
            or not 1 <= high_watermark <= _MAX_MESHTASTIC_PACKET_ID
        ):
            raise _PacketIdStateError("MQTT packet-ID state has an invalid high watermark")
        if enrollment not in {
            _PACKET_ID_ENROLLMENT_PENDING,
            _PACKET_ID_ENROLLMENT_COMPLETE,
        }:
            raise _PacketIdStateError("MQTT packet-ID state has an invalid enrollment status")
        return state

    def _load_node_num_locked(self, *, strict: bool) -> int | None:
        if not os.path.lexists(self._node_num_path):
            if strict:
                raise _PacketIdStateError("persistent MQTT node number is missing")
            return None
        try:
            raw = self._private_read(self._node_num_path).strip()
            if not re.fullmatch(r"[0-9a-fA-F]{8}", raw):
                raise ValueError("expected exactly eight hexadecimal digits")
            node_num = int(raw, 16)
            if not 0x10000000 <= node_num <= 0x7FFFFFFF:
                raise ValueError("node number is outside the private MQTT identity range")
            return node_num
        except (OSError, UnicodeError, ValueError) as exc:
            if strict:
                raise _PacketIdStateError(f"persistent MQTT node number is invalid: {exc}") from exc
            return None

    def _write_state_locked(self, state: dict[str, Any]) -> None:
        _atomic_write_private_text(
            self._state_path,
            json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n",
        )

    def _write_node_num_locked(self, node_num: int) -> None:
        _atomic_write_private_text(self._node_num_path, f"{node_num:08x}\n")

    @staticmethod
    def _new_node_num(previous: int | None) -> int:
        import secrets

        while True:
            candidate = 0x10000000 + secrets.randbelow(0x70000000)
            if candidate != previous:
                return candidate

    def _new_initial_range(self) -> tuple[int, int, int]:
        # Enrollment already commits a cryptographically new node identity,
        # so the new nonce domain can safely use its complete uint32 range.
        return 1, self._block_size, self._block_size

    def _open_lock_fd(self) -> int:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self._lock_path, flags, 0o600)
        os.fchmod(fd, 0o600)
        return fd

    def _under_file_lock(self, callback: Callable[[], Any]) -> Any:
        import fcntl

        os.makedirs(os.path.dirname(self._state_path) or ".", mode=0o700, exist_ok=True)
        with _PACKET_ID_PROCESS_LOCK:
            fd = self._open_lock_fd()
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                return callback()
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def _initialize(self) -> None:
        def initialize_locked() -> None:
            if not os.path.lexists(self._state_path):
                legacy_node_num = self._load_node_num_locked(strict=False)
                node_num = self._new_node_num(legacy_node_num)
                next_id, remaining, high_watermark = self._new_initial_range()
                pending = self._state_payload(
                    node_num,
                    high_watermark,
                    _PACKET_ID_ENROLLMENT_PENDING,
                )
                # State first makes a crash before the node-file replacement
                # recoverable without ever returning the legacy identity.
                self._write_state_locked(pending)
                self._write_node_num_locked(node_num)
                complete = dict(pending)
                complete["enrollment"] = _PACKET_ID_ENROLLMENT_COMPLETE
                self._write_state_locked(complete)
                self.node_num = node_num
                self._next_id = next_id
                self._remaining = remaining
                if self._logger:
                    if legacy_node_num is None:
                        self._logger.info(
                            "Enrolled durable Meshtastic MQTT identity !%08x",
                            node_num,
                        )
                    else:
                        self._logger.warning(
                            "Rotated legacy Meshtastic MQTT identity !%08x to !%08x "
                            "while enrolling crash-safe packet IDs",
                            legacy_node_num,
                            node_num,
                        )
                return

            state = self._load_state_locked()
            node_num = state["node_num"]
            if state["enrollment"] == _PACKET_ID_ENROLLMENT_PENDING:
                # The state file is authoritative because it was durably
                # committed before the node file in the enrollment protocol.
                self._write_node_num_locked(node_num)
                state = dict(state)
                state["enrollment"] = _PACKET_ID_ENROLLMENT_COMPLETE
                self._write_state_locked(state)
            else:
                persisted_node_num = self._load_node_num_locked(strict=True)
                if persisted_node_num != node_num:
                    raise _PacketIdStateError(
                        "persistent MQTT node number does not match packet-ID state"
                    )
            self.node_num = node_num
            self._reserve_after_locked(state)

        self._under_file_lock(initialize_locked)

    def _reserve_after_locked(self, state: dict[str, Any]) -> None:
        high_watermark = state["high_watermark"]
        if high_watermark >= _MAX_MESHTASTIC_PACKET_ID:
            raise _PacketIdExhaustedError(
                "Meshtastic MQTT packet-ID nonce domain is exhausted; "
                "rotate the MQTT node identity or channel key and explicitly "
                "re-enroll packet-ID state before continuing"
            )
        remaining_domain = _MAX_MESHTASTIC_PACKET_ID - high_watermark
        reservation_size = min(self._block_size, remaining_domain)
        new_high_watermark = high_watermark + reservation_size
        updated = dict(state)
        updated["high_watermark"] = new_high_watermark
        self._write_state_locked(updated)
        self._next_id = high_watermark + 1
        self._remaining = reservation_size

    def _reserve_more(self) -> None:
        def reserve_locked() -> None:
            state = self._load_state_locked()
            if state["enrollment"] != _PACKET_ID_ENROLLMENT_COMPLETE:
                raise _PacketIdStateError("MQTT packet-ID enrollment is incomplete")
            if state["node_num"] != self.node_num:
                raise _PacketIdStateError("MQTT packet-ID state changed identity")
            persisted_node_num = self._load_node_num_locked(strict=True)
            if persisted_node_num != self.node_num:
                raise _PacketIdStateError(
                    "persistent MQTT node number does not match packet-ID state"
                )
            self._reserve_after_locked(state)

        self._under_file_lock(reserve_locked)

    def take(self) -> int:
        """Return one already-reserved ID, reserving the next block if needed."""

        with self._lock:
            if self._remaining == 0:
                self._reserve_more()
            packet_id = self._next_id
            if not 1 <= packet_id <= _MAX_MESHTASTIC_PACKET_ID:
                raise _PacketIdStateError("allocator attempted to emit an invalid packet ID")
            self._next_id += 1
            self._remaining -= 1
            return packet_id


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
        packet_id_allocator: _DurablePacketIdAllocator,
        logger: Any = None,
        long_name: str = "",
        short_name: str = "",
        tls: dict[str, Any] | None = None,
        max_nodes: int = 1024,
        node_ttl_seconds: float = 86400.0,
        connack_timeout_seconds: float = _MQTT_CONNACK_TIMEOUT_SECONDS,
    ):
        import base64
        import hashlib
        import weakref

        import paho.mqtt.client as mqtt_client

        if (
            not isinstance(connack_timeout_seconds, (int, float))
            or isinstance(connack_timeout_seconds, bool)
            or not 0 < connack_timeout_seconds <= _MQTT_MAX_CONNACK_TIMEOUT_SECONDS
        ):
            raise ValueError("connack_timeout_seconds must be > 0 and <= 30 seconds")

        self._broker = broker
        self._port = port
        self._root_topic = root_topic.rstrip("/")
        self._ch_index = ch_index
        self._logger = logger
        self._lock = threading.Lock()
        self._packet_id_allocator = packet_id_allocator
        self._connack_timeout_seconds = float(connack_timeout_seconds)
        self._connack_event = threading.Event()
        self._connack_succeeded = False
        self._connack_reason_code: Any = None
        self._closed = False

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

        # The durable allocator owns the MQTT identity and rotated it during
        # first enrollment before any packet ID can be issued.
        self._my_node_num = packet_id_allocator.node_num
        self._long_name = long_name
        self._short_name = short_name

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

        # Register a finalizer so the paho loop thread is reliably stopped
        # if ``close()`` is never called — e.g., if the plugin is hot-
        # reloaded and the old instance is garbage-collected without
        # going through its normal shutdown path. Without this the old
        # paho thread outlives the plugin and keeps publishing NODEINFOs.
        self._finalizer = weakref.finalize(
            self,
            _MeshtasticMQTTClient._safe_shutdown_client,
            self.client,
            self._logger,
        )

        # A TCP connect call is not proof that the broker accepted our MQTT
        # session. Do not return this interface to the plugin until paho's
        # bounded CONNACK callback confirms success.
        self.client.connect_timeout = self._connack_timeout_seconds
        try:
            connect_rc = self.client.connect(broker, port, keepalive=60)
            if connect_rc != mqtt_client.MQTT_ERR_SUCCESS:
                raise ConnectionError(f"MQTT connect request was not accepted (rc={connect_rc})")
            self.client.loop_start()
        except BaseException:
            self.close()
            raise
        if not self._connack_event.wait(self._connack_timeout_seconds):
            self.close()
            raise TimeoutError(
                f"MQTT broker did not return CONNACK within "
                f"{self._connack_timeout_seconds:g} seconds"
            )
        with self._lock:
            connack_succeeded = self._connack_succeeded
            reason_code = self._connack_reason_code
        if not connack_succeeded:
            self.close()
            raise ConnectionError(f"MQTT broker rejected connection (reason={reason_code})")

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
            connack_event = self._connack_event
        connack_event.set()
        cleanup_succeeded = True
        try:
            self.client.loop_stop()
        except Exception:
            cleanup_succeeded = False
            if self._logger:
                self._logger.debug("Error stopping MQTT loop during close", exc_info=True)
        try:
            self.client.disconnect()
        except Exception:
            cleanup_succeeded = False
            if self._logger:
                self._logger.debug("Error disconnecting MQTT client during close", exc_info=True)
        # Detach only after complete cleanup. If either operation failed, the
        # finalizer retains an independent chance to finish exact teardown.
        fin = getattr(self, "_finalizer", None)
        if fin is not None and cleanup_succeeded:
            fin.detach()

    # ── paho callbacks ──────────────────────────────────────────────

    @staticmethod
    def _connack_failed(reason_code: Any) -> bool:
        """Normalize paho v1 integers and v2 ReasonCode objects."""

        failure = getattr(reason_code, "is_failure", None)
        if isinstance(failure, bool):
            return failure
        value = getattr(reason_code, "value", reason_code)
        if isinstance(value, bool):
            return True
        try:
            return int(value) != 0
        except (TypeError, ValueError, OverflowError):
            return True

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        """Subscribe to the root topic on connect and announce our identity."""
        failed = self._connack_failed(reason_code)
        if failed:
            with self._lock:
                if self._closed:
                    return
                self._connack_reason_code = reason_code
                self._connack_succeeded = False
                self._connack_event.set()
            if self._logger:
                self._logger.warning("MQTT broker rejected CONNACK: %s", reason_code)
            return
        topic = f"{self._root_topic}/#"
        try:
            import paho.mqtt.client as mqtt_client

            subscribe_result = client.subscribe(topic, qos=0)
            if isinstance(subscribe_result, tuple) and subscribe_result:
                subscribe_rc = subscribe_result[0]
            else:
                subscribe_rc = getattr(subscribe_result, "rc", subscribe_result)
            subscription_accepted = subscribe_rc == mqtt_client.MQTT_ERR_SUCCESS
        except Exception as exc:
            subscribe_rc = f"error: {exc}"
            subscription_accepted = False
        with self._lock:
            if self._closed:
                return
            self._connack_reason_code = (
                reason_code if subscription_accepted else f"subscribe {subscribe_rc}"
            )
            self._connack_succeeded = subscription_accepted
            self._connack_event.set()
        if not subscription_accepted:
            if self._logger:
                self._logger.warning(
                    "MQTT subscription to %s was not accepted (%s)",
                    topic,
                    subscribe_rc,
                )
            return
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
        import paho.mqtt.client as mqtt_client

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
            packet_id = self._packet_id_allocator.take()

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
        publish_info = self.client.publish(topic, envelope.SerializeToString(), qos=0)
        if publish_info.rc != mqtt_client.MQTT_ERR_SUCCESS:
            raise ConnectionError(f"MQTT NODEINFO publish was not accepted (rc={publish_info.rc})")

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
        import paho.mqtt.client as mqtt_client

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
            packet_id = self._packet_id_allocator.take()

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
        publish_info = self.client.publish(topic, envelope.SerializeToString(), qos=0)
        if publish_info.rc != mqtt_client.MQTT_ERR_SUCCESS:
            raise ConnectionError(f"MQTT text publish was not accepted (rc={publish_info.rc})")

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
    plugin_version = "1.2.1"
    plugin_lifecycle_api = 2
    # MQTT plus an owned physical listener intentionally waits for the default
    # 20s USB initialization delay, a bounded 30s constructor, and a bounded
    # 15s correlated health probe before declaring readiness.
    plugin_start_timeout_seconds = 75.0
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
            serial_port = self.config.get("serial_port", "/dev/meshtastic")
            validate_stable_serial_path(serial_port, "serial_port")

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

            connack_timeout = mqtt_cfg.get(
                "connack_timeout_seconds",
                _MQTT_CONNACK_TIMEOUT_SECONDS,
            )
            if (
                not isinstance(connack_timeout, (int, float))
                or isinstance(connack_timeout, bool)
                or not 0 < connack_timeout <= _MQTT_MAX_CONNACK_TIMEOUT_SECONDS
            ):
                raise ValueError("mqtt.connack_timeout_seconds must be > 0 and <= 30 seconds")

        channel = self.config.get("meshtastic_channel", 0)
        if not isinstance(channel, int) or not 0 <= channel <= 7:
            raise ValueError("meshtastic_channel must be an integer 0-7")

        hci = self.config.get("health_check_interval", 15)
        if not isinstance(hci, (int, float)) or hci < 5:
            raise ValueError("health_check_interval must be >= 5 seconds")

        rd = self.config.get("reconnect_delay", 10)
        if not isinstance(rd, (int, float)) or rd < 1:
            raise ValueError("reconnect_delay must be >= 1 second")

        mra = self.config.get("max_reconnect_attempts", 0)
        if not isinstance(mra, int) or mra < 0:
            raise ValueError("max_reconnect_attempts must be a non-negative integer")

        # Rate limiting
        mpm = self.config.get("max_messages_per_minute", 0)
        if not isinstance(mpm, (int, float)) or mpm < 0:
            raise ValueError("max_messages_per_minute must be >= 0 (0 = unlimited)")

        # Validate device_probe_port (optional, for MQTT mode device telemetry)
        dpp = self.config.get("device_probe_port", "")
        if dpp != "":
            validate_stable_serial_path(dpp, "device_probe_port")
        if mode == MODE_SERIAL and dpp:
            raise ValueError(
                "device_probe_port is only valid in MQTT mode; serial mode already owns serial_port"
            )

        dpi = self.config.get("device_probe_interval", 300)
        if not isinstance(dpi, (int, float)) or dpi < 60:
            raise ValueError("device_probe_interval must be >= 60 seconds")

        sri = self.config.get("serial_retry_interval", 30)
        if not isinstance(sri, (int, float)) or sri < 5:
            raise ValueError("serial_retry_interval must be >= 5 seconds")

        dpsd = self.config.get("device_probe_startup_delay", 20)
        if not isinstance(dpsd, (int, float)) or dpsd < 5:
            raise ValueError("device_probe_startup_delay must be >= 5 seconds")

        dpot = self.config.get("device_probe_open_timeout", 30)
        if not isinstance(dpot, (int, float)) or isinstance(dpot, bool) or dpot <= 0:
            raise ValueError("device_probe_open_timeout must be > 0 seconds")

        serial_command_timeout = self.config.get("serial_command_timeout", 5)
        if (
            not isinstance(serial_command_timeout, (int, float))
            or isinstance(serial_command_timeout, bool)
            or serial_command_timeout <= 0
        ):
            raise ValueError("serial_command_timeout must be > 0 seconds")

        serial_close_timeout = self.config.get("serial_close_timeout", 2)
        if (
            not isinstance(serial_close_timeout, (int, float))
            or isinstance(serial_close_timeout, bool)
            or serial_close_timeout <= 0
        ):
            raise ValueError("serial_close_timeout must be > 0 seconds")

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
        for bool_key, default in (
            ("enabled", True),
            ("auto_reset", True),
            ("usb_power_cycle", False),
        ):
            if not isinstance(fw_wd.get(bool_key, default), bool):
                raise ValueError(f"firmware_watchdog.{bool_key} must be a boolean")
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
        fw_reopen_delay = fw_wd.get("recovery_reopen_delay", 8)
        if (
            not isinstance(fw_reopen_delay, (int, float))
            or isinstance(fw_reopen_delay, bool)
            or fw_reopen_delay < 0
        ):
            raise ValueError("firmware_watchdog.recovery_reopen_delay must be >= 0")
        expected_identity = fw_wd.get("expected_usb_identity", {})
        if not isinstance(expected_identity, dict):
            raise ValueError("firmware_watchdog.expected_usb_identity must be a dictionary")
        allowed_identity_keys = {"vendor_id", "product_id", "serial", "sysfs_path"}
        unknown_identity_keys = set(expected_identity) - allowed_identity_keys
        if unknown_identity_keys:
            raise ValueError(
                "Unknown firmware_watchdog.expected_usb_identity keys: "
                + ", ".join(sorted(unknown_identity_keys))
            )
        for key, value in expected_identity.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"firmware_watchdog.expected_usb_identity.{key} must be a non-empty string"
                )
        reset_port = dpp or self.config.get("serial_port", "/dev/meshtastic")
        if fw_wd.get("usb_power_cycle", False) and reset_port == "auto":
            raise ValueError(
                "firmware_watchdog.usb_power_cycle requires an explicit stable serial device path"
            )

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
        self._lock = threading.Lock()
        self._mode = self.config.get("mode", MODE_SERIAL)
        self._lxmf_destinations: dict[bytes, Any] = {}

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
        # Internet-side MQTT activity must never make a silent physical serial
        # listener appear healthy.  These clocks intentionally remain separate.
        self._last_serial_activity: float = 0.0
        self._last_mqtt_activity: float = 0.0
        self._last_fw_probe_time: float = 0.0
        self._fw_hang_detected: bool = False
        self._fw_hang_reason: str | None = None
        self._fw_total_hangs: int = 0
        self._fw_total_resets: int = 0
        self._fw_open_failure_threshold: int = fw_wd_cfg.get("open_failure_threshold", 3)
        self._fw_consecutive_open_failures: int = 0
        self._fw_first_open_failure_time: float = 0.0
        self._fw_recovery_state: str = FW_HEALTHY
        self._fw_recovery_pending: bool = False
        self._fw_recovery_attempting: bool = False
        self._fw_recovery_epoch: int = 0
        self._fw_recovery_serial_generation: int | None = None
        self._fw_recovery_hard_escalated: bool = False
        self._fw_recovery_method: str | None = None
        self._fw_recovery_started_at: float | None = None
        self._fw_recovery_not_before: float = 0.0
        self._fw_recovery_reopen_delay: float = float(fw_wd_cfg.get("recovery_reopen_delay", 8))
        self._fw_last_verified_at: float | None = None
        self._fw_verified_serial_generation: int | None = None
        self._fw_last_recovery_error: str | None = None
        self._fw_verification_failure_sticky: bool = False
        self._fw_expected_usb_identity: dict[str, str] = self._normalize_usb_identity_mapping(
            fw_wd_cfg.get("expected_usb_identity", {})
        )
        self._fw_bound_usb_identity: dict[str, str] | None = None
        self._fw_reset_limiter: PersistentResetLimiter | None = None
        self._fw_recovery_operation_lock = threading.Lock()
        self._serial_operation_lock = threading.Lock()
        self._serial_worker_lock = threading.Lock()
        self._serial_open_attempt_lock = threading.Lock()
        self._serial_open_attempt_active = False
        self._serial_open_workers: list[threading.Thread] = []
        self._serial_operation_workers: list[threading.Thread] = []
        self._serial_close_workers: list[threading.Thread] = []
        self._serial_close_inflight: set[tuple[int, int]] = set()
        self._serial_closed_interfaces: set[tuple[int, int]] = set()
        self._serial_unclosed_interfaces: dict[tuple[int, int], Any] = {}
        self._serial_command_timeout: float = float(self.config.get("serial_command_timeout", 5))
        self._serial_close_timeout: float = float(self.config.get("serial_close_timeout", 2))
        self._serial_teardown_unproven: bool = False
        self._meshtastic_health = MeshtasticHealthAdapter(max_workers=1)
        self._fw_dependency_error: str | None = (
            self._meshtastic_health.compatibility_error() if self._fw_watchdog_enabled else None
        )
        if self._fw_dependency_error is not None:
            self._fw_recovery_state = FW_DEGRADED
            self._fw_last_recovery_error = (
                f"health probe dependency mismatch: {self._fw_dependency_error}"
            )
        self._serial_reconnect_requested = threading.Event()
        self._serial_open_generation: int = 0
        self._serial_active_generation: int = 0
        self._serial_probe_candidate: tuple[Any, int] | None = None
        self._serial_device_lease: SerialDeviceLease | None = getattr(
            self,
            "_serial_device_lease",
            None,
        )
        self._serial_lease_failure: SerialOpenOutcome | None = None
        self._last_serial_open_result: SerialOpenResult | None = None
        self._fw_last_probe_outcome: str | None = None
        self._fw_last_probe_detail: str | None = None
        self._fw_device_firmware_version: str | None = None
        self._fw_device_hardware_model: int | None = None

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

        # Abandoned serial-open threads — a timed-out constructor keeps the
        # lease and blocks every replacement open until it exits.
        self._abandoned_serial_threads: list[threading.Thread] = []

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
        # and MQTT bridges can replay packets many minutes apart. Meshtastic
        # packet IDs are scoped to their originating node, so the cache key
        # must include the sender rather than suppressing another node that
        # happens to use the same packet ID.
        self._seen_packet_ids: dict[tuple[int | str, int], float] = {}
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

        # ── Persistent storage and LXMF setup ──────────────────
        default_storage = "~/.local/share/reticulumpi/meshtastic_gw_lxmf"
        storage_path = os.path.expanduser(self.config.get("storage_path", default_storage))
        os.makedirs(storage_path, exist_ok=True)

        # Admit the durable MQTT identity before creating LXMF/router resources.
        # Corrupt or exhausted packet-ID state is a fail-closed startup error and
        # must not leave partially registered plugin side effects behind.
        if self._mode == MODE_MQTT:
            node_num_path = os.path.join(storage_path, "meshtastic_node_num")
            packet_id_state_path = os.path.join(storage_path, _PACKET_ID_STATE_NAME)
            self._mqtt_packet_ids: _DurablePacketIdAllocator | None = _DurablePacketIdAllocator(
                packet_id_state_path,
                node_num_path,
                self.log,
            )
            self._mqtt_node_num = self._mqtt_packet_ids.node_num
            self._mqtt_long_name = (
                self.config.get("display_name") or f"{self.app.node_name} Mesh Gateway"
            )
            self._mqtt_short_name = self.config.get("short_name") or _derive_short_name(
                self._mqtt_long_name
            )
        else:
            self._mqtt_packet_ids = None
            self._mqtt_node_num = None
            self._mqtt_long_name = None
            self._mqtt_short_name = None

        # Reset-attempt history is durable state, not a process-local counter.
        # The deployment/rollback tooling already preserves this storage root.
        self._fw_reset_limiter = PersistentResetLimiter(
            os.path.join(storage_path, "firmware_watchdog_state.json"),
            self._fw_max_resets_per_hour,
        )
        self._fw_total_resets = self._fw_reset_limiter.total_attempts
        limiter_metadata = self._fw_reset_limiter.metadata()
        persisted_identity = limiter_metadata.get("device_identity")
        if isinstance(persisted_identity, dict):
            self._fw_bound_usb_identity = self._normalize_usb_identity_mapping(persisted_identity)

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

        self.lxmf_router = create_lxm_router(storagepath=storage_path)
        display_name = self.config.get("display_name") or f"{self.app.node_name} Mesh Gateway"
        self.local_lxmf_destination = self._manage_lxmf_destination(
            self.lxmf_router.register_delivery_identity(
                self._gw_identity,
                display_name=display_name,
            )
        )
        self.lxmf_router.register_delivery_callback(self._handle_lxmf_message)

        # Propagation node auto-selection
        self._best_propagation_hops = RNS.Transport.PATHFINDER_M + 1
        self._announce_sub = self.announce_dispatcher.subscribe(
            "lxmf.propagation",
            self._handle_propagation_announce,
        )

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

        if self._fw_dependency_error is not None:
            self._set_fw_state(
                FW_DEGRADED,
                f"health probe dependency mismatch: {self._fw_dependency_error}",
            )

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
            if (
                self._fw_recovery_pending
                or self._fw_recovery_attempting
                or self._serial_teardown_unproven
            ):
                self.log.info(
                    "Skipping shutdown reboot while Meshtastic recovery or teardown is unresolved"
                )
                return
            iface = self._serial_listener or (
                self._mesh_interface if self._mode == MODE_SERIAL else None
            )
            generation = self._serial_active_generation
        if iface is None:
            return
        local_node = getattr(iface, "localNode", None)
        if local_node is None:
            return

        def reboot() -> bool:
            if not self._reserve_reset("shutdown_reboot"):
                return False
            local_node.reboot(secs=2)
            return True

        result = self._run_serial_command(
            iface,
            generation,
            "shutdown-reboot",
            reboot,
        )
        if result.succeeded and result.value is True:
            self.log.info("Sent reboot to device for graceful flash flush")
            time.sleep(0.3)
        elif result.uncertain:
            self.log.warning("Shutdown reboot delivery is uncertain; it will not be retried")
        else:
            self.log.debug("Could not reserve or send device reboot on stop")

    def stop(self) -> None:
        # Send device reboot BEFORE setting _active=False — the probe loop's
        # finally block clears _serial_listener once _active is False.
        self._graceful_device_shutdown()
        with self._lock:
            # Invalidate every constructor generation even when no interface
            # or candidate has been published yet. A third-party
            # SerialInterface constructor can return after stop() has already
            # inspected both fields; that late result must be fenced and
            # closed, never published by either ownership mode.
            self._active = False
            self._serial_open_generation += 1
            self._lxmf_destinations.clear()
        self._serial_reconnect_requested.set()
        self._save_node_data_cache()
        self._save_name_cache()
        # Close persistent serial listener (if any) before joining threads
        try:
            with self._lock:
                listener = self._serial_listener
                listener_generation = self._serial_active_generation
                candidate = self._serial_probe_candidate
                self._serial_listener = None
                self._serial_probe_candidate = None
                if self._fw_verified_serial_generation == listener_generation:
                    self._fw_verified_serial_generation = None
            if listener is not None:
                self._bounded_close_serial_interface(
                    listener,
                    listener_generation,
                    "shutdown listener close",
                )
            if candidate is not None and candidate[0] is not listener:
                self._bounded_close_serial_interface(
                    candidate[0],
                    candidate[1],
                    "shutdown unpublished-interface close",
                )
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
        self._release_serial_device_lease()

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
        max_attempts = self.config.get("max_reconnect_attempts", 0)
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

                if self._mode == MODE_MQTT and has_interface and since_disconnect < grace_period:
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

                if self._mode == MODE_SERIAL and not self._wait_for_recovery_reopen_delay():
                    break

                try:
                    self._connect_mesh_device()
                    self._reconnect_failures = 0
                except SerialOpenError as exc:
                    if not self._active:
                        break
                    self._reconnect_failures += 1
                    self._record_serial_open_failure(exc.result)
                    self.log.warning(
                        "Meshtastic serial connect failed (%d, %s): %s",
                        self._reconnect_failures,
                        exc.result.outcome.value,
                        exc,
                    )
                    self.event_bus.publish(
                        events.MESHTASTIC_CONNECT_FAILED,
                        {
                            "error": str(exc),
                            "attempt": self._reconnect_failures,
                            "open_outcome": exc.result.outcome.value,
                        },
                    )
                    self._sleep_while_active(self._serial_retry_interval)
                    continue
                except Exception as exc:
                    if not self._active:
                        break
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
                    if (
                        self._mode == MODE_MQTT
                        and max_attempts > 0
                        and self._reconnect_failures >= max_attempts
                    ):
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
                    # MQTT gets exponential network backoff. Physical serial
                    # uses its configured fixed retry interval and is never
                    # routed through the MQTT suspension policy.
                    backoff = (
                        min(
                            reconnect_delay * (2 ** min(self._reconnect_failures - 1, 5)),
                            300,
                        )
                        if self._mode == MODE_MQTT
                        else self._serial_retry_interval
                    )
                    self.log.debug("Reconnect backoff: %ds", backoff)
                    self._sleep_while_active(backoff)
                    continue

            # Health check
            monitor_interval = (
                self._firmware_watchdog_monitor_interval(health_check_interval)
                if self._mode == MODE_SERIAL
                else health_check_interval
            )
            self._sleep_while_active(monitor_interval)
            # In serial mode, prove the radio is alive before allowing queued
            # writes to enter Meshtastic's potentially unbounded TX queue.
            if (
                self._connected
                and self._mode == MODE_SERIAL
                and not self._device_probe_port
                and not self._check_firmware_watchdog()
            ):
                self.event_bus.publish(
                    events.MESHTASTIC_DISCONNECTED,
                    {"reason": "firmware_hang"},
                )
                self._close_mesh_interface()
                continue

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
            self._drain_send_queue()
            self._retry_pending_lxmf()

            # Periodic NODEINFO re-announcement (MQTT mode only)
            if self._connected and self._mode == MODE_MQTT:
                try:
                    with self._lock:
                        iface = self._mesh_interface
                    if iface and hasattr(iface, "maybe_send_nodeinfo"):
                        iface.maybe_send_nodeinfo()
                except Exception:
                    self.log.debug("Error sending periodic NODEINFO", exc_info=True)

    def _record_serial_open_failure(self, result: SerialOpenResult) -> None:
        """Count only authenticated constructor timeouts as possible hangs."""

        if not result.reset_eligible:
            if result.outcome is not SerialOpenOutcome.TEARDOWN_UNPROVEN:
                with self._lock:
                    self._fw_consecutive_open_failures = 0
                    self._fw_first_open_failure_time = 0.0
            self._set_fw_state(
                FW_DEGRADED,
                f"serial open blocked: {result.outcome.value}",
            )
            return

        with self._lock:
            self._fw_consecutive_open_failures += 1
            if self._fw_consecutive_open_failures == 1:
                self._fw_first_open_failure_time = time.monotonic()
            failures = self._fw_consecutive_open_failures
        if self._fw_watchdog_enabled and failures >= self._fw_open_failure_threshold:
            self._handle_startup_firmware_hang()
            with self._lock:
                self._fw_consecutive_open_failures = 0
                self._fw_first_open_failure_time = 0.0

    def _connect_mesh_device(self) -> None:
        """Open connection to the Meshtastic network (serial or MQTT)."""
        recovery_epoch: int | None = None
        generation: int | None = None
        if self._mode == MODE_MQTT:
            iface = self._create_mqtt_interface()
        else:
            iface = self._create_serial_interface()

        if self._mode == MODE_SERIAL:
            with self._lock:
                generation = self._serial_active_generation
            if not self._serial_generation_is_current(iface, generation):
                self._poison_serial_generation(iface, generation, "stopped before validation")
                raise RuntimeError("Meshtastic serial generation stopped before validation")
            if not self._bind_or_validate_usb_identity():
                self._poison_serial_generation(iface, generation, "identity mismatch")
                raise RuntimeError("Meshtastic serial device identity mismatch")
            if self._fw_recovery_pending:
                self._set_fw_state(FW_VERIFYING)
                with self._lock:
                    recovery_epoch = self._fw_recovery_epoch
                verification = self._probe_device_health(iface, generation=generation)
                if not verification.verified:
                    self._handle_recovery_verification_failure(
                        iface,
                        generation,
                        recovery_epoch,
                        verification,
                    )
                    raise RuntimeError("Meshtastic radio reopened but failed active verification")

        with self._lock:
            if self._mode == MODE_SERIAL:
                stale_serial = (
                    not self._active
                    or generation != self._serial_active_generation
                    or generation != self._serial_open_generation
                    or self._serial_probe_candidate != (iface, generation)
                )
                if not stale_serial:
                    self._mesh_interface = iface
                    self._serial_probe_candidate = None
                    self._connected = True
                    self._mqtt_suspended = False
                    self._connect_count += 1
                    self._last_serial_activity = time.monotonic()
            else:
                stale_serial = not self._active
                if not stale_serial:
                    self._mesh_interface = iface
                    self._connected = True
                    self._mqtt_suspended = False
                    self._connect_count += 1
                    self._last_mqtt_activity = time.monotonic()
        if stale_serial:
            if self._mode == MODE_SERIAL:
                self._poison_serial_generation(iface, generation, "stale before publication")
            else:
                try:
                    iface.close()
                except Exception:
                    self.log.debug("Error closing stopped MQTT interface", exc_info=True)
            raise RuntimeError("Meshtastic connection became stale before publication")

        if (
            self._mode == MODE_SERIAL
            and recovery_epoch is not None
            and generation is not None
            and self._fw_recovery_pending
        ):
            self._complete_firmware_recovery(recovery_epoch, iface, generation)
        elif self._mode == MODE_SERIAL:
            with self._lock:
                dependency_error = self._fw_dependency_error
                sticky_error = (
                    self._fw_last_recovery_error if self._fw_verification_failure_sticky else None
                )
                if dependency_error is None and sticky_error is None:
                    self._fw_hang_detected = False
                    self._fw_hang_reason = None
                    self._fw_last_recovery_error = None
            if dependency_error is None and sticky_error is None:
                self._set_fw_state(FW_HEALTHY)
            else:
                self._set_fw_state(
                    FW_DEGRADED,
                    (
                        f"health probe dependency mismatch: {dependency_error}"
                        if dependency_error is not None
                        else sticky_error
                    ),
                )

        # API v2 readiness means the physical device or MQTT connection is
        # genuinely usable, not merely that the reconnect worker was started.
        self._mark_ready_with_radio_guard()

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
        """Create a Meshtastic SerialInterface with a bounded constructor."""
        serial_port = self.config.get("serial_port", "/dev/meshtastic")
        self.log.info("Connecting to Meshtastic device (port=%s)...", serial_port)
        result = self._open_serial_interface_result(serial_port)
        if not result.opened:
            raise SerialOpenError(result, serial_port)
        return result.interface

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
        packet_id_allocator = self._mqtt_packet_ids
        if packet_id_allocator is None:
            raise RuntimeError("durable MQTT packet-ID allocator is unavailable")

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
            packet_id_allocator=packet_id_allocator,
            logger=self.log,
            long_name=self._mqtt_long_name or "",
            short_name=self._mqtt_short_name or "",
            tls=tls_cfg if tls_cfg.get("enabled") else None,
            max_nodes=int(self.config.get("mqtt_max_nodes", 1024)),
            node_ttl_seconds=float(self.config.get("mqtt_node_ttl_seconds", 86400)),
            connack_timeout_seconds=float(
                mqtt_cfg.get(
                    "connack_timeout_seconds",
                    _MQTT_CONNACK_TIMEOUT_SECONDS,
                )
            ),
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
        return f"port={self.config.get('serial_port', '/dev/meshtastic')}"

    def _close_mesh_interface(self) -> None:
        """Tear down the Meshtastic MQTT/serial connection.

        Pubsub subscriptions are NOT touched here — they are owned by the
        plugin lifecycle (start/stop) so the serial LoRa listener keeps
        delivering messages even while MQTT is reconnecting.
        """
        with self._lock:
            iface = self._mesh_interface
            was_connected = self._connected
            generation = getattr(self, "_serial_active_generation", 0)
            candidate = self._serial_probe_candidate if self._mode == MODE_SERIAL else None
            self._mesh_interface = None
            self._connected = False
            self._nodes_cache = None
            self._broadcast_cache = None
            if self._mode == MODE_SERIAL:
                # Invalidate before close(). A late callback or health response
                # from this object can no longer restore current state.
                self._serial_probe_candidate = None
                if self._fw_verified_serial_generation == generation:
                    self._fw_verified_serial_generation = None
                self._serial_open_generation += 1
                self._cached_device_info = None
                self._cached_lora_neighbors = []
        if was_connected:
            self.mark_degraded("Meshtastic connection closed")
        if iface is not None:
            if self._mode == MODE_SERIAL:
                self._bounded_close_serial_interface(iface, generation, "primary close")
            else:
                try:
                    iface.close()
                except Exception:
                    self.log.debug("Error closing Meshtastic interface", exc_info=True)
        if candidate is not None and candidate[0] is not iface:
            self._bounded_close_serial_interface(
                candidate[0],
                candidate[1],
                "unpublished primary close",
            )

    def _ensure_serial_runtime_state(self) -> None:
        """Initialize bounded-I/O state for legacy directly-built test instances."""

        if not hasattr(self, "_mode"):
            self._mode = self.config.get("mode", MODE_SERIAL)
        if not hasattr(self, "_mesh_interface"):
            self._mesh_interface = None
        if not hasattr(self, "_serial_listener"):
            self._serial_listener = None
        if not hasattr(self, "_serial_probe_candidate"):
            self._serial_probe_candidate = None
        if not hasattr(self, "_serial_worker_lock"):
            self._serial_worker_lock = threading.Lock()
        if not hasattr(self, "_serial_open_attempt_lock"):
            self._serial_open_attempt_lock = threading.Lock()
        if not hasattr(self, "_serial_open_attempt_active"):
            self._serial_open_attempt_active = False
        if not hasattr(self, "_serial_open_workers"):
            self._serial_open_workers = []
        if not hasattr(self, "_serial_operation_lock"):
            self._serial_operation_lock = threading.Lock()
        if not hasattr(self, "_serial_operation_workers"):
            self._serial_operation_workers = []
        if not hasattr(self, "_serial_close_workers"):
            self._serial_close_workers = []
        if not hasattr(self, "_serial_close_inflight"):
            self._serial_close_inflight = set()
        if not hasattr(self, "_serial_closed_interfaces"):
            self._serial_closed_interfaces = set()
        if not hasattr(self, "_serial_unclosed_interfaces"):
            self._serial_unclosed_interfaces = {}
        if not hasattr(self, "_abandoned_serial_threads"):
            self._abandoned_serial_threads = []
        if not hasattr(self, "_serial_close_timeout"):
            self._serial_close_timeout = 2.0
        if not hasattr(self, "_serial_command_timeout"):
            self._serial_command_timeout = 5.0
        if not hasattr(self, "_serial_teardown_unproven"):
            self._serial_teardown_unproven = False
        if not hasattr(self, "_serial_lease_release_worker"):
            self._serial_lease_release_worker: threading.Thread | None = None

    def _begin_serial_open_attempt(self) -> bool:
        """Claim the sole constructor attempt before any SDK code can run."""

        self._ensure_serial_runtime_state()
        if not self._serial_open_attempt_lock.acquire(blocking=False):
            return False
        with self._serial_worker_lock:
            self._serial_open_attempt_active = True
        return True

    def _finish_serial_open_attempt(self) -> None:
        """Release a constructor claim exactly once, including from a late worker."""

        self._ensure_serial_runtime_state()
        release = False
        with self._serial_worker_lock:
            if self._serial_open_attempt_active:
                self._serial_open_attempt_active = False
                release = True
        if release:
            self._serial_open_attempt_lock.release()

    def _prune_serial_workers(self, *, ignore_open_attempt: bool = False) -> bool:
        """Return true only when no old work can still own the serial device."""

        self._ensure_serial_runtime_state()
        with self._serial_worker_lock:
            self._serial_open_workers = [
                worker for worker in self._serial_open_workers if worker.is_alive()
            ]
            self._abandoned_serial_threads = [
                worker for worker in self._abandoned_serial_threads if worker.is_alive()
            ]
            self._serial_operation_workers = [
                worker for worker in self._serial_operation_workers if worker.is_alive()
            ]
            self._serial_close_workers = [
                worker for worker in self._serial_close_workers if worker.is_alive()
            ]
            workers_alive = bool(
                self._serial_open_workers
                or self._abandoned_serial_threads
                or self._serial_operation_workers
                or self._serial_close_workers
            )
            open_attempt_active = self._serial_open_attempt_active and not ignore_open_attempt
            unresolved_close = bool(self._serial_unclosed_interfaces)
        adapter = getattr(self, "_meshtastic_health", None)
        adapter_busy = bool(adapter is not None and adapter.has_inflight())
        with self._lock:
            unpublished_interface = self._serial_probe_candidate is not None
            published_interface = self._serial_listener is not None or (
                self._mode == MODE_SERIAL and self._mesh_interface is not None
            )
        quiescent = not (
            workers_alive
            or adapter_busy
            or open_attempt_active
            or unresolved_close
            or unpublished_interface
            or published_interface
        )
        if quiescent:
            self._serial_teardown_unproven = False
        return quiescent

    def _bounded_close_serial_interface(
        self,
        iface: Any,
        generation: int,
        reason: str,
    ) -> bool:
        """Close a third-party serial interface without blocking lifecycle threads."""

        self._ensure_serial_runtime_state()
        close_key = (id(iface), generation)
        with self._serial_worker_lock:
            if close_key in self._serial_closed_interfaces:
                return True
            if close_key in self._serial_unclosed_interfaces:
                self._serial_teardown_unproven = True
                return False
            if close_key in self._serial_close_inflight:
                self._serial_teardown_unproven = True
                return False
            self._serial_close_inflight.add(close_key)

        result = {"closed": False, "error": None}

        def worker() -> None:
            try:
                iface.close()
                result["closed"] = True
                with self._serial_worker_lock:
                    # Teardown is an exact-once ownership transition. Several
                    # lifecycle paths can race to invalidate the same object;
                    # a later path must not call third-party close() again and
                    # turn an already-proven teardown into a false quarantine.
                    self._serial_closed_interfaces.add(close_key)
            except BaseException as exc:
                result["error"] = exc
                with self._serial_worker_lock:
                    # Keep the exact object alive. A raised close() call is not
                    # evidence that its fd or reader thread was released.
                    self._serial_unclosed_interfaces[close_key] = iface
                self.log.debug("Error during bounded Meshtastic close", exc_info=True)
            finally:
                with self._serial_worker_lock:
                    self._serial_close_inflight.discard(close_key)

        close_worker = threading.Thread(
            target=worker,
            name=f"meshtastic-close-g{generation}",
            daemon=True,
        )
        with self._serial_worker_lock:
            self._serial_close_workers.append(close_worker)
        try:
            close_worker.start()
        except RuntimeError:
            with self._serial_worker_lock:
                if close_worker in self._serial_close_workers:
                    self._serial_close_workers.remove(close_worker)
                self._serial_close_inflight.discard(close_key)
            self._serial_teardown_unproven = True
            return False
        close_worker.join(timeout=self._serial_close_timeout)
        if close_worker.is_alive():
            self._serial_teardown_unproven = True
            self.mark_degraded("Meshtastic serial teardown is unproven; device lease retained")
            self.log.error(
                "Meshtastic %s exceeded %.1fs; refusing serial reuse until it exits",
                reason,
                self._serial_close_timeout,
            )
            return False
        if not result["closed"]:
            self._serial_teardown_unproven = True
            self.mark_degraded("Meshtastic serial close failed; exact handle quarantined")
            self.log.error(
                "Meshtastic %s raised %s; refusing serial reuse for this process",
                reason,
                type(result["error"]).__name__ if result["error"] is not None else "an error",
            )
            return False
        self._prune_serial_workers()
        return True

    def _poison_serial_generation(self, iface: Any, generation: int, reason: str) -> None:
        """Detach an uncertain interface generation before initiating teardown."""

        detached = False
        with self._lock:
            if self._mesh_interface is iface:
                self._mesh_interface = None
                self._connected = False
                detached = True
            if self._serial_listener is iface:
                self._serial_listener = None
                detached = True
            if self._serial_probe_candidate == (iface, generation):
                self._serial_probe_candidate = None
                detached = True
            if getattr(self, "_fw_verified_serial_generation", None) == generation:
                self._fw_verified_serial_generation = None
            if (
                generation == self._serial_active_generation
                and generation == self._serial_open_generation
            ):
                self._serial_open_generation += 1
                detached = True
        # Even if another thread invalidated the generation first, this exact
        # object may not have been published where that thread could close it.
        # Always prove teardown (or quarantine the handle) before allowing reuse.
        self.mark_degraded(f"Meshtastic serial generation invalidated: {reason}")
        self._bounded_close_serial_interface(iface, generation, reason)
        if detached:
            self._serial_reconnect_requested.set()

    def _run_serial_command(
        self,
        iface: Any,
        generation: int,
        operation: str,
        callback: Callable[[], Any],
        *,
        timeout: float | None = None,
        poison_on_timeout: bool = True,
    ) -> _SerialCommandResult:
        """Run one serial operation with single-flight and generation fencing.

        A timed-out call may already have reached the radio, so callers must
        never retry it automatically. The old generation is poisoned and its
        lease remains held until every worker and close operation has exited.
        """

        self._ensure_serial_runtime_state()
        if not self._serial_generation_is_current(iface, generation):
            return _SerialCommandResult(_SerialCommandOutcome.STALE)
        if not self._serial_operation_lock.acquire(blocking=False):
            return _SerialCommandResult(_SerialCommandOutcome.BUSY)

        # Meshtastic's private admin send is not cancellable.  The adapter's
        # bounded waiter can time out while its worker is still inside the SDK;
        # in that state the outer command lock has been released, but starting
        # another serial write could corrupt SDK state or duplicate I/O.  Health
        # probes may rejoin their existing correlated flight; every other SDK
        # operation must wait for that exact flight to terminate.  An
        # identity-bound USB reset does not use this gate and remains available
        # to break a genuinely stuck radio.
        health_adapter = getattr(self, "_meshtastic_health", None)
        if (
            operation != "health-probe"
            and health_adapter is not None
            and health_adapter.has_inflight()
        ):
            self._serial_operation_lock.release()
            return _SerialCommandResult(_SerialCommandOutcome.BUSY)

        result: dict[str, Any] = {
            "value": None,
            "error": None,
            "started": False,
            "stale": False,
        }
        done = threading.Event()

        def worker() -> None:
            try:
                if not self._serial_generation_is_current(iface, generation):
                    return
                result["started"] = True
                value = callback()
                # The SDK call may have blocked while stop/recovery replaced
                # this exact interface generation. Its return is then delivery-
                # uncertain and must not be committed as success by the caller.
                if not self._serial_generation_is_current(iface, generation):
                    result["stale"] = True
                    return
                result["value"] = value
            except BaseException as exc:
                result["error"] = exc
            finally:
                self._serial_operation_lock.release()
                done.set()

        command_worker = threading.Thread(
            target=worker,
            name=f"meshtastic-{operation}-g{generation}",
            daemon=True,
        )
        with self._serial_worker_lock:
            self._serial_operation_workers.append(command_worker)
        try:
            command_worker.start()
        except RuntimeError as exc:
            self._serial_operation_lock.release()
            with self._serial_worker_lock:
                self._serial_operation_workers.remove(command_worker)
            return _SerialCommandResult(_SerialCommandOutcome.ERROR, error=exc)

        wait_timeout = self._serial_command_timeout if timeout is None else timeout
        if not done.wait(timeout=max(0.001, float(wait_timeout))):
            self._serial_teardown_unproven = True
            if poison_on_timeout:
                self._poison_serial_generation(
                    iface,
                    generation,
                    f"{operation} timed out",
                )
            return _SerialCommandResult(
                _SerialCommandOutcome.TIMEOUT,
                started=bool(result["started"]),
            )

        self._prune_serial_workers()
        if not result["started"]:
            return _SerialCommandResult(_SerialCommandOutcome.STALE)
        if result["stale"]:
            return _SerialCommandResult(_SerialCommandOutcome.STALE, started=True)
        if result["error"] is not None:
            return _SerialCommandResult(
                _SerialCommandOutcome.ERROR,
                error=result["error"],
                started=True,
            )
        return _SerialCommandResult(
            _SerialCommandOutcome.SUCCESS,
            value=result["value"],
            started=True,
        )

    def _physical_serial_port(self) -> str | None:
        """Return the explicitly configured physical Meshtastic device path."""

        port = self._device_probe_port or self.config.get("serial_port", "/dev/meshtastic")
        if not port or port == "auto":
            return None
        return str(port)

    def _ensure_serial_device_lease(
        self,
        port: str | None = None,
        *,
        ignore_open_attempt: bool = False,
    ) -> bool:
        """Hold an exclusive claim on the exact physical serial endpoint."""

        self._serial_lease_failure = None
        configured = port or self._physical_serial_port()
        if not configured:
            self._serial_lease_failure = SerialOpenOutcome.IDENTITY
            return False

        lease = getattr(self, "_serial_device_lease", None)
        if lease is not None:
            try:
                lease.revalidate()
                return True
            except SerialDeviceChangedError:
                # Normal USB re-enumeration may change tty minor/canonical path.
                # Release the stale endpoint, reclaim the newly resolved one,
                # then enforce the persistent VID/PID/serial binding below.
                with self._lock:
                    published_interface = self._serial_listener or (
                        self._mesh_interface if self._mode == MODE_SERIAL else None
                    )
                if published_interface is not None or not self._prune_serial_workers(
                    ignore_open_attempt=ignore_open_attempt
                ):
                    self._serial_lease_failure = SerialOpenOutcome.TEARDOWN_UNPROVEN
                    return False
                lease.release()
                self._serial_device_lease = None
            except SerialDeviceIdentityError:
                self._serial_lease_failure = SerialOpenOutcome.IDENTITY
                return False
            except Exception:
                self.log.warning("Meshtastic serial lease revalidation failed", exc_info=True)
                self._serial_lease_failure = SerialOpenOutcome.ERROR
                return False

        # A worker from an invalidated generation may still own an fd or be
        # inside the optional library. Reusing the endpoint before teardown is
        # proven would defeat pyserial's exclusive-open guarantee. An already
        # held and revalidated lease may still authorize a USB reset intended
        # to break that stuck operation.
        if not self._prune_serial_workers(ignore_open_attempt=ignore_open_attempt):
            self._serial_lease_failure = SerialOpenOutcome.TEARDOWN_UNPROVEN
            return False

        try:
            self._serial_device_lease = serial_device_registry.claim(
                configured,
                self.plugin_name,
            )
        except SerialDeviceBusyError as exc:
            self.log.warning("Cannot claim Meshtastic serial device %s: %s", configured, exc)
            self._serial_lease_failure = SerialOpenOutcome.BUSY
            return False
        except SerialDeviceIdentityError as exc:
            self.log.warning("Cannot claim Meshtastic serial device %s: %s", configured, exc)
            self._serial_lease_failure = SerialOpenOutcome.IDENTITY
            return False
        except PermissionError as exc:
            self.log.warning("Cannot claim Meshtastic serial device %s: %s", configured, exc)
            self._serial_lease_failure = SerialOpenOutcome.PERMISSION
            return False
        except FileNotFoundError as exc:
            self.log.warning("Cannot claim Meshtastic serial device %s: %s", configured, exc)
            self._serial_lease_failure = SerialOpenOutcome.MISSING
            return False
        except OSError as exc:
            self.log.warning("Cannot claim Meshtastic serial device %s: %s", configured, exc)
            self._serial_lease_failure = self._classify_serial_open_exception(exc)
            return False
        except Exception as exc:
            self.log.warning("Cannot claim Meshtastic serial device %s: %s", configured, exc)
            self._serial_lease_failure = SerialOpenOutcome.ERROR
            return False
        valid = self._bind_or_validate_usb_identity(already_claimed=True)
        if not valid:
            self._serial_lease_failure = SerialOpenOutcome.IDENTITY
        return valid

    def _release_serial_device_lease(self) -> None:
        if not self._prune_serial_workers():
            self._serial_teardown_unproven = True
            with self._serial_worker_lock:
                unresolved = len(self._serial_unclosed_interfaces)
            self.log.warning(
                "Retaining Meshtastic serial-device lease while old-generation work remains"
            )
            if unresolved:
                self.log.error(
                    "Retaining Meshtastic serial-device lease for %d exact handle(s) "
                    "whose close failed; process restart is required",
                    unresolved,
                )
            else:
                self._schedule_serial_lease_release()
            return
        lease = getattr(self, "_serial_device_lease", None)
        self._serial_device_lease = None
        if lease is not None:
            lease.release()

    def _schedule_serial_lease_release(self) -> None:
        """Release a retained lease only after every abandoned worker exits."""

        self._ensure_serial_runtime_state()
        current = self._serial_lease_release_worker
        if current is not None and current.is_alive():
            return

        def worker() -> None:
            while not self._prune_serial_workers():
                time.sleep(0.1)
            with self._lock:
                if getattr(self, "_active", False):
                    return
                lease = getattr(self, "_serial_device_lease", None)
                if lease is None:
                    return
                self._serial_device_lease = None
            try:
                lease.release()
            except Exception:
                self.log.debug("Deferred serial lease release failed", exc_info=True)

        release_worker = threading.Thread(
            target=worker,
            name="meshtastic-lease-release",
            daemon=True,
        )
        self._serial_lease_release_worker = release_worker
        release_worker.start()

    @staticmethod
    def _normalize_usb_identity_mapping(identity: dict[str, Any]) -> dict[str, str]:
        """Normalize hexadecimal IDs while preserving exact stable identifiers."""

        return {
            str(key): (
                str(value).strip().lower()
                if key in {"vendor_id", "product_id"}
                else str(value).strip()
            )
            for key, value in identity.items()
            if value is not None
        }

    @staticmethod
    def _usb_identity_mapping(lease: SerialDeviceLease) -> dict[str, str] | None:
        usb = lease.identity.usb
        if usb is None:
            return None
        result = {
            "vendor_id": usb.vendor_id.lower(),
            "product_id": usb.product_id.lower(),
        }
        if usb.serial_number:
            # USB serial strings and sysfs paths are stable identifiers, not
            # hexadecimal numbers. Preserve their exact case so two distinct
            # Linux identities cannot collapse to one authorization value.
            result["serial"] = usb.serial_number.strip()
        else:
            # Devices without a serial number are bound to the physical port.
            result["sysfs_path"] = usb.sysfs_path.strip()
        return result

    @staticmethod
    def _configured_identity_authorizes_rebind(
        expected: dict[str, str],
        current: dict[str, str] | None,
    ) -> bool:
        """Require an operator-supplied complete identity for durable rebind."""

        if current is None:
            return False
        stable_key = "serial" if current.get("serial") else "sysfs_path"
        required = {"vendor_id", "product_id", stable_key}
        return required.issubset(expected) and all(
            expected.get(key) == current.get(key) for key in required
        )

    def _bind_or_validate_usb_identity(self, *, already_claimed: bool = False) -> bool:
        """Bind first use and reject a later symlink/device substitution."""

        if not already_claimed and not self._ensure_serial_device_lease():
            return False
        lease = self._serial_device_lease
        if lease is None:
            return False
        try:
            current_lease_identity = lease.revalidate()
        except Exception:
            self.log.warning("Meshtastic serial identity changed during validation", exc_info=True)
            return False
        current = self._usb_identity_mapping(lease)
        expected = getattr(self, "_fw_expected_usb_identity", {})
        if expected:
            if current is None or any(current.get(key) != value for key, value in expected.items()):
                self.log.error(
                    "Meshtastic USB identity does not match configured expectation: expected=%s current=%s",
                    expected,
                    current,
                )
                return False

        bound = getattr(self, "_fw_bound_usb_identity", None)
        if bound:
            if current is None or any(current.get(key) != value for key, value in bound.items()):
                if self._configured_identity_authorizes_rebind(expected, current):
                    limiter = getattr(self, "_fw_reset_limiter", None)
                    if limiter is None or not limiter.set_metadata("device_identity", current):
                        self.log.error(
                            "Could not transactionally persist authorized Meshtastic USB "
                            "identity rebind"
                        )
                        return False
                    self._fw_bound_usb_identity = current
                    self.log.warning(
                        "Authorized Meshtastic USB identity rebind from %s to %s; "
                        "reset history was preserved",
                        bound,
                        current,
                    )
                    return current_lease_identity is not None
                self.log.error(
                    "Meshtastic USB identity changed from durable binding: expected=%s current=%s",
                    bound,
                    current,
                )
                return False
            return True

        if current is None:
            if getattr(self, "_fw_usb_power_cycle", False):
                self.log.error("Hard reset requires a resolvable USB device identity")
                return False
            return True

        limiter = getattr(self, "_fw_reset_limiter", None)
        if limiter is None or not limiter.set_metadata("device_identity", current):
            self.log.error("Could not persist Meshtastic USB identity binding")
            return False
        self._fw_bound_usb_identity = current
        self.log.info(
            "Bound Meshtastic serial endpoint to USB identity %s (%s:%s)",
            current.get("serial") or current.get("sysfs_path"),
            current["vendor_id"],
            current["product_id"],
        )
        return current_lease_identity is not None

    # ── Device reset ────────────────────────────────────────────

    def _resolve_usb_device_path(self) -> str | None:
        """Resolve the USB bus device path from the serial port sysfs tree.

        Returns e.g. '/dev/bus/usb/004/016' or None if resolution fails.
        """
        if not self._bind_or_validate_usb_identity():
            return None
        try:
            lease = self._serial_device_lease
            if lease is None or lease.identity.usb is None:
                return None
            return self._usb_bus_path_for_lease(lease)
        except Exception:
            return None

    @staticmethod
    def _usb_bus_path_for_lease(lease: SerialDeviceLease) -> str | None:
        usb = lease.identity.usb
        if usb is None:
            return None
        with open(os.path.join(usb.sysfs_path, "busnum")) as handle:
            busnum = int(handle.read().strip())
        with open(os.path.join(usb.sysfs_path, "devnum")) as handle:
            devnum = int(handle.read().strip())
        return f"/dev/bus/usb/{busnum:03d}/{devnum:03d}"

    def _usb_bus_reset(self, usb_path: str) -> dict[str, Any]:
        """Issue a USBDEVFS_RESET ioctl on the USB bus device."""
        import fcntl

        if not self._bind_or_validate_usb_identity():
            return {
                "ok": False,
                "reason": "serial device identity or ownership changed before USB reset",
            }
        try:
            lease = self._serial_device_lease
            current_usb_path = self._usb_bus_path_for_lease(lease) if lease is not None else None
        except (OSError, TypeError, ValueError):
            current_usb_path = None
        if current_usb_path is None or os.path.normpath(usb_path) != current_usb_path:
            return {
                "ok": False,
                "reason": "USB bus address changed before reset",
            }
        USBDEVFS_RESET = 0x5514
        fd = None
        try:
            fd = os.open(current_usb_path, os.O_WRONLY)
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
        """Start the same verified recovery lifecycle used by the watchdog.

        The API response acknowledges only that a recovery actuator was issued.
        ``MESHTASTIC_FIRMWARE_RECOVERED`` is emitted later, after the radio was
        reopened and answered a correlated local metadata request.
        """

        if self._physical_serial_port() is None:
            return {"ok": False, "reason": "No explicit physical serial port configured"}
        with self._lock:
            if self._fw_recovery_pending or getattr(self, "_fw_recovery_attempting", False):
                return {
                    "ok": False,
                    "reason": "Recovery already in progress",
                    "state": self._fw_recovery_state,
                }
        if not self._attempt_firmware_recovery("manual"):
            with self._lock:
                recovery_in_progress = self._fw_recovery_pending or getattr(
                    self, "_fw_recovery_attempting", False
                )
                state = self._fw_recovery_state
            if recovery_in_progress:
                return {
                    "ok": False,
                    "reason": "Recovery already in progress",
                    "state": state,
                }
            return {"ok": False, "reason": "All configured reset methods failed"}

        self._cleanup_after_reset()
        with self._lock:
            state = self._fw_recovery_state
            method = self._fw_recovery_method
        return {
            "ok": True,
            "accepted": True,
            "verified": False,
            "state": state,
            "method": method,
        }

    def _cleanup_after_reset(self) -> None:
        """Detach the stale generation and wake its owning reconnect loop."""
        with self._lock:
            listener = self._serial_listener
            generation = self._serial_active_generation
            candidate = self._serial_probe_candidate
            self._serial_listener = None
            self._serial_probe_candidate = None
            if self._fw_verified_serial_generation == generation:
                self._fw_verified_serial_generation = None
            self._cached_device_info = None
            self._cached_lora_neighbors = []
            self._serial_open_generation += 1
        if listener is not None:
            self._bounded_close_serial_interface(listener, generation, "post-reset close")
        if candidate is not None and candidate[0] is not listener:
            self._bounded_close_serial_interface(
                candidate[0],
                candidate[1],
                "post-reset unpublished-interface close",
            )
        if self._mode == MODE_SERIAL:
            self._close_mesh_interface()
        self._serial_reconnect_requested.set()

    # ── Device info probe (for dashboard device card) ────────────

    @staticmethod
    def _classify_serial_open_exception(exc: BaseException) -> SerialOpenOutcome:
        if isinstance(exc, PermissionError):
            return SerialOpenOutcome.PERMISSION
        if isinstance(exc, FileNotFoundError):
            return SerialOpenOutcome.MISSING
        if isinstance(exc, OSError):
            if exc.errno in {errno.EACCES, errno.EPERM}:
                return SerialOpenOutcome.PERMISSION
            if exc.errno in {errno.ENOENT, errno.ENODEV, errno.ENXIO}:
                return SerialOpenOutcome.MISSING
            if exc.errno in {errno.EBUSY, errno.EAGAIN}:
                return SerialOpenOutcome.BUSY
        description = f"{type(exc).__name__}: {exc}".lower()
        if any(marker in description for marker in ("resource busy", "in use", "exclusive")):
            return SerialOpenOutcome.BUSY
        if "permission denied" in description:
            return SerialOpenOutcome.PERMISSION
        if any(marker in description for marker in ("no such file", "not found", "no such device")):
            return SerialOpenOutcome.MISSING
        return SerialOpenOutcome.ERROR

    def _open_serial_interface_result(self, port: str | None = None) -> SerialOpenResult:
        """Open one serial generation, returning a typed and bounded result."""

        import meshtastic.serial_interface

        self._ensure_serial_runtime_state()
        configured_port = port if port is not None else self._device_probe_port
        if not configured_port:
            configured_port = self._physical_serial_port() or ""
        if not configured_port:
            open_result = SerialOpenResult(SerialOpenOutcome.IDENTITY)
            self._last_serial_open_result = open_result
            return open_result
        if not self._begin_serial_open_attempt():
            self._serial_teardown_unproven = True
            open_result = SerialOpenResult(SerialOpenOutcome.TEARDOWN_UNPROVEN)
            self._last_serial_open_result = open_result
            return open_result

        # The normal caller releases the constructor claim. After a timeout or
        # stale result, ownership transfers to the worker until exact teardown
        # either succeeds or quarantines the returned interface.
        release_by_worker = False
        worker_started = False
        owner_decision = threading.Event()
        try:
            if not self._ensure_serial_device_lease(
                configured_port,
                ignore_open_attempt=True,
            ):
                outcome = self._serial_lease_failure or SerialOpenOutcome.IDENTITY
                open_result = SerialOpenResult(outcome)
                self._last_serial_open_result = open_result
                return open_result

            # Strict fail-closed behavior: no new constructor while any previous
            # command/open/close, unpublished interface, or health worker may
            # still own the endpoint. Ignore only this call's constructor claim.
            if not self._prune_serial_workers(ignore_open_attempt=True):
                self._serial_teardown_unproven = True
                self.mark_degraded(
                    "Meshtastic serial teardown unproven; refusing a replacement open"
                )
                open_result = SerialOpenResult(SerialOpenOutcome.TEARDOWN_UNPROVEN)
                self._last_serial_open_result = open_result
                return open_result

            with self._lock:
                published = self._serial_listener or (
                    self._mesh_interface if self._mode == MODE_SERIAL else None
                )
                if published is not None or self._serial_probe_candidate is not None:
                    self._serial_teardown_unproven = True
                    open_result = SerialOpenResult(SerialOpenOutcome.TEARDOWN_UNPROVEN)
                    self._last_serial_open_result = open_result
                    return open_result
                self._serial_open_generation += 1
                generation = self._serial_open_generation

            timeout_s = self._device_probe_open_timeout
            result: dict[str, Any] = {"iface": None, "error": None}
            cancelled = threading.Event()
            constructor_done = threading.Event()

            def worker() -> None:
                iface = None
                try:
                    iface = meshtastic.serial_interface.SerialInterface(
                        devPath=configured_port,
                        timeout=max(1, int(timeout_s + 0.999)),
                    )
                    result["iface"] = iface
                except BaseException as exc:
                    result["error"] = exc
                finally:
                    # Do not race the owner between "constructor returned" and
                    # "timeout was declared". The owner first publishes a live
                    # candidate or irrevocably cancels this generation.
                    constructor_done.set()
                    owner_decision.wait()
                    with self._lock:
                        stale_generation = (
                            not self._active or generation != self._serial_open_generation
                        )
                    if (cancelled.is_set() or stale_generation) and iface is not None:
                        self._bounded_close_serial_interface(
                            iface,
                            generation,
                            "abandoned serial open",
                        )
                    if cancelled.is_set():
                        self._finish_serial_open_attempt()

            open_worker = threading.Thread(
                target=worker,
                name=f"meshtastic-serial-open-g{generation}",
                daemon=True,
            )
            with self._serial_worker_lock:
                # Track before start so stop()/lease release can never observe
                # an SDK constructor in progress as quiescent.
                self._serial_open_workers.append(open_worker)
            try:
                open_worker.start()
                worker_started = True
            except RuntimeError as exc:
                with self._serial_worker_lock:
                    if open_worker in self._serial_open_workers:
                        self._serial_open_workers.remove(open_worker)
                open_result = SerialOpenResult(
                    SerialOpenOutcome.ERROR,
                    error=exc,
                    generation=generation,
                )
                self._last_serial_open_result = open_result
                return open_result

            if not constructor_done.wait(timeout=timeout_s):
                cancelled.set()
                with self._lock:
                    if generation == self._serial_open_generation:
                        self._serial_open_generation += 1
                with self._serial_worker_lock:
                    self._abandoned_serial_threads.append(open_worker)
                self._serial_teardown_unproven = True
                release_by_worker = True
                owner_decision.set()
                self.log.warning(
                    "Meshtastic serial open on %s timed out after %.0fs; "
                    "the lease remains held and no replacement will start until "
                    "the abandoned worker proves exact teardown",
                    configured_port,
                    timeout_s,
                )
                open_result = SerialOpenResult(
                    SerialOpenOutcome.TIMEOUT,
                    generation=generation,
                )
                self._last_serial_open_result = open_result
                return open_result

            error = result["error"]
            if error is not None:
                outcome = self._classify_serial_open_exception(error)
                self.log.warning(
                    "Meshtastic serial open on %s failed (%s): %s",
                    configured_port,
                    outcome.value,
                    error,
                )
                open_result = SerialOpenResult(outcome, error=error, generation=generation)
                self._last_serial_open_result = open_result
                return open_result

            iface = result["iface"]
            with self._lock:
                stale_generation = not self._active or generation != self._serial_open_generation
                if not stale_generation and iface is not None:
                    self._serial_active_generation = generation
                    # Keep the just-opened object visible to generation fencing
                    # and reuse checks until its caller publishes it.
                    self._serial_probe_candidate = (iface, generation)
            if stale_generation or iface is None:
                cancelled.set()
                release_by_worker = True
                owner_decision.set()
                open_result = SerialOpenResult(
                    SerialOpenOutcome.TEARDOWN_UNPROVEN,
                    generation=generation,
                )
                self._last_serial_open_result = open_result
                return open_result

            open_result = SerialOpenResult(
                SerialOpenOutcome.OPENED,
                interface=iface,
                generation=generation,
            )
            self._last_serial_open_result = open_result
            return open_result
        finally:
            if worker_started:
                owner_decision.set()
            if not release_by_worker:
                self._finish_serial_open_attempt()

    def _open_serial_interface_with_timeout(self, port: str | None = None) -> Any | None:
        """Compatibility wrapper returning the opened interface or ``None``."""

        return self._open_serial_interface_result(port).interface

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
            if not self._wait_for_recovery_reopen_delay():
                break
            open_result = self._open_serial_interface_result()
            if not open_result.opened:
                self._record_serial_open_failure(open_result)
                with self._lock:
                    consecutive_failures = self._fw_consecutive_open_failures
                self.log.warning(
                    "Serial open blocked (%s; %d/%d timeout failures)",
                    open_result.outcome.value,
                    consecutive_failures,
                    self._fw_open_failure_threshold,
                )
                self._sleep_while_active(self._serial_retry_interval)
                continue
            iface = open_result.interface
            generation = open_result.generation
            if generation is None:
                self._bounded_close_serial_interface(iface, 0, "missing generation")
                self._set_fw_state(FW_DEGRADED, "opened serial interface has no generation")
                continue

            if not self._serial_generation_is_current(iface, generation):
                self._poison_serial_generation(iface, generation, "stopped before validation")
                break

            if not self._bind_or_validate_usb_identity():
                self._poison_serial_generation(iface, generation, "identity mismatch")
                self._set_fw_state(FW_DEGRADED, "serial device identity mismatch")
                self._sleep_while_active(self._serial_retry_interval)
                continue

            recovery_epoch: int | None = None
            if self._fw_watchdog_enabled:
                self._set_fw_state(FW_VERIFYING)
                if self._fw_recovery_pending:
                    with self._lock:
                        recovery_epoch = self._fw_recovery_epoch
                verification = self._probe_device_health(iface, generation=generation)
                if not verification.verified:
                    if recovery_epoch is not None:
                        self._handle_recovery_verification_failure(
                            iface,
                            generation,
                            recovery_epoch,
                            verification,
                        )
                    else:
                        detail = (
                            "initial physical-radio verification failed: "
                            f"{verification.outcome.value}:{verification.detail}"
                        )
                        self._poison_serial_generation(iface, generation, detail)
                        self._set_fw_state(FW_DEGRADED, detail)
                    self._sleep_while_active(self._serial_retry_interval)
                    continue
                with self._lock:
                    self._last_fw_probe_time = time.monotonic()
                    self._last_serial_activity = self._last_fw_probe_time
                    self._fw_last_verified_at = time.time()
                    self._fw_verified_serial_generation = generation
                    self._fw_hang_detected = False
                    self._fw_hang_reason = None
                    self._fw_last_recovery_error = None
                    self._fw_verification_failure_sticky = False

            # Publish only after the opened generation has passed identity and
            # active-health verification. Pubsub packets received during the
            # constructor/probe window are intentionally not trusted.
            with self._lock:
                stale_serial = (
                    not self._active
                    or generation != self._serial_active_generation
                    or generation != self._serial_open_generation
                    or self._serial_probe_candidate != (iface, generation)
                )
                if not stale_serial:
                    self._serial_listener = iface
                    self._serial_probe_candidate = None
                    self._fw_consecutive_open_failures = 0
                    self._fw_first_open_failure_time = 0.0
            if stale_serial:
                self._poison_serial_generation(iface, generation, "stale before publication")
                self._sleep_while_active(self._serial_retry_interval)
                continue
            self.log.info(
                "Serial listener active on %s — receiving LoRa messages",
                self._device_probe_port,
            )

            consecutive_failures = 0
            with self._lock:
                self._last_serial_activity = time.monotonic()
            if recovery_epoch is not None and self._fw_recovery_pending:
                self._complete_firmware_recovery(recovery_epoch, iface, generation)
            else:
                with self._lock:
                    dependency_error = self._fw_dependency_error
                    sticky_error = (
                        self._fw_last_recovery_error
                        if self._fw_verification_failure_sticky
                        else None
                    )
                    if dependency_error is None and sticky_error is None:
                        self._fw_hang_detected = False
                        self._fw_hang_reason = None
                        self._fw_last_recovery_error = None
                if dependency_error is None and sticky_error is None:
                    self._set_fw_state(FW_HEALTHY)
                else:
                    self._set_fw_state(
                        FW_DEGRADED,
                        (
                            f"health probe dependency mismatch: {dependency_error}"
                            if dependency_error is not None
                            else sticky_error
                        ),
                    )
            self._mark_ready_with_radio_guard()
            try:
                # Inner loop: refresh device info from the LIVE interface
                # without closing it.  The meshtastic library's reader
                # thread updates iface.nodesByNum / iface.myInfo as
                # packets arrive, so each read sees fresh state.
                while self._active:
                    try:
                        info = self._read_device_info_from_interface(iface)
                        neighbors = self._extract_lora_neighbors(iface)

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

                    if self._wait_for_serial_wake(self._device_probe_monitor_interval()):
                        self.log.info("Serial listener reconnect requested")
                        break
            finally:
                # Close BEFORE clearing _serial_listener so the identity
                # check in _on_mesh_disconnect (interface is not
                # self._mesh_interface) correctly suppresses the pubsub
                # event fired by close().
                with self._lock:
                    if self._serial_listener is iface:
                        self._serial_listener = None
                        if self._fw_verified_serial_generation == generation:
                            self._fw_verified_serial_generation = None
                        self._serial_open_generation += 1
                self._bounded_close_serial_interface(iface, generation, "listener close")
                self._mark_ready_with_radio_guard()

    def _device_probe_monitor_interval(self) -> float:
        """Return a cadence that honors physical-radio watchdog deadlines."""

        return self._firmware_watchdog_monitor_interval(self._device_probe_interval)

    def _firmware_watchdog_monitor_interval(self, base_interval: float) -> float:
        """Bound a monitor sleep by every configured watchdog deadline."""

        interval = float(base_interval)
        if not self._fw_watchdog_enabled:
            return interval
        interval = min(interval, self._fw_silence_timeout)
        if self._fw_probe_interval > 0:
            interval = min(interval, self._fw_probe_interval)
        return interval

    def _wait_for_recovery_reopen_delay(self) -> bool:
        """Honor reset re-enumeration time in both serial ownership modes."""

        with self._lock:
            not_before = self._fw_recovery_not_before
        recovery_delay = not_before - time.monotonic()
        if recovery_delay > 0:
            self.log.info(
                "Waiting %.1fs for Meshtastic device reboot/re-enumeration",
                recovery_delay,
            )
            # A reconnect wake-up requests this reopen; it must not also bypass
            # the radio's mandatory reboot/re-enumeration delay.
            self._sleep_while_active(recovery_delay)
        return self._active

    def _wait_for_serial_wake(self, timeout: float) -> bool:
        """Wait interruptibly for a manual recovery/reconnect request."""

        deadline = time.monotonic() + max(0.0, timeout)
        while self._active:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._serial_reconnect_requested.wait(min(1.0, remaining)):
                self._serial_reconnect_requested.clear()
                return True
        return True

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

    def _set_fw_state(self, state: str, error: str | None = None) -> None:
        """Advance the observable physical-radio recovery state."""

        with self._lock:
            self._fw_recovery_state = state
            if error is not None:
                self._fw_last_recovery_error = error
        if state in {
            FW_SUSPECT,
            FW_CONFIRMED_HUNG,
            FW_SOFT_RESET_ISSUED,
            FW_HARD_RESET_ISSUED,
            FW_WAITING_FOR_REOPEN,
            FW_VERIFYING,
            FW_DEGRADED,
        }:
            self.mark_degraded(f"Meshtastic physical radio: {state}")

    def _mark_ready_with_radio_guard(self) -> None:
        """Clear degradation only when every enabled Meshtastic path is healthy."""

        with self._lock:
            if not self._active:
                return
            reason: str | None = None
            blocks_initial_readiness = False
            if self._mode == MODE_MQTT and not self._connected:
                reason = "Meshtastic MQTT connection unavailable"
            elif (
                self._mode == MODE_MQTT
                and self._device_probe_port
                and self._serial_listener is None
            ):
                reason = "Meshtastic physical serial listener unavailable"
                blocks_initial_readiness = True
            elif (
                self._mode == MODE_MQTT
                and self._device_probe_port
                and self._fw_watchdog_enabled
                and self._fw_verified_serial_generation != self._serial_active_generation
            ):
                reason = "Meshtastic physical serial generation is not actively verified"
                blocks_initial_readiness = True
            elif self._fw_watchdog_enabled and self._fw_dependency_error is not None:
                reason = f"Meshtastic health probe dependency mismatch: {self._fw_dependency_error}"
            elif self._fw_recovery_pending or self._fw_recovery_attempting:
                reason = "Meshtastic physical-radio recovery is still pending"
            elif self._serial_teardown_unproven:
                reason = "Meshtastic serial teardown is unproven"
            elif self._fw_watchdog_enabled and self._fw_recovery_state not in {
                FW_HEALTHY,
                FW_RECOVERED,
            }:
                reason = f"Meshtastic physical radio: {self._fw_recovery_state}"
        if reason is None:
            self.mark_ready()
            return

        # Preserve API-v2 readiness once the serving path itself is usable,
        # but never publish a transient HEALTHY state while another enabled
        # path is known degraded. mark_ready() preserves an existing degraded
        # health value during the initial STARTING -> READY transition only.
        starting = self.plugin_state is PluginState.STARTING
        self.mark_degraded(reason)
        if starting and not blocks_initial_readiness:
            self.mark_ready()

    def _ensure_recovery_runtime_state(self) -> None:
        if not hasattr(self, "_fw_recovery_operation_lock"):
            self._fw_recovery_operation_lock = threading.Lock()
        if not hasattr(self, "_fw_recovery_attempting"):
            self._fw_recovery_attempting = False
        if not hasattr(self, "_fw_recovery_epoch"):
            self._fw_recovery_epoch = 0
        if not hasattr(self, "_fw_recovery_serial_generation"):
            self._fw_recovery_serial_generation = None
        if not hasattr(self, "_fw_recovery_hard_escalated"):
            self._fw_recovery_hard_escalated = False
        if not hasattr(self, "_fw_verified_serial_generation"):
            self._fw_verified_serial_generation = None
        if not hasattr(self, "_fw_verification_failure_sticky"):
            self._fw_verification_failure_sticky = False

    def _claim_recovery_attempt(self) -> int | None:
        """Atomically claim one immutable recovery epoch."""

        self._ensure_recovery_runtime_state()
        if not self._fw_recovery_operation_lock.acquire(blocking=False):
            return None
        with self._lock:
            if self._fw_recovery_pending or self._fw_recovery_attempting:
                self._fw_recovery_operation_lock.release()
                return None
            self._fw_recovery_epoch += 1
            epoch = self._fw_recovery_epoch
            self._fw_recovery_attempting = True
            self._fw_recovery_hard_escalated = False
            self._fw_recovery_method = None
            self._fw_recovery_serial_generation = None
            self._fw_recovery_started_at = None
        return epoch

    def _release_recovery_attempt(self) -> None:
        with self._lock:
            self._fw_recovery_attempting = False
        if self._fw_recovery_operation_lock.locked():
            self._fw_recovery_operation_lock.release()

    def _begin_pending_recovery(
        self,
        state: str,
        method: str,
        *,
        epoch: int,
        serial_generation: int | None = None,
    ) -> bool:
        now = time.monotonic()
        self._ensure_recovery_runtime_state()
        with self._lock:
            if epoch != self._fw_recovery_epoch:
                return False
            started_at = self._fw_recovery_started_at
            self._fw_recovery_pending = True
            self._fw_recovery_attempting = False
            self._fw_recovery_method = method
            self._fw_recovery_serial_generation = serial_generation
            if started_at is None:
                self._fw_recovery_started_at = time.time()
            self._fw_recovery_not_before = now + getattr(
                self,
                "_fw_recovery_reopen_delay",
                8.0,
            )
        self._set_fw_state(state)
        return True

    def _complete_firmware_recovery(
        self,
        epoch: int,
        iface: Any,
        generation: int,
    ) -> bool:
        """Publish recovery only after reopen and correlated device response."""

        now_wall = time.time()
        with self._lock:
            if (
                not self._active
                or not self._fw_recovery_pending
                or epoch != self._fw_recovery_epoch
                or generation != self._serial_active_generation
                or generation != self._serial_open_generation
                or not (
                    iface is self._serial_listener
                    or (self._mode == MODE_SERIAL and iface is self._mesh_interface)
                )
            ):
                return False
            method = self._fw_recovery_method
            started_at = self._fw_recovery_started_at
            self._fw_recovery_pending = False
            self._fw_recovery_attempting = False
            self._fw_recovery_method = None
            self._fw_recovery_serial_generation = None
            self._fw_recovery_started_at = None
            self._fw_recovery_not_before = 0.0
            self._fw_hang_detected = False
            self._fw_hang_reason = None
            self._fw_last_verified_at = now_wall
            self._fw_verified_serial_generation = generation
            self._fw_last_recovery_error = None
            self._fw_verification_failure_sticky = False
            self._fw_recovery_state = FW_RECOVERED
        self._mark_ready_with_radio_guard()
        self.event_bus.publish(
            events.MESHTASTIC_FIRMWARE_RECOVERED,
            {
                "total_resets": self._fw_total_resets,
                "method": method,
                "verified": True,
                "recovery_seconds": (
                    max(0.0, now_wall - started_at) if started_at is not None else None
                ),
                "recovery_epoch": epoch,
            },
        )
        return True

    def _check_firmware_watchdog(self) -> bool:
        """Check physical USB presence, independent serial silence, and I/O."""
        if not self._fw_watchdog_enabled:
            return True

        if self._fw_recovery_pending:
            return False

        now = time.monotonic()

        # Layer 3: USB enumeration — device vanished from bus entirely
        if not self._check_usb_present():
            self.log.warning("Firmware watchdog: USB device no longer enumerated")
            self._handle_firmware_hang("usb_disappeared")
            return False

        # A positive probe interval is a proactive cadence independent of RX.
        # With the default zero interval, probes remain silence-triggered.
        with self._lock:
            last_activity = self._last_serial_activity
            last_probe = self._last_fw_probe_time
        silence = now - last_activity if last_activity else 0
        silence_due = not last_activity or silence >= self._fw_silence_timeout
        if self._fw_probe_interval > 0:
            probe_due = last_probe <= 0 or now - last_probe >= self._fw_probe_interval
        else:
            probe_due = silence_due
        if not probe_due:
            return True

        # Layer 2: active probe — ask the device for nodeinfo
        self._set_fw_state(FW_SUSPECT)
        if silence_due and last_activity:
            self.log.info(
                "Firmware watchdog: %ds silence, sending probe",
                int(silence),
            )
        elif not last_activity:
            self.log.info("Firmware watchdog: no activity since connect, sending probe")
        else:
            self.log.debug("Firmware watchdog: proactive interval elapsed, sending probe")

        # Record every actual attempt so unsupported/local-pressure outcomes do
        # not create a tight retry loop when proactive probing is configured.
        with self._lock:
            self._last_fw_probe_time = now
        probe_result = self._probe_device_health()
        if probe_result.verified:
            with self._lock:
                self._last_serial_activity = now
                self._fw_last_verified_at = time.time()
                self._fw_verified_serial_generation = self._serial_active_generation
                self._fw_hang_detected = False
                self._fw_hang_reason = None
                self._fw_last_recovery_error = None
                self._fw_verification_failure_sticky = False
                self._fw_recovery_state = FW_HEALTHY
            self._mark_ready_with_radio_guard()
            return True

        if probe_result.outcome is MeshtasticHealthOutcome.ALIVE_PROTOCOL_ERROR:
            # A correlated NAK proves the MCU and serial receive path are alive,
            # but configuration/authentication needs attention.  Do not reset a
            # responsive radio in an attempt to repair a protocol policy error.
            with self._lock:
                self._last_serial_activity = now
            self._set_fw_state(
                FW_DEGRADED,
                f"metadata probe NAK: {probe_result.protocol_error}",
            )
            return True

        if probe_result.outcome is MeshtasticHealthOutcome.UNSUPPORTED:
            # Capability/version mismatch is not evidence of a firmware hang.
            self._set_fw_state(FW_DEGRADED, f"health probe unsupported: {probe_result.detail}")
            return True

        if probe_result.outcome is MeshtasticHealthOutcome.INCONCLUSIVE:
            # Local queue pressure, a concurrent legitimate command, or another
            # unpublished candidate says nothing about MCU liveness. Preserve
            # the current interface and try again on the next watchdog cycle.
            self._set_fw_state(
                FW_SUSPECT,
                f"health probe deferred by local backpressure: {probe_result.detail}",
            )
            return True

        if probe_result.outcome is MeshtasticHealthOutcome.STALE_GENERATION:
            self._set_fw_state(FW_WAITING_FOR_REOPEN, probe_result.detail)
            return False

        if probe_result.outcome is MeshtasticHealthOutcome.TRANSPORT_ERROR:
            self.log.warning(
                "Firmware watchdog: serial transport failed during active probe (%s)",
                probe_result.detail,
            )
            self._handle_firmware_hang("probe_transport_error", allow_soft=False)
            return False

        self.log.warning(
            "Firmware watchdog: device unresponsive after %ds silence + probe",
            int(silence),
        )
        self._handle_firmware_hang("probe_timeout")
        return False

    def _serial_generation_is_current(self, iface: Any, generation: int) -> bool:
        with self._lock:
            return (
                self._active
                and generation == self._serial_active_generation
                and generation == self._serial_open_generation
                and (
                    iface is self._serial_listener
                    or (self._mode == MODE_SERIAL and iface is self._mesh_interface)
                    or self._serial_probe_candidate == (iface, generation)
                )
            )

    def _serial_generation_for_interface(self, iface: Any) -> int | None:
        """Return the current generation only for a physical serial interface."""

        with self._lock:
            if not (
                self._active
                and (
                    iface is self._serial_listener
                    or (self._mode == MODE_SERIAL and iface is self._mesh_interface)
                )
            ):
                return None
            generation = self._serial_active_generation
            if generation != self._serial_open_generation:
                return None
            return generation

    def _invoke_interface_operation(
        self,
        iface: Any,
        operation: str,
        callback: Callable[[], Any],
    ) -> _SerialCommandResult:
        """Run MQTT operations directly and physical serial operations bounded."""

        generation = self._serial_generation_for_interface(iface)
        if generation is None:
            with self._lock:
                is_current_mqtt = (
                    self._active
                    and self._connected
                    and self._mode == MODE_MQTT
                    and iface is self._mesh_interface
                )
            if not is_current_mqtt:
                return _SerialCommandResult(_SerialCommandOutcome.STALE)
            try:
                value = callback()
            except BaseException as exc:
                return _SerialCommandResult(
                    _SerialCommandOutcome.ERROR,
                    error=exc,
                    started=True,
                )
            with self._lock:
                is_current_mqtt = (
                    self._active
                    and self._connected
                    and self._mode == MODE_MQTT
                    and iface is self._mesh_interface
                )
            if not is_current_mqtt:
                return _SerialCommandResult(_SerialCommandOutcome.STALE, started=True)
            return _SerialCommandResult(
                _SerialCommandOutcome.SUCCESS,
                value=value,
                started=True,
            )
        return self._run_serial_command(iface, generation, operation, callback)

    def _probe_device_health(
        self,
        iface: Any | None = None,
        *,
        generation: int | None = None,
    ) -> MeshtasticHealthResult:
        """Run a correlated, non-mutating local metadata transaction."""

        with self._lock:
            selected = (
                iface
                or self._serial_listener
                or (self._mesh_interface if self._mode == MODE_SERIAL else None)
            )
            active_generation = self._serial_active_generation if generation is None else generation
            previous_candidate = self._serial_probe_candidate
            published = selected is not None and (
                selected is self._serial_listener
                or (self._mode == MODE_SERIAL and selected is self._mesh_interface)
            )
            desired_candidate = (
                None if selected is None or published else (selected, active_generation)
            )
            candidate_busy = (
                desired_candidate is not None
                and previous_candidate is not None
                and previous_candidate != desired_candidate
            )
            if not candidate_busy and desired_candidate is not None:
                self._serial_probe_candidate = desired_candidate
            expected_hardware = self._fw_device_hardware_model

        if selected is None:
            result = MeshtasticHealthResult(
                MeshtasticHealthOutcome.STALE_GENERATION,
                "serial_interface_unavailable",
            )
        elif candidate_busy:
            result = MeshtasticHealthResult(
                MeshtasticHealthOutcome.INCONCLUSIVE,
                "serial_probe_candidate_busy",
            )
        else:
            try:
                command_result = self._run_serial_command(
                    selected,
                    active_generation,
                    "health-probe",
                    lambda: self._meshtastic_health.probe(
                        selected,
                        active_generation,
                        is_current=self._serial_generation_is_current,
                        timeout=self._fw_probe_timeout,
                        expected_hardware_model=expected_hardware,
                    ),
                    timeout=self._fw_probe_timeout + 1.0,
                )
                if command_result.succeeded:
                    result = command_result.value
                elif command_result.outcome is _SerialCommandOutcome.STALE:
                    result = MeshtasticHealthResult(
                        MeshtasticHealthOutcome.STALE_GENERATION,
                        "serial_command_generation_stale",
                    )
                elif command_result.outcome is _SerialCommandOutcome.ERROR:
                    result = MeshtasticHealthResult(
                        MeshtasticHealthOutcome.TRANSPORT_ERROR,
                        f"serial_command_transport_error:{type(command_result.error).__name__}",
                    )
                elif command_result.outcome is _SerialCommandOutcome.BUSY:
                    result = MeshtasticHealthResult(
                        MeshtasticHealthOutcome.INCONCLUSIVE,
                        "serial_command_gate_busy",
                    )
                else:
                    result = MeshtasticHealthResult(
                        MeshtasticHealthOutcome.TIMEOUT,
                        f"serial_command_gate_{command_result.outcome.value}",
                    )
            finally:
                if desired_candidate is not None:
                    with self._lock:
                        if self._serial_probe_candidate == desired_candidate:
                            self._serial_probe_candidate = previous_candidate

        with self._lock:
            self._fw_last_probe_outcome = result.outcome.value
            self._fw_last_probe_detail = result.detail
            if result.metadata is not None:
                self._fw_device_firmware_version = result.metadata.firmware_version
                self._fw_device_hardware_model = result.metadata.hardware_model
        return result

    def _probe_device_responsive(
        self,
        iface: Any | None = None,
        *,
        generation: int | None = None,
    ) -> bool:
        """Return true only for a correlated metadata response from the MCU."""

        return self._probe_device_health(iface, generation=generation).verified

    def _check_usb_present(self) -> bool:
        """Check if the serial device path still exists on the filesystem."""
        port = self._device_probe_port or self.config.get("serial_port", "/dev/meshtastic")
        if not port or port == "auto":
            return True
        try:
            return os.path.exists(port)
        except Exception:
            return True

    def _handle_firmware_hang(self, reason: str, *, allow_soft: bool = True) -> None:
        """Record a firmware hang and attempt recovery if configured."""
        self._ensure_recovery_runtime_state()
        now = time.monotonic()
        with self._lock:
            if self._fw_recovery_pending or self._fw_recovery_attempting:
                return
            self._fw_hang_detected = True
            self._fw_hang_reason = reason
            self._fw_total_hangs += 1

        self.event_bus.publish(
            events.MESHTASTIC_FIRMWARE_HANG,
            {
                "reason": reason,
                "silence_seconds": (
                    int(now - self._last_serial_activity) if self._last_serial_activity else None
                ),
                "total_hangs": self._fw_total_hangs,
            },
        )

        if not self._fw_auto_reset:
            self.log.warning("Firmware hang detected but auto_reset is disabled")
            return

        if not self._fw_reset_allowed():
            self.log.warning(
                "Firmware hang detected but durable reset circuit breaker is unavailable or full"
            )
            self._set_fw_state(FW_DEGRADED, "reset circuit breaker blocked recovery")
            return

        self._set_fw_state(FW_CONFIRMED_HUNG)
        if not self._attempt_firmware_recovery(reason, allow_soft=allow_soft):
            self._set_fw_state(FW_DEGRADED, "all configured recovery methods failed")

    def _fw_reset_allowed(self) -> bool:
        """Return whether the durable circuit breaker has capacity."""

        limiter = getattr(self, "_fw_reset_limiter", None)
        if limiter is not None:
            status = limiter.status()
            return not status["state_error"] and (
                self._fw_max_resets_per_hour <= 0
                or status["recent_attempts"] < self._fw_max_resets_per_hour
            )
        # Compatibility for directly constructed unit-test instances.
        if self._fw_max_resets_per_hour <= 0:
            return True
        now = time.monotonic()
        cutoff = now - 3600
        with self._lock:
            timestamps = getattr(self, "_fw_reset_timestamps", [])
            self._fw_reset_timestamps = [stamp for stamp in timestamps if stamp > cutoff]
            return len(self._fw_reset_timestamps) < self._fw_max_resets_per_hour

    def _reserve_reset(self, method: str) -> bool:
        """Durably reserve a reset-rate slot before reset-capable I/O."""

        limiter = getattr(self, "_fw_reset_limiter", None)
        if limiter is None:
            if not self._fw_reset_allowed():
                return False
            with self._lock:
                timestamps = getattr(self, "_fw_reset_timestamps", [])
                timestamps.append(time.monotonic())
                self._fw_reset_timestamps = timestamps
                self._fw_total_resets += 1
            return True

        reservation = limiter.reserve(method)
        if not reservation.allowed:
            self.log.warning(
                "Meshtastic reset blocked (%s, %d recent attempt(s))",
                reservation.reason,
                reservation.recent_attempts,
            )
            self._set_fw_state(FW_DEGRADED, f"reset blocked: {reservation.reason}")
            return False
        with self._lock:
            self._fw_total_resets = limiter.total_attempts
        return True

    def _attempt_firmware_recovery(self, reason: str, *, allow_soft: bool = True) -> bool:
        """Issue one single-flight actuator under an immutable recovery epoch."""

        epoch = self._claim_recovery_attempt()
        if epoch is None:
            self.log.info("Firmware recovery already in progress; duplicate request ignored")
            return False
        self.log.info("Attempting firmware recovery epoch %d (trigger: %s)", epoch, reason)
        try:
            with self._lock:
                iface = self._serial_listener or (
                    self._mesh_interface if self._mode == MODE_SERIAL else None
                )
                generation = self._serial_active_generation

            if allow_soft and self._meshtastic_health.has_inflight():
                # A timed-out private _sendAdmin() call is not cancellable and
                # may still hold SDK internals. Never enter localNode.reboot()
                # concurrently with it; only the identity-bound hard actuator
                # (if configured) may be attempted.
                allow_soft = False
                self.log.warning(
                    "Soft reboot skipped because a Meshtastic health probe is still in flight"
                )

            if allow_soft and iface is not None:
                if not self._bind_or_validate_usb_identity():
                    self._set_fw_state(
                        FW_DEGRADED,
                        "serial identity validation failed; reset refused",
                    )
                    return False
                local_node = getattr(iface, "localNode", None)
                if local_node is not None:

                    def reboot() -> bool:
                        if not self._reserve_reset("soft_reboot"):
                            return False
                        local_node.reboot(secs=5)
                        return True

                    command = self._run_serial_command(
                        iface,
                        generation,
                        "soft-reboot",
                        reboot,
                    )
                    if command.succeeded and command.value is True:
                        self.log.info("Firmware recovery: reboot command sent")
                        return self._begin_pending_recovery(
                            FW_SOFT_RESET_ISSUED,
                            "soft_reboot",
                            epoch=epoch,
                            serial_generation=generation,
                        )
                    if command.succeeded and command.value is False:
                        return False
                    if command.uncertain:
                        # The call may have reached the radio. Never retry an
                        # uncertain reboot automatically; reopen and verify it.
                        self.log.warning(
                            "Soft reboot delivery is uncertain; no automatic resend will occur"
                        )
                        return self._begin_pending_recovery(
                            FW_SOFT_RESET_ISSUED,
                            "soft_reboot_uncertain",
                            epoch=epoch,
                            serial_generation=generation,
                        )
                    self.log.info(
                        "Soft reboot skipped (%s); considering hard recovery",
                        command.outcome.value,
                    )

            if self._attempt_usb_recovery(epoch, "usb_bus_reset"):
                return True

            self.log.error("Firmware recovery: all methods exhausted")
            return False
        finally:
            self._release_recovery_attempt()

    def _attempt_usb_recovery(self, epoch: int, method: str) -> bool:
        """Issue one identity-bound, durably reserved USB bus reset."""

        if not self._fw_usb_power_cycle:
            return False
        port = self._physical_serial_port()
        if port is None:
            return False
        try:
            usb_path = self._resolve_usb_device_path()
            if usb_path is None:
                return False
            if not self._reserve_reset(method):
                return False
            result = self._usb_bus_reset(usb_path)
        except Exception:
            self.log.debug("USB bus reset failed", exc_info=True)
            return False
        if not result.get("ok"):
            self.log.warning("USB bus reset failed: %s", result.get("reason"))
            return False
        self.log.info("Firmware recovery: USB bus reset sent (%s)", usb_path)
        return self._begin_pending_recovery(
            FW_HARD_RESET_ISSUED,
            method,
            epoch=epoch,
        )

    def _handle_recovery_verification_failure(
        self,
        iface: Any,
        generation: int,
        epoch: int,
        result: MeshtasticHealthResult,
    ) -> None:
        """Permit exactly one soft-to-hard escalation after failed verification."""

        detail = f"post-reopen verification failed: {result.outcome.value}:{result.detail}"
        with self._lock:
            self._fw_verification_failure_sticky = True
        self._poison_serial_generation(iface, generation, detail)
        self._set_fw_state(FW_WAITING_FOR_REOPEN, detail)
        self._ensure_recovery_runtime_state()
        if not self._fw_recovery_operation_lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                if not self._fw_recovery_pending or epoch != self._fw_recovery_epoch:
                    return
                method = self._fw_recovery_method or ""
                may_escalate = (
                    result.outcome
                    in {
                        MeshtasticHealthOutcome.TIMEOUT,
                        MeshtasticHealthOutcome.TRANSPORT_ERROR,
                    }
                    and method.startswith("soft_reboot")
                    and not self._fw_recovery_hard_escalated
                )
                if may_escalate:
                    self._fw_recovery_hard_escalated = True
                    self._fw_recovery_attempting = True

            if may_escalate and self._attempt_usb_recovery(
                epoch,
                "usb_bus_reset_escalation",
            ):
                return

            with self._lock:
                if epoch == self._fw_recovery_epoch:
                    self._fw_recovery_pending = False
                    self._fw_recovery_attempting = False
                    self._fw_recovery_not_before = 0.0
            self._set_fw_state(FW_DEGRADED, detail)
        finally:
            with self._lock:
                self._fw_recovery_attempting = False
            self._fw_recovery_operation_lock.release()

    def _escalate_pending_soft_recovery_after_open_timeouts(self) -> bool:
        """Escalate an unreopened soft-reset epoch to hard reset exactly once."""

        self._ensure_recovery_runtime_state()
        if not self._fw_recovery_operation_lock.acquire(blocking=False):
            # The same immutable recovery epoch is already being handled.
            return True
        try:
            with self._lock:
                method = self._fw_recovery_method or ""
                if (
                    not self._fw_recovery_pending
                    or not method.startswith("soft_reboot")
                    or self._fw_recovery_hard_escalated
                ):
                    return False
                epoch = self._fw_recovery_epoch
                self._fw_recovery_hard_escalated = True
                self._fw_recovery_attempting = True

            if self._attempt_usb_recovery(
                epoch,
                "usb_bus_reset_open_timeout_escalation",
            ):
                return True

            detail = "soft-reset device never reopened and hard escalation failed"
            with self._lock:
                if epoch == self._fw_recovery_epoch:
                    self._fw_recovery_pending = False
                    self._fw_recovery_attempting = False
                    self._fw_recovery_not_before = 0.0
            self._set_fw_state(FW_DEGRADED, detail)
            return False
        finally:
            with self._lock:
                self._fw_recovery_attempting = False
            self._fw_recovery_operation_lock.release()

    def _record_reset(self, method: str) -> None:
        """Compatibility wrapper for tests and third-party plugin extensions."""

        self._reserve_reset(method)

    def _post_recovery_wait(self) -> None:
        """Legacy hook retained without the former false recovery event."""

        self._set_fw_state(FW_WAITING_FOR_REOPEN)

    def _handle_startup_firmware_hang(self) -> None:
        """Handle a firmware hang detected during serial open (pre-connection).

        Unlike mid-session hangs, there is no localNode for soft reboot —
        recovery goes straight to USB bus reset.
        """
        self._ensure_recovery_runtime_state()
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

        with self._lock:
            pending = self._fw_recovery_pending
            pending_method = self._fw_recovery_method or ""
            hard_escalated = self._fw_recovery_hard_escalated

        if pending:
            if not self._fw_auto_reset:
                with self._lock:
                    self._fw_recovery_pending = False
                    self._fw_recovery_not_before = 0.0
                self._set_fw_state(
                    FW_DEGRADED,
                    "soft-reset device never reopened and automatic escalation is disabled",
                )
                return
            self._set_fw_state(FW_CONFIRMED_HUNG)
            if pending_method.startswith("soft_reboot") and not hard_escalated:
                self._escalate_pending_soft_recovery_after_open_timeouts()
                return

            # A hard-reset epoch (including the one allowed escalation) also
            # failed to reopen. End it instead of leaving recovery pending
            # forever or starting a fresh reset epoch.
            with self._lock:
                self._fw_recovery_pending = False
                self._fw_recovery_attempting = False
                self._fw_recovery_not_before = 0.0
            self._set_fw_state(
                FW_DEGRADED,
                f"device did not reopen after {pending_method or 'recovery'}",
            )
            return

        if not self._fw_auto_reset:
            self.log.warning("Firmware hang detected but auto_reset is disabled")
            return

        if not self._fw_reset_allowed():
            self.log.warning("Startup recovery blocked by durable reset circuit breaker")
            self._set_fw_state(FW_DEGRADED, "reset circuit breaker blocked recovery")
            return

        self._set_fw_state(FW_CONFIRMED_HUNG)
        if not self._attempt_startup_recovery():
            self._set_fw_state(FW_DEGRADED, "startup recovery unavailable or failed")

    def _attempt_startup_recovery(self) -> bool:
        """Recovery for startup hangs — USB bus reset only (no localNode)."""
        if not self._fw_usb_power_cycle:
            self.log.warning(
                "Startup recovery requires a USB reset, but usb_power_cycle is disabled"
            )
            return False
        self.log.info(
            "Attempting startup recovery via USB bus reset (no localNode available for soft reboot)"
        )
        if not self._check_usb_present():
            self.log.error("Startup recovery: USB device not present")
            return False
        epoch = self._claim_recovery_attempt()
        if epoch is None:
            return False
        try:
            return self._attempt_usb_recovery(epoch, "usb_bus_reset_startup")
        finally:
            self._release_recovery_attempt()

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

            send_result = self._invoke_interface_operation(
                iface,
                "queued-text-send",
                lambda: iface.sendText(
                    formatted,
                    channelIndex=channel,
                    hopLimit=MESHTASTIC_HOP_LIMIT,
                ),
            )
            if send_result.succeeded:
                with self._lock:
                    self._msgs_lxmf_to_mesh += 1
                    self._last_lxmf_msg_time = time.time()
                self.log.debug("Sent queued LXMF message on ch%d", channel)
            elif not send_result.started:
                # The command did not enter third-party code, so retrying the
                # same queue item later cannot duplicate a transmission.
                with self._lock:
                    self._send_queue.appendleft((enqueued_at, formatted, channel))
                return
            else:
                # Once a send entered Meshtastic, timeout/error delivery is
                # uncertain. Never automatically retry and risk duplicates.
                self.log.error(
                    "Queued Meshtastic send became uncertain (%s); dropping without retry",
                    send_result.outcome.value,
                )
                return

    # ── Meshtastic pubsub callbacks ─────────────────────────────────

    def _owned_mesh_source_locked(self, interface: Any) -> str | None:
        """Classify only an interface object owned by this exact gateway."""

        if interface is None:
            return None
        if interface is self._serial_listener:
            return "LoRa"
        if interface is self._mesh_interface:
            return "LoRa" if self._mode == MODE_SERIAL else "MQTT"
        return None

    @staticmethod
    def _packet_dedup_key(packet: dict[str, Any]) -> tuple[int | str, int] | None:
        """Return the sender-scoped identity of one Meshtastic packet."""

        packet_id = packet.get("id")
        if isinstance(packet_id, bool) or not isinstance(packet_id, int) or packet_id <= 0:
            return None
        sender: int | str | None = packet.get("from")
        if isinstance(sender, bool):
            sender = None
        elif isinstance(sender, int):
            if sender < 0:
                sender = None
        else:
            sender = None
        if sender is None:
            from_id = packet.get("fromId")
            if not isinstance(from_id, str) or not from_id.strip():
                return None
            sender = from_id.strip().casefold()
        return sender, packet_id

    def _packet_is_duplicate_locked(self, packet: dict[str, Any]) -> bool:
        """Record one packet identity under ``_lock`` and flag replays."""

        key = self._packet_dedup_key(packet)
        if key is None:
            return False
        now = time.monotonic()
        cutoff = now - self._dedup_ttl_seconds
        seen_at = self._seen_packet_ids.get(key)
        if seen_at is not None and seen_at > cutoff:
            return True
        # An expired exact key is new immediately. Overwrite it now instead
        # of waiting for the amortized whole-cache cleanup scan.
        self._seen_packet_ids[key] = now
        self._dedup_inserts_since_cleanup += 1
        if (
            self._dedup_inserts_since_cleanup >= self._dedup_cleanup_interval
            or len(self._seen_packet_ids) > self._dedup_max_entries
        ):
            self._dedup_inserts_since_cleanup = 0
            self._seen_packet_ids = {
                cached_key: seen_at
                for cached_key, seen_at in self._seen_packet_ids.items()
                if seen_at > cutoff
            }
            if len(self._seen_packet_ids) > self._dedup_max_entries:
                items = sorted(
                    self._seen_packet_ids.items(),
                    key=lambda item: item[1],
                )
                self._seen_packet_ids = dict(items[self._dedup_max_entries // 2 :])
        return False

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
            source_tag = self._owned_mesh_source_locked(interface)
            if source_tag is None:
                self.log.debug("Meshtastic text from non-owned interface ignored")
                return
            if source_tag == "LoRa":
                self._last_serial_activity = time.monotonic()
            else:
                self._last_mqtt_activity = time.monotonic()
            packet_id = packet.get("id", 0)
            if self._packet_is_duplicate_locked(packet):
                return
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
                if self._mode == MODE_SERIAL:
                    self._last_serial_activity = time.monotonic()
                else:
                    self._last_mqtt_activity = time.monotonic()
        if was_disconnected:
            # READY -> READY clears a transient degraded health condition;
            # STARTING -> READY covers an initial connection callback.
            self._mark_ready_with_radio_guard()
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
        self.mark_degraded("Meshtastic connection lost")
        self.event_bus.publish(events.MESHTASTIC_DISCONNECTED, {"reason": "connection_lost"})

    # ── LXMF delivery callback ──────────────────────────────────────

    def _handle_lxmf_message(self, message: Any) -> None:
        """Handle incoming LXMF message and forward to Meshtastic."""
        if not self._active:
            return
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

        send_result = self._invoke_interface_operation(
            iface,
            "lxmf-text-send",
            lambda: iface.sendText(
                formatted,
                channelIndex=channel,
                hopLimit=MESHTASTIC_HOP_LIMIT,
            ),
        )
        if not send_result.succeeded:
            if send_result.uncertain:
                self.log.error(
                    "LXMF-to-Meshtastic delivery is uncertain; not retrying automatically"
                )
            else:
                self.log.warning(
                    "LXMF-to-Meshtastic send skipped (%s)",
                    send_result.outcome.value,
                )
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

        if not self._active:
            return False
        try:
            dest_identity = RNS.Identity.recall(recipient_hash)
            if dest_identity is None:
                RNS.Transport.request_path(recipient_hash)
                self.log.debug(
                    "Path requested for %s, message deferred",
                    RNS.prettyhexrep(recipient_hash),
                )
                return False

            with self._lock:
                if not self._active:
                    return False
                dest = self._lxmf_destinations.get(recipient_hash)
                if dest is None:
                    dest = self._manage_lxmf_destination(
                        RNS.Destination(
                            dest_identity,
                            RNS.Destination.OUT,
                            RNS.Destination.SINGLE,
                            "lxmf",
                            "delivery",
                        )
                    )
                    self._lxmf_destinations[recipient_hash] = dest
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

    def _manage_lxmf_destination(self, destination: Any) -> Any:
        """Own an LXMF destination and undo lifecycle-raced acquisition."""
        return self.manage_destination(destination)

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
            last_serial_activity = getattr(self, "_last_serial_activity", 0.0)
            last_mqtt_activity = getattr(self, "_last_mqtt_activity", 0.0)
            fw_silence_timeout = self._fw_silence_timeout
            fw_total_hangs = self._fw_total_hangs
            fw_total_resets = self._fw_total_resets
            fw_auto_reset = self._fw_auto_reset
            fw_max_resets_per_hour = self._fw_max_resets_per_hour
            fw_consecutive_open_failures = self._fw_consecutive_open_failures
            fw_open_failure_threshold = self._fw_open_failure_threshold
            fw_first_open_failure_time = self._fw_first_open_failure_time
            serial_listener = self._serial_listener
            fw_recovery_state = getattr(self, "_fw_recovery_state", FW_HEALTHY)
            fw_recovery_pending = getattr(self, "_fw_recovery_pending", False)
            fw_recovery_attempting = getattr(self, "_fw_recovery_attempting", False)
            fw_recovery_epoch = getattr(self, "_fw_recovery_epoch", 0)
            fw_recovery_method = getattr(self, "_fw_recovery_method", None)
            fw_last_verified_at = getattr(self, "_fw_last_verified_at", None)
            fw_last_recovery_error = getattr(self, "_fw_last_recovery_error", None)
            fw_last_probe_outcome = getattr(self, "_fw_last_probe_outcome", None)
            fw_last_probe_detail = getattr(self, "_fw_last_probe_detail", None)
            fw_device_firmware_version = getattr(
                self,
                "_fw_device_firmware_version",
                None,
            )
            fw_device_hardware_model = getattr(
                self,
                "_fw_device_hardware_model",
                None,
            )
            fw_dependency_error = getattr(self, "_fw_dependency_error", None)
            serial_teardown_unproven = getattr(
                self,
                "_serial_teardown_unproven",
                False,
            )
            serial_unclosed_interfaces = len(getattr(self, "_serial_unclosed_interfaces", {}))

        limiter = getattr(self, "_fw_reset_limiter", None)
        if limiter is not None:
            limiter_status = limiter.status()
            resets_last_hour = limiter_status["recent_attempts"]
        else:
            limiter_status = {
                "state_error": None,
                "clock_uncertain": False,
                "blocked_seconds": 0,
            }
            now_mono_for_resets = time.monotonic()
            resets_last_hour = len(
                [
                    stamp
                    for stamp in getattr(self, "_fw_reset_timestamps", [])
                    if stamp > now_mono_for_resets - 3600
                ]
            )

        if (
            mode == MODE_MQTT
            and not connected
            and active
            and iface is not None
            and last_disconnect_time > 0
        ):
            grace = self.config.get("reconnect_grace_period", 90)
            elapsed = time.monotonic() - last_disconnect_time
            if elapsed < grace:
                connected = True

        status: dict[str, Any] = {
            "active": active,
            "mode": mode,
            "connected": connected,
            "serial_available": serial_listener is not None
            or (mode == MODE_SERIAL and connected and iface is not None),
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
            status["serial_port"] = self.config.get("serial_port", "/dev/meshtastic")
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
            silence = int(now_mono - last_serial_activity) if last_serial_activity else None
            status["firmware_watchdog"] = {
                "enabled": True,
                "hang_detected": fw_hang_detected,
                "hang_reason": fw_hang_reason,
                "recovery_state": fw_recovery_state,
                "recovery_pending": fw_recovery_pending,
                "recovery_attempting": fw_recovery_attempting,
                "recovery_epoch": fw_recovery_epoch,
                "recovery_method": fw_recovery_method,
                "last_verified_at": fw_last_verified_at,
                "last_recovery_error": fw_last_recovery_error,
                "last_probe_outcome": fw_last_probe_outcome,
                "last_probe_detail": fw_last_probe_detail,
                "device_firmware_version": fw_device_firmware_version,
                "device_hardware_model": fw_device_hardware_model,
                "dependency_error": fw_dependency_error,
                "serial_teardown_unproven": serial_teardown_unproven,
                "serial_unclosed_interfaces": serial_unclosed_interfaces,
                "silence_seconds": silence,
                "silence_timeout": int(fw_silence_timeout),
                "mqtt_activity_seconds": (
                    int(now_mono - last_mqtt_activity) if last_mqtt_activity else None
                ),
                "total_hangs": fw_total_hangs,
                "total_resets": fw_total_resets,
                "auto_reset": fw_auto_reset,
                "resets_last_hour": resets_last_hour,
                "max_resets_per_hour": fw_max_resets_per_hour,
                "reset_state_error": limiter_status["state_error"],
                "reset_clock_uncertain": limiter_status["clock_uncertain"],
                "reset_blocked_seconds": limiter_status["blocked_seconds"],
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

    def _get_serial_interface(self) -> Any | None:
        """Return the currently owned physical serial interface."""

        with self._lock:
            if self._mode == MODE_SERIAL:
                return self._mesh_interface
            return self._serial_listener

    def _get_serial_node(self) -> Any | None:
        """Return the localNode for the currently owned serial interface."""

        iface = self._get_serial_interface()
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
        iface = self._get_serial_interface()
        node = getattr(iface, "localNode", None) if iface is not None else None
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

        iface = self._get_serial_interface()
        node = getattr(iface, "localNode", None) if iface is not None else None
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

        command = self._invoke_interface_operation(
            iface,
            "channel-write",
            lambda: node.writeChannel(index),
        )
        if not command.succeeded:
            reason = "delivery uncertain" if command.uncertain else command.outcome.value
            return {"ok": False, "reason": f"Device write failed: {reason}"}

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

        iface = self._get_serial_interface()
        node = getattr(iface, "localNode", None) if iface is not None else None
        if node is None:
            return {"ok": False, "reason": "Not connected in serial mode"}

        command = self._invoke_interface_operation(
            iface,
            "channel-url-write",
            lambda: node.setURL(url, addOnly=True),
        )
        if not command.succeeded:
            reason = "delivery uncertain" if command.uncertain else command.outcome.value
            return {"ok": False, "reason": f"Channel URL failed: {reason}"}

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
        iface = self._get_serial_interface()
        node = getattr(iface, "localNode", None) if iface is not None else None
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

        command = self._invoke_interface_operation(
            iface,
            "channel-delete",
            lambda: node.deleteChannel(index),
        )
        if not command.succeeded:
            reason = "delivery uncertain" if command.uncertain else command.outcome.value
            return {"ok": False, "reason": f"Device write failed: {reason}"}

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
        is_serial = self._serial_generation_for_interface(iface) is not None
        via_label = "LoRa" if is_serial else "MQTT"

        # Ack tracking is only possible for direct messages via serial
        can_ack = bool(is_serial and destination_id and on_ack)

        def send() -> Any:
            if destination_id:
                if can_ack:
                    # Serial interface supports wantAck + onResponse
                    return iface.sendText(
                        truncate_bytes(text, MESHTASTIC_MTU),
                        channelIndex=ch,
                        destinationId=destination_id,
                        wantAck=True,
                        onResponse=self._make_ack_handler(on_ack),
                        hopLimit=MESHTASTIC_HOP_LIMIT,
                    )
                else:
                    return iface.sendText(
                        truncate_bytes(text, MESHTASTIC_MTU),
                        channelIndex=ch,
                        destinationId=destination_id,
                        hopLimit=MESHTASTIC_HOP_LIMIT,
                    )
            return iface.sendText(
                truncate_bytes(text, MESHTASTIC_MTU),
                channelIndex=ch,
                hopLimit=MESHTASTIC_HOP_LIMIT,
            )

        command = self._invoke_interface_operation(iface, "dashboard-text-send", send)
        if not command.succeeded:
            if command.uncertain:
                return {"sent": False, "reason": "delivery_uncertain"}
            if command.error is not None:
                self.log.error(
                    "Error sending Meshtastic message via %s: %s",
                    via_label,
                    command.error,
                )
                return {"sent": False, "reason": str(command.error)}
            return {"sent": False, "reason": command.outcome.value}
        sent_packet = command.value

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

        def onAckNak(packet: dict) -> None:  # noqa: N802 - Meshtastic callback contract
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

        return onAckNak

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
            iface = self._serial_listener or (
                self._mesh_interface if self._mode == MODE_SERIAL else None
            )
        if iface is None or not hasattr(iface, "sendData"):
            return {"sent": False, "reason": "serial_interface_unavailable"}

        payload = bytes([self._READ_RECEIPT_TAG]) + (packet_id & 0xFFFFFFFF).to_bytes(4, "big")

        try:
            from meshtastic.protobuf.portnums_pb2 import PortNum

            command = self._invoke_interface_operation(
                iface,
                "read-receipt-send",
                lambda: iface.sendData(
                    payload,
                    destinationId=destination_id,
                    portNum=PortNum.PRIVATE_APP,
                    wantAck=False,
                ),
            )
        except Exception as exc:
            self.log.debug("Failed to send read receipt to %s: %s", destination_id, exc)
            return {"sent": False, "reason": str(exc)}

        if not command.succeeded:
            reason = "delivery_uncertain" if command.uncertain else command.outcome.value
            return {"sent": False, "reason": reason}

        self.log.debug("Sent read receipt to %s (packet_id=%d)", destination_id, packet_id)
        return {"sent": True}

    def _on_mesh_data(self, packet: dict, interface: Any = None) -> None:
        """Handle incoming Meshtastic data packets (non-text portnums)."""
        try:
            decoded = packet.get("decoded", {})
            portnum = decoded.get("portnum")
        except Exception:
            self.log.debug("Error parsing data packet envelope", exc_info=True)
            return
        with self._lock:
            if not self._active:
                return
            source_tag = self._owned_mesh_source_locked(interface)
            if source_tag is None:
                self.log.debug("Meshtastic data from non-owned interface ignored")
                return
            if source_tag == "LoRa":
                self._last_serial_activity = time.monotonic()
            else:
                self._last_mqtt_activity = time.monotonic()
            if portnum in ("PRIVATE_APP", 256) and self._packet_is_duplicate_locked(packet):
                return
        try:
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
