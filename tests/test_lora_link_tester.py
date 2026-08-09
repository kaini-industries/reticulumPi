"""Tests for the lora_link_tester plugin."""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi.serial_devices import (
    SerialDeviceBusyError,
    SerialDeviceChangedError,
    SerialDeviceIdentityError,
)

# ---------------------------------------------------------------------------
# Mock meshtastic before importing the plugin
# ---------------------------------------------------------------------------

_mock_meshtastic = MagicMock()
_mock_meshtastic_serial = MagicMock()
_mock_meshtastic.serial_interface = _mock_meshtastic_serial

_mock_meshtastic_protobuf = MagicMock()
_mock_portnums = MagicMock()
_mock_portnums.PortNum.TEXT_MESSAGE_APP = 1
_mock_meshtastic.protobuf = _mock_meshtastic_protobuf
_mock_meshtastic_protobuf.portnums_pb2 = _mock_portnums


@pytest.fixture(autouse=True)
def _patch_meshtastic():
    with patch.dict(
        sys.modules,
        {
            "meshtastic": _mock_meshtastic,
            "meshtastic.serial_interface": _mock_meshtastic_serial,
            "meshtastic.protobuf": _mock_meshtastic_protobuf,
            "meshtastic.protobuf.portnums_pb2": _mock_portnums,
        },
    ):
        # Meshtastic 2.7.10 exposes portnums only below protobuf.  Keeping the
        # removed legacy module absent ensures tests cannot mask a real import
        # failure in probe sends.
        sys.modules.pop("meshtastic.portnums_pb2", None)
        _mock_meshtastic_serial.SerialInterface.reset_mock()
        _mock_meshtastic_serial.SerialInterface.side_effect = None
        registry = MagicMock()
        lease = MagicMock()
        lease.revalidate.return_value = lease.identity
        registry.claim.return_value = lease
        with patch(
            "reticulumpi.builtin_plugins.lora_link_tester.serial_device_registry",
            registry,
        ):
            yield registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app() -> MagicMock:
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.identity.hash = b"\x00" * 16
    app.node_name = "TestNode"
    app.plugins = {}
    app.event_bus = MagicMock()
    return app


