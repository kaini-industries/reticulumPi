"""Focused application tests for external RNS serial-device reservations."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from reticulumpi.app import ReticulumPiApp
from reticulumpi.serial_devices import (
    SerialDeviceBusyError,
    SerialDeviceIdentity,
    SerialDeviceRegistry,
)


def _make_app(tmp_path, rns_config: str | None, *, explicit_dir: bool = True):
    rns_dir = tmp_path / "rns"
    rns_dir.mkdir()
    if rns_config is not None:
        (rns_dir / "config").write_text(rns_config, encoding="utf-8")
    app_config = tmp_path / "reticulumpi.yaml"
    config_dir_line = f"  reticulum_config_dir: {rns_dir}\n" if explicit_dir else ""
    app_config.write_text(
        "reticulumpi:\n"
        f"{config_dir_line}"
        "  identity_path: ~/.config/reticulumpi/test-identity\n"
        "  plugins: {}\n",
        encoding="utf-8",
    )
    return ReticulumPiApp(config_path=str(app_config)), rns_dir


_RNS_INTERFACES = """\
[interfaces]
[[Primary RNode]]
  type = RNodeInterface
  enabled = yes
  port = /dev/rnode-primary
[[Disabled RNode]]
  type = RNodeInterface
  enabled = no
  port = /dev/rnode-disabled
[[TCP]]
  type = TCPClientInterface
  enabled = yes
  port = /dev/not-a-serial-interface
"""


def test_reserves_only_enabled_rnode_interfaces(tmp_path):
    app, _rns_dir = _make_app(tmp_path, _RNS_INTERFACES)
    lease = MagicMock()
    registry = MagicMock()
    registry.reserve_external_intent.return_value = lease

    with patch("reticulumpi.app.serial_device_registry", registry):
        app._reserve_rns_serial_devices()

    registry.reserve_external_intent.assert_called_once_with(
        "/dev/rnode-primary",
        "rns:Primary RNode",
    )
    assert app._rns_serial_reservations == [lease]


@pytest.mark.parametrize(
    "interface_type",
    [
        "RNodeInterface",
        "RNodeMultiInterface",
        "SerialInterface",
        "KISSInterface",
        "AX25KISSInterface",
        "WeaveInterface",
    ],
)
def test_reserves_every_rns_interface_type_that_owns_a_serial_port(
    tmp_path,
    interface_type,
):
    config = f"""\
[interfaces]
[[Serial Owner]]
  type = {interface_type}
  enabled = yes
  port = /dev/serial-owner
[[[Nested Channel]]]
  enabled = yes
  frequency = 915000000
"""
    app, _rns_dir = _make_app(tmp_path, config)
    registry = MagicMock()
    lease = MagicMock()
    registry.reserve_external_intent.return_value = lease

    with patch("reticulumpi.app.serial_device_registry", registry):
        app._reserve_rns_serial_devices()

    registry.reserve_external_intent.assert_called_once_with(
        "/dev/serial-owner",
        "rns:Serial Owner",
    )
    assert app._rns_serial_reservations == [lease]


def test_configobj_forms_reserve_exact_resolved_values(tmp_path):
    config = """\
 [interfaces] # section comment
 [["Quoted RNode"]] # interface comment
 type = "RNodeInterface" # type comment
 interface_enabled = "yes" # legacy RNS flag
 device = "/dev/serial/by-id/radio#one"
 port = "%(device)s" # interpolated exact value
 [[Unindented Weave]]
type = 'WeaveInterface' # type comment
enabled = 'on' # enabled comment
port = '/dev/weave path#one' # port comment
"""
    app, _rns_dir = _make_app(tmp_path, config)
    registry = MagicMock()
    first_lease = MagicMock()
    second_lease = MagicMock()
    registry.reserve_external_intent.side_effect = [first_lease, second_lease]

    with patch("reticulumpi.app.serial_device_registry", registry):
        app._reserve_rns_serial_devices()

    assert registry.reserve_external_intent.call_args_list == [
        call("/dev/serial/by-id/radio#one", "rns:Quoted RNode"),
        call("/dev/weave path#one", "rns:Unindented Weave"),
    ]
    assert app._rns_serial_reservations == [first_lease, second_lease]


@pytest.mark.parametrize(
    "flags",
    [
        "interface_enabled = yes",
        "interface_enabled = no\nenabled = yes",
        "interface_enabled = yes\nenabled = definitely-not-a-boolean",
    ],
)
def test_enable_predicate_matches_rns_short_circuit_or_semantics(tmp_path, flags):
    config = f"""\
