"""Tests for canonical serial-device identity and ownership claims."""

from __future__ import annotations

import os
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import pytest

from reticulumpi import serial_devices
from reticulumpi.serial_devices import (
    InvalidSerialShareTokenError,
    SerialDeviceBusyError,
    SerialDeviceChangedError,
    SerialDeviceIdentity,
    SerialDeviceIdentityError,
    SerialDeviceIntentLease,
    SerialDeviceRegistry,
    StaleSerialDeviceLeaseError,
    USBDeviceIdentity,
    resolve_serial_device,
    validate_stable_serial_path,
)


def _identity(
    configured: str,
    *,
    canonical: str,
    major: int,
    minor: int,
    usb_parent: str | None = None,
    serial_number: str | None = None,
) -> SerialDeviceIdentity:
    usb = None
    if usb_parent is not None:
        usb = USBDeviceIdentity(usb_parent, "239A", "8029", serial_number)
    return SerialDeviceIdentity(configured, canonical, major, minor, usb)


def _mapped_resolver(mapping: dict[str, SerialDeviceIdentity]):
    def resolve(configured_path, *, sysfs_root):
        del sysfs_root
        return mapping[os.fspath(configured_path)]

    return resolve


class TestValidateStableSerialPath:
    @pytest.mark.parametrize(
        "path",
        [
            "/dev/gps",
            "/dev/serial/by-id/usb-Radio_123-if00",
            "/dev/serial/by-path/platform-xhci-hcd.0-usb-0:1:1.0",
            "/dev/ttyAMA0",
            "/dev/ttyS0",
        ],
    )
    def test_accepts_persistent_device_paths(self, path):
        assert validate_stable_serial_path(path) == path

    @pytest.mark.parametrize(
        "path",
        [
            None,
            False,
            1,
            "",
            "   ",
            "auto",
            "AUTO",
            "dev/radio",
            "/tmp/radio",
            "/dev/radio ",
            "/dev/radio\x00suffix",
            "/dev/ttyUSB0",
            "/dev/ttyUSB42",
            "/dev/ttyACM0",
            "/dev/ttyACM42",
        ],
    )
    def test_rejects_implicit_or_volatile_paths(self, path):
        with pytest.raises(ValueError, match="stable serial device path"):
            validate_stable_serial_path(path, "device_port")

    @pytest.mark.parametrize(
        "path",
        [
            "/dev/../dev/ttyACM0",
            "/dev/serial/by-id/../../../dev/ttyUSB42",
            "/dev/serial/../ttyACM0",
            "/dev/./gps",
        ],
    )
    def test_rejects_lexical_traversal_aliases(self, path):
        with pytest.raises(ValueError, match="stable serial device path"):
            validate_stable_serial_path(path)

    @pytest.mark.parametrize(
        "path",
        [
            "/dev//ttyACM0",
            "/dev//ttyUSB42",
            "/dev//serial/by-id/usb-Radio_123-if00",
        ],
    )
    def test_rejects_doubled_slashes(self, path):
        with pytest.raises(ValueError, match="stable serial device path"):
            validate_stable_serial_path(path)


