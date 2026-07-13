"""Container health probe for ReticulumPi and its shared ``rnsd`` dependency."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path


MAX_MARKER_BYTES = 64


class ContainerHealthError(RuntimeError):
    """Raised when a container dependency is not live and usable."""


def _read_owned_marker(path: Path, *, expected_mode: int | None = None) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ContainerHealthError(f"health marker is unavailable: {path}") from exc
    try:
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode):
            raise ContainerHealthError(f"health marker is not a regular file: {path}")
        if value.st_uid != os.geteuid():
            raise ContainerHealthError(f"health marker has an unexpected owner: {path}")
        if expected_mode is not None and stat.S_IMODE(value.st_mode) != expected_mode:
            raise ContainerHealthError(f"health marker has an unexpected mode: {path}")
        data = os.read(descriptor, MAX_MARKER_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > MAX_MARKER_BYTES:
        raise ContainerHealthError(f"health marker is oversized: {path}")
    return data


def _rnsd_command(pid: int, proc_root: Path) -> tuple[str, ...]:
    try:
        command = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError as exc:
        raise ContainerHealthError("rnsd process metadata is unavailable") from exc
    values = tuple(os.path.basename(os.fsdecode(value)) for value in command.split(b"\0") if value)
    if not values or "rnsd" not in values[:2]:
        raise ContainerHealthError("recorded rnsd PID belongs to another process")
    return values


def check_container_health(
    *,
    ready_file: Path | None = None,
    rnsd_pid_file: Path | None = None,
    proc_root: Path = Path("/proc"),
) -> None:
    """Require current application readiness and a responsive shared daemon."""

    ready = ready_file or Path(os.environ.get("RETICULUMPI_READY_FILE", "/run/reticulumpi/ready"))
    pid_file = rnsd_pid_file or Path(
        os.environ.get("RETICULUMPI_RNSD_PID_FILE", "/run/reticulumpi/rnsd.pid")
    )
    if _read_owned_marker(ready) != b"ready\n":
        raise ContainerHealthError("application readiness marker is stale or malformed")
    raw_pid = _read_owned_marker(pid_file, expected_mode=0o600)
    if not raw_pid.endswith(b"\n") or not raw_pid[:-1].isdigit():
        raise ContainerHealthError("rnsd PID marker is malformed")
    pid = int(raw_pid[:-1])
    if pid < 2:
        raise ContainerHealthError("rnsd PID marker is invalid")
    try:
        os.kill(pid, 0)
    except OSError as exc:
        raise ContainerHealthError("rnsd is not running") from exc
    _rnsd_command(pid, proc_root)

    try:
        result = subprocess.run(
            ["rnstatus"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContainerHealthError("rnsd readiness probe failed") from exc
    if result.returncode != 0:
        raise ContainerHealthError("rnsd shared-instance status is unavailable")


def main() -> int:
    try:
        check_container_health()
    except ContainerHealthError as exc:
        print(f"unhealthy: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - Docker invokes the module
    raise SystemExit(main())
