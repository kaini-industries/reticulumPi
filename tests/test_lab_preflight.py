"""Deterministic contracts for the passive lab fixture preflight."""

from __future__ import annotations

import builtins
import datetime as dt
import hashlib
import json
import os
import re
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
from unittest.mock import Mock

import pytest

from reticulumpi.serial_devices import SerialDeviceIdentity, USBDeviceIdentity
from tools import lab_hil, lab_preflight


ROOT = Path(__file__).resolve().parents[1]
PI_MODEL = "Raspberry Pi 5 Model B Rev 1.0"
PI_SERIAL = "10000000abcdef12"
DEVICE_PATH = "/dev/serial/by-id/usb-ReticulumPi_RNode_TEST-001"
CANONICAL_PATH = "/dev/ttyACM7"
SYSFS_PATH = "/sys/devices/platform/axi/1000120000.pcie/usb1/1-1"
USB_SERIAL = "TEST-RNODE-001"
VENDOR_ID = "239a"
PRODUCT_ID = "8029"
FIXED_TIME = dt.datetime(2026, 9, 9, 12, 0, tzinfo=dt.timezone.utc)


def _inventory_document(
    profile: str = "linux-arm64-debian-bookworm-py311",
) -> dict[str, Any]:
    return {
        "schema": 1,
        "classification": lab_preflight.CLASSIFICATION,
        "production": False,
        "fixture_id": "bookworm-lab-a",
        "scope": lab_preflight.SCOPE,
        "expected_platform_profile": profile,
        "expected_pi": {
            "model": PI_MODEL,
            "serial_sha256": hashlib.sha256(PI_SERIAL.encode("ascii")).hexdigest(),
        },
        "serial_devices": [
            {
                "device_id": "lab-rnode-a",
                "role": "rnode",
                "path": DEVICE_PATH,
                "usb_identity": {
                    "vendor_id": VENDOR_ID,
                    "product_id": PRODUCT_ID,
                    "serial_number": USB_SERIAL,
                    "sysfs_path": SYSFS_PATH,
                },
            }
        ],
        "operator_attestations": {
            "dedicated_nonproduction_state": True,
            "dedicated_nonproduction_identity": True,
            "dedicated_nonproduction_radios": True,
            "official_power_supply": True,
            "active_cooling": True,
            "durable_storage": True,
        },
    }


def _inventory(
    profile: str = "linux-arm64-debian-bookworm-py311",
    *,
    document: dict[str, Any] | None = None,
) -> lab_preflight.LabInventory:
    value = document if document is not None else _inventory_document(profile)
    return lab_preflight.parse_inventory(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
    )


def _hil_config(
    *, fixture_id: str = "bookworm-lab-a", expected_local_port: str = DEVICE_PATH
) -> lab_hil.LabHilConfig:
    return lab_hil.LabHilConfig(
        fixture_id=fixture_id,
        destination="de" * 16,
        rns_config_bytes=b"private RNS configuration canary",
        identity_bytes=b"private identity canary",
        expected_node="remote-lab-node",
        expected_version="0.3.8.dev1+g1234567",
        expected_local_interface_name="Lab Client RNode",
        expected_local_port=expected_local_port,
        expected_interface_name="RNodeInterface[RNode LoRa]",
        expected_interface_type="RNodeInterface",
        timeout_seconds=30.0,
    )


def _host_facts(profile: str = "linux-arm64-debian-bookworm-py311") -> lab_preflight.HostFacts:
    if profile == "linux-arm64-ubuntu-noble-py312":
        version_info = (3, 12, 3)
        os_release = {
            "ID": "ubuntu",
            "VERSION_ID": "24.04",
            "VERSION_CODENAME": "noble",
        }
    else:
        version_info = (3, 11, 9)
        os_release = {
            "ID": "debian",
            "VERSION_ID": "12",
            "VERSION_CODENAME": "bookworm",
        }
    return lab_preflight.HostFacts(
        system="Linux",
        machine="aarch64",
        version_info=version_info,
        os_release=os_release,
        pi_model=PI_MODEL,
        pi_serial=PI_SERIAL,
    )


def _device_identity(
    *,
    canonical_path: str = CANONICAL_PATH,
    minor: int = 7,
    vendor_id: str = VENDOR_ID,
    sysfs_path: str = SYSFS_PATH,
) -> SerialDeviceIdentity:
    return SerialDeviceIdentity(
        DEVICE_PATH,
        canonical_path,
        166,
        minor,
        USBDeviceIdentity(sysfs_path, vendor_id, PRODUCT_ID, USB_SERIAL),
    )