def _make_config(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {"serial_port": "/dev/lora-link-tester"}
    cfg.update(overrides)
    return cfg


def _routing_response(
    request_id: int,
    *,
    error_reason: str | int = "NONE",
    response_id: int = 900,
    rssi: int | None = None,
    snr: float | None = None,
) -> dict[str, Any]:
    packet: dict[str, Any] = {
        "id": response_id,
        "decoded": {
            "requestId": request_id,
            "portnum": "ROUTING_APP",
            "routing": {"errorReason": error_reason},
        },
    }
    if rssi is not None:
        packet["rxRssi"] = rssi
    if snr is not None:
        packet["rxSnr"] = snr
    return packet


def _make_plugin(config: dict[str, Any] | None = None) -> Any:
    from reticulumpi.builtin_plugins.lora_link_tester import LoraLinkTester

    return LoraLinkTester(_make_app(), config or _make_config())


def _make_started_plugin(config: dict[str, Any] | None = None) -> Any:
    """Create a plugin with state initialized but no threads running."""
    plugin = _make_plugin(config)
    plugin._serial_port = plugin.config["serial_port"]
    plugin._target_node_id = plugin.config.get("target_node_id")
    plugin._channel_index = plugin.config.get("channel_index", 0)
    plugin._probe_interval = plugin.config.get("probe_interval", 30)
    plugin._probe_count = plugin.config.get("probe_count", 20)
    plugin._probe_timeout = plugin.config.get("probe_timeout", 30)
    plugin._max_history = plugin.config.get("max_history", 500)
    plugin._hop_limit = plugin.config.get("hop_limit")
    plugin._reconnect_delay = plugin.config.get("reconnect_delay", 10)
    plugin._max_reconnect_attempts = plugin.config.get("max_reconnect_attempts", 0)
    plugin._probe_prefix = plugin.config.get("probe_text_prefix", "LT")
    plugin._lock = threading.Lock()
    plugin._serial_worker_condition = threading.Condition(plugin._lock)
    plugin._serial_device_lease = None
    plugin._serial_open_generation = 0
    plugin._serial_open_workers = set()
    plugin._serial_open_attempts = set()
    plugin._abandoned_serial_open_workers = set()
    plugin._serial_send_generation = 0
    plugin._serial_send_workers = set()
    plugin._serial_send_attempts = set()
    plugin._abandoned_serial_send_workers = set()
    plugin._serial_close_attempts = set()
    plugin._unresolved_serial_handles = {}
    plugin._lease_release_watcher = None
    plugin._serial_teardown_complete = False
    plugin._interface = MagicMock()
    plugin._connected = True
    plugin._status = "idle"
    plugin._test_running = False
    plugin._test_generation = 1
    plugin._test_target = None
    plugin._test_stop_event = threading.Event()
    plugin._current_sequence = 0
    plugin._probes_sent = 0
    plugin._probes_acked = 0
    plugin._probes_lost = 0
    plugin._pending_probes = {}
    plugin._pending_probe_sends = set()
    plugin._early_probe_acks = {}
    plugin._history = deque(maxlen=plugin._max_history)
    plugin._active = True
    return plugin


def _make_disconnected_plugin(config: dict[str, Any] | None = None) -> Any:
    """Create initialized ownership state with no published interface."""
    plugin = _make_started_plugin(config)
    plugin._interface = None
    plugin._connected = False
    return plugin


@pytest.fixture
def managed_started_plugin(_patch_meshtastic):
    """Own synthetic plugins whose tests start real probe threads."""
    baseline_probe_ids = {
        id(thread)
        for thread in threading.enumerate()
        if thread.name == "linktester-probe" and thread.is_alive()
    }
    plugins = []

    def _make(config: dict[str, Any] | None = None) -> Any:
        plugin = _make_started_plugin(config)
        plugins.append(plugin)
        return plugin

    yield _make

    # This finalizer runs before _patch_meshtastic is removed, so no probe can
    # outlive the SDK mocks and lazily import the real protobuf package later.
    for plugin in reversed(plugins):
        try:
            plugin.stop_test()
        finally:
            plugin._test_stop_event.set()
            plugin._join_threads()

    leaked = [
        thread
        for thread in threading.enumerate()
        if thread.name == "linktester-probe"
        and thread.is_alive()
        and id(thread) not in baseline_probe_ids
    ]
    assert not leaked, f"Link Tester probe thread(s) survived test teardown: {leaked!r}"


# ===========================================================================
# TestValidateConfig
# ===========================================================================


class TestValidateConfig:
    def test_valid_minimal_config(self):
        _make_plugin()  # should not raise

    def test_valid_full_config(self):
        _make_plugin(
            _make_config(
                target_node_id="!abcd1234",
                channel_index=3,
                probe_interval=15,
                probe_count=50,
                probe_timeout=20,
                max_history=100,
                hop_limit=4,
                reconnect_delay=5,
                max_reconnect_attempts=3,
            )
        )

    def test_missing_serial_port(self):
        with pytest.raises(ValueError, match="serial_port"):
            _make_plugin({"serial_port": ""})

    def test_serial_port_not_string(self):
        with pytest.raises(ValueError, match="serial_port"):
            _make_plugin({"serial_port": 42})

    @pytest.mark.parametrize(
        "serial_port",
        ["   ", "auto", "AUTO", "/dev/ttyUSB0", "/dev/ttyACM17"],
    )
    def test_serial_port_must_name_explicit_device(self, serial_port):
        with pytest.raises(ValueError, match="stable serial device path"):
            _make_plugin({"serial_port": serial_port})

    def test_invalid_target_node_id(self):
        with pytest.raises(ValueError, match="target_node_id"):
            _make_plugin(_make_config(target_node_id="badid"))

    def test_target_node_id_too_short(self):
        with pytest.raises(ValueError, match="target_node_id"):
            _make_plugin(_make_config(target_node_id="!abc"))

    def test_channel_index_out_of_range(self):
        with pytest.raises(ValueError, match="channel_index"):
            _make_plugin(_make_config(channel_index=8))

    def test_channel_index_negative(self):
        with pytest.raises(ValueError, match="channel_index"):
            _make_plugin(_make_config(channel_index=-1))

    def test_probe_interval_too_low(self):
        with pytest.raises(ValueError, match="probe_interval"):
            _make_plugin(_make_config(probe_interval=5))

    def test_probe_count_negative(self):
        with pytest.raises(ValueError, match="probe_count"):
            _make_plugin(_make_config(probe_count=-1))

    def test_probe_count_boolean_is_rejected(self):
        with pytest.raises(ValueError, match="probe_count"):
            _make_plugin(_make_config(probe_count=True))

    def test_probe_count_zero_is_valid(self):
        _make_plugin(_make_config(probe_count=0))

    def test_probe_timeout_too_low(self):
        with pytest.raises(ValueError, match="probe_timeout"):
            _make_plugin(_make_config(probe_timeout=2))

    def test_max_history_too_low(self):
        with pytest.raises(ValueError, match="max_history"):
            _make_plugin(_make_config(max_history=5))

    def test_hop_limit_out_of_range(self):
        with pytest.raises(ValueError, match="hop_limit"):
            _make_plugin(_make_config(hop_limit=0))

    def test_hop_limit_too_high(self):
        with pytest.raises(ValueError, match="hop_limit"):
            _make_plugin(_make_config(hop_limit=8))

    def test_hop_limit_none_is_valid(self):
        _make_plugin(_make_config(hop_limit=None))

    def test_reconnect_delay_too_low(self):
        with pytest.raises(ValueError, match="reconnect_delay"):
            _make_plugin(_make_config(reconnect_delay=0))

    def test_max_reconnect_attempts_negative(self):
        with pytest.raises(ValueError, match="max_reconnect_attempts"):
            _make_plugin(_make_config(max_reconnect_attempts=-1))


class TestLifecycle:
    def test_start_initializes_all_serial_and_probe_state(self):
        plugin = _make_plugin(
            _make_config(
                target_node_id="!abcd1234",
                channel_index=2,
                probe_interval=15,
                probe_count=0,
                probe_timeout=12,
                max_history=25,
                hop_limit=4,
                reconnect_delay=3,
                max_reconnect_attempts=7,
                probe_text_prefix="FIELD",
            )
        )

        with patch.object(plugin, "_start_thread") as start_thread:
            plugin.start()

        assert plugin._serial_port == "/dev/lora-link-tester"
        assert plugin._target_node_id == "!abcd1234"
        assert plugin._channel_index == 2
        assert plugin._probe_interval == 15
        assert plugin._probe_count == 0
        assert plugin._probe_timeout == 12
        assert plugin._max_history == 25
        assert plugin._hop_limit == 4
        assert plugin._reconnect_delay == 3
        assert plugin._max_reconnect_attempts == 7
        assert plugin._probe_prefix == "FIELD"
        assert plugin._active is True
        assert plugin._connected is False
        assert plugin._status == "idle"
        assert plugin._history.maxlen == 25
        assert [call.args[1] for call in start_thread.call_args_list] == [
            "linktester-connect",
            "linktester-timeout",
        ]

        plugin.stop()
        assert plugin._active is False


# ===========================================================================
# TestSerialOwnership
# ===========================================================================


class TestSerialOwnership:
    def test_claims_and_revalidates_dedicated_device_before_open(
        self,
        _patch_meshtastic,
    ):
        plugin = _make_disconnected_plugin()
        calls = []
        lease = MagicMock()
        _patch_meshtastic.claim.side_effect = lambda path, owner: (
            calls.append(("claim", path, owner)) or lease
        )
        lease.revalidate.side_effect = lambda: calls.append(("revalidate",))
        iface = MagicMock()
        _mock_meshtastic_serial.SerialInterface.side_effect = lambda **kwargs: (
            calls.append(("open", kwargs["devPath"])) or iface
        )

        plugin._open_interface()

        assert calls[:3] == [
            ("claim", "/dev/lora-link-tester", "lora_link_tester"),
            ("revalidate",),
            ("open", "/dev/lora-link-tester"),
        ]
        assert plugin._interface is iface
        assert plugin._connected is True
        assert plugin._serial_device_lease is lease
        _patch_meshtastic.claim.assert_called_once_with(
            "/dev/lora-link-tester",
            "lora_link_tester",
        )

    def test_registry_conflict_fails_closed_without_constructor(
        self,
        _patch_meshtastic,
    ):
        plugin = _make_disconnected_plugin()
        _patch_meshtastic.claim.side_effect = SerialDeviceBusyError(
            "/dev/lora-link-tester",
            ("meshtastic_gateway",),
            MagicMock(),
            external=False,
        )

        with pytest.raises(SerialDeviceBusyError, match="meshtastic_gateway"):
            plugin._open_interface()

        _mock_meshtastic_serial.SerialInterface.assert_not_called()
        assert plugin._serial_device_lease is None

    def test_missing_device_fails_closed_and_retains_existing_claim(
        self,
        _patch_meshtastic,
    ):
        plugin = _make_disconnected_plugin()
        lease = MagicMock()
        lease.revalidate.side_effect = SerialDeviceIdentityError("device absent")
        plugin._serial_device_lease = lease

        with pytest.raises(SerialDeviceIdentityError, match="device absent"):
            plugin._open_interface()

        lease.release.assert_not_called()
        _patch_meshtastic.claim.assert_not_called()
        _mock_meshtastic_serial.SerialInterface.assert_not_called()
        assert plugin._serial_device_lease is lease

    def test_changed_identity_that_cannot_be_reclaimed_never_opens(
        self,
        _patch_meshtastic,
    ):
        plugin = _make_disconnected_plugin()
        old_lease = MagicMock()
        old_lease.revalidate.side_effect = SerialDeviceChangedError(
            "/dev/lora-link-tester",
            MagicMock(),
            MagicMock(),
        )
        _patch_meshtastic.claim.side_effect = SerialDeviceIdentityError("replacement unavailable")
        plugin._serial_device_lease = old_lease

        with pytest.raises(SerialDeviceChangedError):
            plugin._open_interface()

        old_lease.release.assert_called_once_with()
        _patch_meshtastic.claim.assert_not_called()
        _mock_meshtastic_serial.SerialInterface.assert_not_called()
        assert plugin._serial_device_lease is None

        with pytest.raises(SerialDeviceIdentityError, match="replacement unavailable"):
            plugin._open_interface()

        _patch_meshtastic.claim.assert_called_once_with(
            "/dev/lora-link-tester",
            "lora_link_tester",
        )
        _mock_meshtastic_serial.SerialInterface.assert_not_called()
        assert plugin._serial_device_lease is None

    def test_changed_identity_is_reclaimed_and_revalidated_before_open(
        self,
        _patch_meshtastic,
    ):
        plugin = _make_disconnected_plugin()
        old_lease = MagicMock()
        old_lease.revalidate.side_effect = SerialDeviceChangedError(
            "/dev/lora-link-tester",
            MagicMock(),
            MagicMock(),
        )
        new_lease = MagicMock()
        _patch_meshtastic.claim.return_value = new_lease
        plugin._serial_device_lease = old_lease
        iface = MagicMock()
        _mock_meshtastic_serial.SerialInterface.return_value = iface

        with pytest.raises(SerialDeviceChangedError):
            plugin._open_interface()

        _mock_meshtastic_serial.SerialInterface.assert_not_called()
        plugin._open_interface()

        old_lease.release.assert_called_once_with()
        new_lease.revalidate.assert_called_once_with()
        assert plugin._serial_device_lease is new_lease
        assert plugin._interface is iface

    def test_failed_fresh_revalidation_releases_claim_without_open(
        self,
        _patch_meshtastic,
    ):
        plugin = _make_disconnected_plugin()
        lease = MagicMock()
        lease.revalidate.side_effect = SerialDeviceIdentityError("changed before open")
        _patch_meshtastic.claim.return_value = lease

        with pytest.raises(SerialDeviceIdentityError, match="changed before open"):
            plugin._open_interface()

        lease.release.assert_called_once_with()
        _mock_meshtastic_serial.SerialInterface.assert_not_called()
        assert plugin._serial_device_lease is None

    def test_constructor_failure_keeps_lease_for_revalidated_retry(
        self,
        _patch_meshtastic,
    ):
        plugin = _make_disconnected_plugin()
        lease = MagicMock()
        _patch_meshtastic.claim.return_value = lease
        iface = MagicMock()
        outcomes = iter((OSError("open failed"), iface))

        def constructor(**kwargs):
            outcome = next(outcomes)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        _mock_meshtastic_serial.SerialInterface.side_effect = constructor

        with pytest.raises(OSError, match="open failed"):
            plugin._open_interface()
        assert plugin._serial_device_lease is lease
        lease.release.assert_not_called()

        plugin._open_interface()

        _patch_meshtastic.claim.assert_called_once_with(
            "/dev/lora-link-tester",
            "lora_link_tester",
        )
        assert lease.revalidate.call_count == 2
        assert plugin._interface is iface

    def test_timed_out_constructor_closes_late_result_without_publishing(
        self,
        _patch_meshtastic,
    ):
        plugin = _make_disconnected_plugin()
        lease = _patch_meshtastic.claim.return_value
        gate = threading.Event()
        closed = threading.Event()
        iface = MagicMock()
        iface.close.side_effect = lambda: closed.set()

        def slow_constructor(**kwargs):
            gate.wait(timeout=5)
            return iface

        _mock_meshtastic_serial.SerialInterface.side_effect = slow_constructor

        with (
            patch(
                "reticulumpi.builtin_plugins.lora_link_tester._SERIAL_OPEN_TIMEOUT",
                0.02,
            ),
            patch("reticulumpi.builtin_plugins.lora_link_tester.record_hung_worker"),
            pytest.raises(TimeoutError, match="timed out"),
        ):
            plugin._open_interface()

        assert plugin._interface is None
        assert plugin._connected is False
        assert plugin._serial_device_lease is lease
        lease.release.assert_not_called()

        gate.set()
        assert closed.wait(timeout=2), "late constructor result was not closed"
        assert plugin._wait_for_serial_open_workers(2)
        assert plugin._interface is None
        assert plugin._connected is False
        assert not any(
            call.args[1].get("connected") is True
            for call in plugin.event_bus.publish.call_args_list
            if len(call.args) > 1 and isinstance(call.args[1], dict)
        )

    def test_abandoned_worker_cap_blocks_another_constructor(
        self,
        _patch_meshtastic,
    ):
        plugin = _make_disconnected_plugin()
        gate = threading.Event()
        closed = threading.Event()
        iface = MagicMock()
        iface.close.side_effect = lambda: closed.set()

        def slow_constructor(**kwargs):
            gate.wait(timeout=5)
            return iface

        _mock_meshtastic_serial.SerialInterface.side_effect = slow_constructor

        with (
            patch(
                "reticulumpi.builtin_plugins.lora_link_tester._SERIAL_OPEN_TIMEOUT",
                0.02,
            ),
            patch("reticulumpi.builtin_plugins.lora_link_tester.record_hung_worker"),
            pytest.raises(TimeoutError),
        ):
            plugin._open_interface()

        with pytest.raises(RuntimeError, match="worker cap"):
            plugin._open_interface()
        assert _mock_meshtastic_serial.SerialInterface.call_count == 1

        gate.set()
        assert closed.wait(timeout=2)
        assert plugin._wait_for_serial_open_workers(2)

    def test_stop_retains_lease_until_late_worker_closes(
        self,
        _patch_meshtastic,
    ):
        plugin = _make_disconnected_plugin()
        lease = _patch_meshtastic.claim.return_value
        gate = threading.Event()
        released = threading.Event()
        order = []
        iface = MagicMock()
        iface.close.side_effect = lambda: order.append("close")
        lease.release.side_effect = lambda: (order.append("release"), released.set())

        def slow_constructor(**kwargs):
            gate.wait(timeout=5)
            return iface

        _mock_meshtastic_serial.SerialInterface.side_effect = slow_constructor
        with (
            patch(
                "reticulumpi.builtin_plugins.lora_link_tester._SERIAL_OPEN_TIMEOUT",
                0.02,
            ),
            patch("reticulumpi.builtin_plugins.lora_link_tester.record_hung_worker"),
            pytest.raises(TimeoutError),
        ):
            plugin._open_interface()

        plugin._join_threads = MagicMock()
        with patch(
            "reticulumpi.builtin_plugins.lora_link_tester._SERIAL_WORKER_SHUTDOWN_TIMEOUT",
            0.01,
        ):
            plugin.stop()

        lease.release.assert_not_called()
        gate.set()
        assert released.wait(timeout=2), "lease was not released after late worker cleanup"
        assert order == ["close", "release"]
        assert plugin._serial_device_lease is None

    def test_stop_closes_interface_and_joins_before_releasing_lease(
        self,
        _patch_meshtastic,
    ):
        plugin = _make_started_plugin()
        lease = _patch_meshtastic.claim.return_value
        plugin._serial_device_lease = lease
        order = []
        plugin._interface.close.side_effect = lambda: order.append("close")
        plugin._join_threads = MagicMock(side_effect=lambda: order.append("join"))
        lease.release.side_effect = lambda: order.append("release")

        plugin.stop()

        assert order == ["close", "join", "release"]
        assert plugin._serial_device_lease is None

    def test_stop_bounds_hung_close_and_retains_lease_until_close_returns(
        self,
        _patch_meshtastic,
    ):
        plugin = _make_started_plugin()
        lease = _patch_meshtastic.claim.return_value
        plugin._serial_device_lease = lease
        close_gate = threading.Event()
        released = threading.Event()
        plugin._interface.close.side_effect = lambda: close_gate.wait(timeout=5)
        plugin._join_threads = MagicMock()
        lease.release.side_effect = lambda: released.set()

        started = time.monotonic()
        with (
            patch(
                "reticulumpi.builtin_plugins.lora_link_tester._SERIAL_CLOSE_TIMEOUT",
                0.02,
            ),
            patch("reticulumpi.builtin_plugins.lora_link_tester.record_hung_worker"),
        ):
            plugin.stop()

        assert time.monotonic() - started < 0.5
        lease.release.assert_not_called()
        assert plugin._serial_device_lease is lease

        close_gate.set()
        assert released.wait(timeout=2), "lease was not released after close completed"
        assert plugin._serial_device_lease is None

    def test_stop_releases_lease_after_late_managed_worker_exits(
        self,
        _patch_meshtastic,
    ):
        plugin = _make_disconnected_plugin()
        lease = _patch_meshtastic.claim.return_value
        live_thread = MagicMock()
        live_thread.is_alive.return_value = True
        plugin._threads = [live_thread]
        plugin._serial_device_lease = lease
        plugin._join_threads = MagicMock()
        released = threading.Event()
        lease.release.side_effect = lambda: released.set()

        plugin.stop()

        lease.release.assert_not_called()
        assert plugin._serial_device_lease is lease

        live_thread.is_alive.return_value = False
        with plugin._serial_worker_condition:
            plugin._serial_worker_condition.notify_all()
        assert released.wait(timeout=2)
        assert plugin._serial_device_lease is None

    def test_stop_retries_transient_close_failure_before_releasing_lease(
        self,
        _patch_meshtastic,
    ):
        plugin = _make_started_plugin()
        lease = _patch_meshtastic.claim.return_value
        iface = plugin._interface
        plugin._serial_device_lease = lease
        released = threading.Event()
        iface.close.side_effect = [OSError("transient close failure"), None]
        plugin._join_threads = MagicMock()
        lease.release.side_effect = lambda: released.set()

        plugin.stop()

        assert released.wait(timeout=3)
        assert iface.close.call_count == 2
        assert plugin._unresolved_serial_handles == {}
        assert plugin._serial_device_lease is None

    def test_close_exception_retains_handle_lease_and_blocks_reopen(
        self,
        _patch_meshtastic,
    ):
        plugin = _make_started_plugin()
        lease = _patch_meshtastic.claim.return_value
        old_iface = plugin._interface
        old_iface.close.side_effect = OSError("USB close failed")
        plugin._serial_device_lease = lease

        plugin._close_interface()

        assert plugin._unresolved_serial_handles == {id(old_iface): old_iface}
        assert plugin.get_status()["serial_reopen_blocked"] is True
        with pytest.raises(RuntimeError, match="teardown is unresolved"):
            plugin._open_interface()
        _mock_meshtastic_serial.SerialInterface.assert_not_called()
        lease.release.assert_not_called()

        old_iface.close.side_effect = None
        assert plugin._retry_unresolved_serial_handles() is True
        assert plugin._unresolved_serial_handles == {}

        replacement = MagicMock()
        _mock_meshtastic_serial.SerialInterface.return_value = replacement
        plugin._open_interface()
        assert plugin._interface is replacement

    def test_abandoned_constructor_close_exception_blocks_reopen_until_retry(
        self,
        _patch_meshtastic,
    ):
        plugin = _make_disconnected_plugin()
        lease = _patch_meshtastic.claim.return_value
        gate = threading.Event()
        iface = MagicMock()
        iface.close.side_effect = OSError("late close failed")

        def slow_constructor(**kwargs):
            gate.wait(timeout=5)
            return iface

        _mock_meshtastic_serial.SerialInterface.side_effect = slow_constructor
        with (
            patch(
                "reticulumpi.builtin_plugins.lora_link_tester._SERIAL_OPEN_TIMEOUT",
                0.02,
            ),
            patch("reticulumpi.builtin_plugins.lora_link_tester.record_hung_worker"),
            pytest.raises(TimeoutError),
        ):
            plugin._open_interface()

        gate.set()
        assert plugin._wait_for_serial_open_workers(2)
        assert plugin._unresolved_serial_handles == {id(iface): iface}
        with pytest.raises(RuntimeError, match="teardown is unresolved"):
            plugin._open_interface()
        assert _mock_meshtastic_serial.SerialInterface.call_count == 1
        lease.release.assert_not_called()

        iface.close.side_effect = None
        assert plugin._retry_unresolved_serial_handles() is True

    def test_finite_reconnect_exhaustion_reports_failed(self):
        plugin = _make_disconnected_plugin(_make_config(max_reconnect_attempts=1))
        plugin._open_interface = MagicMock(side_effect=OSError("radio absent"))

        plugin._connection_loop()

        assert plugin.get_status()["active"] is False
        assert plugin.get_status()["connected"] is False
        assert plugin.get_status()["status"] == "failed"


class TestSerialBoundaryFailures:
    def test_restart_refuses_incomplete_open_teardown(self):
        plugin = _make_plugin()
        with patch.object(plugin, "_start_thread"):
            plugin.start()
        plugin._serial_open_attempts.add(1)

        with pytest.raises(RuntimeError, match="teardown is incomplete"):
            plugin.start()

    def test_serial_lease_refuses_open_after_stop(self):
        plugin = _make_disconnected_plugin()
        plugin._active = False

        with pytest.raises(RuntimeError, match="stopping"):
            plugin._ensure_serial_device_lease()

    def test_shutdown_watcher_is_not_duplicated_or_started_without_lease(self):
        plugin = _make_disconnected_plugin()
        watcher = MagicMock()
        watcher.is_alive.return_value = True
        plugin._lease_release_watcher = watcher
        plugin._serial_device_lease = MagicMock()

        plugin._schedule_shutdown_lease_release()
        assert plugin._lease_release_watcher is watcher

        plugin._lease_release_watcher = None
        plugin._serial_device_lease = None
        plugin._schedule_shutdown_lease_release()
        assert plugin._lease_release_watcher is None

    def test_connection_loop_waits_for_unresolved_close(self):
        plugin = _make_disconnected_plugin()
        plugin._retry_unresolved_serial_handles = MagicMock(return_value=False)
        plugin._sleep_while_active = MagicMock(
            side_effect=lambda _delay: setattr(plugin, "_active", False)
        )

        plugin._connection_loop()

        assert plugin._status == "error"
        plugin._sleep_while_active.assert_called_once_with(plugin._reconnect_delay)

    def test_connection_loop_waits_for_unresolved_send(self):
        plugin = _make_disconnected_plugin()
        plugin._serial_send_attempts.add(1)
        plugin._sleep_while_active = MagicMock(
            side_effect=lambda _delay: setattr(plugin, "_active", False)
        )

        plugin._connection_loop()

        assert plugin._status == "error"
        plugin._sleep_while_active.assert_called_once_with(plugin._reconnect_delay)

    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (lambda plugin: setattr(plugin, "_active", False), "stopping"),
            (
                lambda plugin: plugin._abandoned_serial_open_workers.add(object()),
                "worker cap",
            ),
            (lambda plugin: plugin._serial_close_attempts.add(1), "teardown is unresolved"),
            (lambda plugin: plugin._serial_send_attempts.add(1), "serial send is unresolved"),
            (lambda plugin: setattr(plugin, "_interface", MagicMock()), "already published"),
        ],
    )
    def test_open_rechecks_all_boundaries_after_claim(self, mutate, message):
        plugin = _make_disconnected_plugin()

        def mutate_after_claim():
            mutate(plugin)
            return MagicMock()

        plugin._ensure_serial_device_lease = MagicMock(side_effect=mutate_after_claim)

        with pytest.raises(RuntimeError, match=message):
            plugin._open_interface()

        _mock_meshtastic_serial.SerialInterface.assert_not_called()

    def test_open_worker_start_failure_clears_attempt_tokens(self):
        plugin = _make_disconnected_plugin()

        with (
            patch(
                "reticulumpi.builtin_plugins.lora_link_tester.threading.Thread.start",
                side_effect=RuntimeError("thread unavailable"),
            ),
            pytest.raises(RuntimeError, match="thread unavailable"),
        ):
            plugin._open_interface()

        assert plugin._serial_open_workers == set()
        assert plugin._serial_open_attempts == set()

    def test_open_rejects_constructor_that_returns_no_interface(self):
        plugin = _make_disconnected_plugin()
        _mock_meshtastic_serial.SerialInterface.return_value = None

        with pytest.raises(ConnectionError, match="returned no interface"):
            plugin._open_interface()

        assert plugin._interface is None
        assert plugin._connected is False

    def test_close_worker_start_failure_retains_exact_handle(self):
        plugin = _make_disconnected_plugin()
        iface = MagicMock()

        with (
            patch(
                "reticulumpi.builtin_plugins.lora_link_tester.threading.Thread.start",
                side_effect=RuntimeError("thread unavailable"),
            ),
            patch("reticulumpi.builtin_plugins.lora_link_tester.record_hung_worker") as hung,
        ):
            assert plugin._close_serial_handle_bounded(iface, "test interface") is False

        assert plugin._unresolved_serial_handles == {id(iface): iface}
        assert plugin._serial_close_attempts == set()
        hung.assert_called_once_with()
        assert plugin._retry_unresolved_serial_handles() is True

    def test_send_worker_start_failure_clears_attempt_tokens(self):
        plugin = _make_started_plugin()
        iface = plugin._interface

        with (
            patch(
                "reticulumpi.builtin_plugins.lora_link_tester.threading.Thread.start",
                side_effect=RuntimeError("thread unavailable"),
            ),
            pytest.raises(RuntimeError, match="thread unavailable"),
        ):
            plugin._send_data_bounded(iface, {"data": b"probe"})

        assert plugin._serial_send_workers == set()
        assert plugin._serial_send_attempts == set()

    def test_serial_send_rejects_stopping_or_overlapping_attempt(self):
        plugin = _make_started_plugin()
        iface = plugin._interface
        plugin._active = False
        with pytest.raises(RuntimeError, match="stopping"):
            plugin._send_data_bounded(iface, {})

        plugin._active = True
        plugin._serial_send_attempts.add(1)
        with pytest.raises(RuntimeError, match="Another"):
            plugin._send_data_bounded(iface, {})

    def test_failed_interface_callback_ignores_foreign_handle(self):
        plugin = _make_started_plugin()
        published = plugin._interface

        plugin._disconnect_failed_interface(MagicMock(), RuntimeError("foreign failure"))

        assert plugin._interface is published
        assert plugin._connected is True


