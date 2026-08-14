"""Bounded, correlated health checks for a local Meshtastic serial interface.

Meshtastic Python 2.7.10 does not expose a public, correlated local-health
request.  ``getMyNodeInfo()`` is cache-only and ``sendHeartbeat()`` has no
response callback.  This module contains the narrow compatibility adapter we
need until upstream provides such an API: it sends a read-only device metadata
admin request to the attached node and accepts only the matching metadata
response as verified health.

The Meshtastic dependency is optional, so it is imported lazily.  Callers must
still serialize this adapter with other writes to the same physical interface;
the adapter only serializes health probes that it owns.
"""

from __future__ import annotations

import importlib.metadata
import logging
import math
import threading
import time
from collections.abc import Callable, Hashable, Mapping, MutableMapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

log = logging.getLogger(__name__)

SUPPORTED_MESHTASTIC_VERSION: Final = "2.7.10"
_UINT32_MAX: Final = 0xFFFFFFFF


class MeshtasticHealthOutcome(str, Enum):
    """Terminal outcomes from a local Meshtastic health probe."""

    VERIFIED = "verified"
    ALIVE_PROTOCOL_ERROR = "alive_protocol_error"
    INCONCLUSIVE = "inconclusive"
    TIMEOUT = "timeout"
    TRANSPORT_ERROR = "transport_error"
    STALE_GENERATION = "stale_generation"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class MeshtasticDeviceMetadata:
    """Strictly validated identity fields returned by the local radio."""

    firmware_version: str
    device_state_version: int
    hardware_model: int


@dataclass(frozen=True, slots=True)
class MeshtasticHealthResult:
    """Result of a bounded local-radio health transaction."""

    outcome: MeshtasticHealthOutcome
    detail: str
    packet_id: int | None = None
    metadata: MeshtasticDeviceMetadata | None = None
    protocol_error: str | None = None
    elapsed_seconds: float = 0.0

    @property
    def verified(self) -> bool:
        return self.outcome is MeshtasticHealthOutcome.VERIFIED


@dataclass(frozen=True, slots=True)
class _ProbeSpec:
    expected_node_num: int
    expected_hardware_model: int | None


@dataclass(slots=True)
class _ProbeFlight:
    interface: Any
    generation: Hashable
    spec: _ProbeSpec
    is_current: Callable[[Any, Hashable], bool]
    started: float
    deadline: float
    done: threading.Event = field(default_factory=threading.Event)
    result: MeshtasticHealthResult | None = None
    packet_id: int | None = None
    callback: Callable[[dict[str, Any]], None] | None = None
    worker: threading.Thread | None = None