class TestResolveSerialDevice:
    def test_resolves_alias_and_nearest_usb_parent(self, tmp_path: Path):
        device_stat = os.stat("/dev/null")
        major = os.major(device_stat.st_rdev)
        minor = os.minor(device_stat.st_rdev)

        sysfs_root = tmp_path / "sys"
        usb_parent = sysfs_root / "devices/platform/usb1/1-2"
        tty_endpoint = usb_parent / "1-2:1.0/tty/ttyUSB0"
        tty_endpoint.mkdir(parents=True)
        (usb_parent / "idVendor").write_text("239A\n", encoding="ascii")
        (usb_parent / "idProduct").write_text("8029\n", encoding="ascii")
        (usb_parent / "serial").write_text(" RADIO-001 \n", encoding="ascii")
        char_link = sysfs_root / "dev/char" / f"{major}:{minor}"
        char_link.parent.mkdir(parents=True)
        char_link.symlink_to(tty_endpoint)

        configured = tmp_path / "radio-by-id"
        configured.symlink_to("/dev/null")
        identity = resolve_serial_device(configured, sysfs_root=sysfs_root)

        assert identity.configured_path == str(configured)
        assert identity.canonical_path == "/dev/null"
        assert identity.major == major
        assert identity.minor == minor
        assert identity.usb == USBDeviceIdentity(
            str(usb_parent),
            "239a",
            "8029",
            "RADIO-001",
        )
        assert ("path", "/dev/null") in identity.claim_keys
        assert ("device", (major, minor)) in identity.claim_keys
        assert ("usb_parent", str(usb_parent)) in identity.claim_keys
        assert ("usb_device", ("239a", "8029", "RADIO-001")) in identity.claim_keys

    def test_non_usb_character_device_keeps_endpoint_identity(self, tmp_path: Path):
        identity = resolve_serial_device("/dev/null", sysfs_root=tmp_path / "empty-sys")

        assert identity.canonical_path == "/dev/null"
        assert identity.usb is None
        assert len(identity.claim_keys) == 2

    def test_rejects_missing_path(self, tmp_path: Path):
        with pytest.raises(SerialDeviceIdentityError, match="does not exist"):
            resolve_serial_device(tmp_path / "missing", sysfs_root=tmp_path / "sys")

    def test_rejects_non_character_device(self, tmp_path: Path):
        regular_file = tmp_path / "not-a-tty"
        regular_file.write_text("data", encoding="ascii")

        with pytest.raises(SerialDeviceIdentityError, match="not a character device"):
            resolve_serial_device(regular_file, sysfs_root=tmp_path / "sys")

    def test_rejects_partial_usb_identity(self, tmp_path: Path):
        device_stat = os.stat("/dev/null")
        major = os.major(device_stat.st_rdev)
        minor = os.minor(device_stat.st_rdev)
        sysfs_root = tmp_path / "sys"
        usb_parent = sysfs_root / "devices/usb1/1-2"
        tty_endpoint = usb_parent / "1-2:1.0/tty/ttyUSB0"
        tty_endpoint.mkdir(parents=True)
        (usb_parent / "idVendor").write_text("239a\n", encoding="ascii")
        char_link = sysfs_root / "dev/char" / f"{major}:{minor}"
        char_link.parent.mkdir(parents=True)
        char_link.symlink_to(tty_endpoint)

        with pytest.raises(SerialDeviceIdentityError, match="missing idProduct"):
            resolve_serial_device("/dev/null", sysfs_root=sysfs_root)

    def test_identity_objects_are_immutable(self):
        identity = _identity(
            "/dev/radio",
            canonical="/dev/ttyACM0",
            major=166,
            minor=0,
            usb_parent="/sys/devices/usb1/1-2",
            serial_number="RADIO-001",
        )

        with pytest.raises(FrozenInstanceError):
            identity.minor = 1  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            identity.usb.serial_number = "changed"  # type: ignore[misc,union-attr]


