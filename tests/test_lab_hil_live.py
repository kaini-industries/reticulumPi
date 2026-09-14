"""Opt-in integration test against a real lab Reticulum radio path."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping
from unittest.mock import Mock

import pytest

from tools import lab_hil, lab_preflight


pytestmark = pytest.mark.integration


_HIL_VARIABLES = (
    "RETICULUMPI_HIL_INVENTORY",
    "RETICULUMPI_HIL_CONFIG",
    "RETICULUMPI_HIL_PREFLIGHT_REPORT",
    "RETICULUMPI_HIL_REPORT",
)


def _run_lab_lane(paths: Mapping[str, Path]) -> tuple[dict[str, object], dict[str, object]]:
    inventory = lab_preflight.load_inventory(paths["RETICULUMPI_HIL_INVENTORY"])
    config = lab_hil.load_config(paths["RETICULUMPI_HIL_CONFIG"])
    preflight_report = lab_preflight.run(
        inventory,
        config,
        paths["RETICULUMPI_HIL_PREFLIGHT_REPORT"],
    )
    report = lab_hil.run(config, paths["RETICULUMPI_HIL_REPORT"])
    return preflight_report, report


def test_authenticated_rns_radio_round_trip() -> None:
    """Exercise the real RNS client, remote plugin, and selected radio interface."""

    variables = {name: os.environ.get(name) for name in _HIL_VARIABLES}
    if all(value is None for value in variables.values()):
        pytest.skip("set all RETICULUMPI_HIL_* variables for the lab lane")
    if any(not value for value in variables.values()):
        pytest.fail("all RETICULUMPI_HIL_* variables are required")

    paths = {name: Path(value) for name, value in variables.items() if value is not None}
    try:
        preflight_report, report = _run_lab_lane(paths)
    except lab_hil.LabHilError as exc:
        pytest.fail(f"lab HIL failed ({exc.code})", pytrace=False)

    assert preflight_report["status"] == "passed"
    assert preflight_report["hardware_opened"] is False
    assert preflight_report["rf_transmitted"] is False
    assert report["status"] == "passed"
    assert report["release_evidence"] is False
    assert report["gate85_eligible"] is False
    assert report["operator_declared_nonproduction"] is True


@pytest.mark.parametrize(
    ("failed_step", "expected_events"),
    [
        ("inventory", ["inventory"]),
        ("config", ["inventory", "config"]),
        ("preflight", ["inventory", "config", "preflight"]),
    ],
)
def test_setup_or_preflight_failure_never_starts_active_hil(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_step: str,
    expected_events: list[str],
) -> None:
    events: list[str] = []
    inventory = object()
    config = object()

    def stop_or_return(step: str, value: object) -> object:
        events.append(step)
        if failed_step == step:
            raise lab_hil.LabHilError("deterministic_test_failure", "expected test failure")
        return value

    monkeypatch.setattr(
        lab_preflight,
        "load_inventory",
        lambda _path: stop_or_return("inventory", inventory),
    )
    monkeypatch.setattr(
        lab_hil,
        "load_config",
        lambda _path: stop_or_return("config", config),
    )
    monkeypatch.setattr(
        lab_preflight,
        "run",
        lambda *_args: stop_or_return("preflight", {"status": "passed"}),
    )
    active_hil = Mock(side_effect=AssertionError("active HIL must not start"))
    monkeypatch.setattr(lab_hil, "run", active_hil)

    paths = {name: tmp_path / name for name in _HIL_VARIABLES}
    with pytest.raises(lab_hil.LabHilError, match="expected test failure"):
        _run_lab_lane(paths)

    assert events == expected_events
    active_hil.assert_not_called()


def test_preflight_completes_before_active_hil_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    inventory = object()
    config = object()
    preflight_report = {"status": "passed"}
    hil_report = {"status": "passed"}

    monkeypatch.setattr(
        lab_preflight,
        "load_inventory",
        lambda _path: events.append("inventory") or inventory,
    )
    monkeypatch.setattr(
        lab_hil,
        "load_config",
        lambda _path: events.append("config") or config,
    )
    monkeypatch.setattr(
        lab_preflight,
        "run",
        lambda *_args: events.append("preflight") or preflight_report,
    )
    monkeypatch.setattr(
        lab_hil,
        "run",
        lambda *_args: events.append("hil") or hil_report,
    )

    paths = {name: tmp_path / name for name in _HIL_VARIABLES}
    reports = _run_lab_lane(paths)

    assert events == ["inventory", "config", "preflight", "hil"]
    assert reports == (preflight_report, hil_report)
