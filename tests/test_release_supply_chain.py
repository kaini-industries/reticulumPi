"""Regression tests for release versioning and reproducible dependency inputs."""

from __future__ import annotations

import asyncio
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from reticulumpi import admin_cli
from reticulumpi import __version__ as runtime_version
from reticulumpi.builtin_plugins.web_dashboard.server import _serve_static
from tools import verify_release_tag
from tools.verify_sbom import SbomValidationError, validate_sbom


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATHS = tuple(
    sorted(
        path for path in (ROOT / ".github/workflows").iterdir() if path.suffix in {".yaml", ".yml"}
    )
)
CONSTRAINT_PROFILES = (
    "production-universal-core",
    "production-universal-dashboard-nomadnet",
    "production-universal-all-features",
    "production-universal-build",
)


def test_setuptools_scm_is_the_project_version_source():
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)

    assert project["project"]["dynamic"] == ["version"]
    assert "version" not in project["project"]
    assert project["build-system"]["requires"] == [
        "setuptools==83.0.0",
        "setuptools-scm==10.2.0",
        "wheel==0.47.0",
    ]
    assert project["tool"]["setuptools_scm"] == {
        "version_file": "src/reticulumpi/_version.py",
        "fallback_version": "0+unknown",
        "parentdir_prefix_version": "reticulumpi-",
        "tag_regex": r"^v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)$",
        "version_scheme": "no-guess-dev",
    }
    assert ".git_archival.txt export-subst" in (ROOT / ".gitattributes").read_text()
    assert "exclude .git_archival.txt" in (ROOT / "MANIFEST.in").read_text()


def test_pytest_treats_warnings_as_release_failures():
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)

    assert project["tool"]["pytest"]["ini_options"]["filterwarnings"] == ["error"]


