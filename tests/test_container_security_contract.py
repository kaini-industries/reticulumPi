"""Focused regression tests for the production container security contract."""

from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
DOCKERFILE = ROOT / "docker/Dockerfile"
RUNTIME_VERIFIER = ROOT / "tools/verify_container_runtime.sh"
VEX_PATH = "docker/security/cve-2026-15308.openvex.json"
PATCH_SCRIPT = ROOT / "docker/security/patch_cpython_html_parser.py"
PATCHED_PARSER_SHA256 = "951b46301862483dbcb3debbbd39b4cef3b85ebe488f86cc2ff667f834dfe523"


def _container_steps() -> list[dict[str, object]]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["container"]["steps"]


def test_production_container_uses_digest_pinned_python_314_trixie() -> None:
    source = DOCKERFILE.read_text(encoding="utf-8")
    base_images = re.findall(r"^ARG PYTHON_TRIXIE_IMAGE=(.+)$", source, re.MULTILINE)

    assert base_images == [
        "python:3.14-slim-trixie@sha256:"
        "b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1"
    ]
    assert "FROM ${PYTHON_TRIXIE_IMAGE} AS python-patched" in source
    assert "FROM python-patched AS test" in source
    assert "FROM python-patched AS runtime" in source
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


def test_vex_suppression_is_bound_to_the_exported_parser_postimage() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    verifier = RUNTIME_VERIFIER.read_text(encoding="utf-8")
    patch_script = PATCH_SCRIPT.read_text(encoding="utf-8")
    vex = (ROOT / VEX_PATH).read_text(encoding="utf-8")

    parser_path = "/usr/local/lib/python3.14/html/parser.py"
    assert f"patch_cpython_html_parser.py {parser_path}" in dockerfile
    assert parser_path in verifier
    assert PATCHED_PARSER_SHA256 in verifier
    assert PATCHED_PARSER_SHA256 in patch_script
    assert PATCHED_PARSER_SHA256 in vex