# ===========================================================================
# TestStartTest
# ===========================================================================


class TestStartTest:
    def test_start_returns_ok(self, managed_started_plugin):
        plugin = managed_started_plugin(_make_config(target_node_id="!abcd1234"))
        result = plugin.start_test()
        assert result["ok"] is True
        assert result["target"] == "!abcd1234"

    def test_start_with_runtime_target(self, managed_started_plugin):
        plugin = managed_started_plugin()
        result = plugin.start_test(target="!11223344")
        assert result["ok"] is True
        assert result["target"] == "!11223344"

    def test_start_no_target_fails(self):
        plugin = _make_started_plugin()
        result = plugin.start_test()
        assert result["ok"] is False
        assert "no target" in result["reason"]

    def test_start_invalid_target_fails(self):
        plugin = _make_started_plugin()
        result = plugin.start_test(target="badid")
        assert result["ok"] is False
        assert "invalid target" in result["reason"]

    @pytest.mark.parametrize("target", [123, True, {"id": "!11223344"}, ["!11223344"]])
    def test_start_non_string_target_returns_invalid_result(self, target):
        plugin = _make_started_plugin()

        result = plugin.start_test(target=target)

        assert result == {"ok": False, "reason": f"invalid target: {target!r}"}
        assert plugin._test_running is False

    def test_start_while_running_fails(self):
        plugin = _make_started_plugin(_make_config(target_node_id="!abcd1234"))
        plugin._test_running = True
        result = plugin.start_test()
        assert result["ok"] is False
        assert "already running" in result["reason"]

    def test_start_while_disconnected_fails(self):
        plugin = _make_started_plugin(_make_config(target_node_id="!abcd1234"))
        plugin._connected = False
        result = plugin.start_test()
        assert result["ok"] is False
        assert "not connected" in result["reason"]

    def test_start_custom_count(self, managed_started_plugin):
        plugin = managed_started_plugin(_make_config(target_node_id="!abcd1234"))
        result = plugin.start_test(count=5)
        assert result["ok"] is True
        assert result["count"] == 5

    @pytest.mark.parametrize("count", [True, False, -1])
    def test_start_rejects_boolean_and_negative_count(self, count):
        plugin = _make_started_plugin(_make_config(target_node_id="!abcd1234"))

        result = plugin.start_test(count=count)

        assert result == {
            "ok": False,
            "reason": "count must be a non-negative integer (0 = unlimited)",
        }
        assert plugin._test_running is False

    def test_start_count_zero_is_preserved_as_unlimited(self):
        plugin = _make_started_plugin(_make_config(target_node_id="!abcd1234"))
        plugin._probe_loop = MagicMock()
        plugin._start_thread = MagicMock()

        result = plugin.start_test(count=0)

        assert result["ok"] is True
        assert result["count"] == 0
        probe_target = plugin._start_thread.call_args.args[0]
        probe_target()
        plugin._probe_loop.assert_called_once_with(
            "!abcd1234",
            0,
            plugin._test_generation,
        )


