"""Behavioral contracts for the systemd integration-fixture boot gate."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tools/systemd_fixture_health.sh"


def _run_health_gate(
    tmp_path: Path,
    states: list[str],
    *,
    timeout_seconds: int = 60,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state_file = tmp_path / "states"
    state_file.write_text("\n".join(states) + "\n", encoding="utf-8")
    call_log = tmp_path / "systemctl-calls"

    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$SYSTEMD_CALL_LOG"
if [[ ${1:-} == is-system-running ]]; then
    state=$(head -n 1 "$SYSTEMD_STATE_FILE")
    tail -n +2 "$SYSTEMD_STATE_FILE" > "${SYSTEMD_STATE_FILE}.next"
    mv "${SYSTEMD_STATE_FILE}.next" "$SYSTEMD_STATE_FILE"
    printf '%s\n' "$state"
    [[ $state == running ]]
    exit
fi
if [[ ${1:-} == --failed ]]; then
    echo 'fake-failed.service loaded failed failed synthetic failure'
    exit 0
fi
exit 2
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    sleep = fake_bin / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o755)

    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SYSTEMD_CALL_LOG": str(call_log),
        "SYSTEMD_STATE_FILE": str(state_file),
    }
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; wait_for_systemd_running Fixture "$2"',
            "bash",
            str(HELPER),
            str(timeout_seconds),
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )
    calls = call_log.read_text(encoding="utf-8").splitlines()
    return result, calls


def test_health_gate_waits_through_both_boot_transition_states(tmp_path: Path) -> None:
    result, calls = _run_health_gate(tmp_path, ["initializing", "starting", "running"])

    assert result.returncode == 0, result.stderr
    assert calls == ["is-system-running", "is-system-running", "is-system-running"]


def test_health_gate_accepts_an_already_running_fixture(tmp_path: Path) -> None:
    result, calls = _run_health_gate(tmp_path, ["running"])

    assert result.returncode == 0, result.stderr
    assert calls == ["is-system-running"]


@pytest.mark.parametrize("state", ["degraded", "maintenance", "offline", "unknown"])
def test_health_gate_rejects_non_running_terminal_states(tmp_path: Path, state: str) -> None:
    result, calls = _run_health_gate(tmp_path, [state])

    assert result.returncode == 1
    assert f"Fixture systemd fixture is not healthy: {state}" in result.stderr
    assert "fake-failed.service" in result.stderr
    assert calls == ["is-system-running", "--failed --no-pager"]


@pytest.mark.parametrize("state", ["initializing", "starting"])
def test_health_gate_times_out_in_a_boot_transition(tmp_path: Path, state: str) -> None:
    result, calls = _run_health_gate(tmp_path, [state], timeout_seconds=0)

    assert result.returncode == 1
    assert (
        "Fixture systemd did not finish booting within 0 seconds "
        f"(last state: {state})" in result.stderr
    )
    assert "fake-failed.service" in result.stderr
    assert calls == ["is-system-running", "--failed --no-pager"]


@pytest.mark.parametrize(
    ("verifier", "label"),
    [
        ("tools/verify_bookworm_systemd.sh", "Bookworm"),
        ("tools/verify_noble_systemd.sh", "Noble"),
    ],
)
def test_distro_verifiers_use_the_shared_fail_closed_gate(verifier: str, label: str) -> None:
    source = (ROOT / verifier).read_text(encoding="utf-8")

    assert 'source "$(dirname "${BASH_SOURCE[0]}")/systemd_fixture_health.sh"' in source
    assert f'wait_for_systemd_running "{label}" 60' in source
    assert "systemctl is-system-running" not in source
