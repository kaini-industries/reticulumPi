"""Bidirectional message bridge between Meshtastic and MeshCore networks.

Subscribes to ``MESHTASTIC_MESSAGE_RECEIVED`` and ``MESHCORE_MESSAGE_RECEIVED``
events and relays broadcasts (and optionally DMs) to the opposite mesh via
``messaging_hub.send_message()``.  Bridged messages are prefixed with a
short origin tag (``[via Mesh]``/``[via Core]``) so recipients know where
the message came from, and that same prefix is used for loop detection.

Runtime pause/resume is supported — the operator can stop all relaying via
the dashboard toggle, the ``reticulumpi mesh-bridge pause`` CLI command, or
``POST /api/mesh_bridge/running``.  The runtime state persists across
restarts.  A traffic-rate circuit breaker auto-pauses if relays exceed a
configurable threshold.

Requires: ``meshtastic_gateway`` AND ``meshcore_gateway`` plugins enabled.
Strongly recommended: ``messaging_hub`` enabled.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import deque
from typing import Any

from reticulumpi import events
from reticulumpi.mtu import MESHCORE_MTU, MESHTASTIC_MTU, truncate_for_mtu
from reticulumpi.plugin_base import PluginBase

_DEFAULT_DEDUP_TTL = 60.0
_DEFAULT_DEDUP_MAX = 256
_DEFAULT_LOOP_REGEX = r"^\[via (Mesh|Core)\]\s"
_DEFAULT_AUTO_PAUSE_THRESHOLD = 20
_CIRCUIT_BREAKER_WINDOW = 60.0
_DEFAULT_STARTUP_GRACE = 30.0
_STATE_FILENAME = "mesh_bridge_state.json"

# Meshtastic/MeshCore auto-generated position-share alerts have a consistent
# English phrase in the text body.  Matched case-insensitively.
_POSITION_SHARE_RE = re.compile(
    r"has shared their (position|location)", re.IGNORECASE,
)

# Tapback heuristic: text is short (≤6 chars after stripping) AND contains
# no alphanumeric characters — i.e. it's emoji, punctuation, or symbols
# only.  Catches 👍, ❤️, !!, 😂 etc. without false-positiving "ok", "hi".
_TAPBACK_MAX_LEN = 6


def _looks_like_tapback(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > _TAPBACK_MAX_LEN:
        return False
    return not any(c.isalnum() for c in stripped)


def _select_sender_label(from_name: str | None, from_id: str | None) -> str:
    """Prefer human name; fall back to short id; final fallback is '?'."""
    if from_name:
        return from_name
    if from_id:
        return from_id[:12] if len(from_id) > 12 else from_id
    return "?"


def _normalize_mesh_event(data: dict) -> dict:
    """Meshtastic event payload → common bridge shape."""
    return {
        "from_id": data.get("from_id") or "",
        "from_name": data.get("from_name"),
        "text": data.get("text") or "",
        "channel": data.get("channel"),
        "msg_type": "broadcast" if data.get("is_broadcast") else "direct",
        "to_id": data.get("to_id") or "",
    }


def _normalize_core_event(data: dict) -> dict:
    """MeshCore event payload → common bridge shape."""
    return {
        "from_id": data.get("from_key") or "",
        "from_name": data.get("from_name"),
        "text": data.get("text") or "",
        "channel": data.get("channel"),
        "msg_type": data.get("msg_type") or "direct",
        "to_id": "",
    }


class MeshBridge(PluginBase):
    """Relay messages between Meshtastic and MeshCore meshes."""

    plugin_name = "mesh_bridge"
    plugin_version = "1.0.0"
    plugin_description = (
        "Relays broadcasts (and optionally DMs) between Meshtastic and MeshCore."
    )
    broadcast_tier = 1
    broadcast_keys = "mesh_bridge"
    plugin_dependencies = ("meshtastic_gateway", "meshcore_gateway", "messaging_hub")

    def validate_config(self) -> None:
        pairs = self.config.get("channel_pairs", [])
        if not isinstance(pairs, list):
            raise ValueError("channel_pairs must be a list")
        for pair in pairs:
            if not isinstance(pair, dict):
                raise ValueError("each channel_pair must be a dict")
            m_ch = pair.get("meshtastic")
            c_ch = pair.get("meshcore")
            if not isinstance(m_ch, int) or not (0 <= m_ch <= 7):
                raise ValueError(
                    f"channel_pair.meshtastic must be int 0-7, got {m_ch!r}"
                )
            if not isinstance(c_ch, int) or c_ch < 0:
                raise ValueError(
                    f"channel_pair.meshcore must be non-negative int, got {c_ch!r}"
                )
            direction = pair.get("direction", "both")
            if direction not in ("both", "mesh_to_core", "core_to_mesh"):
                raise ValueError(f"invalid direction: {direction!r}")
            for key in ("allow_regex", "deny_regex"):
                pattern = pair.get(key)
                if pattern is not None:
                    try:
                        re.compile(pattern)
                    except re.error as exc:
                        raise ValueError(f"invalid {key}: {exc}") from exc

        dm_pairs = self.config.get("dm_pairs", [])
        if not isinstance(dm_pairs, list):
            raise ValueError("dm_pairs must be a list")
        for dm in dm_pairs:
            if not isinstance(dm, dict):
                raise ValueError("each dm_pair must be a dict")
            m_id = dm.get("meshtastic", "")
            c_id = dm.get("meshcore", "")
            if not (isinstance(m_id, str) and re.match(r"^![0-9a-fA-F]{8}$", m_id)):
                raise ValueError(
                    f"dm_pair.meshtastic must be '!XXXXXXXX', got {m_id!r}"
                )
            if not (isinstance(c_id, str) and re.match(r"^[0-9a-fA-F]{12,}$", c_id)):
                raise ValueError(
                    f"dm_pair.meshcore must be hex prefix >=12 chars, got {c_id!r}"
                )

        loop_regex = self.config.get("loop_detect_regex", _DEFAULT_LOOP_REGEX)
        try:
            re.compile(loop_regex)
        except re.error as exc:
            raise ValueError(f"invalid loop_detect_regex: {exc}") from exc

    def start(self) -> None:
        self._active = True

        # Tags
        self._tag_mesh = self.config.get("tag_mesh", "[via Mesh]")
        self._tag_core = self.config.get("tag_core", "[via Core]")

        # MTUs
        self._mesh_mtu = int(
            self.config.get("meshtastic_mtu", MESHTASTIC_MTU)
        )
        self._core_mtu = int(self.config.get("meshcore_mtu", MESHCORE_MTU))

        # Loop detection + dedup
        self._loop_re = re.compile(
            self.config.get("loop_detect_regex", _DEFAULT_LOOP_REGEX)
        )
        self._dedup_ttl = float(
            self.config.get("dedup_ttl_seconds", _DEFAULT_DEDUP_TTL)
        )
        self._dedup_max = int(
            self.config.get("dedup_max_entries", _DEFAULT_DEDUP_MAX)
        )
        self._dedup_cache: dict[tuple[str, str, int], float] = {}

        # DM config.  Note: only mesh→core DMs can be routed to a specific
        # recipient — the MeshCore gateway event payload does not include a
        # "to_key" field, so core-side DMs have no destination to map from.
        # We still build _dm_core_to_mesh for future use, but
        # `_resolve_dm_target("core", ...)` currently always drops.
        self._bridge_dms = bool(self.config.get("bridge_dms", False))
        self._dm_mesh_to_core: dict[str, str] = {}
        self._dm_core_to_mesh: dict[str, str] = {}
        for dm in self.config.get("dm_pairs", []):
            m_id = dm["meshtastic"]
            c_id = dm["meshcore"]
            self._dm_mesh_to_core[m_id] = c_id
            self._dm_core_to_mesh[c_id.lower()] = m_id

        # Channel pairs
        self._mesh_to_core: dict[int, dict] = {}
        self._core_to_mesh: dict[int, dict] = {}
        default_pairs = [{"meshtastic": 0, "meshcore": 0, "enabled": True}]
        raw_pairs = self.config.get("channel_pairs")
        if raw_pairs is None:
            raw_pairs = default_pairs
        for pair in raw_pairs:
            if not pair.get("enabled", True):
                continue
            direction = pair.get("direction", "both")
            compiled = {
                "meshtastic": pair["meshtastic"],
                "meshcore": pair["meshcore"],
                "direction": direction,
                "allow_regex": re.compile(pair["allow_regex"])
                if pair.get("allow_regex") else None,
                "deny_regex": re.compile(pair["deny_regex"])
                if pair.get("deny_regex") else None,
            }
            if direction in ("both", "mesh_to_core"):
                self._mesh_to_core[pair["meshtastic"]] = compiled
            if direction in ("both", "core_to_mesh"):
                self._core_to_mesh[pair["meshcore"]] = compiled

        # Content filters — block auto-generated chatter that adds noise
        # when relayed across networks.
        self._filter_position_shares = bool(
            self.config.get("filter_position_shares", True)
        )
        self._filter_tapbacks = bool(self.config.get("filter_tapbacks", True))

        # Circuit breaker
        self._auto_pause_threshold = int(
            self.config.get("auto_pause_threshold", _DEFAULT_AUTO_PAUSE_THRESHOLD)
        )
        self._recent_relays: deque[float] = deque(maxlen=10_000)

        # Startup grace — MeshCore's auto_message_fetching drains queued
        # messages from the device on connect, and MQTT may deliver recent
        # broadcasts from before the service restart.  Dropping everything
        # for the first N seconds prevents the bridge from re-broadcasting
        # stale traffic after a restart.
        self._startup_grace = float(
            self.config.get("startup_grace_seconds", _DEFAULT_STARTUP_GRACE)
        )
        self._started_at = time.monotonic()

        # Stats
        self._stats = {
            "msgs_relayed_mesh_to_core": 0,
            "msgs_relayed_core_to_mesh": 0,
            "msgs_dropped_paused": 0,
            "msgs_dropped_loop": 0,
            "msgs_dropped_dedup": 0,
            "msgs_dropped_no_pair": 0,
            "msgs_dropped_filter": 0,
            "msgs_dropped_send_failed": 0,
            "msgs_dropped_empty": 0,
            "msgs_dropped_startup_grace": 0,
            "msgs_dropped_position_share": 0,
            "msgs_dropped_tapback": 0,
        }

        self._lock = threading.Lock()
        self._log_dropped = bool(self.config.get("log_dropped", True))

        # Runtime state (pause/resume persistence)
        self._state_path = self._resolve_state_path()
        self._auto_paused_reason: str | None = None
        self._auto_paused_at: float | None = None
        default_running = bool(self.config.get("default_running", True))
        self._running = self._load_state(default_running)

        # Subscribe (offloaded to avoid blocking gateway callback threads)
        self.event_bus.subscribe_offloaded(
            events.MESHTASTIC_MESSAGE_RECEIVED, self._on_mesh_message,
        )
        self.event_bus.subscribe_offloaded(
            events.MESHCORE_MESSAGE_RECEIVED, self._on_core_message,
        )

        self.log.info(
            "mesh_bridge: %d broadcast pair(s), %d DM pair(s) active, "
            "running=%s, grace=%.0fs",
            len(self._mesh_to_core) + len(self._core_to_mesh) -
            sum(1 for p in self._mesh_to_core.values()
                if p.get("direction") == "both"),
            len(self._dm_mesh_to_core) + len(self._dm_core_to_mesh),
            self._running,
            self._startup_grace,
        )

    def stop(self) -> None:
        self._active = False
        try:
            self.event_bus.unsubscribe(
                events.MESHTASTIC_MESSAGE_RECEIVED, self._on_mesh_message,
            )
            self.event_bus.unsubscribe(
                events.MESHCORE_MESSAGE_RECEIVED, self._on_core_message,
            )
        except Exception:
            self.log.debug("Error unsubscribing from event bus", exc_info=True)

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._active,
                "running": self._running,
                "config_enabled": bool(self.config.get("enabled", False)),
                "auto_paused_reason": self._auto_paused_reason,
                "auto_paused_at": self._auto_paused_at,
                "pairs_broadcast": len(self._mesh_to_core) + len(self._core_to_mesh),
                "pairs_dm": len(self._dm_mesh_to_core) + len(self._dm_core_to_mesh),
                "bridge_dms": self._bridge_dms,
                "stats": dict(self._stats),
            }

    def broadcast_snapshot(self, cycle_count: int = 0) -> dict | None:
        return self.get_status()

    def set_running(
        self, running: bool, reason: str | None = None,
    ) -> dict[str, Any]:
        """Toggle runtime state; persists to disk.

        When ``running`` transitions to True, auto_paused_reason is cleared.
        ``reason`` is only meaningful when pausing (e.g. "rate_limit").
        """
        with self._lock:
            prev = self._running
            self._running = bool(running)
            if running:
                self._auto_paused_reason = None
                self._auto_paused_at = None
                self._recent_relays.clear()
            elif reason:
                self._auto_paused_reason = reason
                self._auto_paused_at = time.time()
            state_snapshot = {
                "running": self._running,
                "auto_paused_reason": self._auto_paused_reason,
                "auto_paused_at": self._auto_paused_at,
            }
        if prev != running:
            self.log.info(
                "mesh_bridge: %s (reason=%s)",
                "resumed" if running else "paused",
                reason or "manual",
            )
            self._persist_state(state_snapshot)
        return self.get_status()

    # ── Inbound handlers ───────────────────────────────────────────

    def _on_mesh_message(self, event_type: str, data: dict) -> None:
        try:
            self._relay("mesh", _normalize_mesh_event(data))
        except Exception:
            self.log.exception("Error relaying Meshtastic message")

    def _on_core_message(self, event_type: str, data: dict) -> None:
        try:
            self._relay("core", _normalize_core_event(data))
        except Exception:
            self.log.exception("Error relaying MeshCore message")

    # ── Core relay ────────────────────────────────────────────────

    def _relay(self, origin: str, msg: dict) -> None:
        # 0a. Startup grace — drop queue-drained / stale messages that
        # arrive in the first N seconds after start.  MeshCore's auto
        # message fetcher empties the device buffer on connect, and MQTT
        # can deliver recent broadcasts from before the restart.
        if self._startup_grace > 0 and (
            time.monotonic() - self._started_at < self._startup_grace
        ):
            self._inc_stat("msgs_dropped_startup_grace")
            return

        # 0b. Paused — hard gate after grace
        if not self._running:
            self._inc_stat("msgs_dropped_paused")
            return

        text = msg.get("text") or ""
        if not text:
            self._inc_stat("msgs_dropped_empty")
            return

        # 1. Loop detection via tag regex
        if self._loop_re.match(text.lstrip()):
            self._inc_stat("msgs_dropped_loop")
            if self._log_dropped:
                self.log.debug(
                    "mesh_bridge: dropped loop-tagged message from %s",
                    msg.get("from_id"),
                )
            return

        # 2. Dedup cache
        from_id_norm = (msg.get("from_id") or "").lower()
        text_hash = hash(text)
        key = (origin, from_id_norm, text_hash)
        now = time.monotonic()
        if self._dedup_hit(key, now):
            self._inc_stat("msgs_dropped_dedup")
            return

        # 3. Content filters — block auto-generated noise that's annoying
        # when relayed across networks.
        if self._filter_position_shares and _POSITION_SHARE_RE.search(text):
            self._inc_stat("msgs_dropped_position_share")
            if self._log_dropped:
                self.log.debug(
                    "mesh_bridge: dropped position-share from %s",
                    msg.get("from_id"),
                )
            return
        if self._filter_tapbacks and _looks_like_tapback(text):
            self._inc_stat("msgs_dropped_tapback")
            if self._log_dropped:
                self.log.debug(
                    "mesh_bridge: dropped tapback from %s: %r",
                    msg.get("from_id"), text,
                )
            return

        # 4. Resolve target
        target_transport = "meshcore" if origin == "mesh" else "meshtastic"
        msg_type = msg.get("msg_type", "direct")

        if msg_type == "broadcast":
            target, pair = self._resolve_broadcast_target(origin, msg.get("channel"))
        elif msg_type == "direct":
            if not self._bridge_dms:
                self._inc_stat("msgs_dropped_no_pair")
                return
            target, pair = self._resolve_dm_target(origin, msg.get("to_id"))
        else:
            self._inc_stat("msgs_dropped_no_pair")
            return

        if target is None or pair is None:
            self._inc_stat("msgs_dropped_no_pair")
            return

        # 5. Allow/deny regex filters (per-pair)
        body = text
        if pair.get("deny_regex") and pair["deny_regex"].search(body):
            self._inc_stat("msgs_dropped_filter")
            return
        if pair.get("allow_regex") and not pair["allow_regex"].search(body):
            self._inc_stat("msgs_dropped_filter")
            return

        # 6. Build attributed text (MTU-aware)
        tag = self._tag_mesh if origin == "mesh" else self._tag_core
        sender = _select_sender_label(msg.get("from_name"), msg.get("from_id"))
        header = f"{tag} {sender}: "
        mtu = self._mesh_mtu if target_transport == "meshtastic" else self._core_mtu
        attributed = truncate_for_mtu(header, body, mtu)

        # 7. Dispatch
        bridge_origin = {
            "transport": "meshtastic" if origin == "mesh" else "meshcore",
            "from_id": msg.get("from_id"),
            "from_name": msg.get("from_name"),
            "channel": msg.get("channel"),
            "msg_type": msg_type,
        }
        result = self._dispatch(
            target_transport, attributed, target, msg.get("channel"),
            msg_type, bridge_origin,
        )

        if not result.get("sent"):
            self._inc_stat("msgs_dropped_send_failed")
            reason = result.get("reason", "unknown")
            if reason in {"not_connected", "not connected", "rate_limited"}:
                self.log.warning(
                    "mesh_bridge: %s→%s send dropped (%s)",
                    origin, target_transport, reason,
                )
            else:
                self.log.info(
                    "mesh_bridge: %s→%s send failed: %s",
                    origin, target_transport, reason,
                )
            return

        # 8. Success — record dedup on BOTH sides, update stats
        self._dedup_record(key, now)
        opposite = "core" if origin == "mesh" else "mesh"
        self._dedup_record((opposite, from_id_norm, text_hash), now)
        stat_key = (
            "msgs_relayed_mesh_to_core" if origin == "mesh"
            else "msgs_relayed_core_to_mesh"
        )
        self._inc_stat(stat_key)
        self.log.info(
            'mesh_bridge: relayed %s→%s from %s: "%s"',
            origin, target_transport, sender, body[:60],
        )

        # 9. Circuit breaker
        self._check_circuit_breaker()

    # ── Target resolution ────────────────────────────────────────

    def _resolve_broadcast_target(
        self, origin: str, channel: Any,
    ) -> tuple[str | None, dict | None]:
        """Look up the opposite-network channel for a broadcast relay."""
        if not isinstance(channel, int):
            return None, None
        if origin == "mesh":
            pair = self._mesh_to_core.get(channel)
            if pair is None:
                return None, None
            return "broadcast", pair
        pair = self._core_to_mesh.get(channel)
        if pair is None:
            return None, None
        return "broadcast", pair

    def _resolve_dm_target(
        self, origin: str, to_id: str,
    ) -> tuple[str | None, dict | None]:
        """Look up the opposite-network identity for a DM relay."""
        if not to_id:
            return None, None
        if origin == "mesh":
            dest = self._dm_mesh_to_core.get(to_id)
        else:
            dest = self._dm_core_to_mesh.get(to_id.lower())
        if not dest:
            return None, None
        # DMs pass through filters but have no per-pair config; synthesize
        # a minimal pair so allow/deny logic stays uniform.
        return dest, {"allow_regex": None, "deny_regex": None}

    # ── Dispatch ─────────────────────────────────────────────────

    def _dispatch(
        self,
        transport: str,
        text: str,
        dest: str,
        channel: Any,
        msg_type: str,
        bridge_origin: dict,
    ) -> dict[str, Any]:
        """Send via messaging_hub (preferred) or fall back to gateway direct."""
        hub = self.app.get_plugin("messaging_hub")
        kwargs: dict[str, Any] = {
            "msg_type": msg_type,
            "metadata": {"bridge_origin": bridge_origin},
        }
        if msg_type == "broadcast" and isinstance(channel, int):
            kwargs["channel"] = channel
        if transport == "meshtastic":
            kwargs["sub_transport"] = "lora"

        destination = dest if msg_type == "direct" else "broadcast"
        try:
            if hub is not None and hasattr(hub, "send_message"):
                return hub.send_message(transport, text, destination, **kwargs)

            gw_name = f"{transport}_gateway"
            gw = self.app.get_plugin(gw_name)
            if gw is None or not hasattr(gw, "send_message"):
                return {"sent": False, "reason": "no gateway"}
            if transport == "meshtastic":
                return gw.send_message(
                    text,
                    destination_id=dest if msg_type == "direct" else None,
                    channel=channel if isinstance(channel, int) else None,
                )
            return gw.send_message(
                text,
                destination=dest if msg_type == "direct" else None,
                channel=channel if isinstance(channel, int) else None,
            )
        except Exception as exc:
            return {"sent": False, "reason": str(exc)}

    # ── Dedup cache ──────────────────────────────────────────────

    def _dedup_hit(
        self, key: tuple[str, str, int], now: float,
    ) -> bool:
        with self._lock:
            ts = self._dedup_cache.get(key)
            if ts is None:
                return False
            if now - ts > self._dedup_ttl:
                del self._dedup_cache[key]
                return False
            return True

    def _dedup_record(self, key: tuple[str, str, int], now: float) -> None:
        with self._lock:
            self._dedup_cache[key] = now
            if len(self._dedup_cache) > self._dedup_max:
                cutoff = now - self._dedup_ttl
                self._dedup_cache = {
                    k: v for k, v in self._dedup_cache.items() if v > cutoff
                }
                if len(self._dedup_cache) > self._dedup_max:
                    excess = len(self._dedup_cache) - self._dedup_max
                    for k in list(self._dedup_cache)[:excess]:
                        del self._dedup_cache[k]

    # ── Circuit breaker ──────────────────────────────────────────

    def _check_circuit_breaker(self) -> None:
        if self._auto_pause_threshold <= 0:
            return
        now = time.monotonic()
        with self._lock:
            self._recent_relays.append(now)
            cutoff = now - _CIRCUIT_BREAKER_WINDOW
            while self._recent_relays and self._recent_relays[0] < cutoff:
                self._recent_relays.popleft()
            tripped = len(self._recent_relays) > self._auto_pause_threshold
        if tripped:
            self.log.warning(
                "mesh_bridge: auto-pausing (rate limit: %d relays in %ds)",
                self._auto_pause_threshold + 1, int(_CIRCUIT_BREAKER_WINDOW),
            )
            self.set_running(False, reason="rate_limit")

    # ── State persistence ────────────────────────────────────────

    def _resolve_state_path(self) -> str:
        override = self.config.get("state_path")
        if override:
            return os.path.expanduser(override)
        base = os.environ.get(
            "XDG_DATA_HOME",
            os.path.expanduser("~/.local/share"),
        )
        return os.path.join(base, "reticulumpi", _STATE_FILENAME)

    def _load_state(self, default_running: bool) -> bool:
        try:
            with open(self._state_path, encoding="utf-8") as f:
                state = json.load(f)
            self._auto_paused_reason = state.get("auto_paused_reason")
            self._auto_paused_at = state.get("auto_paused_at")
            return bool(state.get("running", default_running))
        except (OSError, ValueError):
            return default_running

    def _persist_state(self, state: dict | None = None) -> None:
        if state is None:
            with self._lock:
                state = {
                    "running": self._running,
                    "auto_paused_reason": self._auto_paused_reason,
                    "auto_paused_at": self._auto_paused_at,
                }
        tmp = self._state_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._state_path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp, self._state_path)
        except Exception:
            self.log.warning(
                "mesh_bridge: failed to persist state to %s",
                self._state_path, exc_info=True,
            )
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # ── Internal ─────────────────────────────────────────────────

    def _inc_stat(self, key: str) -> None:
        with self._lock:
            self._stats[key] = self._stats.get(key, 0) + 1