def _read_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="ascii"))


def _schema_path_accepts(schema: dict[str, Any], value: str) -> bool:
    if re.search(schema["pattern"], value) is None:
        return False
    return not any(re.search(rule["pattern"], value) for rule in schema["not"]["anyOf"])


def test_load_inventory_requires_private_external_file_and_hashes_exact_bytes(
    tmp_path: Path,
) -> None:
    payload = json.dumps(_inventory_document(), indent=2).encode("utf-8") + b"\n"
    inventory_path = tmp_path / "fixture.json"
    inventory_path.write_bytes(payload)
    inventory_path.chmod(0o600)

    inventory = lab_preflight.load_inventory(inventory_path)

    assert inventory.inventory_sha256 == hashlib.sha256(payload).hexdigest()
    inventory_path.chmod(0o640)
    with pytest.raises(lab_preflight.LabPreflightError) as stopped:
        lab_preflight.load_inventory(inventory_path)
    assert stopped.value.code == "inventory_unsafe"


def test_load_inventory_refuses_a_tracked_template_as_live_private_state() -> None:
    template = ROOT / "config/lab/fixture-inventory.bookworm.example.json"

    with pytest.raises(lab_preflight.LabPreflightError) as stopped:
        lab_preflight.load_inventory(template)

    assert stopped.value.code == "inventory_unsafe"


@pytest.mark.parametrize(
    "profile",
    [
        "linux-arm64-debian-bookworm-py311",
        "linux-arm64-ubuntu-noble-py312",
    ],
)
def test_preflight_passes_both_supported_fixture_tuples(tmp_path: Path, profile: str) -> None:
    inventory = _inventory(profile)
    facts = _host_facts(profile)
    resolver = Mock(side_effect=[_device_identity(), _device_identity()])
    report_path = tmp_path / f"{profile}.json"

    report = lab_preflight.run(
        inventory,
        _hil_config(),
        report_path,
        host_facts_provider=lambda: facts,
        device_resolver=resolver,
        clock=lambda: FIXED_TIME,
    )

    assert report["status"] == "passed"
    assert report["expected_platform_profile"] == profile
    assert report["observed"] == {"device_count": 1, "platform_profile": profile}
    assert all(report["checks"].values())
    assert report["checks"]["hil_binding"] is True
    assert report["devices"] == [{"device_id": "lab-rnode-a", "role": "rnode"}]
    assert [call.args for call in resolver.call_args_list] == [
        (DEVICE_PATH,),
        (DEVICE_PATH,),
    ]


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b'{"schema":1,"schema":1}', "inventory_duplicate_key"),
        (
            b'{"expected_pi":{"model":"safe","model":"duplicate"}}',
            "inventory_duplicate_key",
        ),
        (b'{"schema":NaN}', "inventory_json_invalid"),
        (b'{"schema":Infinity}', "inventory_json_invalid"),
        (b"\xff", "inventory_json_invalid"),
    ],
)
def test_inventory_parser_rejects_duplicate_keys_nonfinite_numbers_and_invalid_utf8(
    payload: bytes, code: str
) -> None:
    with pytest.raises(lab_preflight.LabPreflightError) as stopped:
        lab_preflight.parse_inventory(payload)

    assert stopped.value.code == code


def _set_schema_bool(document: dict[str, Any]) -> None:
    document["schema"] = True


def _set_schema_future(document: dict[str, Any]) -> None:
    document["schema"] = 2


def _remove_scope(document: dict[str, Any]) -> None:
    del document["scope"]


def _add_top_level_key(document: dict[str, Any]) -> None:
    document["unexpected"] = "value"


def _add_nested_key(document: dict[str, Any]) -> None:
    document["expected_pi"]["unexpected"] = "value"


def _enable_production(document: dict[str, Any]) -> None:
    document["production"] = True


def _change_role(document: dict[str, Any]) -> None:
    document["serial_devices"][0]["role"] = "meshtastic"


def _drop_attestation(document: dict[str, Any]) -> None:
    document["operator_attestations"]["active_cooling"] = False


def _remove_usb_serial(document: dict[str, Any]) -> None:
    document["serial_devices"][0]["usb_identity"]["serial_number"] = None


def _blank_usb_serial(document: dict[str, Any]) -> None:
    document["serial_devices"][0]["usb_identity"]["serial_number"] = " "


