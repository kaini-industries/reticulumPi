"""Emergency Broadcast plugin — flood-style priority messaging across the mesh."""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Any

import RNS
import RNS.vendor.umsgpack as umsgpack

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

# Message priorities
PRIORITY_INFO = 0
PRIORITY_WARNING = 1
PRIORITY_CRITICAL = 2
PRIORITY_EMERGENCY = 3

PRIORITY_NAMES = {
    PRIORITY_INFO: "INFO",
    PRIORITY_WARNING: "WARNING",
    PRIORITY_CRITICAL: "CRITICAL",
    PRIORITY_EMERGENCY: "EMERGENCY",
}


class EmergencyBroadcastPlugin(PluginBase):
    """Flood-style priority messaging to all reachable mesh nodes.

    Emergency messages are broadcast via Reticulum announces and re-broadcast
    by receiving nodes (with TTL decrement) to propagate across the mesh.
    Deduplication prevents broadcast storms. Messages are stored locally
    for review via the dashboard or API.
    """

    plugin_name = "emergency_broadcast"
    plugin_version = "1.0.0"
    plugin_description = "Mesh-wide emergency broadcast with flood propagation"

    def validate_config(self) -> None:
        max_ttl = self.config.get("max_ttl", 5)
        if not isinstance(max_ttl, int) or max_ttl < 1 or max_ttl > 20:
            raise ValueError("max_ttl must be an integer between 1 and 20")

        max_stored = self.config.get("max_stored_messages", 100)
        if not isinstance(max_stored, int) or max_stored < 1:
            raise ValueError("max_stored_messages must be a positive integer")

        rebroadcast_delay = self.config.get("rebroadcast_delay", 5)
        if not isinstance(rebroadcast_delay, (int, float)) or rebroadcast_delay < 0:
            raise ValueError("rebroadcast_delay must be a non-negative number")

    def start(self) -> None:
        self._active = True
        self._lock = threading.Lock()
        # Seen message IDs for deduplication
        self._seen_ids: set[str] = set()
        # Stored messages (most recent first)
        self._messages: list[dict[str, Any]] = []
        self._max_stored = self.config.get("max_stored_messages", 100)
        self._max_ttl = self.config.get("max_ttl", 5)
        self._rebroadcast_delay = self.config.get("rebroadcast_delay", 5)
        self._messages_sent = 0
        self._messages_received = 0
        self._messages_rebroadcast = 0

        # Create destination for emergency broadcasts
        self.destination = RNS.Destination(
            self.identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            "reticulumpi",
            "emergency",
            "broadcast",
        )

        # Register announce handler to receive emergency broadcasts from others
        self._handler = _EmergencyHandler(self)
        RNS.Transport.register_announce_handler(self._handler)

        self.log.info(
            "Emergency broadcast active at %s (max TTL: %d)",
            RNS.prettyhexrep(self.destination.hash),
            self._max_ttl,
        )

    def stop(self) -> None:
        self._active = False
        try:
            RNS.Transport.deregister_announce_handler(self._handler)
        except Exception:
            pass
        self._join_threads()

    def get_status(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "messages_sent": self._messages_sent,
            "messages_received": self._messages_received,
            "messages_rebroadcast": self._messages_rebroadcast,
            "stored_messages": len(self._messages),
        }

    def get_messages(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return stored emergency messages, most recent first."""
        with self._lock:
            return list(self._messages[:limit])

    def send_emergency(
        self,
        message: str,
        priority: int = PRIORITY_EMERGENCY,
        ttl: int | None = None,
    ) -> str:
        """Originate a new emergency broadcast.

        Returns the message ID.
        """
        if ttl is None:
            ttl = self._max_ttl

        msg_id = self._generate_id(message, time.time(), self.identity.hash)

        msg = {
            "type": "emergency",
            "id": msg_id,
            "origin": RNS.prettyhexrep(self.identity.hash),
            "origin_name": self.app.node_name,
            "ttl": ttl,
            "priority": priority,
            "message": message,
            "timestamp": time.time(),
        }

        # Store locally
        with self._lock:
            self._store_message(msg)
            self._seen_ids.add(msg_id)

        # Broadcast
        self._broadcast_message(msg)
        self._messages_sent += 1

        self.log.warning(
            "EMERGENCY BROADCAST [%s]: %s (TTL=%d)",
            PRIORITY_NAMES.get(priority, "UNKNOWN"),
            message,
            ttl,
        )

        self.event_bus.publish(events.EMERGENCY_RECEIVED, {
            "message": msg,
            "source": "local",
        })

        return msg_id

    def receive_emergency(
        self,
        destination_hash: bytes,
        app_data: bytes | None,
    ) -> None:
        """Process a received emergency broadcast."""
        if not app_data:
            return

        try:
            msg = umsgpack.unpackb(app_data)
        except Exception:
            self.log.debug("Failed to decode emergency broadcast")
            return

        if not isinstance(msg, dict) or msg.get("type") != "emergency":
            return

        msg_id = msg.get("id", "")
        if not msg_id:
            return

        # Deduplication
        with self._lock:
            if msg_id in self._seen_ids:
                self.log.debug("Ignoring duplicate emergency: %s", msg_id[:16])
                return

            self._seen_ids.add(msg_id)
            self._messages_received += 1

            # Prune seen_ids if it gets too large
            if len(self._seen_ids) > self._max_stored * 10:
                self._seen_ids = set(m.get("id", "") for m in self._messages)

        priority = msg.get("priority", PRIORITY_INFO)
        self.log.warning(
            "EMERGENCY RECEIVED [%s] from %s: %s",
            PRIORITY_NAMES.get(priority, "UNKNOWN"),
            msg.get("origin_name", msg.get("origin", "unknown")),
            msg.get("message", ""),
        )

        # Store locally
        msg["received_at"] = time.time()
        msg["received_from"] = RNS.prettyhexrep(destination_hash)
        with self._lock:
            self._store_message(msg)

        # Publish event
        self.event_bus.publish(events.EMERGENCY_RECEIVED, {
            "message": msg,
            "source": "mesh",
        })

        # Re-broadcast with decremented TTL
        ttl = msg.get("ttl", 0)
        if ttl > 1 and self.config.get("rebroadcast", True):
            rebroadcast_msg = dict(msg)
            rebroadcast_msg["ttl"] = ttl - 1
            # Remove receiver-specific fields
            rebroadcast_msg.pop("received_at", None)
            rebroadcast_msg.pop("received_from", None)

            if self._rebroadcast_delay > 0:
                self._start_thread(
                    lambda: self._delayed_rebroadcast(rebroadcast_msg),
                    "emergency-rebroadcast",
                )
            else:
                self._broadcast_message(rebroadcast_msg)
                self._messages_rebroadcast += 1

    def _delayed_rebroadcast(self, msg: dict[str, Any]) -> None:
        """Wait, then re-broadcast to avoid collision with other nodes."""
        self._sleep_while_active(self._rebroadcast_delay)
        if self._active:
            self._broadcast_message(msg)
            self._messages_rebroadcast += 1
            self.log.debug(
                "Re-broadcast emergency %s (TTL=%d)",
                msg.get("id", "")[:16],
                msg.get("ttl", 0),
            )

    def _broadcast_message(self, msg: dict[str, Any]) -> None:
        """Announce an emergency message over Reticulum."""
        try:
            payload = umsgpack.packb(msg)
            self.destination.announce(app_data=payload)
        except Exception:
            self.log.exception("Failed to broadcast emergency message")

    def _store_message(self, msg: dict[str, Any]) -> None:
        """Store a message in the local buffer."""
        self._messages.insert(0, msg)
        # Trim to max size
        while len(self._messages) > self._max_stored:
            self._messages.pop()

    @staticmethod
    def _generate_id(message: str, timestamp: float, origin_hash: bytes | None = None) -> str:
        """Generate a unique message ID including origin identity for deduplication."""
        origin = origin_hash.hex() if origin_hash else "local"
        raw = f"{origin}:{message}:{timestamp}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:32]


class _EmergencyHandler:
    """Registered with RNS.Transport to receive emergency broadcasts."""

    def __init__(self, plugin: EmergencyBroadcastPlugin):
        self._plugin = plugin
        self.aspect_filter = "reticulumpi.emergency.broadcast"

    def received_announce(
        self,
        destination_hash: bytes,
        announced_identity: Any,
        app_data: bytes | None,
    ) -> None:
        try:
            self._plugin.receive_emergency(destination_hash, app_data)
        except Exception:
            self._plugin.log.debug("Error handling emergency broadcast", exc_info=True)
