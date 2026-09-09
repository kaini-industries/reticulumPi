"""Opt-in integration test against a real lab Reticulum radio path."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools import lab_hil


pytestmark = pytest.mark.integration


def test_authenticated_rns_radio_round_trip() -> None:
    """Exercise the real RNS client, remote plugin, and selected radio interface."""

    config_value = os.environ.get("RETICULUMPI_HIL_CONFIG")
    report_value = os.environ.get("RETICULUMPI_HIL_REPORT")
    if config_value is None and report_value is None:
        pytest.skip("set RETICULUMPI_HIL_CONFIG and RETICULUMPI_HIL_REPORT for the lab lane")
    if not config_value or not report_value:
        pytest.fail("both RETICULUMPI_HIL_CONFIG and RETICULUMPI_HIL_REPORT are required")

    try:
        config = lab_hil.load_config(Path(config_value))
        report = lab_hil.run(config, Path(report_value))
    except lab_hil.LabHilError as exc:
        pytest.fail(f"lab HIL failed ({exc.code})", pytrace=False)

    assert report["status"] == "passed"
    assert report["release_evidence"] is False
    assert report["gate85_eligible"] is False
    assert report["operator_declared_nonproduction"] is True