def _pad_sysfs_path(document: dict[str, Any]) -> None:
    document["serial_devices"][0]["usb_identity"]["sysfs_path"] += " "


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (_set_schema_bool, "inventory_schema_invalid"),
        (_set_schema_future, "inventory_schema_invalid"),
        (_remove_scope, "inventory_schema_invalid"),
        (_add_top_level_key, "inventory_schema_invalid"),
        (_add_nested_key, "inventory_schema_invalid"),
        (_enable_production, "inventory_production_forbidden"),
        (_change_role, "inventory_role_invalid"),
        (_drop_attestation, "operator_attestation_missing"),
        (_remove_usb_serial, "inventory_schema_invalid"),
        (_blank_usb_serial, "inventory_schema_invalid"),
        (_pad_sysfs_path, "inventory_schema_invalid"),
    ],
)
def test_inventory_parser_enforces_exact_schema_and_keys(
    mutation: Callable[[dict[str, Any]], None], code: str
) -> None:
    document = deepcopy(_inventory_document())
    mutation(document)

    with pytest.raises(lab_preflight.LabPreflightError) as stopped:
        lab_preflight.parse_inventory(json.dumps(document).encode("utf-8"))

    assert stopped.value.code == code


@pytest.mark.parametrize(
    "hil_config",
    [
        _hil_config(fixture_id="another-fixture"),
        _hil_config(expected_local_port="/dev/serial/by-id/another-rnode"),
    ],
)
def test_hil_fixture_and_rnode_path_binding_fail_before_host_or_device_inspection(
    tmp_path: Path, hil_config: lab_hil.LabHilConfig
) -> None:
    host_provider = Mock(side_effect=AssertionError("host inspection must not run"))
    resolver = Mock(side_effect=AssertionError("device inspection must not run"))
    report_path = tmp_path / "binding-failed.json"

    with pytest.raises(lab_preflight.LabPreflightError) as stopped:
        lab_preflight.run(
            _inventory(),
            hil_config,
            report_path,
            host_facts_provider=host_provider,
            device_resolver=resolver,
            clock=lambda: FIXED_TIME,
        )

    assert stopped.value.code == "hil_binding_mismatch"
    assert host_provider.call_count == 0
    assert resolver.call_count == 0
    report = _read_report(report_path)
    assert report["failure"] == {"code": "hil_binding_mismatch", "subject": "hil_config"}
    assert report["checks"]["hil_binding"] is False
    assert report["checks"]["supported_platform"] is False


@pytest.mark.parametrize(
    ("facts", "code", "subject"),
    [
        (replace(_host_facts(), machine="x86_64"), "unsupported_platform", "platform"),
        (
            _host_facts("linux-arm64-ubuntu-noble-py312"),
            "expected_platform_mismatch",
            "platform",
        ),
        (
            replace(_host_facts(), pi_model="Raspberry Pi 5 Model B Rev 2.0"),
            "pi_identity_mismatch",
            "pi",
        ),
        (replace(_host_facts(), pi_serial="another-pi"), "pi_identity_mismatch", "pi"),
    ],
)
def test_platform_and_pi_mismatches_have_stable_redacted_codes(
    tmp_path: Path,
    facts: lab_preflight.HostFacts,
    code: str,
    subject: str,
) -> None:
    resolver = Mock(side_effect=AssertionError("device inspection must not run"))
    report_path = tmp_path / f"{code}.json"

    with pytest.raises(lab_preflight.LabPreflightError) as stopped:
        lab_preflight.run(
            _inventory(),
            _hil_config(),
            report_path,
            host_facts_provider=lambda: facts,
            device_resolver=resolver,
            clock=lambda: FIXED_TIME,
        )

    assert stopped.value.code == code
    assert resolver.call_count == 0
    report = _read_report(report_path)
    assert report["failure"] == {"code": code, "subject": subject}
    assert PI_SERIAL not in report_path.read_text(encoding="ascii")


@pytest.mark.parametrize(
    "path",
    [
        "/dev/ttyUSB0",
        "/dev/ttyACM17",
        "/dev//serial/by-id/rnode",
        "/dev/serial/by-id/../rnode",
        "relative-rnode",
        "auto",
    ],
)
def test_inventory_parser_rejects_an_unstable_serial_path(path: str) -> None:
    document = _inventory_document()
    document["serial_devices"][0]["path"] = path

    with pytest.raises(lab_preflight.LabPreflightError) as stopped:
        _inventory(document=document)

    assert stopped.value.code == "inventory_serial_path_invalid"
    assert stopped.value.subject == "lab-rnode-a"