class TestSerialDeviceClaims:
    def test_canonical_path_aliases_conflict(self):
        first = _identity("/dev/a", canonical="/dev/ttyACM0", major=166, minor=0)
        alias = _identity("/dev/by-id/a", canonical="/dev/ttyACM0", major=166, minor=0)
        registry = SerialDeviceRegistry()

        with patch(
            "reticulumpi.serial_devices.resolve_serial_device",
            side_effect=_mapped_resolver({"/dev/a": first, "/dev/by-id/a": alias}),
        ):
            lease = registry.claim("/dev/a", "meshtastic")
            with pytest.raises(SerialDeviceBusyError) as exc_info:
                registry.claim("/dev/by-id/a", "gps")

        assert exc_info.value.owners == ("meshtastic",)
        assert exc_info.value.identity is alias
        assert not exc_info.value.external
        lease.release()

    def test_major_minor_aliases_conflict_even_with_different_paths(self):
        first = _identity("/dev/a", canonical="/dev/ttyA", major=188, minor=0)
        second = _identity("/dev/b", canonical="/dev/ttyB", major=188, minor=0)
        registry = SerialDeviceRegistry()

        with patch(
            "reticulumpi.serial_devices.resolve_serial_device",
            side_effect=_mapped_resolver({"/dev/a": first, "/dev/b": second}),
        ):
            lease = registry.claim("/dev/a", "meshcore")
            with pytest.raises(SerialDeviceBusyError):
                registry.claim("/dev/b", "link-tester")

        lease.release()

    def test_sibling_interfaces_on_same_usb_parent_conflict(self):
        parent = "/sys/devices/usb1/1-2"
        first = _identity(
            "/dev/a",
            canonical="/dev/ttyACM0",
            major=166,
            minor=0,
            usb_parent=parent,
        )
        second = _identity(
            "/dev/b",
            canonical="/dev/ttyACM1",
            major=166,
            minor=1,
            usb_parent=parent,
        )
        registry = SerialDeviceRegistry()

        with patch(
            "reticulumpi.serial_devices.resolve_serial_device",
            side_effect=_mapped_resolver({"/dev/a": first, "/dev/b": second}),
        ):
            lease = registry.claim("/dev/a", "meshtastic")
            with pytest.raises(SerialDeviceBusyError):
                registry.claim("/dev/b", "gps")

        lease.release()

    def test_same_serialized_usb_device_conflicts_after_moving_ports(self):
        first = _identity(
            "/dev/a",
            canonical="/dev/ttyACM0",
            major=166,
            minor=0,
            usb_parent="/sys/devices/usb1/1-2",
            serial_number="RADIO-001",
        )
        moved = _identity(
            "/dev/b",
            canonical="/dev/ttyACM1",
            major=166,
            minor=1,
            usb_parent="/sys/devices/usb1/1-3",
            serial_number="RADIO-001",
        )
        registry = SerialDeviceRegistry()

        with patch(
            "reticulumpi.serial_devices.resolve_serial_device",
            side_effect=_mapped_resolver({"/dev/a": first, "/dev/b": moved}),
        ):
            lease = registry.claim("/dev/a", "meshtastic")
            with pytest.raises(SerialDeviceBusyError):
                registry.claim("/dev/b", "gps")

        lease.release()

    def test_distinct_devices_can_be_claimed(self):
        first = _identity("/dev/a", canonical="/dev/ttyACM0", major=166, minor=0)
        second = _identity("/dev/b", canonical="/dev/ttyUSB0", major=188, minor=0)
        registry = SerialDeviceRegistry()

        with patch(
            "reticulumpi.serial_devices.resolve_serial_device",
            side_effect=_mapped_resolver({"/dev/a": first, "/dev/b": second}),
        ):
            lease_a = registry.claim("/dev/a", "meshtastic")
            lease_b = registry.claim("/dev/b", "gps")

        assert registry.claim_count() == 2
        lease_a.release()
        lease_b.release()
        assert registry.claim_count() == 0

    def test_concurrent_claims_have_exactly_one_winner(self):
        identity = _identity("/dev/a", canonical="/dev/ttyACM0", major=166, minor=0)
        registry = SerialDeviceRegistry()
        barrier = threading.Barrier(2)
        results = []
        errors = []
        results_lock = threading.Lock()

        def resolve(configured_path, *, sysfs_root):
            del configured_path, sysfs_root
            barrier.wait(timeout=2)
            return identity

        def claim(owner: str) -> None:
            try:
                result = registry.claim("/dev/a", owner)
            except Exception as exc:  # results are asserted below
                with results_lock:
                    errors.append(exc)
            else:
                with results_lock:
                    results.append(result)

        with patch("reticulumpi.serial_devices.resolve_serial_device", side_effect=resolve):
            threads = [threading.Thread(target=claim, args=(owner,)) for owner in ("a", "b")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)

        assert all(not thread.is_alive() for thread in threads)
        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], SerialDeviceBusyError)
        results[0].release()

    def test_stale_release_cannot_release_a_later_claim(self):
        identity = _identity("/dev/a", canonical="/dev/ttyACM0", major=166, minor=0)
        registry = SerialDeviceRegistry()

        with patch(
            "reticulumpi.serial_devices.resolve_serial_device",
            return_value=identity,
        ):
            stale = registry.claim("/dev/a", "first")
            stale.release()
            current = registry.claim("/dev/a", "second")
            stale.release()
            with pytest.raises(SerialDeviceBusyError):
                registry.claim("/dev/a", "third")

        assert registry.claim_count() == 1
        current.release()


class TestRevalidation:
    def test_revalidation_returns_matching_current_identity(self):
        identity = _identity(
            "/dev/a",
            canonical="/dev/ttyACM0",
            major=166,
            minor=0,
            usb_parent="/sys/devices/usb1/1-2",
            serial_number="RADIO-001",
        )
        registry = SerialDeviceRegistry()

        with patch(
            "reticulumpi.serial_devices.resolve_serial_device",
            return_value=identity,
        ):
            lease = registry.claim("/dev/a", "meshtastic")
            assert lease.revalidate() is identity

        lease.release()

    @pytest.mark.parametrize(
        "changed",
        [
            _identity("/dev/a", canonical="/dev/ttyACM1", major=166, minor=1),
            _identity(
                "/dev/a",
                canonical="/dev/ttyACM0",
                major=166,
                minor=0,
                usb_parent="/sys/devices/usb1/1-3",
                serial_number="OTHER",
            ),
            _identity(
                "/dev/a",
                canonical="/dev/ttyACM0",
                major=166,
                minor=0,
                usb_parent="/sys/devices/usb1/1-2",
                serial_number="REPLACEMENT",
            ),
        ],
        ids=["symlink-retarget", "usb-parent-replacement", "usb-serial-replacement"],
    )
    def test_revalidation_fails_closed_on_identity_change(self, changed):
        original = _identity(
            "/dev/a",
            canonical="/dev/ttyACM0",
            major=166,
            minor=0,
            usb_parent="/sys/devices/usb1/1-2",
            serial_number="RADIO-001",
        )
        registry = SerialDeviceRegistry()

        with patch(
            "reticulumpi.serial_devices.resolve_serial_device",
            side_effect=[original, changed],
        ):
            lease = registry.claim("/dev/a", "meshtastic")
            with pytest.raises(SerialDeviceChangedError) as exc_info:
                lease.revalidate()

        assert exc_info.value.expected is original
        assert exc_info.value.current is changed
        lease.release()

    def test_released_lease_cannot_be_revalidated(self):
        identity = _identity("/dev/a", canonical="/dev/ttyACM0", major=166, minor=0)
        registry = SerialDeviceRegistry()

        with patch("reticulumpi.serial_devices.resolve_serial_device", return_value=identity):
            lease = registry.claim("/dev/a", "gps")
        lease.release()

        with pytest.raises(StaleSerialDeviceLeaseError, match="stale"):
            lease.revalidate()