# ===========================================================================
# TestStopTest
# ===========================================================================


class TestStopTest:
    def test_stop_when_not_running(self):
        plugin = _make_started_plugin()
        result = plugin.stop_test()
        assert result["ok"] is True
        assert "no test running" in result["reason"]

    def test_stop_running_test(self):
        plugin = _make_started_plugin(_make_config(target_node_id="!abcd1234"))
        plugin._test_running = True
        plugin._probes_sent = 10
        plugin._probes_acked = 8
        plugin._probes_lost = 2
        result = plugin.stop_test()
        assert result["ok"] is True
        assert result["stats"]["sent"] == 10
        assert result["stats"]["acked"] == 8
        assert result["stats"]["lost"] == 2
        assert not plugin._test_running


# ===========================================================================
# TestProbeCallback
# ===========================================================================


class TestProbeCallback:
    def test_send_probe_uses_pinned_sdk_protobuf_portnums(self):
        plugin = _make_started_plugin()
        plugin._test_running = True
        plugin._interface.sendData.return_value = {"id": 41}

        plugin._send_probe("!11223344", 0, plugin._test_generation)

        assert "meshtastic.portnums_pb2" not in sys.modules
        assert plugin._interface.sendData.call_args.kwargs["portNum"] == 1

    def test_ack_records_result(self):
        plugin = _make_started_plugin()
        plugin._test_running = True
        send_mono = time.monotonic()
        send_wall = time.time()
        callback = plugin._make_probe_callback(seq=0, send_mono=send_mono, send_wall=send_wall)

        plugin._pending_probes[42] = (send_mono, send_wall, 0, plugin._test_generation)

        callback(_routing_response(42, rssi=-95, snr=6.5))

        assert plugin._probes_acked == 1
        assert 42 not in plugin._pending_probes
        assert len(plugin._history) == 1

        result = plugin._history[0]
        assert result["seq"] == 0
        assert result["status"] == "ack"
        assert result["rssi"] == -95
        assert result["snr"] == 6.5
        assert result["rtt_ms"] is not None
        assert result["rtt_ms"] >= 0

    def test_ack_with_missing_rssi(self):
        plugin = _make_started_plugin()
        plugin._test_running = True
        send_mono = time.monotonic()
        send_wall = time.time()
        callback = plugin._make_probe_callback(seq=1, send_mono=send_mono, send_wall=send_wall)
        plugin._pending_probes[99] = (send_mono, send_wall, 1, plugin._test_generation)
        callback(_routing_response(99))

        result = plugin._history[0]
        assert result["rssi"] is None
        assert result["snr"] is None
        assert result["status"] == "ack"

    def test_ack_with_omitted_default_error_reason_is_success(self):
        plugin = _make_started_plugin()
        plugin._test_running = True
        send_mono = time.monotonic()
        callback = plugin._make_probe_callback(1, send_mono, time.time())
        plugin._pending_probes[99] = (
            send_mono,
            time.time(),
            1,
            plugin._test_generation,
        )
        packet = _routing_response(99)
        packet["decoded"]["routing"].pop("errorReason")

        callback(packet)

        assert plugin._probes_acked == 1
        assert plugin._probes_lost == 0
        assert plugin._history[-1]["status"] == "ack"

    def test_unknown_or_prior_generation_ack_is_ignored(self):
        plugin = _make_started_plugin()
        plugin._test_running = True
        send_mono = time.monotonic()
        send_wall = time.time()
        callback = plugin._make_probe_callback(
            seq=1,
            send_mono=send_mono,
            send_wall=send_wall,
            generation=plugin._test_generation,
        )

        callback(_routing_response(99))
        plugin._test_generation += 1
        callback(_routing_response(100))

        assert plugin._probes_acked == 0
        assert list(plugin._history) == []

    def test_synchronous_ack_during_send_is_correlated_once(self):
        plugin = _make_started_plugin()
        plugin._test_running = True
        generation = plugin._test_generation

        def send_data(**kwargs):
            kwargs["onResponse"](_routing_response(77, rssi=-80, snr=8.0))
            return {"id": 77}

        plugin._interface.sendData.side_effect = send_data

        plugin._send_probe("!11223344", 0, generation)

        assert plugin._probes_sent == 1
        assert plugin._probes_acked == 1
        assert plugin._pending_probes == {}
        assert len(plugin._history) == 1

    def test_correlated_routing_nak_is_loss_not_ack(self):
        plugin = _make_started_plugin()
        plugin._test_running = True
        send_mono = time.monotonic()
        send_wall = time.time()
        callback = plugin._make_probe_callback(3, send_mono, send_wall)
        plugin._pending_probes[55] = (send_mono, send_wall, 3, plugin._test_generation)

        callback(_routing_response(55, error_reason="NO_ROUTE"))

        assert plugin._probes_acked == 0
        assert plugin._probes_lost == 1
        assert plugin._pending_probes == {}
        assert plugin._history[-1]["status"] == "nak"
        assert plugin._history[-1]["error_reason"] == "NO_ROUTE"

    @pytest.mark.parametrize(
        "packet",
        [
            {"id": 55},
            {"decoded": {"requestId": 55, "portnum": "TEXT_MESSAGE_APP"}},
            {
                "decoded": {
                    "requestId": 55,
                    "portnum": "ROUTING_APP",
                    "routing": None,
                }
            },
        ],
    )
    def test_non_routing_or_malformed_response_is_ignored(self, packet):
        plugin = _make_started_plugin()
        plugin._test_running = True
        send_mono = time.monotonic()
        callback = plugin._make_probe_callback(3, send_mono, time.time())
        plugin._pending_probes[55] = (send_mono, time.time(), 3, plugin._test_generation)

        callback(packet)

        assert plugin._probes_acked == 0
        assert plugin._probes_lost == 0
        assert 55 in plugin._pending_probes

    def test_hung_send_blocks_reopen_and_retains_lease_until_worker_returns(
        self,
        _patch_meshtastic,
    ):
        plugin = _make_started_plugin()
        plugin._test_running = True
        generation = plugin._test_generation
        lease = _patch_meshtastic.claim.return_value
        plugin._serial_device_lease = lease
        send_gate = threading.Event()
        released = threading.Event()
        plugin._interface.sendData.side_effect = lambda **kwargs: (
            send_gate.wait(timeout=5) or {"id": 88}
        )
        lease.release.side_effect = lambda: released.set()

        with (
            patch(
                "reticulumpi.builtin_plugins.lora_link_tester._SERIAL_SEND_TIMEOUT",
                0.02,
            ),
            patch("reticulumpi.builtin_plugins.lora_link_tester.record_hung_worker"),
            pytest.raises(TimeoutError, match="serial send timed out"),
        ):
            plugin._send_probe("!11223344", 0, generation)

        assert plugin._connected is False
        assert plugin._serial_send_attempts
        assert plugin._pending_probe_sends == set()
        with pytest.raises(RuntimeError, match="serial send is unresolved"):
            plugin._open_interface()
        _mock_meshtastic_serial.SerialInterface.assert_not_called()

        plugin._join_threads = MagicMock()
        with patch(
            "reticulumpi.builtin_plugins.lora_link_tester._SERIAL_WORKER_SHUTDOWN_TIMEOUT",
            0.01,
        ):
            plugin.stop()
        lease.release.assert_not_called()

        send_gate.set()
        assert plugin._wait_for_serial_send_workers(2)
        assert released.wait(timeout=2)
        assert plugin._serial_device_lease is None

    def test_fast_send_failure_fences_and_closes_interface_for_reconnect(self):
        plugin = _make_started_plugin()
        plugin._test_running = True
        generation = plugin._test_generation
        iface = plugin._interface
        open_generation = plugin._serial_open_generation
        send_generation = plugin._serial_send_generation
        iface.sendData.side_effect = OSError("USB disappeared")

        with pytest.raises(OSError, match="USB disappeared"):
            plugin._send_probe("!11223344", 0, generation)

        assert plugin._interface is None
        assert plugin._connected is False
        assert plugin._status == "error"
        assert plugin._serial_open_generation == open_generation + 1
        assert plugin._serial_send_generation > send_generation
        assert plugin._pending_probe_sends == set()
        assert plugin._early_probe_acks == {}
        iface.close.assert_called_once_with()
        plugin.event_bus.publish.assert_any_call(
            "link_test.connection_changed",
            {
                "connected": False,
                "error": "USB disappeared",
            },
        )

        def reopen_once():
            plugin._connected = True
            plugin._active = False

        plugin._retry_unresolved_serial_handles = MagicMock(return_value=True)
        plugin._open_interface = MagicMock(side_effect=reopen_once)
        plugin._connection_loop()

        plugin._open_interface.assert_called_once_with()
        assert plugin._connected is True

    def test_late_ack_from_stopped_test_cannot_corrupt_next_test(self):
        plugin = _make_started_plugin()
        plugin._test_running = True
        old_generation = plugin._test_generation
        send_mono = time.monotonic()
        send_wall = time.time()
        callback = plugin._make_probe_callback(0, send_mono, send_wall, old_generation)
        plugin._pending_probes[42] = (send_mono, send_wall, 0, old_generation)

        plugin.stop_test()
        plugin._test_running = True
        plugin._test_generation += 1
        callback(_routing_response(42))

        assert plugin._probes_acked == 0
        assert list(plugin._history) == []

    def test_callback_paused_before_commit_cannot_outlive_generation(self):
        plugin = _make_started_plugin()
        plugin._test_running = True
        generation = plugin._test_generation
        send_mono = time.monotonic()
        send_wall = time.time()
        plugin._pending_probes[42] = (send_mono, send_wall, 0, generation)

        comparison_entered = threading.Event()
        release_comparison = threading.Event()

        class PausingAckReason:
            def __eq__(self, other):
                comparison_entered.set()
                assert release_comparison.wait(timeout=2)
                return other == "NONE"

        plugin._parse_routing_response = MagicMock(
            return_value=(42, PausingAckReason()),
        )
        callback = plugin._make_probe_callback(0, send_mono, send_wall, generation)
        callback_thread = threading.Thread(target=lambda: callback({}), daemon=True)
        callback_thread.start()
        assert comparison_entered.wait(timeout=2)

        # The result is fully constructed before the callback acquires the
        # admission lock, so stopping this generation cannot be held hostage
        # by a half-committed callback.
        stopped = threading.Event()
        stop_thread = threading.Thread(
            target=lambda: (plugin.stop_test(), stopped.set()),
            daemon=True,
        )
        stop_thread.start()
        try:
            assert stopped.wait(timeout=1)
        finally:
            release_comparison.set()

        callback_thread.join(timeout=2)
        stop_thread.join(timeout=2)
        assert not callback_thread.is_alive()
        assert plugin._probes_acked == 0
        assert plugin._probes_lost == 0
        assert list(plugin._history) == []
        assert not any(
            call.args[0] == "link_test.probe_result"
            for call in plugin.event_bus.publish.call_args_list
        )

    def test_stop_after_history_linearization_retains_admitted_result(self):
        plugin = _make_started_plugin()
        plugin._test_running = True
        generation = plugin._test_generation
        send_mono = time.monotonic()
        send_wall = time.time()
        plugin._pending_probes[42] = (send_mono, send_wall, 0, generation)
        publish_entered = threading.Event()
        release_publish = threading.Event()

        def pause_result_publish(event_type, _data):
            if event_type == "link_test.probe_result":
                publish_entered.set()
                assert release_publish.wait(timeout=2)

        plugin.event_bus.publish.side_effect = pause_result_publish
        callback = plugin._make_probe_callback(0, send_mono, send_wall, generation)
        callback_thread = threading.Thread(
            target=lambda: callback(_routing_response(42)),
            daemon=True,
        )
        callback_thread.start()
        assert publish_entered.wait(timeout=2)

        stopped = plugin.stop_test()
        release_publish.set()
        callback_thread.join(timeout=2)

        assert stopped["stats"]["acked"] == 1
        assert plugin._probes_acked == 1
        assert len(plugin._history) == 1
        assert plugin._history[0]["status"] == "ack"
        assert not callback_thread.is_alive()