class MeshtasticHealthAdapter:
    """Run exact-2.7.10-compatible, correlated local metadata probes.

    One worker is shared by concurrent callers for the same interface and
    generation.  ``max_workers`` bounds workers that can remain blocked inside
    the third-party library, whose send path is not cancellable.
    """

    def __init__(
        self,
        *,
        max_workers: int = 1,
        supported_version: str = SUPPORTED_MESHTASTIC_VERSION,
        version_getter: Callable[[str], str] | None = None,
        admin_message_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        if not supported_version:
            raise ValueError("supported_version must not be empty")

        self._supported_version = supported_version
        self._version_getter = version_getter or importlib.metadata.version
        self._admin_message_factory = admin_message_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._worker_slots = threading.BoundedSemaphore(max_workers)
        self._inflight: dict[tuple[int, Hashable], _ProbeFlight] = {}

    def probe(
        self,
        interface: Any,
        generation: Hashable,
        *,
        is_current: Callable[[Any, Hashable], bool],
        timeout: float = 5.0,
        expected_node_num: int | None = None,
        expected_hardware_model: int | None = None,
    ) -> MeshtasticHealthResult:
        """Perform one bounded, non-mutating local metadata transaction.

        ``is_current`` is checked before sending, after receiving, and before a
        waiter consumes the result.  This prevents a late response from an old
        interface generation from restoring health.
        """

        started = self._clock()
        timeout_value = self._validate_timeout(timeout)
        if timeout_value is None:
            return self._result(
                MeshtasticHealthOutcome.UNSUPPORTED,
                "invalid_timeout",
                started=started,
            )
        if not callable(is_current):
            return self._result(
                MeshtasticHealthOutcome.UNSUPPORTED,
                "is_current_must_be_callable",
                started=started,
            )
        try:
            hash(generation)
        except (TypeError, ValueError):
            return self._result(
                MeshtasticHealthOutcome.UNSUPPORTED,
                "generation_must_be_hashable",
                started=started,
            )

        current = self._generation_is_current(is_current, interface, generation)
        if current is None:
            return self._result(
                MeshtasticHealthOutcome.UNSUPPORTED,
                "generation_predicate_failed",
                started=started,
            )
        if not current:
            return self._result(
                MeshtasticHealthOutcome.STALE_GENERATION,
                "generation_is_not_current",
                started=started,
            )

        support_error = self._support_error()
        if support_error is not None:
            return self._result(
                MeshtasticHealthOutcome.UNSUPPORTED,
                support_error,
                started=started,
            )

        local_node = getattr(interface, "localNode", None)
        if local_node is None or not callable(getattr(local_node, "_sendAdmin", None)):
            return self._result(
                MeshtasticHealthOutcome.UNSUPPORTED,
                "meshtastic_2_7_10_send_admin_api_unavailable",
                started=started,
            )

        node_num = self._uint32(
            expected_node_num
            if expected_node_num is not None
            else getattr(local_node, "nodeNum", None)
        )
        if node_num is None:
            return self._result(
                MeshtasticHealthOutcome.UNSUPPORTED,
                "local_node_number_invalid",
                started=started,
            )
        if expected_hardware_model is not None:
            expected_hardware_model = self._positive_int(expected_hardware_model)
            if expected_hardware_model is None:
                return self._result(
                    MeshtasticHealthOutcome.UNSUPPORTED,
                    "expected_hardware_model_invalid",
                    started=started,
                )

        spec = _ProbeSpec(node_num, expected_hardware_model)
        key = (id(interface), generation)

        with self._lock:
            flight = self._inflight.get(key)
            if flight is not None:
                if flight.interface is not interface or flight.spec != spec:
                    return self._result(
                        MeshtasticHealthOutcome.UNSUPPORTED,
                        "conflicting_probe_for_interface_generation",
                        started=started,
                    )
            else:
                queue_outcome = self._queue_precheck(interface, started)
                if queue_outcome is not None:
                    return queue_outcome
                if not self._worker_slots.acquire(blocking=False):
                    return self._result(
                        MeshtasticHealthOutcome.INCONCLUSIVE,
                        "probe_worker_capacity_exhausted",
                        started=started,
                    )
                flight = _ProbeFlight(
                    interface=interface,
                    generation=generation,
                    spec=spec,
                    is_current=is_current,
                    started=started,
                    deadline=started + timeout_value,
                )
                self._inflight[key] = flight
                flight.worker = threading.Thread(
                    target=self._run_flight,
                    args=(key, flight),
                    name=f"meshtastic-health-{generation}",
                    daemon=True,
                )
                try:
                    flight.worker.start()
                except RuntimeError as exc:
                    self._inflight.pop(key, None)
                    self._worker_slots.release()
                    return self._result(
                        MeshtasticHealthOutcome.UNSUPPORTED,
                        f"probe_worker_start_failed:{type(exc).__name__}",
                        started=started,
                    )

        return self._wait_for_flight(flight, timeout_value, is_current)

    def compatibility_error(self) -> str | None:
        """Return why the installed dependency cannot use this exact adapter."""

        return self._support_error()

    def has_inflight(self) -> bool:
        """Whether an adapter worker can still be inside third-party code."""

        with self._lock:
            return bool(self._inflight)

    def _run_flight(self, key: tuple[int, Hashable], flight: _ProbeFlight) -> None:
        response_event = threading.Event()
        response_lock = threading.Lock()
        response: dict[str, Any] = {}

        def _on_metadata_probe_response(packet: dict[str, Any]) -> None:
            # Keep the Meshtastic reader callback minimal. Validation and the
            # caller-supplied generation predicate run on our worker instead.
            with response_lock:
                if "packet" not in response:
                    response["packet"] = packet
                    response_event.set()

        flight.callback = _on_metadata_probe_response
        result: MeshtasticHealthResult
        try:
            current = self._generation_is_current(
                flight.is_current,
                flight.interface,
                flight.generation,
            )
            if current is None:
                result = self._flight_result(
                    flight,
                    MeshtasticHealthOutcome.UNSUPPORTED,
                    "generation_predicate_failed",
                )
                return self._finish_flight(key, flight, result)
            if not current:
                result = self._flight_result(
                    flight,
                    MeshtasticHealthOutcome.STALE_GENERATION,
                    "generation_became_stale_before_send",
                )
                return self._finish_flight(key, flight, result)

            message = self._new_admin_message()
            if message is None:
                result = self._flight_result(
                    flight,
                    MeshtasticHealthOutcome.UNSUPPORTED,
                    "meshtastic_admin_message_unavailable",
                )
                return self._finish_flight(key, flight, result)
            try:
                message.get_device_metadata_request = True
            except (AttributeError, TypeError, ValueError):
                result = self._flight_result(
                    flight,
                    MeshtasticHealthOutcome.UNSUPPORTED,
                    "metadata_request_field_unavailable",
                )
                return self._finish_flight(key, flight, result)

            local_node = flight.interface.localNode
            try:
                sent_packet = local_node._sendAdmin(
                    message,
                    wantResponse=True,
                    onResponse=_on_metadata_probe_response,
                )
            except (AttributeError, NotImplementedError, TypeError) as exc:
                result = self._flight_result(
                    flight,
                    MeshtasticHealthOutcome.UNSUPPORTED,
                    f"metadata_request_api_incompatible:{type(exc).__name__}",
                )
                return self._finish_flight(key, flight, result)
            except Exception as exc:
                transport_failure = self._is_transport_exception(exc)
                result = self._flight_result(
                    flight,
                    (
                        MeshtasticHealthOutcome.TRANSPORT_ERROR
                        if transport_failure
                        else MeshtasticHealthOutcome.UNSUPPORTED
                    ),
                    (
                        "metadata_request_transport_failed:"
                        if transport_failure
                        else "metadata_request_send_failed:"
                    )
                    + type(exc).__name__,
                )
                return self._finish_flight(key, flight, result)
            except BaseException as exc:
                # Never interpret process-control exceptions as a radio hang.
                result = self._flight_result(
                    flight,
                    MeshtasticHealthOutcome.UNSUPPORTED,
                    f"metadata_request_aborted:{type(exc).__name__}",
                )
                return self._finish_flight(key, flight, result)

            flight.packet_id = self._uint32(getattr(sent_packet, "id", None))
            if flight.packet_id is None:
                result = self._flight_result(
                    flight,
                    MeshtasticHealthOutcome.UNSUPPORTED,
                    "metadata_request_packet_id_invalid",
                )
                return self._finish_flight(key, flight, result)

            remaining = max(0.0, flight.deadline - self._clock())
            if not response_event.wait(timeout=remaining):
                result = self._flight_result(
                    flight,
                    MeshtasticHealthOutcome.TIMEOUT,
                    "metadata_response_timeout",
                )
                return self._finish_flight(key, flight, result)

            current = self._generation_is_current(
                flight.is_current,
                flight.interface,
                flight.generation,
            )
            if current is None:
                result = self._flight_result(
                    flight,
                    MeshtasticHealthOutcome.UNSUPPORTED,
                    "generation_predicate_failed",
                )
            elif not current:
                result = self._flight_result(
                    flight,
                    MeshtasticHealthOutcome.STALE_GENERATION,
                    "metadata_response_from_stale_generation",
                )
            else:
                result = self._validate_response(flight, response.get("packet"))
            return self._finish_flight(key, flight, result)
        except BaseException as exc:  # defensive boundary around an optional dependency
            log.debug("Unexpected Meshtastic health adapter failure", exc_info=True)
            result = self._flight_result(
                flight,
                MeshtasticHealthOutcome.UNSUPPORTED,
                f"health_adapter_failure:{type(exc).__name__}",
            )
            return self._finish_flight(key, flight, result)

    def _finish_flight(
        self,
        key: tuple[int, Hashable],
        flight: _ProbeFlight,
        result: MeshtasticHealthResult,
    ) -> None:
        try:
            self._remove_own_response_handler(flight)
        except BaseException:
            # Cleanup is best-effort, but completion and slot release are not.
            # Swallow here so the worker's outer boundary cannot finish twice.
            log.debug("Meshtastic response-handler cleanup failed", exc_info=True)
        finally:
            # Never strand waiters or worker capacity because optional-library
            # cleanup misbehaved. The worker boundary must remain one-shot.
            flight.result = result
            try:
                with self._lock:
                    if self._inflight.get(key) is flight:
                        self._inflight.pop(key, None)
                self._worker_slots.release()
            finally:
                # Signal only after the slot is reusable. A caller that sees a
                # terminal result can immediately start the next generation.
                flight.done.set()

    def _wait_for_flight(
        self,
        flight: _ProbeFlight,
        timeout: float,
        is_current: Callable[[Any, Hashable], bool],
    ) -> MeshtasticHealthResult:
        waiter_deadline = self._clock() + timeout
        wait_until = min(waiter_deadline, flight.deadline)
        flight.done.wait(timeout=max(0.0, wait_until - self._clock()))

        current = self._generation_is_current(is_current, flight.interface, flight.generation)
        if current is None:
            self._remove_own_response_handler(flight)
            return self._flight_result(
                flight,
                MeshtasticHealthOutcome.UNSUPPORTED,
                "generation_predicate_failed",
            )
        if not current:
            self._remove_own_response_handler(flight)
            return self._flight_result(
                flight,
                MeshtasticHealthOutcome.STALE_GENERATION,
                "generation_became_stale_while_waiting",
            )
        if flight.done.is_set() and flight.result is not None:
            return flight.result

        # If the owning deadline expired, remove only our callback.  This is
        # safe even when _sendAdmin() is still blocked because cleanup scans by
        # callback identity and never removes a replacement handler.
        if self._clock() >= flight.deadline:
            self._remove_own_response_handler(flight)
        return self._flight_result(
            flight,
            MeshtasticHealthOutcome.TIMEOUT,
            "probe_deadline_exceeded",
        )

    def _validate_response(
        self,
        flight: _ProbeFlight,
        packet: Any,
    ) -> MeshtasticHealthResult:
        if not isinstance(packet, Mapping):
            return self._flight_result(
                flight,
                MeshtasticHealthOutcome.UNSUPPORTED,
                "metadata_response_not_a_mapping",
            )

        source = self._uint32(packet.get("from"))
        if source != flight.spec.expected_node_num:
            return self._flight_result(
                flight,
                MeshtasticHealthOutcome.UNSUPPORTED,
                "metadata_response_source_mismatch",
            )
        decoded = packet.get("decoded")
        if not isinstance(decoded, Mapping):
            return self._flight_result(
                flight,
                MeshtasticHealthOutcome.UNSUPPORTED,
                "metadata_response_decoded_payload_invalid",
            )
        request_id = self._uint32(decoded.get("requestId"))
        if request_id != flight.packet_id:
            return self._flight_result(
                flight,
                MeshtasticHealthOutcome.UNSUPPORTED,
                "metadata_response_request_id_mismatch",
            )

        portnum = decoded.get("portnum")
        if portnum == "ROUTING_APP":
            routing = decoded.get("routing")
            if not isinstance(routing, Mapping):
                return self._flight_result(
                    flight,
                    MeshtasticHealthOutcome.UNSUPPORTED,
                    "routing_response_payload_invalid",
                )
            error_reason = routing.get("errorReason")
            if not isinstance(error_reason, str) or not error_reason or error_reason == "NONE":
                return self._flight_result(
                    flight,
                    MeshtasticHealthOutcome.UNSUPPORTED,
                    "unexpected_ack_without_metadata",
                )
            return self._flight_result(
                flight,
                MeshtasticHealthOutcome.ALIVE_PROTOCOL_ERROR,
                "metadata_request_nak",
                protocol_error=error_reason,
            )

        if portnum != "ADMIN_APP":
            return self._flight_result(
                flight,
                MeshtasticHealthOutcome.UNSUPPORTED,
                "metadata_response_port_invalid",
            )
        admin = decoded.get("admin")
        if not isinstance(admin, Mapping):
            return self._flight_result(
                flight,
                MeshtasticHealthOutcome.UNSUPPORTED,
                "metadata_admin_payload_invalid",
            )
        raw = admin.get("raw")
        has_field = getattr(raw, "HasField", None)
        if not callable(has_field):
            return self._flight_result(
                flight,
                MeshtasticHealthOutcome.UNSUPPORTED,
                "metadata_admin_raw_message_invalid",
            )
        try:
            contains_metadata = has_field("get_device_metadata_response")
        except (TypeError, ValueError):
            contains_metadata = False
        if contains_metadata is not True:
            return self._flight_result(
                flight,
                MeshtasticHealthOutcome.UNSUPPORTED,
                "metadata_response_field_missing",
            )

        metadata = getattr(raw, "get_device_metadata_response", None)
        firmware_version = getattr(metadata, "firmware_version", None)
        if not isinstance(firmware_version, str) or not firmware_version.strip():
            return self._flight_result(
                flight,
                MeshtasticHealthOutcome.UNSUPPORTED,
                "metadata_firmware_version_invalid",
            )
        device_state_version = self._nonnegative_int(
            getattr(metadata, "device_state_version", None)
        )
        if device_state_version is None:
            return self._flight_result(
                flight,
                MeshtasticHealthOutcome.UNSUPPORTED,
                "metadata_device_state_version_invalid",
            )
        hardware_model = self._positive_int(getattr(metadata, "hw_model", None))
        if hardware_model is None:
            return self._flight_result(
                flight,
                MeshtasticHealthOutcome.UNSUPPORTED,
                "metadata_hardware_model_invalid",
            )
        if (
            flight.spec.expected_hardware_model is not None
            and hardware_model != flight.spec.expected_hardware_model
        ):
            return self._flight_result(
                flight,
                MeshtasticHealthOutcome.UNSUPPORTED,
                "metadata_hardware_model_mismatch",
            )

        return self._flight_result(
            flight,
            MeshtasticHealthOutcome.VERIFIED,
            "correlated_local_metadata_response",
            metadata=MeshtasticDeviceMetadata(
                firmware_version=firmware_version.strip(),
                device_state_version=device_state_version,
                hardware_model=hardware_model,
            ),
        )

    def _queue_precheck(
        self,
        interface: Any,
        started: float,
    ) -> MeshtasticHealthResult | None:
        queue_status = getattr(interface, "queueStatus", None)
        if queue_status is None:
            return None
        free = self._nonnegative_int(getattr(queue_status, "free", None))
        if free is None:
            return self._result(
                MeshtasticHealthOutcome.UNSUPPORTED,
                "tx_queue_status_invalid",
                started=started,
            )
        if free == 0:
            return self._result(
                MeshtasticHealthOutcome.INCONCLUSIVE,
                "tx_queue_has_no_free_space",
                started=started,
            )
        return None

    def _remove_own_response_handler(self, flight: _ProbeFlight) -> None:
        callback = flight.callback
        if callback is None:
            return
        handlers = getattr(flight.interface, "responseHandlers", None)
        if not isinstance(handlers, MutableMapping):
            return

        try:
            if flight.packet_id is not None:
                handler = handlers.get(flight.packet_id)
                if getattr(handler, "callback", None) is callback:
                    handlers.pop(flight.packet_id, None)
                    return
            # _sendAdmin registers before entering its potentially blocking TX
            # queue. If it has not returned an ID yet, find only our callback.
            for request_id, handler in list(handlers.items()):
                if getattr(handler, "callback", None) is callback:
                    handlers.pop(request_id, None)
        except (AttributeError, RuntimeError, TypeError):
            log.debug("Could not clean Meshtastic response handler", exc_info=True)

    def _new_admin_message(self) -> Any | None:
        try:
            if self._admin_message_factory is not None:
                return self._admin_message_factory()
            from meshtastic.protobuf import admin_pb2

            return admin_pb2.AdminMessage()
        except (ImportError, AttributeError, RuntimeError, TypeError):
            return None

    def _support_error(self) -> str | None:
        try:
            installed = self._version_getter("meshtastic")
        except (importlib.metadata.PackageNotFoundError, ImportError, RuntimeError, ValueError):
            return "meshtastic_distribution_unavailable"
        if installed != self._supported_version:
            return f"meshtastic_version_unsupported:{installed}"
        return None

    @staticmethod
    def _is_transport_exception(exc: Exception) -> bool:
        if isinstance(exc, (ConnectionError, OSError, TimeoutError)):
            return True
        exception_type = type(exc)
        if exception_type.__module__.startswith("serial") or exception_type.__name__ in {
            "SerialException",
            "SerialTimeoutException",
        }:
            return True
        detail = str(exc).lower()
        return any(
            marker in detail
            for marker in (
                "broken pipe",
                "device disconnected",
                "not connected",
                "port is closed",
                "serial port",
                "timed out",
                "transport closed",
            )
        )

    def _generation_is_current(
        self,
        predicate: Callable[[Any, Hashable], bool],
        interface: Any,
        generation: Hashable,
    ) -> bool | None:
        try:
            result = predicate(interface, generation)
        except BaseException:
            log.debug("Meshtastic generation predicate failed", exc_info=True)
            return None
        return result if isinstance(result, bool) else None

    def _flight_result(
        self,
        flight: _ProbeFlight,
        outcome: MeshtasticHealthOutcome,
        detail: str,
        *,
        metadata: MeshtasticDeviceMetadata | None = None,
        protocol_error: str | None = None,
    ) -> MeshtasticHealthResult:
        return MeshtasticHealthResult(
            outcome=outcome,
            detail=detail,
            packet_id=flight.packet_id,
            metadata=metadata,
            protocol_error=protocol_error,
            elapsed_seconds=max(0.0, self._clock() - flight.started),
        )

    def _result(
        self,
        outcome: MeshtasticHealthOutcome,
        detail: str,
        *,
        started: float,
    ) -> MeshtasticHealthResult:
        return MeshtasticHealthResult(
            outcome=outcome,
            detail=detail,
            elapsed_seconds=max(0.0, self._clock() - started),
        )

    @staticmethod
    def _validate_timeout(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None

    @staticmethod
    def _uint32(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, str):
            if not value.isdecimal():
                return None
            value = int(value, 10)
        if not isinstance(value, int) or not 0 <= value <= _UINT32_MAX:
            return None
        return value

    @staticmethod
    def _nonnegative_int(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return None
        return value