class TestIntentionalSharing:
    def test_active_token_allows_an_exact_alias_and_no_untokened_claim(self):
        first = _identity("/dev/a", canonical="/dev/ttyACM0", major=166, minor=0)
        alias = _identity("/dev/by-id/a", canonical="/dev/ttyACM0", major=166, minor=0)
        registry = SerialDeviceRegistry()

        with patch(
            "reticulumpi.serial_devices.resolve_serial_device",
            side_effect=_mapped_resolver({"/dev/a": first, "/dev/by-id/a": alias}),
        ):
            gateway = registry.claim("/dev/a", "meshcore-gateway")
            token = gateway.issue_share_token()
            observer = registry.claim(
                "/dev/by-id/a",
                "meshcore-observer",
                share_token=token,
            )
            with pytest.raises(SerialDeviceBusyError):
                registry.claim("/dev/a", "unrelated")

        assert repr(token) == "<SerialDeviceShareToken>"
        assert registry.claim_count() == 2
        gateway.release()
        observer.release()

    def test_token_cannot_target_another_endpoint(self):
        first = _identity("/dev/a", canonical="/dev/ttyACM0", major=166, minor=0)
        second = _identity("/dev/b", canonical="/dev/ttyUSB0", major=188, minor=0)
        registry = SerialDeviceRegistry()

        with patch(
            "reticulumpi.serial_devices.resolve_serial_device",
            side_effect=_mapped_resolver({"/dev/a": first, "/dev/b": second}),
        ):
            owner = registry.claim("/dev/a", "owner")
            token = owner.issue_share_token()
            with pytest.raises(InvalidSerialShareTokenError, match="another endpoint"):
                registry.claim("/dev/b", "other", share_token=token)

        owner.release()

    def test_token_from_another_registry_is_rejected(self):
        identity = _identity("/dev/a", canonical="/dev/ttyACM0", major=166, minor=0)
        first_registry = SerialDeviceRegistry()
        second_registry = SerialDeviceRegistry()

        with patch("reticulumpi.serial_devices.resolve_serial_device", return_value=identity):
            first = first_registry.claim("/dev/a", "first")
            second = second_registry.claim("/dev/a", "second")
            token = first.issue_share_token()
            with pytest.raises(InvalidSerialShareTokenError, match="foreign"):
                second_registry.claim("/dev/a", "third", share_token=token)

        first.release()
        second.release()

    def test_token_expires_when_the_last_shared_claim_releases(self):
        identity = _identity("/dev/a", canonical="/dev/ttyACM0", major=166, minor=0)
        registry = SerialDeviceRegistry()

        with patch("reticulumpi.serial_devices.resolve_serial_device", return_value=identity):
            owner = registry.claim("/dev/a", "owner")
            token = owner.issue_share_token()
            shared = registry.claim("/dev/a", "shared", share_token=token)
            owner.release()
            shared.release()
            with pytest.raises(InvalidSerialShareTokenError, match="stale"):
                registry.claim("/dev/a", "later", share_token=token)


class TestExternalReservations:
    def test_external_reservation_blocks_local_claim_until_release(self):
        identity = _identity("/dev/rnode", canonical="/dev/ttyACM0", major=166, minor=0)
        registry = SerialDeviceRegistry()

        with patch("reticulumpi.serial_devices.resolve_serial_device", return_value=identity):
            reservation = registry.reserve_external("/dev/rnode", "rnsd:RNode")
            with pytest.raises(SerialDeviceBusyError) as exc_info:
                registry.claim("/dev/rnode", "meshtastic")
            reservation.release()
            local = registry.claim("/dev/rnode", "meshtastic")

        assert reservation.external
        assert exc_info.value.external
        assert exc_info.value.owners == ("rnsd:RNode",)
        local.release()

    def test_external_reservation_cannot_issue_share_token(self):
        identity = _identity("/dev/rnode", canonical="/dev/ttyACM0", major=166, minor=0)
        registry = SerialDeviceRegistry()

        with patch("reticulumpi.serial_devices.resolve_serial_device", return_value=identity):
            reservation = registry.reserve_external("/dev/rnode", "rnsd:RNode")
            with pytest.raises(InvalidSerialShareTokenError, match="cannot be shared"):
                reservation.issue_share_token()

        reservation.release()


