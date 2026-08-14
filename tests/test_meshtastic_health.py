"""Tests for the exact Meshtastic 2.7.10 local-health adapter."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import patch

import pytest

from reticulumpi.meshtastic_health import (
    MeshtasticHealthAdapter,
    MeshtasticHealthOutcome,
    MeshtasticHealthResult,
)


class _AdminMessage:
    def __init__(self) -> None:
        self.get_device_metadata_request = False


@dataclass
class _Handler:
    callback: Callable[[dict[str, Any]], None]


@dataclass
class _Packet:
    id: int


@dataclass
class _Metadata:
    firmware_version: str = "2.7.12.test"
    device_state_version: int = 24
    hw_model: int = 42


class _RawAdmin:
    def __init__(self, metadata: _Metadata | None = None, *, has_metadata: bool = True) -> None:
        self.get_device_metadata_response = metadata
        self._has_metadata = has_metadata

    def HasField(self, name: str) -> bool:
        return name == "get_device_metadata_response" and self._has_metadata


class _LocalNode:
    def __init__(
        self,
        interface: "_Interface",
        behavior: Callable[["_Interface", Callable[[dict[str, Any]], None], _Packet], None],
    ) -> None:
        self.iface = interface
        self.nodeNum = 0x12345678
        self._behavior = behavior
        self.calls: list[tuple[Any, bool, Callable[[dict[str, Any]], None]]] = []

    def _sendAdmin(
        self,
        message: Any,
        *,
        wantResponse: bool,
        onResponse: Callable[[dict[str, Any]], None],
    ) -> _Packet:
        self.calls.append((message, wantResponse, onResponse))
        packet = _Packet(0xAABBCCDD)
        self.iface.responseHandlers[packet.id] = _Handler(onResponse)
        self._behavior(self.iface, onResponse, packet)
        return packet


class _Interface:
    def __init__(
        self,
        behavior: Callable[["_Interface", Callable[[dict[str, Any]], None], _Packet], None],
        *,
        queue_free: int | None = 1,
    ) -> None:
        self.responseHandlers: dict[int, _Handler] = {}
        self.queueStatus = None if queue_free is None else SimpleNamespace(free=queue_free)
        self.localNode = _LocalNode(self, behavior)


def _adapter(*, max_workers: int = 1, version: str = "2.7.10") -> MeshtasticHealthAdapter:
    return MeshtasticHealthAdapter(
        max_workers=max_workers,
        version_getter=lambda _distribution: version,
        admin_message_factory=_AdminMessage,
    )


def _metadata_packet(
    packet_id: int,
    *,
    node_num: int = 0x12345678,
    metadata: _Metadata | None = None,
    has_metadata: bool = True,
) -> dict[str, Any]:
    return {
        "from": node_num,
        "decoded": {
            "requestId": packet_id,
            "portnum": "ADMIN_APP",
            "admin": {
                "raw": _RawAdmin(metadata or _Metadata(), has_metadata=has_metadata),
            },
        },
    }


def _nak_packet(packet_id: int, error_reason: str) -> dict[str, Any]:
    return {
        "from": 0x12345678,
        "decoded": {
            "requestId": packet_id,
            "portnum": "ROUTING_APP",
            "routing": {"errorReason": error_reason},
        },
    }


def _always_current(_interface: Any, _generation: Any) -> bool:
    return True


def _wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_verified_metadata_response_uses_exact_send_admin_contract_and_cleans_handler():
    def respond(_interface: _Interface, callback: Callable, packet: _Packet) -> None:
        # Deliberately respond synchronously, before _sendAdmin returns, to
        # exercise the fastest possible reader/caller race.
        callback(_metadata_packet(packet.id))

    interface = _Interface(respond)
    result = _adapter().probe(
        interface,
        7,
        is_current=_always_current,
        expected_node_num=0x12345678,
        expected_hardware_model=42,
        timeout=0.5,
    )

    assert result.outcome is MeshtasticHealthOutcome.VERIFIED
    assert result.verified is True
    assert result.packet_id == 0xAABBCCDD
    assert result.metadata is not None
    assert result.metadata.firmware_version == "2.7.12.test"
    assert result.metadata.device_state_version == 24
    assert result.metadata.hardware_model == 42
    assert interface.responseHandlers == {}
    [(message, want_response, callback)] = interface.localNode.calls
    assert message.get_device_metadata_request is True
    assert want_response is True
    assert callback.__name__ == "_on_metadata_probe_response"


def test_correlated_nak_is_alive_protocol_error_not_verified():
    def respond(_interface: _Interface, callback: Callable, packet: _Packet) -> None:
        callback(_nak_packet(packet.id, "NOT_AUTHORIZED"))

    result = _adapter().probe(
        _Interface(respond),
        1,
        is_current=_always_current,
        timeout=0.5,
    )

    assert result.outcome is MeshtasticHealthOutcome.ALIVE_PROTOCOL_ERROR
    assert result.protocol_error == "NOT_AUTHORIZED"
    assert result.verified is False


def test_regular_ack_without_metadata_is_not_accepted():
    def respond(_interface: _Interface, callback: Callable, packet: _Packet) -> None:
        callback(_nak_packet(packet.id, "NONE"))

    result = _adapter().probe(
        _Interface(respond),
        1,
        is_current=_always_current,
        timeout=0.5,
    )

    assert result.outcome is MeshtasticHealthOutcome.UNSUPPORTED
    assert result.detail == "unexpected_ack_without_metadata"


def test_timeout_removes_only_its_own_response_handler():
    interface = _Interface(lambda _interface, _callback, _packet: None)

    result = _adapter().probe(
        interface,
        1,
        is_current=_always_current,
        timeout=0.03,
    )

    assert result.outcome is MeshtasticHealthOutcome.TIMEOUT
    assert _wait_until(lambda: not interface.responseHandlers)


def test_cleanup_preserves_replacement_handler_with_same_packet_id():
    replacement = _Handler(lambda _packet: None)

    def replace(interface: _Interface, _callback: Callable, packet: _Packet) -> None:
        interface.responseHandlers[packet.id] = replacement

    interface = _Interface(replace)
    result = _adapter().probe(
        interface,
        1,
        is_current=_always_current,
        timeout=0.03,
    )

    assert result.outcome is MeshtasticHealthOutcome.TIMEOUT
    assert interface.responseHandlers[0xAABBCCDD] is replacement


def test_stale_generation_before_send_does_not_touch_interface():
    interface = _Interface(lambda _interface, _callback, _packet: None)

    result = _adapter().probe(
        interface,
        2,
        is_current=lambda _interface, generation: generation == 1,
        timeout=0.1,
    )

    assert result.outcome is MeshtasticHealthOutcome.STALE_GENERATION
    assert interface.localNode.calls == []


def test_response_from_generation_that_became_stale_is_rejected():
    state = {"current": True}

    def respond(_interface: _Interface, callback: Callable, packet: _Packet) -> None:
        state["current"] = False
        callback(_metadata_packet(packet.id))

    result = _adapter().probe(
        _Interface(respond),
        3,
        is_current=lambda _interface, _generation: state["current"],
        timeout=0.5,
    )

    assert result.outcome is MeshtasticHealthOutcome.STALE_GENERATION
    assert result.verified is False


def test_zero_queue_space_fails_before_starting_worker():
    interface = _Interface(lambda _interface, _callback, _packet: None, queue_free=0)

    result = _adapter().probe(
        interface,
        1,
        is_current=_always_current,
        timeout=0.1,
    )

    assert result.outcome is MeshtasticHealthOutcome.INCONCLUSIVE
    assert result.detail == "tx_queue_has_no_free_space"
    assert interface.localNode.calls == []


def test_missing_queue_status_is_compatible_with_older_firmware():
    def respond(_interface: _Interface, callback: Callable, packet: _Packet) -> None:
        callback(_metadata_packet(packet.id))

    result = _adapter().probe(
        _Interface(respond, queue_free=None),
        1,
        is_current=_always_current,
        timeout=0.5,
    )

    assert result.outcome is MeshtasticHealthOutcome.VERIFIED


def test_concurrent_callers_share_one_probe_for_same_interface_generation():
    release_response = threading.Event()
    send_entered = threading.Event()
    second_waiting = threading.Event()

    def respond(_interface: _Interface, callback: Callable, packet: _Packet) -> None:
        send_entered.set()

        def later() -> None:
            release_response.wait(timeout=5)
            callback(_metadata_packet(packet.id))

        threading.Thread(target=later, daemon=True).start()

    interface = _Interface(respond)
    adapter = _adapter()
    second_checked = threading.Event()
    original_wait_for_flight = adapter._wait_for_flight

    def wait_for_flight(*args: Any, **kwargs: Any) -> MeshtasticHealthResult:
        if threading.current_thread().name == "second-probe":
            second_waiting.set()
        return original_wait_for_flight(*args, **kwargs)

    adapter._wait_for_flight = wait_for_flight  # type: ignore[method-assign]

    def current(_interface: Any, _generation: Any) -> bool:
        if threading.current_thread().name == "second-probe":
            second_checked.set()
        return True

    results: list[Any] = []
    first = threading.Thread(
        target=lambda: results.append(adapter.probe(interface, 4, is_current=current, timeout=1.0)),
        name="first-probe",
    )
    second = threading.Thread(
        target=lambda: results.append(adapter.probe(interface, 4, is_current=current, timeout=1.0)),
        name="second-probe",
    )
    first.start()
    assert send_entered.wait(timeout=1)
    second.start()
    assert second_checked.wait(timeout=1)
    assert second_waiting.wait(timeout=1)
    release_response.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(interface.localNode.calls) == 1
    assert len(results) == 2
    assert all(result.outcome is MeshtasticHealthOutcome.VERIFIED for result in results)


def test_stuck_send_worker_consumes_bounded_capacity_without_spawning_another():
    entered = threading.Event()
    release = threading.Event()

    def block_send(_interface: _Interface, _callback: Callable, _packet: _Packet) -> None:
        entered.set()
        release.wait(timeout=1)

    first_interface = _Interface(block_send)
    second_interface = _Interface(lambda _interface, _callback, _packet: None)
    adapter = _adapter(max_workers=1)

    first_result = adapter.probe(
        first_interface,
        1,
        is_current=_always_current,
        timeout=0.03,
    )
    assert entered.is_set()
    assert first_result.outcome is MeshtasticHealthOutcome.TIMEOUT
    assert adapter.has_inflight() is True

    second_result = adapter.probe(
        second_interface,
        2,
        is_current=_always_current,
        timeout=0.03,
    )
    assert second_result.outcome is MeshtasticHealthOutcome.INCONCLUSIVE
    assert second_result.detail == "probe_worker_capacity_exhausted"
    assert second_interface.localNode.calls == []

    release.set()
    assert _wait_until(lambda: not first_interface.responseHandlers)
    assert _wait_until(lambda: not adapter.has_inflight())


@pytest.mark.parametrize(
    ("packet_factory", "detail"),
    [
        (
            lambda packet_id: _metadata_packet(packet_id + 1),
            "metadata_response_request_id_mismatch",
        ),
        (
            lambda packet_id: _metadata_packet(packet_id, node_num=0x99999999),
            "metadata_response_source_mismatch",
        ),
        (
            lambda packet_id: _metadata_packet(
                packet_id,
                metadata=_Metadata(firmware_version=""),
            ),
            "metadata_firmware_version_invalid",
        ),
        (
            lambda packet_id: _metadata_packet(
                packet_id,
                metadata=_Metadata(hw_model=0),
            ),
            "metadata_hardware_model_invalid",
        ),
        (
            lambda packet_id: _metadata_packet(packet_id, has_metadata=False),
            "metadata_response_field_missing",
        ),
    ],
)
def test_strict_response_validation(packet_factory: Callable, detail: str):
    def respond(_interface: _Interface, callback: Callable, packet: _Packet) -> None:
        callback(packet_factory(packet.id))

    result = _adapter().probe(
        _Interface(respond),
        1,
        is_current=_always_current,
        timeout=0.5,
    )

    assert result.outcome is MeshtasticHealthOutcome.UNSUPPORTED
    assert result.detail == detail


def test_expected_hardware_model_must_match_response():
    def respond(_interface: _Interface, callback: Callable, packet: _Packet) -> None:
        callback(_metadata_packet(packet.id))

    result = _adapter().probe(
        _Interface(respond),
        1,
        is_current=_always_current,
        expected_hardware_model=99,
        timeout=0.5,
    )

    assert result.outcome is MeshtasticHealthOutcome.UNSUPPORTED
    assert result.detail == "metadata_hardware_model_mismatch"


def test_non_exact_dependency_version_is_unsupported_without_sending():
    interface = _Interface(lambda _interface, _callback, _packet: None)

    result = _adapter(version="2.7.11").probe(
        interface,
        1,
        is_current=_always_current,
        timeout=0.1,
    )

    assert result.outcome is MeshtasticHealthOutcome.UNSUPPORTED
    assert result.detail == "meshtastic_version_unsupported:2.7.11"
    assert interface.localNode.calls == []


def test_missing_exact_send_admin_api_is_unsupported():
    interface = SimpleNamespace(
        localNode=SimpleNamespace(nodeNum=0x12345678),
        queueStatus=SimpleNamespace(free=1),
        responseHandlers={},
    )

    result = _adapter().probe(
        interface,
        1,
        is_current=_always_current,
        timeout=0.1,
    )

    assert result.outcome is MeshtasticHealthOutcome.UNSUPPORTED
    assert result.detail == "meshtastic_2_7_10_send_admin_api_unavailable"


def test_system_exit_from_optional_library_does_not_strand_worker_capacity():
    def exit_send(_interface: _Interface, _callback: Callable, _packet: _Packet) -> None:
        raise SystemExit(1)

    adapter = _adapter(max_workers=1)
    failed = adapter.probe(
        _Interface(exit_send),
        1,
        is_current=_always_current,
        timeout=0.1,
    )
    assert failed.outcome is MeshtasticHealthOutcome.UNSUPPORTED
    assert failed.detail == "metadata_request_aborted:SystemExit"

    def respond(_interface: _Interface, callback: Callable, packet: _Packet) -> None:
        callback(_metadata_packet(packet.id))

    recovered = adapter.probe(
        _Interface(respond),
        2,
        is_current=_always_current,
        timeout=0.5,
    )
    assert recovered.outcome is MeshtasticHealthOutcome.VERIFIED


def test_send_transport_failure_is_distinct_from_static_api_incompatibility():
    def transport_failure(
        _interface: _Interface,
        _callback: Callable,
        _packet: _Packet,
    ) -> None:
        raise OSError("serial link vanished")

    transport = _adapter().probe(
        _Interface(transport_failure),
        1,
        is_current=_always_current,
        timeout=0.1,
    )
    assert transport.outcome is MeshtasticHealthOutcome.TRANSPORT_ERROR
    assert transport.detail == "metadata_request_transport_failed:OSError"

    def incompatible_api(
        _interface: _Interface,
        _callback: Callable,
        _packet: _Packet,
    ) -> None:
        raise TypeError("signature changed")

    unsupported = _adapter().probe(
        _Interface(incompatible_api),
        2,
        is_current=_always_current,
        timeout=0.1,
    )
    assert unsupported.outcome is MeshtasticHealthOutcome.UNSUPPORTED
    assert unsupported.detail == "metadata_request_api_incompatible:TypeError"


def test_compatibility_and_inflight_state_are_observable():
    adapter = _adapter(version="2.7.11")
    assert adapter.compatibility_error() == "meshtastic_version_unsupported:2.7.11"
    assert adapter.has_inflight() is False


@pytest.mark.parametrize("max_workers", [True, False, 0, -1, 1.0, "1"])
def test_constructor_rejects_invalid_worker_bounds(max_workers):
    with pytest.raises(ValueError, match="positive integer"):
        MeshtasticHealthAdapter(max_workers=max_workers)


def test_constructor_rejects_empty_supported_version():
    with pytest.raises(ValueError, match="must not be empty"):
        MeshtasticHealthAdapter(supported_version="")


@pytest.mark.parametrize(
    ("kwargs", "detail"),
    [
        ({"timeout": True}, "invalid_timeout"),
        ({"is_current": None}, "is_current_must_be_callable"),
        ({"generation": []}, "generation_must_be_hashable"),
        ({"expected_node_num": -1}, "local_node_number_invalid"),
        ({"expected_hardware_model": True}, "expected_hardware_model_invalid"),
    ],
)
def test_probe_rejects_invalid_inputs_without_sending(kwargs, detail):
    interface = _Interface(lambda _interface, _callback, _packet: None)
    arguments = {
        "interface": interface,
        "generation": 1,
        "is_current": _always_current,
        "timeout": 0.1,
    }
    arguments.update(kwargs)

    result = _adapter().probe(**arguments)

    assert result.outcome is MeshtasticHealthOutcome.UNSUPPORTED
    assert result.detail == detail
    assert interface.localNode.calls == []


@pytest.mark.parametrize("predicate", [lambda *_args: "yes", lambda *_args: 1])
def test_probe_rejects_non_boolean_generation_predicates(predicate):
    result = _adapter().probe(
        _Interface(lambda *_args: None),
        1,
        is_current=predicate,
        timeout=0.1,
    )

    assert result.detail == "generation_predicate_failed"


def test_probe_rejects_generation_predicate_exception():
    def failed_predicate(*_args):
        raise KeyboardInterrupt

    result = _adapter().probe(
        _Interface(lambda *_args: None),
        1,
        is_current=failed_predicate,
        timeout=0.1,
    )

    assert result.detail == "generation_predicate_failed"


def test_conflicting_inflight_probe_is_rejected_without_second_send():
    release = threading.Event()
    entered = threading.Event()

    def block(_interface, _callback, _packet):
        entered.set()
        release.wait(timeout=1)

    interface = _Interface(block)
    adapter = _adapter()
    first_result = []
    first = threading.Thread(
        target=lambda: first_result.append(
            adapter.probe(interface, 9, is_current=_always_current, timeout=0.5)
        )
    )
    first.start()
    assert entered.wait(timeout=1)

    conflict = adapter.probe(
        interface,
        9,
        is_current=_always_current,
        expected_hardware_model=42,
        timeout=0.1,
    )
    release.set()
    first.join(timeout=1)

    assert conflict.detail == "conflicting_probe_for_interface_generation"
    assert len(interface.localNode.calls) == 1


def test_worker_start_failure_releases_capacity_and_inflight_entry():
    adapter = _adapter()
    interface = _Interface(lambda *_args: None)

    with patch.object(threading.Thread, "start", side_effect=RuntimeError("no thread")):
        result = adapter.probe(interface, 1, is_current=_always_current, timeout=0.1)

    assert result.detail == "probe_worker_start_failed:RuntimeError"
    assert adapter.has_inflight() is False
    assert adapter._worker_slots.acquire(blocking=False) is True
    adapter._worker_slots.release()


@pytest.mark.parametrize(
    ("worker_current", "detail"),
    [
        (False, "generation_became_stale_before_send"),
        (None, "generation_predicate_failed"),
    ],
)
def test_worker_rechecks_generation_before_sending(worker_current, detail):
    main_thread = threading.current_thread()

    def current(_interface, _generation):
        if threading.current_thread() is main_thread:
            return True
        if worker_current is None:
            raise RuntimeError("predicate failed")
        return worker_current

    interface = _Interface(lambda *_args: None)
    result = _adapter().probe(interface, 1, is_current=current, timeout=0.2)

    assert result.detail == detail
    assert interface.localNode.calls == []


class _RejectMetadataRequest:
    @property
    def get_device_metadata_request(self):
        return False

    @get_device_metadata_request.setter
    def get_device_metadata_request(self, _value):
        raise ValueError("read only")


@pytest.mark.parametrize(
    ("factory", "detail"),
    [
        (lambda: None, "meshtastic_admin_message_unavailable"),
        (lambda: _RejectMetadataRequest(), "metadata_request_field_unavailable"),
    ],
)
def test_worker_rejects_unavailable_admin_message_contract(factory, detail):
    adapter = MeshtasticHealthAdapter(
        version_getter=lambda _distribution: "2.7.10",
        admin_message_factory=factory,
    )
    interface = _Interface(lambda *_args: None)

    result = adapter.probe(interface, 1, is_current=_always_current, timeout=0.2)

    assert result.detail == detail
    assert interface.localNode.calls == []


def test_invalid_sent_packet_id_is_unsupported():
    interface = _Interface(lambda *_args: None)

    def send_admin(_message, *, wantResponse, onResponse):
        assert wantResponse is True
        interface.responseHandlers[1] = _Handler(onResponse)
        return SimpleNamespace(id=True)

    interface.localNode._sendAdmin = send_admin

    result = _adapter().probe(interface, 1, is_current=_always_current, timeout=0.2)

    assert result.detail == "metadata_request_packet_id_invalid"


def test_post_response_generation_predicate_failure_is_unsupported():
    main_thread = threading.current_thread()
    worker_calls = 0

    def current(_interface, _generation):
        nonlocal worker_calls
        if threading.current_thread() is main_thread:
            return True
        worker_calls += 1
        if worker_calls == 2:
            raise RuntimeError("predicate failed after response")
        return True

    def respond(_interface, callback, packet):
        callback(_metadata_packet(packet.id))

    result = _adapter().probe(
        _Interface(respond),
        1,
        is_current=current,
        timeout=0.2,
    )

    assert result.detail == "generation_predicate_failed"


def test_unexpected_worker_failure_is_contained_and_capacity_released():
    adapter = _adapter()
    interface = _Interface(lambda *_args: None)

    with patch.object(adapter, "_new_admin_message", side_effect=KeyboardInterrupt):
        result = adapter.probe(interface, 1, is_current=_always_current, timeout=0.2)

    assert result.detail == "health_adapter_failure:KeyboardInterrupt"
    assert adapter.has_inflight() is False


def test_cleanup_failure_does_not_hide_success_or_strand_capacity():
    def respond(_interface, callback, packet):
        callback(_metadata_packet(packet.id))

    adapter = _adapter()
    with patch.object(
        adapter,
        "_remove_own_response_handler",
        side_effect=RuntimeError("cleanup failed"),
    ):
        result = adapter.probe(
            _Interface(respond),
            1,
            is_current=_always_current,
            timeout=0.2,
        )

    assert result.verified is True
    assert adapter.has_inflight() is False


@pytest.mark.parametrize(
    ("packet", "detail"),
    [
        (None, "metadata_response_not_a_mapping"),
        ({"from": 0x12345678, "decoded": None}, "metadata_response_decoded_payload_invalid"),
        (
            {
                "from": 0x12345678,
                "decoded": {
                    "requestId": 0xAABBCCDD,
                    "portnum": "ROUTING_APP",
                    "routing": None,
                },
            },
            "routing_response_payload_invalid",
        ),
        (
            {
                "from": 0x12345678,
                "decoded": {"requestId": 0xAABBCCDD, "portnum": "TEXT_MESSAGE_APP"},
            },
            "metadata_response_port_invalid",
        ),
        (
            {
                "from": 0x12345678,
                "decoded": {
                    "requestId": 0xAABBCCDD,
                    "portnum": "ADMIN_APP",
                    "admin": None,
                },
            },
            "metadata_admin_payload_invalid",
        ),
        (
            {
                "from": 0x12345678,
                "decoded": {
                    "requestId": 0xAABBCCDD,
                    "portnum": "ADMIN_APP",
                    "admin": {"raw": object()},
                },
            },
            "metadata_admin_raw_message_invalid",
        ),
        (
            _metadata_packet(
                0xAABBCCDD,
                metadata=_Metadata(device_state_version=-1),
            ),
            "metadata_device_state_version_invalid",
        ),
    ],
)
def test_additional_strict_response_shapes(packet, detail):
    def respond(_interface, callback, _sent_packet):
        callback(packet)

    result = _adapter().probe(
        _Interface(respond),
        1,
        is_current=_always_current,
        timeout=0.2,
    )

    assert result.detail == detail


def test_metadata_presence_check_exception_is_field_missing():
    class RaisingRaw:
        def HasField(self, _name):
            raise TypeError("bad protobuf")

    packet = _metadata_packet(0xAABBCCDD)
    packet["decoded"]["admin"]["raw"] = RaisingRaw()

    def respond(_interface, callback, _sent_packet):
        callback(packet)

    result = _adapter().probe(
        _Interface(respond),
        1,
        is_current=_always_current,
        timeout=0.2,
    )

    assert result.detail == "metadata_response_field_missing"


def test_invalid_queue_status_is_unsupported_before_send():
    interface = _Interface(lambda *_args: None)
    interface.queueStatus.free = True

    result = _adapter().probe(interface, 1, is_current=_always_current, timeout=0.1)

    assert result.detail == "tx_queue_status_invalid"
    assert interface.localNode.calls == []


def test_handler_cleanup_early_returns_and_swallows_mapping_errors():
    adapter = _adapter()
    adapter._remove_own_response_handler(SimpleNamespace(callback=None))
    adapter._remove_own_response_handler(
        SimpleNamespace(callback=lambda _packet: None, interface=object())
    )

    class BrokenHandlers(dict):
        def items(self):
            raise RuntimeError("mapping changed")

    adapter._remove_own_response_handler(
        SimpleNamespace(
            callback=lambda _packet: None,
            interface=SimpleNamespace(responseHandlers=BrokenHandlers()),
            packet_id=None,
        )
    )


def test_admin_factory_fallback_and_failure_paths():
    assert _adapter()._new_admin_message() is not None
    failed = MeshtasticHealthAdapter(
        version_getter=lambda _distribution: "2.7.10",
        admin_message_factory=lambda: (_ for _ in ()).throw(RuntimeError("factory failed")),
    )
    assert failed._new_admin_message() is None


def test_missing_distribution_is_reported():
    def missing(_distribution):
        raise RuntimeError("metadata backend failed")

    adapter = MeshtasticHealthAdapter(version_getter=missing)
    assert adapter.compatibility_error() == "meshtastic_distribution_unavailable"


def test_transport_exception_classification_covers_optional_serial_types_and_text():
    serial_error = type("SerialException", (Exception,), {"__module__": "vendor.serial"})

    assert MeshtasticHealthAdapter._is_transport_exception(serial_error("bad")) is True
    assert MeshtasticHealthAdapter._is_transport_exception(Exception("broken pipe")) is True
    assert MeshtasticHealthAdapter._is_transport_exception(Exception("unrelated")) is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, None),
        ("abc", None),
        ("4294967295", 0xFFFFFFFF),
        ("4294967296", None),
        (-1, None),
        (1.0, None),
    ],
)
def test_uint32_strict_validation(value, expected):
    assert MeshtasticHealthAdapter._uint32(value) == expected


@pytest.mark.parametrize("value", [True, -1, 1.0, "1"])
def test_nonnegative_int_rejects_non_integer_values(value):
    assert MeshtasticHealthAdapter._nonnegative_int(value) is None
