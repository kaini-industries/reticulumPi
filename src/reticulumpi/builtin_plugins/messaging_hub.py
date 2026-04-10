"""Unified Messaging Hub — transport-agnostic message store and routing.

Provides a central message store (SQLite) and a transport adapter registry.
Built-in adapters: LXMF (direct Reticulum messaging) and Meshtastic (bridges
to the meshtastic_gateway plugin).  Future transports register via
``hub.register_adapter(MyAdapter())``.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Any

import RNS
import RNS.vendor.umsgpack as umsgpack

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# Default limits
_DEFAULT_HISTORY_LIMIT = 500
_DEFAULT_DB_PATH = "~/.local/share/reticulumpi/messaging_hub.db"
_DEFAULT_LXMF_STORAGE = "~/.local/share/reticulumpi/messaging_hub_lxmf"


# ═══════════════════════════════════════════════════════════════════
# Transport Adapter Interface
# ═══════════════════════════════════════════════════════════════════


class TransportAdapter:
    """Base class for messaging transport adapters.

    Subclasses must set ``transport_name`` and ``display_name``, and
    implement ``send()``, ``get_contacts()``, and ``is_available()``.
    Adapters are lightweight plain objects — NOT plugins.
    """

    transport_name: str = ""
    display_name: str = ""

    def __init__(self) -> None:
        self._hub_callback: Any = None

    def send(self, text: str, destination: str, **kwargs: Any) -> dict[str, Any]:
        """Send a message.  Returns ``{"sent": bool, "reason"?: str, ...}``."""
        raise NotImplementedError

    def get_contacts(self) -> list[dict[str, Any]]:
        """Known reachable contacts for this transport.

        Each entry: ``{"id": str, "name": str, "transport": str, ...}``.
        """
        return []

    def is_available(self) -> bool:
        """Is this transport currently connected/available?"""
        return False

    def on_message_received(self, callback: Any) -> None:
        """Register the hub's inbound message handler."""
        self._hub_callback = callback

    def start(self) -> None:
        """Optional lifecycle hook — called when the adapter is registered."""

    def stop(self) -> None:
        """Optional lifecycle hook — called on hub shutdown."""


# ═══════════════════════════════════════════════════════════════════
# SQLite Message Store
# ═══════════════════════════════════════════════════════════════════


