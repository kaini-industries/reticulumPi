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
import json
import os
import threading
import time
from typing import Any

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase
from reticulumpi.serial_devices import (
    SerialDeviceChangedError,
    SerialDeviceLease,
    StaleSerialDeviceLeaseError,
    serial_device_registry,
    validate_stable_serial_path,
)

_DELIVERY_UNCERTAIN_REASON = "delivery_uncertain_connection_changed"


class MeshCoreGateway(PluginBase):
    """Bridges MeshCore LoRa mesh messages with the ReticulumPi event bus."""

    plugin_name = "meshcore_gateway"
    plugin_description = "Bridges MeshCore LoRa mesh with ReticulumPi"
    plugin_version = "1.0.3"
    broadcast_tier = 1
    broadcast_keys = ["meshcore_status", "meshcore_device", "meshcore_contacts"]

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
        validate_stable_serial_path(port)

        baud = self.config.get("baudrate", 115200)
        if not isinstance(baud, int) or baud <= 0:
            raise ValueError("baudrate must be a positive integer")

        hci = self.config.get("health_check_interval", 30)
        if not isinstance(hci, (int, float)) or hci < 5:
            raise ValueError("health_check_interval must be >= 5 seconds")

        hqt = self.config.get("health_query_timeout", 5)
        if not isinstance(hqt, (int, float)) or isinstance(hqt, bool) or not 0 < hqt <= 30:
            raise ValueError("health_query_timeout must be > 0 and <= 30 seconds")

        hrma = self.config.get("health_response_max_age", max(float(hci) * 2, float(hqt)))
        if not isinstance(hrma, (int, float)) or isinstance(hrma, bool) or not 0 <= hrma <= 3600:
            raise ValueError("health_response_max_age must be between 0 and 3600 seconds")

        hft = self.config.get("health_failure_threshold", 3)
        if not isinstance(hft, int) or isinstance(hft, bool) or hft < 1:
            raise ValueError("health_failure_threshold must be a positive integer")

        rd = self.config.get("reconnect_delay", 10)
        if not isinstance(rd, (int, float)) or rd < 1:
            raise ValueError("reconnect_delay must be >= 1 second")

        mra = self.config.get("max_reconnect_attempts", 0)
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
        self._disconnect_lock = threading.Lock()
        self._serial_device_lease: SerialDeviceLease | None = None
        self._serial_reopen_blocked = False
        self._connection_generation = 0
        self._open_attempt: dict[str, Any] | None = None
        self._teardown_attempt: dict[str, Any] | None = None
        self._device_teardown_timeout = 5.0
        self._broadcast_cache: tuple[float, dict] | None = None
        self._broadcast_cache_ttl = 5.0

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
        self._last_contact_refresh: float = 0
        self._contact_refresh_interval: float = self.config.get("contact_refresh_interval", 300)

        # The meshcore library's ``is_connected`` value is cached and can
        # remain true after a USB companion stops answering.  Health is based
        # on a bounded local device query instead, with a short response-age
        # grace period and consecutive-failure hysteresis before reconnecting
        # this plugin's client.
        health_check_interval = float(self.config.get("health_check_interval", 30))
        self._health_query_timeout: float = float(self.config.get("health_query_timeout", 5))
        self._health_response_max_age: float = float(
            self.config.get(
                "health_response_max_age",
                max(health_check_interval * 2, self._health_query_timeout),
            )
        )
        self._health_failure_threshold: int = int(self.config.get("health_failure_threshold", 3))
        self._health_consecutive_failures: int = 0
        self._health_query_failures: int = 0
        self._last_device_response_monotonic: float = 0.0
        self._last_device_response_time: float | None = None

        # Fuzzy dedup cache — MeshCore packets may arrive twice under mesh
        # flooding, and the companion radio exposes no packet ID. Key on
        # (from_key_prefix, msg_type, text) and drop duplicates within a
        # small window.
        self._dedup_ttl_seconds: float = max(
            10.0,
            float(self.config.get("dedup_ttl_seconds", 60.0)),
        )
        self._dedup_max_entries: int = max(
            32,
            int(self.config.get("dedup_max_entries", 256)),
        )
        self._seen_msg_keys: dict[tuple[str, str, int], float] = {}
        # Cleanup is amortized: we only scan for stale/over-cap entries
        # every N inserts, not per-packet, to keep dedup O(1) amortized.
        self._seen_msg_cleanup_interval: int = max(
            16,
            self._dedup_max_entries // 8,
        )
        self._seen_msg_inserts_since_cleanup: int = 0

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
        if not self._loop_ready.wait(timeout=10):
            self.log.error("MeshCore event loop failed to start within 10s timeout")
        self._start_thread(self._connection_loop, "meshcore-connect")

        self.log.info(
            "MeshCore Gateway started (port=%s)",
            self.config.get("serial_port", "/dev/meshcore"),
        )

    def stop(self) -> None:
        self._active = False
        try:
            disconnect_proven = self._disconnect_device()
        except Exception:
            disconnect_proven = False
            self.log.exception("Unexpected error while closing MeshCore during shutdown")
        try:
            loop = self._loop
            # Shut down the asyncio event loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
            self._join_threads()
        finally:
            # A timed join is not proof that the serial owner stopped. Retain
            # the claim if disconnect failed, a managed worker is still alive,
            # or a late connection published a new handle during shutdown.
            with self._threads_lock:
                live_threads = [thread.name for thread in self._threads if thread.is_alive()]
            with self._lock:
                handle_cleared = self._mc is None
                reopen_blocked = self._serial_reopen_blocked
                open_quiescent = self._open_attempt is None
                teardown_quiescent = self._teardown_attempt is None
            if (
                disconnect_proven
                and handle_cleared
                and not reopen_blocked
                and open_quiescent
                and teardown_quiescent
                and not live_threads
            ):
                self._release_serial_device_lease()
            elif self._serial_device_lease is not None:
                self.log.warning(
                    "MeshCore shutdown was not proven quiescent; retaining serial-device "
                    "ownership until process exit (live threads: %s)",
                    ", ".join(live_threads) or "none",
                )

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
            loop = self._loop
            self._loop = None
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def _run_async(self, coro: Any, timeout: float = 15) -> Any:
        """Run an async coroutine from a sync context, blocking until done."""
        if not self._loop or not self._loop.is_running():
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            raise RuntimeError("MeshCore async loop not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            # A timed-out serial command must not remain queued and consume a
            # later response intended for the next liveness query.
            future.cancel()
            raise

    @staticmethod
    def _close_awaitable(awaitable: Any) -> None:
        """Close an unscheduled coroutine without assuming its concrete type."""
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()

    def _begin_connection_generation(self) -> int:
        """Reserve one connection generation, refusing overlap with old work."""
        with self._lock:
            if (
                self._mc is not None
                or self._open_attempt is not None
                or self._teardown_attempt is not None
            ):
                self._serial_reopen_blocked = True
                raise RuntimeError("previous MeshCore connection work is not quiescent")
            self._connection_generation += 1
            return self._connection_generation

    def _connection_is_current(
        self,
        mc: Any,
        generation: int,
        *,
        require_connected: bool = False,
    ) -> bool:
        """Return whether a client still owns the current live generation."""
        with self._lock:
            return (
                self._active
                and self._connection_generation == generation
                and self._mc is mc
                and (self._connected or not require_connected)
            )

    def _require_current_connection(self, mc: Any, generation: int) -> None:
        if not self._connection_is_current(mc, generation):
            raise RuntimeError("MeshCore connection became stale during initialization")

    def _invalidate_connection_generation(self) -> None:
        """Fence callbacks/setup and request cancellation of an in-flight open."""
        future = None
        with self._lock:
            self._connection_generation += 1
            self._connected = False
            attempt = self._open_attempt
            if attempt is not None:
                attempt["abandoned"] = True
                self._serial_reopen_blocked = True
                future = attempt.get("future")
        if future is not None:
            future.cancel()

    async def _async_close_unpublished_mc(self, mc: Any) -> bool:
        """Close a client returned after its caller timed out or was stopped."""
        try:
            await mc.disconnect()
        except BaseException:
            self.log.debug("Error closing late MeshCore client", exc_info=True)
            return False
        return True

    def _run_tracked_open(self, coro: Any, generation: int, timeout: float) -> Any:
        """Run an SDK create call without losing cancellation-resistant results.

        The wrapper records a successfully-created client on ``self`` before it
        wakes the synchronous caller.  If the generation has become stale, it
        closes the late result on the owning event loop.  A create coroutine
        that ignores cancellation remains represented by ``_open_attempt``, so
        neither a retry nor serial-lease release can race it.
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            # Compatibility path for lightweight hosts/tests.  Production opens
            # always use the dedicated running loop above.
            mc = self._run_async(coro, timeout=timeout)
            with self._lock:
                if mc is not None:
                    self._mc = mc
                    self._connected = False
                    self._subscriptions = []
                    self._serial_reopen_blocked = not (
                        self._active and self._connection_generation == generation
                    )
            return mc

        done = threading.Event()
        attempt: dict[str, Any] = {
            "generation": generation,
            "done": done,
            "future": None,
            "result": None,
            "error": None,
            "abandoned": False,
            "orphaned_client": None,
        }
        with self._lock:
            if self._connection_generation != generation or self._open_attempt is not None:
                self._close_awaitable(coro)
                raise RuntimeError("MeshCore open generation is no longer current")
            self._open_attempt = attempt
            self._serial_reopen_blocked = True

        async def _capture_open_result() -> None:
            mc = None
            error: BaseException | None = None
            try:
                mc = await coro
            except BaseException as exc:  # includes cancellation
                error = exc

            close_late = False
            with self._lock:
                current = (
                    self._active
                    and self._connection_generation == generation
                    and self._open_attempt is attempt
                    and not attempt["abandoned"]
                )
                if error is None and current:
                    attempt["result"] = mc
                    if mc is not None:
                        # Publish the disconnected candidate atomically before
                        # waking the setup thread.  Stop can now always find it.
                        self._mc = mc
                        self._connected = False
                        self._subscriptions = []
                    self._open_attempt = None
                    self._serial_reopen_blocked = False
                elif error is not None:
                    attempt["error"] = error
                    if self._open_attempt is attempt:
                        self._open_attempt = None
                        self._serial_reopen_blocked = self._mc is not None
                else:
                    close_late = mc is not None
                    if mc is None and self._open_attempt is attempt:
                        self._open_attempt = None
                        self._serial_reopen_blocked = self._mc is not None

            if close_late:
                closed = await self._async_close_unpublished_mc(mc)
                with self._lock:
                    if not closed:
                        # Make a failed late close visible to the normal
                        # teardown path instead of losing the only handle.
                        if self._mc is None:
                            self._mc = mc
                            self._connected = False
                            self._subscriptions = []
                        else:
                            attempt["orphaned_client"] = mc
                        self._serial_reopen_blocked = True
                    if self._open_attempt is attempt and attempt["orphaned_client"] is None:
                        self._open_attempt = None
                    if closed and self._open_attempt is None and self._mc is None:
                        self._serial_reopen_blocked = False

            done.set()

        wrapper = _capture_open_result()
        try:
            future = asyncio.run_coroutine_threadsafe(wrapper, loop)
        except BaseException:
            self._close_awaitable(wrapper)
            self._close_awaitable(coro)
            with self._lock:
                if self._open_attempt is attempt:
                    self._open_attempt = None
                    self._serial_reopen_blocked = self._mc is not None
            raise

        cancel_now = False
        with self._lock:
            attempt["future"] = future
            cancel_now = bool(attempt["abandoned"])
        if cancel_now:
            future.cancel()

        if not done.wait(timeout=timeout):
            timed_out = True
            with self._lock:
                if done.is_set():
                    timed_out = False
                    future = None
                elif self._open_attempt is attempt:
                    attempt["abandoned"] = True
                    self._serial_reopen_blocked = True
                    future = attempt.get("future")
                else:
                    future = None
            if future is not None:
                future.cancel()
            if timed_out:
                raise TimeoutError("MeshCore create operation timed out")

        error = attempt["error"]
        if error is not None:
            if isinstance(error, asyncio.CancelledError):
                raise RuntimeError("MeshCore create operation was cancelled")
            raise error
        return attempt["result"]

    # ── Connection management ───────────────────────────────────────

    def _ensure_serial_device_lease(self, configured_path: str) -> SerialDeviceLease:
        """Claim or revalidate the configured physical serial endpoint.

        MeshCore Observer's supported shared mode borrows this gateway's live
        client and never opens the device itself.  Independent serial opens
        therefore remain exclusive and do not receive a registry share token.
        """
        with self._lock:
            lease = self._serial_device_lease
            if lease is not None:
                try:
                    lease.revalidate()
                    return lease
                except (SerialDeviceChangedError, StaleSerialDeviceLeaseError):
                    # USB re-enumeration can change the tty binding.  Release
                    # exactly the stale claim before atomically claiming the
                    # configured path's current identity.
                    lease.release()
                    self._serial_device_lease = None

            lease = serial_device_registry.claim(configured_path, self.plugin_name)
            self._serial_device_lease = lease
            try:
                # Close the claim/open race as far as the filesystem API
                # permits by resolving the endpoint again immediately before
                # handing the configured path to MeshCore.
                lease.revalidate()
            except Exception:
                lease.release()
                self._serial_device_lease = None
                raise
            return lease

    def _release_serial_device_lease(self) -> None:
        """Release this plugin's exact serial claim, if one is active."""
        with self._lock:
            lease = self._serial_device_lease
            self._serial_device_lease = None
        if lease is not None:
            lease.release()

    def _connection_loop(self) -> None:
        """Background thread: connect to MeshCore device and monitor health."""
        reconnect_delay = self.config.get("reconnect_delay", 10)
        health_check_interval = self.config.get("health_check_interval", 30)
        max_attempts = self.config.get("max_reconnect_attempts", 0)

        while self._active:
            if not self._connected:
                # A DISCONNECTED callback marks the handle unusable but
                # leaves teardown to this owner thread.  Dispose only the
                # MeshCore client before the normal hotplug retry path.
                with self._lock:
                    stale_mc = self._mc
                if stale_mc is not None:
                    if not self._disconnect_device():
                        self.log.warning(
                            "MeshCore serial teardown is still uncertain; refusing a second open"
                        )
                        self._sleep_while_active(reconnect_delay)
                        continue
                try:
                    self._connect_device()
                    self._reconnect_failures = 0
                    with self._lock:
                        self._health_consecutive_failures = 0
                except Exception as exc:
                    self._reconnect_failures += 1
                    self.log.warning(
                        "MeshCore connect failed (%d): %s",
                        self._reconnect_failures,
                        exc,
                    )
                    self.event_bus.publish(
                        events.MESHCORE_CONNECT_FAILED,
                        {
                            "error": str(exc),
                            "attempt": self._reconnect_failures,
                        },
                    )
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
            if self._connected:
                if self._check_health():
                    with self._lock:
                        recovered_failures = self._health_consecutive_failures
                        self._health_consecutive_failures = 0
                    if recovered_failures > 0:
                        self.log.info(
                            "MeshCore health recovered after %d failed check(s)",
                            recovered_failures,
                        )
                else:
                    with self._lock:
                        self._health_consecutive_failures += 1
                        consecutive_health_failures = self._health_consecutive_failures
                    if consecutive_health_failures < self._health_failure_threshold:
                        self.log.info(
                            "MeshCore health check failed (%d/%d) — will retry",
                            consecutive_health_failures,
                            self._health_failure_threshold,
                        )
                        continue
                    self.log.warning(
                        "MeshCore health check failed %d times, reconnecting",
                        consecutive_health_failures,
                    )
                    with self._lock:
                        self._health_consecutive_failures = 0
                    self._disconnect_device()
                    self.event_bus.publish(
                        events.MESHCORE_DISCONNECTED,
                        {
                            "reason": "health_check_failed",
                        },
                    )
                    continue

            # Periodic advertisement
            if self._connected and self._advert_interval > 0:
                now = time.time()
                if now - self._last_advert_time >= self._advert_interval:
                    self._send_periodic_advert()

            # Periodic contact refresh
            if self._connected and self._contact_refresh_interval > 0:
                now = time.time()
                if now - self._last_contact_refresh >= self._contact_refresh_interval:
                    self._refresh_contacts()
                    self._last_contact_refresh = now

    def _connect_device(self) -> None:
        """Open connection to the MeshCore companion radio."""
        from meshcore import MeshCore
        from meshcore.events import EventType

        serial_port = self.config.get("serial_port", "/dev/meshcore")
        baudrate = self.config.get("baudrate", 115200)

        self.log.info("Connecting to MeshCore device (port=%s)...", serial_port)

        # Resolve and exclusively own the physical endpoint before every open
        # or reconnect attempt.  Registry conflicts and missing devices fail
        self._ensure_serial_device_lease(serial_port)

        generation = self._begin_connection_generation()

        mc = self._run_tracked_open(
            MeshCore.create_serial(
                serial_port,
                baudrate,
                auto_reconnect=False,
            ),
            generation,
            30,
        )
        if mc is None:
            raise ConnectionError(
                f"MeshCore device on {serial_port} did not respond — "
                "is it flashed with Companion Radio firmware?"
            )

        # _run_tracked_open publishes the disconnected candidate before it
        # returns, so shutdown and retry paths can always find the exact handle.
        self._require_current_connection(mc, generation)

        # Sync time
        self._run_async(mc.commands.set_time(int(time.time())))
        self._require_current_connection(mc, generation)

        # Query the local radio rather than trusting the client's cached
        # connection flag.  A missing initial response is allowed to enter
        # the normal hysteresis path so hotplug/retry semantics stay intact.
        result = self._run_async(
            mc.commands.send_device_query(),
            timeout=self._health_query_timeout,
        )
        self._require_current_connection(mc, generation)
        if not self._record_device_response(result):
            self.log.warning("MeshCore initial device query returned no usable response")

        # Fetch contacts
        self._run_async(mc.commands.get_contacts())
        self._require_current_connection(mc, generation)
        self._sync_contact_cache(mc)

        # Subscribe to incoming messages
        async def _on_contact_msg(event):
            if not self._connection_is_current(mc, generation, require_connected=True):
                return
            self._handle_incoming_message(event, msg_type="direct")

        async def _on_channel_msg(event):
            if not self._connection_is_current(mc, generation, require_connected=True):
                return
            self._handle_incoming_message(event, msg_type="broadcast")

        async def _on_disconnect(event):
            with self._lock:
                if (
                    not self._active
                    or self._connection_generation != generation
                    or self._mc is not mc
                ):
                    return
                # A disconnect during setup must fence the setup thread before
                # it can publish this client as connected.
                self._connection_generation += 1
                self._connected = False
            self.log.warning("MeshCore device disconnected")
            self.event_bus.publish(
                events.MESHCORE_DISCONNECTED,
                {
                    "reason": "device_disconnected",
                },
            )

        async def _on_ack(event):
            if not self._connection_is_current(mc, generation, require_connected=True):
                return
            self._handle_ack_event(event)

        async def _on_new_contact(event):
            if not self._connection_is_current(mc, generation, require_connected=True):
                return
            self._handle_new_contact(event)

        for event_type, callback in (
            (EventType.CONTACT_MSG_RECV, _on_contact_msg),
            (EventType.CHANNEL_MSG_RECV, _on_channel_msg),
            (EventType.DISCONNECTED, _on_disconnect),
            (EventType.ACK, _on_ack),
            (EventType.NEW_CONTACT, _on_new_contact),
        ):
            self._require_current_connection(mc, generation)
            sub = mc.subscribe(event_type, callback)
            with self._lock:
                if self._mc is mc:
                    self._subscriptions.append(sub)
                current = (
                    self._active and self._connection_generation == generation and self._mc is mc
                )
            if not current:
                raise RuntimeError("MeshCore connection became stale while subscribing")

        # Start auto-fetching queued messages
        auto_sub = self._run_async(mc.start_auto_message_fetching())
        self._require_current_connection(mc, generation)
        with self._lock:
            if self._mc is mc:
                self._subscriptions.append(auto_sub)
            current = self._active and self._connection_generation == generation and self._mc is mc
        if not current:
            raise RuntimeError("MeshCore connection became stale while enabling message fetch")

        with self._lock:
            if not self._active or self._connection_generation != generation or self._mc is not mc:
                raise RuntimeError("MeshCore connection became stale before commit")
            self._connected = True
            self._connect_count += 1
            self._health_consecutive_failures = 0
            self._serial_reopen_blocked = False

        # Send initial advertisement so other MeshCore nodes can discover us
        try:
            self._require_current_connection(mc, generation)
            self._run_async(mc.commands.send_advert(), timeout=10)
            self._require_current_connection(mc, generation)
            self._last_advert_time = time.time()
            self.log.info("MeshCore initial advertisement sent")
        except Exception:
            if self._connection_is_current(mc, generation):
                self.log.warning("Failed to send initial MeshCore advertisement", exc_info=True)

        if not self._connection_is_current(mc, generation, require_connected=True):
            return

        fw = self._device_info.get("ver", "unknown")
        model = self._device_info.get("model", "unknown")
        self.log.info("MeshCore connected: %s %s", model, fw)
        self.event_bus.publish(
            events.MESHCORE_CONNECTED,
            {
                "firmware": fw,
                "model": model,
                "serial_port": serial_port,
            },
        )

    def _disconnect_device(self) -> bool:
        """Close the current client, retaining it when teardown is uncertain."""
        with self._disconnect_lock:
            self._invalidate_connection_generation()
            with self._lock:
                mc = self._mc
                subs = list(self._subscriptions)
                self._connected = False
                open_pending = self._open_attempt is not None
                teardown = self._teardown_attempt

            if mc is None:
                return not open_pending and teardown is None

            if teardown is not None and teardown.get("mc") is mc:
                done = teardown["done"]
                if not done.is_set():
                    done.wait(timeout=self._device_teardown_timeout)
                    return bool(done.is_set() and teardown.get("success"))
                with self._lock:
                    if self._teardown_attempt is teardown:
                        self._teardown_attempt = None

            done = threading.Event()
            teardown = {"mc": mc, "done": done, "success": False}
            with self._lock:
                self._teardown_attempt = teardown
                self._serial_reopen_blocked = True

            def _teardown_client() -> None:
                # unsubscribe() is synchronous in current meshcore releases and
                # has no SDK timeout.  Keep it in a managed worker so a wedged
                # callback registry cannot wedge stop() or permit a second open.
                for sub in subs:
                    try:
                        mc.unsubscribe(sub)
                    except Exception:
                        self.log.debug(
                            "Error unsubscribing MeshCore event handler",
                            exc_info=True,
                        )

                success = False
                try:
                    if self._loop and self._loop.is_running():
                        success = bool(
                            self._run_async(
                                self._async_disconnect_mc(mc),
                                timeout=self._device_teardown_timeout,
                            )
                        )
                    else:
                        self.log.debug(
                            "MeshCore async loop not running; cannot prove device disconnect"
                        )
                except Exception:
                    self.log.debug("Error disconnecting MeshCore", exc_info=True)

                with self._lock:
                    teardown["success"] = success
                    if self._mc is mc:
                        self._serial_reopen_blocked = not success
                        if success:
                            self._mc = None
                            self._subscriptions = []
                            self._last_device_response_monotonic = 0.0
                            self._last_device_response_time = None
                    if self._teardown_attempt is teardown:
                        self._teardown_attempt = None
                if success:
                    self._save_contact_cache()
                done.set()

            self._start_thread(_teardown_client, "meshcore-teardown")
            if not done.wait(timeout=self._device_teardown_timeout):
                return False
            return bool(teardown["success"])

    async def _async_disconnect_mc(self, mc: Any) -> bool:
        """Async helper — disconnect a specific MeshCore instance."""
        success = True
        try:
            await mc.stop_auto_message_fetching()
        except Exception:
            success = False
            self.log.debug(
                "Error stopping MeshCore auto-fetch (mc)",
                exc_info=True,
            )
        try:
            await mc.disconnect()
        except Exception:
            success = False
            self.log.debug("Error during MeshCore disconnect (mc)", exc_info=True)
        return success

    @staticmethod
    def _validate_device_info_payload(payload: Any) -> tuple[bool, dict[str, Any]]:
        """Validate a DEVICE_INFO payload independent of SDK result shape."""
        if not isinstance(payload, dict) or not payload:
            return False, {}
        metadata = dict(payload)
        normalized = {str(key).strip().lower(): value for key, value in metadata.items()}
        if {"error", "err", "reason", "timeout", "timed_out", "busy"} & normalized.keys():
            return False, {}
        for key in ("status", "state"):
            value = normalized.get(key)
            if isinstance(value, str) and any(
                marker in value.strip().lower()
                for marker in ("error", "fail", "timeout", "timed out", "busy")
            ):
                return False, {}
        device_info_keys = {
            "fw ver",
            "ver",
            "version",
            "firmware",
            "firmware_version",
            "fw_ver",
            "model",
            "board",
            "device_id",
            "public_key",
            "radio_freq",
            "radio_bw",
            "radio_sf",
            "radio_cr",
            "max_tx_power",
        }
        has_device_info = any(
            key in normalized and normalized[key] not in (None, "", [], {})
            for key in device_info_keys
        )
        return has_device_info, metadata if has_device_info else {}

    @staticmethod
    def _parse_device_query_response(result: Any) -> tuple[bool, dict[str, Any]]:
        """Return whether *result* proves a local response and its metadata.

        MeshCore releases expose command responses as event objects, while
        lightweight integrations and tests sometimes return the payload
        mapping directly.  Accept both without depending on a particular
        EventType enum identity, but reject explicit command errors.
        """
        if result is None:
            return False, {}

        event_type = getattr(result, "type", None)
        event_name = str(getattr(event_type, "name", "")).lower()
        event_value = str(getattr(event_type, "value", "")).lower()
        event_label = str(event_type).lower()
        if isinstance(result, dict):
            return MeshCoreGateway._validate_device_info_payload(result)
        if not hasattr(result, "payload"):
            return False, {}
        payload = getattr(result, "payload", None)
        if (
            event_name != "device_info"
            and event_value != "device_info"
            and event_label != "device_info"
        ):
            return False, {}
        return MeshCoreGateway._validate_device_info_payload(payload)

    def _record_device_response(self, result: Any) -> bool:
        """Record a successful local device response and refresh metadata."""
        usable, payload = self._parse_device_query_response(result)
        if not usable:
            return False
        now_monotonic = time.monotonic()
        now_wall = time.time()
        with self._lock:
            self._last_device_response_monotonic = now_monotonic
            self._last_device_response_time = now_wall
            if payload:
                self._device_info.update(payload)
        return True

    def _device_response_age(self, now: float | None = None) -> float | None:
        """Return seconds since the most recent proven local response."""
        with self._lock:
            last_response = self._last_device_response_monotonic
        if last_response <= 0:
            return None
        current = time.monotonic() if now is None else now
        return max(0.0, current - last_response)

    def _recent_device_response_is_acceptable(self) -> bool:
        age = self._device_response_age()
        return (
            age is not None
            and self._health_response_max_age > 0
            and age <= self._health_response_max_age
        )

    def _check_health(self) -> bool:
        """Query the local companion with a bounded response-age fallback."""
        with self._lock:
            mc = self._mc
            connected = self._connected
            generation = self._connection_generation
        if mc is None or not connected:
            return False

        try:
            result = self._run_async(
                mc.commands.send_device_query(),
                timeout=self._health_query_timeout,
            )
        except Exception as exc:
            with self._lock:
                self._health_query_failures += 1
            if self._recent_device_response_is_acceptable():
                self.log.debug(
                    "MeshCore health query failed; accepting recent local response",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                return True
            self.log.debug(
                "MeshCore health query failed with no recent local response",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return False

        if not self._connection_is_current(mc, generation, require_connected=True):
            return False

        if self._record_device_response(result):
            return True

        with self._lock:
            self._health_query_failures += 1
        if self._recent_device_response_is_acceptable():
            self.log.debug("MeshCore health query returned no data; using recent response")
            return True
        self.log.debug("MeshCore health query returned no usable local response")
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

    def _handle_new_contact(self, event: Any) -> None:
        """Process a NEW_CONTACT push from the device (new peer advert)."""
        try:
            payload = event.payload if hasattr(event, "payload") else event
            if not isinstance(payload, dict):
                return
            key = payload.get("public_key", "")
            if not key:
                return
            name = payload.get("adv_name", "")
            with self._lock:
                self._contact_cache[key] = dict(payload)
            self._save_contact_cache()
            self.log.info("New MeshCore peer: %s (%s…)", name or "unnamed", key[:12])
        except Exception:
            self.log.debug("Error handling new contact event", exc_info=True)

    def _refresh_contacts(self) -> None:
        """Re-fetch the contact list from the device."""
        with self._lock:
            mc = self._mc
        if mc is None:
            return
        try:
            self._run_async(mc.commands.get_contacts())
            self._sync_contact_cache(mc)
        except Exception:
            self.log.debug("Error refreshing contacts", exc_info=True)

    # ── Message handling ────────────────────────────────────────────

    def _resolve_pubkey_prefix(self, prefix: str) -> tuple[str, str]:
        """Resolve a pubkey_prefix to (full_key, adv_name) from contact cache.

        Returns the first contact whose key starts with *prefix*.
        Falls back to (prefix, "") if no match.
        """
        with self._lock:
            items = list(self._contact_cache.items())
        for key, contact in items:
            if key.startswith(prefix):
                return key, contact.get("adv_name", "")
        return prefix, ""

    def _resolve_name_to_key(self, name: str) -> str:
        """Look up a contact's full pubkey by adv_name.  Returns "" if unknown."""
        if not name:
            return ""
        with self._lock:
            items = list(self._contact_cache.items())
        for key, contact in items:
            if contact.get("adv_name", "") == name:
                return key
        return ""

    @staticmethod
    def _split_channel_sender(text: str) -> tuple[str, str]:
        """Split a channel message into (sender_name, body).

        MeshCore channel (broadcast) packets carry no pubkey, so the sending
        client prepends its advertised name as ``"<name>: <body>"``.  If the
        format is not present (or the prefix is implausibly long / contains a
        newline), fall back to ``("", text)``.
        """
        if not text:
            return "", text
        idx = text.find(": ")
        if idx <= 0 or idx > 40:
            return "", text
        prefix = text[:idx]
        # Reject prefixes that span lines or look like URL-ish content
        if "\n" in prefix or "/" in prefix:
            return "", text
        return prefix, text[idx + 2 :]

    def _handle_incoming_message(
        self,
        event: Any,
        msg_type: str = "direct",
    ) -> None:
        """Process an incoming MeshCore message event."""
        try:
            payload = event.payload if isinstance(event.payload, dict) else {}
            text = payload.get(
                "text", str(event.payload) if not isinstance(event.payload, dict) else ""
            )
            channel = payload.get("channel_idx")

            # Direct messages carry a pubkey_prefix; channel messages don't —
            # the sender name is prepended to the text as "<name>: <body>".
            pubkey_prefix = payload.get("pubkey_prefix", "")
            if pubkey_prefix:
                from_key, from_name = self._resolve_pubkey_prefix(pubkey_prefix)
            else:
                from_key = ""
                from_name = ""
                if msg_type == "broadcast":
                    parsed_name, parsed_body = self._split_channel_sender(text)
                    if parsed_name:
                        resolved_key = self._resolve_name_to_key(parsed_name)
                        if resolved_key:
                            from_name = parsed_name
                            from_key = resolved_key
                            text = parsed_body

            if not text.strip():
                return

            # Fuzzy dedup: drop repeats of the same (sender, type, body)
            # within the configured TTL. Missing pubkey means broadcast —
            # key on the stripped-name payload instead so the same
            # channel post relayed twice still collapses.
            dedup_key = (
                (from_key or "").lower(),
                msg_type,
                hash(text),
            )
            now = time.time()
            cutoff = now - self._dedup_ttl_seconds
            with self._lock:
                prior = self._seen_msg_keys.get(dedup_key)
                if prior is not None and prior > cutoff:
                    self.log.debug(
                        "Dropping duplicate MeshCore %s from %s",
                        msg_type,
                        from_key[:12] or "anon",
                    )
                    return
                self._seen_msg_keys[dedup_key] = now
                self._seen_msg_inserts_since_cleanup += 1
                # Amortized cleanup: drop TTL-expired entries, then trim
                # oldest if still over the absolute cap.
                if (
                    self._seen_msg_inserts_since_cleanup >= self._seen_msg_cleanup_interval
                    or len(self._seen_msg_keys) > self._dedup_max_entries
                ):
                    self._seen_msg_inserts_since_cleanup = 0
                    self._seen_msg_keys = {
                        k: v for k, v in self._seen_msg_keys.items() if v > cutoff
                    }
                    if len(self._seen_msg_keys) > self._dedup_max_entries:
                        # Hard cap — keep newest N by timestamp
                        sorted_items = sorted(
                            self._seen_msg_keys.items(),
                            key=lambda kv: kv[1],
                            reverse=True,
                        )
                        self._seen_msg_keys = dict(sorted_items[: self._dedup_max_entries])

            from_label = (
                f"{from_name} ({from_key[:12]})" if from_name else (from_key[:12] or "unknown")
            )

            if msg_type == "broadcast":
                self.log.info(
                    "MeshCore channel msg (ch%s) from %s: %s",
                    channel,
                    from_label,
                    text[:80],
                )
            else:
                self.log.info(
                    "MeshCore direct msg from %s: %s",
                    from_label,
                    text[:80],
                )

            with self._lock:
                self._msgs_received += 1
                self._last_msg_time = time.time()

            path_len = payload.get("path_len")
            if path_len is None and from_key:
                with self._lock:
                    cached = self._contact_cache.get(from_key)
                if cached:
                    opl = cached.get("out_path_len")
                    if opl is not None and opl >= 0:
                        path_len = opl

            self.event_bus.publish(
                events.MESHCORE_MESSAGE_RECEIVED,
                {
                    "from_key": from_key,
                    "from_name": from_name,
                    "text": text[:500],
                    "msg_type": msg_type,
                    "channel": channel,
                    "path_len": path_len,
                },
            )

        except Exception:
            self.log.exception("Error handling incoming MeshCore message")

    def _handle_ack_event(self, event: Any) -> None:
        """Process an incoming MeshCore ACK event and publish on the event bus."""
        try:
            ack_code: Any = ""
            if hasattr(event, "attributes") and isinstance(event.attributes, dict):
                ack_code = event.attributes.get("code", "")
            if not ack_code and isinstance(event.payload, dict):
                ack_code = event.payload.get("code", "")
            # Normalize to hex string to match send_message()'s expected_ack
            if isinstance(ack_code, bytes):
                ack_code = ack_code.hex()
            elif ack_code:
                ack_code = str(ack_code)
            if ack_code:
                self.log.debug("MeshCore ACK received: %s", ack_code)
                self.event_bus.publish(
                    events.MESHCORE_MESSAGE_ACKED,
                    {
                        "ack_code": ack_code,
                    },
                )
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
            snapshot = {key: dict(contact) for key, contact in contacts.items()}
            with self._lock:
                self._contact_cache.update(snapshot)
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
                with self._lock:
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
        with self._lock:
            snapshot = dict(self._contact_cache)
        try:
            with open(self._cache_path, "w") as f:
                json.dump(snapshot, f)
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

        try:
            # Validate and enter the SDK while holding the same lock used by
            # disconnect/reconnect generation changes. The async operation runs
            # without the lock, so its callbacks remain free to fence this send.
            with self._lock:
                mc = self._mc
                generation = self._connection_generation
                if not self._active or not self._connected or mc is None:
                    return {"sent": False, "reason": "not_connected"}
                if destination:
                    operation = mc.commands.send_msg(destination, text)
                else:
                    ch = channel if channel is not None else 0
                    operation = mc.commands.send_chan_msg(ch, text)
            result = self._run_async(operation, timeout=10)
        except (ConnectionError, TimeoutError, AttributeError):
            if not self._connection_is_current(mc, generation, require_connected=True):
                self.log.warning("MeshCore delivery is uncertain after connection changed")
                return {"sent": False, "reason": _DELIVERY_UNCERTAIN_REASON}
            self.log.warning("MeshCore device disconnected during send")
            return {"sent": False, "reason": "device_disconnected_during_send"}
        except Exception as exc:
            if not self._connection_is_current(mc, generation, require_connected=True):
                self.log.warning("MeshCore delivery is uncertain after connection changed")
                return {"sent": False, "reason": _DELIVERY_UNCERTAIN_REASON}
            self.log.exception("Error sending MeshCore message")
            return {"sent": False, "reason": str(exc)}

        # A completed SDK call on an old client cannot prove whether the radio
        # transmitted before disconnect/reconnect or shutdown fenced it. Never
        # retry that ambiguous operation and never report it as sent.
        if not self._connection_is_current(mc, generation, require_connected=True):
            self.log.warning("MeshCore delivery is uncertain after connection changed")
            return {"sent": False, "reason": _DELIVERY_UNCERTAIN_REASON}

        from meshcore.events import EventType

        if result and result.type == EventType.ERROR:
            reason = (
                result.payload.get("reason", "unknown error")
                if isinstance(result.payload, dict)
                else str(result.payload)
            )
            return {"sent": False, "reason": reason}

        dest_label = destination[:12] if destination else f"channel {channel or 0}"

        # Extract ack tracking info for direct messages
        expected_ack = None
        suggested_timeout = None
        if destination and result and isinstance(result.payload, dict):
            raw_ack = result.payload.get("expected_ack")
            if raw_ack is not None:
                expected_ack = raw_ack.hex() if isinstance(raw_ack, bytes) else str(raw_ack)
            suggested_timeout = result.payload.get("suggested_timeout")

        # Commit statistics only while the exact client and generation captured
        # before SDK entry remain the published connected owner.
        with self._lock:
            if (
                not self._active
                or self._connection_generation != generation
                or self._mc is not mc
                or not self._connected
            ):
                self.log.warning("MeshCore delivery is uncertain after connection changed")
                return {"sent": False, "reason": _DELIVERY_UNCERTAIN_REASON}
            self._msgs_sent += 1

        self.log.info("Sent MeshCore message to %s", dest_label)

        self.event_bus.publish(
            events.MESHCORE_MESSAGE_SENT,
            {
                "text": text[:100],
                "destination": dest_label,
                "expected_ack": expected_ack,
            },
        )
        return {
            "sent": True,
            "expected_ack": expected_ack,
            "suggested_timeout": suggested_timeout,
        }

    # ── Public query methods ────────────────────────────────────────

    def get_device_handle(self) -> Any:
        """Return the live MeshCore client (or None) for peer plugins.

        The returned handle is owned by this plugin; callers may read state
        and subscribe to events but MUST NOT disconnect it or stop its loop.
        """
        with self._lock:
            return self._mc

    def get_async_loop(self) -> Any:
        """Return the asyncio loop driving the MeshCore client (or None).

        Callers may schedule coroutines on the loop but MUST NOT stop it —
        the loop is owned by this plugin.
        """
        return self._loop

    def get_status(self) -> dict[str, Any]:
        """Return current gateway status for monitoring and API."""
        response_age = self._device_response_age()
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
                "serial_reopen_blocked": self._serial_reopen_blocked,
                "health_query_failures": self._health_query_failures,
                "health_consecutive_failures": self._health_consecutive_failures,
                "health_failure_threshold": self._health_failure_threshold,
                "last_device_response_time": self._last_device_response_time,
                "device_response_age_seconds": (
                    round(response_age, 1) if response_age is not None else None
                ),
                "last_msg_time": self._last_msg_time,
                "contacts": len(self._contact_cache),
            }

    def broadcast_snapshot(self, cycle_count: int = 0) -> dict | None:
        now = time.monotonic()
        cached = self._broadcast_cache
        if cached is not None and (now - cached[0]) < self._broadcast_cache_ttl:
            return cached[1]
        result: dict[str, Any] = {}
        s = self.get_status()
        if s:
            result["meshcore_status"] = s
        d = self.get_device_info()
        if d:
            result["meshcore_device"] = d
        c = self.get_contacts()
        if c is not None:
            result["meshcore_contacts"] = c
        if result:
            self._broadcast_cache = (now, result)
        return result or None

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
                with self._lock:
                    for key, contact in contacts_dict.items():
                        self._contact_cache[key] = dict(contact)
            except Exception:
                self.log.debug("Error reading live contacts", exc_info=True)
                with self._lock:
                    contacts_dict = dict(self._contact_cache)
        else:
            with self._lock:
                contacts_dict = dict(self._contact_cache)

        # Filter stale contacts (0 = show all)
        now = time.time()
        max_age_days = self.config.get("stale_contact_days", 30)
        cutoff = (now - max_age_days * 86400) if max_age_days > 0 else 0
        # Some devices send adverts with badly-skewed RTCs, producing
        # last_advert values hours/days ahead of host time. Treat anything
        # >60 s in the future as missing so consumers don't render "just now"
        # forever.
        future_cutoff = now + 60

        result = []
        for key, contact in contacts_dict.items():
            last_advert = contact.get("last_advert", 0)
            if last_advert and last_advert > future_cutoff:
                last_advert = 0
            if cutoff and last_advert and last_advert < cutoff:
                continue
            result.append(
                {
                    "public_key": key,
                    "name": contact.get("adv_name", ""),
                    "type": contact.get("type", 0),
                    "last_advert": last_advert,
                    "latitude": contact.get("adv_lat", 0),
                    "longitude": contact.get("adv_lon", 0),
                    "flags": contact.get("flags", 0),
                    "out_path_len": contact.get("out_path_len", -1),
                }
            )
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