@pytest.mark.parametrize(
    "path",
    [
        "/dev/ttyUSB0",
        "/dev/ttyACM17",
        "/dev//serial/by-id/rnode",
        "/dev/serial/by-id/../rnode",
        "/dev/serial/by-id/rnode/",
        "/dev/serial/by-id/rnode ",
    ],
)
def test_inventory_schema_rejects_runtime_invalid_serial_paths(path: str) -> None:
    schema = json.loads((ROOT / "config/lab/fixture-inventory.schema.json").read_text())
    path_schema = schema["properties"]["serial_devices"]["items"]["properties"]["path"]

    assert not _schema_path_accepts(path_schema, path)


@pytest.mark.parametrize(
    "path",
    [
        "/sys/devices//usb1/1-1",
        "/sys/devices/usb1/../usb2/2-1",
        "/sys/devices/usb1/1-1/",
        "/sys/devices/usb1/1-1 ",
    ],
)
def test_inventory_schema_rejects_runtime_invalid_sysfs_paths(path: str) -> None:
    schema = json.loads((ROOT / "config/lab/fixture-inventory.schema.json").read_text())
    path_schema = schema["properties"]["serial_devices"]["items"]["properties"]["usb_identity"][
        "properties"
    ]["sysfs_path"]

    assert not _schema_path_accepts(path_schema, path)


def test_invalid_typed_stable_path_fails_before_resolution(tmp_path: Path) -> None:
    inventory = _inventory()
    invalid_device = replace(inventory.device, path="/dev/ttyUSB0")
    inventory = replace(inventory, serial_devices=(invalid_device,))
    resolver = Mock(side_effect=AssertionError("invalid paths must not resolve"))
    report_path = tmp_path / "bad-path.json"

    with pytest.raises(lab_preflight.LabPreflightError) as stopped:
        lab_preflight.run(
            inventory,
            _hil_config(expected_local_port="/dev/ttyUSB0"),
            report_path,
            host_facts_provider=_host_facts,
            device_resolver=resolver,
            clock=lambda: FIXED_TIME,
        )

    assert stopped.value.code == "serial_path_invalid"
    assert resolver.call_count == 0
    assert _read_report(report_path)["failure"] == {
        "code": "serial_path_invalid",
        "subject": "lab-rnode-a",
    }


@pytest.mark.parametrize(
    ("resolver", "code"),
    [
        (Mock(side_effect=OSError("private path canary")), "serial_device_unavailable"),
        (Mock(return_value=_device_identity(vendor_id="ffff")), "serial_identity_mismatch"),
    ],
)
def test_device_failures_have_stable_codes(tmp_path: Path, resolver: Mock, code: str) -> None:
    report_path = tmp_path / f"{code}.json"

    with pytest.raises(lab_preflight.LabPreflightError) as stopped:
        lab_preflight.run(
            _inventory(),
            _hil_config(),
            report_path,
            host_facts_provider=_host_facts,
            device_resolver=resolver,
            clock=lambda: FIXED_TIME,
        )

    assert stopped.value.code == code
    assert resolver.call_count == 1
    assert _read_report(report_path)["failure"] == {
        "code": code,
        "subject": "lab-rnode-a",
    }
    assert "private path canary" not in report_path.read_text(encoding="ascii")


@pytest.mark.parametrize(
    ("second", "code"),
    [
        (OSError("second resolution path canary"), "serial_revalidation_failed"),
        (_device_identity(canonical_path="/dev/ttyACM8", minor=8), "serial_binding_changed"),
    ],
)
def test_second_resolution_detects_disappearance_and_identity_drift(
    tmp_path: Path, second: object, code: str
) -> None:
    resolver = Mock(side_effect=[_device_identity(), second])
    report_path = tmp_path / f"{code}.json"

    with pytest.raises(lab_preflight.LabPreflightError) as stopped:
        lab_preflight.run(
            _inventory(),
            _hil_config(),
            report_path,
            host_facts_provider=_host_facts,
            device_resolver=resolver,
            clock=lambda: FIXED_TIME,
        )

    assert stopped.value.code == code
    assert resolver.call_count == 2
    report = _read_report(report_path)
    assert report["checks"]["serial_identity"] is True
    assert report["checks"]["serial_binding_stable"] is False
    assert report["failure"] == {"code": code, "subject": "lab-rnode-a"}