[interfaces]
[[RNode]]
type = RNodeInterface
{flags}
port = /dev/rnode
"""
    app, _rns_dir = _make_app(tmp_path, config)
    registry = MagicMock()
    registry.reserve_external_intent.return_value = MagicMock()

    with patch("reticulumpi.app.serial_device_registry", registry):
        app._reserve_rns_serial_devices()

    registry.reserve_external_intent.assert_called_once_with("/dev/rnode", "rns:RNode")


@pytest.mark.parametrize(
    "flags",
    [
        "",
        "enabled = no",
        "interface_enabled = no",
        "interface_enabled = no\nenabled = no",
    ],
)
def test_rns_disabled_forms_do_not_reserve_even_without_port(tmp_path, flags):
    config = f"""\
[interfaces]
[[RNode]]
type = RNodeInterface
{flags}
"""
    app, _rns_dir = _make_app(tmp_path, config)
    registry = MagicMock()

    with patch("reticulumpi.app.serial_device_registry", registry):
        app._reserve_rns_serial_devices()

    registry.reserve_external_intent.assert_not_called()


@pytest.mark.parametrize(
    ("property_line", "message"),
    [
        ("enabled = yes", "must have one non-empty scalar port"),
        ("enabled = yes\nport =", "must have one non-empty scalar port"),
        ("enabled = yes\nport = /dev/one, /dev/two", "must have one non-empty scalar port"),
        ("enabled = yes\nport = %(missing)s", "unresolved port"),
        (
            "enabled = yes\ndevice = /dev/one, /dev/two\nport = %(device)s",
            "unresolved port",
        ),
        ("enabled = maybe\nport = /dev/rnode", "invalid enabled flag"),
        ("interface_enabled = no\nenabled = maybe\nport = /dev/rnode", "invalid enabled flag"),
    ],
)
def test_unsafe_enabled_serial_configuration_fails_closed(
    tmp_path,
    property_line,
    message,
):
    config = f"""\
[interfaces]
[[Unsafe RNode]]
type = RNodeInterface
{property_line}
"""
    app, rns_dir = _make_app(tmp_path, config)
    registry = MagicMock()

    with (
        patch("reticulumpi.app.serial_device_registry", registry),
        pytest.raises(RuntimeError, match="Could not inspect RNS config") as raised,
    ):
        app._reserve_rns_serial_devices()

    assert message in str(raised.value.__cause__)
    registry.reserve_external_intent.assert_not_called()
    assert str(rns_dir / "config") not in app._rns_serial_config_paths_attempted


def test_invalid_configobj_syntax_fails_before_any_reservation(tmp_path):
    config = """\
[interfaces]
[[Broken]]
type = RNodeInterface
enabled = yes
port = "/dev/unclosed
"""
    app, rns_dir = _make_app(tmp_path, config)
    registry = MagicMock()

    with (
        patch("reticulumpi.app.serial_device_registry", registry),
        pytest.raises(RuntimeError, match="Could not inspect RNS config"),
    ):
        app._reserve_rns_serial_devices()

    registry.reserve_external_intent.assert_not_called()
    assert str(rns_dir / "config") not in app._rns_serial_config_paths_attempted


def test_later_unsafe_interface_prevents_all_reservations(tmp_path):
    config = """\
[interfaces]
[[Valid First]]
type = RNodeInterface
enabled = yes
port = /dev/first
[[Unsafe Second]]
type = WeaveInterface
enabled = yes
"""
    app, rns_dir = _make_app(tmp_path, config)
    registry = MagicMock()

    with (
        patch("reticulumpi.app.serial_device_registry", registry),
        pytest.raises(RuntimeError, match="Could not inspect RNS config"),
    ):
        app._reserve_rns_serial_devices()

    registry.reserve_external_intent.assert_not_called()
    assert str(rns_dir / "config") not in app._rns_serial_config_paths_attempted


def test_interface_type_matching_has_rns_case_semantics(tmp_path):
    config = """\
