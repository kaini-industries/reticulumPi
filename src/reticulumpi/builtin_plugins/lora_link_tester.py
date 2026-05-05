"""LoRa Link Tester — measures RF link quality to a remote Meshtastic device.

Connects to a dedicated Meshtastic radio (separate from any meshtastic_gateway
device) and sends periodic probe packets to a target node.  Meshtastic's
built-in ACK mechanism provides per-probe RTT, RSSI, and SNR.  Results are
stored in a rolling buffer and streamed to the web dashboard.

Requires: pip install reticulumpi[meshtastic]
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from typing import Any

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

_MESH_NODE_ID_RE = re.compile(r"^![0-9a-fA-F]{8}$")

_MIN_PROBE_INTERVAL = 10
_TIMEOUT_SWEEP_INTERVAL = 5
_SERIAL_OPEN_TIMEOUT = 30
_SNAPSHOT_TAIL = 10


class LoraLinkTester(PluginBase):
    plugin_name = "lora_link_tester"
    plugin_version = "0.1.0"
    plugin_description = "Meshtastic LoRa link quality tester (dedicated radio)"
    broadcast_tier = 2
    broadcast_keys = "link_tester"

    # ── Config validation ──────────────────────────────────────────

    def validate_config(self) -> None:
        try:
            import meshtastic  # noqa: F401
        except ImportError:
            raise ValueError(
                "meshtastic package not found. "
                "Install with: pip install reticulumpi[meshtastic]"
            )

        sp = self.config.get("serial_port")
        if not sp or not isinstance(sp, str):
            raise ValueError("serial_port is required and must be a non-empty string")

        target = self.config.get("target_node_id")
        if target is not None:
            if not isinstance(target, str) or not _MESH_NODE_ID_RE.match(target):
                raise ValueError(
                    f"target_node_id must match !XXXXXXXX (8 hex chars), got {target!r}"
                )

        ch = self.config.get("channel_index", 0)
        if not isinstance(ch, int) or not 0 <= ch <= 7:
            raise ValueError("channel_index must be an integer 0-7")

        pi = self.config.get("probe_interval", 30)
        if not isinstance(pi, (int, float)) or pi < _MIN_PROBE_INTERVAL:
            raise ValueError(f"probe_interval must be >= {_MIN_PROBE_INTERVAL}")

        pc = self.config.get("probe_count", 20)
        if not isinstance(pc, int) or pc < 0:
            raise ValueError("probe_count must be a non-negative integer (0 = unlimited)")

        pt = self.config.get("probe_timeout", 30)
        if not isinstance(pt, (int, float)) or pt < 5:
            raise ValueError("probe_timeout must be >= 5 seconds")

        mh = self.config.get("max_history", 500)
        if not isinstance(mh, int) or mh < 10:
            raise ValueError("max_history must be >= 10")

        hl = self.config.get("hop_limit")
        if hl is not None and (not isinstance(hl, int) or not 1 <= hl <= 7):
            raise ValueError("hop_limit must be 1-7 or null")

        rd = self.config.get("reconnect_delay", 10)
        if not isinstance(rd, (int, float)) or rd < 1:
            raise ValueError("reconnect_delay must be >= 1")

        mra = self.config.get("max_reconnect_attempts", 5)
        if not isinstance(mra, int) or mra < 0:
            raise ValueError("max_reconnect_attempts must be >= 0")

    # ── Lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        self._serial_port: str = self.config["serial_port"]
        self._target_node_id: str | None = self.config.get("target_node_id")
        self._channel_index: int = self.config.get("channel_index", 0)
        self._probe_interval: float = self.config.get("probe_interval", 30)
        self._probe_count: int = self.config.get("probe_count", 20)
        self._probe_timeout: float = self.config.get("probe_timeout", 30)
        self._max_history: int = self.config.get("max_history", 500)
        self._hop_limit: int | None = self.config.get("hop_limit")
        self._reconnect_delay: float = self.config.get("reconnect_delay", 10)
        self._max_reconnect_attempts: int = self.config.get("max_reconnect_attempts", 5)
        self._probe_prefix: str = self.config.get("probe_text_prefix", "LT")

        self._lock = threading.Lock()
        self._interface: Any = None
        self._connected = False
        self._status = "idle"

        self._test_running = False
        self._test_target: str | None = None
        self._test_stop_event = threading.Event()
        self._current_sequence = 0
        self._probes_sent = 0
        self._probes_acked = 0
        self._probes_lost = 0
        self._pending_probes: dict[int, tuple[float, float, int]] = {}
        self._history: deque[dict[str, Any]] = deque(maxlen=self._max_history)

        self._active = True
        self._start_thread(self._connection_loop, "linktester-connect")
        self._start_thread(self._timeout_loop, "linktester-timeout")
        self.log.info("Link tester started (port=%s)", self._serial_port)

    def stop(self) -> None:
        self._active = False
        self._test_stop_event.set()
        self._close_interface()
        self._join_threads()
        self.log.info("Link tester stopped")

    # ── Public API ─────────────────────────────────────────────────

    def start_test(
        self, target: str | None = None, count: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._test_running:
                return {"ok": False, "reason": "test already running"}
            if not self._connected:
                return {"ok": False, "reason": "radio not connected"}

            effective_target = target or self._target_node_id
            if not effective_target:
                return {"ok": False, "reason": "no target specified"}
            if not _MESH_NODE_ID_RE.match(effective_target):
                return {"ok": False, "reason": f"invalid target: {effective_target!r}"}

            effective_count = count if count is not None else self._probe_count

            self._test_running = True
            self._test_target = effective_target
            self._test_stop_event.clear()
            self._current_sequence = 0
            self._probes_sent = 0
            self._probes_acked = 0
            self._probes_lost = 0
            self._pending_probes.clear()

        self._start_thread(
            lambda: self._probe_loop(effective_target, effective_count),
            "linktester-probe",
        )
        self.event_bus.publish(events.LINK_TEST_STARTED, {
            "target": effective_target, "count": effective_count,
        })
        self.log.info("Test started → %s (%s probes)", effective_target, effective_count or "unlimited")
        return {"ok": True, "target": effective_target, "count": effective_count}

    def stop_test(self) -> dict[str, Any]:
        with self._lock:
            if not self._test_running:
                return {"ok": True, "reason": "no test running"}
            self._test_stop_event.set()
            self._test_running = False
        stats = self._compute_stats()
        self.event_bus.publish(events.LINK_TEST_STOPPED, stats)
        self.log.info("Test stopped (%d sent, %d acked, %d lost)", stats["sent"], stats["acked"], stats["lost"])
        return {"ok": True, "stats": stats}

    def clear_history(self) -> dict[str, Any]:
        with self._lock:
            self._history.clear()
            self._probes_sent = 0
            self._probes_acked = 0
            self._probes_lost = 0
        return {"ok": True}

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._active,
                "connected": self._connected,
                "status": self._status,
                "test_running": self._test_running,
                "target": self._test_target or self._target_node_id,
                "serial_port": self._serial_port,
                "probes_sent": self._probes_sent,
                "probes_acked": self._probes_acked,
                "probes_lost": self._probes_lost,
            }

    def get_snapshot(self) -> dict[str, Any]:
        with self._lock:
            tail = list(self._history)[-_SNAPSHOT_TAIL:]
            stats = self._compute_stats_unlocked()
            return {
                "available": True,
                "connected": self._connected,
                "status": self._status,
                "test_running": self._test_running,
                "target": self._test_target or self._target_node_id,
                "results": tail,
                "stats": stats,
            }

    def get_history(self) -> dict[str, Any]:
        with self._lock:
            return {
                "available": True,
                "connected": self._connected,
                "status": self._status,
                "test_running": self._test_running,
                "target": self._test_target or self._target_node_id,
                "results": list(self._history),
                "stats": self._compute_stats_unlocked(),
            }

    # ── Connection management ──────────────────────────────────────

    def _connection_loop(self) -> None:
        reconnect_delay = self._reconnect_delay
        max_attempts = self._max_reconnect_attempts
        failures = 0

        while self._active:
            if self._connected:
                self._sleep_while_active(10)
                continue

            try:
                self._open_interface()
                failures = 0
                self._status = "idle"
            except Exception as exc:
                failures += 1
                self._status = "error"
                self.log.warning("Connection failed (%d): %s", failures, exc)
                self.event_bus.publish(events.LINK_TEST_CONNECTION_CHANGED, {
                    "connected": False, "error": str(exc),
                })
                if max_attempts and failures >= max_attempts:
                    self.log.error("Max reconnect attempts reached, giving up")
                    break
                delay = min(reconnect_delay * (2 ** min(failures - 1, 5)), 300)
                self._sleep_while_active(delay)

    def _open_interface(self) -> None:
        import meshtastic.serial_interface

        result: dict[str, Any] = {"iface": None, "error": None}

        def worker() -> None:
            try:
                result["iface"] = meshtastic.serial_interface.SerialInterface(
                    devPath=self._serial_port,
                )
            except Exception as exc:
                result["error"] = exc

        t = threading.Thread(target=worker, name="linktester-serial-open", daemon=True)
        t.start()
        t.join(timeout=_SERIAL_OPEN_TIMEOUT)

        if t.is_alive():
            raise TimeoutError(
                f"SerialInterface open on {self._serial_port} timed out after {_SERIAL_OPEN_TIMEOUT}s"
            )
        if result["error"] is not None:
            raise result["error"]

        with self._lock:
            self._interface = result["iface"]
            self._connected = True

        self.log.info("Connected to %s", self._serial_port)
        self.event_bus.publish(events.LINK_TEST_CONNECTION_CHANGED, {"connected": True})

    def _close_interface(self) -> None:
        with self._lock:
            iface = self._interface
            self._interface = None
            self._connected = False

        if iface is not None:
            try:
                iface.close()
            except Exception:
                pass

    # ── Probe send/receive ─────────────────────────────────────────

    def _probe_loop(self, target: str, count: int) -> None:
        seq = 0
        while self._active and not self._test_stop_event.is_set():
            if count > 0 and seq >= count:
                break
            try:
                self._send_probe(target, seq)
            except Exception as exc:
                self.log.warning("Probe send failed: %s", exc)
                with self._lock:
                    if not self._connected:
                        break
            seq += 1
            # Interruptible sleep between probes
            self._test_stop_event.wait(timeout=self._probe_interval)

        with self._lock:
            self._test_running = False
        stats = self._compute_stats()
        self.event_bus.publish(events.LINK_TEST_STOPPED, stats)
        self.log.info("Probe loop finished (%d sent)", seq)

    def _send_probe(self, target: str, seq: int) -> None:
        with self._lock:
            iface = self._interface
            if iface is None:
                raise RuntimeError("No interface")

        payload = f"{self._probe_prefix}:{seq:04d}".encode("utf-8")
        send_mono = time.monotonic()
        send_wall = time.time()

        import meshtastic.portnums_pb2 as portnums_pb2

        send_kwargs: dict[str, Any] = {
            "data": payload,
            "destinationId": target,
            "portNum": portnums_pb2.PortNum.TEXT_MESSAGE_APP,
            "wantAck": True,
            "wantResponse": False,
            "onResponse": self._make_probe_callback(seq, send_mono, send_wall),
            "onResponseAckPermitted": True,
            "channelIndex": self._channel_index,
        }
        if self._hop_limit is not None:
            send_kwargs["hopLimit"] = self._hop_limit

        packet = iface.sendData(**send_kwargs)

        packet_id = packet.id if hasattr(packet, "id") else getattr(packet, "get", lambda k, d: d)("id", 0)

        with self._lock:
            self._pending_probes[packet_id] = (send_mono, send_wall, seq)
            self._probes_sent += 1
            self._current_sequence = seq + 1

    def _make_probe_callback(self, seq: int, send_mono: float, send_wall: float):
        def on_response(packet: dict) -> None:
            recv_mono = time.monotonic()
            rtt_ms = round((recv_mono - send_mono) * 1000, 1)

            rssi = packet.get("rxRssi")
            snr = packet.get("rxSnr")

            packet_id = packet.get("id", 0)
            with self._lock:
                self._pending_probes.pop(packet_id, None)
                self._probes_acked += 1

            result = {
                "seq": seq,
                "time": send_wall,
                "rtt_ms": rtt_ms,
                "rssi": rssi,
                "snr": snr,
                "status": "ack",
            }
            with self._lock:
                self._history.append(result)

            self.event_bus.publish(events.LINK_TEST_PROBE_RESULT, result)

        return on_response

    def _timeout_loop(self) -> None:
        while self._active:
            self._sleep_while_active(_TIMEOUT_SWEEP_INTERVAL)
            self._sweep_timeouts()

    def _sweep_timeouts(self) -> None:
        now = time.monotonic()
        timed_out: list[tuple[int, float, int]] = []

        with self._lock:
            for pkt_id, (send_mono, send_wall, seq) in list(self._pending_probes.items()):
                if now - send_mono > self._probe_timeout:
                    timed_out.append((pkt_id, send_wall, seq))

            for pkt_id, send_wall, seq in timed_out:
                self._pending_probes.pop(pkt_id, None)
                self._probes_lost += 1
                result = {
                    "seq": seq,
                    "time": send_wall,
                    "rtt_ms": None,
                    "rssi": None,
                    "snr": None,
                    "status": "lost",
                }
                self._history.append(result)

        for _, send_wall, seq in timed_out:
            self.event_bus.publish(events.LINK_TEST_PROBE_RESULT, {
                "seq": seq, "time": send_wall,
                "rtt_ms": None, "rssi": None, "snr": None, "status": "lost",
            })

    # ── Statistics ─────────────────────────────────────────────────

    def _compute_stats(self) -> dict[str, Any]:
        with self._lock:
            return self._compute_stats_unlocked()

    def _compute_stats_unlocked(self) -> dict[str, Any]:
        sent = self._probes_sent
        acked = self._probes_acked
        lost = self._probes_lost
        loss_pct = round(lost / sent * 100, 1) if sent > 0 else 0.0

        rtts = [r["rtt_ms"] for r in self._history if r["rtt_ms"] is not None]
        rssis = [r["rssi"] for r in self._history if r["rssi"] is not None]
        snrs = [r["snr"] for r in self._history if r["snr"] is not None]

        return {
            "sent": sent,
            "acked": acked,
            "lost": lost,
            "loss_pct": loss_pct,
            "rtt_min": round(min(rtts), 1) if rtts else None,
            "rtt_avg": round(sum(rtts) / len(rtts), 1) if rtts else None,
            "rtt_max": round(max(rtts), 1) if rtts else None,
            "rssi_avg": round(sum(rssis) / len(rssis), 1) if rssis else None,
            "snr_avg": round(sum(snrs) / len(snrs), 1) if snrs else None,
        }