def test_pass_report_is_canonical_private_and_excludes_hardware_and_hil_secrets(
    tmp_path: Path,
) -> None:
    inventory = _inventory()
    hil_config = _hil_config()
    report_path = tmp_path / "preflight.json"

    report = lab_preflight.run(
        inventory,
        hil_config,
        report_path,
        host_facts_provider=_host_facts,
        device_resolver=Mock(side_effect=[_device_identity(), _device_identity()]),
        clock=lambda: FIXED_TIME,
    )

    payload = report_path.read_text(encoding="ascii")
    assert payload == (
        json.dumps(
            json.loads(payload),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    assert report_path.stat().st_mode & 0o777 == 0o600
    assert report["inventory_sha256"] == inventory.inventory_sha256
    assert report["fixture_id"] == inventory.fixture_id
    for secret in (
        DEVICE_PATH,
        CANONICAL_PATH,
        SYSFS_PATH,
        USB_SERIAL,
        PI_SERIAL,
        PI_MODEL,
        hil_config.destination,
        hil_config.identity_bytes.hex(),
        hil_config.rns_config_bytes.decode("ascii"),
    ):
        assert secret not in payload
    assert report["release_evidence"] is False
    assert report["gate85_eligible"] is False
    assert report["candidate_bound"] is False
    assert report["hardware_opened"] is False
    assert report["rf_transmitted"] is False
    assert report["hardware_mutated"] is False

    schema = json.loads((ROOT / "config/lab/preflight-report.schema.json").read_text())
    assert set(report) == set(schema["required"])
    assert set(report["checks"]) == set(schema["properties"]["checks"]["required"])
    assert set(report["devices"][0]) == set(schema["properties"]["devices"]["items"]["required"])
    assert set(report["observed"]) == set(schema["properties"]["observed"]["required"])


def test_run_never_imports_rns_or_opens_the_serial_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_import = builtins.__import__
    real_open = os.open
    resolver = Mock(side_effect=[_device_identity(), _device_identity()])

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "RNS" or name.startswith("RNS."):
            raise AssertionError("passive preflight must not import RNS")
        return real_import(name, *args, **kwargs)

    def guarded_open(path: str | os.PathLike[str], *args: Any, **kwargs: Any) -> int:
        value = os.fspath(path)
        if value == DEVICE_PATH or value.startswith("/dev/tty"):
            raise AssertionError("passive preflight must not open serial hardware")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(os, "open", guarded_open)

    report = lab_preflight.run(
        _inventory(),
        _hil_config(),
        tmp_path / "passive.json",
        host_facts_provider=_host_facts,
        device_resolver=resolver,
        clock=lambda: FIXED_TIME,
    )

    assert report["status"] == "passed"
    assert resolver.call_count == 2


@pytest.mark.parametrize(
    ("filename", "profile"),
    [
        (
            "fixture-inventory.bookworm.example.json",
            "linux-arm64-debian-bookworm-py311",
        ),
        (
            "fixture-inventory.noble.example.json",
            "linux-arm64-ubuntu-noble-py312",
        ),
    ],
)
def test_tracked_fixture_examples_pass_the_production_parser(filename: str, profile: str) -> None:
    payload = (ROOT / "config/lab" / filename).read_bytes()

    inventory = lab_preflight.parse_inventory(payload)

    assert inventory.expected_platform_profile == profile
    assert inventory.inventory_sha256 == hashlib.sha256(payload).hexdigest()
    assert inventory.device.role == "rnode"


def test_report_schema_requires_hil_binding_and_packaging_includes_lab_contracts() -> None:
    schema = json.loads((ROOT / "config/lab/preflight-report.schema.json").read_text())
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    required_checks = set(schema["properties"]["checks"]["required"])
    assert "hil_binding" in required_checks
    assert schema["properties"]["checks"]["properties"]["hil_binding"] == {"type": "boolean"}
    failed_all_true = schema["allOf"][0]["else"]["properties"]["checks"]["not"]["properties"]
    assert set(failed_all_true) == required_checks
    assert all(constraint == {"const": True} for constraint in failed_all_true.values())
    assert schema["allOf"][0]["then"]["properties"]["failure"] == {}
    assert schema["allOf"][0]["then"]["not"]["properties"]["failure"] == {}
    assert schema["allOf"][0]["else"]["properties"]["failure"] == {}
    expected_lab_contracts = {
        "include config/lab/fixture-inventory.bookworm.example.json",
        "include config/lab/fixture-inventory.noble.example.json",
        "include config/lab/fixture-inventory.schema.json",
        "include config/lab/preflight-report.schema.json",
    }
    assert expected_lab_contracts <= set(manifest.splitlines())
    assert "recursive-include config/lab *.json" not in manifest
    assert "recursive-include tools *.py *.mjs" in manifest