[interfaces]
[[Not an RNS built-in type]]
type = rnodeinterface
enabled = yes
port = /dev/rnode
"""
    app, _rns_dir = _make_app(tmp_path, config)
    registry = MagicMock()

    with patch("reticulumpi.app.serial_device_registry", registry):
        app._reserve_rns_serial_devices()

    registry.reserve_external_intent.assert_not_called()


def test_absent_rns_config_is_a_safe_noop(tmp_path):
    app, _rns_dir = _make_app(tmp_path, None)
    registry = MagicMock()

    with patch("reticulumpi.app.serial_device_registry", registry):
        app._reserve_rns_serial_devices()

    registry.reserve_external_intent.assert_not_called()
    assert app._rns_serial_reservations == []


def test_unreadable_or_invalid_rns_config_fails_closed(tmp_path):
    app, rns_dir = _make_app(tmp_path, _RNS_INTERFACES)

    with (
        patch(
            "reticulumpi.app.parse_enabled_rns_serial_interfaces",
            side_effect=UnicodeError("invalid config encoding"),
        ),
        pytest.raises(RuntimeError, match="Could not inspect RNS config"),
    ):
        app._reserve_rns_serial_devices()

    assert str(rns_dir / "config") not in app._rns_serial_config_paths_attempted
    assert app._rns_serial_reservations == []


def test_mocked_rns_config_path_is_a_safe_noop(tmp_path):
    app, _rns_dir = _make_app(tmp_path, None, explicit_dir=False)
    app.reticulum = MagicMock()

    with (
        patch("reticulumpi.app.parse_enabled_rns_serial_interfaces") as parse_config,
        patch("reticulumpi.app.serial_device_registry") as registry,
    ):
        app._reserve_rns_serial_devices()

    parse_config.assert_not_called()
    registry.reserve_external_intent.assert_not_called()


def test_offline_rnode_receives_pending_path_reservation(tmp_path, caplog):
    app, _rns_dir = _make_app(tmp_path, _RNS_INTERFACES)
    registry = MagicMock()
    lease = MagicMock()
    lease.identity = None
    registry.reserve_external_intent.return_value = lease

    with (
        patch("reticulumpi.app.serial_device_registry", registry),
        caplog.at_level(logging.INFO),
    ):
        app._reserve_rns_serial_devices()

    assert app._rns_serial_reservations == [lease]
    assert "Reserved pending serial path" in caplog.text
    assert "/dev/rnode-primary" in caplog.text


def test_real_registry_external_reservation_blocks_local_claim(tmp_path):
    config = """\
[interfaces]
[[RNode]]
  type = RNodeInterface
  enabled = yes
  port = /dev/null
"""
    app, _rns_dir = _make_app(tmp_path, config)
    registry = SerialDeviceRegistry(sysfs_root=tmp_path / "empty-sys")

    with patch("reticulumpi.app.serial_device_registry", registry):
        app._reserve_rns_serial_devices()
        with pytest.raises(SerialDeviceBusyError):
            registry.claim("/dev/null", "application-plugin")
        app._release_rns_serial_reservations()
        plugin_lease = registry.claim("/dev/null", "application-plugin")

    plugin_lease.release()


def test_partial_reservations_roll_back_on_conflict(tmp_path):
    config = """\
[interfaces]
[[First]]
  type = RNodeInterface
  enabled = yes
  port = /dev/first
[[Second]]
  type = RNodeInterface
  enabled = yes
  port = /dev/second
"""
    app, rns_dir = _make_app(tmp_path, config)
    first_lease = MagicMock()
    identity = SerialDeviceIdentity("/dev/second", "/dev/ttyACM1", 166, 1)
    busy = SerialDeviceBusyError(
        "/dev/second",
        ("other-owner",),
        identity,
        external=False,
    )
    registry = MagicMock()
    registry.reserve_external_intent.side_effect = [first_lease, busy]

    with (
        patch("reticulumpi.app.serial_device_registry", registry),
        pytest.raises(SerialDeviceBusyError),
    ):
        app._reserve_rns_serial_devices()

    first_lease.release.assert_called_once_with()
    assert app._rns_serial_reservations == []
    assert str(rns_dir / "config") not in app._rns_serial_config_paths_attempted


def test_partial_rollback_releases_every_lease_and_preserves_original_error(tmp_path):
    config = """\