# ===========================================================================
# TestTimeoutSweep
# ===========================================================================


class TestTimeoutSweep:
    def test_timeout_marks_lost(self):
        plugin = _make_started_plugin()
        old_mono = time.monotonic() - 60
        old_wall = time.time() - 60
        plugin._pending_probes[100] = (old_mono, old_wall, 5, plugin._test_generation)

        plugin._sweep_timeouts()

        assert plugin._probes_lost == 1
        assert 100 not in plugin._pending_probes
        assert len(plugin._history) == 1
        assert plugin._history[0]["status"] == "lost"
        assert plugin._history[0]["seq"] == 5
        assert plugin._history[0]["rtt_ms"] is None

    def test_no_timeout_for_recent_probes(self):
        plugin = _make_started_plugin()
        plugin._pending_probes[200] = (
            time.monotonic(),
            time.time(),
            0,
            plugin._test_generation,
        )

        plugin._sweep_timeouts()

        assert plugin._probes_lost == 0
        assert 200 in plugin._pending_probes
        assert len(plugin._history) == 0


# ===========================================================================
# TestStatistics
# ===========================================================================


class TestStatistics:
    def test_empty_stats(self):
        plugin = _make_started_plugin()
        stats = plugin._compute_stats()
        assert stats["sent"] == 0
        assert stats["loss_pct"] == 0.0
        assert stats["rtt_min"] is None
        assert stats["rssi_avg"] is None

    def test_stats_with_data(self):
        plugin = _make_started_plugin()
        plugin._probes_sent = 3
        plugin._probes_acked = 2
        plugin._probes_lost = 1
        plugin._history.extend(
            [
                {"seq": 0, "time": 1.0, "rtt_ms": 1000.0, "rssi": -90, "snr": 8.0, "status": "ack"},
                {
                    "seq": 1,
                    "time": 2.0,
                    "rtt_ms": 2000.0,
                    "rssi": -100,
                    "snr": 4.0,
                    "status": "ack",
                },
                {
                    "seq": 2,
                    "time": 3.0,
                    "rtt_ms": None,
                    "rssi": None,
                    "snr": None,
                    "status": "lost",
                },
            ]
        )

        stats = plugin._compute_stats()
        assert stats["sent"] == 3
        assert stats["acked"] == 2
        assert stats["lost"] == 1
        assert stats["loss_pct"] == pytest.approx(33.3, abs=0.1)
        assert stats["rtt_min"] == 1000.0
        assert stats["rtt_avg"] == 1500.0
        assert stats["rtt_max"] == 2000.0
        assert stats["rssi_avg"] == -95.0
        assert stats["snr_avg"] == 6.0


