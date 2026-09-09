#!/usr/bin/env python3
"""Run one repeatable, lab-only Reticulum hardware smoke probe.

This runner deliberately produces development evidence, not release evidence.  It uses a
pre-existing client identity and a lab RNS configuration to perform an authenticated round trip
against a real ReticulumPi endpoint.  It never invokes a remote mutation endpoint and cannot emit
Gate 85 or publication authority. Establishing the path and link does transmit radio traffic.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import multiprocessing
import os
import re
import secrets
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol


CLASSIFICATION = "development-lab-rehearsal-not-release-evidence"
REPORT_SCHEMA = 1
CONFIG_SCHEMA = 1
MAX_CONFIG_BYTES = 64 * 1024
MAX_IDENTITY_BYTES = 4 * 1024
MAX_OPERATION_TIMEOUT_SECONDS = 60
ISOLATED_PROBE_TIMEOUT_MULTIPLIER = 6
ISOLATED_PROBE_TIMEOUT_OVERHEAD_SECONDS = 15
ISOLATED_PROBE_STOP_GRACE_SECONDS = 2
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RNODE_INTERFACE_TYPE = "RNodeInterface"
LOCAL_INTERFACE_TYPES = frozenset({"LocalClientInterface", "LocalServerInterface"})
_RNS_TOP_LEVEL_SECTIONS = frozenset({"reticulum", "interfaces"})
_RNS_RETICULUM_KEYS = frozenset(
    {
        "share_instance",
        "enable_transport",
        "discover_interfaces",
        "autoconnect_discovered_interfaces",
    }
)
_RNODE_REQUIRED_INTEGER_RANGES = {
    "frequency": (137_000_000, 3_000_000_000),
    "bandwidth": (7_800, 1_625_000),
    "txpower": (0, 37),
    "spreadingfactor": (5, 12),
    "codingrate": (5, 8),
}
_RNODE_ALLOWED_KEYS = frozenset(
    {
        "type",
        "interface_enabled",
        "enabled",
        "port",
        *_RNODE_REQUIRED_INTEGER_RANGES,
    }
)
DESTINATION = re.compile(r"^[0-9a-f]{32}$")
FIXTURE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,127}$")
FAILURE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PROBE_CHECK_NAMES = frozenset(
    {
        "authenticated_link",
        "client_close_invoked",
        "interface_counters_increased",
        "remote_interface_path_exclusive",
        "remote_interface_online",
        "remote_ping",
        "remote_status",
    }
)
_CONFIG_KEYS = frozenset(
    {
        "schema",
        "classification",
        "production",
        "fixture_id",
        "destination",
        "rns_config_dir",
        "client_identity",
        "expected_node",
        "expected_version",
        "expected_local_interface_name",
        "expected_local_port",
        "expected_interface_name",
        "expected_interface_type",
        "timeout_seconds",
    }
)


class LabHilError(ValueError):
    """Raised when a lab HIL input or probe fails closed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class LabHilConfig:
    """Validated inputs for one lab-only probe."""

    fixture_id: str
    destination: str
    rns_config_bytes: bytes = field(repr=False)
    identity_bytes: bytes = field(repr=False)
    expected_node: str
    expected_version: str
    expected_local_interface_name: str
    expected_local_port: str
    expected_interface_name: str
    expected_interface_type: str
    timeout_seconds: float


class _LabClient(Protocol):
    def connect(self) -> bool: ...

    def request(
        self, path: str, data: Any = None, timeout: float | None = None
    ) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


ClientFactory = Callable[[LabHilConfig], _LabClient]
Clock = Callable[[], dt.datetime]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LabHilError("config_duplicate_key", f"lab HIL config repeats key {key!r}")
        result[key] = value
    return result


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _reject_repository_path(path: Path, code: str, label: str) -> None:
    if _is_within(path, REPOSITORY_ROOT):
        raise LabHilError(code, f"{label} must be outside the repository")


def _stat_fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_bounded_descriptor(
    descriptor: int,
    before: os.stat_result,
    *,
    maximum: int,
    unsafe_code: str,
    label: str,
) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    after = os.fstat(descriptor)
    if _stat_fingerprint(after) != _stat_fingerprint(before):
        raise LabHilError(unsafe_code, f"{label} changed while it was read")
    if len(payload) > maximum:
        raise LabHilError(unsafe_code, f"{label} exceeds the size limit")
    return payload