[interfaces]
[[First]]
type = RNodeInterface
enabled = yes
port = /dev/first
[[Second]]
type = RNodeInterface
enabled = yes
port = /dev/second
[[Third]]
type = RNodeInterface
enabled = yes
port = /dev/third
"""
    app, rns_dir = _make_app(tmp_path, config)
    first_lease = MagicMock()
    second_lease = MagicMock()
    second_lease.release.side_effect = RuntimeError("release failed")
    busy = SerialDeviceBusyError(
        "/dev/third",
        ("other-owner",),
        SerialDeviceIdentity("/dev/third", "/dev/ttyACM2", 166, 2),
        external=False,
    )
    registry = MagicMock()
    registry.reserve_external_intent.side_effect = [first_lease, second_lease, busy]

    with (
        patch("reticulumpi.app.serial_device_registry", registry),
        pytest.raises(SerialDeviceBusyError) as raised,
    ):
        app._reserve_rns_serial_devices()

    assert raised.value is busy
    second_lease.release.assert_called_once_with()
    first_lease.release.assert_called_once_with()
    assert app._rns_serial_reservations == []
    assert str(rns_dir / "config") not in app._rns_serial_config_paths_attempted


def test_explicit_rnode_port_is_reserved_before_rns_and_plugins(tmp_path):
    config = """\
[interfaces]
[[Primary]]
  type = RNodeInterface
  enabled = yes
  port = /dev/rnode
"""
    app, _rns_dir = _make_app(tmp_path, config)
    events: list[str] = []
    lease = MagicMock()
    registry = MagicMock()

    def reserve_external_intent(*_args):
        events.append("reserve")
        return lease

    registry.reserve_external_intent.side_effect = reserve_external_intent

    def make_reticulum(**_kwargs):
        events.append("rns")
        return MagicMock()

    identity = SimpleNamespace(hash=b"\x00" * 16)
    app._shutdown_event.set()
    with (
        patch("reticulumpi.app.serial_device_registry", registry),
        patch("reticulumpi.app.RNS.Reticulum", side_effect=make_reticulum),
        patch("reticulumpi.app.identity_manager.load_or_create", return_value=identity),
        patch.object(app.announce_dispatcher, "start"),
        patch.object(app.sdr_scheduler, "start"),
        patch("reticulumpi.app.InternetProbe") as probe_class,
        patch.object(app, "_load_plugins", side_effect=lambda: events.append("plugins")),
        patch.object(app, "_print_startup_report"),
        patch.object(app, "_install_signal_handlers"),
        patch.object(app, "shutdown"),
        patch("reticulumpi.app.set_readiness_file"),
        patch("reticulumpi.app.systemd_ready"),
    ):
        probe_class.return_value.is_online = True
        app.start()

    assert events == ["reserve", "rns", "plugins"]
    app._release_rns_serial_reservations()


def test_rns_constructor_failure_releases_preexisting_reservation(tmp_path):
    config = """\
[interfaces]
[[Primary]]
  type = RNodeInterface
  enabled = yes
  port = /dev/rnode
