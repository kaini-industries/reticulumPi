"""Minimal systemd notification support without a runtime dependency."""

from __future__ import annotations

import logging
import os
import socket
import tempfile
from pathlib import Path


log = logging.getLogger(__name__)


def notify(message: str) -> bool:
    """Send one datagram to ``NOTIFY_SOCKET`` when launched by systemd.

    Returns ``False`` outside a notify-enabled unit or when delivery fails;
    notification must never make a foreground/development launch fail.
    """

    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
            channel.connect(address)
            channel.sendall(message.encode("utf-8"))
        return True
    except OSError as exc:
        log.warning("Could not notify systemd: %s", exc)
        return False


def ready(status: str = "ReticulumPi is ready") -> bool:
    """Declare service readiness after runtime dependencies are usable."""

    return notify(f"READY=1\nSTATUS={status}")


def stopping(status: str = "ReticulumPi is stopping") -> bool:
    """Tell systemd that the internal graceful-shutdown deadline has begun."""

    return notify(f"STOPPING=1\nSTATUS={status}")


def set_readiness_file(is_ready: bool) -> bool:
    """Create or remove the container/systemd readiness marker atomically."""

    path = Path(os.environ.get("RETICULUMPI_READY_FILE", "/run/reticulumpi/ready"))
    if not is_ready:
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError as exc:
            log.warning("Could not remove readiness marker %s: %s", path, exc)
            return False
    if not path.parent.is_dir():
        return False
    temporary: Path | None = None
    try:
        fd, raw = tempfile.mkstemp(prefix=".ready-", dir=path.parent)
        temporary = Path(raw)
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write("ready\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        return True
    except OSError as exc:
        log.warning("Could not write readiness marker %s: %s", path, exc)
        return False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
