"""Socket-activated, root-owned broker for fixed privileged operations.

The broker accepts one request on the systemd-provided connected Unix socket,
verifies the peer UID, validates a tiny JSON schema, and executes only commands
declared in this module. It never evaluates shell text or caller environment.
"""

from __future__ import annotations

import ipaddress
import json
import pwd
import re
import socket
import struct
import subprocess
from dataclasses import dataclass
from typing import Callable

from reticulumpi.control_client import MAX_CONTROL_MESSAGE


SERVICE_USER = "reticulumpi"
CONTROL_READ_TIMEOUT = 5.0
CAPTIVE_HELPER = "/usr/libexec/reticulumpi/captive_portal_helper.sh"
CHRONY_HELPER = "/usr/libexec/reticulumpi/chrony_helper.sh"
OFFLINE_HELPER = "/usr/libexec/reticulumpi/simulate_offline.sh"
SAFE_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}
_INTERFACE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_NUMBER = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$")


class BrokerError(RuntimeError):
    """A rejected or failed control operation."""


@dataclass(frozen=True)
class Operation:
    validator: Callable[[list[str]], list[str]]
    command: Callable[[list[str]], list[str]]
    timeout: float = 30.0


def _no_arguments(arguments: list[str]) -> list[str]:
    if arguments:
        raise BrokerError("operation does not accept arguments")
    return []


def _captive_arguments(arguments: list[str]) -> list[str]:
    if not arguments:
        raise BrokerError("captive_portal requires an action")
    action = arguments[0]
    if action in {"deactivate", "cleanup", "status"}:
        if len(arguments) != 1:
            raise BrokerError(f"{action} does not accept arguments")
        return [action]
    if action != "activate" or len(arguments) != 4:
        raise BrokerError("activate requires interface, port, and gateway IPv4 address")
    interface, raw_port, raw_address = arguments[1:]
    if not _INTERFACE.fullmatch(interface):
        raise BrokerError("invalid network interface")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise BrokerError("invalid captive portal port") from exc
    if port < 1024 or port > 65535:
        raise BrokerError("captive portal port must be between 1024 and 65535")
    try:
        address = ipaddress.ip_address(raw_address)
    except ValueError as exc:
        raise BrokerError("invalid gateway address") from exc
    if address.version != 4:
        raise BrokerError("gateway address must be IPv4")
    return [action, interface, str(port), str(address)]


def _chrony_arguments(arguments: list[str]) -> list[str]:
    if not arguments:
        raise BrokerError("chrony requires an action")
    action = arguments[0]
    if action in {"online", "remove"}:
        if len(arguments) != 1:
            raise BrokerError(f"chrony {action} does not accept arguments")
        return [action]
    if action != "configure" or len(arguments) != 7:
        raise BrokerError(
            "chrony configure requires shm, precision, offset, delay, PPS device, and PPS precision"
        )
    raw_shm, precision, offset, delay, pps_device, pps_precision = arguments[1:]
    try:
        shm = int(raw_shm)
    except ValueError as exc:
        raise BrokerError("invalid chrony SHM segment") from exc
    if shm < 0 or shm > 15:
        raise BrokerError("chrony SHM segment must be between 0 and 15")
    for label, value in (
        ("precision", precision),
        ("offset", offset),
        ("delay", delay),
        ("PPS precision", pps_precision),
    ):
        if not _NUMBER.fullmatch(value):
            raise BrokerError(f"invalid chrony {label}")
    if pps_device != "-" and not re.fullmatch(r"/dev/pps[0-9]+", pps_device):
        raise BrokerError("invalid chrony PPS device")
    return [
        action,
        str(shm),
        precision,
        offset,
        delay,
        pps_device,
        pps_precision,
    ]


def _offline_arguments(arguments: list[str]) -> list[str]:
    if len(arguments) != 1 or arguments[0] not in {"on", "off", "status"}:
        raise BrokerError("offline requires exactly one of: on, off, status")
    return arguments


def _systemctl(unit: str) -> Callable[[list[str]], list[str]]:
    def command(_arguments: list[str]) -> list[str]:
        return ["/usr/bin/systemctl", "restart", unit]

    return command


