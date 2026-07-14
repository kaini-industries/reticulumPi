"""Regression tests for exact-artifact release publication and container evidence."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

from tools import (
    build_admin_deb,
    build_install_bundle,
    container_state_probe,
    prepare_release_assets,
)


ROOT = Path(__file__).resolve().parents[1]
VERSION = "3.2.1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tar_bytes(
    archive: tarfile.TarFile,
    name: str,
    payload: bytes,
    *,
    mode: int = 0o644,
) -> None:
    member = tarfile.TarInfo(name)
    member.mode = mode
    member.size = len(payload)
    archive.addfile(member, io.BytesIO(payload))


def _python_artifacts(directory: Path) -> tuple[Path, Path, Path]:
    directory.mkdir(parents=True)
    wheel = directory / f"reticulumpi-{VERSION}-py3-none-any.whl"
    metadata = (f"Metadata-Version: 2.4\nName: reticulumpi\nVersion: {VERSION}\n").encode()
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr(f"reticulumpi-{VERSION}.dist-info/METADATA", metadata)
        archive.writestr("reticulumpi/__init__.py", b"")
        archive.writestr("reticulumpi/admin_cli.py", b"def main(): return 0\n")
        archive.writestr("reticulumpi/cli_help.py", b"class StableHelpFormatter: pass\n")
        archive.writestr("reticulumpi/external_artifacts.py", b"")
        archive.writestr("reticulumpi/migration_catalog.py", b"")
        archive.writestr("reticulumpi/migrations.py", b"")
        archive.writestr("reticulumpi/platform_policy.py", b"PROFILE = 'fixture'\n")
        archive.writestr("reticulumpi/recovery_config.py", b"")
        archive.writestr("reticulumpi/runtime_metrics.py", b"")

    sdist = directory / f"reticulumpi-{VERSION}.tar.gz"
    root = f"reticulumpi-{VERSION}"
    sources = {
        "PKG-INFO": metadata,
        "pyproject.toml": b"[project]\nname='reticulumpi'\ndynamic=['version']\n",
        "README.md": b"# ReticulumPi\n",
        "src/reticulumpi/__init__.py": b"",
        "systemd/reticulumpi.service": b"[Service]\nExecStart=/usr/bin/reticulumpi\n",
        "config/config.example.yaml": b"reticulumpi: {}\n",
        "scripts/bootstrap.sh": b"#!/bin/sh\nexit 0\n",
        "constraints/production-universal-core.txt": b"example==1 --hash=sha256:00\n",
        "constraints/production-universal-dashboard-nomadnet.txt": (
            b"example==1 --hash=sha256:00\n"
        ),
        "constraints/production-universal-all-features.txt": (b"example==1 --hash=sha256:00\n"),
    }
    with tarfile.open(sdist, mode="w:gz") as archive:
        for name, payload in sources.items():
            mode = 0o755 if name.endswith(".sh") else 0o644
            _tar_bytes(archive, f"{root}/{name}", payload, mode=mode)

    sbom = directory / "reticulumpi.cdx.json"
    sbom.write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "version": 1,
                "components": [{"type": "application", "name": "reticulumpi", "version": VERSION}],
            }
        ),
        encoding="utf-8",
    )
    return wheel, sdist, sbom


def _fake_bundle_signature(
    manifest: Path,
    signature: Path,
    signing_key: Path,
    minisign: Path,
) -> None:
    del manifest, signing_key, minisign
    signature.write_text("untrusted comment: test fixture\nsignature\n", encoding="ascii")


def _build_bundle(
    tmp_path: Path,
    wheel: Path,
    sdist: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    parent: str = "bundle",
) -> Path:
    key = tmp_path / "release.key"
    key.write_text("test-only key material\n", encoding="ascii")
    key.chmod(0o600)
    output_directory = tmp_path / parent
    output_directory.mkdir()
    output = output_directory / f"reticulumpi-install-arm64-{VERSION}.tar.gz"
    monkeypatch.setattr(build_install_bundle, "sign_manifest", _fake_bundle_signature)
    return build_install_bundle.build_install_bundle(
        sdist=sdist,
        wheel=wheel,
        output=output,
        version=VERSION,
        signing_key=key,
        minisign=Path("/usr/bin/minisign"),
        source_date_epoch=1_700_000_000,
    )


def _image_artifact(directory: Path, architecture: str) -> Path:
    directory.mkdir(parents=True)
    archive_path = directory / f"reticulumpi-{architecture}.tar.gz"
    config_name = f"{architecture}.json"
    layer_name = f"{architecture}/layer.tar"
    with tarfile.open(archive_path, mode="w:gz") as archive:
        _tar_bytes(
            archive,
            config_name,
            json.dumps({"architecture": architecture, "os": "linux"}).encode(),
        )
        _tar_bytes(archive, layer_name, b"container layer")
        _tar_bytes(
            archive,
            "manifest.json",
            json.dumps(
                [
                    {
                        "Config": config_name,
                        "RepoTags": [f"reticulumpi:{architecture}"],
                        "Layers": [layer_name],
                    }
                ]
            ).encode(),
        )
    archive_path.with_name(f"{archive_path.name}.sha256").write_text(
        f"{_sha256(archive_path)}  {archive_path.name}\n",
        encoding="ascii",
    )
    return archive_path


def _admin_artifacts(directory: Path, wheel: Path) -> list[Path]:
    directory.mkdir(parents=True)
    runtime = directory.parent / "empty-admin-runtime"
    runtime.mkdir()
    manifest = directory.parent / "empty-admin-runtime.SHA256SUMS"
    manifest.write_bytes(b"")
    artifacts: list[Path] = []
    for profile in build_admin_deb.SUPPORTED_PLATFORM_PYTHON:
        output = directory / build_admin_deb.admin_deb_filename(VERSION, profile)
        result = build_admin_deb.build_admin_deb(
            wheel=wheel,
            wheel_sha256=_sha256(wheel),
            runtime_source=runtime,
            runtime_manifest=manifest,
            runtime_kind="site-packages",
            output=output,
            version=VERSION,
            platform_profile=profile,
            source_date_epoch=1_700_000_000,
        )
        artifacts.extend((result.package, result.sha256))
    return artifacts


def test_install_bundle_is_deterministic_and_contains_the_exact_wheel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel, sdist, _sbom = _python_artifacts(tmp_path / "python")
    first = _build_bundle(tmp_path, wheel, sdist, monkeypatch, parent="first")
    second = _build_bundle(tmp_path, wheel, sdist, monkeypatch, parent="second")

    assert first.read_bytes() == second.read_bytes()
    prepare_release_assets.inspect_install_bundle(first, VERSION, wheel)
    with tarfile.open(first, mode="r:gz") as archive:
        names = {member.name for member in archive}
        packaged = archive.extractfile(f"reticulumpi-{VERSION}/{wheel.name}")
        assert packaged is not None
        assert packaged.read() == wheel.read_bytes()
    for filename in (
        "production-universal-core.txt",
        "production-universal-dashboard-nomadnet.txt",
        "production-universal-all-features.txt",
    ):
        assert f"reticulumpi-{VERSION}/constraints/{filename}" in names
    assert not any("bookworm-py311-" in name for name in names)


def test_install_bundle_publisher_rejects_retired_dependency_aliases(tmp_path: Path) -> None:
    root = tmp_path / f"reticulumpi-{VERSION}"
    for directory in build_install_bundle.REQUIRED_DIRECTORIES:
        (root / directory).mkdir(parents=True, exist_ok=True)
    for filename in build_install_bundle.REQUIRED_FILES:
        path = root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    build_install_bundle._validate_source_root(root)

    retired = (
        root / "constraints" / build_install_bundle.LEGACY_CONSTRAINT_GLOB.replace("*", "core.txt")
    )
    retired.write_text("legacy fixture\n", encoding="utf-8")
    with pytest.raises(
        build_install_bundle.InstallBundleError,
        match="retired dependency profile aliases",
    ):
        build_install_bundle._validate_source_root(root)


def test_minisign_signing_is_noninteractive_and_rejects_an_unsafe_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = tmp_path / "release.key"
    key.write_text("private test fixture\n", encoding="ascii")
    key.chmod(0o600)
    manifest = tmp_path / "SHA256SUMS"
    manifest.write_text("0" * 64 + "  artifact\n", encoding="ascii")
    signature = tmp_path / "SHA256SUMS.minisig"
    invocation: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        invocation["command"] = command
        invocation.update(kwargs)
        signature.write_text("signature\n", encoding="ascii")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(build_install_bundle.subprocess, "run", fake_run)
    build_install_bundle.sign_manifest(manifest, signature, key, Path("/usr/bin/minisign"))

    command = invocation["command"]
    assert isinstance(command, list)
    assert command[:5] == [
        "/usr/bin/minisign",
        "-S",
        "-W",
        "-t",
        "ReticulumPi install-bundle SHA256SUMS",
    ]
    assert invocation["stdin"] is subprocess.DEVNULL
    assert invocation["stdout"] is subprocess.DEVNULL
    assert invocation["env"] == {"LANG": "C", "PATH": "/usr/bin:/bin"}

    key.chmod(0o640)
    with pytest.raises(build_install_bundle.InstallBundleError, match="group- or world"):
        build_install_bundle.sign_manifest(
            manifest,
            tmp_path / "unsafe.minisig",
            key,
            Path("/usr/bin/minisign"),
        )


def test_release_staging_rejects_drift_and_writes_one_global_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python_directory = tmp_path / "python"
    wheel, sdist, _sbom = _python_artifacts(python_directory)
    bundle = _build_bundle(tmp_path, wheel, sdist, monkeypatch)
    image_directories = {
        architecture: tmp_path / "images" / architecture
        for architecture in prepare_release_assets.ARCHITECTURES
    }
    image_archives = {
        architecture: _image_artifact(directory, architecture)
        for architecture, directory in image_directories.items()
    }
    admin_directory = tmp_path / "recovery-admin"
    admin_artifacts = _admin_artifacts(admin_directory, wheel)
    provenance = tmp_path / prepare_release_assets.RELEASE_PROVENANCE_NAME
    provenance.write_text(
        json.dumps(
            {"commit": "a" * 40, "schema": 1, "tag": f"v{VERSION}"},
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="ascii",
    )

    output = tmp_path / "release-assets"
    staged = prepare_release_assets.prepare_release_assets(
        tag=f"v{VERSION}",
        python_directory=python_directory,
        image_directories=image_directories,
        recovery_admin_directory=admin_directory,
        provenance=provenance,
        install_bundle=bundle,
        output_directory=output,
    )

    expected_names = {
        wheel.name,
        sdist.name,
        f"reticulumpi-{VERSION}.cdx.json",
        f"reticulumpi-container-{VERSION}-amd64.tar.gz",
        f"reticulumpi-container-{VERSION}-arm64.tar.gz",
        bundle.name,
        prepare_release_assets.RELEASE_PROVENANCE_NAME,
        *(path.name for path in admin_artifacts),
        "SHA256SUMS",
    }
    assert {path.name for path in staged} == expected_names
    manifest_entries = {
        name: digest
        for digest, name in (
            line.split("  ", maxsplit=1)
            for line in (output / "SHA256SUMS").read_text(encoding="ascii").splitlines()
        )
    }
    assert set(manifest_entries) == expected_names - {"SHA256SUMS"}
    assert all(_sha256(output / name) == digest for name, digest in manifest_entries.items())
    assert (output / wheel.name).read_bytes() == wheel.read_bytes()
    assert (output / provenance.name).read_bytes() == provenance.read_bytes()

    with pytest.raises(prepare_release_assets.ReleaseAssetError, match="arm64"):
        prepare_release_assets.inspect_image_archive(image_archives["amd64"], "arm64")


def test_recovery_admin_release_validation_fails_closed(tmp_path: Path) -> None:
    wheel, _sdist, _sbom = _python_artifacts(tmp_path / "python")
    directory = tmp_path / "recovery-admin"
    artifacts = _admin_artifacts(directory, wheel)
    packages = {path.name: path for path in artifacts if path.suffix == ".deb"}

    for profile in build_admin_deb.SUPPORTED_PLATFORM_PYTHON:
        package = packages[build_admin_deb.admin_deb_filename(VERSION, profile)]
        prepare_release_assets.inspect_admin_deb(
            package, version=VERSION, profile=profile, wheel=wheel
        )

    bookworm = "linux-arm64-debian-bookworm-py311"
    noble = "linux-arm64-ubuntu-noble-py312"
    forged = tmp_path / build_admin_deb.admin_deb_filename(VERSION, bookworm)
    forged.write_bytes(packages[build_admin_deb.admin_deb_filename(VERSION, noble)].read_bytes())
    with pytest.raises(prepare_release_assets.ReleaseAssetError, match="control field"):
        prepare_release_assets.inspect_admin_deb(
            forged, version=VERSION, profile=bookworm, wheel=wheel
        )

    malformed = tmp_path / "malformed" / build_admin_deb.admin_deb_filename(VERSION, bookworm)
    malformed.parent.mkdir()
    malformed.write_bytes(b"not a deb")
    with pytest.raises(prepare_release_assets.ReleaseAssetError, match="ar signature"):
        prepare_release_assets.inspect_admin_deb(
            malformed, version=VERSION, profile=bookworm, wheel=wheel
        )

    package = packages[build_admin_deb.admin_deb_filename(VERSION, bookworm)]
    sidecar = package.with_name(f"{package.name}.sha256")
    sidecar.write_text(f"{'0' * 64}  {package.name}\n", encoding="ascii")
    with pytest.raises(prepare_release_assets.ReleaseAssetError, match="does not match"):
        prepare_release_assets._verify_sidecar(package, sidecar)


def test_container_probe_rejects_links_modes_and_corrupt_databases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(container_state_probe, "DATA", data)
    secret = data / "secret"
    secret.write_bytes(b"secret")
    secret.chmod(0o600)
    assert container_state_probe._required(secret, mode=0o600) == secret

    secret.chmod(0o640)
    with pytest.raises(SystemExit, match="unsafe mode"):
        container_state_probe._required(secret, mode=0o600)
    secret.chmod(0o600)

    link = data / "link"
    link.symlink_to(secret)
    with pytest.raises(SystemExit, match="symbolic link"):
        container_state_probe._required(link, mode=0o600)

    database = data / "state.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
    connection.commit()
    connection.close()
    database.chmod(0o600)
    database_state = container_state_probe._database_state(database)
    assert database_state == {
        "inode": database.stat().st_ino,
        "mode": 0o600,
        "integrity": "ok",
        "tables": ["state"],
        "user_version": 0,
    }
    database.write_bytes(b"not sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        container_state_probe._database_state(database)


def test_tag_publication_job_promotes_validated_artifacts_without_rebuilding() -> None:
    workflow_path = ROOT / ".github/workflows/ci.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    candidate_path = ROOT / ".github/workflows/release-candidate.yml"
    candidate_workflow = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
    release_path = ROOT / ".github/workflows/release.yml"
    release_workflow = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    release_inputs = workflow["jobs"]["release-inputs"]
    release_inputs_source = json.dumps(release_inputs)
    global_request = candidate_workflow["jobs"]["global-signing-request"]
    global_request_source = json.dumps(global_request)
    candidate = release_workflow["jobs"]["release-candidate"]
    candidate_source = json.dumps(candidate)
    release = release_workflow["jobs"]["release"]
    release_source = json.dumps(release)
    tag_trust = workflow["jobs"]["release-tag-trust"]
    tag_trust_source = json.dumps(tag_trust)
    all_workflow_source = "\n".join(
        path.read_text(encoding="utf-8") for path in (workflow_path, candidate_path, release_path)
    )

    assert "github.ref_type == 'tag'" in release_inputs["if"]
    assert "needs['release-tag-trust'].result == 'success'" in release_inputs["if"]
    assert "needs['dashboard-performance'].result == 'success'" in release_inputs["if"]
    assert "needs['bookworm-systemd'].result == 'success'" in release_inputs["if"]
    assert "needs['noble-systemd'].result == 'success'" in release_inputs["if"]
    assert set(release_inputs["needs"]) >= {
        "package",
        "recovery-admin",
        "container",
        "coverage",
        "test",
        "dashboard-performance",
        "bookworm-systemd",
        "noble-systemd",
        "release-tag-trust",
    }
    assert release_inputs["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
    }
    assert "environment" not in release_inputs
    assert "tools.offline_release prepare-inputs" in release_inputs_source
    assert "release-signing-input-${{ github.ref_name }}" in release_inputs_source
    assert "RELEASE-INPUTS.SHA256SUMS" in release_inputs_source
    assert "actions/attest@59d89421af93a897026c735860bf21b6eb4f7b26" in (release_inputs_source)

    assert "workflow_dispatch:" in candidate_path.read_text(encoding="utf-8")
    assert candidate_workflow["permissions"] == {
        "contents": "read",
        "actions": "read",
        "id-token": "write",
        "attestations": "write",
    }
    assert "environment" not in global_request
    assert "tools.offline_release stage-global-request" in global_request_source
    assert "release-signing-input-${{ inputs.tag }}" in global_request_source
    assert "global-signing-request-${{ inputs.tag }}" in global_request_source
    assert "--input-manifest-sha256" in global_request_source
    assert "--paginate --slurp" in global_request_source
    assert ".github/workflows/ci.yml" in global_request_source
    assert "verify-tag --raw" in global_request_source
    assert "docker push" not in global_request_source
    assert "gh release create" not in global_request_source

    assert candidate["environment"] == "release-signing"
    assert candidate["permissions"] == {"contents": "read", "actions": "read"}
    assert "tools.offline_release finalize-candidate" in candidate_source
    assert "global-signing-request-${{ inputs.tag }}" in candidate_source
    assert "signed-release-candidate-${{ inputs.tag }}" in candidate_source
    assert "--input-manifest-sha256" in candidate_source
    assert "--paginate --slurp" in candidate_source
    assert "reticulumpi.admin_cli install" in candidate_source
    assert "--dry-run" in candidate_source
    assert "docker push" not in candidate_source
    assert "gh release create" not in candidate_source

    assert release["environment"] == "release"
    assert release["needs"] == "release-candidate"
    assert release["permissions"] == {
        "contents": "write",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
    }
    assert workflow["concurrency"]["cancel-in-progress"] == "${{ github.ref_type != 'tag' }}"
    assert release_workflow["concurrency"]["cancel-in-progress"] is False
    assert "docker build" not in release_source
    assert "python -m build" not in release_source
    assert "tools.offline_release verify-candidate" in release_source
    assert "signed-release-candidate-${{ inputs.tag }}" in release_source
    assert "MINISIGN_SECRET_KEY" not in all_workflow_source
    assert "minisign -S" not in all_workflow_source
    assert tag_trust["if"] == "github.ref_type == 'tag'"
    assert tag_trust["permissions"] == {"contents": "read"}
    assert "RELEASE_TAG_PUBLIC_KEY" in tag_trust_source
    assert "RELEASE_TAG_FINGERPRINT" in tag_trust_source
    assert "verify-tag --raw" in tag_trust_source
    assert "gpg.program=/usr/bin/gpg" in tag_trust_source
    assert "GNUPG:" in tag_trust_source
    assert "VALIDSIG" in tag_trust_source
    assert "tools/verify_release_tag.py" not in tag_trust_source
    annotated_tag_ref = "${{ github.ref_type == 'tag' && github.ref || github.sha }}"
    tag_object_jobs = (
        workflow["jobs"]["release-tag-trust"],
        workflow["jobs"]["package"],
        candidate_workflow["jobs"]["global-signing-request"],
        release_workflow["jobs"]["release-candidate"],
        release_workflow["jobs"]["release"],
    )
    for job in tag_object_jobs:
        checkout = next(
            step
            for step in job["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        assert checkout["with"]["ref"] == annotated_tag_ref
    package_job_steps = workflow["jobs"]["package"]["steps"]
    package_steps = {step.get("name"): step for step in package_job_steps if step.get("name")}
    package_tag_binding = package_steps["Bind package build to the exact annotated tag"]
    assert package_tag_binding["if"] == "github.ref_type == 'tag'"
    assert "git cat-file -t" in package_tag_binding["run"]
    assert "refs/tags/${GITHUB_REF_NAME}^{}" in package_tag_binding["run"]
    assert "git rev-parse HEAD" in package_tag_binding["run"]
    assert "GITHUB_SHA" in package_tag_binding["run"]
    assert package_job_steps.index(package_tag_binding) < next(
        index
        for index, step in enumerate(package_job_steps)
        if str(step.get("uses", "")).startswith("actions/setup-python@")
    )
    assert "subject-checksums" in release_source
    assert "sbom-path" in release_source
    assert "push-to-registry" in release_source
    assert release_source.count("actions/attest@59d89421af93a897026c735860bf21b6eb4f7b26") == 3
    release_steps = {step.get("name"): step for step in release["steps"] if step.get("name")}
    attest_action = "actions/attest@59d89421af93a897026c735860bf21b6eb4f7b26"
    assert release_steps["Attest release artifact provenance"] == {
        "name": "Attest release artifact provenance",
        "uses": attest_action,
        "with": {"subject-checksums": "release-assets/SHA256SUMS"},
    }
    assert release_steps["Attest wheel SBOM"] == {
        "name": "Attest wheel SBOM",
        "uses": attest_action,
        "with": {
            "subject-path": "release-assets/*.whl",
            "sbom-path": (
                "release-assets/reticulumpi-${{ steps.release-meta.outputs.version }}.cdx.json"
            ),
        },
    }
    assert release_steps["Attest published multi-architecture image"] == {
        "name": "Attest published multi-architecture image",
        "uses": attest_action,
        "with": {
            "subject-name": "${{ steps.images.outputs.image }}",
            "subject-digest": "${{ steps.images.outputs.digest }}",
            "push-to-registry": True,
        },
    }
    bookworm = workflow["jobs"]["bookworm-systemd"]
    bookworm_source = json.dumps(bookworm)
    assert bookworm["runs-on"] == "ubuntu-24.04-arm"
    assert not any(
        str(step.get("uses", "")).startswith("docker/setup-qemu-action@")
        for step in bookworm["steps"]
    )
    assert set(bookworm["needs"]) == {"package", "recovery-admin", "release-tag-trust"}
    assert "github.event_name == 'push'" in bookworm["if"]
    assert "needs['release-tag-trust'].result == 'success'" in bookworm["if"]
    assert "docker/systemd-ci.Dockerfile" in bookworm_source
    assert "docker/arm64-all-features-ci.Dockerfile" in bookworm_source
    assert "--privileged" in bookworm_source
    assert bookworm_source.count("linux/arm64") >= 3
    assert "/opt/reticulumpi" in bookworm_source
    assert "/srv/reticulumpi" in bookworm_source
    assert "tools/verify_bookworm_systemd.sh" in bookworm_source
    assert "recovery-administrators" in bookworm_source
    systemd_dockerfile = (ROOT / "docker/systemd-ci.Dockerfile").read_text(encoding="utf-8")
    assert "python:3.11-slim-bookworm@sha256:" in systemd_dockerfile
    assert "COPY dist/reticulumpi-*.whl" in systemd_dockerfile
    assert "linux-arm64-debian-bookworm-py311_arm64.deb" in systemd_dockerfile
    assert "dpkg --install /tmp/reticulumpi-admin.deb" in systemd_dockerfile
    assert "/usr/sbin/reticulumpi-admin --help" in systemd_dockerfile
    assert 'CMD ["/sbin/init"]' in systemd_dockerfile

    fixture = (ROOT / "tools/verify_bookworm_systemd.sh").read_text(encoding="utf-8")
    assert "uname -m" in fixture
    assert "minisign -G -W" in fixture
    assert "reticulumpi-admin install" in fixture
    assert "/usr/sbin/reticulumpi-admin install" in fixture
    assert '"$install_root/current/.venv/bin/reticulumpi-admin" doctor' in fixture
    assert "--apply --start" in fixture
    assert "0.3.9-interrupted" in fixture
    assert "--failing-service" in fixture
    assert "reticulumpi-admin rollback --to 0.3.0 --apply" in fixture

    feature_gate = (ROOT / "docker/arm64-all-features-ci.Dockerfile").read_text(encoding="utf-8")
    assert "production-universal-all-features.txt" in feature_gate
    assert "--require-hashes" in feature_gate
    assert "python -m pip check" in feature_gate
    assert 'CMD ["python", "-m", "pip", "check"]' in feature_gate

    noble = workflow["jobs"]["noble-systemd"]
    noble_source = json.dumps(noble)
    assert noble["runs-on"] == "ubuntu-24.04-arm"
    assert not any(
        str(step.get("uses", "")).startswith("docker/setup-qemu-action@") for step in noble["steps"]
    )
    assert set(noble["needs"]) == {"package", "recovery-admin", "release-tag-trust"}
    assert "github.event_name == 'push'" in noble["if"]
    assert "needs['release-tag-trust'].result == 'success'" in noble["if"]
    assert "docker/noble-systemd-ci.Dockerfile" in noble_source
    assert "--privileged" in noble_source
    assert noble_source.count("linux/arm64") >= 2
    assert "/opt/reticulumpi" in noble_source
    assert "/srv/reticulumpi" in noble_source
    assert "legacy-bridge:/srv/reticulumpi" in noble_source
    assert "RETICULUMPI_CI_SCENARIO" in noble_source
    assert "tools/verify_noble_systemd.sh" in noble_source
    assert "recovery-administrators" in noble_source

    noble_dockerfile = (ROOT / "docker/noble-systemd-ci.Dockerfile").read_text(encoding="utf-8")
    assert (
        "ubuntu:24.04@sha256:"
        "4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90" in noble_dockerfile
    )
    assert "python3-venv" in noble_dockerfile
    assert "python3-dev" in noble_dockerfile
    assert "build-essential" in noble_dockerfile
    assert "iproute2" in noble_dockerfile
    assert "COPY dist/reticulumpi-*.whl" in noble_dockerfile
    assert "linux-arm64-ubuntu-noble-py312_arm64.deb" in noble_dockerfile
    assert "dpkg --install /tmp/reticulumpi-admin.deb" in noble_dockerfile
    assert "/usr/sbin/reticulumpi-admin --help" in noble_dockerfile
    assert 'CMD ["/sbin/init"]' in noble_dockerfile

    recovery = workflow["jobs"]["recovery-admin"]
    recovery_source = json.dumps(recovery)
    assert recovery["needs"] == "package"
    assert "python-distributions" in recovery_source
    assert "linux-arm64-debian-bookworm-py311" in recovery_source
    assert "linux-arm64-ubuntu-noble-py312" in recovery_source
    assert "admin-runtime.SHA256SUMS" in recovery_source
    assert "recovery-administrators" in recovery_source
    assert "tools/build_admin_deb.py" in recovery_source
    assert "--wheel-sha256" in recovery_source
    assert 'print(metadata[\\"Version\\"])' in recovery_source
    assert "GITHUB_REF_NAME#v" not in recovery_source
    assert "--runtime-kind site-packages" in recovery_source
    assert "--runtime-source admin-runtime" in recovery_source
    assert "--runtime-manifest admin-runtime.SHA256SUMS" in recovery_source
    assert "--platform-profile" in recovery_source
    assert "--source-date-epoch" in recovery_source
    assert "recovery-administrators" in release_inputs_source
    assert "release-input/recovery-admin" in release_inputs_source

    noble_fixture = (ROOT / "tools/verify_noble_systemd.sh").read_text(encoding="utf-8")
    assert "VERSION_CODENAME:-} != noble" in noble_fixture
    assert "VERSION_ID:-} != 24.04" in noble_fixture
    assert "ID:-} != ubuntu" in noble_fixture
    assert "Python 3.12" in noble_fixture
    assert "uname -m" in noble_fixture
    assert "minisign -G -W" in noble_fixture
    assert "reticulumpi-admin install" in noble_fixture
    assert "/usr/sbin/reticulumpi-admin install" in noble_fixture
    assert '"$install_root/current/.venv/bin/reticulumpi-admin" doctor' in noble_fixture
    assert "--apply --start" in noble_fixture
    assert "0.3.9-interrupted" in noble_fixture
    assert "--failing-service" in noble_fixture
    assert "reticulumpi-admin rollback --to 0.3.0 --apply" in noble_fixture
    assert "RETICULUMPI_CI_SCENARIO" in noble_fixture
    assert "legacy-bridge" in noble_fixture
    assert "reticulumpi-admin rollback --to legacy --apply" in noble_fixture
    assert "linux-arm64-ubuntu-noble-py312" in noble_fixture
    assert "production-universal-all-features.txt" in noble_fixture
    assert "/opt/reticulumpi/meshchat/storage/continuity.txt" in noble_fixture
    assert "/var/lib/reticulumpi/meshchat/storage/continuity.txt" in noble_fixture
    assert "/srv/reticulumpi-external/meshchat" in noble_fixture
    assert "schema: 1" in noble_fixture
    assert "--config /etc/reticulumpi/config.yaml --check" in noble_fixture
    assert "Config validation: OK" in noble_fixture
    assert "stub-ready" in noble_fixture
    assert "fixture refuses hardware access" in noble_fixture
    assert 'load_manifest("/etc/reticulumpi/external-artifacts.yaml")' in noble_fixture
    assert "root:reticulumpi 640" in noble_fixture
    assert '"root:root 555"' in noble_fixture
    assert "/etc/sudoers.d/reticulumpi-offline" in noble_fixture
    assert "/opt/reticulumpi/scripts/simulate_offline.sh" in noble_fixture
    assert "/usr/libexec/reticulumpi/simulate_offline.sh" in noble_fixture
    assert "/usr/share/reticulumpi/config/offline_profile.yaml" in noble_fixture
    assert (
        "reticulumpi ALL=(ALL) NOPASSWD: /opt/reticulumpi/scripts/simulate_offline.sh"
        in noble_fixture
    )
    assert "legacy_offline_sudoers_hash" in noble_fixture
    assert "legacy_offline_sudoers_stat" in noble_fixture
    assert '"root:root 755"' in noble_fixture
    assert '"root:root 644"' in noble_fixture
    for artifact in ("meshchat", "rtl_test", "dump1090", "rtl_fm", "rtl_power"):
        assert f"  {artifact}:\n" in noble_fixture
    for feature in (
        "adsb",
        "captive-portal",
        "chrony-control",
        "dashboard",
        "gps",
        "lora",
        "meshcore",
        "meshtastic",
        "nomadnet",
        "offline-tools",
        "sensors",
        "shared-rnsd",
        "space",
        "watchdog",
    ):
        assert f"\n        {feature}\n" in noble_fixture


def test_systemd_fixture_bundle_builder_runs_as_a_script() -> None:
    result = subprocess.run(
        [sys.executable, "tools/build_systemd_ci_bundle.py", "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--signing-key" in result.stdout


def test_container_release_scripts_enforce_hardened_exact_image_flow() -> None:
    runtime = (ROOT / "tools/verify_container_runtime.sh").read_text(encoding="utf-8")
    publication = (ROOT / "tools/publish_validated_images.sh").read_text(encoding="utf-8")
    dockerfile = (ROOT / "docker/Dockerfile").read_text(encoding="utf-8")
    config = yaml.safe_load((ROOT / "docker/config.ci.yaml").read_text(encoding="utf-8"))
    compose = yaml.safe_load((ROOT / "docker/docker-compose.yml").read_text(encoding="utf-8"))

    assert runtime.count("--read-only") >= 3
    assert runtime.count("--cap-drop ALL") >= 3
    assert runtime.count("--network none") >= 3
    assert "no-new-privileges=true" in runtime
    assert 'urlopen("http://127.0.0.1:8080/login.html"' in runtime
    assert 'urlopen("http://127.0.0.1:8080/api/version"' in runtime
    assert 'urlopen("http://127.0.0.1:8080/api/status"' in runtime
    assert "anonymous loopback access to a protected API unexpectedly succeeded" in runtime
    assert 'kill -TERM "$pid"' in runtime
    assert "container remained live after rnsd exited" in runtime
    assert "touch /cache/recreation-sentinel" in runtime
    assert "test ! -e /cache/recreation-sentinel" in runtime
    assert "reticulumpi-cache-" not in runtime
    assert "docker build" not in publication
    assert "docker save" not in publication
    assert "docker load" in publication
    assert "docker manifest push" in publication
    assert config["reticulumpi"]["plugins"]["web_dashboard"]["local_api"] == {"enabled": False}
    assert config["reticulumpi"]["identity_path"].startswith("/data/")
    service = compose["services"]["reticulumpi"]
    assert service["user"] == "10001:10001"
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["pids_limit"] == 256
    assert "./config/config.yaml:/config/config.yaml:ro" in service["volumes"]
    assert not any(str(path).endswith(":/cache") for path in service["volumes"])
    assert any(path.startswith("/cache:") for path in service["tmpfs"])
    assert any(path.startswith("/run/reticulumpi:") for path in service["tmpfs"])
    assert 'VOLUME ["/data"]' in dockerfile
    assert 'VOLUME ["/data", "/cache"]' not in dockerfile


def test_image_publisher_fails_closed_and_emits_registry_digests(tmp_path: Path) -> None:
    assets = tmp_path / "release-assets"
    assets.mkdir()
    for architecture in prepare_release_assets.ARCHITECTURES:
        path = assets / f"reticulumpi-container-{VERSION}-{architecture}.tar.gz"
        with gzip.open(path, mode="wb") as archive:
            archive.write(b"validated image archive")
    image_assets = sorted(assets.glob("*.tar.gz"))
    (assets / "SHA256SUMS").write_text(
        "".join(f"{_sha256(path)}  {path.name}\n" for path in image_assets),
        encoding="ascii",
    )
    (assets / "SHA256SUMS.minisig").write_text("signature fixture\n", encoding="ascii")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sha256sum = fake_bin / "sha256sum"
    sha256sum.write_text(
        '#!/usr/bin/env bash\nexec shasum -a 256 -c "${@: -1}"\n',
        encoding="utf-8",
    )
    sha256sum.chmod(0o755)
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ $1 == manifest && $2 == inspect ]]; then
    if [[ ${DOCKER_INSPECT_ERROR:-} == auth ]]; then
        echo 'authorization failed' >&2
    else
        echo 'manifest unknown' >&2
    fi
    exit 1
fi
if [[ $1 == load ]]; then cat >/dev/null; exit 0; fi
if [[ $1 == image && $2 == inspect ]]; then
    target=${@: -1}
    if [[ $* == *Architecture* ]]; then echo "${target##*:}"; else echo linux; fi
    exit 0
fi
if [[ $1 == tag ]]; then exit 0; fi
if [[ $1 == push ]]; then
    printf 'digest: sha256:%064d size: 1\\n' 1
    exit 0
fi
if [[ $1 == manifest && $2 == create ]]; then exit 0; fi
if [[ $1 == manifest && $2 == annotate ]]; then exit 0; fi
if [[ $1 == manifest && $2 == push ]]; then
    printf 'sha256:%064d\\n' 2
    exit 0
fi
exit 9
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    output = tmp_path / "github-output"
    environment = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(output),
    }
    command = [
        "bash",
        str(ROOT / "tools/publish_validated_images.sh"),
        str(assets),
        "ghcr.io/example/reticulumpi",
        VERSION,
    ]
    result = subprocess.run(command, env=environment, check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    fields = dict(
        line.split("=", maxsplit=1) for line in output.read_text(encoding="utf-8").splitlines()
    )
    assert fields["image"] == "ghcr.io/example/reticulumpi"
    assert fields["digest"] == f"sha256:{2:064d}"
    assert fields["amd64-digest"] == f"sha256:{1:064d}"
    assert fields["arm64-digest"] == f"sha256:{1:064d}"

    environment["DOCKER_INSPECT_ERROR"] = "auth"
    refused = subprocess.run(
        command,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert refused.returncode == 1
    assert "could not prove GHCR tag is absent" in refused.stderr
