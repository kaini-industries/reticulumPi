#!/usr/bin/env python3
"""Passively validate one private, non-production lab fixture inventory.

The preflight reads host and sysfs metadata only.  It does not initialise RNS,
open the configured serial device, transmit RF, mutate hardware, or create
release evidence.  The separate lab HIL runner owns the active
authenticated radio probe after this preflight has passed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shlex
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn

if __package__:
    from tools import lab_hil
else:  # Support ``python tools/lab_preflight.py`` from the repository root.
    import lab_hil  # type: ignore[no-redef]

from reticulumpi.platform_policy import UnsupportedPlatformError, select_platform_profile
from reticulumpi.serial_devices import (
    SerialDeviceIdentity,
    resolve_serial_device,
    validate_stable_serial_path,
)


SCHEMA = 1
CLASSIFICATION = "development-lab-fixture-preflight-not-release-evidence"
SCOPE = "rns-radio-smoke"
ROLE = "rnode"
MAX_INVENTORY_BYTES = 64 * 1024
MAX_OS_RELEASE_BYTES = 64 * 1024
MAX_DEVICE_TREE_BYTES = 4 * 1024

SUPPORTED_PLATFORM_PROFILES = frozenset(
    {
        "linux-arm64-debian-bookworm-py311",
        "linux-arm64-ubuntu-noble-py312",
    }
)
LOGICAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
USB_ID = re.compile(r"^[0-9a-f]{4}$")
PI_MODEL = re.compile(r"^Raspberry Pi 5 Model B Rev [0-9]+(?:\.[0-9]+)*$")
FAILURE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SYSFS_PATH = re.compile(r"^/sys/devices/[^\x00-\x1f\x7f]+$")

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        "classification",
        "production",
        "fixture_id",
        "scope",
        "expected_platform_profile",
        "expected_pi",
        "serial_devices",
        "operator_attestations",
    }
)
_EXPECTED_PI_KEYS = frozenset({"model", "serial_sha256"})
_SERIAL_DEVICE_KEYS = frozenset({"device_id", "role", "path", "usb_identity"})
_USB_IDENTITY_KEYS = frozenset({"vendor_id", "product_id", "serial_number", "sysfs_path"})
_ATTESTATION_KEYS = frozenset(
    {
        "dedicated_nonproduction_state",
        "dedicated_nonproduction_identity",
        "dedicated_nonproduction_radios",
        "official_power_supply",
        "active_cooling",
        "durable_storage",
    }
)
_CHECK_NAMES = frozenset(
    {
        "hil_binding",
        "supported_platform",
        "expected_platform",
        "pi_identity",
        "operator_attestations",
        "serial_path",
        "serial_identity",
        "serial_inventory_unique",
        "serial_binding_stable",
    }
)
_SAFE_STATIC_SUBJECTS = frozenset(
    {"inventory", "host", "platform", "pi", "operator_attestations", "hil_config"}
)


class LabPreflightError(lab_hil.LabHilError):
    """A fixture input or passive preflight check failed closed."""

    def __init__(self, code: str, message: str, *, subject: str = "inventory") -> None:
        super().__init__(code, message)
        self.subject = subject


@dataclass(frozen=True, slots=True)
class ExpectedPi:
    model: str
    serial_sha256: str


@dataclass(frozen=True, slots=True)
class ExpectedUsbIdentity:
    vendor_id: str
    product_id: str
    serial_number: str
    sysfs_path: str


@dataclass(frozen=True, slots=True)
class SerialDevice:
    device_id: str
    role: str
    path: str
    usb_identity: ExpectedUsbIdentity


@dataclass(frozen=True, slots=True)
class LabInventory:
    """Validated private inventory for one passive RNode fixture preflight."""

    fixture_id: str
    expected_platform_profile: str
    expected_pi: ExpectedPi
    serial_devices: tuple[SerialDevice, ...]
    inventory_sha256: str

    @property
    def device(self) -> SerialDevice:
        return self.serial_devices[0]


FixtureInventory = LabInventory


@dataclass(frozen=True, slots=True)
class HostFacts:
    """Bounded host facts used to select a supported Pi production tuple."""

    system: str
    machine: str
    version_info: tuple[int, ...]
    os_release: Mapping[str, str]
    pi_model: str
    pi_serial: str


HostFactsProvider = Callable[[], HostFacts]
DeviceResolver = Callable[[str], SerialDeviceIdentity]
Clock = Callable[[], dt.datetime]


def _fail(code: str, message: str, *, subject: str = "inventory") -> None:
    raise LabPreflightError(code, message, subject=subject)


def _exact_object(value: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        _fail("inventory_schema_invalid", f"{label} fields differ")
    return value


def _text(
    value: Any,
    label: str,
    *,
    maximum: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        _fail("inventory_schema_invalid", f"{label} is invalid")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        _fail("inventory_schema_invalid", f"{label} is invalid")
    if pattern is not None and pattern.fullmatch(value) is None:
        _fail("inventory_schema_invalid", f"{label} is invalid")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("inventory_duplicate_key", "fixture inventory contains a duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    _fail("inventory_json_invalid", "fixture inventory contains a non-finite number")


def _parse_inventory_document(payload: bytes) -> dict[str, Any]:
    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except LabPreflightError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LabPreflightError(
            "inventory_json_invalid",
            "fixture inventory is not valid strict UTF-8 JSON",
        ) from exc
    if type(document) is not dict:
        _fail("inventory_schema_invalid", "fixture inventory must be a JSON object")
    return document


def parse_inventory(payload: bytes) -> LabInventory:
    """Strictly parse inventory bytes without applying private-file path policy."""

    if type(payload) is not bytes or len(payload) > MAX_INVENTORY_BYTES:
        _fail("inventory_unsafe", "fixture inventory payload is invalid or too large")
    document = _exact_object(_parse_inventory_document(payload), _TOP_LEVEL_KEYS, "inventory")
    if type(document["schema"]) is not int or document["schema"] != SCHEMA:
        _fail("inventory_schema_invalid", "fixture inventory schema is unsupported")
    if document["classification"] != CLASSIFICATION:
        _fail("inventory_classification_invalid", "fixture inventory classification is invalid")
    if type(document["production"]) is not bool or document["production"] is not False:
        _fail("inventory_production_forbidden", "fixture inventory must declare production false")
    if document["scope"] != SCOPE:
        _fail("inventory_scope_invalid", "fixture inventory scope is unsupported")

    fixture_id = _text(document["fixture_id"], "fixture_id", maximum=64, pattern=LOGICAL_ID)
    expected_platform_profile = _text(
        document["expected_platform_profile"],
        "expected_platform_profile",
        maximum=64,
    )
    if expected_platform_profile not in SUPPORTED_PLATFORM_PROFILES:
        _fail("inventory_platform_invalid", "expected platform profile is unsupported")

    pi_value = _exact_object(document["expected_pi"], _EXPECTED_PI_KEYS, "expected_pi")
    expected_pi = ExpectedPi(
        model=_text(pi_value["model"], "expected_pi.model", maximum=128, pattern=PI_MODEL),
        serial_sha256=_text(
            pi_value["serial_sha256"],
            "expected_pi.serial_sha256",
            maximum=64,
            pattern=SHA256,
        ),
    )

    devices_value = document["serial_devices"]
    if type(devices_value) is not list or len(devices_value) != 1:
        _fail("inventory_schema_invalid", "serial_devices must contain exactly one RNode")
    device_value = _exact_object(devices_value[0], _SERIAL_DEVICE_KEYS, "serial_devices entry")
    device_id = _text(
        device_value["device_id"], "serial_devices.device_id", maximum=64, pattern=LOGICAL_ID
    )
    if device_value["role"] != ROLE:
        _fail("inventory_role_invalid", "serial device role must be rnode")
    raw_device_path = _text(device_value["path"], "serial_devices.path", maximum=4096)
    try:
        device_path = validate_stable_serial_path(raw_device_path, "fixture RNode path")
    except (TypeError, ValueError):
        _fail(
            "inventory_serial_path_invalid",
            "fixture inventory RNode path is not a stable serial path",
            subject=device_id,
        )

    usb_value = _exact_object(
        device_value["usb_identity"], _USB_IDENTITY_KEYS, "serial_devices.usb_identity"
    )
    serial_number = _text(
        usb_value["serial_number"],
        "serial_devices.usb_identity.serial_number",
        maximum=128,
    )
    if serial_number != serial_number.strip():
        _fail("inventory_schema_invalid", "USB serial number must not have outer whitespace")
    sysfs_path = _text(
        usb_value["sysfs_path"],
        "serial_devices.usb_identity.sysfs_path",
        maximum=4096,
        pattern=SYSFS_PATH,
    )
    if sysfs_path != sysfs_path.strip() or sysfs_path != os.path.normpath(sysfs_path):
        _fail("inventory_schema_invalid", "USB sysfs path must be normalized")
    expected_usb = ExpectedUsbIdentity(
        vendor_id=_text(
            usb_value["vendor_id"],
            "serial_devices.usb_identity.vendor_id",
            maximum=4,
            pattern=USB_ID,
        ),
        product_id=_text(
            usb_value["product_id"],
            "serial_devices.usb_identity.product_id",
            maximum=4,
            pattern=USB_ID,
        ),
        serial_number=serial_number,
        sysfs_path=sysfs_path,
    )

    attestations = _exact_object(
        document["operator_attestations"], _ATTESTATION_KEYS, "operator_attestations"
    )
    if any(
        type(attestations[key]) is not bool or attestations[key] is not True for key in attestations
    ):
        _fail(
            "operator_attestation_missing",
            "all required operator attestations must be explicitly true",
            subject="operator_attestations",
        )

    device = SerialDevice(device_id, ROLE, device_path, expected_usb)
    return LabInventory(
        fixture_id=fixture_id,
        expected_platform_profile=expected_platform_profile,
        expected_pi=expected_pi,
        serial_devices=(device,),
        inventory_sha256=hashlib.sha256(payload).hexdigest(),
    )


def load_inventory(path: Path) -> LabInventory:
    """Load and strictly validate one private lab fixture inventory."""

    try:
        payload = lab_hil._read_private_regular(
            Path(path),
            maximum=MAX_INVENTORY_BYTES,
            unavailable_code="inventory_unavailable",
            unsafe_code="inventory_unsafe",
            label="lab fixture inventory",
        )
    except lab_hil.LabHilError as exc:
        raise LabPreflightError(
            exc.code,
            "lab fixture inventory cannot be read safely",
        ) from exc
    return parse_inventory(payload)


def _bounded_host_read(path: Path, maximum: int, label: str) -> bytes:
    try:
        resolved = path.resolve(strict=True)
        before = resolved.stat()
    except OSError as exc:
        raise RuntimeError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError(f"{label} is not a regular metadata file")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(resolved, flags)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError(f"{label} changed while opening")
        return lab_hil._read_bounded_descriptor(
            descriptor,
            opened,
            maximum=maximum,
            unsafe_code="host_metadata_unsafe",
            label=label,
        )
    except OSError as exc:
        raise RuntimeError(f"{label} cannot be read") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _parse_os_release(payload: bytes) -> dict[str, str]:
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise RuntimeError("operating-system metadata is not UTF-8") from exc
    values: dict[str, str] = {}
    for line in lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        if re.fullmatch(r"[A-Z0-9_]+", key) is None:
            continue
        if key in values:
            raise RuntimeError("operating-system metadata contains a duplicate key")
        try:
            parsed = shlex.split(raw, posix=True)
        except ValueError as exc:
            raise RuntimeError("operating-system metadata contains an invalid value") from exc
        if len(parsed) > 1:
            raise RuntimeError("operating-system metadata contains an ambiguous value")
        values[key] = parsed[0] if parsed else ""
    return values


def _device_tree_text(payload: bytes, label: str) -> str:
    if payload.endswith(b"\x00"):
        payload = payload[:-1]
    if payload.endswith(b"\n"):
        payload = payload[:-1]
    try:
        value = payload.decode("ascii")
    except UnicodeError as exc:
        raise RuntimeError(f"{label} is not ASCII") from exc
    if not value or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise RuntimeError(f"{label} is invalid")
    return value


def collect_host_facts() -> HostFacts:
    """Read the bounded host metadata needed for the passive preflight."""

    return HostFacts(
        system=platform.system(),
        machine=platform.machine(),
        version_info=tuple(sys.version_info[:3]),
        os_release=_parse_os_release(
            _bounded_host_read(Path("/etc/os-release"), MAX_OS_RELEASE_BYTES, "OS metadata")
        ),
        pi_model=_device_tree_text(
            _bounded_host_read(
                Path("/proc/device-tree/model"), MAX_DEVICE_TREE_BYTES, "Pi model metadata"
            ),
            "Pi model metadata",
        ),
        pi_serial=_device_tree_text(
            _bounded_host_read(
                Path("/proc/device-tree/serial-number"),
                MAX_DEVICE_TREE_BYTES,
                "Pi serial metadata",
            ),
            "Pi serial metadata",
        ),
    )


def _new_checks() -> dict[str, bool]:
    return {name: False for name in _CHECK_NAMES}


def _base_report(
    inventory: LabInventory,
    *,
    started_at: str,
    checks: dict[str, bool],
    platform_profile: str | None,
) -> dict[str, Any]:
    return {
        "candidate_bound": False,
        "checks": checks,
        "classification": CLASSIFICATION,
        "devices": [{"device_id": inventory.device.device_id, "role": ROLE}],
        "expected_platform_profile": inventory.expected_platform_profile,
        "finished_at": started_at,
        "fixture_id": inventory.fixture_id,
        "gate85_eligible": False,
        "hardware_mutated": False,
        "hardware_opened": False,
        "inventory_sha256": inventory.inventory_sha256,
        "observed": {"device_count": 1, "platform_profile": platform_profile},
        "operator_declared_nonproduction": True,
        "release_evidence": False,
        "rf_transmitted": False,
        "schema": SCHEMA,
        "scope": SCOPE,
        "started_at": started_at,
        "status": "failed",
    }


def _publish_failure(
    report_path: Path,
    report: dict[str, Any],
    *,
    code: str,
    subject: str,
    allowed_device_id: str,
    clock: Clock,
) -> NoReturn:
    if FAILURE_CODE.fullmatch(code) is None:
        code = "unexpected_preflight_failure"
        subject = "host"
    if subject not in _SAFE_STATIC_SUBJECTS and subject != allowed_device_id:
        code = "unexpected_preflight_failure"
        subject = "host"
    report["finished_at"] = lab_hil._normalise_time(clock())
    report["failure"] = {"code": code, "subject": subject}
    lab_hil._write_report(report_path, report)
    raise LabPreflightError(code, "lab fixture preflight failed", subject=subject)


def _usb_identity_matches(expected: ExpectedUsbIdentity, identity: SerialDeviceIdentity) -> bool:
    usb = identity.usb
    return bool(
        usb is not None
        and usb.vendor_id == expected.vendor_id
        and usb.product_id == expected.product_id
        and usb.serial_number == expected.serial_number
        and usb.sysfs_path == expected.sysfs_path
    )


def run(
    inventory: LabInventory,
    hil_config: lab_hil.LabHilConfig,
    report_path: Path,
    *,
    host_facts_provider: HostFactsProvider = collect_host_facts,
    device_resolver: DeviceResolver = resolve_serial_device,
    clock: Clock = lab_hil._utc_now,
) -> dict[str, Any]:
    """Run one passive fixture preflight and publish a redacted report."""

    report_path = Path(report_path)
    lab_hil._validate_report_path(report_path)
    started_at = lab_hil._normalise_time(clock())
    checks = _new_checks()
    checks["operator_attestations"] = True
    report = _base_report(
        inventory,
        started_at=started_at,
        checks=checks,
        platform_profile=None,
    )
    device = inventory.device

    if (
        not isinstance(hil_config, lab_hil.LabHilConfig)
        or inventory.fixture_id != hil_config.fixture_id
        or device.path != hil_config.expected_local_port
    ):
        _publish_failure(
            report_path,
            report,
            code="hil_binding_mismatch",
            subject="hil_config",
            allowed_device_id=device.device_id,
            clock=clock,
        )
    checks["hil_binding"] = True

    try:
        facts = host_facts_provider()
    except Exception:
        _publish_failure(
            report_path,
            report,
            code="host_facts_unavailable",
            subject="host",
            allowed_device_id=device.device_id,
            clock=clock,
        )
    if not isinstance(facts, HostFacts):
        _publish_failure(
            report_path,
            report,
            code="host_facts_invalid",
            subject="host",
            allowed_device_id=device.device_id,
            clock=clock,
        )

    try:
        profile = select_platform_profile(
            system=facts.system,
            machine=facts.machine,
            version_info=facts.version_info,
            os_release=facts.os_release,
        )
    except UnsupportedPlatformError:
        _publish_failure(
            report_path,
            report,
            code="unsupported_platform",
            subject="platform",
            allowed_device_id=device.device_id,
            clock=clock,
        )
    except Exception:
        _publish_failure(
            report_path,
            report,
            code="platform_facts_invalid",
            subject="platform",
            allowed_device_id=device.device_id,
            clock=clock,
        )
    checks["supported_platform"] = True
    report["observed"]["platform_profile"] = profile.profile_key
    if profile.profile_key != inventory.expected_platform_profile:
        _publish_failure(
            report_path,
            report,
            code="expected_platform_mismatch",
            subject="platform",
            allowed_device_id=device.device_id,
            clock=clock,
        )
    checks["expected_platform"] = True

    if (
        type(facts.pi_model) is not str
        or PI_MODEL.fullmatch(facts.pi_model) is None
        or facts.pi_model != inventory.expected_pi.model
        or type(facts.pi_serial) is not str
        or not facts.pi_serial
        or not facts.pi_serial.isascii()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in facts.pi_serial)
        or hashlib.sha256(facts.pi_serial.encode("ascii")).hexdigest()
        != inventory.expected_pi.serial_sha256
    ):
        _publish_failure(
            report_path,
            report,
            code="pi_identity_mismatch",
            subject="pi",
            allowed_device_id=device.device_id,
            clock=clock,
        )
    checks["pi_identity"] = True

    try:
        stable_path = validate_stable_serial_path(device.path, "fixture RNode path")
    except (TypeError, ValueError):
        _publish_failure(
            report_path,
            report,
            code="serial_path_invalid",
            subject=device.device_id,
            allowed_device_id=device.device_id,
            clock=clock,
        )
    if stable_path != device.path:
        _publish_failure(
            report_path,
            report,
            code="serial_path_invalid",
            subject=device.device_id,
            allowed_device_id=device.device_id,
            clock=clock,
        )
    checks["serial_path"] = True

    try:
        first_identity = device_resolver(device.path)
    except Exception:
        _publish_failure(
            report_path,
            report,
            code="serial_device_unavailable",
            subject=device.device_id,
            allowed_device_id=device.device_id,
            clock=clock,
        )
    if not isinstance(first_identity, SerialDeviceIdentity) or not _usb_identity_matches(
        device.usb_identity, first_identity
    ):
        _publish_failure(
            report_path,
            report,
            code="serial_identity_mismatch",
            subject=device.device_id,
            allowed_device_id=device.device_id,
            clock=clock,
        )
    checks["serial_identity"] = True

    # Schema 1 has one RNode, so there can be no duplicate identity within the
    # inventory.  This check is inventory-scoped; it does not open the device
    # or claim, lock, or inspect process ownership of the serial endpoint.
    checks["serial_inventory_unique"] = True

    try:
        second_identity = device_resolver(device.path)
    except Exception:
        _publish_failure(
            report_path,
            report,
            code="serial_revalidation_failed",
            subject=device.device_id,
            allowed_device_id=device.device_id,
            clock=clock,
        )
    if (
        not isinstance(second_identity, SerialDeviceIdentity)
        or second_identity.binding != first_identity.binding
        or not _usb_identity_matches(device.usb_identity, second_identity)
    ):
        _publish_failure(
            report_path,
            report,
            code="serial_binding_changed",
            subject=device.device_id,
            allowed_device_id=device.device_id,
            clock=clock,
        )
    checks["serial_binding_stable"] = True

    report["finished_at"] = lab_hil._normalise_time(clock())
    report["status"] = "passed"
    lab_hil._write_report(report_path, report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--hil-config", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        inventory = load_inventory(arguments.inventory)
        hil_config = lab_hil.load_config(arguments.hil_config)
        run(inventory, hil_config, arguments.report)
    except lab_hil.LabHilError as exc:
        print(f"lab fixture preflight failed ({exc.code})", file=sys.stderr)
        return 1
    except Exception:
        print("lab fixture preflight failed (unexpected_preflight_failure)", file=sys.stderr)
        return 1
    print("lab fixture preflight passed; non-release report created")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
