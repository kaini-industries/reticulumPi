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
from reticulumpi.runtime_metrics import record_hung_worker
from reticulumpi.serial_devices import (
    SerialDeviceChangedError,
    SerialDeviceLease,
    StaleSerialDeviceLeaseError,
    serial_device_registry,
    validate_stable_serial_path,
)

_MESH_NODE_ID_RE = re.compile(r"^![0-9a-fA-F]{8}$")

_MIN_PROBE_INTERVAL = 10
_TIMEOUT_SWEEP_INTERVAL = 5
_SERIAL_OPEN_TIMEOUT = 30
_SERIAL_CLOSE_TIMEOUT = 2
_SERIAL_SEND_TIMEOUT = 15
_SERIAL_WORKER_SHUTDOWN_TIMEOUT = 5
# A timed-out Meshtastic constructor may already hold the tty. Never start a
# second constructor until that worker has returned and closed its late result.
_MAX_ABANDONED_SERIAL_OPEN_WORKERS = 1
_SNAPSHOT_TAIL = 10


class LoraLinkTester(PluginBase):
    plugin_name = "lora_link_tester"
    plugin_version = "0.1.1"
    plugin_description = "Meshtastic LoRa link quality tester (dedicated radio)"
    broadcast_tier = 2
    broadcast_keys = "link_tester"

    # ── Config validation ──────────────────────────────────────────

    def validate_config(self) -> None:
        try:
            import meshtastic  # noqa: F401
        except ImportError:
            raise ValueError(
                "meshtastic package not found. Install with: pip install reticulumpi[meshtastic]"
            )

        sp = self.config.get("serial_port")
        validate_stable_serial_path(sp)

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
        if isinstance(pc, bool) or not isinstance(pc, int) or pc < 0:
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

        mra = self.config.get("max_reconnect_attempts", 0)
        if not isinstance(mra, int) or mra < 0:
            raise ValueError("max_reconnect_attempts must be >= 0")

    # ── Lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        previous_lock = getattr(self, "_lock", None)
        if previous_lock is not None:
            with previous_lock:
                teardown_incomplete = (
                    bool(getattr(self, "_serial_open_attempts", set()))
                    or bool(getattr(self, "_serial_open_workers", set()))
                    or bool(getattr(self, "_serial_send_attempts", set()))
                    or bool(getattr(self, "_serial_send_workers", set()))
                    or bool(getattr(self, "_serial_close_attempts", set()))
                    or bool(getattr(self, "_unresolved_serial_handles", {}))
                    or (
                        (watcher := getattr(self, "_lease_release_watcher", None)) is not None
                        and watcher.is_alive()
                    )
                    or getattr(self, "_interface", None) is not None
                    or getattr(self, "_serial_device_lease", None) is not None
                )
            if teardown_incomplete:
                raise RuntimeError("Link Tester serial teardown is incomplete; refusing restart")

        self._serial_port: str = self.config["serial_port"]
        self._target_node_id: str | None = self.config.get("target_node_id")
        self._channel_index: int = self.config.get("channel_index", 0)
        self._probe_interval: float = self.config.get("probe_interval", 30)
        self._probe_count: int = self.config.get("probe_count", 20)
        self._probe_timeout: float = self.config.get("probe_timeout", 30)
        self._max_history: int = self.config.get("max_history", 500)
        self._hop_limit: int | None = self.config.get("hop_limit")
        self._reconnect_delay: float = self.config.get("reconnect_delay", 10)
        self._max_reconnect_attempts: int = self.config.get("max_reconnect_attempts", 0)
        self._probe_prefix: str = self.config.get("probe_text_prefix", "LT")

        self._lock = threading.Lock()
        self._serial_worker_condition = threading.Condition(self._lock)
        self._serial_device_lease: SerialDeviceLease | None = None
        self._serial_open_generation = 0
        self._serial_open_workers: set[threading.Thread] = set()
        self._serial_open_attempts: set[int] = set()
        self._abandoned_serial_open_workers: set[threading.Thread] = set()
        self._serial_send_generation = 0
        self._serial_send_workers: set[threading.Thread] = set()
        self._serial_send_attempts: set[int] = set()
        self._abandoned_serial_send_workers: set[threading.Thread] = set()
        self._serial_close_attempts: set[int] = set()
        self._unresolved_serial_handles: dict[int, Any] = {}
        self._lease_release_watcher: threading.Thread | None = None
        self._serial_teardown_complete = False
        self._interface: Any = None
        self._connected = False
        self._status = "idle"

        self._test_running = False
        self._test_generation = 0
        self._test_target: str | None = None
        self._test_stop_event = threading.Event()
        self._current_sequence = 0
        self._probes_sent = 0
        self._probes_acked = 0
        self._probes_lost = 0
        self._pending_probes: dict[int, tuple[float, float, int, int]] = {}
        self._pending_probe_sends: set[tuple[int, int]] = set()
        self._early_probe_acks: dict[tuple[int, int], dict[str, Any]] = {}
        self._history: deque[dict[str, Any]] = deque(maxlen=self._max_history)

        self._active = True
        self._start_thread(self._connection_loop, "linktester-connect")
        self._start_thread(self._timeout_loop, "linktester-timeout")
        self.log.info("Link tester started (port=%s)", self._serial_port)

    def stop(self) -> None:
        with self._serial_worker_condition:
            self._active = False
            # Invalidate every constructor already in flight before closing
            # the published interface. Late results must close themselves.
            self._serial_open_generation += 1
            self._serial_send_generation += 1
            self._test_generation += 1
            self._test_running = False
            self._pending_probes.clear()
            self._pending_probe_sends.clear()
            self._early_probe_acks.clear()
            self._serial_worker_condition.notify_all()
        self._test_stop_event.set()
        self._close_interface()
        self._join_threads()
        workers_stopped = self._wait_for_serial_open_workers(_SERIAL_WORKER_SHUTDOWN_TIMEOUT)
        send_workers_stopped = self._wait_for_serial_send_workers(_SERIAL_WORKER_SHUTDOWN_TIMEOUT)
        with self._serial_worker_condition:
            self._serial_teardown_complete = True
        released = self._release_serial_device_lease_if_quiescent()
        if not released:
            self._schedule_shutdown_lease_release()
        if (not workers_stopped or not send_workers_stopped) and not released:
            self.log.warning(
                "Serial I/O worker still running at shutdown; retaining the "
                "device lease until it returns"
            )
        self.log.info("Link tester stopped")

    # ── Public API ─────────────────────────────────────────────────

    def start_test(
        self,
        target: str | None = None,
        count: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._test_running:
                return {"ok": False, "reason": "test already running"}
            if not self._connected:
                return {"ok": False, "reason": "radio not connected"}

            effective_target = target or self._target_node_id
            if not effective_target:
                return {"ok": False, "reason": "no target specified"}
            if not isinstance(effective_target, str) or not _MESH_NODE_ID_RE.match(
                effective_target
            ):
                return {"ok": False, "reason": f"invalid target: {effective_target!r}"}

            if count is not None and (
                isinstance(count, bool) or not isinstance(count, int) or count < 0
            ):
                return {
                    "ok": False,
                    "reason": "count must be a non-negative integer (0 = unlimited)",
                }
            effective_count = count if count is not None else self._probe_count

            self._test_running = True
            self._test_generation += 1
            generation = self._test_generation
            self._test_target = effective_target
            self._test_stop_event.clear()
            self._current_sequence = 0
            self._probes_sent = 0
            self._probes_acked = 0
            self._probes_lost = 0
            self._pending_probes.clear()
            self._pending_probe_sends.clear()
            self._early_probe_acks.clear()

        self._start_thread(
            lambda: self._probe_loop(effective_target, effective_count, generation),
            "linktester-probe",
        )
        self.event_bus.publish(
            events.LINK_TEST_STARTED,
            {
                "target": effective_target,
                "count": effective_count,
            },
        )
        self.log.info(
            "Test started → %s (%s probes)", effective_target, effective_count or "unlimited"
        )
        return {"ok": True, "target": effective_target, "count": effective_count}

    def stop_test(self) -> dict[str, Any]:
        with self._lock:
            if not self._test_running:
                return {"ok": True, "reason": "no test running"}
            self._test_stop_event.set()
            self._test_running = False
            self._test_generation += 1
            self._pending_probes.clear()
            self._pending_probe_sends.clear()
            self._early_probe_acks.clear()
        stats = self._compute_stats()
        self.event_bus.publish(events.LINK_TEST_STOPPED, stats)
        self.log.info(
            "Test stopped (%d sent, %d acked, %d lost)",
            stats["sent"],
            stats["acked"],
            stats["lost"],
        )
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
                "serial_reopen_blocked": bool(
                    self._serial_open_attempts
                    or self._serial_send_attempts
                    or self._serial_close_attempts
                    or self._unresolved_serial_handles
                ),
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

    def _ensure_serial_device_lease(self) -> SerialDeviceLease:
        """Claim or revalidate the link tester's dedicated serial device."""
        with self._serial_worker_condition:
            if not self._active:
                raise RuntimeError("Link tester is stopping; refusing serial open")

            lease = self._serial_device_lease
            if lease is not None:
                try:
                    lease.revalidate()
                    return lease
                except (SerialDeviceChangedError, StaleSerialDeviceLeaseError):
                    # The configured path now identifies a different endpoint.
                    # Fail this attempt closed; the reconnect loop may claim
                    # and verify the replacement on its next attempt.
                    lease.release()
                    self._serial_device_lease = None
                    raise

            lease = serial_device_registry.claim(self._serial_port, self.plugin_name)
            self._serial_device_lease = lease
            try:
                lease.revalidate()
            except Exception:
                lease.release()
                self._serial_device_lease = None
                raise
            return lease

    def _wait_for_serial_open_workers(self, timeout: float) -> bool:
        """Wait until every constructor attempt has disposed of its result."""
        deadline = time.monotonic() + timeout
        with self._serial_worker_condition:
            while self._serial_open_attempts:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._serial_worker_condition.wait(timeout=remaining)
            return True

    def _wait_for_serial_send_workers(self, timeout: float) -> bool:
        """Wait until every bounded SDK send attempt has returned."""

        deadline = time.monotonic() + timeout
        with self._serial_worker_condition:
            while self._serial_send_attempts:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._serial_worker_condition.wait(timeout=remaining)
            return True

    def _release_serial_device_lease_if_quiescent(self) -> bool:
        """Release only after shutdown proves no tty handle can remain."""
        with self._threads_lock:
            managed_worker_alive = any(thread.is_alive() for thread in self._threads)
        with self._serial_worker_condition:
            if (
                self._active
                or not self._serial_teardown_complete
                or managed_worker_alive
                or self._serial_open_attempts
                or self._serial_open_workers
                or self._serial_send_attempts
                or self._serial_send_workers
                or self._interface is not None
                or self._serial_close_attempts
                or self._unresolved_serial_handles
            ):
                return False
            lease = self._serial_device_lease
            self._serial_device_lease = None
        if lease is not None:
            lease.release()
        return True

    def _schedule_shutdown_lease_release(self) -> None:
        """Recheck fail-closed teardown after late workers or close retries finish."""

        with self._serial_worker_condition:
            existing = self._lease_release_watcher
            if existing is not None and existing.is_alive():
                return
            if self._serial_device_lease is None:
                return

        def watch() -> None:
            delay = 0.1
            try:
                while True:
                    with self._serial_worker_condition:
                        if self._active or self._serial_device_lease is None:
                            return
                    self._retry_unresolved_serial_handles()
                    if self._release_serial_device_lease_if_quiescent():
                        return
                    with self._serial_worker_condition:
                        self._serial_worker_condition.wait(timeout=delay)
                    delay = min(delay * 2, 30.0)
            finally:
                with self._serial_worker_condition:
                    if self._lease_release_watcher is threading.current_thread():
                        self._serial_worker_condition.notify_all()

        thread = threading.Thread(
            target=watch,
            name="linktester-shutdown-release",
            daemon=True,
        )
        with self._serial_worker_condition:
            self._lease_release_watcher = thread
        try:
            thread.start()
        except BaseException:
            record_hung_worker()
            self.log.exception("Could not start Link Tester shutdown release watcher")

    def _connection_loop(self) -> None:
        reconnect_delay = self._reconnect_delay
        max_attempts = self._max_reconnect_attempts
        failures = 0

        while self._active:
            if not self._retry_unresolved_serial_handles():
                self._status = "error"
                self._sleep_while_active(reconnect_delay)
                continue
            with self._serial_worker_condition:
                send_unresolved = bool(self._serial_send_attempts or self._serial_send_workers)
            if send_unresolved:
                self._status = "error"
                self._sleep_while_active(reconnect_delay)
                continue
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
                self.event_bus.publish(
                    events.LINK_TEST_CONNECTION_CHANGED,
                    {
                        "connected": False,
                        "error": str(exc),
                    },
                )
                if max_attempts and failures >= max_attempts:
                    self.log.error("Max reconnect attempts reached, giving up")
                    with self._serial_worker_condition:
                        self._active = False
                        self._connected = False
                        self._status = "failed"
                    self._stop_event.set()
                    break
                delay = min(reconnect_delay * (2 ** min(failures - 1, 5)), 300)
                self._sleep_while_active(delay)

    def _open_interface(self) -> None:
        import meshtastic.serial_interface

        with self._serial_worker_condition:
            if not self._active:
                raise RuntimeError("Link tester is stopping; refusing serial open")
            if len(self._abandoned_serial_open_workers) >= _MAX_ABANDONED_SERIAL_OPEN_WORKERS:
                raise RuntimeError("Serial-open worker cap reached; refusing another constructor")
            if self._serial_close_attempts or self._unresolved_serial_handles:
                raise RuntimeError("Previous serial handle teardown is unresolved")
            if self._serial_send_attempts or self._serial_send_workers:
                raise RuntimeError("Previous serial send is unresolved")
            if self._interface is not None:
                raise RuntimeError("A Link Tester serial interface is already published")

        # Resolve and exclusively own the explicit dedicated endpoint before
        # starting any Meshtastic constructor. The lease survives ordinary
        # constructor failures and reconnect backoff.
        self._ensure_serial_device_lease()

        result: dict[str, Any] = {"iface": None, "error": None}
        cancelled = threading.Event()

        with self._serial_worker_condition:
            if not self._active:
                raise RuntimeError("Link tester is stopping; refusing serial open")
            if len(self._abandoned_serial_open_workers) >= _MAX_ABANDONED_SERIAL_OPEN_WORKERS:
                raise RuntimeError("Serial-open worker cap reached; refusing another constructor")
            if self._serial_close_attempts or self._unresolved_serial_handles:
                raise RuntimeError("Previous serial handle teardown is unresolved")
            if self._serial_send_attempts or self._serial_send_workers:
                raise RuntimeError("Previous serial send is unresolved")
            if self._interface is not None:
                raise RuntimeError("A Link Tester serial interface is already published")
            self._serial_open_generation += 1
            generation = self._serial_open_generation
            self._serial_open_attempts.add(generation)

        def worker() -> None:
            iface = None
            error: BaseException | None = None
            try:
                iface = meshtastic.serial_interface.SerialInterface(
                    devPath=self._serial_port,
                )
            except BaseException as exc:
                error = exc

            # Completion and timeout cancellation serialize on the same lock.
            # If the worker is still current, the caller consumes the result;
            # otherwise this worker owns disposal of the late interface.
            with self._serial_worker_condition:
                stale = (
                    cancelled.is_set()
                    or generation != self._serial_open_generation
                    or not self._active
                )
                if not stale:
                    result["iface"] = iface
                    result["error"] = error
                    self._serial_open_workers.discard(thread)
                    self._abandoned_serial_open_workers.discard(thread)
                    self._serial_worker_condition.notify_all()
                    return

            if iface is not None:
                self._close_serial_handle_once(
                    iface,
                    "abandoned Link Tester serial interface",
                )

            with self._serial_worker_condition:
                self._serial_open_workers.discard(thread)
                self._abandoned_serial_open_workers.discard(thread)
                self._serial_open_attempts.discard(generation)
                self._serial_worker_condition.notify_all()
            # stop() deliberately retains the lease when a constructor is
            # still live. The final worker releases it after closing its late
            # result and proving that no other open attempt remains.
            self._release_serial_device_lease_if_quiescent()

        thread = threading.Thread(
            target=worker,
            name=f"linktester-serial-open-{generation}",
            daemon=True,
        )
        with self._serial_worker_condition:
            self._serial_open_workers.add(thread)
            try:
                thread.start()
            except BaseException:
                self._serial_open_workers.discard(thread)
                self._serial_open_attempts.discard(generation)
                self._serial_worker_condition.notify_all()
                raise

        deadline = time.monotonic() + _SERIAL_OPEN_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            thread.join(timeout=max(0.0, min(0.1, remaining)))
            with self._serial_worker_condition:
                if thread not in self._serial_open_workers:
                    iface = result["iface"]
                    error = result["error"]
                    break
                if not self._active:
                    cancelled.set()
                    if generation == self._serial_open_generation:
                        self._serial_open_generation += 1
                    raise RuntimeError("Link tester stopped during serial open")
                if remaining <= 0:
                    cancelled.set()
                    if generation == self._serial_open_generation:
                        self._serial_open_generation += 1
                    self._abandoned_serial_open_workers.add(thread)
                    record_hung_worker()
                    raise TimeoutError(
                        f"SerialInterface open on {self._serial_port} timed out "
                        f"after {_SERIAL_OPEN_TIMEOUT}s"
                    )

        with self._serial_worker_condition:
            stale = generation != self._serial_open_generation or not self._active
            if error is None and iface is None:
                error = ConnectionError(
                    f"SerialInterface open on {self._serial_port} returned no interface"
                )
            if error is None and not stale:
                self._interface = iface
                self._connected = True
            self._serial_open_attempts.discard(generation)
            self._serial_worker_condition.notify_all()

        if stale:
            if iface is not None:
                self._close_serial_handle_bounded(iface, "stale serial generation")
            self._release_serial_device_lease_if_quiescent()
            raise RuntimeError("Serial open completed after its generation was cancelled")
        if error is not None:
            raise error

        self.log.info("Connected to %s", self._serial_port)
        self.event_bus.publish(events.LINK_TEST_CONNECTION_CHANGED, {"connected": True})

    def _close_interface(self) -> None:
        with self._serial_worker_condition:
            self._serial_send_generation += 1
            iface = self._interface
            if iface is not None:
                # Publish the teardown barrier before detaching the handle so
                # no reconnect thread can slip into a replacement open.
                self._unresolved_serial_handles[id(iface)] = iface
            self._interface = None
            self._connected = False

        if iface is not None:
            self._close_serial_handle_bounded(iface, "Link Tester serial interface")

    def _disconnect_failed_interface(self, iface: Any, error: BaseException) -> None:
        """Fence and retire the exact interface whose serial send failed."""

        with self._serial_worker_condition:
            if not self._active or self._interface is not iface:
                return
            # Fence both a late serial constructor and every callback/result
            # associated with this failed handle before making reconnect
            # eligible to publish a replacement.
            self._serial_open_generation += 1
            self._serial_send_generation += 1
            self._unresolved_serial_handles[id(iface)] = iface
            self._interface = None
            self._connected = False
            self._status = "error"
            self._serial_worker_condition.notify_all()

        self._close_serial_handle_bounded(iface, "failed Link Tester serial interface")
        self.event_bus.publish(
            events.LINK_TEST_CONNECTION_CHANGED,
            {
                "connected": False,
                "error": str(error),
            },
        )

    def _close_serial_handle_once(self, iface: Any, context: str) -> bool:
        """Attempt one close and retain the exact handle unless it is proven closed."""

        handle_id = id(iface)
        try:
            iface.close()
        except BaseException:
            with self._serial_worker_condition:
                self._unresolved_serial_handles[handle_id] = iface
                self._serial_worker_condition.notify_all()
            self.log.warning("Error closing %s; retaining device ownership", context, exc_info=True)
            return False

        with self._serial_worker_condition:
            self._unresolved_serial_handles.pop(handle_id, None)
            self._serial_worker_condition.notify_all()
        return True

    def _close_serial_handle_bounded(self, iface: Any, context: str) -> bool:
        """Close outside lifecycle threads while preserving unresolved ownership."""

        handle_id = id(iface)
        result = {"closed": False}
        with self._serial_worker_condition:
            self._unresolved_serial_handles[handle_id] = iface
            if handle_id in self._serial_close_attempts:
                return False
            self._serial_close_attempts.add(handle_id)

        def close_worker() -> None:
            result["closed"] = self._close_serial_handle_once(iface, context)
            with self._serial_worker_condition:
                self._serial_close_attempts.discard(handle_id)
                self._serial_worker_condition.notify_all()
            self._release_serial_device_lease_if_quiescent()

        thread = threading.Thread(
            target=close_worker,
            name=f"linktester-serial-close-{handle_id}",
            daemon=True,
        )
        try:
            thread.start()
        except BaseException:
            with self._serial_worker_condition:
                self._serial_close_attempts.discard(handle_id)
                self._serial_worker_condition.notify_all()
            record_hung_worker()
            self.log.exception("Could not start %s close worker", context)
            return False
        thread.join(timeout=_SERIAL_CLOSE_TIMEOUT)
        if thread.is_alive():
            record_hung_worker()
            self.log.warning(
                "%s close exceeded %.1fs; retaining device ownership",
                context,
                _SERIAL_CLOSE_TIMEOUT,
            )
            return False
        return result["closed"]

    def _retry_unresolved_serial_handles(self) -> bool:
        """Retry proven-failed closes before allowing any replacement open."""

        with self._serial_worker_condition:
            if self._serial_close_attempts:
                return False
            handles = list(self._unresolved_serial_handles.values())
        for iface in handles:
            if not self._close_serial_handle_bounded(iface, "unresolved Link Tester handle"):
                return False
        with self._serial_worker_condition:
            return not self._serial_close_attempts and not self._unresolved_serial_handles

    def _send_data_bounded(self, iface: Any, send_kwargs: dict[str, Any]) -> Any:
        """Bound a blocking SDK send and keep the tty exclusive while it is unresolved."""

        result: dict[str, Any] = {"packet": None, "error": None}
        cancelled = threading.Event()
        with self._serial_worker_condition:
            if not self._active:
                raise RuntimeError("Link Tester is stopping; refusing serial send")
            if self._serial_send_attempts or self._serial_send_workers:
                raise RuntimeError("Another Link Tester serial send is unresolved")
            self._serial_send_generation += 1
            generation = self._serial_send_generation
            self._serial_send_attempts.add(generation)

        def worker() -> None:
            packet = None
            error: BaseException | None = None
            try:
                packet = iface.sendData(**send_kwargs)
            except BaseException as exc:
                error = exc
            with self._serial_worker_condition:
                stale = cancelled.is_set() or generation != self._serial_send_generation
                if not stale:
                    result["packet"] = packet
                    result["error"] = error
                self._serial_send_workers.discard(thread)
                self._abandoned_serial_send_workers.discard(thread)
                self._serial_send_attempts.discard(generation)
                self._serial_worker_condition.notify_all()
            self._release_serial_device_lease_if_quiescent()

        thread = threading.Thread(
            target=worker,
            name=f"linktester-serial-send-{generation}",
            daemon=True,
        )
        with self._serial_worker_condition:
            self._serial_send_workers.add(thread)
            try:
                thread.start()
            except BaseException:
                self._serial_send_workers.discard(thread)
                self._serial_send_attempts.discard(generation)
                self._serial_worker_condition.notify_all()
                raise

        deadline = time.monotonic() + _SERIAL_SEND_TIMEOUT
        timed_out = False
        while True:
            remaining = deadline - time.monotonic()
            thread.join(timeout=max(0.0, min(0.1, remaining)))
            with self._serial_worker_condition:
                if thread not in self._serial_send_workers:
                    break
                if not self._active:
                    cancelled.set()
                    if generation == self._serial_send_generation:
                        self._serial_send_generation += 1
                    raise RuntimeError("Link Tester stopped during serial send")
                if remaining <= 0:
                    cancelled.set()
                    if generation == self._serial_send_generation:
                        self._serial_send_generation += 1
                    self._abandoned_serial_send_workers.add(thread)
                    self._connected = False
                    self._status = "error"
                    # Establish a teardown barrier before the send worker can
                    # return and clear its attempt token.
                    self._unresolved_serial_handles[id(iface)] = iface
                    timed_out = True
                    break

        if timed_out:
            record_hung_worker()
            self._close_interface()
            raise TimeoutError(f"Link Tester serial send timed out after {_SERIAL_SEND_TIMEOUT}s")

        error = result["error"]
        if error is not None:
            self._disconnect_failed_interface(iface, error)
            raise error
        return result["packet"]

    # ── Probe send/receive ─────────────────────────────────────────

    def _probe_loop(self, target: str, count: int, generation: int) -> None:
        seq = 0
        try:
            while self._test_generation_is_active(generation):
                if count > 0 and seq >= count:
                    break
                try:
                    self._send_probe(target, seq, generation)
                except Exception as exc:
                    self.log.warning("Probe send failed: %s", exc)
                    with self._lock:
                        if not self._connected:
                            break
                seq += 1
                self._test_stop_event.wait(timeout=self._probe_interval)
        finally:
            with self._lock:
                publish_finished = self._test_generation == generation
                if publish_finished:
                    self._test_running = False
        if publish_finished:
            stats = self._compute_stats()
            self.event_bus.publish(events.LINK_TEST_STOPPED, stats)
            self.log.info("Probe loop finished (%d sent)", seq)

    def _test_generation_is_active(self, generation: int) -> bool:
        with self._lock:
            return (
                self._active
                and self._test_running
                and self._test_generation == generation
                and not self._test_stop_event.is_set()
            )

    def _send_probe(self, target: str, seq: int, generation: int | None = None) -> None:
        with self._lock:
            iface = self._interface
            if iface is None:
                raise RuntimeError("No interface")
            if generation is None:
                generation = self._test_generation
            if self._test_generation != generation or not self._test_running:
                raise RuntimeError("Link test generation is no longer active")

        payload = f"{self._probe_prefix}:{seq:04d}".encode("utf-8")
        send_mono = time.monotonic()
        send_wall = time.time()

        from meshtastic.protobuf import portnums_pb2

        send_kwargs: dict[str, Any] = {
            "data": payload,
            "destinationId": target,
            "portNum": portnums_pb2.PortNum.TEXT_MESSAGE_APP,
            "wantAck": True,
            "wantResponse": False,
            "onResponse": self._make_probe_callback(seq, send_mono, send_wall, generation),
            "onResponseAckPermitted": True,
            "channelIndex": self._channel_index,
        }
        if self._hop_limit is not None:
            send_kwargs["hopLimit"] = self._hop_limit

        staging_key = (generation, seq)
        with self._lock:
            self._pending_probe_sends.add(staging_key)
        try:
            packet = self._send_data_bounded(iface, send_kwargs)
        except BaseException:
            with self._lock:
                self._pending_probe_sends.discard(staging_key)
                self._early_probe_acks.pop(staging_key, None)
            raise

        packet_id = (
            packet.id if hasattr(packet, "id") else getattr(packet, "get", lambda k, d: d)("id", 0)
        )

        with self._lock:
            if self._test_generation != generation or not self._test_running:
                self._pending_probe_sends.discard(staging_key)
                self._early_probe_acks.pop(staging_key, None)
                return
            self._pending_probes[packet_id] = (send_mono, send_wall, seq, generation)
            self._probes_sent += 1
            self._current_sequence = seq + 1
            self._pending_probe_sends.discard(staging_key)
            early_ack = self._early_probe_acks.pop(staging_key, None)

        if early_ack is not None:
            self._make_probe_callback(seq, send_mono, send_wall, generation)(early_ack)

    def _make_probe_callback(
        self,
        seq: int,
        send_mono: float,
        send_wall: float,
        generation: int | None = None,
    ):
        if generation is None:
            with self._lock:
                generation = self._test_generation

        def on_response(packet: dict) -> None:
            correlated = self._parse_routing_response(packet)
            if correlated is None:
                return
            request_id, error_reason = correlated
            recv_mono = time.monotonic()
            rtt_ms = round((recv_mono - send_mono) * 1000, 1)

            rssi = packet.get("rxRssi")
            snr = packet.get("rxSnr")

            acknowledged = error_reason == "NONE"
            result = {
                "seq": seq,
                "time": send_wall,
                "rtt_ms": rtt_ms if acknowledged else None,
                "rssi": rssi,
                "snr": snr,
                "status": "ack" if acknowledged else "nak",
                "error_reason": None if acknowledged else error_reason,
            }

            with self._lock:
                if (
                    not self._active
                    or self._test_generation != generation
                    or not self._test_running
                ):
                    return
                pending = self._pending_probes.get(request_id)
                if pending is None:
                    staging_key = (generation, seq)
                    if staging_key in self._pending_probe_sends:
                        self._early_probe_acks.setdefault(staging_key, dict(packet))
                    return
                if pending[2] != seq or pending[3] != generation:
                    return
                self._pending_probes.pop(request_id)
                if acknowledged:
                    self._probes_acked += 1
                else:
                    self._probes_lost += 1
                # History append is the callback's linearization point.  A
                # concurrent stop admitted after this locked commit observes
                # the complete result rather than a half-committed callback.
                self._history.append(result)

            self.event_bus.publish(events.LINK_TEST_PROBE_RESULT, result)

        return on_response

    @staticmethod
    def _parse_routing_response(packet: Any) -> tuple[int, str] | None:
        """Return the outgoing request ID and normalized routing result.

        Meshtastic response packets have their own top-level ``id``. The SDK
        correlates ``onResponse`` callbacks using ``decoded.requestId``, which
        names the outgoing probe. Accept only an actual routing response so an
        unrelated application packet cannot become a false ACK.
        """

        if not isinstance(packet, dict):
            return None
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict) or decoded.get("portnum") != "ROUTING_APP":
            return None
        request_id = decoded.get("requestId")
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            return None
        if not 0 <= request_id <= 0xFFFFFFFF:
            return None
        routing = decoded.get("routing")
        if not isinstance(routing, dict):
            return None
        if "errorReason" not in routing:
            error_reason = "NONE"
        elif (raw_error := routing.get("errorReason")) == 0:
            error_reason = "NONE"
        elif isinstance(raw_error, str) and raw_error:
            error_reason = raw_error.upper()
        else:
            return None
        return request_id, error_reason

    def _timeout_loop(self) -> None:
        while self._active:
            self._sleep_while_active(_TIMEOUT_SWEEP_INTERVAL)
            self._sweep_timeouts()

    def _sweep_timeouts(self) -> None:
        now = time.monotonic()
        timed_out: list[tuple[int, float, int]] = []

        with self._lock:
            for pkt_id, (send_mono, send_wall, seq, generation) in list(
                self._pending_probes.items()
            ):
                if generation == self._test_generation and now - send_mono > self._probe_timeout:
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
            self.event_bus.publish(
                events.LINK_TEST_PROBE_RESULT,
                {
                    "seq": seq,
                    "time": send_wall,
                    "rtt_ms": None,
                    "rssi": None,
                    "snr": None,
                    "status": "lost",
                },
            )

    # ── Statistics ─────────────────────────────────────────────────

    def _compute_stats(self) -> dict[str, Any]:
        with self._lock:
            return self._compute_stats_unlocked()

    @staticmethod
    def _percentile(sorted_vals: list[float], pct: float) -> float:
        """Compute percentile from a pre-sorted list (nearest-rank method)."""
        idx = max(0, min(int(len(sorted_vals) * pct / 100.0 + 0.5) - 1, len(sorted_vals) - 1))
        return sorted_vals[idx]

    def _compute_stats_unlocked(self) -> dict[str, Any]:
        sent = self._probes_sent
        acked = self._probes_acked
        lost = self._probes_lost
        loss_pct = round(lost / sent * 100, 1) if sent > 0 else 0.0

        rtts = [r["rtt_ms"] for r in self._history if r["rtt_ms"] is not None]
        rssis = [r["rssi"] for r in self._history if r["rssi"] is not None]
        snrs = [r["snr"] for r in self._history if r["snr"] is not None]

        # RTT percentiles
        sorted_rtts = sorted(rtts) if rtts else []
        rtt_p50 = round(self._percentile(sorted_rtts, 50), 1) if sorted_rtts else None
        rtt_p95 = round(self._percentile(sorted_rtts, 95), 1) if sorted_rtts else None
        rtt_p99 = round(self._percentile(sorted_rtts, 99), 1) if sorted_rtts else None

        # Jitter: mean absolute deviation of RTT
        if rtts:
            rtt_mean = sum(rtts) / len(rtts)
            jitter_ms = round(sum(abs(r - rtt_mean) for r in rtts) / len(rtts), 1)
        else:
            jitter_ms = None

        return {
            "sent": sent,
            "acked": acked,
            "lost": lost,
            "loss_pct": loss_pct,
            "rtt_min": round(min(rtts), 1) if rtts else None,
            "rtt_avg": round(sum(rtts) / len(rtts), 1) if rtts else None,
            "rtt_max": round(max(rtts), 1) if rtts else None,
            "rtt_p50": rtt_p50,
            "rtt_p95": rtt_p95,
            "rtt_p99": rtt_p99,
            "rtt_jitter_ms": jitter_ms,
            "rssi_avg": round(sum(rssis) / len(rssis), 1) if rssis else None,
            "rssi_min": round(min(rssis), 1) if rssis else None,
            "rssi_max": round(max(rssis), 1) if rssis else None,
            "snr_avg": round(sum(snrs) / len(snrs), 1) if snrs else None,
            "snr_min": round(min(snrs), 1) if snrs else None,
            "snr_max": round(max(snrs), 1) if snrs else None,
        }