def _restart_services(_arguments: list[str]) -> list[str]:
    # A small root-owned wrapper handles ordered health checks and schedules the
    # caller's service restart after the broker response is flushed.
    return ["/usr/libexec/reticulumpi/restart_services.sh"]


def _restart_reticulumpi(_arguments: list[str]) -> list[str]:
    return [
        "/usr/bin/systemd-run",
        "--quiet",
        "--collect",
        "--unit=reticulumpi-delayed-restart",
        "--on-active=2s",
        "/usr/bin/systemctl",
        "restart",
        "reticulumpi.service",
    ]


OPERATIONS: dict[str, Operation] = {
    "restart_rnsd": Operation(_no_arguments, _systemctl("rnsd.service"), 45.0),
    "restart_reticulumpi": Operation(
        _no_arguments,
        _restart_reticulumpi,
        10.0,
    ),
    "restart_services": Operation(_no_arguments, _restart_services, 60.0),
    "captive_portal": Operation(
        _captive_arguments,
        lambda arguments: [CAPTIVE_HELPER, *arguments],
        20.0,
    ),
    "chrony": Operation(
        _chrony_arguments,
        lambda arguments: [CHRONY_HELPER, *arguments],
        40.0,
    ),
    "offline": Operation(
        _offline_arguments,
        lambda arguments: [OFFLINE_HELPER, *arguments],
        45.0,
    ),
}


def _peer_uid(connection: socket.socket) -> int:
    if not hasattr(socket, "SO_PEERCRED"):
        raise BrokerError("peer credentials are unavailable on this platform")
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    return int(uid)


def _read_request(connection: socket.socket) -> dict[str, object]:
    connection.settimeout(CONTROL_READ_TIMEOUT)
    buffer = bytearray()
    while len(buffer) <= MAX_CONTROL_MESSAGE:
        try:
            block = connection.recv(min(1024, MAX_CONTROL_MESSAGE + 1 - len(buffer)))
        except TimeoutError as exc:
            raise BrokerError("request read timed out") from exc
        if not block:
            break
        buffer.extend(block)
        if b"\n" in block:
            break
    if len(buffer) > MAX_CONTROL_MESSAGE:
        raise BrokerError("request exceeds 4096 bytes")
    try:
        value = json.loads(bytes(buffer).split(b"\n", 1)[0])
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BrokerError("invalid JSON request") from exc
    if not isinstance(value, dict) or set(value) != {"operation", "arguments"}:
        raise BrokerError("request must contain only operation and arguments")
    if not isinstance(value["operation"], str) or not isinstance(value["arguments"], list):
        raise BrokerError("invalid request types")
    if not all(isinstance(item, str) and len(item) <= 256 for item in value["arguments"]):
        raise BrokerError("arguments must be short strings")
    return value


def handle_request(value: dict[str, object]) -> dict[str, object]:
    """Validate and execute one already-decoded request."""
    name = str(value["operation"])
    operation = OPERATIONS.get(name)
    if operation is None:
        raise BrokerError(f"unsupported operation: {name}")
    arguments = operation.validator(list(value["arguments"]))  # type: ignore[arg-type]
    command = operation.command(arguments)
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=operation.timeout,
        check=False,
        env=SAFE_ENV,
        cwd="/",
    )
    if result.returncode != 0:
        error = result.stderr.strip() or f"operation exited {result.returncode}"
        raise BrokerError(error[:512])
    return {"ok": True, "operation": name, "output": result.stdout.strip()[:2048]}


def serve_connection(connection: socket.socket, *, expected_uid: int | None = None) -> int:
    """Serve exactly one request and write exactly one bounded response."""
    try:
        uid = _peer_uid(connection)
        service_uid = (
            expected_uid if expected_uid is not None else pwd.getpwnam(SERVICE_USER).pw_uid
        )
        if uid != service_uid:
            raise BrokerError("peer is not the ReticulumPi service user")
        response = handle_request(_read_request(connection))
        status = 0
    except (BrokerError, KeyError, OSError, subprocess.SubprocessError) as exc:
        response = {"ok": False, "error": str(exc)[:512]}
        status = 1
    connection.sendall(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
    return status


def main() -> int:
    """Serve the systemd-provided connected socket on stdin."""
    connection = socket.fromfd(0, socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        return serve_connection(connection)
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