# ===========================================================================
# TestSnapshot
# ===========================================================================


class TestSnapshot:
    def test_snapshot_structure(self):
        plugin = _make_started_plugin()
        snap = plugin.get_snapshot()
        assert snap["available"] is True
        assert "connected" in snap
        assert "status" in snap
        assert "test_running" in snap
        assert "results" in snap
        assert "stats" in snap
        assert isinstance(snap["results"], list)

    def test_snapshot_tails_history(self):
        plugin = _make_started_plugin()
        for i in range(20):
            plugin._history.append(
                {
                    "seq": i,
                    "time": float(i),
                    "rtt_ms": 100.0,
                    "rssi": -80,
                    "snr": 5.0,
                    "status": "ack",
                }
            )

        snap = plugin.get_snapshot()
        assert len(snap["results"]) == 10
        assert snap["results"][0]["seq"] == 10

    def test_history_returns_full_buffer(self):
        plugin = _make_started_plugin()
        for i in range(20):
            plugin._history.append(
                {
                    "seq": i,
                    "time": float(i),
                    "rtt_ms": 100.0,
                    "rssi": -80,
                    "snr": 5.0,
                    "status": "ack",
                }
            )

        hist = plugin.get_history()
        assert len(hist["results"]) == 20


# ===========================================================================
# TestClearHistory
# ===========================================================================


class TestClearHistory:
    def test_clear_empties_buffer(self):
        plugin = _make_started_plugin()
        plugin._history.append({"seq": 0})
        plugin._probes_sent = 5
        plugin._probes_acked = 3
        plugin._probes_lost = 2

        result = plugin.clear_history()
        assert result["ok"] is True
        assert len(plugin._history) == 0
        assert plugin._probes_sent == 0
        assert plugin._probes_acked == 0
        assert plugin._probes_lost == 0


# ===========================================================================
# TestGetStatus
# ===========================================================================


class TestGetStatus:
    def test_status_fields(self):
        plugin = _make_started_plugin(_make_config(target_node_id="!abcd1234"))
        status = plugin.get_status()
        assert status["active"] is True
        assert status["connected"] is True
        assert status["status"] == "idle"
        assert status["test_running"] is False
        assert status["target"] == "!abcd1234"
        assert status["serial_port"] == "/dev/lora-link-tester"
        assert status["probes_sent"] == 0
