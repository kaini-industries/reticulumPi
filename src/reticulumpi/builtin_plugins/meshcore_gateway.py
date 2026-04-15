"""MeshCore Gateway plugin — bridges MeshCore LoRa mesh with ReticulumPi.

Connects to a MeshCore companion radio over USB serial and exposes the
MeshCore mesh to the ReticulumPi event bus.  Incoming direct and channel
messages are published as events; the messaging hub adapter (in
messaging_hub.py) stores them and makes them available in the dashboard.

The meshcore Python library is fully asyncio-based, so this plugin runs
a dedicated asyncio event loop in a background thread and bridges
async↔sync at the boundary.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import threading
import time
from typing import Any

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase


class MeshCoreGateway(PluginBase):
    """Bridges MeshCore LoRa mesh messages with the ReticulumPi event bus."""

    plugin_name = "meshcore_gateway"
    plugin_description = "Bridges MeshCore LoRa mesh with ReticulumPi"
    plugin_version = "1.0.0"

    # ── Configuration validation ────────────────────────────────────

    def validate_config(self) -> None:
        try:
            import meshcore  # noqa: F401
        except ImportError:
            raise ValueError(
                "meshcore package not found. "
                "Install with: pip install meshcore  "
                "(or: pip install reticulumpi[meshcore])"
            )

        port = self.config.get("serial_port", "/dev/meshcore")
        if not isinstance(port, str) or not port:
            raise ValueError("serial_port must be a non-empty string")

        baud = self.config.get("baudrate", 115200)
        if not isinstance(baud, int) or baud <= 0:
            raise ValueError("baudrate must be a positive integer")

        hci = self.config.get("health_check_interval", 30)
        if not isinstance(hci, (int, float)) or hci < 5:
            raise ValueError("health_check_interval must be >= 5 seconds")

        rd = self.config.get("reconnect_delay", 10)
        if not isinstance(rd, (int, float)) or rd < 1:
            raise ValueError("reconnect_delay must be >= 1 second")

        mra = self.config.get("max_reconnect_attempts", 10)
        if not isinstance(mra, int) or mra < 0:
            raise ValueError("max_reconnect_attempts must be a non-negative integer")

        mpm = self.config.get("max_messages_per_minute", 0)
        if not isinstance(mpm, (int, float)) or mpm < 0:
            raise ValueError("max_messages_per_minute must be >= 0 (0 = unlimited)")

        ai = self.config.get("advert_interval", 900)
        if not isinstance(ai, (int, float)) or ai < 60:
            raise ValueError("advert_interval must be >= 60 seconds")

        scd = self.config.get("stale_contact_days", 30)
        if not isinstance(scd, (int, float)) or scd < 0:
            raise ValueError("stale_contact_days must be >= 0 (0 = show all)")

    # ── Lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        self._lock = threading.Lock()

        # Stats
        self._msgs_received = 0
        self._msgs_sent = 0
        self._msgs_rate_limited = 0
        self._connect_count = 0
        self._reconnect_failures = 0
        self._last_msg_time: float | None = None

        # Rate limiting
        max_per_min = self.config.get("max_messages_per_minute", 0)
        if max_per_min > 0:
            self._send_min_interval = 60.0 / max_per_min
        else:
            self._send_min_interval = 0
        self._last_send_time = 0.0

        # MeshCore state
        self._mc: Any = None
        self._connected = False
        self._device_info: dict[str, Any] = {}
        self._subscriptions: list[Any] = []
        self._last_advert_time: float = 0
        self._advert_interval: float = self.config.get("advert_interval", 900)

        # Contact name cache (survives reconnects)
        self._contact_cache: dict[str, dict[str, Any]] = {}
        self._cache_path = ""
        storage_dir = os.path.expanduser(
            self.config.get("storage_path", "~/.local/share/reticulumpi/meshcore_gw")
        )
        os.makedirs(storage_dir, exist_ok=True)
        self._cache_path = os.path.join(storage_dir, "contact_cache.json")
        self._load_contact_cache()

        # Dedicated asyncio event loop for the meshcore library
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()

        self._active = True
        self._start_thread(self._run_async_loop, "meshcore-loop")
        # Wait for the event loop to be ready before starting the connection thread
        self._loop_ready.wait(timeout=10)
        self._start_thread(self._connection_loop, "meshcore-connect")

        self.log.info(
            "MeshCore Gateway started (port=%s)",
            self.config.get("serial_port", "/dev/meshcore"),
        )

    def stop(self) -> None:
        self._active = False
        # Disconnect MeshCore in the async loop
        if self._loop and self._mc:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._async_disconnect(), self._loop,
                )
                future.result(timeout=5)
            except Exception:
                self.log.debug("Error during MeshCore disconnect", exc_info=True)
        # Shut down the asyncio event loop
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._join_threads()

    # ── Asyncio event loop (dedicated thread) ───────────────────────

    def _run_async_loop(self) -> None:
        """Run a dedicated asyncio event loop for the meshcore library."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        try:
            self._loop.run_forever()
        finally:
            # Clean up pending tasks
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.close()

    def _run_async(self, coro: Any, timeout: float = 15) -> Any:
        """Run an async coroutine from a sync context, blocking until done."""
        if not self._loop or not self._loop.is_running():
            raise RuntimeError("MeshCore async loop not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # ── Connection management ───────────────────────────────────────

    def _connection_loop(self) -> None:
        """Background thread: connect to MeshCore device and monitor health."""
        reconnect_delay = self.config.get("reconnect_delay", 10)
        health_check_interval = self.config.get("health_check_interval", 30)
        max_attempts = self.config.get("max_reconnect_attempts", 10)

        while self._active:
            if not self._connected:
                try:
                    self._connect_device()
                    self._reconnect_failures = 0
                except Exception as exc:
                    self._reconnect_failures += 1
                    self.log.warning(
                        "MeshCore connect failed (%d): %s",
                        self._reconnect_failures,
                        exc,
                    )
                    self.event_bus.publish(events.MESHCORE_CONNECT_FAILED, {
                        "error": str(exc),
                        "attempt": self._reconnect_failures,
                    })
                    if max_attempts > 0 and self._reconnect_failures >= max_attempts:
                        self.log.error(
                            "Max reconnect attempts (%d) reached, giving up",
                            max_attempts,
                        )
                        self._active = False
                        break
                    backoff = min(
                        reconnect_delay * (2 ** min(self._reconnect_failures - 1, 5)),
                        300,
                    )
                    self.log.debug("Reconnect backoff: %ds", backoff)
                    self._sleep_while_active(backoff)
                    continue

            # Health check
            self._sleep_while_active(health_check_interval)
            if self._connected and not self._check_health():
                self.log.warning("MeshCore health check failed, reconnecting")
                self._disconnect_device()
                self.event_bus.publish(events.MESHCORE_DISCONNECTED, {
                    "reason": "health_check_failed",
                })
                continue

            # Periodic advertisement
            if self._connected and self._advert_interval > 0:
                now = time.time()
                if now - self._last_advert_time >= self._advert_interval:
                    self._send_periodic_advert()

    def _connect_device(self) -> None:
        """Open connection to the MeshCore companion radio."""
        from meshcore import MeshCore
        from meshcore.events import EventType

        serial_port = self.config.get("serial_port", "/dev/meshcore")
        baudrate = self.config.get("baudrate", 115200)

        self.log.info("Connecting to MeshCore device (port=%s)...", serial_port)

        mc = self._run_async(
            MeshCore.create_serial(
                serial_port,
                baudrate,
                auto_reconnect=True,
                max_reconnect_attempts=3,
            ),
            timeout=30,
        )
        if mc is None:
            raise ConnectionError(
                f"MeshCore device on {serial_port} did not respond — "
                "is it flashed with Companion Radio firmware?"
            )

        # Sync time
        self._run_async(mc.commands.set_time(int(time.time())))

        # Query device info
        result = self._run_async(mc.commands.send_device_query())
        if result and hasattr(result, "payload"):
            self._device_info = dict(result.payload)

        # Fetch contacts
        self._run_async(mc.commands.get_contacts())
        self._sync_contact_cache(mc)

        # Subscribe to incoming messages
        subs = []

        async def _on_contact_msg(event):
            self._handle_incoming_message(event, msg_type="direct")

        async def _on_channel_msg(event):
            self._handle_incoming_message(event, msg_type="broadcast")

        async def _on_disconnect(event):
            self.log.warning("MeshCore device disconnected")
            with self._lock:
                self._connected = False
            self.event_bus.publish(events.MESHCORE_DISCONNECTED, {
                "reason": "device_disconnected",
            })

        async def _on_ack(event):
            self._handle_ack_event(event)

        subs.append(mc.subscribe(EventType.CONTACT_MSG_RECV, _on_contact_msg))
        subs.append(mc.subscribe(EventType.CHANNEL_MSG_RECV, _on_channel_msg))
        subs.append(mc.subscribe(EventType.DISCONNECTED, _on_disconnect))
        subs.append(mc.subscribe(EventType.ACK, _on_ack))

        # Start auto-fetching queued messages
        auto_sub = self._run_async(mc.start_auto_message_fetching())
        subs.append(auto_sub)

        with self._lock:
            self._mc = mc
            self._connected = True
            self._connect_count += 1
            self._subscriptions = subs

        # Send initial advertisement so other MeshCore nodes can discover us
        try:
            self._run_async(mc.commands.send_advert(), timeout=10)
            self._last_advert_time = time.time()
            self.log.info("MeshCore initial advertisement sent")
        except Exception:
            self.log.warning("Failed to send initial MeshCore advertisement", exc_info=True)

        fw = self._device_info.get("ver", "unknown")
        model = self._device_info.get("model", "unknown")
        self.log.info("MeshCore connected: %s %s", model, fw)
        self.event_bus.publish(events.MESHCORE_CONNECTED, {
            "firmware": fw,
            "model": model,
            "serial_port": serial_port,
        })

    def _disconnect_device(self) -> None:
        """Tear down the MeshCore connection."""
        with self._lock:
            mc = self._mc
            subs = self._subscriptions
            self._mc = None
            self._connected = False
            self._subscriptions = []

        if mc is None:
            return

        # Unsubscribe event handlers
        for sub in subs:
            try:
                mc.unsubscribe(sub)
            except Exception:
                pass

        # Disconnect
        try:
            self._run_async(self._async_disconnect_mc(mc), timeout=5)
        except Exception:
            self.log.debug("Error disconnecting MeshCore", exc_info=True)

        self._save_contact_cache()

    async def _async_disconnect(self) -> None:
        """Async helper for stop() — disconnects the current MeshCore instance."""
        mc = self._mc
        if mc:
            try:
                await mc.stop_auto_message_fetching()
            except Exception:
                pass
            try:
                await mc.disconnect()
            except Exception:
                pass

    @staticmethod
    async def _async_disconnect_mc(mc: Any) -> None:
        """Async helper — disconnect a specific MeshCore instance."""
        try:
            await mc.stop_auto_message_fetching()
        except Exception:
            pass
        try:
            await mc.disconnect()
        except Exception:
            pass

    def _check_health(self) -> bool:
        """Return True if the MeshCore connection appears healthy."""
        with self._lock:
            mc = self._mc
        if mc is None:
            return False
        try:
            return mc.is_connected
        except Exception:
            return False

    def _send_periodic_advert(self) -> None:
        """Send a periodic advertisement to the MeshCore mesh."""
        with self._lock:
            mc = self._mc
        if mc is None:
            return
        try:
            self._run_async(mc.commands.send_advert(), timeout=10)
            self._last_advert_time = time.time()
            self.log.debug("MeshCore periodic advertisement sent")
        except Exception:
            self.log.warning("Failed to send periodic MeshCore advertisement", exc_info=True)

    # ── Message handling ────────────────────────────────────────────

    def _resolve_pubkey_prefix(self, prefix: str) -> tuple[str, str]:
        """Resolve a pubkey_prefix to (full_key, adv_name) from contact cache.

        Returns the first contact whose key starts with *prefix*.
        Falls back to (prefix, "") if no match.
        """
        for key, contact in self._contact_cache.items():
            if key.startswith(prefix):
                return key, contact.get("adv_name", "")
        return prefix, ""

    def _handle_incoming_message(
        self, event: Any, msg_type: str = "direct",
    ) -> None:
        """Process an incoming MeshCore message event."""
        try:
            payload = event.payload if isinstance(event.payload, dict) else {}
            text = payload.get("text", str(event.payload) if not isinstance(event.payload, dict) else "")
            channel = payload.get("channel_idx")

            # Direct messages carry a pubkey_prefix; channel messages don't
            pubkey_prefix = payload.get("pubkey_prefix", "")
            if pubkey_prefix:
                from_key, from_name = self._resolve_pubkey_prefix(pubkey_prefix)
            else:
                from_key = ""
                from_name = ""

            if not text.strip():
                return

            from_label = f"{from_name} ({from_key[:12]})" if from_name else (from_key[:12] or "unknown")

            if msg_type == "broadcast":
                self.log.info(
                    "MeshCore channel msg (ch%s) from %s: %s",
                    channel, from_label, text[:80],
                )
            else:
                self.log.info(
                    "MeshCore direct msg from %s: %s",
                    from_label, text[:80],
                )

            with self._lock:
                self._msgs_received += 1
                self._last_msg_time = time.time()

            self.event_bus.publish(events.MESHCORE_MESSAGE_RECEIVED, {
                "from_key": from_key,
                "from_name": from_name,
                "text": text[:500],
                "msg_type": msg_type,
                "channel": channel,
            })

        except Exception:
            self.log.exception("Error handling incoming MeshCore message")

    def _handle_ack_event(self, event: Any) -> None:
        """Process an incoming MeshCore ACK event and publish on the event bus."""
        try:
            ack_code = ""
            if hasattr(event, "attributes") and isinstance(event.attributes, dict):
                ack_code = event.attributes.get("code", "")
            if not ack_code and isinstance(event.payload, dict):
                ack_code = event.payload.get("code", "")
            if ack_code:
                self.log.debug("MeshCore ACK received: %s", ack_code)
                self.event_bus.publish(events.MESHCORE_MESSAGE_ACKED, {
                    "ack_code": ack_code,
                })
        except Exception:
            self.log.exception("Error handling MeshCore ACK event")

    # ── Rate limiting ───────────────────────────────────────────────

    def _check_send_rate_limit(self) -> bool:
        """Check if we're allowed to send a message."""
        if self._send_min_interval <= 0:
            return True
        now = time.time()
        with self._lock:
            if now - self._last_send_time >= self._send_min_interval:
                self._last_send_time = now
                return True
            self._msgs_rate_limited += 1
            return False

    # ── Contact cache ───────────────────────────────────────────────

    def _sync_contact_cache(self, mc: Any) -> None:
        """Update the local contact cache from the MeshCore device."""
        try:
            contacts = mc.contacts or {}
            for key, contact in contacts.items():
                self._contact_cache[key] = dict(contact)
            self._save_contact_cache()
        except Exception:
            self.log.debug("Error syncing contact cache", exc_info=True)

    def _load_contact_cache(self) -> None:
        """Load the persisted contact cache from disk."""
        if not self._cache_path:
            return
        try:
            with open(self._cache_path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._contact_cache = data
                self.log.debug("Loaded %d MeshCore contacts from cache", len(data))
        except FileNotFoundError:
            pass
        except Exception:
            self.log.debug("Error loading MeshCore contact cache", exc_info=True)

    def _save_contact_cache(self) -> None:
        """Persist the contact cache to disk."""
        if not self._cache_path:
            return
        try:
            with open(self._cache_path, "w") as f:
                json.dump(self._contact_cache, f)
        except Exception:
            self.log.debug("Error saving MeshCore contact cache", exc_info=True)

    # ── Public send API ─────────────────────────────────────────────

    def send_message(
        self,
        text: str,
        destination: str | None = None,
        channel: int | None = None,
    ) -> dict[str, Any]:
        """Send a text message to the MeshCore mesh.

        Args:
            text: Message text.
            destination: Public key prefix for direct messages.
                ``None`` sends a channel broadcast.
            channel: Channel index for broadcast (default 0).

        Returns:
            ``{"sent": True, "expected_ack": str|None, ...}`` on success, or
            ``{"sent": False, "reason": str}`` on failure.
            ``expected_ack`` is set for direct messages and can be used to
            match against incoming ACK events.
        """
        if not self._check_send_rate_limit():
            return {"sent": False, "reason": "rate_limited"}

        with self._lock:
            mc = self._mc
            connected = self._connected
        if not connected or mc is None:
            return {"sent": False, "reason": "not_connected"}

        try:
            if destination:
                result = self._run_async(
                    mc.commands.send_msg(destination, text),
                    timeout=10,
                )
            else:
                ch = channel if channel is not None else 0
                result = self._run_async(
                    mc.commands.send_chan_msg(ch, text),
                    timeout=10,
                )
        except Exception as exc:
            self.log.exception("Error sending MeshCore message")
            return {"sent": False, "reason": str(exc)}

        from meshcore.events import EventType
        if result and result.type == EventType.ERROR:
            reason = result.payload.get("reason", "unknown error") if isinstance(result.payload, dict) else str(result.payload)
            return {"sent": False, "reason": reason}

        with self._lock:
            self._msgs_sent += 1

        dest_label = destination[:12] if destination else f"channel {channel or 0}"
        self.log.info("Sent MeshCore message to %s", dest_label)

        # Extract ack tracking info for direct messages
        expected_ack = None
        suggested_timeout = None
        if destination and result and isinstance(result.payload, dict):
            raw_ack = result.payload.get("expected_ack")
            if raw_ack is not None:
                expected_ack = raw_ack.hex() if isinstance(raw_ack, bytes) else str(raw_ack)
            suggested_timeout = result.payload.get("suggested_timeout")

        self.event_bus.publish(events.MESHCORE_MESSAGE_SENT, {
            "text": text[:100],
            "destination": dest_label,
            "expected_ack": expected_ack,
        })
        return {
            "sent": True,
            "expected_ack": expected_ack,
            "suggested_timeout": suggested_timeout,
        }

    # ── Public query methods ────────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        """Return current gateway status for monitoring and API."""
        with self._lock:
            return {
                "active": self._active,
                "connected": self._connected,
                "serial_port": self.config.get("serial_port", "/dev/meshcore"),
                "firmware": self._device_info.get("ver"),
                "model": self._device_info.get("model"),
                "msgs_received": self._msgs_received,
                "msgs_sent": self._msgs_sent,
                "msgs_rate_limited": self._msgs_rate_limited,
                "connect_count": self._connect_count,
                "reconnect_failures": self._reconnect_failures,
                "last_msg_time": self._last_msg_time,
                "contacts": len(self._contact_cache),
            }

    def get_device_info(self) -> dict[str, Any]:
        """Return MeshCore device hardware and firmware info."""
        with self._lock:
            info: dict[str, Any] = {
                "connected": self._connected,
                "serial_port": self.config.get("serial_port", "/dev/meshcore"),
            }
            info.update(self._device_info)
            return info

    def get_contacts(self) -> list[dict[str, Any]]:
        """Return known MeshCore contacts."""
        # Prefer live contacts from the device, fall back to cache
        with self._lock:
            mc = self._mc
            connected = self._connected

        contacts_dict: dict[str, Any] = {}
        if connected and mc is not None:
            try:
                contacts_dict = dict(mc.contacts or {})
                # Update cache while we're at it
                for key, contact in contacts_dict.items():
                    self._contact_cache[key] = dict(contact)
            except Exception:
                self.log.debug("Error reading live contacts", exc_info=True)
                contacts_dict = dict(self._contact_cache)
        else:
            contacts_dict = dict(self._contact_cache)

        # Filter stale contacts (0 = show all)
        max_age_days = self.config.get("stale_contact_days", 30)
        cutoff = (time.time() - max_age_days * 86400) if max_age_days > 0 else 0

        result = []
        for key, contact in contacts_dict.items():
            last_advert = contact.get("last_advert", 0)
            if cutoff and last_advert < cutoff:
                continue
            result.append({
                "public_key": key,
                "name": contact.get("adv_name", ""),
                "type": contact.get("type", 0),
                "last_advert": last_advert,
                "latitude": contact.get("adv_lat", 0),
                "longitude": contact.get("adv_lon", 0),
                "flags": contact.get("flags", 0),
                "out_path_len": contact.get("out_path_len", -1),
            })
        return result

    def get_meshcore_nodes(self) -> list[dict[str, Any]]:
        """Return contacts formatted for the messaging hub adapter."""
        contacts = self.get_contacts()
        return [
            {
                "id": c["public_key"],
                "name": c.get("name") or c["public_key"][:12],
                "last_heard": c.get("last_advert"),
            }
            for c in contacts
        ]