class MessageStore:
    """Thread-safe SQLite message store for all transports."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._create_tables()

    def _create_tables(self) -> None:
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    transport TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    msg_type TEXT NOT NULL,
                    from_id TEXT,
                    from_name TEXT,
                    to_id TEXT,
                    to_name TEXT,
                    text TEXT NOT NULL,
                    status TEXT DEFAULT 'sent',
                    metadata TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_msg_ts
                    ON messages(timestamp);
                CREATE INDEX IF NOT EXISTS idx_msg_transport
                    ON messages(transport);
            """)

    # ── Write ──────────────────────────────────────────────────────

    def store(
        self,
        transport: str,
        direction: str,
        msg_type: str,
        text: str,
        *,
        from_id: str | None = None,
        from_name: str | None = None,
        to_id: str | None = None,
        to_name: str | None = None,
        status: str = "sent",
        metadata: dict | None = None,
    ) -> int:
        """Insert a message and return its row ID."""
        ts = time.time()
        meta_json = json.dumps(metadata) if metadata else None
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO messages
                   (timestamp, transport, direction, msg_type,
                    from_id, from_name, to_id, to_name,
                    text, status, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts, transport, direction, msg_type,
                    from_id, from_name, to_id, to_name,
                    text, status, meta_json,
                ),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def update_status(self, msg_id: int, status: str) -> None:
        """Update the delivery status of a message."""
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET status = ? WHERE id = ?", (status, msg_id)
            )
            self._conn.commit()

    def prune(self, max_messages: int) -> int:
        """Delete oldest messages beyond *max_messages*.  Returns rows deleted."""
        if max_messages <= 0:
            return 0
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM messages")
            count = cur.fetchone()[0]
            if count <= max_messages:
                return 0
            excess = count - max_messages
            self._conn.execute(
                "DELETE FROM messages WHERE id IN "
                "(SELECT id FROM messages ORDER BY timestamp ASC LIMIT ?)",
                (excess,),
            )
            self._conn.commit()
            return excess

    # ── Read ───────────────────────────────────────────────────────

    def get_messages(
        self,
        limit: int = 50,
        offset: int = 0,
        transport: str | None = None,
        direction: str | None = None,
        since: float | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve messages with optional filters.  Newest first."""
        clauses: list[str] = []
        params: list[Any] = []
        if transport:
            clauses.append("transport = ?")
            params.append(transport)
        if direction:
            clauses.append("direction = ?")
            params.append(direction)
        if since is not None:
            clauses.append("timestamp > ?")
            params.append(since)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM messages{where} ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_message(self, msg_id: int) -> dict[str, Any] | None:
        """Retrieve a single message by ID."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE id = ?", (msg_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_stats(self) -> dict[str, Any]:
        """Return aggregate counts by transport and direction."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT transport, direction, COUNT(*) as cnt "
                "FROM messages GROUP BY transport, direction"
            ).fetchall()
            total = self._conn.execute(
                "SELECT COUNT(*) FROM messages"
            ).fetchone()[0]
        by_transport: dict[str, int] = {}
        by_direction: dict[str, int] = {}
        for r in rows:
            t, d, c = r["transport"], r["direction"], r["cnt"]
            by_transport[t] = by_transport.get(t, 0) + c
            by_direction[d] = by_direction.get(d, 0) + c
        return {
            "total": total,
            "by_transport": by_transport,
            "by_direction": by_direction,
        }

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        if d.get("metadata"):
            try:
                d["metadata"] = json.loads(d["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d


# ═══════════════════════════════════════════════════════════════════
# LXMF Propagation Announce Handler (shared pattern)
# ═══════════════════════════════════════════════════════════════════


class _LXMFPropagationHandler:
    """RNS announce handler that auto-selects the nearest LXMF propagation node."""

    def __init__(self, adapter: "LXMFAdapter") -> None:
        self.aspect_filter = "lxmf.propagation"
        self._adapter = adapter

    def received_announce(
        self, destination_hash: bytes, announced_identity: Any, app_data: bytes
    ) -> None:
        self._adapter._handle_propagation_announce(
            destination_hash, announced_identity, app_data
        )


# ═══════════════════════════════════════════════════════════════════
# LXMF Transport Adapter
# ═══════════════════════════════════════════════════════════════════


class LXMFAdapter(TransportAdapter):
    """LXMF adapter — send and receive messages over Reticulum.

    Follows the same identity/router pattern as message_echo, info_bot, etc.
    Creates its own LXMF identity so it gets a unique address.
    """

    transport_name = "lxmf"
    display_name = "LXMF"

    def __init__(self, hub: "MessagingHubPlugin") -> None:
        super().__init__()
        self._hub = hub
        self._router: Any = None
        self._destination: Any = None
        self._identity: Any = None
        self._propagation_handler: _LXMFPropagationHandler | None = None
        self._best_propagation_hops: int = 0

    def start(self) -> None:
        import LXMF

        cfg = self._hub.config.get("lxmf", {})
        storage_path = os.path.expanduser(
            cfg.get("storage_path", _DEFAULT_LXMF_STORAGE)
        )
        os.makedirs(storage_path, exist_ok=True)

        # Identity management — same pattern as message_echo.py lines 42-49
        identity_path = os.path.join(storage_path, "identity")
        if os.path.isfile(identity_path):
            self._identity = RNS.Identity.from_file(identity_path)
            self._hub.log.debug("Loaded messaging LXMF identity from %s", identity_path)
        else:
            self._identity = RNS.Identity()
            self._identity.to_file(identity_path)
            self._hub.log.info("Created new messaging LXMF identity at %s", identity_path)

        # LXMRouter.__init__ registers a SIGINT handler which only works
        # on the main thread.  When plugin startup runs in a worker thread
        # (for timeout enforcement), this raises ValueError.  Work around
        # it by temporarily making signal.signal a no-op on failure.
        import signal

        _orig_signal = signal.signal
        def _safe_signal(signum, handler):
            try:
                return _orig_signal(signum, handler)
            except ValueError:
                return None

        signal.signal = _safe_signal
        try:
            self._router = LXMF.LXMRouter(storagepath=storage_path)
        finally:
            signal.signal = _orig_signal
        display_name = cfg.get("display_name") or f"{self._hub.app.node_name} Messages"
        self._destination = self._router.register_delivery_identity(
            self._identity, display_name=display_name,
        )
        self._router.register_delivery_callback(self._on_lxmf_message)

        # Auto-select nearest propagation node for store-and-forward
        self._best_propagation_hops = RNS.Transport.PATHFINDER_M + 1
        self._propagation_handler = _LXMFPropagationHandler(self)
        RNS.Transport.register_announce_handler(self._propagation_handler)

        self._hub.log.info(
            "LXMF messaging active at %s",
            RNS.prettyhexrep(self._destination.hash),
        )

    def stop(self) -> None:
        if self._propagation_handler:
            RNS.Transport.deregister_announce_handler(self._propagation_handler)
        if self._router:
            self._router.register_delivery_callback(None)

    def send(self, text: str, destination: str, **kwargs: Any) -> dict[str, Any]:
        """Send an LXMF message to a destination hash (hex string)."""
        import LXMF

        if not self._router or not self._destination:
            return {"sent": False, "reason": "LXMF adapter not started"}

        try:
            dest_hash = bytes.fromhex(destination)
        except ValueError:
            return {"sent": False, "reason": f"Invalid destination hash: {destination}"}

        # Warm path if path_warmer is available
        warmer = self._hub.app.get_plugin("path_warmer")
        if warmer and hasattr(warmer, "ensure_path"):
            try:
                warmer.ensure_path(dest_hash)
            except Exception:
                pass

        dest_identity = RNS.Identity.recall(dest_hash)
        if dest_identity is None:
            RNS.Transport.request_path(dest_hash)
            return {"sent": False, "reason": "Path not found, requested"}

        try:
            dest = RNS.Destination(
                dest_identity,
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                "lxmf",
                "delivery",
            )
            msg = LXMF.LXMessage(
                dest,
                self._destination,
                text,
                desired_method=LXMF.LXMessage.OPPORTUNISTIC,
            )
            self._router.handle_outbound(msg)
            return {"sent": True, "destination": RNS.prettyhexrep(dest_hash)}
        except Exception as exc:
            return {"sent": False, "reason": str(exc)}

    def get_contacts(self) -> list[dict[str, Any]]:
        # LXMF doesn't maintain a contact list natively.
        # Future: cross-reference with network_map discovered nodes.
        return []

    def is_available(self) -> bool:
        return self._router is not None and self._destination is not None

    @property
    def address(self) -> str | None:
        """Return our LXMF address as a hex string, or None."""
        if self._destination:
            return self._destination.hash.hex()
        return None

    def _on_lxmf_message(self, message: Any) -> None:
        """Handle an incoming LXMF message and notify the hub."""
        if not self._hub_callback:
            return
        try:
            sender_hash = message.source_hash.hex()
            sender_pretty = RNS.prettyhexrep(message.source_hash)
            content = message.content_as_string()
            self._hub.log.info(
                "LXMF message from %s: %s", sender_pretty, content[:80]
            )
            self._hub_callback({
                "transport": "lxmf",
                "from_id": sender_hash,
                "from_name": sender_pretty,
                "to_id": self.address,
                "to_name": None,
                "text": content,
                "msg_type": "direct",
                "metadata": {"source_hash": sender_pretty},
            })
        except Exception:
            self._hub.log.exception("Error handling incoming LXMF message")

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
            if hops < self._best_propagation_hops:
                self._best_propagation_hops = hops
                self._router.set_outbound_propagation_node(destination_hash)
                self._hub.log.info(
                    "Auto-selected propagation node %s (%d hops)",
                    RNS.prettyhexrep(destination_hash),
                    hops,
                )
        except Exception:
            self._hub.log.exception("Error handling propagation node announce")


# ═══════════════════════════════════════════════════════════════════
# Meshtastic Transport Adapter
# ═══════════════════════════════════════════════════════════════════


class MeshtasticAdapter(TransportAdapter):
    """Meshtastic adapter — bridges to the meshtastic_gateway plugin.

    Does NOT own the Meshtastic connection.  Delegates sending to the
    gateway's ``send_message()`` method and subscribes to its events
    for inbound messages.
    """

    transport_name = "meshtastic"
    display_name = "Meshtastic"

    def __init__(self, hub: "MessagingHubPlugin") -> None:
        super().__init__()
        self._hub = hub

    def start(self) -> None:
        self._hub.event_bus.subscribe(
            events.MESHTASTIC_MESSAGE_RECEIVED, self._on_mesh_event
        )

    def stop(self) -> None:
        self._hub.event_bus.unsubscribe(
            events.MESHTASTIC_MESSAGE_RECEIVED, self._on_mesh_event
        )

    def _on_mesh_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Event bus callback for incoming Meshtastic messages."""
        if not self._hub_callback:
            return
        from_id = data.get("from_id", "")
        from_name = self._resolve_node_name(from_id) or from_id
        self._hub_callback({
            "transport": "meshtastic",
            "from_id": from_id,
            "from_name": from_name,
            "to_id": None,
            "to_name": None,
            "text": data.get("text", ""),
            "msg_type": "broadcast",
            "metadata": {k: v for k, v in data.items() if k not in ("text",)},
        })

    def _resolve_node_name(self, node_id: str) -> str | None:
        """Look up a Meshtastic node's human-readable name from the gateway."""
        try:
            gw = self._hub.app.get_plugin("meshtastic_gateway")
            if not gw or not hasattr(gw, "get_meshtastic_nodes"):
                return None
            for n in gw.get_meshtastic_nodes():
                if n.get("id") == node_id:
                    return n.get("long_name") or n.get("short_name") or None
        except Exception:
            pass
        return None

    def send(self, text: str, destination: str, **kwargs: Any) -> dict[str, Any]:
        gw = self._hub.app.get_plugin("meshtastic_gateway")
        if not gw or not hasattr(gw, "send_message"):
            return {"sent": False, "reason": "meshtastic_gateway plugin not available"}
        dest_id = destination if destination and destination != "broadcast" else None
        return gw.send_message(text, destination_id=dest_id)

    def get_contacts(self) -> list[dict[str, Any]]:
        gw = self._hub.app.get_plugin("meshtastic_gateway")
        if not gw or not hasattr(gw, "get_meshtastic_nodes"):
            return []
        return [
            {
                "id": n["id"],
                "name": n.get("long_name") or n.get("short_name") or n["id"],
                "transport": "meshtastic",
            }
            for n in gw.get_meshtastic_nodes()
            if not n.get("is_self")
        ]

    def is_available(self) -> bool:
        gw = self._hub.app.get_plugin("meshtastic_gateway")
        if not gw:
            return False
        try:
            return gw.get_status().get("connected", False)
        except Exception:
            return False


# ═══════════════════════════════════════════════════════════════════
# Messaging Hub Plugin
# ═══════════════════════════════════════════════════════════════════


class MessagingHubPlugin(PluginBase):
    """Unified messaging hub with transport-agnostic message store."""

    plugin_name = "messaging_hub"
    plugin_version = "1.0.0"
    plugin_description = "Unified message store and chat hub for LXMF and Meshtastic"

    def validate_config(self) -> None:
        limit = self.config.get("message_history_limit", _DEFAULT_HISTORY_LIMIT)
        if not isinstance(limit, int) or limit < 0:
            raise ValueError("message_history_limit must be a non-negative integer")

    def start(self) -> None:
        self._lock = threading.Lock()
        self._adapters: dict[str, TransportAdapter] = {}

        # Initialize SQLite store
        db_path = os.path.expanduser(
            self.config.get("db_path", _DEFAULT_DB_PATH)
        )
        self._store = MessageStore(db_path)
        self._history_limit = self.config.get(
            "message_history_limit", _DEFAULT_HISTORY_LIMIT
        )

        # Register built-in LXMF adapter
        lxmf_cfg = self.config.get("lxmf", {})
        if lxmf_cfg.get("enabled", True):
            lxmf_adapter = LXMFAdapter(self)
            self.register_adapter(lxmf_adapter)

        # Register Meshtastic adapter (bridges to gateway plugin)
        mesh_cfg = self.config.get("meshtastic", {})
        if mesh_cfg.get("enabled", True):
            mesh_adapter = MeshtasticAdapter(self)
            self.register_adapter(mesh_adapter)

        self._active = True
        self.log.info(
            "Messaging hub started with %d transport(s): %s",
            len(self._adapters),
            ", ".join(self._adapters.keys()),
        )

    def stop(self) -> None:
        self._active = False
        for adapter in list(self._adapters.values()):
            try:
                adapter.stop()
            except Exception:
                self.log.exception(
                    "Error stopping adapter %s", adapter.transport_name
                )
        self._adapters.clear()
        if hasattr(self, "_store"):
            self._store.close()
        self._join_threads()

    # ── Adapter registry ───────────────────────────────────────────

    def register_adapter(self, adapter: TransportAdapter) -> None:
        """Register a transport adapter.

        Called during ``start()`` for built-in adapters, or by external
        plugins for custom transports.
        """
        adapter.on_message_received(self._on_adapter_message)
        try:
            adapter.start()
        except Exception:
            self.log.exception(
                "Failed to start adapter %s", adapter.transport_name
            )
            return
        with self._lock:
            self._adapters[adapter.transport_name] = adapter
        self.log.info("Registered transport adapter: %s", adapter.display_name)

    # ── Inbound ────────────────────────────────────────────────────

    def _on_adapter_message(self, msg: dict[str, Any]) -> None:
        """Callback from adapters when a message is received."""
        try:
            msg_id = self._store.store(
                transport=msg["transport"],
                direction="received",
                msg_type=msg.get("msg_type", "direct"),
                text=msg["text"],
                from_id=msg.get("from_id"),
                from_name=msg.get("from_name"),
                to_id=msg.get("to_id"),
                to_name=msg.get("to_name"),
                status="received",
                metadata=msg.get("metadata"),
            )
            self._maybe_prune()
            self.event_bus.publish(events.MESSAGE_RECEIVED, {
                "id": msg_id,
                "transport": msg["transport"],
                "from_id": msg.get("from_id"),
                "from_name": msg.get("from_name"),
                "text": msg["text"][:100],
                "msg_type": msg.get("msg_type", "direct"),
                "timestamp": time.time(),
            })
        except Exception:
            self.log.exception("Error storing inbound message")

    # ── Outbound ───────────────────────────────────────────────────

    def send_message(
        self,
        transport: str,
        text: str,
        destination: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a message via the named transport.

        Returns ``{"sent": bool, "msg_id": int, ...}`` on success, or
        ``{"sent": False, "reason": str}`` on failure.
        """
        with self._lock:
            adapter = self._adapters.get(transport)
        if not adapter:
            return {"sent": False, "reason": f"Transport '{transport}' not registered"}
        if not adapter.is_available():
            return {"sent": False, "reason": f"Transport '{transport}' not available"}

        result = adapter.send(text, destination, **kwargs)

        # Resolve destination name from contacts if possible
        to_name = kwargs.get("to_name")
        if not to_name:
            for c in adapter.get_contacts():
                if c.get("id") == destination:
                    to_name = c.get("name")
                    break

        msg_id = self._store.store(
            transport=transport,
            direction="sent",
            msg_type=kwargs.get("msg_type", "direct"),
            text=text,
            from_id="self",
            from_name=self.app.node_name,
            to_id=destination,
            to_name=to_name,
            status="sent" if result.get("sent") else "failed",
            metadata=kwargs.get("metadata"),
        )
        self._maybe_prune()

        if result.get("sent"):
            self.event_bus.publish(events.MESSAGE_SENT, {
                "id": msg_id,
                "transport": transport,
                "destination": destination,
                "text": text[:100],
                "timestamp": time.time(),
            })

        return {**result, "msg_id": msg_id}

    # ── Queries (used by dashboard API) ────────────────────────────

    def get_messages(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Retrieve messages from the store with optional filters."""
        return self._store.get_messages(**kwargs)

    def get_transports(self) -> list[dict[str, Any]]:
        """Return registered transports with availability status."""
        with self._lock:
            adapters = list(self._adapters.values())
        result = []
        for a in adapters:
            info: dict[str, Any] = {
                "name": a.transport_name,
                "display": a.display_name,
                "available": a.is_available(),
            }
            # Include LXMF address if available
            if hasattr(a, "address") and a.address:
                info["address"] = a.address
            result.append(info)
        return result

    def get_contacts(self, transport: str | None = None) -> list[dict[str, Any]]:
        """Aggregate contacts from all or a specific transport."""
        with self._lock:
            adapters = list(self._adapters.values())
        contacts: list[dict[str, Any]] = []
        for a in adapters:
            if transport and a.transport_name != transport:
                continue
            try:
                contacts.extend(a.get_contacts())
            except Exception:
                self.log.debug(
                    "Error getting contacts from %s", a.transport_name,
                    exc_info=True,
                )
        return contacts

    def get_stats(self) -> dict[str, Any]:
        """Return message statistics."""
        return self._store.get_stats()

    def get_status(self) -> dict[str, Any]:
        stats = self._store.get_stats()
        with self._lock:
            transports = {
                name: adapter.is_available()
                for name, adapter in self._adapters.items()
            }
        return {
            "active": self._active,
            "transports": transports,
            "total_messages": stats.get("total", 0),
            "by_transport": stats.get("by_transport", {}),
            "by_direction": stats.get("by_direction", {}),
        }

    # ── Internal ───────────────────────────────────────────────────

    def _maybe_prune(self) -> None:
        """Prune old messages if history limit is set."""
        if self._history_limit > 0:
            try:
                self._store.prune(self._history_limit)
            except Exception:
                self.log.debug("Error pruning messages", exc_info=True)
