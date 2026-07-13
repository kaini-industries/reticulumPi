"""Regression tests for container dependency liveness."""

from __future__ import annotations

import subprocess
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from reticulumpi import container_healthcheck as health


def _markers(tmp_path: Path, *, pid: int = 123) -> tuple[Path, Path, Path]:
    ready = tmp_path / "ready"
    ready.write_bytes(b"ready\n")
    pid_file = tmp_path / "rnsd.pid"
    pid_file.write_bytes(f"{pid}\n".encode("ascii"))
    pid_file.chmod(0o600)
    proc = tmp_path / "proc" / str(pid)
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"/usr/local/bin/python\0/usr/local/bin/rnsd\0")
    return ready, pid_file, tmp_path / "proc"


def test_container_health_requires_live_responsive_rnsd(monkeypatch, tmp_path):
    ready, pid_file, proc = _markers(tmp_path)
    probe: dict[str, object] = {}

    def successful_probe(command, **kwargs):
        probe["command"] = command
        probe.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(health.os, "kill", lambda pid, signal: None)
    monkeypatch.setattr(health.subprocess, "run", successful_probe)

    health.check_container_health(ready_file=ready, rnsd_pid_file=pid_file, proc_root=proc)
    assert probe == {
        "command": ["rnstatus"],
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "check": False,
        "timeout": 3,
    }


@pytest.mark.parametrize(
    ("ready_value", "pid_value", "message"),
    [
        (b"stale\n", b"123\n", "readiness marker"),
        (b"ready\n", b"123", "PID marker"),
        (b"ready\n", b"not-a-pid\n", "PID marker"),
        (b"ready\n", b"1\n", "PID marker"),
    ],
)
def test_container_health_rejects_stale_or_malformed_markers(
    monkeypatch,
    tmp_path,
    ready_value,
    pid_value,
    message,
):
    ready, pid_file, proc = _markers(tmp_path)
    ready.write_bytes(ready_value)
    pid_file.write_bytes(pid_value)
    pid_file.chmod(0o600)
    monkeypatch.setattr(health.os, "kill", lambda pid, signal: None)

    with pytest.raises(health.ContainerHealthError, match=message):
        health.check_container_health(ready_file=ready, rnsd_pid_file=pid_file, proc_root=proc)


def test_stale_ready_file_cannot_mask_dead_rnsd(monkeypatch, tmp_path):
    ready, pid_file, proc = _markers(tmp_path)

    def dead(_pid, _signal):
        raise ProcessLookupError

    monkeypatch.setattr(health.os, "kill", dead)
    with pytest.raises(health.ContainerHealthError, match="not running"):
        health.check_container_health(ready_file=ready, rnsd_pid_file=pid_file, proc_root=proc)


def test_container_health_rejects_pid_reuse_and_unresponsive_daemon(monkeypatch, tmp_path):
    ready, pid_file, proc = _markers(tmp_path)
    monkeypatch.setattr(health.os, "kill", lambda pid, signal: None)
    (proc / "123" / "cmdline").write_bytes(b"/usr/bin/not-rnsd\0")
    with pytest.raises(health.ContainerHealthError, match="another process"):
        health.check_container_health(ready_file=ready, rnsd_pid_file=pid_file, proc_root=proc)

    (proc / "123" / "cmdline").write_bytes(b"/usr/local/bin/rnsd\0")
    monkeypatch.setattr(
        health.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )
    with pytest.raises(health.ContainerHealthError, match="status is unavailable"):
        health.check_container_health(ready_file=ready, rnsd_pid_file=pid_file, proc_root=proc)

    monkeypatch.setattr(
        health.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("rnstatus", 3)),
    )
    with pytest.raises(health.ContainerHealthError, match="probe failed"):
        health.check_container_health(ready_file=ready, rnsd_pid_file=pid_file, proc_root=proc)


def test_container_health_rejects_symlink_and_mode(monkeypatch, tmp_path):
    ready, pid_file, proc = _markers(tmp_path)
    link = tmp_path / "ready-link"
    link.symlink_to(ready)
    with pytest.raises(health.ContainerHealthError, match="unavailable"):
        health.check_container_health(ready_file=link, rnsd_pid_file=pid_file, proc_root=proc)

    pid_file.chmod(0o644)
    with pytest.raises(health.ContainerHealthError, match="unexpected mode"):
        health.check_container_health(ready_file=ready, rnsd_pid_file=pid_file, proc_root=proc)


def test_container_health_main_reports_failure(monkeypatch, capsys):
    monkeypatch.setattr(
        health,
        "check_container_health",
        lambda: (_ for _ in ()).throw(health.ContainerHealthError("rnsd died")),
    )
    assert health.main() == 1
    assert "unhealthy: rnsd died" in capsys.readouterr().err


def test_marker_rejects_directory_wrong_owner_and_oversize(monkeypatch, tmp_path):
    directory = tmp_path / "marker-dir"
    directory.mkdir()
    with pytest.raises(health.ContainerHealthError, match="not a regular file"):
        health._read_owned_marker(directory)

    marker = tmp_path / "marker"
    marker.write_bytes(b"x")
    real_fstat = health.os.fstat

    def wrong_owner(descriptor):
        value = real_fstat(descriptor)
        return SimpleNamespace(st_mode=value.st_mode, st_uid=os.geteuid() + 1)

    monkeypatch.setattr(health.os, "fstat", wrong_owner)
    with pytest.raises(health.ContainerHealthError, match="unexpected owner"):
        health._read_owned_marker(marker)
    monkeypatch.setattr(health.os, "fstat", real_fstat)

    marker.write_bytes(b"x" * (health.MAX_MARKER_BYTES + 1))
    with pytest.raises(health.ContainerHealthError, match="oversized"):
        health._read_owned_marker(marker)


def test_missing_proc_metadata_and_successful_main(monkeypatch, tmp_path):
    with pytest.raises(health.ContainerHealthError, match="metadata is unavailable"):
        health._rnsd_command(404, tmp_path / "proc")
    monkeypatch.setattr(health, "check_container_health", lambda: None)
    assert health.main() == 0