"""
    app, _rns_dir = _make_app(tmp_path, config)
    lease = MagicMock()
    registry = MagicMock()
    registry.reserve_external_intent.return_value = lease

    with (
        patch("reticulumpi.app.serial_device_registry", registry),
        patch("reticulumpi.app.RNS.Reticulum", side_effect=RuntimeError("RNS failed")),
        patch("reticulumpi.app.set_readiness_file"),
        pytest.raises(RuntimeError, match="RNS failed"),
    ):
        app.start()

    lease.release.assert_called_once_with()
    assert app._rns_serial_reservations == []


def test_post_initialization_reservation_failure_cleans_up_rns(tmp_path):
    app, _rns_dir = _make_app(tmp_path, None, explicit_dir=False)
    reticulum = SimpleNamespace(configpath=str(tmp_path / "selected" / "config"))

    with (
        patch.object(
            app,
            "_reserve_rns_serial_devices",
            side_effect=[None, RuntimeError("unsafe selected config")],
        ) as reserve,
        patch("reticulumpi.app.RNS.Reticulum", return_value=reticulum),
        patch("reticulumpi.app.RNS.Transport.exit_handler") as exit_handler,
        patch("reticulumpi.app.set_readiness_file"),
        pytest.raises(RuntimeError, match="unsafe selected config"),
    ):
        app.start()

    assert reserve.call_count == 2
    exit_handler.assert_called_once_with()
    assert app.reticulum is None


def test_rns_selected_config_path_is_reserved_after_initialization(tmp_path):
    app, rns_dir = _make_app(tmp_path, _RNS_INTERFACES, explicit_dir=False)
    app.reticulum = SimpleNamespace(configpath=str(rns_dir / "config"))
    lease = MagicMock()
    registry = MagicMock()
    registry.reserve_external_intent.return_value = lease

    with patch("reticulumpi.app.serial_device_registry", registry):
        app._reserve_rns_serial_devices()

    registry.reserve_external_intent.assert_called_once_with(
        "/dev/rnode-primary",
        "rns:Primary RNode",
    )


def test_shutdown_releases_reservations_after_rns_cleanup(tmp_path):
    app, _rns_dir = _make_app(tmp_path, None)
    order: list[str] = []
    lease = MagicMock()
    lease.release.side_effect = lambda: order.append("release")
    app._rns_serial_reservations = [lease]
    app.reticulum = MagicMock()
    app.event_bus = MagicMock()
    app.sdr_scheduler = MagicMock()
    app.announce_dispatcher = MagicMock()

    with (
        patch(
            "reticulumpi.app.RNS.Transport.exit_handler",
            side_effect=lambda: order.append("rns-cleanup"),
        ),
        patch("reticulumpi.app.set_readiness_file"),
        patch("reticulumpi.app.systemd_stopping"),
    ):
        app.shutdown()

    assert order == ["rns-cleanup", "release"]
    assert app._rns_serial_reservations == []
    assert app.reticulum is None


def test_failed_rns_cleanup_retains_instance_and_reservations(tmp_path, caplog):
    app, _rns_dir = _make_app(tmp_path, None)
    lease = MagicMock()
    reticulum = MagicMock()
    app._rns_serial_reservations = [lease]
    app.reticulum = reticulum

    with (
        patch(
            "reticulumpi.app.RNS.Transport.exit_handler",
            side_effect=RuntimeError("transport still live"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        app._cleanup_rns()

    assert app.reticulum is reticulum
    assert app._rns_serial_reservations == [lease]
    lease.release.assert_not_called()
    assert "transport cleanup failed" in caplog.text


def test_cleanup_without_rns_releases_preinitialization_reservations(tmp_path):
    app, _rns_dir = _make_app(tmp_path, None)
    lease = MagicMock()
    app._rns_serial_reservations = [lease]
    app.reticulum = None

    app._cleanup_rns()

    lease.release.assert_called_once_with()
    assert app._rns_serial_reservations == []


def test_shutdown_in_progress_prevents_new_rns_reservations(tmp_path):
    app, rns_dir = _make_app(tmp_path, _RNS_INTERFACES)
    app._shutting_down.set()
    registry = MagicMock()

    with patch("reticulumpi.app.serial_device_registry", registry):
        app._reserve_rns_serial_devices()

    registry.reserve_external_intent.assert_not_called()
    assert str(rns_dir / "config") not in app._rns_serial_config_paths_attempted
    assert app._rns_serial_reservations == []


def test_release_continues_when_one_lease_raises(tmp_path):
    app, _rns_dir = _make_app(tmp_path, None)
    first = MagicMock()
    second = MagicMock()
    second.release.side_effect = RuntimeError("broken release")
    app._rns_serial_reservations = [first, second]

    app._release_rns_serial_reservations()

    assert [second.release.call_count, first.release.call_count] == [1, 1]
    assert app._rns_serial_reservations == []
