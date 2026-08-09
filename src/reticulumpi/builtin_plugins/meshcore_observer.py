"""MeshCore Observer plugin — companion observer for letsmesh.net analyzer.

Connects to a MeshCore companion radio, captures every RF packet the radio
hears via RX_LOG_DATA events, and publishes them to the letsmesh.net MQTT
broker (mqtt-us-v1.letsmesh.net:443) over WebSocket+TLS with Ed25519 JWT
authentication.

Two device modes:
  standalone — connects to its own MeshCore radio via serial/TCP.
  shared     — borrows the mc instance from the meshcore_gateway plugin.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import queue
import threading
import time
from datetime import datetime, timezone
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

_JWT_LIFETIME = 3600
_JWT_REFRESH_BUFFER = 300

_PAHO_WS_PATCHED = False


def _patch_paho_websocket() -> None:
    """Fix paho-mqtt 2.x unmasked PONG/CONNCLOSE frames and add WS ping.

    paho's _WebsocketWrapper responds to server PINGs with do_masking=0,
    violating RFC 6455 §5.1.  Cloudflare drops unmasked frames, which
    kills the proxy path and prevents MQTT PINGRESP from arriving.
    """
    global _PAHO_WS_PATCHED
    if _PAHO_WS_PATCHED:
        return

    from paho.mqtt.client import _WebsocketWrapper

    _orig_create_frame = _WebsocketWrapper._create_frame

    def _masked_create_frame(self, opcode, data, do_masking=1):
        return _orig_create_frame(self, opcode, data, do_masking=1)

    _WebsocketWrapper._create_frame = _masked_create_frame

    def ping(self):
        frame = _orig_create_frame(
            self,
            _WebsocketWrapper.OPCODE_PING,
            bytearray(),
            do_masking=1,
        )
        self._socket.send(frame)

    _WebsocketWrapper.ping = ping
    _PAHO_WS_PATCHED = True


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class MeshCoreObserver(PluginBase):
    """Companion observer for the letsmesh.net MeshCore analyzer."""

    plugin_name = "meshcore_observer"
    plugin_description = "MeshCore companion observer for letsmesh.net analyzer"
    plugin_version = "1.0.3"
    broadcast_tier = 1
    broadcast_keys = "meshcore_observer"
    plugin_dependencies = ("meshcore_gateway",)

    # ── Configuration validation ────────────────────────────────────

    def validate_config(self) -> None:
        try:
            import meshcore  # noqa: F401
        except ImportError:
            raise ValueError(
                "meshcore package not found. Install with: pip install reticulumpi[meshcore]"
            )

        try:
            import paho.mqtt.client  # noqa: F401
        except ImportError:
            raise ValueError("paho-mqtt package not found. Install with: pip install paho-mqtt")

        shared = self.config.get("use_gateway_device", False)
        if not shared:
            conn = self.config.get("connection_type", "serial")
            if conn not in ("serial", "tcp"):
                raise ValueError("connection_type must be 'serial' or 'tcp'")
            if conn == "serial":
                port = self.config.get("serial_port", "/dev/meshcore-observer")
                validate_stable_serial_path(port)
            else:
                host = self.config.get("tcp_host")
                if not host:
                    raise ValueError("tcp_host is required for tcp connection")
                tcp_port = self.config.get("tcp_port", 5000)
                if not isinstance(tcp_port, int) or tcp_port <= 0:
                    raise ValueError("tcp_port must be a positive integer")

        iata = self.config.get("iata", "XXX")
        if not isinstance(iata, str) or len(iata) != 3:
            raise ValueError("iata must be a 3-character string")

        baud = self.config.get("serial_baud", 115200)
        if not isinstance(baud, int) or baud <= 0:
            raise ValueError("serial_baud must be a positive integer")

        hci = self.config.get("health_check_interval", 30)
        if not isinstance(hci, (int, float)) or isinstance(hci, bool) or hci < 5:
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

        ws_ping = self.config.get("ws_ping_interval", 30)
        if not isinstance(ws_ping, (int, float)) or ws_ping < 10:
            raise ValueError("ws_ping_interval must be >= 10 seconds")

    # ── Lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        _patch_paho_websocket()

        self._lock = threading.Lock()
        self._disconnect_lock = threading.Lock()
        self._mqtt_lifecycle_lock = threading.RLock()
        self._serial_device_lease: SerialDeviceLease | None = None
        self._serial_reopen_blocked = False
        self._connection_generation = 0
        self._open_attempt: dict[str, Any] | None = None
        self._teardown_attempt: dict[str, Any] | None = None
        self._device_teardown_timeout = 5.0
        self._shared_attach_in_progress = False
        self._gateway_handlers_subscribed = False
        self._start_monotonic = time.monotonic()

        # Stats
        self._packets_captured = 0
        self._packets_published = 0
        self._packets_failed = 0
        self._connect_count = 0
        self._reconnect_failures = 0
        self._last_packet_time: float | None = None
        self._last_mqtt_publish_time: float | None = None

        # Device state
        self._mc: Any = None
        self._connected_device = False
        self._device_info: dict[str, Any] = {}
        self._public_key = ""
        self._subscriptions: list[Any] = []
        self._shared_mode = bool(self.config.get("use_gateway_device", False))
        health_check_interval = float(self.config.get("health_check_interval", 30))
        self._health_query_timeout = float(self.config.get("health_query_timeout", 5))
        self._health_response_max_age = float(
            self.config.get(
                "health_response_max_age",
                max(health_check_interval * 2, self._health_query_timeout),
            )
        )
        self._health_query_failures = 0
        self._last_device_response_monotonic = 0.0
        self._last_device_response_time: float | None = None

        # MQTT state
        self._mqtt_client: Any = None
        self._mqtt_generation = 0
        self._connected_mqtt = False
        self._ws_ping_stop: threading.Event = threading.Event()
        self._ws_ping_thread: threading.Thread | None = None

        # JWT state
        self._jwt_token: str | None = None
        self._jwt_expires: float = 0
        self._signing_mode = "unknown"

        # Packet queue (device callback → MQTT publish thread)
        max_queue = self.config.get("packet_queue_size", 1000)
        self._packet_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_queue)

        # Dedicated asyncio event loop for meshcore (standalone mode only)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()

        self._active = True

        if self._shared_mode:
            # Install lifecycle handlers before the watcher starts so stop()
            # cannot race a late worker-side registration.
            self.event_bus.subscribe(
                events.MESHCORE_CONNECTED,
                self._on_gateway_connected,
            )
            self.event_bus.subscribe(
                events.MESHCORE_DISCONNECTED,
                self._on_gateway_disconnected,
            )
            self.event_bus.subscribe(
                events.PLUGIN_STOPPING,
                self._on_plugin_stopping,
            )
            self._gateway_handlers_subscribed = True
            self._start_thread(self._gateway_watcher_loop, "observer-gateway-watcher")
        else:
            self._start_thread(self._run_async_loop, "observer-async-loop")
            self._loop_ready.wait(timeout=10)
            self._start_thread(self._device_connection_loop, "observer-device")

        self._start_thread(self._mqtt_connection_loop, "observer-mqtt")

        mode = "shared" if self._shared_mode else "standalone"
        self.log.info("MeshCore Observer started (mode=%s)", mode)

    def stop(self) -> None:
        self._active = False
        disconnect_proven = self._shared_mode
        try:
            if self._shared_mode and self._gateway_handlers_subscribed:
                try:
                    self.event_bus.unsubscribe_all(self._on_gateway_connected)
                    self.event_bus.unsubscribe_all(self._on_gateway_disconnected)
                    self.event_bus.unsubscribe_all(self._on_plugin_stopping)
                    self._gateway_handlers_subscribed = False
                except Exception:
                    self.log.debug(
                        "Error unsubscribing gateway handlers",
                        exc_info=True,
                    )

            if not self._shared_mode:
                try:
                    disconnect_proven = self._disconnect_device()
                except Exception:
                    disconnect_proven = False
                    self.log.exception(
                        "Unexpected error while closing MeshCore Observer during shutdown"
                    )
            else:
                # The gateway owns the physical device and its async loop.
                # Shared mode only removes this observer's subscriptions.
                self._detach_from_gateway()

            self._disconnect_mqtt()

            if not self._shared_mode and self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)

            self._join_threads()
        finally:
            # Standalone serial ownership spans disconnects and reconnect
            # backoff. A timed join or swallowed SDK disconnect error is not
            # proof that the endpoint is closed, so fail closed and retain the
            # claim until process exit whenever shutdown remains uncertain.
            if not self._shared_mode and self.config.get("connection_type", "serial") == "serial":
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
                        "MeshCore Observer shutdown was not proven quiescent; retaining "
                        "serial-device ownership until process exit (live threads: %s)",
                        ", ".join(live_threads) or "none",
                    )

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._active,
                "mode": "shared" if self._shared_mode else "standalone",
                "device_connected": self._connected_device,
                "mqtt_connected": self._connected_mqtt,
                "public_key": self._public_key,
                "iata": self.config.get("iata", "XXX"),
                "mqtt_broker": self.config.get("mqtt_broker", "mqtt-us-v1.letsmesh.net"),
                "packets_captured": self._packets_captured,
                "packets_published": self._packets_published,
                "packets_failed": self._packets_failed,
                "last_packet_time": self._last_packet_time,
                "last_mqtt_publish_time": self._last_mqtt_publish_time,
                "connect_count": self._connect_count,
                "reconnect_failures": self._reconnect_failures,
                "serial_reopen_blocked": self._serial_reopen_blocked,
                "health_query_failures": self._health_query_failures,
                "last_device_response_time": self._last_device_response_time,
                "signing_mode": self._signing_mode,
                "firmware": self._device_info.get("ver"),
                "model": self._device_info.get("model"),
                "queue_depth": self._packet_queue.qsize(),
                "ws_ping_active": (
                    self._ws_ping_thread is not None and self._ws_ping_thread.is_alive()
                ),
            }

    def broadcast_snapshot(self, cycle_count: int = 0) -> dict | None:
        s = self.get_status()
        return {"available": True, **s} if s else None

    # ── Asyncio event loop (standalone mode) ───────────────────────

    def _run_async_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

    def _run_async(self, coro: Any, timeout: float = 15) -> Any:
        if not self._loop or not self._loop.is_running():
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            raise RuntimeError("MeshCore async loop not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise

    @staticmethod
    def _close_awaitable(awaitable: Any) -> None:
        """Close an unscheduled coroutine without assuming its concrete type."""
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()

    def _begin_connection_generation(self) -> int:
        """Reserve a connection generation only after prior work is quiescent."""
        with self._lock:
            if (
                self._mc is not None
                or self._open_attempt is not None
                or self._teardown_attempt is not None
            ):
                self._serial_reopen_blocked = True
                raise RuntimeError("previous MeshCore Observer connection work is not quiescent")
            self._connection_generation += 1
            return self._connection_generation

    def _connection_is_current(
        self,
        mc: Any,
        generation: int,
        *,
        require_connected: bool = False,
    ) -> bool:
        with self._lock:
            return (
                self._active
                and self._connection_generation == generation
                and self._mc is mc
                and (self._connected_device or not require_connected)
            )

    def _require_current_connection(self, mc: Any, generation: int) -> None:
        if not self._connection_is_current(mc, generation):
            raise RuntimeError("MeshCore Observer connection became stale during initialization")

    def _invalidate_connection_generation(self) -> None:
        """Fence callbacks/setup and cancel an in-flight standalone open."""
        future = None
        with self._lock:
            self._connection_generation += 1
            self._connected_device = False
            attempt = self._open_attempt
            if attempt is not None:
                attempt["abandoned"] = True
                self._serial_reopen_blocked = True
                future = attempt.get("future")
        if future is not None:
            future.cancel()

    async def _async_close_unpublished_mc(self, mc: Any) -> bool:
        try:
            await mc.disconnect()
        except BaseException:
            self.log.debug("Error closing late MeshCore Observer client", exc_info=True)
            return False
        return True

    def _run_tracked_open(self, coro: Any, generation: int, timeout: float) -> Any:
        """Run create_serial/create_tcp while retaining every late result."""
        loop = self._loop
        if loop is None or not loop.is_running():
            mc = self._run_async(coro, timeout=timeout)
            with self._lock:
                if mc is not None:
                    self._mc = mc
                    self._connected_device = False
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
                raise RuntimeError("MeshCore Observer open generation is no longer current")
            self._open_attempt = attempt
            self._serial_reopen_blocked = True

        async def _capture_open_result() -> None:
            mc = None
            error: BaseException | None = None
            try:
                mc = await coro
            except BaseException as exc:
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
                        self._mc = mc
                        self._connected_device = False
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
                        if self._mc is None:
                            self._mc = mc
                            self._connected_device = False
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
                raise TimeoutError("MeshCore Observer create operation timed out")

        error = attempt["error"]
        if error is not None:
            if isinstance(error, asyncio.CancelledError):
                raise RuntimeError("MeshCore Observer create operation was cancelled")
            raise error
        return attempt["result"]

    def on_internet_available(self) -> None:
        self.log.info("Internet restored — MQTT reconnection enabled")

    def on_internet_lost(self) -> None:
        self.log.warning("Internet lost — MQTT reconnection paused, packets queued")

    # ── Standalone device connection ───────────────────────────────

    def _ensure_serial_device_lease(self, configured_path: str) -> SerialDeviceLease:
        """Claim or revalidate the standalone observer's serial endpoint."""
        with self._lock:
            lease = self._serial_device_lease
            if lease is not None:
                try:
                    lease.revalidate()
                    return lease
                except (SerialDeviceChangedError, StaleSerialDeviceLeaseError):
                    # USB re-enumeration can change the tty binding. Drop the
                    # stale claim before claiming the configured path's new
                    # physical identity.
                    lease.release()
                    self._serial_device_lease = None

            lease = serial_device_registry.claim(configured_path, self.plugin_name)
            self._serial_device_lease = lease
            try:
                # Narrow the claim/open race by resolving the path again
                # immediately before it is handed to MeshCore.
                lease.revalidate()
            except Exception:
                lease.release()
                self._serial_device_lease = None
                raise
            return lease

    def _release_serial_device_lease(self) -> None:
        """Release this observer's exact standalone serial claim, if any."""
        with self._lock:
            lease = self._serial_device_lease
            self._serial_device_lease = None
        if lease is not None:
            lease.release()

    def _device_connection_loop(self) -> None:
        reconnect_delay = self.config.get("reconnect_delay", 10)
        health_check_interval = self.config.get("health_check_interval", 30)
        max_attempts = self.config.get("max_reconnect_attempts", 0)
        health_failure_threshold = max(1, int(self.config.get("health_failure_threshold", 3)))
        consecutive_health_failures = 0

        while self._active:
            if not self._connected_device:
                with self._lock:
                    stale_mc = self._mc
                if stale_mc is not None and not self._disconnect_device():
                    self.log.warning(
                        "MeshCore Observer teardown is still uncertain; refusing a second open"
                    )
                    self._sleep_while_active(reconnect_delay)
                    continue
                try:
                    self._connect_device()
                    self._reconnect_failures = 0
                    consecutive_health_failures = 0
                except Exception as exc:
                    self._reconnect_failures += 1
                    self.log.warning(
                        "MeshCore observer connect failed (%d): %s",
                        self._reconnect_failures,
                        exc,
                    )
                    self.event_bus.publish(
                        events.MESHCORE_OBSERVER_CONNECT_FAILED,
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
                    self._sleep_while_active(backoff)
                    continue

            self._sleep_while_active(health_check_interval)
            if self._connected_device:
                if self._check_health():
                    if consecutive_health_failures > 0:
                        self.log.info(
                            "Observer health recovered after %d failed check(s)",
                            consecutive_health_failures,
                        )
                    consecutive_health_failures = 0
                else:
                    consecutive_health_failures += 1
                    if consecutive_health_failures < health_failure_threshold:
                        self.log.info(
                            "Observer health check failed (%d/%d) — will retry",
                            consecutive_health_failures,
                            health_failure_threshold,
                        )
                        continue
                    self.log.warning(
                        "Observer health check failed %d times, reconnecting",
                        consecutive_health_failures,
                    )
                    consecutive_health_failures = 0
                    self._disconnect_device()
                    self.event_bus.publish(
                        events.MESHCORE_OBSERVER_DEVICE_DISCONNECTED,
                        {"reason": "health_check_failed"},
                    )

    def _connect_device(self) -> None:
        from meshcore import MeshCore
        from meshcore.events import EventType

        conn_type = self.config.get("connection_type", "serial")
        generation: int
        if conn_type == "serial":
            serial_port = self.config.get("serial_port", "/dev/meshcore-observer")
            baudrate = self.config.get("serial_baud", 115200)
            self.log.info(
                "Connecting observer to MeshCore device (port=%s)...",
                serial_port,
            )
            # Shared mode never enters this path. A standalone observer owns
            # its physical endpoint exclusively before every open attempt and
            # keeps that lease throughout hotplug/reconnect backoff.
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
        else:
            host = self.config.get("tcp_host")
            port = self.config.get("tcp_port", 5000)
            self.log.info(
                "Connecting observer to MeshCore device (tcp=%s:%d)...",
                host,
                port,
            )
            generation = self._begin_connection_generation()
            mc = self._run_tracked_open(
                MeshCore.create_tcp(
                    host,
                    port,
                    auto_reconnect=True,
                    max_reconnect_attempts=3,
                ),
                generation,
                30,
            )

        if mc is None:
            raise ConnectionError("MeshCore device did not respond")

        self._require_current_connection(mc, generation)

        self._run_async(mc.commands.set_time(int(time.time())))
        self._require_current_connection(mc, generation)

        result = self._run_async(
            mc.commands.send_device_query(),
            timeout=self._health_query_timeout,
        )
        self._require_current_connection(mc, generation)
        if not self._record_device_response(result):
            self.log.warning("MeshCore Observer initial device query returned no usable response")

        result = self._run_async(mc.commands.send_appstart())
        self._require_current_connection(mc, generation)
        public_key = ""
        if mc.self_info:
            public_key = mc.self_info.get("public_key", "")
        if not public_key and result and hasattr(result, "payload"):
            public_key = result.payload.get("public_key", "")

        async def _on_rx_log(event):
            if not self._connection_is_current(mc, generation, require_connected=True):
                return
            self._handle_rx_log(event)

        async def _on_disconnect(event):
            with self._lock:
                if (
                    not self._active
                    or self._connection_generation != generation
                    or self._mc is not mc
                ):
                    return
                self._connection_generation += 1
                self._connected_device = False
            self.log.warning("MeshCore observer device disconnected")
            self.event_bus.publish(
                events.MESHCORE_OBSERVER_DEVICE_DISCONNECTED,
                {"reason": "device_disconnected"},
            )

        for event_type, callback in (
            (EventType.RX_LOG_DATA, _on_rx_log),
            (EventType.DISCONNECTED, _on_disconnect),
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
                raise RuntimeError("MeshCore Observer became stale while subscribing")

        with self._lock:
            if not self._active or self._connection_generation != generation or self._mc is not mc:
                raise RuntimeError("MeshCore Observer became stale before commit")
            self._connected_device = True
            self._connect_count += 1
            self._public_key = public_key.upper()
            self._serial_reopen_blocked = False

        self._probe_signing(mc)
        if not self._connection_is_current(mc, generation, require_connected=True):
            return

        fw = self._device_info.get("ver", "unknown")
        model = self._device_info.get("model", "unknown")
        self.log.info("MeshCore observer connected: %s %s (key=%s…)", model, fw, public_key[:12])
        self.event_bus.publish(
            events.MESHCORE_OBSERVER_DEVICE_CONNECTED,
            {
                "firmware": fw,
                "model": model,
                "public_key": public_key,
                "mode": "standalone",
            },
        )

    def _disconnect_device(self) -> bool:
        """Close the current client without permitting an uncertain reopen."""
        with self._disconnect_lock:
            self._invalidate_connection_generation()
            with self._lock:
                mc = self._mc
                subs = list(self._subscriptions)
                self._connected_device = False
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
                for sub in subs:
                    try:
                        mc.unsubscribe(sub)
                    except Exception:
                        self.log.debug("Error unsubscribing observer handler", exc_info=True)

                success = False
                try:
                    if self._loop and self._loop.is_running():
                        self._run_async(
                            mc.disconnect(),
                            timeout=self._device_teardown_timeout,
                        )
                        success = True
                    else:
                        self.log.debug(
                            "Observer async loop not running; cannot prove device disconnect"
                        )
                except Exception:
                    self.log.debug("Error disconnecting observer device", exc_info=True)

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
                done.set()

            self._start_thread(_teardown_client, "observer-device-teardown")
            if not done.wait(timeout=self._device_teardown_timeout):
                return False
            return bool(teardown["success"])

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
        """Return whether a command result proves a local companion response."""
        if result is None:
            return False, {}
        event_type = getattr(result, "type", None)
        event_name = str(getattr(event_type, "name", "")).lower()
        event_value = str(getattr(event_type, "value", "")).lower()
        event_label = str(event_type).lower()
        if isinstance(result, dict):
            return MeshCoreObserver._validate_device_info_payload(result)
        if not hasattr(result, "payload"):
            return False, {}
        payload = getattr(result, "payload", None)
        if (
            event_name != "device_info"
            and event_value != "device_info"
            and event_label != "device_info"
        ):
            return False, {}
        return MeshCoreObserver._validate_device_info_payload(payload)

    def _record_device_response(self, result: Any) -> bool:
        usable, payload = self._parse_device_query_response(result)
        if not usable:
            return False
        with self._lock:
            self._last_device_response_monotonic = time.monotonic()
            self._last_device_response_time = time.time()
            if payload:
                self._device_info.update(payload)
        return True

    def _recent_device_response_is_acceptable(self) -> bool:
        with self._lock:
            last_response = self._last_device_response_monotonic
        if last_response <= 0 or self._health_response_max_age <= 0:
            return False
        return time.monotonic() - last_response <= self._health_response_max_age

    def _check_health(self) -> bool:
        """Actively query the local companion instead of trusting cached state."""
        with self._lock:
            mc = self._mc
            connected = self._connected_device
            generation = self._connection_generation
        if mc is None or not connected:
            return False
        try:
            result = self._run_async(
                mc.commands.send_device_query(),
                timeout=self._health_query_timeout,
            )
        except Exception:
            with self._lock:
                self._health_query_failures += 1
            return self._recent_device_response_is_acceptable()
        if not self._connection_is_current(mc, generation, require_connected=True):
            return False
        if self._record_device_response(result):
            return True
        with self._lock:
            self._health_query_failures += 1
        return self._recent_device_response_is_acceptable()

    # ── Shared mode (borrow gateway's device) ──────────────────────

    def _gateway_watcher_loop(self) -> None:
        while self._active:
            with self._lock:
                connected = self._connected_device
                stale_candidate = self._mc is not None or self._teardown_attempt is not None
            if not connected and stale_candidate:
                # Never attach over an unproven old subscription set.
                self._detach_from_gateway()
            elif not connected:
                self._try_attach_to_gateway()
            self._sleep_while_active(5)

    def _try_attach_to_gateway(self) -> None:
        with self._lock:
            if (
                not self._active
                or not self._shared_mode
                or self._connected_device
                or self._shared_attach_in_progress
                or self._mc is not None
                or self._teardown_attempt is not None
            ):
                return
            self._shared_attach_in_progress = True
            self._connection_generation += 1
            generation = self._connection_generation

        mc = None
        public_key = ""
        gw_status: dict[str, Any] = {}
        try:
            gw = self.get_ready_plugin("meshcore_gateway")
            if gw is None:
                return
            gw_status = gw.get_status()
            if not gw_status.get("connected"):
                return

            mc = gw.get_device_handle()
            borrowed_loop = gw.get_async_loop()
            if mc is None or borrowed_loop is None:
                return

            # Publish the borrowed candidate before synchronous SDK setup. A
            # concurrent detach can now always find this exact client.
            with self._lock:
                if (
                    not self._active
                    or self._connection_generation != generation
                    or not self._shared_attach_in_progress
                    or self._mc is not None
                ):
                    return
                self._mc = mc
                self._connected_device = False
                self._subscriptions = []
                self._loop = borrowed_loop

            from meshcore.events import EventType

            if mc.self_info:
                public_key = mc.self_info.get("public_key", "")

            async def _on_rx_log(event):
                if not self._connection_is_current(mc, generation, require_connected=True):
                    return
                self._handle_rx_log(event)

            sub = mc.subscribe(EventType.RX_LOG_DATA, _on_rx_log)

            with self._lock:
                if self._mc is mc:
                    self._subscriptions.append(sub)
                current = (
                    self._active and self._connection_generation == generation and self._mc is mc
                )
                if not current and self._mc is None:
                    # detach may have completed while subscribe() was blocked;
                    # retain this late subscription for bounded removal.
                    self._mc = mc
                    self._subscriptions = [sub]
                    self._loop = borrowed_loop
                    self._serial_reopen_blocked = True
            if not current:
                self._detach_from_gateway()
                return

            self._probe_signing(mc)

            # The gateway may have reconnected while signing was probed. Bind
            # this observer only to the exact client we subscribed to.
            if gw.get_device_handle() is not mc or not gw.get_status().get("connected"):
                self._detach_from_gateway()
                return

            with self._lock:
                if (
                    not self._active
                    or self._connection_generation != generation
                    or self._mc is not mc
                ):
                    stale = True
                else:
                    stale = False
                    self._connected_device = True
                    self._connect_count += 1
                    self._public_key = public_key.upper()
                    self._device_info = {
                        "ver": gw_status.get("firmware"),
                        "model": gw_status.get("model"),
                    }
                    self._serial_reopen_blocked = False
            if stale:
                self._detach_from_gateway()
                return

            self.log.info(
                "Observer attached to gateway device (key=%s…)",
                public_key[:12],
            )
            if self._connection_is_current(mc, generation, require_connected=True):
                self.event_bus.publish(
                    events.MESHCORE_OBSERVER_DEVICE_CONNECTED,
                    {
                        "firmware": gw_status.get("firmware"),
                        "model": gw_status.get("model"),
                        "public_key": public_key,
                        "mode": "shared",
                    },
                )
        except Exception:
            self.log.warning("Failed to attach observer to gateway", exc_info=True)
            if mc is not None:
                self._detach_from_gateway()
        finally:
            with self._lock:
                self._shared_attach_in_progress = False

    def _detach_from_gateway(self) -> bool:
        """Bound shared unsubscribe and retain state whenever it is unproven."""
        with self._disconnect_lock:
            self._invalidate_connection_generation()
            with self._lock:
                mc = self._mc
                subs = list(self._subscriptions)
                teardown = self._teardown_attempt
                self._connected_device = False

            if mc is None:
                with self._lock:
                    self._loop = None
                return teardown is None

            if teardown is not None and teardown.get("mc") is mc:
                done = teardown["done"]
                if not done.is_set():
                    done.wait(timeout=self._device_teardown_timeout)
                    return bool(done.is_set() and teardown.get("success"))
                with self._lock:
                    if self._teardown_attempt is teardown:
                        self._teardown_attempt = None

            done = threading.Event()
            teardown = {"mc": mc, "done": done, "success": False, "shared": True}
            with self._lock:
                self._teardown_attempt = teardown
                self._serial_reopen_blocked = True

            def _unsubscribe_shared() -> None:
                success = True
                for sub in subs:
                    try:
                        mc.unsubscribe(sub)
                    except Exception:
                        success = False
                        self.log.debug(
                            "Error unsubscribing observer from gateway",
                            exc_info=True,
                        )

                with self._lock:
                    teardown["success"] = success
                    if self._mc is mc:
                        self._serial_reopen_blocked = not success
                        if success:
                            self._mc = None
                            self._subscriptions = []
                            # Release the borrowed loop; never stop it here.
                            self._loop = None
                    if self._teardown_attempt is teardown:
                        self._teardown_attempt = None
                done.set()

            self._start_thread(_unsubscribe_shared, "observer-shared-unsubscribe")
            if not done.wait(timeout=self._device_teardown_timeout):
                return False
            return bool(teardown["success"])

    def _on_gateway_connected(self, event_type: str, data: dict[str, Any]) -> None:
        if self._active and self._shared_mode and not self._connected_device:
            self._try_attach_to_gateway()

    def _on_gateway_disconnected(self, event_type: str, data: dict[str, Any]) -> None:
        if self._active and self._shared_mode and self._mc is not None:
            self.log.info("Gateway disconnected — observer detaching")
            self._detach_from_gateway()
            self.event_bus.publish(
                events.MESHCORE_OBSERVER_DEVICE_DISCONNECTED,
                {"reason": "gateway_disconnected"},
            )

    def _on_plugin_stopping(self, event_type: str, data: dict[str, Any]) -> None:
        if (
            self._active
            and data.get("name") == "meshcore_gateway"
            and self._shared_mode
            and self._mc is not None
        ):
            self.log.info("Gateway stopping — observer pre-emptively detaching")
            self._detach_from_gateway()

    # ── Packet capture ─────────────────────────────────────────────

    def _handle_rx_log(self, event: Any) -> None:
        try:
            payload = event.payload if isinstance(event.payload, dict) else {}
            packet_json = self._build_packet_json(payload)

            try:
                self._packet_queue.put_nowait(packet_json)
            except queue.Full:
                try:
                    self._packet_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._packet_queue.put_nowait(packet_json)
                except queue.Full:
                    # Rare race: producer refilled the slot before we could.
                    # The new packet is lost too, but we count the overflow once.
                    pass
                with self._lock:
                    self._packets_failed += 1

            with self._lock:
                self._packets_captured += 1
                self._last_packet_time = time.time()

        except Exception:
            self.log.exception("Error handling RX_LOG_DATA event")

    def _build_packet_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        recv_time = payload.get("recv_time", int(time.time()))
        recv_dt = datetime.fromtimestamp(recv_time, tz=timezone.utc)
        return {
            "origin": self.config.get("iata", "XXX"),
            "origin_id": self._public_key[:12] if self._public_key else "",
            "timestamp": recv_dt.isoformat(),
            "type": "PACKET",
            "direction": "rx",
            "time": recv_dt.strftime("%H:%M:%S"),
            "date": recv_dt.strftime("%d/%m/%Y"),
            "len": payload.get("payload_length", 0),
            "packet_type": str(payload.get("payload_type", "")),
            "route": payload.get("route_typename", "UNK"),
            "payload_len": payload.get("payload_length", 0),
            "raw": payload.get("raw_hex", payload.get("payload", "")),
            "SNR": payload.get("snr", 0),
            "RSSI": payload.get("rssi", 0),
            "score": 0,
            "hash": str(payload.get("pkt_hash", "")),
            "path_len": payload.get("path_len", 0),
            "path": payload.get("path", ""),
        }

    # ── MQTT connection ────────────────────────────────────────────

    def _mqtt_connection_loop(self) -> None:
        reconnect_delay = self.config.get("reconnect_delay", 10)
        status_interval = self.config.get("status_interval", 60)
        last_status_time = 0.0

        # Wait for device connection and public key
        while self._active and not self._public_key:
            self._sleep_while_active(2)

        while self._active:
            if not self.internet_available:
                self._sleep_while_active(30)
                continue

            if not self._connected_mqtt:
                try:
                    self._connect_mqtt()
                except Exception as exc:
                    self.log.warning("MQTT connect failed: %s", exc)
                    self.event_bus.publish(
                        events.MESHCORE_OBSERVER_MQTT_DISCONNECTED,
                        {"reason": str(exc)},
                    )
                    self._sleep_while_active(reconnect_delay)
                    continue

            # Drain packet queue
            while self._active and self._connected_mqtt:
                try:
                    packet = self._packet_queue.get(timeout=1)
                    self._publish_packet(packet)
                except queue.Empty:
                    pass
                except Exception:
                    self.log.exception("Error publishing packet")

                # Periodic status publish
                now = time.monotonic()
                if now - last_status_time >= status_interval:
                    self._publish_status()
                    last_status_time = now

                # Check MQTT health
                if self._mqtt_client and not self._mqtt_client.is_connected():
                    self.log.warning("MQTT connection lost")
                    self._disconnect_mqtt()
                    self.event_bus.publish(
                        events.MESHCORE_OBSERVER_MQTT_DISCONNECTED,
                        {"reason": "connection_lost"},
                    )
                    break

                # paho's username_pw_set only takes effect on the next connect,
                # so refresh the JWT by cleanly reconnecting before it expires.
                if self._jwt_expires and time.time() >= self._jwt_expires - _JWT_REFRESH_BUFFER:
                    self.log.info("JWT approaching expiry — reconnecting MQTT")
                    self._disconnect_mqtt()
                    self.event_bus.publish(
                        events.MESHCORE_OBSERVER_MQTT_DISCONNECTED,
                        {"reason": "jwt_refresh"},
                    )
                    break

    def _connect_mqtt(self) -> None:
        import paho.mqtt.client as mqtt

        broker = self.config.get("mqtt_broker", "mqtt-us-v1.letsmesh.net")
        port = self.config.get("mqtt_port", 443)
        transport = self.config.get("mqtt_transport", "websockets")

        self._refresh_jwt_if_needed()
        if not self._jwt_token:
            raise ConnectionError("Failed to generate JWT token")

        client_id = f"reticulumpi-observer-{self._public_key[:12]}"
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            transport=transport,
        )

        if transport == "websockets":
            ws_path = self.config.get("mqtt_ws_path", "/")
            client.ws_set_options(path=ws_path)

        client.tls_set()
        client.username_pw_set(
            username=f"v1_{self._public_key.upper()}",
            password=self._jwt_token,
        )
        client.reconnect_delay_set(min_delay=1, max_delay=120)

        iata = self.config.get("iata", "XXX")

        # LWT (Last Will and Testament) — marks observer offline if connection drops
        status_topic = f"meshcore/{iata}/{self._public_key}/status"
        client.will_set(
            status_topic,
            json.dumps({"online": False}),
            qos=0,
            retain=True,
        )

        # Publish the exact candidate before paho can start callbacks or its
        # network loop.  stop() can therefore always find and close an MQTT
        # client whose connection setup is in progress.
        with self._mqtt_lifecycle_lock:
            with self._lock:
                if not self._active:
                    raise ConnectionError("MeshCore Observer stopped during MQTT setup")
                if self._mqtt_client is not None:
                    raise RuntimeError("previous MQTT client is still published")
                self._mqtt_generation += 1
                generation = self._mqtt_generation
                self._mqtt_client = client
                self._connected_mqtt = False

        def on_connect(client, userdata, flags, rc, properties=None):
            self._handle_mqtt_connect(
                client,
                rc,
                broker,
                port,
                iata,
                status_topic,
                generation,
            )

        def on_disconnect(client, userdata, flags, rc, properties=None):
            with self._mqtt_lifecycle_lock:
                with self._lock:
                    if (
                        not self._active
                        or self._mqtt_client is not client
                        or self._mqtt_generation != generation
                    ):
                        return
                    self._connected_mqtt = False
                self.log.info("MQTT disconnected: %s", rc)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect

        try:
            client.connect(broker, port, keepalive=60)
            with self._mqtt_lifecycle_lock:
                if not self._mqtt_client_is_current(client, generation):
                    raise ConnectionError("MQTT setup was superseded or stopped")
                client.loop_start()
                if not self._mqtt_client_is_current(client, generation):
                    raise ConnectionError("MQTT setup was superseded or stopped")
        except BaseException:
            self._close_mqtt_candidate(client, generation)
            raise

        # Wait briefly for connection to establish
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self._mqtt_client_is_current(client, generation, require_connected=True):
                return
            if not self._mqtt_client_is_current(client, generation):
                self._close_mqtt_candidate(client, generation)
                raise ConnectionError("MQTT setup was superseded or stopped")
            time.sleep(0.2)

        self._close_mqtt_candidate(client, generation)
        raise ConnectionError(f"MQTT connection to {broker}:{port} timed out")

    def _mqtt_client_is_current(
        self,
        client: Any,
        generation: int,
        *,
        require_connected: bool = False,
    ) -> bool:
        """Return whether *client* still owns the active MQTT generation."""

        with self._lock:
            return (
                self._active
                and self._mqtt_client is client
                and self._mqtt_generation == generation
                and (self._connected_mqtt or not require_connected)
            )

    @staticmethod
    def _close_mqtt_client(client: Any) -> None:
        """Stop and disconnect one exact paho client, containing SDK errors."""

        try:
            client.loop_stop()
        except Exception:
            pass
        try:
            client.disconnect()
        except Exception:
            pass

    def _close_mqtt_candidate(self, client: Any, generation: int) -> bool:
        """Detach and close *client* only while it owns *generation*."""

        with self._mqtt_lifecycle_lock:
            with self._lock:
                owns_generation = (
                    self._mqtt_client is client and self._mqtt_generation == generation
                )
                if not owns_generation and self._mqtt_client is client:
                    # The same object was deliberately republished under a
                    # newer generation; never let stale cleanup close it.
                    return False
                if owns_generation:
                    self._mqtt_generation += 1
                    self._mqtt_client = None
                    self._connected_mqtt = False
                # If stop() already detached and closed this exact candidate,
                # close it again after a late connect/loop_start returns.  SDK
                # close calls are idempotent and this prevents resurrection.
            self._close_mqtt_client(client)
            return True

    def _handle_mqtt_connect(
        self,
        client: Any,
        rc: Any,
        broker: str,
        port: int,
        iata: str,
        status_topic: str,
        generation: int | None = None,
    ) -> None:
        """Handle a paho CONNACK. Extracted from the on_connect closure for testing.

        The LWT online publish is issued BEFORE _connected_mqtt is set so that
        status-reporting reflects what actually landed on the broker.
        """
        import paho.mqtt.client as mqtt

        with self._mqtt_lifecycle_lock:
            if generation is None:
                with self._lock:
                    generation = self._mqtt_generation
            if not self._mqtt_client_is_current(client, generation):
                return

            is_failure = getattr(rc, "is_failure", rc != 0)
            if is_failure:
                self.log.warning("MQTT connect failed: %s", rc)
                return
            self.log.info("MQTT connected to %s:%s", broker, port)
            info = client.publish(
                status_topic,
                json.dumps({"online": True}),
                qos=0,
                retain=True,
            )
            if not self._mqtt_client_is_current(client, generation):
                return
            if getattr(info, "rc", 0) != mqtt.MQTT_ERR_SUCCESS:
                self.log.warning("LWT online publish rc=%s", info.rc)
            with self._lock:
                self._connected_mqtt = True

            transport = self.config.get("mqtt_transport", "websockets")
            if transport == "websockets":
                self._ws_ping_stop.set()
                if self._ws_ping_thread is not None:
                    self._ws_ping_thread.join(timeout=2)
                self._ws_ping_stop.clear()
                self._ws_ping_thread = self._start_thread(
                    lambda: self._ws_ping_loop(client, generation),
                    "observer-ws-ping",
                )

            self.event_bus.publish(
                events.MESHCORE_OBSERVER_MQTT_CONNECTED,
                {
                    "broker": broker,
                    "iata": iata,
                },
            )

    def _ws_ping_loop(self, client: Any, generation: int) -> None:
        interval = self.config.get("ws_ping_interval", 30)
        while not self._ws_ping_stop.wait(interval):
            with self._mqtt_lifecycle_lock:
                if not self._mqtt_client_is_current(
                    client,
                    generation,
                    require_connected=True,
                ):
                    break
                try:
                    sock = getattr(client, "_sock", None)
                    if sock is not None and hasattr(sock, "ping"):
                        sock.ping()
                except Exception:
                    self.log.debug("WebSocket ping failed", exc_info=True)
                    break

    def _disconnect_mqtt(self) -> None:
        with self._mqtt_lifecycle_lock:
            with self._lock:
                client = self._mqtt_client
                self._mqtt_generation += 1
                self._mqtt_client = None
                self._connected_mqtt = False
            self._ws_ping_stop.set()
            ping_thread = self._ws_ping_thread
            self._ws_ping_thread = None

        if ping_thread is not None and ping_thread is not threading.current_thread():
            ping_thread.join(timeout=2)
        if client is not None:
            self._close_mqtt_client(client)
        with self._lock:
            self._connected_mqtt = False

    def _mqtt_publish_snapshot(self) -> tuple[Any, int] | None:
        """Capture one connected MQTT owner for an outbound publish."""

        with self._lock:
            client = self._mqtt_client
            if not self._active or client is None or not self._connected_mqtt:
                return None
            return client, self._mqtt_generation

    def _publish_packet(self, packet_json: dict[str, Any]) -> None:
        snapshot = self._mqtt_publish_snapshot()
        if snapshot is None:
            return
        client, generation = snapshot
        iata = self.config.get("iata", "XXX")
        topic = f"meshcore/{iata}/{self._public_key}/packets"
        try:
            result = client.publish(
                topic,
                json.dumps(packet_json),
                qos=0,
            )
            if result.rc == 0:
                with self._lock:
                    if (
                        not self._active
                        or self._mqtt_client is not client
                        or self._mqtt_generation != generation
                        or not self._connected_mqtt
                    ):
                        return
                    self._packets_published += 1
                    self._last_mqtt_publish_time = time.time()
            else:
                with self._lock:
                    self._packets_failed += 1
                self.log.debug("MQTT publish failed rc=%d", result.rc)
        except Exception:
            with self._lock:
                self._packets_failed += 1
            self.log.debug("Error publishing packet to MQTT", exc_info=True)

    def _publish_status(self) -> None:
        snapshot = self._mqtt_publish_snapshot()
        if snapshot is None:
            return
        client, generation = snapshot
        iata = self.config.get("iata", "XXX")
        topic = f"meshcore/{iata}/{self._public_key}/status"
        now = time.monotonic()
        started = getattr(self, "_start_monotonic", now)
        status = {
            "online": True,
            "firmware": self._device_info.get("ver"),
            "model": self._device_info.get("model"),
            "packets_captured": self._packets_captured,
            "uptime": int(max(0.0, now - started)),
        }
        try:
            client.publish(topic, json.dumps(status), qos=0, retain=True)
            if not self._mqtt_client_is_current(client, generation, require_connected=True):
                return
        except Exception:
            self.log.debug("Error publishing status to MQTT", exc_info=True)

    # ── JWT authentication ─────────────────────────────────────────

    def _probe_signing(self, mc: Any) -> None:
        """Determine whether device signing works; fall back to local."""
        try:
            test_data = b"test"
            result = self._run_async(mc.commands.sign(test_data), timeout=15)
            from meshcore.events import EventType

            if result and result.type == EventType.SIGNATURE:
                sig = result.payload.get("signature", b"")
                if sig and len(sig) == 64:
                    self._signing_mode = "device"
                    self.log.info("Observer using on-device Ed25519 signing")
                    return
                self.log.debug("Device signing returned empty/short signature")
        except Exception:
            self.log.debug("Device signing probe failed", exc_info=True)

        # Fallback: try pynacl
        try:
            import nacl.bindings  # noqa: F401

            self._signing_mode = "local"
            self.log.info("Observer using local PyNaCl signing (fallback)")
        except ImportError:
            self._signing_mode = "none"
            self.log.warning(
                "No signing method available — MQTT auth will fail. "
                "Install pynacl or use firmware that supports signing."
            )

    def _refresh_jwt_if_needed(self) -> None:
        if self._jwt_token and time.time() < self._jwt_expires - _JWT_REFRESH_BUFFER:
            return
        try:
            self._jwt_token = self._generate_jwt()
            self._jwt_expires = time.time() + _JWT_LIFETIME
        except Exception:
            self.log.exception("Failed to generate JWT")
            self._jwt_token = None

    def _generate_jwt(self) -> str:
        now = int(time.time())
        broker = self.config.get("mqtt_broker", "mqtt-us-v1.letsmesh.net")
        header = {"alg": "Ed25519", "typ": "JWT"}
        payload = {
            "publicKey": self._public_key.upper(),
            "aud": broker,
            "iat": now,
            "exp": now + _JWT_LIFETIME,
        }

        header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
        payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
        signing_input = f"{header_b64}.{payload_b64}".encode()

        if self._signing_mode == "device":
            signature = self._sign_on_device(signing_input)
        elif self._signing_mode == "local":
            signature = self._sign_local(signing_input)
        else:
            raise RuntimeError("No signing method available")

        sig_hex = signature.hex().upper()
        return f"{header_b64}.{payload_b64}.{sig_hex}"

    def _sign_on_device(self, data: bytes) -> bytes:
        with self._lock:
            mc = self._mc
        if mc is None:
            raise RuntimeError("MeshCore device not connected")

        result = self._run_async(mc.commands.sign(data), timeout=20)
        if result is None:
            self.log.warning("Device signing returned None — device may have disconnected")
            raise RuntimeError("Device signing returned no result")
        from meshcore.events import EventType

        if result.type == EventType.ERROR:
            reason = (
                result.payload.get("reason", "unknown")
                if isinstance(result.payload, dict)
                else str(result.payload)
            )
            raise RuntimeError(f"Device signing failed: {reason}")
        sig = result.payload.get("signature", b"")
        if isinstance(sig, bytes):
            return sig
        return bytes.fromhex(sig) if isinstance(sig, str) else bytes(sig)

    def _sign_local(self, data: bytes) -> bytes:
        import nacl.bindings

        with self._lock:
            mc = self._mc
        if mc is None:
            raise RuntimeError("MeshCore device not connected")

        result = self._run_async(mc.commands.export_private_key())
        from meshcore.events import EventType

        if result.type in (EventType.ERROR, EventType.DISABLED):
            raise RuntimeError("Cannot export private key from device")

        prv_key_bytes = result.payload.get("private_key", b"")
        if isinstance(prv_key_bytes, str):
            try:
                prv_key_bytes = bytes.fromhex(prv_key_bytes)
            except ValueError as exc:
                raise RuntimeError("MeshCore private key is not valid hexadecimal") from exc
        elif isinstance(prv_key_bytes, (bytes, bytearray)):
            prv_key_bytes = bytes(prv_key_bytes)
        else:
            raise RuntimeError("MeshCore private key has an unsupported type")
        if len(prv_key_bytes) != 64:
            raise RuntimeError("MeshCore private key must be 64-byte expanded Ed25519 material")

        try:
            public_key = bytes.fromhex(self._public_key)
        except ValueError as exc:
            raise RuntimeError("MeshCore public key is not valid hexadecimal") from exc
        if len(public_key) != 32:
            raise RuntimeError("MeshCore public key must be 32 bytes")

        # MeshCore/orlp exports an expanded Ed25519 key: the first half is the
        # already-clamped secret scalar and the second half is the nonce prefix.
        # It is not a seed and cannot be passed to SigningKey/from_private_bytes.
        scalar = prv_key_bytes[:32]
        prefix = prv_key_bytes[32:]
        derived_public_key = nacl.bindings.crypto_scalarmult_ed25519_base_noclamp(scalar)
        if derived_public_key != public_key:
            raise RuntimeError("Exported MeshCore private key does not match device public key")

        group_order = 2**252 + 27742317777372353535851937790883648493
        nonce = int.from_bytes(hashlib.sha512(prefix + data).digest(), "little") % group_order
        nonce_point = nacl.bindings.crypto_scalarmult_ed25519_base_noclamp(
            nonce.to_bytes(32, "little")
        )
        challenge = (
            int.from_bytes(hashlib.sha512(nonce_point + public_key + data).digest(), "little")
            % group_order
        )
        response = (nonce + challenge * int.from_bytes(scalar, "little")) % group_order
        return nonce_point + response.to_bytes(32, "little")