def _read_private_regular(
    path: Path,
    *,
    maximum: int,
    unavailable_code: str,
    unsafe_code: str,
    label: str,
) -> bytes:
    if not path.is_absolute() or path != Path(os.path.normpath(str(path))):
        raise LabHilError(unsafe_code, f"{label} must use a normalized absolute path")
    _reject_repository_path(path, unsafe_code, label)
    try:
        before = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise LabHilError(unavailable_code, f"{label} is unavailable") from exc
    _reject_repository_path(resolved, unsafe_code, label)
    if resolved != path or not stat.S_ISREG(before.st_mode):
        raise LabHilError(unsafe_code, f"{label} must be a real regular file")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LabHilError(unavailable_code, f"{label} is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != (before.st_dev, before.st_ino):
            raise LabHilError(unsafe_code, f"{label} changed while it was opened")
        if not stat.S_ISREG(metadata.st_mode):
            raise LabHilError(unsafe_code, f"{label} must be a regular file")
        if metadata.st_size > maximum:
            raise LabHilError(unsafe_code, f"{label} exceeds the size limit")
        if (
            metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise LabHilError(
                unsafe_code,
                f"{label} must be caller-owned, private, and have one link",
            )
        payload = _read_bounded_descriptor(
            descriptor,
            metadata,
            maximum=maximum,
            unsafe_code=unsafe_code,
            label=label,
        )
    finally:
        os.close(descriptor)
    return payload


def _open_private_json(path: Path) -> dict[str, Any]:
    payload = _read_private_regular(
        path,
        maximum=MAX_CONFIG_BYTES,
        unavailable_code="config_unavailable",
        unsafe_code="config_unsafe",
        label="lab HIL config",
    )
    try:
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except LabHilError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LabHilError("config_json_invalid", "lab HIL config is not valid UTF-8 JSON") from exc
    if type(document) is not dict:
        raise LabHilError("config_not_object", "lab HIL config must be a JSON object")
    return document


def _required_text(
    document: dict[str, Any], key: str, *, maximum: int, pattern: re.Pattern[str] | None = None
) -> str:
    value = document.get(key)
    if type(value) is not str or not value or len(value) > maximum:
        raise LabHilError("config_field_invalid", f"lab HIL config field {key!r} is invalid")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise LabHilError("config_field_invalid", f"lab HIL config field {key!r} is invalid")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise LabHilError("config_field_invalid", f"lab HIL config field {key!r} is invalid")
    return value


def _absolute_path(document: dict[str, Any], key: str) -> Path:
    raw = _required_text(document, key, maximum=4096)
    path = Path(raw)
    if not path.is_absolute() or path != Path(os.path.normpath(raw)):
        raise LabHilError("config_path_invalid", f"lab HIL config field {key!r} must be absolute")
    return path


def _load_private_identity(path: Path) -> bytes:
    payload = _read_private_regular(
        path,
        maximum=MAX_IDENTITY_BYTES,
        unavailable_code="identity_unavailable",
        unsafe_code="identity_unsafe",
        label="lab client identity",
    )
    try:
        import RNS

        identity = RNS.Identity.from_bytes(payload)
    except Exception as exc:
        raise LabHilError(
            "identity_invalid", "lab client identity is not valid RNS key material"
        ) from exc
    if identity is None:
        raise LabHilError("identity_invalid", "lab client identity is not valid RNS key material")
    return payload


def _validate_rns_config_dir(path: Path) -> None:
    if not path.is_absolute() or path != Path(os.path.normpath(str(path))):
        raise LabHilError(
            "rns_config_unsafe", "lab RNS config directory must use a normalized absolute path"
        )
    _reject_repository_path(path, "rns_config_unsafe", "lab RNS config directory")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise LabHilError(
            "rns_config_unavailable", "lab RNS config directory is unavailable"
        ) from exc
    _reject_repository_path(resolved, "rns_config_unsafe", "lab RNS config directory")
    if resolved != path or not stat.S_ISDIR(metadata.st_mode):
        raise LabHilError("rns_config_unsafe", "lab RNS config must be a real directory")
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise LabHilError(
            "rns_config_unsafe", "lab RNS config directory must be caller-owned and private"
        )


def _read_rns_config(path: Path) -> bytes:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory = os.open(path, directory_flags)
    except OSError as exc:
        raise LabHilError(
            "rns_config_unavailable", "lab RNS config directory is unavailable"
        ) from exc
    descriptor: int | None = None
    try:
        directory_metadata = os.fstat(directory)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != os.getuid()
            or stat.S_IMODE(directory_metadata.st_mode) & 0o077
        ):
            raise LabHilError(
                "rns_config_unsafe", "lab RNS config directory must be caller-owned and private"
            )
        try:
            before = os.stat("config", dir_fd=directory, follow_symlinks=False)
        except OSError as exc:
            raise LabHilError(
                "rns_config_file_unavailable", "lab RNS config file is unavailable"
            ) from exc
        if not stat.S_ISREG(before.st_mode):
            raise LabHilError(
                "rns_config_file_unsafe", "lab RNS config file must be a real regular file"
            )
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open("config", file_flags, dir_fd=directory)
        except OSError as exc:
            raise LabHilError(
                "rns_config_file_unavailable", "lab RNS config file is unavailable"
            ) from exc
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != (before.st_dev, before.st_ino):
            raise LabHilError(
                "rns_config_file_unsafe", "lab RNS config file changed while it was opened"
            )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > MAX_CONFIG_BYTES
        ):
            raise LabHilError(
                "rns_config_file_unsafe",
                "lab RNS config file must be caller-owned, private, bounded, and have one link",
            )
        return _read_bounded_descriptor(
            descriptor,
            metadata,
            maximum=MAX_CONFIG_BYTES,
            unsafe_code="rns_config_file_unsafe",
            label="lab RNS config file",
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def _resolve_config_values(section: Any) -> None:
    for key in section.scalars:
        section[key]
    for name in section.sections:
        _resolve_config_values(section[name])


def _required_config_bool(section: Any, key: str, expected: bool) -> None:
    if key not in section.scalars:
        raise LabHilError("rns_topology_unsafe", f"RNS config must explicitly set {key}")
    try:
        value = section.as_bool(key)
    except (KeyError, TypeError, ValueError) as exc:
        raise LabHilError("rns_topology_unsafe", f"RNS config value {key} is invalid") from exc
    if value is not expected:
        raise LabHilError("rns_topology_unsafe", f"RNS config value {key} is unsafe")


def _required_config_int(section: Any, key: str, minimum: int, maximum: int) -> int:
    if key not in section.scalars:
        raise LabHilError("rns_topology_unsafe", f"RNode config must explicitly set {key}")
    try:
        value = section.as_int(key)
    except (KeyError, TypeError, ValueError) as exc:
        raise LabHilError("rns_topology_unsafe", f"RNode config value {key} is invalid") from exc
    if not minimum <= value <= maximum:
        raise LabHilError("rns_topology_unsafe", f"RNode config value {key} is out of range")
    return value


def _rns_interface_enabled(section: Any) -> bool:
    try:
        # Preserve RNS's ordered, short-circuit compatibility semantics.
        if "interface_enabled" in section and section.as_bool("interface_enabled") is True:
            return True
        return "enabled" in section and section.as_bool("enabled") is True
    except (KeyError, TypeError, ValueError) as exc:
        raise LabHilError("rns_topology_unsafe", "RNS interface enabled flag is invalid") from exc


def _validate_rns_topology(
    payload: bytes,
    *,
    expected_local_interface_name: str,
    expected_local_port: str,
    expected_remote_interface_type: str,
) -> None:
    try:
        from RNS.vendor.configobj import ConfigObj

        parsed = ConfigObj(io.BytesIO(payload), raise_errors=True)
        _resolve_config_values(parsed)
    except Exception as exc:
        raise LabHilError(
            "rns_config_parse_failed", "lab RNS config cannot be parsed safely"
        ) from exc

    if parsed.scalars or set(parsed.sections) != _RNS_TOP_LEVEL_SECTIONS:
        raise LabHilError(
            "rns_topology_unsafe",
            "RNS config may contain only [reticulum] and [interfaces] sections",
        )
    if "reticulum" not in parsed.sections:
        raise LabHilError("rns_topology_unsafe", "RNS config requires a [reticulum] section")
    reticulum = parsed["reticulum"]
    if reticulum.sections or set(reticulum.scalars) != _RNS_RETICULUM_KEYS:
        raise LabHilError(
            "rns_topology_unsafe", "RNS [reticulum] must contain only the required lab settings"
        )
    _required_config_bool(reticulum, "share_instance", False)
    _required_config_bool(reticulum, "enable_transport", False)
    _required_config_bool(reticulum, "discover_interfaces", False)
    if "autoconnect_discovered_interfaces" not in reticulum.scalars:
        raise LabHilError(
            "rns_topology_unsafe",
            "RNS config must explicitly set autoconnect_discovered_interfaces",
        )
    try:
        autoconnect = reticulum.as_int("autoconnect_discovered_interfaces")
    except (KeyError, TypeError, ValueError) as exc:
        raise LabHilError(
            "rns_topology_unsafe", "RNS autoconnect_discovered_interfaces is invalid"
        ) from exc
    if autoconnect != 0:
        raise LabHilError(
            "rns_topology_unsafe", "RNS autoconnect_discovered_interfaces must be zero"
        )
    if "network_identity" in reticulum:
        raise LabHilError(
            "rns_topology_unsafe", "RNS network_identity is forbidden in the lab lane"
        )

    if "interfaces" not in parsed.sections:
        raise LabHilError("rns_topology_unsafe", "RNS config requires an [interfaces] section")
    interfaces = parsed["interfaces"]
    if interfaces.scalars:
        raise LabHilError(
            "rns_topology_unsafe", "RNS [interfaces] may contain only top-level sections"
        )
    enabled_names = [
        name for name in interfaces.sections if _rns_interface_enabled(interfaces[name])
    ]
    if len(enabled_names) != 1:
        raise LabHilError(
            "rns_topology_unsafe", "RNS config must enable exactly one top-level interface"
        )

    selected_name = enabled_names[0]
    selected = interfaces[selected_name]
    if expected_remote_interface_type != RNODE_INTERFACE_TYPE:
        raise LabHilError(
            "rns_topology_unsafe", "lab HIL expected interface type must be RNodeInterface"
        )
    if selected_name != expected_local_interface_name:
        raise LabHilError(
            "rns_topology_unsafe", "enabled RNS interface does not match the expected local name"
        )
    if selected.sections:
        raise LabHilError(
            "rns_topology_unsafe", "the selected RNodeInterface may not contain nested sections"
        )
    if not set(selected.scalars) <= _RNODE_ALLOWED_KEYS:
        raise LabHilError(
            "rns_topology_unsafe", "the selected RNodeInterface contains unsupported settings"
        )
    if selected.get("type") != RNODE_INTERFACE_TYPE:
        raise LabHilError(
            "rns_topology_unsafe", "the enabled RNS interface must be an RNodeInterface"
        )

    from reticulumpi.serial_devices import validate_stable_serial_path

    try:
        configured_port = validate_stable_serial_path(selected.get("port"), "RNodeInterface port")
        expected_port = validate_stable_serial_path(expected_local_port, "expected local port")
    except ValueError as exc:
        raise LabHilError("rns_topology_unsafe", "RNodeInterface port is not stable") from exc
    if configured_port != expected_port:
        raise LabHilError(
            "rns_topology_unsafe", "enabled RNodeInterface port does not match the lab config"
        )
    for key, (minimum, maximum) in _RNODE_REQUIRED_INTEGER_RANGES.items():
        _required_config_int(selected, key, minimum, maximum)


def load_config(path: Path) -> LabHilConfig:
    """Load and fail closed on a non-lab or unsafe HIL configuration."""

    document = _open_private_json(path)
    if set(document) != _CONFIG_KEYS:
        missing = sorted(_CONFIG_KEYS - set(document))
        extra = sorted(set(document) - _CONFIG_KEYS)
        raise LabHilError(
            "config_keys_invalid",
            f"lab HIL config keys differ (missing={missing!r}, extra={extra!r})",
        )
    if type(document["schema"]) is not int or document["schema"] != CONFIG_SCHEMA:
        raise LabHilError("config_schema_invalid", "unsupported lab HIL config schema")
    if document["classification"] != CLASSIFICATION:
        raise LabHilError("config_classification_invalid", "config is not a lab-only rehearsal")
    if type(document["production"]) is not bool or document["production"] is not False:
        raise LabHilError(
            "config_production_forbidden",
            "operator must explicitly declare the target non-production",
        )

    timeout = document["timeout_seconds"]
    if type(timeout) not in {int, float} or not 5 <= timeout <= MAX_OPERATION_TIMEOUT_SECONDS:
        raise LabHilError(
            "config_timeout_invalid",
            f"lab HIL timeout must be between 5 and {MAX_OPERATION_TIMEOUT_SECONDS} seconds",
        )

    rns_config_dir = _absolute_path(document, "rns_config_dir")
    client_identity = _absolute_path(document, "client_identity")
    expected_local_interface_name = _required_text(
        document, "expected_local_interface_name", maximum=128
    )
    expected_local_port = _required_text(document, "expected_local_port", maximum=4096)
    expected_interface_type = _required_text(document, "expected_interface_type", maximum=64)
    _validate_rns_config_dir(rns_config_dir)
    rns_payload = _read_rns_config(rns_config_dir)
    _validate_rns_topology(
        rns_payload,
        expected_local_interface_name=expected_local_interface_name,
        expected_local_port=expected_local_port,
        expected_remote_interface_type=expected_interface_type,
    )
    identity_bytes = _load_private_identity(client_identity)
    return LabHilConfig(
        fixture_id=_required_text(document, "fixture_id", maximum=64, pattern=FIXTURE_ID),
        destination=_required_text(document, "destination", maximum=32, pattern=DESTINATION),
        rns_config_bytes=rns_payload,
        identity_bytes=identity_bytes,
        expected_node=_required_text(document, "expected_node", maximum=128),
        expected_version=_required_text(document, "expected_version", maximum=128, pattern=VERSION),
        expected_local_interface_name=expected_local_interface_name,
        expected_local_port=expected_local_port,
        expected_interface_name=_required_text(document, "expected_interface_name", maximum=256),
        expected_interface_type=expected_interface_type,
        timeout_seconds=float(timeout),
    )


def _default_client_factory(config: LabHilConfig) -> _LabClient:
    snapshot_dir = _create_rns_config_snapshot(config.rns_config_bytes)
    return _client_from_snapshot(config, snapshot_dir)


def _client_from_snapshot(config: LabHilConfig, snapshot_dir: Path) -> _LabClient:
    from reticulumpi.remote_client import RemoteClient

    return RemoteClient(
        destination_hex=config.destination,
        reticulum_config_dir=str(snapshot_dir),
        timeout=config.timeout_seconds,
        identity_bytes=config.identity_bytes,
        output=None,
    )


def _request(client: _LabClient, path: str, timeout: float) -> dict[str, Any]:
    try:
        response = client.request(path, timeout=timeout)
    except Exception as exc:
        raise LabHilError("request_failed", f"lab HIL request failed: {path}") from exc
    if type(response) is not dict or response.get("ok") is not True:
        raise LabHilError("response_invalid", f"lab HIL response failed: {path}")
    return response


def _interface_snapshot(response: dict[str, Any], config: LabHilConfig) -> tuple[int, int]:
    interfaces = response.get("data")
    if type(interfaces) is not list:
        raise LabHilError("interfaces_invalid", "remote interface response is not a list")
    online_nonlocal: list[dict[str, Any]] = []
    for interface in interfaces:
        if type(interface) is not dict:
            raise LabHilError("interfaces_invalid", "remote interface entry is not an object")
        name = interface.get("name")
        interface_type = interface.get("type")
        online = interface.get("online")
        if type(name) is not str or type(interface_type) is not str or type(online) is not bool:
            raise LabHilError("interfaces_invalid", "remote interface entry is incomplete")
        if interface_type not in LOCAL_INTERFACE_TYPES and online:
            online_nonlocal.append(interface)
    matches = [
        interface
        for interface in interfaces
        if type(interface) is dict
        and interface.get("name") == config.expected_interface_name
        and interface.get("type") == config.expected_interface_type
    ]
    if len(matches) != 1:
        raise LabHilError(
            "interface_not_unique", "expected remote radio interface did not match exactly once"
        )
    interface = matches[0]
    if interface["online"] is not True:
        raise LabHilError("interface_offline", "expected remote radio interface is not online")
    if online_nonlocal != [interface]:
        raise LabHilError(
            "interface_path_ambiguous",
            "expected RNode must be the sole online non-local remote interface",
        )
    counters: list[int] = []
    for key in ("rxb", "txb"):
        value = interface.get(key)
        if type(value) is not int or value < 0:
            raise LabHilError("interface_counters_invalid", "remote radio counters are invalid")
        counters.append(value)
    return counters[0], counters[1]


def _new_checks() -> dict[str, bool]:
    return {name: False for name in _PROBE_CHECK_NAMES}


def _failed_probe_result(code: str) -> dict[str, Any]:
    return {"checks": _new_checks(), "observed": {}, "failure_code": code}


def _execute_probe(config: LabHilConfig, client_factory: ClientFactory) -> dict[str, Any]:
    """Execute the network operations and return only the bounded report fields."""

    checks = _new_checks()
    observed: dict[str, Any] = {}
    failure_code: str | None = None
    client: _LabClient | None = None
    try:
        try:
            client = client_factory(config)
        except Exception as exc:
            raise LabHilError(
                "client_initialization_failed", "cannot initialize lab client"
            ) from exc
        if client.connect() is not True:
            raise LabHilError("authentication_failed", "authenticated RNS link was not established")

        before_response = _request(client, "/interfaces", config.timeout_seconds)
        checks["authenticated_link"] = True
        before = _interface_snapshot(before_response, config)
        checks["remote_interface_online"] = True
        checks["remote_interface_path_exclusive"] = True

        ping = _request(client, "/ping", config.timeout_seconds)
        if ping.get("node") != config.expected_node:
            raise LabHilError("node_mismatch", "remote ping node does not match the lab config")
        checks["remote_ping"] = True

        status = _request(client, "/status", config.timeout_seconds).get("data")
        if type(status) is not dict:
            raise LabHilError("status_invalid", "remote status data is not an object")
        if status.get("node_name") != config.expected_node:
            raise LabHilError("node_mismatch", "remote status node does not match the lab config")
        if status.get("version") != config.expected_version:
            raise LabHilError(
                "version_mismatch", "remote status version does not match the lab config"
            )
        if type(status.get("failed_plugins")) is not list or status["failed_plugins"]:
            raise LabHilError("plugins_unhealthy", "remote status reports failed plugins")
        checks["remote_status"] = True

        after = _interface_snapshot(_request(client, "/interfaces", config.timeout_seconds), config)
        if after[0] <= before[0] or after[1] <= before[1]:
            raise LabHilError(
                "interface_counters_static",
                "remote radio counters did not increase across the authenticated exchange",
            )
        checks["interface_counters_increased"] = True
        observed = {
            "interface": {
                "name": config.expected_interface_name,
                "rx_bytes_delta": after[0] - before[0],
                "tx_bytes_delta": after[1] - before[1],
                "type": config.expected_interface_type,
            },
            "node": config.expected_node,
            "version": config.expected_version,
        }
    except LabHilError as exc:
        failure_code = exc.code
    except Exception:
        failure_code = "unexpected_probe_failure"
    finally:
        if client is not None:
            checks["client_close_invoked"] = True
            try:
                client.close()
            except Exception:
                if failure_code is None:
                    failure_code = "client_close_failed"
    return {"checks": checks, "observed": observed, "failure_code": failure_code}


def _normalise_time(value: dt.datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LabHilError("clock_invalid", "lab HIL clock must be timezone-aware")
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _reject_report_namespace(path: Path) -> None:
    _reject_repository_path(path, "report_path_forbidden", "lab HIL report")
    if any(part == "release-verification" or part.startswith(".codex-") for part in path.parts):
        raise LabHilError(
            "report_path_forbidden",
            "lab HIL reports may not use release-verification or .codex-* evidence paths",
        )


def _validate_report_path(path: Path) -> tuple[int, int]:
    if not path.is_absolute() or path != Path(os.path.normpath(str(path))):
        raise LabHilError("report_path_invalid", "lab HIL report path must be absolute")
    _reject_report_namespace(path)
    if path.exists() or path.is_symlink():
        raise LabHilError("report_exists", "lab HIL report path already exists")
    try:
        parent = path.parent
        metadata = parent.lstat()
        resolved = parent.resolve(strict=True)
    except OSError as exc:
        raise LabHilError(
            "report_parent_unavailable", "lab HIL report parent is unavailable"
        ) from exc
    _reject_report_namespace(resolved / path.name)
    if resolved != parent or not stat.S_ISDIR(metadata.st_mode):
        raise LabHilError("report_parent_unsafe", "lab HIL report parent must be a real directory")
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise LabHilError(
            "report_parent_unsafe", "lab HIL report parent must be caller-owned and private"
        )
    return metadata.st_dev, metadata.st_ino


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while creating lab HIL report")
        view = view[written:]


_RNS_SNAPSHOT_HOLDERS: list[tempfile.TemporaryDirectory[str]] = []


def _allocate_rns_config_snapshot(
    payload: bytes,
) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
    """Create a process-owned RNS tree containing the exact validated config bytes.

    RNS accepts only a directory pathname and reopens ``config`` during construction. Keeping a
    private snapshot alive for the process prevents later changes to the operator-owned input from
    changing what RNS loads. The root is made read/execute-only after all directories RNS creates
    at startup have been prepared; writable runtime state remains below ``storage``.
    """

    holder: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory(
        prefix="reticulumpi-lab-hil-rns-"
    )
    root = Path(holder.name).resolve(strict=True)
    descriptor: int | None = None
    directory_descriptor: int | None = None
    try:
        _reject_repository_path(root, "rns_snapshot_unsafe", "lab RNS config snapshot")
        root.chmod(0o700)
        for relative in (
            "storage",
            "storage/cache",
            "storage/cache/announces",
            "storage/resources",
            "storage/identities",
            "storage/blackhole",
            "interfaces",
        ):
            directory = root / relative
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(root / "config", flags, 0o400)
        os.fchmod(descriptor, 0o400)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None

        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_descriptor = os.open(root, directory_flags)
        os.fsync(directory_descriptor)
        os.fchmod(directory_descriptor, 0o500)
        os.fsync(directory_descriptor)
    except LabHilError:
        holder.cleanup()
        raise
    except OSError as exc:
        holder.cleanup()
        raise LabHilError(
            "rns_snapshot_failed", "cannot create the validated lab RNS config snapshot"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)
    return root, holder


def _create_rns_config_snapshot(payload: bytes) -> Path:
    root, holder = _allocate_rns_config_snapshot(payload)
    _RNS_SNAPSHOT_HOLDERS.append(holder)
    return root


def _child_result_is_valid(result: Any, config: LabHilConfig) -> bool:
    if type(result) is not dict or set(result) != {"checks", "observed", "failure_code"}:
        return False
    checks = result["checks"]
    if (
        type(checks) is not dict
        or set(checks) != _PROBE_CHECK_NAMES
        or any(type(value) is not bool for value in checks.values())
    ):
        return False
    failure_code = result["failure_code"]
    if failure_code is not None and (
        type(failure_code) is not str or FAILURE_CODE.fullmatch(failure_code) is None
    ):
        return False

    observed = result["observed"]
    if observed != {}:
        if type(observed) is not dict or set(observed) != {"interface", "node", "version"}:
            return False
        interface = observed["interface"]
        if type(interface) is not dict or set(interface) != {
            "name",
            "rx_bytes_delta",
            "tx_bytes_delta",
            "type",
        }:
            return False
        if (
            observed["node"] != config.expected_node
            or observed["version"] != config.expected_version
            or interface["name"] != config.expected_interface_name
            or interface["type"] != config.expected_interface_type
            or type(interface["rx_bytes_delta"]) is not int
            or interface["rx_bytes_delta"] <= 0
            or type(interface["tx_bytes_delta"]) is not int
            or interface["tx_bytes_delta"] <= 0
        ):
            return False
    if failure_code is None and (observed == {} or not all(checks.values())):
        return False
    return True


def _isolated_probe_child(config: LabHilConfig, snapshot_dir: str, sender: Any) -> None:
    """Run RNS in a disposable process that may safely be terminated by RNS.panic()."""

    try:
        result = _execute_probe(
            config,
            lambda child_config: _client_from_snapshot(child_config, Path(snapshot_dir)),
        )
    except BaseException:
        result = _failed_probe_result("unexpected_probe_failure")
    try:
        sender.send(result)
        sender.close()
    except BaseException:
        os._exit(74)
    # RNS owns process-global state and background threads with no reliable public teardown.
    # Exiting the disposable child is the cleanup boundary; the parent owns report publication.
    os._exit(0)


def _stop_isolated_child(process: Any) -> bool:
    try:
        process.terminate()
        process.join(ISOLATED_PROBE_STOP_GRACE_SECONDS)
        if process.is_alive() and callable(getattr(process, "kill", None)):
            process.kill()
            process.join(ISOLATED_PROBE_STOP_GRACE_SECONDS)
        return not process.is_alive()
    except Exception:
        return False


def _run_isolated_probe(
    config: LabHilConfig,
    *,
    context: Any | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Run the real probe in a child and return a bounded, redacted result."""

    try:
        snapshot_dir, snapshot_holder = _allocate_rns_config_snapshot(config.rns_config_bytes)
    except LabHilError as exc:
        return _failed_probe_result(exc.code)

    receiver: Any | None = None
    sender: Any | None = None
    process: Any | None = None
    started = False
    child_stopped = True
    try:
        child_context = context or multiprocessing.get_context("spawn")
        receiver, sender = child_context.Pipe(duplex=False)
        process = child_context.Process(
            target=_isolated_probe_child,
            args=(config, str(snapshot_dir), sender),
            daemon=True,
        )
        try:
            process.start()
            started = True
        except Exception:
            return _failed_probe_result("probe_child_start_failed")
        sender.close()
        sender = None

        maximum = (
            timeout_seconds
            if timeout_seconds is not None
            else config.timeout_seconds * ISOLATED_PROBE_TIMEOUT_MULTIPLIER
            + ISOLATED_PROBE_TIMEOUT_OVERHEAD_SECONDS
        )
        process.join(maximum)
        if process.is_alive():
            child_stopped = _stop_isolated_child(process)
            return _failed_probe_result(
                "probe_child_timeout" if child_stopped else "probe_child_stop_failed"
            )
        if process.exitcode != 0:
            return _failed_probe_result("probe_child_exit")
        try:
            if not receiver.poll(0):
                return _failed_probe_result("probe_child_result_missing")
            result = receiver.recv()
        except (EOFError, OSError):
            return _failed_probe_result("probe_child_result_missing")
        if not _child_result_is_valid(result, config):
            return _failed_probe_result("probe_child_result_invalid")
        return result
    except Exception:
        if started and process is not None and process.is_alive():
            child_stopped = _stop_isolated_child(process)
        return _failed_probe_result("probe_child_control_failed")
    finally:
        for connection in (sender, receiver):
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        if started and process is not None and not process.is_alive():
            try:
                process.close()
            except Exception:
                pass
        if child_stopped:
            try:
                snapshot_holder.cleanup()
            except Exception:
                pass
        else:
            # Do not remove state from under a child whose termination could not be proven.
            _RNS_SNAPSHOT_HOLDERS.append(snapshot_holder)


def _write_report(path: Path, report: dict[str, Any]) -> None:
    # Revalidate after the network probe so a path created or redirected while
    # the probe ran cannot be treated as this run's report.
    expected_parent = _validate_report_path(path)
    payload = (
        json.dumps(
            report, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )
        + "\n"
    ).encode("ascii")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_descriptor = os.open(path.parent, directory_flags)
    except OSError as exc:
        raise LabHilError(
            "report_write_failed", "cannot open the lab HIL report directory"
        ) from exc
    descriptor: int | None = None
    temp_name: str | None = None
    try:
        parent_metadata = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or (parent_metadata.st_dev, parent_metadata.st_ino) != expected_parent
            or parent_metadata.st_uid != os.getuid()
            or stat.S_IMODE(parent_metadata.st_mode) & 0o077
        ):
            raise LabHilError(
                "report_parent_unsafe", "lab HIL report parent must be caller-owned and private"
            )
        temp_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        for _attempt in range(100):
            candidate = f".lab-hil-{secrets.token_hex(16)}.tmp"
            try:
                descriptor = os.open(candidate, temp_flags, 0o600, dir_fd=parent_descriptor)
            except FileExistsError:
                continue
            temp_name = candidate
            break
        if descriptor is None or temp_name is None:
            raise LabHilError("report_write_failed", "cannot allocate a lab HIL report temp file")
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(
                temp_name,
                path.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise LabHilError("report_exists", "lab HIL report path already exists") from exc
        try:
            os.fsync(parent_descriptor)
        except OSError as exc:
            raise LabHilError("report_write_failed", "cannot sync the lab HIL report") from exc
    except LabHilError:
        raise
    except OSError as exc:
        raise LabHilError("report_write_failed", "cannot create the lab HIL report") from exc
    finally:
        active_error = sys.exc_info()[0] is not None
        cleanup_error: OSError | None = None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                cleanup_error = exc
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
            except FileNotFoundError:
                pass
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        try:
            os.close(parent_descriptor)
        except OSError as exc:
            cleanup_error = cleanup_error or exc
        if cleanup_error is not None and not active_error:
            raise LabHilError(
                "report_write_failed", "cannot clean up the lab HIL report temp file"
            ) from cleanup_error


def run(
    config: LabHilConfig,
    report_path: Path,
    *,
    client_factory: ClientFactory = _default_client_factory,
    clock: Clock = _utc_now,
) -> dict[str, Any]:
    """Run the fixed non-mutating remote API probe and create a non-release JSON report."""

    _validate_report_path(report_path)
    started_at = _normalise_time(clock())
    if client_factory is _default_client_factory:
        result = _run_isolated_probe(config)
    else:
        result = _execute_probe(config, client_factory)
    checks = result["checks"]
    observed = result["observed"]
    failure_code = result["failure_code"]

    report: dict[str, Any] = {
        "checks": checks,
        "classification": CLASSIFICATION,
        "finished_at": _normalise_time(clock()),
        "fixture_id": config.fixture_id,
        "gate85_eligible": False,
        "observed": observed,
        "operator_declared_nonproduction": True,
        "release_evidence": False,
        "schema": REPORT_SCHEMA,
        "scope": "authenticated-rns-radio-round-trip",
        "started_at": started_at,
        "status": "failed" if failure_code else "passed",
        "target_digest": hashlib.sha256(bytes.fromhex(config.destination)).hexdigest(),
    }
    if failure_code:
        report["failure"] = {"code": failure_code}
    _write_report(report_path, report)
    if failure_code:
        raise LabHilError(
            failure_code,
            "lab HIL probe failed; a non-release report was written",
        )
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        run(config, args.report)
    except LabHilError as exc:
        print(f"lab HIL failed ({exc.code}): {exc}", file=sys.stderr)
        return 1
    print("lab HIL passed; non-release report created")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
