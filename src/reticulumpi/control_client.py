"""Client for the narrowly scoped privileged control broker."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any


DEFAULT_CONTROL_SOCKET = Path("/run/reticulumpi/control.sock")
MAX_CONTROL_MESSAGE = 4096


class ControlError(RuntimeError):
    """Raised when a privileged control operation fails."""


def request_control(
    operation: str,
    arguments: list[str] | None = None,
    *,
    socket_path: str | os.PathLike[str] = DEFAULT_CONTROL_SOCKET,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Request one enumerated operation from the local root broker."""
    payload = (
        json.dumps(
            {"operation": operation, "arguments": list(arguments or [])},
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    if len(payload) > MAX_CONTROL_MESSAGE:
        raise ControlError("control request exceeds 4096 bytes")
    path = os.fspath(socket_path)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(path)
            client.sendall(payload)
            chunks = bytearray()
            while len(chunks) <= MAX_CONTROL_MESSAGE:
                block = client.recv(min(1024, MAX_CONTROL_MESSAGE + 1 - len(chunks)))
                if not block:
                    break
                chunks.extend(block)
                if b"\n" in block:
                    break
    except OSError as exc:
        raise ControlError(f"control broker unavailable: {exc}") from exc
    if len(chunks) > MAX_CONTROL_MESSAGE:
        raise ControlError("control broker response exceeds 4096 bytes")
    try:
        response = json.loads(bytes(chunks).split(b"\n", 1)[0])
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ControlError("control broker returned an invalid response") from exc
    if not isinstance(response, dict) or not response.get("ok"):
        message = (
            response.get("error", "control operation failed")
            if isinstance(response, dict)
            else "control operation failed"
        )
        raise ControlError(str(message))
    return response