def test_cyclonedx_release_sbom_is_verified_fail_closed(tmp_path):
    sbom = tmp_path / "reticulumpi.cdx.json"
    sbom.write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "version": 1,
                "components": [
                    {
                        "type": "application",
                        "name": "reticulumpi",
                        "version": "0.3.0",
                        "components": [{"type": "library", "name": "rns"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert validate_sbom(sbom) == ("1.6", 2)

    sbom.write_text(
        '{"bomFormat":"CycloneDX","specVersion":"1.6","version":1,"components":[]}',
        encoding="utf-8",
    )
    with pytest.raises(SbomValidationError, match="no components"):
        validate_sbom(sbom)


def test_ci_pins_security_actions_and_runs_supply_chain_gates():
    workflow_path = ROOT / ".github/workflows/ci.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    workflow_actions = []
    workflow_sources = []
    for path in WORKFLOW_PATHS:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict), f"{path.name} is not a workflow mapping"
        workflow_sources.append(path.read_text(encoding="utf-8"))
        for job in document["jobs"].values():
            if "uses" in job:
                workflow_actions.append((path.name, job["uses"]))
            workflow_actions.extend(
                (path.name, step["uses"])
                for step in job.get("steps", [])
                if isinstance(step, dict) and "uses" in step
            )

    assert workflow_actions
    unpinned = [
        f"{filename}: {action}"
        for filename, action in workflow_actions
        if re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action) is None
    ]
    assert not unpinned, "workflow actions must use exact commit SHAs:\n" + "\n".join(unpinned)
    actions = [action for _filename, action in workflow_actions]
    assert any(action.startswith("pypa/gh-action-pip-audit@") for action in actions)
    assert any(action.startswith("anchore/sbom-action@") for action in actions)
    assert any(action.startswith("anchore/scan-action@") for action in actions)

    all_workflow_source = "\n".join(workflow_sources)
    assert "MINISIGN_SECRET_KEY" not in all_workflow_source
    assert "--signing-key" not in all_workflow_source
    assert "sign-request" not in all_workflow_source
    assert re.search(r"(^|[;&|]\s*)minisign\s+-S(?:\s|$)", all_workflow_source) is None

    source = workflow_path.read_text(encoding="utf-8")
    assert "gitleaks/gitleaks-action@" not in source
    assert "GITLEAKS_VERSION: 8.30.1" in source
    assert (
        "GITLEAKS_SHA256: 551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
        in source
    )
    assert "systemd-analyze verify" in source
    assert "sudo visudo --check" not in source
    assert "node --check" in source
    assert "tools/verify_sbom.py" in source

    privileged_step = next(
        step
        for step in workflow["jobs"]["static-analysis"]["steps"]
        if step.get("name") == "Validate privileged policy and systemd units"
    )
    assert "test ! -d config/sudoers.d" in privileged_step["run"]
    assert "find config/sudoers.d -type f -print -quit" in privileged_step["run"]

    package_steps = workflow["jobs"]["package"]["steps"]
    prepare_sbom = next(
        step
        for step in package_steps
        if step.get("name") == "Prepare the exact wheel for SBOM generation"
    )
    assert "wheels=(dist/reticulumpi-*.whl)" in prepare_sbom["run"]
    assert 'python -m zipfile -e "${wheels[0]}" "$sbom_root"' in prepare_sbom["run"]
    generate_sbom = next(
        step for step in package_steps if step.get("name") == "Generate exact-wheel CycloneDX SBOM"
    )
    assert generate_sbom["with"]["path"] == "${{ runner.temp }}/reticulumpi-wheel-root"


def test_dependency_audit_uses_verified_disable_pip_action_interface():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    audit_steps = [
        step
        for step in workflow["jobs"]["dependency-audit"]["steps"]
        if str(step.get("uses", "")).startswith("pypa/gh-action-pip-audit@")
    ]

    assert len(audit_steps) == 1
    audit_step = audit_steps[0]
    assert audit_step["uses"] == (
        "pypa/gh-action-pip-audit@fb241f581674a1bb995061d62504857a9ea4b69e"
    )
    assert audit_step["with"]["disable-pip"] is True
    assert audit_step["with"]["require-hashes"] is True
    assert "internal-be-careful-extra-flags" not in audit_step["with"]


def test_workflow_inline_shell_steps_have_valid_bash_syntax():
    failures = []

    for path in WORKFLOW_PATHS:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in workflow["jobs"].items():
            for step in job.get("steps", []):
                script = step.get("run")
                if not isinstance(script, str):
                    continue
                result = subprocess.run(
                    ["bash", "-n"],
                    input=script,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if result.returncode:
                    step_name = step.get("name", "<unnamed>")
                    failures.append(f"{path.name}:{job_name}/{step_name}: {result.stderr.strip()}")

    assert not failures, "\n".join(failures)


def test_container_job_loads_then_validates_and_exports_one_runtime_image():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["container"]["steps"]
    named_steps = {step["name"]: step for step in steps if "name" in step}
    ordered_names = [step.get("name") for step in steps]

    build_name = "Build runtime target from the same wheel"
    report_name = "Report all runtime image vulnerabilities"
    report_upload_name = "Upload complete runtime vulnerability report"
    gate_name = "Enforce actionable high-severity vulnerability gate"
    verify_name = "Verify minimal runtime, readiness, shutdown, and durable recreation"
    export_name = "Export the exact validated runtime image"
    assert [
        ordered_names.index(name)
        for name in (
            build_name,
            report_name,
            report_upload_name,
            gate_name,
            verify_name,
            export_name,
        )
    ] == sorted(
        ordered_names.index(name)
        for name in (
            build_name,
            report_name,
            report_upload_name,
            gate_name,
            verify_name,
            export_name,
        )
    )

    build = named_steps[build_name]
    assert build["uses"].startswith("docker/build-push-action@")
    assert build["with"] == {
        "context": ".",
        "file": "docker/Dockerfile",
        "target": "runtime",
        "platforms": "${{ matrix.platform }}",
        "push": False,
        "load": True,
        "provenance": False,
        "sbom": False,
        "tags": "reticulumpi:${{ matrix.suffix }}",
    }

    report = named_steps[report_name]
    assert report["with"] == {
        "image": "reticulumpi:${{ matrix.suffix }}",
        "fail-build": False,
        "severity-cutoff": "high",
        "only-fixed": False,
        "output-format": "json",
        "output-file": "grype-all-${{ matrix.suffix }}.json",
        "grype-version": "v0.110.0",
        "cache-db": True,
    }
    report_upload = named_steps[report_upload_name]
    assert report_upload["with"]["path"] == "grype-all-${{ matrix.suffix }}.json"
    assert report_upload["with"]["if-no-files-found"] == "error"

    gate = named_steps[gate_name]
    assert gate["with"] == {
        "image": "reticulumpi:${{ matrix.suffix }}",
        "fail-build": True,
        "severity-cutoff": "high",
        "only-fixed": True,
        "output-format": "table",
        "grype-version": "v0.110.0",
        "vex": "docker/security/cve-2026-15308.openvex.json",
        "cache-db": True,
    }
    assert "reticulumpi:${{ matrix.suffix }}" in named_steps[verify_name]["run"]
    export = named_steps[export_name]["run"]
    assert export.startswith("set -euo pipefail\n")
    assert 'archive="reticulumpi-${{ matrix.suffix }}.tar.gz"' in export
    assert 'docker save "reticulumpi:${{ matrix.suffix }}"' in export
    assert 'sha256sum "$archive" > "${archive}.sha256"' in export


@pytest.mark.parametrize("profile", CONSTRAINT_PROFILES)
def test_production_universal_constraint_profiles_are_fully_pinned_and_hashed(profile):
    source = (ROOT / "constraints" / f"{profile}.in").read_text(encoding="utf-8")
    lock = (ROOT / "constraints" / f"{profile}.txt").read_text(encoding="utf-8")
    normalized = lock.replace("\\\n", " ")
    requirements = [line for line in normalized.splitlines() if line and line[0].isalnum()]

    assert requirements
    assert all("==" in requirement for requirement in requirements)
    assert all("--hash=sha256:" in requirement for requirement in requirements)
    assert "git+" not in lock
    assert " @ http" not in lock
    assert "bookworm-py311-" not in source
    assert "bookworm-py311-" not in lock
    assert f"constraints/{profile}.in" in lock.splitlines()[1]

    direct_names = {
        re.sub(r"[-_.]+", "-", re.split(r"[<>=!~]", line, maxsplit=1)[0].lower())
        for line in source.splitlines()
        if line and line[0].isalnum()
    }
    canonical_lock = re.sub(r"[-_.]+", "-", lock.lower())
    for name in direct_names:
        assert f"{name}==" in canonical_lock


def test_docker_build_consumes_one_prebuilt_wheel_with_hashed_runtime_dependencies():
    dockerfile = (ROOT / "docker/Dockerfile").read_text(encoding="utf-8")

    assert "COPY dist/reticulumpi-*.whl /wheels/" in dockerfile
    assert "python -m build" not in dockerfile
    assert dockerfile.count("RUN set -eu;") == 2
    assert dockerfile.count("--require-hashes") == 2
    assert 'python -m pip install --no-deps "${1}"' in dockerfile
    assert dockerfile.count("python -m pip check") == 2
    assert "docker/config/" in (ROOT / ".dockerignore").read_text()


def test_noble_systemd_fixture_uses_the_reviewed_official_ubuntu_digest():
    dockerfile = (ROOT / "docker/noble-systemd-ci.Dockerfile").read_text(encoding="utf-8")

    assert re.search(
        r"^ARG UBUNTU_NOBLE_IMAGE=ubuntu:24\.04@sha256:[0-9a-f]{64}$",
        dockerfile,
        re.MULTILINE,
    )
    assert (
        "ubuntu:24.04@sha256:"
        "4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90" in dockerfile
    )
    assert "FROM ${UBUNTU_NOBLE_IMAGE}" in dockerfile
    assert "build-essential" in dockerfile
    assert "iproute2" in dockerfile
    assert "python3 -m venv" in dockerfile
    assert "python -m pip check" in dockerfile


def test_installed_wheel_smoke_discovers_packaged_plugins() -> None:
    verifier = (ROOT / "scripts/verify_wheel.py").read_text(encoding="utf-8")

    assert "from reticulumpi.plugin_loader import PluginLoader" in verifier
    assert "PluginLoader().discover([str(builtin_directory)])" in verifier
    for plugin_name in ("file_transfer", "messaging_hub", "nomadnet_server", "web_dashboard"):
        assert f'"{plugin_name}"' in verifier


def test_dashboard_service_worker_version_comes_from_package_metadata():
    static_version = ROOT / "src/reticulumpi/builtin_plugins/web_dashboard/static/version.js"
    assert not static_version.exists()

    response = asyncio.run(_serve_static(SimpleNamespace(match_info={"asset_path": "version.js"})))
    assert response.content_type == "application/javascript"
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.text == f"var APP_VERSION = {json.dumps(runtime_version)};\n"


@pytest.mark.parametrize(
    ("tag", "version"),
    (("v0.2.5", "0.2.5"), ("v3.14.159", "3.14.159")),
)
def test_release_tag_policy_accepts_strict_versions(tag, version):
    assert verify_release_tag.version_from_tag(tag) == version


@pytest.mark.parametrize(
    "tag",
    ("0.2.5", "v0.2", "v01.2.3", "v1.02.3", "v1.2.03", "v1.2.3-rc1", "v1.2.3.4"),
)
def test_release_tag_policy_rejects_noncanonical_versions(tag):
    with pytest.raises(ValueError, match="vMAJOR.MINOR.PATCH"):
        verify_release_tag.version_from_tag(tag)


def test_release_artifact_versions_are_read_from_metadata(tmp_path):
    wheel = tmp_path / "reticulumpi-0.3.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr(
            "reticulumpi-0.3.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: reticulumpi\nVersion: 0.3.0\n",
        )

    sdist = tmp_path / "reticulumpi-0.3.0.tar.gz"
    metadata = b"Metadata-Version: 2.4\nName: reticulumpi\nVersion: 0.3.0\n"
    member = tarfile.TarInfo("reticulumpi-0.3.0/PKG-INFO")
    member.size = len(metadata)
    with tarfile.open(sdist, mode="w:gz") as archive:
        archive.addfile(member, io.BytesIO(metadata))

    verify_release_tag.verify_artifacts([wheel, sdist], "0.3.0")
    with pytest.raises(ValueError, match="artifact version mismatch"):
        verify_release_tag.verify_artifacts([wheel], "0.3.1")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required for archive test")
def test_git_export_archive_preserves_exact_tag_version(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    shutil.copy2(ROOT / "pyproject.toml", repository / "pyproject.toml")
    shutil.copy2(ROOT / ".gitattributes", repository / ".gitattributes")
    shutil.copy2(ROOT / ".git_archival.txt", repository / ".git_archival.txt")
    (repository / "src/reticulumpi").mkdir(parents=True)

    commands = (
        ("init", "--quiet"),
        ("config", "user.name", "ReticulumPi test"),
        ("config", "user.email", "test@example.invalid"),
        ("add", "."),
        ("commit", "--quiet", "-m", "archive fixture"),
        ("tag", "v3.2.1"),
    )
    for command in commands:
        subprocess.run(["git", *command], cwd=repository, check=True)

    archive_path = tmp_path / "repository.tar"
    subprocess.run(
        ["git", "archive", "--format=tar", f"--output={archive_path}", "v3.2.1"],
        cwd=repository,
        check=True,
    )
    exported = tmp_path / "exported"
    exported.mkdir()
    with tarfile.open(archive_path) as archive:
        archive.extractall(exported, filter="data")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "setuptools_scm",
            "--root",
            str(exported),
            "--config",
            str(exported / "pyproject.toml"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "3.2.1"


def test_release_tag_must_be_annotated_and_signed(monkeypatch):
    responses = {
        ("cat-file", "-t", "refs/tags/v0.3.0"): "tag\n",
        ("rev-parse", "refs/tags/v0.3.0^{}"): "abc\n",
        ("rev-parse", "HEAD"): "abc\n",
        ("cat-file", "-p", "refs/tags/v0.3.0"): (
            "object abc\ntag v0.3.0\n\nrelease\n-----BEGIN SSH SIGNATURE-----\n"
        ),
    }
    monkeypatch.setattr(verify_release_tag, "_git", lambda *args: responses[args])
    verify_release_tag.verify_tag_object(
        "v0.3.0",
        require_signature=True,
        verify_signature=False,
    )

    responses[("rev-parse", "HEAD")] = "def\n"
    with pytest.raises(ValueError, match="does not identify"):
        verify_release_tag.verify_tag_object(
            "v0.3.0",
            require_signature=True,
            verify_signature=False,
        )

    responses[("rev-parse", "HEAD")] = "abc\n"
    responses[("cat-file", "-p", "refs/tags/v0.3.0")] = "object abc\n\nunsigned\n"
    with pytest.raises(ValueError, match="not signed"):
        verify_release_tag.verify_tag_object(
            "v0.3.0",
            require_signature=True,
            verify_signature=False,
        )


def test_signature_marker_cannot_replace_cryptographic_tag_verification(monkeypatch):
    responses = {
        ("cat-file", "-t", "refs/tags/v0.3.0"): "tag\n",
        ("rev-parse", "refs/tags/v0.3.0^{}"): "abc\n",
        ("rev-parse", "HEAD"): "abc\n",
        ("cat-file", "-p", "refs/tags/v0.3.0"): (
            "object abc\ntag v0.3.0\n\nfake\n-----BEGIN PGP SIGNATURE-----\n"
        ),
    }
    monkeypatch.setattr(verify_release_tag, "_git", lambda *args: responses[args])

    def reject_fake_signature(command, **kwargs):
        del kwargs
        assert command == ["git", "verify-tag", "v0.3.0"]
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(verify_release_tag.subprocess, "run", reject_fake_signature)
    with pytest.raises(subprocess.CalledProcessError):
        verify_release_tag.verify_tag_object(
            "v0.3.0",
            require_signature=True,
            verify_signature=True,
        )


def test_trusted_tag_verification_uses_isolated_keyring_and_exact_fingerprint(
    tmp_path,
    monkeypatch,
):
    fingerprint = "A" * 40
    signing_subkey = "B" * 40
    key = tmp_path / "release-tag.asc"
    key.write_text("public key fixture\n", encoding="ascii")
    responses = {
        ("cat-file", "-t", "refs/tags/v0.3.0"): "tag\n",
        ("rev-parse", "refs/tags/v0.3.0^{}"): "abc\n",
        ("rev-parse", "HEAD"): "abc\n",
        ("cat-file", "-p", "refs/tags/v0.3.0"): (
            "object abc\ntag v0.3.0\n\nrelease\n-----BEGIN PGP SIGNATURE-----\n"
        ),
    }
    monkeypatch.setattr(verify_release_tag, "_git", lambda *args: responses[args])
    invocations = []

    def fake_verifier(command, **kwargs):
        invocations.append((command, kwargs))
        if "show-only" in command:
            return subprocess.CompletedProcess(command, 0, f"fpr:::::::::{fingerprint}:\n", "")
        if command[0] == "gpg":
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(
            command,
            0,
            "",
            f"[GNUPG:] VALIDSIG {signing_subkey} 0 0 0 0 0 0 0 0 {fingerprint}\n",
        )

    monkeypatch.setattr(verify_release_tag.subprocess, "run", fake_verifier)
    verify_release_tag.verify_tag_object(
        "v0.3.0",
        require_signature=True,
        verify_signature=True,
        trusted_key=key,
        trusted_fingerprint=fingerprint.lower(),
    )

    assert len(invocations) == 3
    keyring_paths = {call[1]["env"]["GNUPGHOME"] for call in invocations}
    assert len(keyring_paths) == 1
    assert invocations[-1][0][:7] == [
        "git",
        "-c",
        "gpg.format=openpgp",
        "-c",
        "gpg.program=gpg",
        "verify-tag",
        "--raw",
    ]
    with pytest.raises(ValueError, match="absent from the trusted key"):
        verify_release_tag.verify_tag_object(
            "v0.3.0",
            require_signature=True,
            verify_signature=True,
            trusted_key=key,
            trusted_fingerprint="C" * 40,
        )


def test_admin_reads_generated_scm_version_without_executing_bundle_code(tmp_path):
    bundle = tmp_path / "reticulumpi-sdist"
    version_file = bundle / "src/reticulumpi/_version.py"
    version_file.parent.mkdir(parents=True)
    (bundle / "pyproject.toml").write_text(
        '[project]\nname = "reticulumpi"\ndynamic = ["version"]\n',
        encoding="utf-8",
    )
    version_file.write_text(
        "raise RuntimeError('must not execute')\n__version__ = version = '0.3.0'\n",
        encoding="utf-8",
    )

    assert admin_cli._source_metadata(bundle) == ("0.3.0", bundle)