class TestExternalIntentReservations:
    def test_missing_path_is_reserved_by_normalized_configured_name(self):
        registry = SerialDeviceRegistry()

        with patch(
            "reticulumpi.serial_devices.resolve_serial_device",
            side_effect=SerialDeviceIdentityError("not attached"),
        ):
            reservation = registry.reserve_external_intent("/dev/rnode", "rns:RNode")
            with pytest.raises(SerialDeviceBusyError) as exc_info:
                registry.claim("/dev/../dev/rnode", "gps")

        assert isinstance(reservation, SerialDeviceIntentLease)
        assert reservation.pending
        assert reservation.identity is None
        assert exc_info.value.identity is None
        assert exc_info.value.external
        assert exc_info.value.owners == ("rns:RNode",)
        assert registry.claim_count() == 1
        reservation.release()
        assert registry.claim_count() == 0

    def test_hotplugged_intent_blocks_a_canonical_alias(self):
        intent_identity = _identity(
            "/dev/rnode",
            canonical="/dev/ttyACM0",
            major=166,
            minor=0,
        )
        alias_identity = _identity(
            "/dev/ttyACM0",
            canonical="/dev/ttyACM0",
            major=166,
            minor=0,
        )
        attached = False

        def resolve(configured_path, *, sysfs_root):
            del sysfs_root
            path = os.fspath(configured_path)
            if path == "/dev/rnode" and not attached:
                raise SerialDeviceIdentityError("not attached")
            return {
                "/dev/rnode": intent_identity,
                "/dev/ttyACM0": alias_identity,
            }[path]

        registry = SerialDeviceRegistry()
        with patch("reticulumpi.serial_devices.resolve_serial_device", side_effect=resolve):
            reservation = registry.reserve_external_intent("/dev/rnode", "rns:RNode")
            assert reservation.pending
            attached = True
            with pytest.raises(SerialDeviceBusyError) as exc_info:
                registry.claim("/dev/ttyACM0", "meshtastic")

        assert reservation.identity is intent_identity
        assert exc_info.value.identity is alias_identity
        assert exc_info.value.external
        reservation.release()

    def test_hotplugged_intent_blocks_a_sibling_usb_interface(self):
        usb_parent = "/sys/devices/usb1/1-2"
        intent_identity = _identity(
            "/dev/rnode",
            canonical="/dev/ttyACM0",
            major=166,
            minor=0,
            usb_parent=usb_parent,
        )
        sibling_identity = _identity(
            "/dev/ttyACM1",
            canonical="/dev/ttyACM1",
            major=166,
            minor=1,
            usb_parent=usb_parent,
        )
        attached = False

        def resolve(configured_path, *, sysfs_root):
            del sysfs_root
            path = os.fspath(configured_path)
            if path == "/dev/rnode" and not attached:
                raise SerialDeviceIdentityError("not attached")
            return {
                "/dev/rnode": intent_identity,
                "/dev/ttyACM1": sibling_identity,
            }[path]

        registry = SerialDeviceRegistry()
        with patch("reticulumpi.serial_devices.resolve_serial_device", side_effect=resolve):
            reservation = registry.reserve_external_intent("/dev/rnode", "rns:RNode")
            attached = True
            with pytest.raises(SerialDeviceBusyError):
                registry.claim("/dev/ttyACM1", "gps")

        reservation.release()

    def test_resolved_intent_tracks_identity_changes(self):
        original = _identity(
            "/dev/rnode",
            canonical="/dev/ttyACM0",
            major=166,
            minor=0,
        )
        replacement = _identity(
            "/dev/rnode",
            canonical="/dev/ttyACM2",
            major=166,
            minor=2,
        )
        replacement_alias = _identity(
            "/dev/ttyACM2",
            canonical="/dev/ttyACM2",
            major=166,
            minor=2,
        )
        original_alias = _identity(
            "/dev/ttyACM0",
            canonical="/dev/ttyACM0",
            major=166,
            minor=0,
        )
        current = original

        def resolve(configured_path, *, sysfs_root):
            del sysfs_root
            path = os.fspath(configured_path)
            if path == "/dev/rnode":
                return current
            return {
                "/dev/ttyACM0": original_alias,
                "/dev/ttyACM2": replacement_alias,
            }[path]

        registry = SerialDeviceRegistry()
        with patch("reticulumpi.serial_devices.resolve_serial_device", side_effect=resolve):
            reservation = registry.reserve_external_intent("/dev/rnode", "rns:RNode")
            current = replacement
            with pytest.raises(SerialDeviceBusyError):
                registry.claim("/dev/ttyACM2", "meshcore")
            # RNS may still own the old file descriptor after its stable alias
            # retargets. Keep every seen identity blocked until intent release.
            with pytest.raises(SerialDeviceBusyError):
                registry.claim("/dev/ttyACM0", "gps")

        assert reservation.identity is replacement
        reservation.release()

    def test_admission_refresh_keeps_last_identity_when_alias_temporarily_disappears(self):
        intent_identity = _identity(
            "/dev/rnode",
            canonical="/dev/ttyACM0",
            major=166,
            minor=0,
        )
        independent_identity = _identity(
            "/dev/other",
            canonical="/dev/ttyACM1",
            major=166,
            minor=1,
        )
        intent_available = True

        def resolve(configured_path, *, sysfs_root):
            del sysfs_root
            path = os.fspath(configured_path)
            if path == "/dev/rnode":
                if not intent_available:
                    raise SerialDeviceIdentityError("temporarily unavailable")
                return intent_identity
            return independent_identity

        registry = SerialDeviceRegistry()
        with patch("reticulumpi.serial_devices.resolve_serial_device", side_effect=resolve):
            reservation = registry.reserve_external_intent("/dev/rnode", "rns:RNode")
            intent_available = False
            local = registry.claim("/dev/other", "gps")

        assert reservation.identity is intent_identity
        local.release()
        reservation.release()

    def test_release_removes_pending_path_ownership(self):
        identity = _identity(
            "/dev/rnode",
            canonical="/dev/ttyACM0",
            major=166,
            minor=0,
        )
        attached = False

        def resolve(configured_path, *, sysfs_root):
            del sysfs_root
            if not attached:
                raise SerialDeviceIdentityError("not attached")
            return identity

        registry = SerialDeviceRegistry()
        with patch("reticulumpi.serial_devices.resolve_serial_device", side_effect=resolve):
            reservation = registry.reserve_external_intent("/dev/rnode", "rns:RNode")
            reservation.release()
            attached = True
            local = registry.claim("/dev/rnode", "gps")

        assert registry.claim_count() == 1
        local.release()

    def test_external_intent_cannot_issue_share_token(self):
        registry = SerialDeviceRegistry()
        with patch(
            "reticulumpi.serial_devices.resolve_serial_device",
            side_effect=SerialDeviceIdentityError("not attached"),
        ):
            reservation = registry.reserve_external_intent("/dev/rnode", "rns:RNode")

        with pytest.raises(InvalidSerialShareTokenError, match="cannot be shared"):
            reservation.issue_share_token()
        reservation.release()


