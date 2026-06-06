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
import json
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

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
    plugin_version = "1.0.0"
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
                port = self.config.get("serial_port", "/dev/ttyUSB0")
                if not isinstance(port, str) or not port:
                    raise ValueError("serial_port must be a non-empty string")
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
        if not isinstance(hci, (int, float)) or hci < 5:
            raise ValueError("health_check_interval must be >= 5 seconds")

        rd = self.config.get("reconnect_delay", 10)
        if not isinstance(rd, (int, float)) or rd < 1:
            raise ValueError("reconnect_delay must be >= 1 second")

        mra = self.config.get("max_reconnect_attempts", 10)
        if not isinstance(mra, int) or mra < 0:
            raise ValueError("max_reconnect_attempts must be a non-negative integer")

        ws_ping = self.config.get("ws_ping_interval", 30)
        if not isinstance(ws_ping, (int, float)) or ws_ping < 10:
            raise ValueError("ws_ping_interval must be >= 10 seconds")

    # ── Lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        _patch_paho_websocket()

        self._lock = threading.Lock()
        self._start_time = time.time()

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

        # MQTT state
        self._mqtt_client: Any = None
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

        if self._shared_mode:
            try:
                self.event_bus.unsubscribe_all(self._on_gateway_connected)
                self.event_bus.unsubscribe_all(self._on_gateway_disconnected)
                self.event_bus.unsubscribe_all(self._on_plugin_stopping)
            except Exception:
                self.log.debug(
                    "Error unsubscribing gateway handlers",
                    exc_info=True,
                )

        if not self._shared_mode and self._loop and self._mc:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._async_disconnect(),
                    self._loop,
                )
                future.result(timeout=5)
            except Exception:
                self.log.debug("Error during MeshCore disconnect", exc_info=True)
        elif self._shared_mode:
            self._detach_from_gateway()

        self._disconnect_mqtt()

        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

        self._join_threads()

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
            raise RuntimeError("MeshCore async loop not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def on_internet_available(self) -> None:
        self.log.info("Internet restored — MQTT reconnection enabled")

    def on_internet_lost(self) -> None:
        self.log.warning("Internet lost — MQTT reconnection paused, packets queued")

    # ── Standalone device connection ───────────────────────────────

    def _device_connection_loop(self) -> None:
        reconnect_delay = self.config.get("reconnect_delay", 10)
        health_check_interval = self.config.get("health_check_interval", 30)
        max_attempts = self.config.get("max_reconnect_attempts", 10)
        health_failure_threshold = max(1, int(self.config.get("health_failure_threshold", 3)))
        consecutive_health_failures = 0

        while self._active:
            if not self._connected_device:
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
        if conn_type == "serial":
            serial_port = self.config.get("serial_port", "/dev/ttyUSB0")
            baudrate = self.config.get("serial_baud", 115200)
            self.log.info(
                "Connecting observer to MeshCore device (port=%s)...",
                serial_port,
            )
            mc = self._run_async(
                MeshCore.create_serial(
                    serial_port,
                    baudrate,
                    auto_reconnect=True,
                    max_reconnect_attempts=3,
                ),
                timeout=30,
            )
        else:
            host = self.config.get("tcp_host")
            port = self.config.get("tcp_port", 5000)
            self.log.info(
                "Connecting observer to MeshCore device (tcp=%s:%d)...",
                host,
                port,
            )
            mc = self._run_async(
                MeshCore.create_tcp(
                    host,
                    port,
                    auto_reconnect=True,
                    max_reconnect_attempts=3,
                ),
                timeout=30,
            )

        if mc is None:
            raise ConnectionError("MeshCore device did not respond")

        self._run_async(mc.commands.set_time(int(time.time())))

        result = self._run_async(mc.commands.send_device_query())
        if result and hasattr(result, "payload"):
            self._device_info = dict(result.payload)

        result = self._run_async(mc.commands.send_appstart())
        public_key = ""
        if mc.self_info:
            public_key = mc.self_info.get("public_key", "")
        if not public_key and result and hasattr(result, "payload"):
            public_key = result.payload.get("public_key", "")

        subs = []

        async def _on_rx_log(event):
            self._handle_rx_log(event)

        async def _on_disconnect(event):
            self.log.warning("MeshCore observer device disconnected")
            with self._lock:
                self._connected_device = False
            self.event_bus.publish(
                events.MESHCORE_OBSERVER_DEVICE_DISCONNECTED,
                {"reason": "device_disconnected"},
            )

        subs.append(mc.subscribe(EventType.RX_LOG_DATA, _on_rx_log))
        subs.append(mc.subscribe(EventType.DISCONNECTED, _on_disconnect))

        with self._lock:
            self._mc = mc
            self._connected_device = True
            self._connect_count += 1
            self._subscriptions = subs
            self._public_key = public_key.upper()

        self._probe_signing(mc)

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

    def _disconnect_device(self) -> None:
        with self._lock:
            mc = self._mc
            subs = self._subscriptions
            self._mc = None
            self._connected_device = False
            self._subscriptions = []

        if mc is None:
            return

        for sub in subs:
            try:
                mc.unsubscribe(sub)
            except Exception:
                self.log.debug("Error unsubscribing observer handler", exc_info=True)

        if self._loop and self._loop.is_running():
            try:
                self._run_async(mc.disconnect(), timeout=5)
            except Exception:
                self.log.debug("Error disconnecting observer device", exc_info=True)

    async def _async_disconnect(self) -> None:
        mc = self._mc
        if mc:
            try:
                await mc.disconnect()
            except Exception:
                self.log.debug("Error during observer disconnect", exc_info=True)

    def _check_health(self) -> bool:
        with self._lock:
            mc = self._mc
        if mc is None:
            return False
        try:
            return mc.is_connected
        except Exception:
            return False

    # ── Shared mode (borrow gateway's device) ──────────────────────

    def _gateway_watcher_loop(self) -> None:
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

        while self._active:
            if not self._connected_device:
                self._try_attach_to_gateway()
            self._sleep_while_active(5)

    def _try_attach_to_gateway(self) -> None:
        gw = self.app.get_plugin("meshcore_gateway")
        if gw is None:
            return
        gw_status = gw.get_status()
        if not gw_status.get("connected"):
            return

        mc = gw.get_device_handle()
        if mc is None:
            return

        # Claim the attach slot under lock so racing callers (watcher loop vs
        # MESHCORE_CONNECTED handler) can't both run setup and double-subscribe.
        with self._lock:
            if self._connected_device:
                return
            self._connected_device = True

        try:
            from meshcore.events import EventType

            public_key = ""
            if mc.self_info:
                public_key = mc.self_info.get("public_key", "")

            async def _on_rx_log(event):
                self._handle_rx_log(event)

            subs = [mc.subscribe(EventType.RX_LOG_DATA, _on_rx_log)]

            with self._lock:
                self._mc = mc
                self._connect_count += 1
                self._subscriptions = subs
                self._public_key = public_key.upper()
                self._device_info = {
                    "ver": gw_status.get("firmware"),
                    "model": gw_status.get("model"),
                }
                # Borrow gateway's async loop for signing — we MUST NOT stop it.
                self._loop = gw.get_async_loop()

            self._probe_signing(mc)
        except Exception:
            with self._lock:
                self._connected_device = False
            raise

        self.log.info(
            "Observer attached to gateway device (key=%s…)",
            public_key[:12],
        )
        self.event_bus.publish(
            events.MESHCORE_OBSERVER_DEVICE_CONNECTED,
            {
                "firmware": gw_status.get("firmware"),
                "model": gw_status.get("model"),
                "public_key": public_key,
                "mode": "shared",
            },
        )

    def _detach_from_gateway(self) -> None:
        with self._lock:
            mc = self._mc
            subs = self._subscriptions
            self._mc = None
            self._connected_device = False
            self._subscriptions = []
            # Release borrowed loop — we do not own it.
            self._loop = None

        if mc is None:
            return
        for sub in subs:
            try:
                mc.unsubscribe(sub)
            except Exception:
                self.log.debug("Error unsubscribing observer from gateway", exc_info=True)

    def _on_gateway_connected(self, event_type: str, data: dict[str, Any]) -> None:
        if self._shared_mode and not self._connected_device:
            self._try_attach_to_gateway()

    def _on_gateway_disconnected(self, event_type: str, data: dict[str, Any]) -> None:
        if self._shared_mode and self._connected_device:
            self.log.info("Gateway disconnected — observer detaching")
            self._detach_from_gateway()
            self.event_bus.publish(
                events.MESHCORE_OBSERVER_DEVICE_DISCONNECTED,
                {"reason": "gateway_disconnected"},
            )

    def _on_plugin_stopping(self, event_type: str, data: dict[str, Any]) -> None:
        if data.get("name") == "meshcore_gateway" and self._shared_mode and self._connected_device:
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
                now = time.time()
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

        def on_connect(client, userdata, flags, rc, properties=None):
            self._handle_mqtt_connect(client, rc, broker, port, iata, status_topic)

        def on_disconnect(client, userdata, flags, rc, properties=None):
            self.log.info("MQTT disconnected: %s", rc)
            with self._lock:
                self._connected_mqtt = False

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect

        client.connect(broker, port, keepalive=60)
        client.loop_start()

        self._mqtt_client = client

        # Wait briefly for connection to establish
        deadline = time.time() + 10
        while time.time() < deadline and not self._connected_mqtt and self._active:
            time.sleep(0.2)

        if not self._connected_mqtt:
            client.loop_stop()
            client.disconnect()
            self._mqtt_client = None
            raise ConnectionError(f"MQTT connection to {broker}:{port} timed out")

    def _handle_mqtt_connect(
        self,
        client: Any,
        rc: Any,
        broker: str,
        port: int,
        iata: str,
        status_topic: str,
    ) -> None:
        """Handle a paho CONNACK. Extracted from the on_connect closure for testing.

        The LWT online publish is issued BEFORE _connected_mqtt is set so that
        status-reporting reflects what actually landed on the broker.
        """
        import paho.mqtt.client as mqtt

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
        if getattr(info, "rc", 0) != mqtt.MQTT_ERR_SUCCESS:
            self.log.warning("LWT online publish rc=%s", info.rc)
        with self._lock:
            self._connected_mqtt = True

        self.event_bus.publish(
            events.MESHCORE_OBSERVER_MQTT_CONNECTED,
            {
                "broker": broker,
                "iata": iata,
            },
        )

        transport = self.config.get("mqtt_transport", "websockets")
        if transport == "websockets":
            self._ws_ping_stop.set()
            if self._ws_ping_thread is not None:
                self._ws_ping_thread.join(timeout=2)
            self._ws_ping_stop.clear()
            self._ws_ping_thread = self._start_thread(
                self._ws_ping_loop,
                "observer-ws-ping",
            )

    def _ws_ping_loop(self) -> None:
        interval = self.config.get("ws_ping_interval", 30)
        while not self._ws_ping_stop.wait(interval):
            client = self._mqtt_client
            if client is None:
                break
            try:
                sock = getattr(client, "_sock", None)
                if sock is not None and hasattr(sock, "ping"):
                    sock.ping()
            except Exception:
                self.log.debug("WebSocket ping failed", exc_info=True)
                break

    def _disconnect_mqtt(self) -> None:
        client = self._mqtt_client
        if client is None:
            return

        if hasattr(self, "_ws_ping_stop"):
            self._ws_ping_stop.set()
            if self._ws_ping_thread is not None:
                self._ws_ping_thread.join(timeout=2)
                self._ws_ping_thread = None
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            self.log.debug("Error disconnecting MQTT", exc_info=True)
        self._mqtt_client = None
        with self._lock:
            self._connected_mqtt = False

    def _publish_packet(self, packet_json: dict[str, Any]) -> None:
        if not self._mqtt_client or not self._connected_mqtt:
            return
        iata = self.config.get("iata", "XXX")
        topic = f"meshcore/{iata}/{self._public_key}/packets"
        try:
            result = self._mqtt_client.publish(
                topic,
                json.dumps(packet_json),
                qos=0,
            )
            if result.rc == 0:
                with self._lock:
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
        if not self._mqtt_client or not self._connected_mqtt:
            return
        iata = self.config.get("iata", "XXX")
        topic = f"meshcore/{iata}/{self._public_key}/status"
        status = {
            "online": True,
            "firmware": self._device_info.get("ver"),
            "model": self._device_info.get("model"),
            "packets_captured": self._packets_captured,
            "uptime": int(time.time() - self._start_time),
        }
        try:
            self._mqtt_client.publish(topic, json.dumps(status), qos=0, retain=True)
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
            import nacl.signing  # noqa: F401

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
        import nacl.signing

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
            prv_key_bytes = bytes.fromhex(prv_key_bytes)

        signing_key = nacl.signing.SigningKey(prv_key_bytes)
        signed = signing_key.sign(data)
        return signed.signature
