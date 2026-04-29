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
from collections import deque
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
    max_message_bytes: int | None = None

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
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        # Wall-clock timestamps can jump backwards (NTP correction, manual
        # clock changes). Track the highest timestamp we've written so we
        # can keep the stored message order monotonic even across a jump.
        self._last_ts: float = 0.0
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
            self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Add conversation-related columns if missing (v2/v3 migration)."""
        cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "contact_id" not in cols:
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN contact_id TEXT"
            )
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN read INTEGER DEFAULT 0"
            )
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN search_text TEXT"
            )
            # Backfill contact_id from existing data
            self._conn.execute("""
                UPDATE messages SET contact_id = CASE
                    WHEN msg_type = 'broadcast'
                        THEN '__broadcast_' || transport || '__'
                    WHEN direction = 'sent'
                        THEN COALESCE(to_id, '__unknown__')
                    ELSE COALESCE(from_id, '__unknown__')
                END
                WHERE contact_id IS NULL
            """)
            # Backfill search_text
            self._conn.execute(
                "UPDATE messages SET search_text = lower(text) "
                "WHERE search_text IS NULL"
            )
            # Mark all existing received messages as read
            self._conn.execute(
                "UPDATE messages SET read = 1 "
                "WHERE direction = 'received' AND read = 0"
            )
            self._conn.commit()

        # v3: sub_transport column for MQTT/LoRa split on Meshtastic
        if "sub_transport" not in cols:
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN sub_transport TEXT DEFAULT ''"
            )
            # Historical Meshtastic messages had sub_transport embedded in
            # contact_id for broadcasts only.  Back-fill the new column
            # from that, and assume "lora" for any Meshtastic DM whose
            # sub_transport we can't recover — LoRa is by far the most
            # common path for pre-existing users.
            self._conn.execute("""
                UPDATE messages
                SET sub_transport = CASE
                    WHEN contact_id = '__broadcast_meshtastic_mqtt__' THEN 'mqtt'
                    WHEN contact_id = '__broadcast_meshtastic_lora__' THEN 'lora'
                    WHEN transport = 'meshtastic' AND msg_type = 'direct' THEN 'lora'
                    ELSE ''
                END
                WHERE sub_transport IS NULL OR sub_transport = ''
            """)
            # Rewrite Meshtastic DM contact_ids to include the sub_transport
            # suffix so each panel sees its own conversation thread.
            self._conn.execute("""
                UPDATE messages
                SET contact_id = contact_id || '__' || sub_transport
                WHERE transport = 'meshtastic'
                  AND msg_type = 'direct'
                  AND sub_transport <> ''
                  AND contact_id NOT LIKE '%\\_\\_mqtt' ESCAPE '\\'
                  AND contact_id NOT LIKE '%\\_\\_lora' ESCAPE '\\'
            """)
            self._conn.commit()

        # Ensure indexes exist (idempotent)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_msg_contact "
            "ON messages(contact_id)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_msg_contact_ts "
            "ON messages(contact_id, timestamp DESC)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_msg_read "
            "ON messages(read) WHERE read = 0"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_msg_sub_transport "
            "ON messages(transport, sub_transport)"
        )
        # One-time cleanup (idempotent): pre-channel-split Meshtastic
        # broadcast rows can't be re-bucketed by channel, so drop them.
        self._conn.execute(
            "DELETE FROM messages WHERE msg_type = 'broadcast' "
            "AND transport = 'meshtastic' "
            "AND contact_id IN ("
            "  '__broadcast_meshtastic_lora__', "
            "  '__broadcast_meshtastic_mqtt__'"
            ")"
        )
        # One-time cleanup (idempotent): an interim MQTT path keyed
        # broadcast threads by the envelope channel-name string (e.g.
        # __broadcast_meshtastic_mqtt_chLongFast__) while outbound kept
        # using the configured local slot index, which split the same
        # conversation across two rows.  Inbound now also uses the
        # local index, so any surviving name-keyed rows are orphans
        # and can be dropped.  GLOB matches rows whose channel suffix
        # contains a letter; index-keyed rows (digits only) are left
        # untouched.
        self._conn.execute(
            "DELETE FROM messages WHERE msg_type = 'broadcast' "
            "AND transport = 'meshtastic' AND sub_transport = 'mqtt' "
            "AND contact_id GLOB '__broadcast_meshtastic_mqtt_ch*[A-Za-z]*__'"
        )
        # One-time cleanup (idempotent): MeshCore broadcasts briefly
        # inherited the meshtastic channel-suffix scheme, but the
        # MeshCore panel doesn't support per-channel threads, so
        # outbound landed at "__broadcast_meshcore_ch0__" while inbound
        # stayed at "__broadcast_meshcore__".  Fold any surviving
        # _chN suffixed rows back onto the canonical cid.
        self._conn.execute(
            "UPDATE messages "
            "SET contact_id = '__broadcast_meshcore__' "
            "WHERE msg_type = 'broadcast' AND transport = 'meshcore' "
            "AND contact_id GLOB '__broadcast_meshcore_ch*__'"
        )
        self._conn.commit()

    @staticmethod
    def _compute_contact_id(
        direction: str, msg_type: str, transport: str,
        from_id: str | None, to_id: str | None,
        sub_transport: str = "",
        channel: int | str | None = None,
    ) -> str:
        """Compute the contact_id for a message.

        ``sub_transport`` (e.g. "mqtt" or "lora") creates separate
        conversations per source channel for broadcasts AND direct
        messages.  This lets the dashboard split the same Meshtastic
        peer's MQTT vs LoRa traffic into their own panels.

        ``channel`` further splits broadcast threads by Meshtastic
        channel.  Usually an integer index (local radio channel), but
        may be a string channel name when the source (typically MQTT)
        reports a channel we don't have mapped locally.  DMs ignore
        channel (same peer is the same conversation regardless of
        which channel carried it).  Other transports (meshcore, lxmf)
        don't expose per-channel broadcast threads in the UI, so the
        channel suffix is only applied for meshtastic — this keeps all
        meshcore public-channel traffic on the single pinned broadcast
        row the panel renders.
        """
        if msg_type == "broadcast":
            use_channel = channel is not None and transport == "meshtastic"
            ch_suffix = f"_ch{channel}" if use_channel else ""
            if sub_transport:
                return f"__broadcast_{transport}_{sub_transport}{ch_suffix}__"
            return f"__broadcast_{transport}{ch_suffix}__"
        peer = (to_id if direction == "sent" else from_id) or "__unknown__"
        if sub_transport:
            return f"{peer}__{sub_transport}"
        return peer

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
        sub_transport: str = "",
        channel: int | str | None = None,
    ) -> int:
        """Insert a message and return its row ID."""
        now = time.time()
        if channel is not None:
            metadata = dict(metadata) if metadata else {}
            metadata.setdefault("channel", channel)
        meta_json = json.dumps(metadata) if metadata else None
        contact_id = self._compute_contact_id(
            direction, msg_type, transport, from_id, to_id,
            sub_transport=sub_transport, channel=channel,
        )
        search_text = text.lower() if text else None
        # New received messages default to unread
        read_flag = 0 if direction == "received" else 1
        with self._lock:
            # Guarantee monotonic insert order even if the wall clock
            # jumped backwards — readers sort by timestamp so a regression
            # would reshuffle history. Nudge forward by 1 ms per conflict.
            if now <= self._last_ts:
                if self._last_ts - now > 5.0:
                    log.warning(
                        "Wall clock regressed by %.1fs while storing a "
                        "message; compensating to keep history ordered.",
                        self._last_ts - now,
                    )
                ts = self._last_ts + 0.001
            else:
                ts = now
            self._last_ts = ts
            cur = self._conn.execute(
                """INSERT INTO messages
                   (timestamp, transport, direction, msg_type,
                    from_id, from_name, to_id, to_name,
                    text, status, metadata,
                    contact_id, read, search_text, sub_transport)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts, transport, direction, msg_type,
                    from_id, from_name, to_id, to_name,
                    text, status, meta_json,
                    contact_id, read_flag, search_text, sub_transport,
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

    def add_reaction(
        self,
        packet_id: int,
        emoji: str,
        from_id: str,
        from_name: str | None = None,
    ) -> int | None:
        """Append an emoji reaction to the message matching *packet_id*.

        Looks up the target message by ``metadata.packet_id`` (stored on
        Meshtastic text messages).  Returns the DB row id of the updated
        message, or ``None`` if no match was found.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT id, metadata FROM messages "
                "WHERE json_extract(metadata, '$.packet_id') = ? "
                "AND transport = 'meshtastic' "
                "ORDER BY timestamp DESC LIMIT 1",
                (packet_id,),
            ).fetchone()
            if not row:
                return None
            msg_id = row[0]
            raw_meta = row[1]
            try:
                meta = json.loads(raw_meta) if raw_meta else {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
            reactions = meta.get("reactions", [])
            if any(r["emoji"] == emoji and r["from_id"] == from_id for r in reactions):
                return msg_id
            reactions.append({
                "emoji": emoji,
                "from_id": from_id,
                "from_name": from_name or from_id,
                "timestamp": time.time(),
            })
            meta["reactions"] = reactions
            self._conn.execute(
                "UPDATE messages SET metadata = ? WHERE id = ?",
                (json.dumps(meta), msg_id),
            )
            self._conn.commit()
            return msg_id

    def get_status(self, msg_id: int) -> str | None:
        """Return the current delivery status for *msg_id*, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM messages WHERE id = ?", (msg_id,)
            ).fetchone()
            return row[0] if row else None

    def prune(self, max_messages: int) -> int:
        """Delete oldest messages beyond *max_messages* PER (transport, sub_transport).

        Pruning is bucketed per ``(transport, sub_transport)`` so a chatty
        channel can't starve a sparse one.  Originally a single global cap
        let MQTT evict LXMF; bucketing by transport fixed that but left a
        second-order version of the same bug inside Meshtastic, where
        chatty MQTT broadcasts (~500/day) evicted rare LoRa direct
        messages from the shared 500-row Meshtastic bucket.  Each
        ``(transport, sub_transport)`` pair now independently keeps its
        newest ``max_messages`` rows.  Returns total rows deleted.
        """
        if max_messages <= 0:
            return 0
        with self._lock:
            over_quota = self._conn.execute(
                "SELECT transport, COALESCE(sub_transport, '') FROM messages "
                "GROUP BY transport, COALESCE(sub_transport, '') "
                "HAVING COUNT(*) > ?",
                (max_messages,),
            ).fetchall()
            total_deleted = 0
            for tr, sub in over_quota:
                count = self._conn.execute(
                    "SELECT COUNT(*) FROM messages "
                    "WHERE transport = ? AND COALESCE(sub_transport, '') = ?",
                    (tr, sub),
                ).fetchone()[0]
                excess = count - max_messages
                if excess <= 0:
                    continue
                self._conn.execute(
                    "DELETE FROM messages WHERE id IN ("
                    "  SELECT id FROM messages "
                    "  WHERE transport = ? AND COALESCE(sub_transport, '') = ? "
                    "  ORDER BY timestamp ASC LIMIT ?"
                    ")",
                    (tr, sub, excess),
                )
                total_deleted += excess
            if total_deleted:
                self._conn.commit()
            return total_deleted

    # ── Read ───────────────────────────────────────────────────────

    def get_messages(
        self,
        limit: int = 50,
        offset: int = 0,
        transport: str | None = None,
        direction: str | None = None,
        since: float | None = None,
        sub_transport: str | None = None,
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
        if sub_transport is not None:
            clauses.append("sub_transport = ?")
            params.append(sub_transport)
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

    # ── Conversation queries ─────────────────────────────────────────

    def get_conversations(
        self, transport: str | None = None,
        sub_transport: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return conversation summaries, one per contact_id.

        Each entry: ``{contact_id, contact_name, transport, sub_transport,
        msg_type, last_text, last_ts, unread_count}``.  Ordered by most
        recent first.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if transport:
            clauses.append("transport = ?")
            params.append(transport)
        if sub_transport is not None:
            clauses.append("sub_transport = ?")
            params.append(sub_transport)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        # Group on every non-aggregate column so the per-group value is
        # deterministic. ``transport``, ``sub_transport``, and ``msg_type``
        # are functionally dependent on ``contact_id`` (baked into the
        # contact_id itself for broadcasts; invariant per-peer for DMs),
        # so adding them to GROUP BY doesn't fragment groups — it just
        # tells SQLite we promise they're constant within a group.
        # COALESCE keeps a NULL sub_transport (from pre-migration rows)
        # from bucketing separately to the ``''`` default.
        sql = f"""
            SELECT contact_id,
                   transport,
                   COALESCE(sub_transport, '') AS sub_transport,
                   msg_type,
                   MAX(timestamp) AS last_ts,
                   MAX(CASE
                       WHEN direction = 'received' THEN from_name
                       ELSE to_name
                   END) AS contact_name,
                   SUM(CASE
                       WHEN direction = 'received' AND read = 0 THEN 1
                       ELSE 0
                   END) AS unread_count,
                   (SELECT m2.text FROM messages m2
                    WHERE m2.contact_id = messages.contact_id
                    ORDER BY m2.timestamp DESC LIMIT 1
                   ) AS last_text
            FROM messages{where}
            GROUP BY contact_id, transport, COALESCE(sub_transport, ''), msg_type
            ORDER BY last_ts DESC
        """
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "contact_id": r["contact_id"],
                "contact_name": r["contact_name"],
                "transport": r["transport"],
                "sub_transport": r["sub_transport"] or "",
                "msg_type": r["msg_type"],
                "last_ts": r["last_ts"],
                "last_text": r["last_text"],
                "unread_count": r["unread_count"],
            }
            for r in rows
        ]

    def get_conversation_messages(
        self,
        contact_id: str,
        limit: int = 50,
        before: float | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch messages for a single conversation, newest first."""
        params: list[Any] = [contact_id]
        time_clause = ""
        if before is not None:
            time_clause = " AND timestamp < ?"
            params.append(before)
        params.append(limit)
        sql = (
            "SELECT * FROM messages "
            f"WHERE contact_id = ?{time_clause} "
            "ORDER BY timestamp DESC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def search_messages(
        self,
        query: str,
        limit: int = 50,
        transport: str | None = None,
        sub_transport: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search messages by text content (case-insensitive)."""
        clauses = ["search_text LIKE ?"]
        params: list[Any] = [f"%{query.lower()}%"]
        if transport:
            clauses.append("transport = ?")
            params.append(transport)
        if sub_transport is not None:
            clauses.append("sub_transport = ?")
            params.append(sub_transport)
        where = " AND ".join(clauses)
        params.append(limit)
        sql = (
            f"SELECT * FROM messages WHERE {where} "
            "ORDER BY timestamp DESC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def mark_read(self, contact_id: str) -> int:
        """Mark all unread received messages from a contact as read.

        Returns the number of messages updated.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE messages SET read = 1 "
                "WHERE contact_id = ? AND direction = 'received' AND read = 0",
                (contact_id,),
            )
            self._conn.commit()
            return cur.rowcount

    def delete_conversation(self, contact_id: str) -> int:
        """Delete all messages for a conversation.  Returns rows deleted."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM messages WHERE contact_id = ?", (contact_id,),
            )
            self._conn.commit()
            return cur.rowcount

    def get_unread_counts(
        self,
        transport: str | None = None,
        sub_transport: str | None = None,
    ) -> dict[str, int]:
        """Return ``{contact_id: unread_count}`` for contacts with unread > 0."""
        clauses = ["direction = 'received'", "read = 0"]
        params: list[Any] = []
        if transport:
            clauses.append("transport = ?")
            params.append(transport)
        if sub_transport is not None:
            clauses.append("sub_transport = ?")
            params.append(sub_transport)
        where = " AND ".join(clauses)
        sql = (
            f"SELECT contact_id, COUNT(*) AS cnt FROM messages "
            f"WHERE {where} GROUP BY contact_id"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return {r["contact_id"]: r["cnt"] for r in rows}

    def get_unread_counts_grouped(self) -> dict[str, dict[str, int]]:
        """Return ``{bucket_key: {contact_id: count}}`` for all transports.

        ``bucket_key`` is ``transport`` when ``sub_transport`` is empty, else
        ``transport:sub_transport`` — matches the per-panel key the web
        dashboard computes from ``cfg.transport`` / ``cfg.subTransport``.
        """
        sql = (
            "SELECT transport, COALESCE(sub_transport, '') AS sub, "
            "contact_id, COUNT(*) AS cnt FROM messages "
            "WHERE direction = 'received' AND read = 0 "
            "GROUP BY transport, sub, contact_id"
        )
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        grouped: dict[str, dict[str, int]] = {}
        for r in rows:
            key = f"{r['transport']}:{r['sub']}" if r["sub"] else r["transport"]
            grouped.setdefault(key, {})[r["contact_id"]] = r["cnt"]
        return grouped

    def get_peer_sub_transports(self, transport: str) -> dict[str, set[str]]:
        """Return ``{peer_id: {sub_transport, ...}}`` for DMs on *transport*.

        Used by the contacts endpoint to filter by sub_transport when the
        upstream adapter doesn't tag its contact list: a peer we've only
        ever exchanged MQTT DMs with shouldn't appear in the LoRa panel.
        Only considers ``direct`` messages — broadcasts don't define a
        "peer" in the conversational sense.
        """
        sql = (
            "SELECT DISTINCT "
            "  CASE WHEN direction = 'sent' THEN to_id ELSE from_id END AS peer_id, "
            "  COALESCE(sub_transport, '') AS sub "
            "FROM messages "
            "WHERE transport = ? AND msg_type = 'direct'"
        )
        out: dict[str, set[str]] = {}
        with self._lock:
            rows = self._conn.execute(sql, (transport,)).fetchall()
        for r in rows:
            peer = r["peer_id"]
            if not peer or peer == "self":
                continue
            out.setdefault(peer, set()).add(r["sub"])
        return out

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

    _MAX_PROP_NODES = 3
    _PROP_STALE_S = 3600  # 1 hour
    _IDENTITY_CACHE_TTL = 300.0  # 5 minutes

    def __init__(self, hub: "MessagingHubPlugin") -> None:
        super().__init__()
        self._hub = hub
        self._router: Any = None
        self._destination: Any = None
        self._identity: Any = None
        self._announce_sub: str | None = None
        # Propagation node fallback list: sorted by (hops, -last_seen)
        self._propagation_nodes: list[dict[str, Any]] = []
        self._current_prop_node: bytes | None = None
        self._prop_lock = threading.Lock()
        # Track outbound messages awaiting delivery confirmation:
        # {lxm_hash_hex: {"msg_id": int, "timestamp": float}}
        self._pending_delivery: dict[str, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        # Identity recall cache: dest_hash_hex -> (identity, timestamp)
        self._identity_cache: dict[str, tuple[Any, float]] = {}

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
        # (for timeout enforcement), this raises ValueError.  Suppress it
        # by patching the module-level attribute that LXMRouter reads.
        import signal
        import threading

        if threading.current_thread() is not threading.main_thread():
            _orig_signal = signal.signal
            signal.signal = lambda *_a, **_kw: None
            try:
                self._router = LXMF.LXMRouter(storagepath=storage_path)
            finally:
                signal.signal = _orig_signal
        else:
            self._router = LXMF.LXMRouter(storagepath=storage_path)
        display_name = cfg.get("display_name") or f"{self._hub.app.node_name} Messages"
        self._destination = self._router.register_delivery_identity(
            self._identity, display_name=display_name,
        )
        self._router.register_delivery_callback(self._on_lxmf_message)

        # Auto-select nearest propagation node for store-and-forward
        self._announce_sub = self._hub.announce_dispatcher.subscribe(
            "lxmf.propagation", self._handle_propagation_announce,
        )

        self._hub.log.info(
            "LXMF messaging active at %s",
            RNS.prettyhexrep(self._destination.hash),
        )

    def stop(self) -> None:
        if self._announce_sub:
            self._hub.announce_dispatcher.unsubscribe(self._announce_sub)
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

        dest_identity = self._recall_identity(dest_hash)
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
            msg.register_delivery_callback(self._on_lxmf_delivery)
            msg.register_failed_callback(self._on_lxmf_failed)
            self._router.handle_outbound(msg)
            return {
                "sent": True,
                "destination": RNS.prettyhexrep(dest_hash),
                "lxm_hash": msg.hash.hex() if msg.hash else None,
            }
        except Exception as exc:
            return {"sent": False, "reason": str(exc)}

    def track_pending(self, msg_id: int, lxm_hash: str | None) -> None:
        """Register an outbound message for delivery tracking."""
        if not lxm_hash:
            return
        with self._pending_lock:
            self._pending_delivery[lxm_hash] = {
                "msg_id": msg_id,
                "timestamp": time.time(),
            }

    def get_pending_delivery(self) -> dict[str, dict[str, Any]]:
        """Return a copy of the pending delivery tracker."""
        with self._pending_lock:
            return dict(self._pending_delivery)

    def _on_lxmf_delivery(self, message: Any) -> None:
        """LXMF delivery callback — message was delivered or propagated."""
        try:
            import LXMF

            lxm_hash = message.hash.hex() if message.hash else None
            if not lxm_hash:
                return
            with self._pending_lock:
                entry = self._pending_delivery.pop(lxm_hash, None)
            if not entry:
                return

            # DELIVERED = confirmed at recipient; SENT = accepted by propagation node
            if message.state == LXMF.LXMessage.DELIVERED:
                status = "delivered"
            else:
                status = "propagated"

            self._hub.log.info(
                "LXMF message %s: %s (msg_id=%d)",
                lxm_hash[:12], status, entry["msg_id"],
            )
            self._hub._on_delivery_status_update(
                entry["msg_id"], "lxmf", status,
            )
        except Exception:
            self._hub.log.exception("Error in LXMF delivery callback")

    def _on_lxmf_failed(self, message: Any) -> None:
        """LXMF failed callback — message delivery failed."""
        try:
            lxm_hash = message.hash.hex() if message.hash else None
            if not lxm_hash:
                return
            with self._pending_lock:
                entry = self._pending_delivery.pop(lxm_hash, None)
            if not entry:
                return

            self._hub.log.warning(
                "LXMF message %s: delivery_failed (msg_id=%d)",
                lxm_hash[:12], entry["msg_id"],
            )
            self._hub._on_delivery_status_update(
                entry["msg_id"], "lxmf", "delivery_failed",
            )

            # Invalidate identity cache for this destination
            try:
                if hasattr(message, "destination_hash") and message.destination_hash:
                    with self._pending_lock:
                        self._identity_cache.pop(message.destination_hash.hex(), None)
            except Exception:
                pass

            # Check if propagation node needs failover
            self._check_propagation_failover()
        except Exception:
            self._hub.log.exception("Error in LXMF failed callback")

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

    def _resolve_lxmf_name(self, dest_hash_hex: str) -> str | None:
        """Look up an LXMF peer's announced display name via network_map."""
        try:
            nm = self._hub.app.get_plugin("network_map")
            if nm and hasattr(nm, "get_node_name"):
                return nm.get_node_name(dest_hash_hex)
        except Exception:
            pass
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
                "from_name": self._resolve_lxmf_name(sender_hash) or sender_pretty,
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
        """Maintain a ranked list of propagation nodes and select the best."""
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
            now = time.time()

            with self._prop_lock:
                existing = None
                for entry in self._propagation_nodes:
                    if entry["hash"] == destination_hash:
                        existing = entry
                        break
                if existing:
                    existing["hops"] = hops
                    existing["last_seen"] = now
                    existing["failures"] = 0
                else:
                    self._propagation_nodes.append({
                        "hash": destination_hash,
                        "hops": hops,
                        "last_seen": now,
                        "failures": 0,
                    })

                # Prune stale entries
                self._propagation_nodes = [
                    e for e in self._propagation_nodes
                    if now - e["last_seen"] < self._PROP_STALE_S
                ]
                # Sort by hops, then freshness
                self._propagation_nodes.sort(
                    key=lambda e: (e["hops"], -e["last_seen"])
                )
                self._propagation_nodes = self._propagation_nodes[:self._MAX_PROP_NODES]

                best = self._select_best_prop_node()

            if best and best != self._current_prop_node:
                self._current_prop_node = best
                self._router.set_outbound_propagation_node(best)
                self._hub.log.info(
                    "Selected propagation node %s (%d hops, %d candidates)",
                    RNS.prettyhexrep(best),
                    hops,
                    len(self._propagation_nodes),
                )
        except Exception:
            self._hub.log.exception("Error handling propagation node announce")

    def _select_best_prop_node(self) -> bytes | None:
        """Pick best propagation node (fewest hops, <3 failures). Caller holds lock."""
        for entry in self._propagation_nodes:
            if entry["failures"] < 3:
                return entry["hash"]
        if self._propagation_nodes:
            return min(self._propagation_nodes, key=lambda e: e["failures"])["hash"]
        return None

    def _check_propagation_failover(self) -> None:
        """Increment failure count on current prop node; failover if needed."""
        with self._prop_lock:
            if not self._current_prop_node or not self._propagation_nodes:
                return
            for entry in self._propagation_nodes:
                if entry["hash"] == self._current_prop_node:
                    entry["failures"] += 1
                    break
            new_best = self._select_best_prop_node()
        if new_best and new_best != self._current_prop_node:
            self._current_prop_node = new_best
            self._router.set_outbound_propagation_node(new_best)
            self._hub.log.warning(
                "Propagation node failover to %s",
                RNS.prettyhexrep(new_best),
            )

    def _recall_identity(self, dest_hash: bytes) -> Any:
        """Recall an identity, using a short-lived cache to avoid redundant lookups."""
        dest_hex = dest_hash.hex()
        now = time.time()
        with self._pending_lock:
            cached = self._identity_cache.get(dest_hex)
            if cached is not None:
                identity, cached_at = cached
                if now - cached_at < self._IDENTITY_CACHE_TTL:
                    return identity
                del self._identity_cache[dest_hex]

        identity = RNS.Identity.recall(dest_hash)

        if identity is not None:
            with self._pending_lock:
                self._identity_cache[dest_hex] = (identity, now)
                # Lazy prune when cache grows large
                if len(self._identity_cache) > 500:
                    cutoff = now - self._IDENTITY_CACHE_TTL
                    self._identity_cache = {
                        k: v for k, v in self._identity_cache.items()
                        if v[1] > cutoff
                    }
        return identity


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
    max_message_bytes = 233

    def __init__(self, hub: "MessagingHubPlugin") -> None:
        super().__init__()
        self._hub = hub
        # Track outbound messages awaiting ack:
        # {msg_id: {"timestamp": float}}
        self._pending_delivery: dict[int, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()

    def track_pending(self, msg_id: int, ack_tracking: str | None) -> None:
        """Register an outbound message for ack tracking."""
        if not ack_tracking:
            return
        with self._pending_lock:
            self._pending_delivery[msg_id] = {
                "timestamp": time.time(),
            }

    def get_pending_delivery(self) -> dict[str, dict[str, Any]]:
        """Return pending entries keyed by str(msg_id) for timeout scanning."""
        with self._pending_lock:
            return {str(k): v for k, v in self._pending_delivery.items()}

    def start(self) -> None:
        self._hub.event_bus.subscribe_offloaded(
            events.MESHTASTIC_MESSAGE_RECEIVED, self._on_mesh_event
        )
        self._hub.event_bus.subscribe_offloaded(
            events.MESHTASTIC_REACTION_RECEIVED, self._on_reaction_event
        )

    def stop(self) -> None:
        self._hub.event_bus.unsubscribe(
            events.MESHTASTIC_MESSAGE_RECEIVED, self._on_mesh_event
        )
        self._hub.event_bus.unsubscribe(
            events.MESHTASTIC_REACTION_RECEIVED, self._on_reaction_event
        )

    def _on_reaction_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Event bus callback for Meshtastic emoji reactions."""
        packet_id = data.get("reply_to_packet_id")
        if not packet_id:
            return
        emoji = data.get("emoji", "")
        from_id = data.get("from_id", "")
        from_name = data.get("from_name") or self._resolve_node_name(from_id) or from_id
        source = (data.get("source") or "").lower()
        if source not in ("lora", "mqtt"):
            source = "lora"
        self._hub.handle_reaction(
            packet_id=packet_id,
            emoji=emoji,
            from_id=from_id,
            from_name=from_name,
            sub_transport=source,
        )

    def _on_mesh_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Event bus callback for incoming Meshtastic messages."""
        if not self._hub_callback:
            return
        from_id = data.get("from_id", "")
        # Prefer the name resolved by the gateway (checks MQTT, serial,
        # and persistent cache).  Fall back to our own lookup.
        from_name = (
            data.get("from_name")
            or self._resolve_node_name(from_id)
            or from_id
        )
        # Normalize the gateway's "LoRa"/"MQTT" source tag to lowercase
        # sub_transport values; fall back to "lora" for legacy events
        # that predate the source field.
        source = (data.get("source") or "").lower()
        if source not in ("lora", "mqtt"):
            source = "lora"
        is_broadcast = data.get("is_broadcast", True)

        # Meshtastic channel identity.  Serial/LoRa gives us a local
        # integer index (0..7).  MQTT gives us a channel-name string
        # resolved from the ServiceEnvelope — falls back to the name
        # itself when we have no matching local channel (e.g. a peer
        # broadcasting on a channel we don't have configured).  DMs
        # ignore channel.
        raw_ch = data.get("channel")
        channel: int | str | None
        if raw_ch is None:
            channel = None
        elif isinstance(raw_ch, bool):
            # bool is a subclass of int — reject it explicitly.
            channel = None
        elif isinstance(raw_ch, int):
            channel = raw_ch
        elif isinstance(raw_ch, str):
            channel = raw_ch
        else:
            try:
                channel = int(raw_ch)
            except (TypeError, ValueError):
                channel = None

        # Tag every Meshtastic message (broadcast AND direct) with
        # sub_transport so the dashboard can show MQTT and LoRa traffic
        # in separate panels.
        self._hub_callback({
            "transport": "meshtastic",
            "sub_transport": source,
            "from_id": from_id,
            "from_name": from_name,
            "to_id": None if is_broadcast else data.get("to_id", ""),
            "to_name": None,
            "text": data.get("text", ""),
            "msg_type": "broadcast" if is_broadcast else "direct",
            "channel": channel if is_broadcast else None,
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
        via = kwargs.get("sub_transport", "")
        # DMs prefer LoRa for direct delivery to local neighbors;
        # gateway falls back to MQTT if serial listener is unavailable.
        if dest_id and not via:
            via = "lora"

        # Create an ack callback that will be passed to the gateway.
        # The actual msg_id gets bound after the hub stores the message,
        # via the track_pending() method. Use an Event to block until
        # binding completes, rather than polling — this eliminates a
        # previous race where a very fast ACK could arrive before the
        # hub finished storing and would be silently dropped after 500ms.
        ack_holder: dict[str, Any] = {"bound": threading.Event()}

        def _on_ack(acked: bool) -> None:
            # Wait for _register_delivery_tracking to set msg_id. If the
            # bind never arrives within 5s something is broken upstream,
            # so log and give up instead of hanging the ACK thread.
            if ack_holder.get("msg_id") is None:
                if not ack_holder["bound"].wait(timeout=5.0):
                    self._hub.log.warning(
                        "Meshtastic ACK arrived but msg_id never bound "
                        "(acked=%s) — hub tracking state is inconsistent",
                        acked,
                    )
                    return
            msg_id = ack_holder.get("msg_id")
            if msg_id is None:
                return
            with self._pending_lock:
                self._pending_delivery.pop(msg_id, None)
            status = "delivered" if acked else "delivery_failed"
            self._hub.log.info(
                "Meshtastic message ack: %s (msg_id=%d)", status, msg_id,
            )
            self._hub._on_delivery_status_update(msg_id, "meshtastic", status)

        # The hub passes `channel` as the thread's channel identity,
        # which is normally an int radio-slot index but may be a string
        # channel name when the thread came from an MQTT peer broadcasting
        # on a channel we don't have locally.  The radio call below needs
        # an int; resolve strings against local channels and refuse if we
        # don't have a matching slot (we can't transmit on a channel the
        # radio isn't configured for).
        channel = kwargs.get("channel")
        tx_channel: int | None = None
        if isinstance(channel, bool):
            pass  # reject bool (subclass of int) explicitly
        elif isinstance(channel, int):
            tx_channel = channel
        elif isinstance(channel, str):
            try:
                for ch in gw.get_channels() or []:
                    if ch.get("active") and ch.get("name") == channel:
                        tx_channel = int(ch["index"])
                        break
            except Exception:
                tx_channel = None
            if tx_channel is None:
                return {
                    "sent": False,
                    "reason": (
                        f"Channel '{channel}' is not configured on the local "
                        "radio, so this node can't transmit on it."
                    ),
                }
        result = gw.send_message(
            text, destination_id=dest_id, channel=tx_channel, via=via,
            on_ack=_on_ack,
        )
        # Stash the holder ref so the hub's send_message can bind msg_id
        if result.get("sent"):
            result["_ack_holder"] = ack_holder
        return result

    def get_contacts(self) -> list[dict[str, Any]]:
        gw = self._hub.app.get_plugin("meshtastic_gateway")
        if not gw or not hasattr(gw, "get_meshtastic_nodes"):
            return []
        return [
            {
                "id": n["id"],
                "name": n.get("long_name") or n.get("short_name") or n["id"],
                "transport": "meshtastic",
                "last_heard": n.get("last_heard"),
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
# MeshCore Transport Adapter
# ═══════════════════════════════════════════════════════════════════


class MeshCoreAdapter(TransportAdapter):
    """MeshCore adapter — bridges to the meshcore_gateway plugin.

    Does NOT own the MeshCore connection.  Delegates sending to the
    gateway's ``send_message()`` method and subscribes to its events
    for inbound messages.
    """

    transport_name = "meshcore"
    display_name = "MeshCore"

    def __init__(self, hub: "MessagingHubPlugin") -> None:
        super().__init__()
        self._hub = hub
        # Track outbound direct messages awaiting ACK:
        # {ack_code: {"msg_id": int, "timestamp": float}}
        self._pending_delivery: dict[str, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()

    def track_pending(self, msg_id: int, expected_ack: str | None) -> None:
        """Register an outbound message for ACK tracking."""
        if not expected_ack:
            return
        with self._pending_lock:
            self._pending_delivery[expected_ack] = {
                "msg_id": msg_id,
                "timestamp": time.time(),
            }

    def get_pending_delivery(self) -> dict[str, dict[str, Any]]:
        """Return a copy of the pending delivery tracker."""
        with self._pending_lock:
            return dict(self._pending_delivery)

    def start(self) -> None:
        self._hub.event_bus.subscribe_offloaded(
            events.MESHCORE_MESSAGE_RECEIVED, self._on_meshcore_event
        )
        self._hub.event_bus.subscribe(
            events.MESHCORE_MESSAGE_ACKED, self._on_meshcore_ack
        )

    def stop(self) -> None:
        self._hub.event_bus.unsubscribe(
            events.MESHCORE_MESSAGE_RECEIVED, self._on_meshcore_event
        )
        self._hub.event_bus.unsubscribe(
            events.MESHCORE_MESSAGE_ACKED, self._on_meshcore_ack
        )

    def _on_meshcore_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Event bus callback for incoming MeshCore messages."""
        if not self._hub_callback:
            return
        from_key = data.get("from_key", "")
        from_name = (
            data.get("from_name")
            or self._resolve_contact_name(from_key)
            or from_key[:12]
        )
        msg_type = data.get("msg_type", "direct")

        channel = data.get("channel")
        try:
            channel = int(channel) if channel is not None else None
        except (TypeError, ValueError):
            channel = None

        self._hub_callback({
            "transport": "meshcore",
            "sub_transport": "",
            "from_id": from_key,
            "from_name": from_name,
            "to_id": None,
            "to_name": None,
            "text": data.get("text", ""),
            "msg_type": msg_type,
            "channel": channel,
            "metadata": {k: v for k, v in data.items() if k not in ("text",)},
        })

    def _on_meshcore_ack(self, event_type: str, data: dict[str, Any]) -> None:
        """Event bus callback for MeshCore ACK events."""
        ack_code = data.get("ack_code", "")
        if not ack_code:
            return
        with self._pending_lock:
            entry = self._pending_delivery.pop(ack_code, None)
        if not entry:
            return
        self._hub.log.info(
            "MeshCore message ACK received: %s (msg_id=%d)",
            ack_code, entry["msg_id"],
        )
        self._hub._on_delivery_status_update(
            entry["msg_id"], "meshcore", "delivered",
        )

    def _resolve_contact_name(self, public_key: str) -> str | None:
        """Look up a MeshCore contact name from the gateway."""
        try:
            gw = self._hub.app.get_plugin("meshcore_gateway")
            if not gw or not hasattr(gw, "get_meshcore_nodes"):
                return None
            for n in gw.get_meshcore_nodes():
                if n.get("id") == public_key:
                    return n.get("name") or None
        except Exception:
            pass
        return None

    def send(self, text: str, destination: str, **kwargs: Any) -> dict[str, Any]:
        gw = self._hub.app.get_plugin("meshcore_gateway")
        if not gw or not hasattr(gw, "send_message"):
            return {"sent": False, "reason": "meshcore_gateway plugin not available"}
        dest = destination if destination and destination != "broadcast" else None
        channel = kwargs.get("channel")
        result = gw.send_message(text, destination=dest, channel=channel)
        # Pass expected_ack through so the hub can register tracking
        if result.get("sent") and result.get("expected_ack"):
            result["expected_ack"] = result["expected_ack"]
        return result

    def get_contacts(self) -> list[dict[str, Any]]:
        gw = self._hub.app.get_plugin("meshcore_gateway")
        if not gw or not hasattr(gw, "get_meshcore_nodes"):
            return []
        return [
            {
                "id": n["id"],
                "name": n.get("name") or n["id"][:12],
                "transport": "meshcore",
                "last_heard": n.get("last_heard"),
            }
            for n in gw.get_meshcore_nodes()
        ]

    def is_available(self) -> bool:
        gw = self._hub.app.get_plugin("meshcore_gateway")
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
    plugin_version = "1.1.0"
    plugin_description = "Unified message store and chat hub for LXMF, Meshtastic, and MeshCore"

    def validate_config(self) -> None:
        limit = self.config.get("message_history_limit", _DEFAULT_HISTORY_LIMIT)
        if not isinstance(limit, int) or limit < 0:
            raise ValueError("message_history_limit must be a non-negative integer")

    def start(self) -> None:
        self._lock = threading.Lock()
        self._adapters: dict[str, TransportAdapter] = {}

        # Outbound retry queue. When a transport is disconnected, outbound
        # sends land here with status=queued in the store; we drain on the
        # transport's CONNECTED event and on a periodic sweep.
        self._outbound_lock = threading.Lock()
        self._outbound_queues: dict[str, deque[dict[str, Any]]] = {}
        self._outbound_max_per_transport = int(
            self.config.get("outbound_queue_max", 50)
        )
        self._outbound_max_age_s = float(
            self.config.get("outbound_queue_ttl_seconds", 600.0)
        )

        # Initialize SQLite store
        db_path = os.path.expanduser(
            self.config.get("db_path", _DEFAULT_DB_PATH)
        )
        self._store = MessageStore(db_path)
        self._history_limit = self.config.get(
            "message_history_limit", _DEFAULT_HISTORY_LIMIT
        )
        self._last_prune_ts = 0.0

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

        # Register MeshCore adapter (bridges to meshcore_gateway plugin)
        mc_cfg = self.config.get("meshcore", {})
        if mc_cfg.get("enabled", True):
            mc_adapter = MeshCoreAdapter(self)
            self.register_adapter(mc_adapter)

        # Drain queued sends as soon as a transport comes back online.
        self.event_bus.subscribe(
            events.MESHTASTIC_CONNECTED, self._on_transport_connected
        )
        self.event_bus.subscribe(
            events.MESHCORE_CONNECTED, self._on_transport_connected
        )

        self._active = True
        self._delivery_timeout = float(
            self.config.get("delivery_timeout", 300)
        )
        self._start_thread(self._delivery_timeout_loop, name="msg-delivery-timeout")
        self.log.info(
            "Messaging hub started with %d transport(s): %s",
            len(self._adapters),
            ", ".join(self._adapters.keys()),
        )

    def stop(self) -> None:
        self._active = False
        try:
            self.event_bus.unsubscribe_all(self._on_transport_connected)
        except Exception:
            self.log.debug("Error unsubscribing connected handler", exc_info=True)
        for adapter in list(self._adapters.values()):
            try:
                adapter.stop()
            except Exception:
                self.log.exception(
                    "Error stopping adapter %s", adapter.transport_name
                )
        self._adapters.clear()
        # Don't close the store here: plugins stop in reverse order, so
        # messaging_hub stops before web_dashboard (the HTTP server).
        # Between our stop() and web_dashboard's, in-flight requests can
        # still call get_unread_counts() etc. The connection is released
        # when Python GCs the store at process exit moments later.
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
            msg_type = msg.get("msg_type", "direct")
            sub_transport = msg.get("sub_transport", "")
            msg_id = self._store.store(
                transport=msg["transport"],
                direction="received",
                msg_type=msg_type,
                text=msg["text"],
                from_id=msg.get("from_id"),
                from_name=msg.get("from_name"),
                to_id=msg.get("to_id"),
                to_name=msg.get("to_name"),
                status="received",
                metadata=msg.get("metadata"),
                sub_transport=sub_transport,
                channel=msg.get("channel"),
            )
            self._maybe_prune()
            # Include contact_id + sub_transport so the WS skeleton-fallback
            # path (when the stored row isn't yet readable) still carries
            # the fields panels use to route events to the right tab.
            contact_id = MessageStore._compute_contact_id(
                "received", msg_type, msg["transport"],
                msg.get("from_id"), msg.get("to_id"),
                sub_transport=sub_transport,
                channel=msg.get("channel"),
            )
            self.event_bus.publish(events.MESSAGE_RECEIVED, {
                "id": msg_id,
                "transport": msg["transport"],
                "sub_transport": sub_transport,
                "contact_id": contact_id,
                "direction": "received",
                "status": "received",
                "from_id": msg.get("from_id"),
                "from_name": msg.get("from_name"),
                "text": msg["text"],
                "msg_type": msg_type,
                "timestamp": time.time(),
            })
        except Exception:
            self.log.exception("Error storing inbound message")

    # ── Reactions ──────────────────────────────────────────────────

    def handle_reaction(
        self,
        packet_id: int,
        emoji: str,
        from_id: str,
        from_name: str,
        sub_transport: str = "",
    ) -> None:
        """Store an emoji reaction and push it to connected clients."""
        try:
            msg_id = self._store.add_reaction(
                packet_id, emoji, from_id, from_name,
            )
            if msg_id is None:
                self.log.debug(
                    "Reaction from %s (target packet %d) — no matching message",
                    from_id, packet_id,
                )
                return
            row = self._store.get_message(msg_id)
            self.event_bus.publish(events.MESSAGE_REACTION_RECEIVED, {
                "id": msg_id,
                "transport": row.get("transport", "meshtastic") if row else "meshtastic",
                "sub_transport": row.get("sub_transport", sub_transport) if row else sub_transport,
                "contact_id": row.get("contact_id") if row else None,
                "emoji": emoji,
                "from_id": from_id,
                "from_name": from_name,
                "reactions": (row.get("metadata") or {}).get("reactions", [])
                    if row else [],
            })
        except Exception:
            self.log.exception("Error storing reaction")

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
        ``{"sent": False, "reason": str}`` on failure. If the transport is
        registered but currently unavailable, the send is queued for retry
        (up to ``outbound_queue_ttl_seconds``) and the result includes
        ``"queued": True``.
        """
        with self._lock:
            adapter = self._adapters.get(transport)
        if not adapter:
            return {"sent": False, "reason": f"Transport '{transport}' not registered"}

        # If the transport is down, queue for retry rather than failing.
        if not adapter.is_available():
            return self._queue_outbound(transport, text, destination, kwargs)

        result = adapter.send(text, destination, **kwargs)
        # Some adapters (Meshtastic, MeshCore) can detect "not_connected" at
        # send time even though is_available() returned True — queue those too.
        if (
            not result.get("sent")
            and result.get("reason", "") in {"not_connected", "not connected"}
        ):
            return self._queue_outbound(transport, text, destination, kwargs)

        return self._finalize_send(
            adapter, transport, text, destination, kwargs, result,
        )

    def _finalize_send(
        self,
        adapter: TransportAdapter,
        transport: str,
        text: str,
        destination: str,
        kwargs: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        # Resolve destination name from contacts if possible
        to_name = kwargs.get("to_name")
        if not to_name:
            for c in adapter.get_contacts():
                if c.get("id") == destination:
                    to_name = c.get("name")
                    break
        if not to_name and transport == "lxmf":
            nm = self.app.get_plugin("network_map")
            if nm and hasattr(nm, "get_node_name"):
                try:
                    to_name = nm.get_node_name(destination)
                except Exception:
                    to_name = None

        # Broadcast without an explicit channel lands on 0 (Primary),
        # matching the gateway's radio-side default, so the stored
        # thread aligns with the channel the packet was actually sent on.
        channel = kwargs.get("channel")
        if channel is None and kwargs.get("msg_type") == "broadcast":
            channel = 0
        msg_type = kwargs.get("msg_type", "direct")
        sub_transport = kwargs.get("sub_transport", "")
        meta = dict(kwargs.get("metadata") or {})
        if result.get("packet_id") is not None:
            meta["packet_id"] = result["packet_id"]
        msg_id = self._store.store(
            transport=transport,
            direction="sent",
            msg_type=msg_type,
            text=text,
            from_id="self",
            from_name=self.app.node_name,
            to_id=destination,
            to_name=to_name,
            status="sent" if result.get("sent") else "failed",
            metadata=meta or None,
            sub_transport=sub_transport,
            channel=channel,
        )
        self._maybe_prune()

        if result.get("sent"):
            contact_id = MessageStore._compute_contact_id(
                "sent", msg_type, transport,
                "self", destination,
                sub_transport=sub_transport,
                channel=channel,
            )
            self.event_bus.publish(events.MESSAGE_SENT, {
                "id": msg_id,
                "transport": transport,
                "sub_transport": sub_transport,
                "contact_id": contact_id,
                "direction": "sent",
                "status": "sent",
                "destination": destination,
                "text": text,
                "msg_type": msg_type,
                "timestamp": time.time(),
            })
            # Register for delivery tracking if the adapter supports it
            self._register_delivery_tracking(adapter, msg_id, result)

        return {**result, "msg_id": msg_id}

    # ── Outbound retry queue ───────────────────────────────────────

    def _queue_outbound(
        self,
        transport: str,
        text: str,
        destination: str,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a send that couldn't go out, to be retried on reconnect."""
        to_name = kwargs.get("to_name")
        channel = kwargs.get("channel")
        if channel is None and kwargs.get("msg_type") == "broadcast":
            channel = 0
        msg_type = kwargs.get("msg_type", "direct")
        sub_transport = kwargs.get("sub_transport", "")
        msg_id = self._store.store(
            transport=transport,
            direction="sent",
            msg_type=msg_type,
            text=text,
            from_id="self",
            from_name=self.app.node_name,
            to_id=destination,
            to_name=to_name,
            status="queued",
            metadata=kwargs.get("metadata"),
            sub_transport=sub_transport,
            channel=channel,
        )
        entry = {
            "msg_id": msg_id,
            "text": text,
            "destination": destination,
            "kwargs": dict(kwargs),
            "queued_at": time.time(),
            "attempts": 0,
        }
        evicted: dict[str, Any] | None = None
        with self._outbound_lock:
            q = self._outbound_queues.setdefault(transport, deque())
            if len(q) >= self._outbound_max_per_transport:
                evicted = q.popleft()
            q.append(entry)
        if evicted is not None:
            self.log.warning(
                "Outbound queue for %s full; evicting oldest msg_id=%d",
                transport, evicted["msg_id"],
            )
            self._on_delivery_status_update(
                evicted["msg_id"], transport, "failed",
            )
        self.log.info(
            "Queued outbound %s msg_id=%d for retry (dest=%s)",
            transport, msg_id, destination,
        )
        # Reuse MESSAGE_SENT so the dashboard picks up the queued row and
        # renders a bubble with status="queued" right away instead of
        # waiting until the queue drains. The only subscriber is the WS
        # handler, which enriches via `_lookup_message_row` and reads the
        # status off the stored row — no consumers misinterpret this.
        contact_id = MessageStore._compute_contact_id(
            "sent", msg_type, transport,
            "self", destination,
            sub_transport=sub_transport,
            channel=channel,
        )
        self.event_bus.publish(events.MESSAGE_SENT, {
            "id": msg_id,
            "transport": transport,
            "sub_transport": sub_transport,
            "contact_id": contact_id,
            "direction": "sent",
            "status": "queued",
            "destination": destination,
            "text": text,
            "msg_type": msg_type,
            "timestamp": time.time(),
        })
        return {
            "sent": False,
            "queued": True,
            "msg_id": msg_id,
            "reason": f"Transport '{transport}' not available — queued for retry",
        }

    def _on_transport_connected(self, event_type: str, data: dict[str, Any]) -> None:
        """Event-bus callback: drain the queue when a transport reconnects."""
        # Event names are "<transport>.connected"
        transport = event_type.split(".")[0]
        try:
            drained, requeued, expired = self._drain_outbound_queue(transport)
            if drained or requeued or expired:
                self.log.info(
                    "Outbound queue drain for %s: sent=%d requeued=%d expired=%d",
                    transport, drained, requeued, expired,
                )
        except Exception:
            self.log.exception(
                "Error draining outbound queue for %s", transport,
            )

    def _drain_outbound_queue(
        self, transport: str,
    ) -> tuple[int, int, int]:
        """Retry queued sends for *transport*. Returns (sent, requeued, expired)."""
        with self._outbound_lock:
            q = self._outbound_queues.get(transport)
            if not q:
                return 0, 0, 0
            pending = list(q)
            q.clear()

        with self._lock:
            adapter = self._adapters.get(transport)
        if not adapter:
            # Transport vanished; expire everything.
            for item in pending:
                self._on_delivery_status_update(
                    item["msg_id"], transport, "failed",
                )
            return 0, 0, len(pending)

        now = time.time()
        sent = requeued = expired = 0
        for idx, item in enumerate(pending):
            age = now - item["queued_at"]
            if age > self._outbound_max_age_s:
                self._on_delivery_status_update(
                    item["msg_id"], transport, "expired",
                )
                expired += 1
                continue
            if not adapter.is_available():
                # Transport flapped back down. Requeue THIS item and all
                # remaining ones in order, then stop — no point checking
                # availability again for each item in the same drain.
                with self._outbound_lock:
                    dest_q = self._outbound_queues.setdefault(
                        transport, deque(),
                    )
                    for remaining in pending[idx:]:
                        dest_q.append(remaining)
                        requeued += 1
                break
            item["attempts"] += 1
            try:
                result = adapter.send(
                    item["text"], item["destination"], **item["kwargs"],
                )
            except Exception:
                self.log.exception(
                    "Error retrying queued send msg_id=%d", item["msg_id"],
                )
                result = {"sent": False, "reason": "exception during retry"}
            if result.get("sent"):
                # Route the queued→sent transition through the same helper
                # used by "failed"/"expired" in this loop, so the dashboard
                # receives MESSAGE_STATUS_CHANGED and flips the existing
                # bubble in place. The MESSAGE_SENT publish below is kept
                # for any pure-transmit subscribers; the WS client
                # deduplicates by id so it arrives as a no-op there.
                self._on_delivery_status_update(
                    item["msg_id"], transport, "sent",
                )
                drain_kwargs = item.get("kwargs") or {}
                drain_msg_type = drain_kwargs.get("msg_type", "direct")
                drain_sub = drain_kwargs.get("sub_transport", "")
                drain_channel = drain_kwargs.get("channel")
                if drain_channel is None and drain_msg_type == "broadcast":
                    drain_channel = 0
                drain_contact_id = MessageStore._compute_contact_id(
                    "sent", drain_msg_type, transport,
                    "self", item["destination"],
                    sub_transport=drain_sub,
                    channel=drain_channel,
                )
                self.event_bus.publish(events.MESSAGE_SENT, {
                    "id": item["msg_id"],
                    "transport": transport,
                    "sub_transport": drain_sub,
                    "contact_id": drain_contact_id,
                    "direction": "sent",
                    "status": "sent",
                    "destination": item["destination"],
                    "text": item["text"],
                    "msg_type": drain_msg_type,
                    "timestamp": time.time(),
                })
                self._register_delivery_tracking(adapter, item["msg_id"], result)
                sent += 1
            elif result.get("reason", "") in {"not_connected", "not connected"}:
                with self._outbound_lock:
                    self._outbound_queues.setdefault(transport, deque()).append(item)
                requeued += 1
            else:
                self._on_delivery_status_update(
                    item["msg_id"], transport, "failed",
                )
                expired += 1
        if sent or expired:
            self._maybe_prune()
        return sent, requeued, expired

    def expire_queued_outbound(self) -> int:
        """Drop queued sends older than the TTL. Returns count expired."""
        now = time.time()
        expired: list[tuple[str, dict[str, Any]]] = []
        with self._outbound_lock:
            for transport, q in self._outbound_queues.items():
                kept: deque = deque()
                for item in q:
                    if now - item["queued_at"] > self._outbound_max_age_s:
                        expired.append((transport, item))
                    else:
                        kept.append(item)
                self._outbound_queues[transport] = kept
        for transport, item in expired:
            self._on_delivery_status_update(
                item["msg_id"], transport, "expired",
            )
        return len(expired)

    def get_queued_outbound(self) -> dict[str, int]:
        """Return a snapshot of queued outbound counts per transport."""
        with self._outbound_lock:
            return {t: len(q) for t, q in self._outbound_queues.items() if q}

    # ── Delivery tracking ─────────────────────────────────────────

    def _register_delivery_tracking(
        self, adapter: TransportAdapter, msg_id: int, result: dict[str, Any],
    ) -> None:
        """Register an outbound message for delivery tracking with its adapter."""
        if not hasattr(adapter, "track_pending"):
            return

        if isinstance(adapter, LXMFAdapter):
            adapter.track_pending(msg_id, result.get("lxm_hash"))
        elif isinstance(adapter, MeshtasticAdapter):
            # Order matters: register with adapter BEFORE releasing the
            # ACK waiter. If the ACK arrives before track_pending runs,
            # its pop() is a no-op and the entry we add next goes stale,
            # eventually getting overwritten with "timeout" 300s later.
            ack_holder = result.pop("_ack_holder", None)
            if ack_holder is not None:
                ack_holder["msg_id"] = msg_id
            adapter.track_pending(msg_id, result.get("ack_tracking"))
            if ack_holder is not None:
                bound = ack_holder.get("bound")
                if bound is not None:
                    bound.set()
        elif isinstance(adapter, MeshCoreAdapter):
            adapter.track_pending(msg_id, result.get("expected_ack"))

    # Terminal statuses are final — once set, later updates from slower
    # sources (e.g. 300s stale-pending sweeper) must not overwrite them.
    _TERMINAL_STATUSES = frozenset({"delivered", "delivery_failed", "failed"})
    _NON_OVERWRITING_STATUSES = frozenset({"timeout", "expired"})

    def _on_delivery_status_update(
        self, msg_id: int, transport: str, status: str,
    ) -> None:
        """Called by adapter callbacks when a delivery status changes."""
        try:
            if status in self._NON_OVERWRITING_STATUSES:
                current = self._store.get_status(msg_id)
                if current in self._TERMINAL_STATUSES:
                    self.log.debug(
                        "Ignoring %s for msg %d — already %s",
                        status, msg_id, current,
                    )
                    return
            self._store.update_status(msg_id, status)
            # Enrich with contact_id / sub_transport so the WS handler can
            # route the status update to the correct panel even when its
            # live get_message() lookup races with shutdown/reload and
            # returns None.
            row = self._store.get_message(msg_id)
            payload = {
                "id": msg_id,
                "transport": transport,
                "status": status,
                "timestamp": time.time(),
            }
            if row:
                payload["contact_id"] = row.get("contact_id")
                payload["sub_transport"] = row.get("sub_transport", "") or ""
            self.event_bus.publish(events.MESSAGE_STATUS_CHANGED, payload)
        except Exception:
            self.log.exception("Error updating delivery status for msg %d", msg_id)

    def expire_stale_pending(self, max_age: float = 300.0) -> int:
        """Mark pending messages older than *max_age* seconds as timed out.

        Returns the number of messages expired.
        """
        now = time.time()
        expired = 0
        with self._lock:
            adapters = list(self._adapters.values())
        for adapter in adapters:
            if not hasattr(adapter, "get_pending_delivery"):
                continue
            pending = adapter.get_pending_delivery()
            for key, entry in pending.items():
                age = now - entry.get("timestamp", now)
                if age <= max_age:
                    continue
                msg_id = entry.get("msg_id")
                if msg_id is None:
                    # Meshtastic uses msg_id as the key itself
                    try:
                        msg_id = int(key)
                    except (ValueError, TypeError):
                        continue
                # Remove from adapter's pending tracker
                if hasattr(adapter, "_pending_lock"):
                    with adapter._pending_lock:
                        if isinstance(adapter, MeshtasticAdapter):
                            adapter._pending_delivery.pop(msg_id, None)
                        else:
                            adapter._pending_delivery.pop(key, None)
                self._on_delivery_status_update(
                    msg_id, adapter.transport_name, "timeout",
                )
                expired += 1
        if expired:
            self.log.debug("Expired %d stale pending delivery entries", expired)
        return expired

    def _delivery_timeout_loop(self) -> None:
        """Periodically expire stale pending delivery entries + queued sends."""
        while self._active:
            try:
                self.expire_stale_pending(self._delivery_timeout)
            except Exception:
                self.log.exception("Error in delivery timeout loop")
            try:
                self.expire_queued_outbound()
            except Exception:
                self.log.exception("Error expiring queued outbound messages")
            # Check every 60 seconds
            for _ in range(60):
                if not self._active:
                    return
                time.sleep(1)

    # ── Queries (used by dashboard API) ────────────────────────────

    def get_messages(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Retrieve messages from the store with optional filters."""
        return self._store.get_messages(**kwargs)

    def get_message(self, msg_id: int) -> dict[str, Any] | None:
        """Retrieve a single message row by ID."""
        return self._store.get_message(msg_id)

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
            if a.max_message_bytes is not None:
                info["max_message_bytes"] = a.max_message_bytes
            if hasattr(a, "address") and a.address:
                info["address"] = a.address
            result.append(info)
        return result

    def get_contacts(
        self,
        transport: str | None = None,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        """Aggregate contacts from all or a specific transport.

        Args:
            transport: Optional filter by transport name.
            query: Optional search string — filters by name or id
                   (case-insensitive substring match).

        Returns contacts sorted by ``last_heard`` descending (most recent
        first).  Contacts without a ``last_heard`` value sort last.
        """
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

        # Optional text search
        if query:
            q = query.lower()
            contacts = [
                c for c in contacts
                if q in (c.get("name") or "").lower()
                or q in (c.get("id") or "").lower()
            ]

        # Sort by last_heard descending (None sorts last)
        contacts.sort(
            key=lambda c: (c.get("last_heard") or 0),
            reverse=True,
        )
        return contacts

    def get_stats(self) -> dict[str, Any]:
        """Return message statistics."""
        return self._store.get_stats()

    def get_conversations(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return conversation summaries from the store."""
        return self._store.get_conversations(**kwargs)

    def get_conversation_messages(
        self, contact_id: str, **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Fetch messages for a single conversation."""
        return self._store.get_conversation_messages(contact_id, **kwargs)

    def search_messages(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Search messages by text content."""
        return self._store.search_messages(query, **kwargs)

    def mark_read(self, contact_id: str) -> int:
        """Mark all unread messages from a contact as read."""
        return self._store.mark_read(contact_id)

    def delete_conversation(self, contact_id: str) -> int:
        """Delete all stored messages for a conversation."""
        return self._store.delete_conversation(contact_id)

    def get_unread_counts(self, **kwargs: Any) -> dict[str, int]:
        """Return unread counts per contact."""
        return self._store.get_unread_counts(**kwargs)

    def get_unread_counts_grouped(self) -> dict[str, dict[str, int]]:
        """Return per-bucket unread counts keyed by transport[:sub_transport]."""
        return self._store.get_unread_counts_grouped()

    def get_peer_sub_transports(self, transport: str) -> dict[str, set[str]]:
        """Return ``{peer_id: {sub_transport, ...}}`` for DMs on *transport*."""
        return self._store.get_peer_sub_transports(transport)

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
        """Prune old messages if history limit is set.

        Throttled to run at most once every 30s: prune() does a COUNT + DELETE
        per (transport, sub_transport) bucket and was otherwise firing on every
        single stored message during bursty traffic.
        """
        if self._history_limit <= 0:
            return
        now = time.time()
        if now - self._last_prune_ts < 30.0:
            return
        self._last_prune_ts = now
        try:
            self._store.prune(self._history_limit)
        except Exception:
            self.log.debug("Error pruning messages", exc_info=True)