class TestSerialDeviceDefensiveBranches:
    def test_identity_endpoint_and_invalid_configured_paths(self):
        identity = _identity("/dev/a", canonical="/dev/ttyACM0", major=166, minor=7)
        assert identity.endpoint == ("/dev/ttyACM0", 166, 7)

        for invalid in ("", "bad\x00path"):
            with pytest.raises(ValueError, match="non-empty filesystem path"):
                serial_devices._normalize_configured_path(invalid)
            with pytest.raises(ValueError, match="non-empty filesystem path"):
                resolve_serial_device(invalid)

    def test_usb_identity_without_optional_serial_is_supported(self, tmp_path):
        device_stat = os.stat("/dev/null")
        major = os.major(device_stat.st_rdev)
        minor = os.minor(device_stat.st_rdev)
        sysfs_root = tmp_path / "sys"
        usb_parent = sysfs_root / "devices/usb1/1-2"
        endpoint = usb_parent / "1-2:1.0/tty/ttyUSB0"
        endpoint.mkdir(parents=True)
        (usb_parent / "idVendor").write_text("239a\n", encoding="ascii")
        (usb_parent / "idProduct").write_text("8029\n", encoding="ascii")
        char_link = sysfs_root / "dev/char" / f"{major}:{minor}"
        char_link.parent.mkdir(parents=True)
        char_link.symlink_to(endpoint)

        identity = resolve_serial_device("/dev/null", sysfs_root=sysfs_root)
        assert identity.usb is not None
        assert identity.usb.serial_number is None

    def test_empty_and_unreadable_sysfs_attributes_fail_closed(self, tmp_path, monkeypatch):
        parent = tmp_path / "usb"
        parent.mkdir()
        (parent / "idVendor").write_text("", encoding="ascii")
        with pytest.raises(SerialDeviceIdentityError, match="empty idVendor"):
            serial_devices._read_sysfs_attribute(parent, "idVendor", required=True)

        def unreadable(_self, **_kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "read_text", unreadable)
        with pytest.raises(SerialDeviceIdentityError, match="Cannot read USB"):
            serial_devices._read_sysfs_attribute(parent, "idVendor", required=True)

    def test_sysfs_inspection_and_resolution_errors_fail_closed(self, tmp_path, monkeypatch):
        attribute = tmp_path / "idVendor"

        def stat_denied(_self):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "stat", stat_denied)
        with pytest.raises(SerialDeviceIdentityError, match="Cannot inspect USB"):
            serial_devices._sysfs_attribute_exists(attribute)

        def resolve_denied(_self, *, strict):
            assert strict is True
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "resolve", resolve_denied)
        with pytest.raises(SerialDeviceIdentityError, match="Cannot resolve sysfs"):
            serial_devices._resolve_usb_parent(166, 0, sysfs_root=tmp_path)

    def test_mapped_non_usb_endpoint_without_attributes_returns_none(self, tmp_path):
        endpoint = tmp_path / "devices/platform/tty/ttyS0"
        endpoint.mkdir(parents=True)
        link = tmp_path / "dev/char/4:64"
        link.parent.mkdir(parents=True)
        link.symlink_to(endpoint)
        assert serial_devices._resolve_usb_parent(4, 64, sysfs_root=tmp_path) is None

    def test_serial_stat_permission_error_is_wrapped(self, monkeypatch):
        monkeypatch.setattr(
            serial_devices.os.path,
            "realpath",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("denied")),
        )
        with pytest.raises(SerialDeviceIdentityError, match="Cannot inspect serial device"):
            resolve_serial_device("/dev/radio")

    def test_unbacked_lease_and_intent_operations_are_stale(self):
        identity = _identity("/dev/a", canonical="/dev/ttyACM0", major=166, minor=0)
        lease = serial_devices.SerialDeviceLease(identity, "owner")
        with pytest.raises(StaleSerialDeviceLeaseError, match="not registry-backed"):
            lease.revalidate()
        with pytest.raises(StaleSerialDeviceLeaseError, match="not registry-backed"):
            lease.issue_share_token()

        intent = SerialDeviceIntentLease("/dev/a", "/dev/a", "external")
        with pytest.raises(StaleSerialDeviceLeaseError, match="not registry-backed"):
            _ = intent.identity
        with pytest.raises(StaleSerialDeviceLeaseError, match="not registry-backed"):
            intent.revalidate()

    @pytest.mark.parametrize("owner", [None, False, "", "   "])
    def test_claim_and_external_intent_reject_invalid_owners(self, owner):
        registry = SerialDeviceRegistry()
        with pytest.raises(ValueError, match="owner must be"):
            registry.claim("/dev/a", owner)
        with pytest.raises(ValueError, match="owner must be"):
            registry.reserve_external_intent("/dev/a", owner)

    def test_external_intents_reject_path_claim_and_alias_collisions(self):
        first = _identity("/dev/a", canonical="/dev/ttyACM0", major=166, minor=0)
        alias = _identity("/dev/by-id/a", canonical="/dev/ttyACM0", major=166, minor=0)
        registry = SerialDeviceRegistry()

        with patch(
            "reticulumpi.serial_devices.resolve_serial_device",
            side_effect=_mapped_resolver({"/dev/a": first, "/dev/by-id/a": alias}),
        ):
            intent = registry.reserve_external_intent("/dev/a", "rns:first")
            with pytest.raises(SerialDeviceBusyError):
                registry.reserve_external_intent("/dev/a", "rns:duplicate")
            with pytest.raises(SerialDeviceBusyError):
                registry.reserve_external_intent("/dev/by-id/a", "rns:alias")
            intent.release()

            local = registry.claim("/dev/a", "local")
            with pytest.raises(SerialDeviceBusyError):
                registry.reserve_external_intent("/dev/by-id/a", "rns:claimed")
            local.release()

    def test_claim_closes_intent_creation_race_after_resolution(self):
        identity = _identity("/dev/a", canonical="/dev/ttyACM0", major=166, minor=0)
        registry = SerialDeviceRegistry()

        def resolve_and_inject(_configured):
            registry._next_intent_id += 1
            intent_id = registry._next_intent_id
            normalized = serial_devices._normalize_configured_path("/dev/a")
            record = serial_devices._IntentRecord(
                intent_id,
                "rns:race",
                "/dev/a",
                normalized,
                identity,
                identity.claim_keys,
            )
            registry._intents[intent_id] = record
            registry._intents_by_path.setdefault(normalized, set()).add(intent_id)
            registry._index_intent_identity_locked(intent_id, identity)
            return identity

        registry._resolve = resolve_and_inject
        with pytest.raises(SerialDeviceBusyError, match="rns:race"):
            registry.claim("/dev/a", "local")

    def test_external_private_claim_cannot_consume_share_token(self):
        first = _identity("/dev/a", canonical="/dev/ttyACM0", major=166, minor=0)
        second = _identity("/dev/b", canonical="/dev/ttyACM1", major=166, minor=1)
        registry = SerialDeviceRegistry()
        with patch(
            "reticulumpi.serial_devices.resolve_serial_device",
            side_effect=_mapped_resolver({"/dev/a": first, "/dev/b": second}),
        ):
            local = registry.claim("/dev/a", "local")
            token = local.issue_share_token()
            with pytest.raises(InvalidSerialShareTokenError, match="cannot be shared"):
                registry._claim("/dev/b", "external", external=True, share_token=token)
        local.release()

    def test_invalid_share_token_type_is_rejected(self):
        identity = _identity("/dev/a", canonical="/dev/ttyACM0", major=166, minor=0)
        registry = SerialDeviceRegistry()
        with patch("reticulumpi.serial_devices.resolve_serial_device", return_value=identity):
            with pytest.raises(InvalidSerialShareTokenError, match="Invalid serial-device"):
                registry.claim("/dev/a", "owner", share_token=object())

    def test_revalidate_detects_claim_mutation_during_filesystem_io(self):
        original = _identity("/dev/a", canonical="/dev/ttyACM0", major=166, minor=0)
        changed = _identity("/dev/a", canonical="/dev/ttyACM1", major=166, minor=1)
        registry = SerialDeviceRegistry()
        with patch("reticulumpi.serial_devices.resolve_serial_device", return_value=original):
            lease = registry.claim("/dev/a", "owner")

        def mutate_during_resolve(_configured):
            record = registry._claims[lease._claim_id]
            object.__setattr__(lease, "identity", changed)
            registry._claims[lease._claim_id] = serial_devices._ClaimRecord(
                record.claim_id,
                record.group_id,
                record.owner,
                changed,
                record.external,
            )
            return changed

        registry._resolve = mutate_during_resolve
        with pytest.raises(StaleSerialDeviceLeaseError, match="changed while validating"):
            registry.revalidate(lease)
        lease.release()

    def test_intent_revalidation_attaches_and_preserves_last_identity_when_missing(self):
        identity = _identity("/dev/a", canonical="/dev/ttyACM0", major=166, minor=0)
        available = False

        def resolve(_configured, *, sysfs_root):
            del sysfs_root
            if not available:
                raise SerialDeviceIdentityError("missing")
            return identity

        registry = SerialDeviceRegistry()
        with patch("reticulumpi.serial_devices.resolve_serial_device", side_effect=resolve):
            intent = registry.reserve_external_intent("/dev/a", "rns")
            assert intent.revalidate() is None
            available = True
            assert intent.revalidate() is identity
            assert intent.revalidate() is identity
            available = False
            assert intent.revalidate() is identity
        intent.release()

    def test_foreign_and_released_intent_leases_are_stale(self):
        identity = _identity("/dev/a", canonical="/dev/ttyACM0", major=166, minor=0)
        first = SerialDeviceRegistry()
        second = SerialDeviceRegistry()
        with patch("reticulumpi.serial_devices.resolve_serial_device", return_value=identity):
            intent = first.reserve_external_intent("/dev/a", "rns")
        with pytest.raises(StaleSerialDeviceLeaseError, match="another registry"):
            second.intent_identity(intent)
        intent.release()
        with pytest.raises(StaleSerialDeviceLeaseError, match="stale"):
            first.intent_identity(intent)
        intent.release()

    def test_foreign_local_lease_is_stale(self):
        identity = _identity("/dev/a", canonical="/dev/ttyACM0", major=166, minor=0)
        first = SerialDeviceRegistry()
        second = SerialDeviceRegistry()
        with patch("reticulumpi.serial_devices.resolve_serial_device", return_value=identity):
            lease = first.claim("/dev/a", "owner")
        with pytest.raises(StaleSerialDeviceLeaseError, match="another registry"):
            second.revalidate(lease)
        lease.release()

    def test_corrupt_or_absent_indexes_are_removed_defensively(self):
        identity = _identity("/dev/a", canonical="/dev/ttyACM0", major=166, minor=0)
        registry = SerialDeviceRegistry()
        with patch("reticulumpi.serial_devices.resolve_serial_device", return_value=identity):
            lease = registry.claim("/dev/a", "owner")
        registry._claims_by_key.clear()
        lease.release()

        registry._unindex_intent_keys_locked(999, frozenset({("path", "missing")}))
