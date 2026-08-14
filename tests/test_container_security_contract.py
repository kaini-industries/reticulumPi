"""Focused regression tests for the production container security contract."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
DOCKERFILE = ROOT / "docker/Dockerfile"
RUNTIME_VERIFIER = ROOT / "tools/verify_container_runtime.sh"
VEX_PATH = "docker/security/python-3.14.7-grype-db-bridge.openvex.json"
NATIVE_PARSER_SHA256 = "5c5ed245889135564e75dfed9a47aeb6b4d3e5a2e9614d918a986767e3747539"
NATIVE_TARFILE_SHA256 = "3c8d585a77d7d376aea66e5e11a4d53c2605100d4c05a71b5385ed54bc526f51"


def _container_steps() -> list[dict[str, object]]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["container"]["steps"]


def test_production_container_uses_digest_pinned_python_3147_trixie() -> None:
    source = DOCKERFILE.read_text(encoding="utf-8")
    base_images = re.findall(r"^ARG PYTHON_TRIXIE_IMAGE=(.+)$", source, re.MULTILINE)

    assert base_images == [
        "python:3.14.7-slim-trixie@sha256:"
        "83c1cebb322d099ac9e3a3a532ba74b0146d702838b25e4c75c02fa81ffeb910"
    ]
    assert "FROM ${PYTHON_TRIXIE_IMAGE} AS test" in source
    assert "FROM ${PYTHON_TRIXIE_IMAGE} AS runtime" in source
    assert "python-patched" not in source
    assert "patch_cpython_" not in source
    assert "PYTHON_BOOKWORM_IMAGE" not in source
    assert "slim-bookworm" not in source


def test_container_matrix_preserves_both_architecture_results() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    container = workflow["jobs"]["container"]
    strategy = container["strategy"]
    matrix = strategy["matrix"]["include"]

    assert container["runs-on"] == "${{ matrix.runner }}"
    assert strategy["fail-fast"] is False
    assert len(matrix) == 2
    assert {
        (
            entry["platform"],
            entry["suffix"],
            entry["runner"],
            entry["pytest_workers"],
            entry["pytest_timeout"],
        )
        for entry in matrix
    } == {
        ("linux/amd64", "amd64", "ubuntu-24.04", 2, 60),
        ("linux/arm64", "arm64", "ubuntu-24.04-arm", 0, 180),
    }

    assert not any(
        str(step.get("uses", "")).startswith("docker/setup-qemu-action@")
        for step in container["steps"]
    )

    test_build = next(
        step for step in _container_steps() if step.get("name") == "Build test target"
    )
    assert test_build["with"]["build-args"].splitlines() == [
        "PYTEST_WORKERS=${{ matrix.pytest_workers }}",
        "PYTEST_TIMEOUT=${{ matrix.pytest_timeout }}",
    ]


def test_container_scans_publish_full_evidence_before_enforcing_actionable_gate() -> None:
    steps = _container_steps()
    named_steps = {step["name"]: step for step in steps if "name" in step}

    report_name = "Report all runtime image vulnerabilities"
    gate_name = "Enforce actionable high-severity vulnerability gate"
    report = named_steps[report_name]
    gate = named_steps[gate_name]

    assert str(report["uses"]).startswith("anchore/scan-action@")
    assert {
        "image": "reticulumpi:${{ matrix.suffix }}",
        "fail-build": False,
        "only-fixed": False,
        "output-format": "json",
        "output-file": "grype-all-${{ matrix.suffix }}.json",
    }.items() <= report["with"].items()
    assert "vex" not in report["with"]

    report_path = report["with"]["output-file"]
    matching_uploads = [
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
        and step.get("with", {}).get("path") == report_path
    ]
    assert len(matching_uploads) == 1
    upload = matching_uploads[0]
    assert upload["with"]["name"] == "vulnerability-report-${{ matrix.suffix }}"
    assert upload["with"]["if-no-files-found"] == "error"

    assert str(gate["uses"]).startswith("anchore/scan-action@")
    assert {
        "image": "reticulumpi:${{ matrix.suffix }}",
        "fail-build": True,
        "severity-cutoff": "high",
        "only-fixed": True,
        "output-format": "table",
        "vex": VEX_PATH,
    }.items() <= gate["with"].items()
    assert (ROOT / VEX_PATH).is_file()

    report_index = steps.index(report)
    upload_index = steps.index(upload)
    gate_index = steps.index(gate)
    assert report_index < upload_index < gate_index


def test_runtime_verifier_enforces_absence_of_python_packaging_toolchain() -> None:
    source = RUNTIME_VERIFIER.read_text(encoding="utf-8")
    runtime_probe = source.split(
        "# The runtime image is intentionally wheel-only and unprivileged.",
        maxsplit=1,
    )[1].split("\nstart_container\n", maxsplit=1)[0]

    assert re.search(r"^\s*! command -v pip\s*$", runtime_probe, re.MULTILINE)
    assert re.search(r"^\s*! command -v pip3\s*$", runtime_probe, re.MULTILINE)

    explicit_probes = all(
        re.search(
            rf"(?:importlib\.util\.)?find_spec\(\s*['\"]{module}['\"]\s*\)\s+is\s+None",
            runtime_probe,
        )
        for module in ("ensurepip", "pip", "setuptools", "wheel")
    )
    iterable_probe = bool(
        re.search(
            r"(?:importlib\.util\.)?find_spec\(\s*[a-zA-Z_]\w*\s*\)\s+is\s+None",
            runtime_probe,
        )
        and all(
            re.search(rf"['\"]{module}['\"]", runtime_probe)
            for module in ("ensurepip", "pip", "setuptools", "wheel")
        )
    )
    assert explicit_probes or iterable_probe


def test_native_fixed_stdlib_and_scanner_bridge_are_bound_to_the_exported_runtime() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    verifier = RUNTIME_VERIFIER.read_text(encoding="utf-8")
    vex = json.loads((ROOT / VEX_PATH).read_text(encoding="utf-8"))

    parser_path = "/usr/local/lib/python3.14/html/parser.py"
    tarfile_path = "/usr/local/lib/python3.14/tarfile.py"
    assert "python:3.14.7-slim-trixie@sha256:" in dockerfile
    assert "patch_cpython_" not in dockerfile
    assert parser_path in verifier
    assert tarfile_path in verifier
    assert "sys.version_info[:3] == (3, 14, 7)" in verifier
    assert NATIVE_PARSER_SHA256 in verifier
    assert NATIVE_TARFILE_SHA256 in verifier
    assert not list((ROOT / "docker/security").glob("patch_cpython_*.py"))
    assert list((ROOT / "docker/security").glob("*.openvex.json")) == [ROOT / VEX_PATH]

    statements = {statement["vulnerability"]["name"]: statement for statement in vex["statements"]}
    assert statements.keys() == {
        "CVE-2026-11940",
        "CVE-2026-11972",
        "CVE-2026-15308",
    }
    for vulnerability, statement in statements.items():
        assert statement["products"] == [{"@id": "pkg:generic/python@3.14.7"}]
        assert statement["status"] == "fixed"
        notes = statement["status_notes"]
        assert f"https://nvd.nist.gov/vuln/detail/{vulnerability}" in notes
        assert "https://www.python.org/downloads/release/python-3147/" in notes
        assert "83c1cebb322d099ac9e3a3a532ba74b0146d702838b25e4c75c02fa81ffeb910" in notes
        assert "Grype 0.110.0 database schema 6.1.9 built 2026-08-08T06:22:53Z" in notes
        assert "Remove this statement as soon as" in notes
    assert NATIVE_PARSER_SHA256 in statements["CVE-2026-15308"]["status_notes"]
    assert NATIVE_TARFILE_SHA256 in statements["CVE-2026-11940"]["status_notes"]
    assert NATIVE_TARFILE_SHA256 in statements["CVE-2026-11972"]["status_notes"]
