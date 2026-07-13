"""Regression tests for the two-stage public-signature release envelope."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.test_release_publication import (  # noqa: PLC2701
    VERSION,
    _admin_artifacts,
    _image_artifact,
    _python_artifacts,
)
from tools import build_install_bundle, offline_release


SOURCE = offline_release.ExpectedRelease(
    tag=f"v{VERSION}",
    repository="example/reticulumpi",
    commit="a" * 40,
    source_run_id=101,
    source_run_attempt=2,
)
CANDIDATE = offline_release.ExpectedRelease(
    tag=SOURCE.tag,
    repository=SOURCE.repository,
    commit=SOURCE.commit,
    source_run_id=SOURCE.source_run_id,
    source_run_attempt=SOURCE.source_run_attempt,
    candidate_run_id=202,
    candidate_run_attempt=3,
)


def _release_inputs(tmp_path: Path) -> Path:
    root = tmp_path / "release-input"
    wheel, _sdist, _sbom = _python_artifacts(root / "python")
    for architecture in offline_release.prepare_release_assets.ARCHITECTURES:
        _image_artifact(root / "images" / architecture, architecture)
    built_admin = _admin_artifacts(tmp_path / "admin-build" / "recovery-admin", wheel)
    admin = root / "recovery-admin"
    admin.mkdir()
    for artifact in built_admin:
        shutil.copy2(artifact, admin / artifact.name)
    return root


def _public_key(path: Path) -> Path:
    material = base64.b64encode(b"p" * 42).decode("ascii")
    path.write_text(f"untrusted comment: test public key\n{material}\n", encoding="ascii")
    path.chmod(0o644)
    return path


def _prepare(tmp_path: Path) -> Path:
    inputs = _release_inputs(tmp_path)
    offline_release.prepare_inputs(
        input_directory=inputs,
        expected=SOURCE,
        source_date_epoch=1_700_000_000,
    )
    return inputs


def _candidate(inputs: Path) -> offline_release.ExpectedRelease:
    digest = hashlib.sha256((inputs / offline_release.INPUT_MANIFEST_NAME).read_bytes()).hexdigest()
    return replace(CANDIDATE, input_manifest_sha256=digest)


def test_prepare_inputs_binds_the_exact_tree_and_install_manifest(tmp_path: Path) -> None:
    inputs = _prepare(tmp_path)

    selected = offline_release.verify_inputs(input_directory=inputs, expected=SOURCE)
    provenance = json.loads((inputs / offline_release.PROVENANCE_NAME).read_text())

    assert selected.version == VERSION
    assert provenance == {
        "artifact": f"release-signing-input-v{VERSION}",
        "commit": "a" * 40,
        "kind": offline_release.INPUT_PROVENANCE_KIND,
        "repository": "example/reticulumpi",
        "schema": 1,
        "source_date_epoch": 1_700_000_000,
        "source_run_attempt": 2,
        "source_run_id": 101,
        "source_workflow": offline_release.SOURCE_WORKFLOW,
        "tag": f"v{VERSION}",
    }
    assert (inputs / offline_release.INSTALL_MANIFEST_NAME).read_bytes() == (
        build_install_bundle.render_install_manifest(
            sdist=selected.sdist,
            wheel=selected.wheel,
            version=VERSION,
        )
    )

    selected.sbom.write_text("{}", encoding="ascii")
    with pytest.raises(offline_release.OfflineReleaseError, match="checksum mismatch"):
        offline_release.verify_inputs(input_directory=inputs, expected=SOURCE)


def test_input_verification_rejects_run_drift_and_extra_paths(tmp_path: Path) -> None:
    inputs = _prepare(tmp_path)
    wrong = offline_release.ExpectedRelease(
        tag=SOURCE.tag,
        repository=SOURCE.repository,
        commit=SOURCE.commit,
        source_run_id=999,
        source_run_attempt=SOURCE.source_run_attempt,
    )
    with pytest.raises(offline_release.OfflineReleaseError, match="expected run"):
        offline_release.verify_inputs(input_directory=inputs, expected=wrong)

    (inputs / "unexpected").write_bytes(b"unexpected")
    with pytest.raises(offline_release.OfflineReleaseError, match="top-level"):
        offline_release.verify_inputs(input_directory=inputs, expected=SOURCE)


def test_input_verification_rejects_unsigned_empty_directories(tmp_path: Path) -> None:
    inputs = _prepare(tmp_path)
    (inputs / "python" / "unsigned-empty-directory").mkdir()

    with pytest.raises(offline_release.OfflineReleaseError, match="unsigned empty directories"):
        offline_release.verify_inputs(input_directory=inputs, expected=SOURCE)


def test_two_stage_candidate_is_exact_and_fails_closed_on_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _prepare(tmp_path)
    candidate_release = _candidate(inputs)
    key = _public_key(tmp_path / "release.pub")
    inner_signature = tmp_path / "inner.minisig"
    inner_signature.write_text(
        "untrusted comment: fixture\ninner signature fixture\n", encoding="ascii"
    )
    global_signature = tmp_path / "global.minisig"
    global_signature.write_text(
        "untrusted comment: fixture\nglobal signature fixture\n", encoding="ascii"
    )
    monkeypatch.setattr(offline_release, "_run_minisign", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        build_install_bundle,
        "_verify_manifest_signature",
        lambda *args, **kwargs: None,
    )

    wrong_digest = replace(candidate_release, input_manifest_sha256="0" * 64)
    with pytest.raises(offline_release.OfflineReleaseError, match="offline-verified digest"):
        offline_release.stage_global_request(
            input_directory=inputs,
            output_directory=tmp_path / "wrong-global-request",
            inner_signature=inner_signature,
            public_key=key,
            minisign=Path("/usr/bin/minisign"),
            expected=wrong_digest,
        )

    request = tmp_path / "global-request"
    offline_release.stage_global_request(
        input_directory=inputs,
        output_directory=request,
        inner_signature=inner_signature,
        public_key=key,
        minisign=Path("/usr/bin/minisign"),
        expected=candidate_release,
    )
    assert (request / offline_release.GLOBAL_MANIFEST_NAME).is_file()
    assert not (request / offline_release.GLOBAL_SIGNATURE_NAME).exists()
    offline_release.verify_global_request(
        directory=request,
        expected=candidate_release,
        public_key=key,
        minisign=Path("/usr/bin/minisign"),
    )
    with pytest.raises(offline_release.OfflineReleaseError, match="expected runs"):
        offline_release.verify_global_request(
            directory=request,
            expected=wrong_digest,
            public_key=key,
            minisign=Path("/usr/bin/minisign"),
        )

    candidate = tmp_path / "candidate"
    offline_release.finalize_candidate(
        request_directory=request,
        output_directory=candidate,
        global_signature=global_signature,
        expected=candidate_release,
        public_key=key,
        minisign=Path("/usr/bin/minisign"),
    )
    assert (candidate / offline_release.GLOBAL_SIGNATURE_NAME).read_bytes() == (
        global_signature.read_bytes()
    )
    offline_release.verify_candidate(
        directory=candidate,
        expected=candidate_release,
        public_key=key,
        minisign=Path("/usr/bin/minisign"),
    )

    wheel = next(candidate.glob("*.whl"))
    wheel.write_bytes(wheel.read_bytes() + b"drift")
    with pytest.raises(offline_release.OfflineReleaseError, match="checksum mismatch"):
        offline_release.verify_candidate(
            directory=candidate,
            expected=candidate_release,
            public_key=key,
            minisign=Path("/usr/bin/minisign"),
        )


def test_global_request_rejects_unsigned_extra_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _prepare(tmp_path)
    candidate_release = _candidate(inputs)
    key = _public_key(tmp_path / "release.pub")
    signature = tmp_path / "inner.minisig"
    signature.write_text("signature fixture\n", encoding="ascii")
    monkeypatch.setattr(offline_release, "_run_minisign", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        build_install_bundle,
        "_verify_manifest_signature",
        lambda *args, **kwargs: None,
    )
    request = tmp_path / "request"
    offline_release.stage_global_request(
        input_directory=inputs,
        output_directory=request,
        inner_signature=signature,
        public_key=key,
        minisign=Path("/usr/bin/minisign"),
        expected=candidate_release,
    )
    (request / "unexpected").write_bytes(b"not signed")

    with pytest.raises(offline_release.OfflineReleaseError, match="exact release asset"):
        offline_release.verify_global_request(
            directory=request,
            expected=candidate_release,
            public_key=key,
            minisign=Path("/usr/bin/minisign"),
        )


def test_local_signing_derives_the_public_key_and_never_weakens_key_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    secret = keys / "release.key"
    secret.write_text("test secret key material\n", encoding="ascii")
    secret.chmod(0o600)
    public = _public_key(keys / "release.pub")
    manifest = tmp_path / offline_release.INSTALL_MANIFEST_NAME
    manifest.write_text(f"{'0' * 64}  artifact\n", encoding="ascii")
    signature = tmp_path / "install.minisig"
    encoded = tmp_path / "install.minisig.b64"

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        if "-R" in command:
            Path(command[command.index("-p") + 1]).write_bytes(public.read_bytes())
        elif "-S" in command:
            Path(command[command.index("-x") + 1]).write_text(
                "untrusted comment: fixture\nsignature fixture\n",
                encoding="ascii",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(offline_release.subprocess, "run", fake_run)
    monkeypatch.setattr(offline_release, "_run_minisign", lambda *args, **kwargs: None)
    offline_release.sign_request(
        kind="install",
        tag=f"v{VERSION}",
        manifest=manifest,
        signature=signature,
        base64_output=encoded,
        signing_key=secret,
        public_key=public,
        minisign=Path("/opt/homebrew/bin/minisign"),
    )

    assert base64.b64decode(encoded.read_text(encoding="ascii").strip(), validate=True) == (
        signature.read_bytes()
    )
    global_manifest = tmp_path / offline_release.GLOBAL_MANIFEST_NAME
    global_manifest.write_text(f"{'0' * 64}  artifact\n", encoding="ascii")
    secret.chmod(0o640)
    with pytest.raises(offline_release.OfflineReleaseError, match="mode 0600"):
        offline_release.sign_request(
            kind="release",
            tag=f"v{VERSION}",
            manifest=global_manifest,
            signature=tmp_path / "unsafe.minisig",
            base64_output=tmp_path / "unsafe.b64",
            signing_key=secret,
            public_key=public,
            minisign=Path("/opt/homebrew/bin/minisign"),
        )


def test_verified_snapshot_prevents_live_request_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _prepare(tmp_path / "fixture")
    candidate_release = _candidate(request)
    signed_source = replace(
        SOURCE,
        input_manifest_sha256=candidate_release.input_manifest_sha256,
    )
    key = _public_key(tmp_path / "release.pub")
    key_directory = tmp_path / "keys"
    key_directory.mkdir(mode=0o700)
    secret = key_directory / "release.key"
    secret.write_text("fixture\n", encoding="ascii")
    secret.chmod(0o600)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    signature = outputs / "request.minisig"
    encoded = outputs / "request.minisig.b64"
    original_manifest = (request / offline_release.INSTALL_MANIFEST_NAME).read_bytes()
    copied_snapshot = offline_release._snapshot_tree
    signed_payload: list[bytes] = []

    def snapshot_then_swap(source: Path, destination: Path, label: str) -> Path:
        snapshot = copied_snapshot(source, destination, label)
        (source / offline_release.INSTALL_MANIFEST_NAME).write_bytes(b"swapped after snapshot\n")
        return snapshot

    def fake_sign_request(**kwargs: Any) -> tuple[Path, Path]:
        manifest = kwargs["manifest"]
        output_signature = kwargs["signature"]
        output_base64 = kwargs["base64_output"]
        assert isinstance(manifest, Path)
        assert isinstance(output_signature, Path)
        assert isinstance(output_base64, Path)
        signed_payload.append(manifest.read_bytes())
        output_signature.write_bytes(b"public signature fixture\n")
        output_base64.write_bytes(b"cHVibGljIHNpZ25hdHVyZSBmaXh0dXJlCg==\n")
        return output_signature, output_base64

    monkeypatch.setattr(offline_release, "_snapshot_tree", snapshot_then_swap)
    monkeypatch.setattr(offline_release, "sign_request", fake_sign_request)
    monkeypatch.setattr(offline_release, "_run_minisign", lambda *args, **kwargs: None)

    with pytest.raises(offline_release.OfflineReleaseError, match="changed while"):
        offline_release.verify_and_sign_request(
            kind="install",
            request_directory=request,
            signature=signature,
            base64_output=encoded,
            signing_key=secret,
            public_key=key,
            minisign=Path("/usr/bin/minisign"),
            expected=signed_source,
        )

    assert signed_payload == [original_manifest]
    assert not signature.exists()
    assert not encoded.exists()


def test_local_signing_round_trip_with_installed_minisign(tmp_path: Path) -> None:
    executable = shutil.which("minisign")
    if executable is None:
        pytest.skip("Minisign is not installed")

    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    secret = keys / "release.key"
    public = keys / "release.pub"
    subprocess.run(
        [executable, "-G", "-W", "-s", str(secret), "-p", str(public)],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    secret.chmod(0o600)
    public.chmod(0o644)
    request = _prepare(tmp_path / "fixture")
    candidate_release = _candidate(request)
    signed_source = replace(
        SOURCE,
        input_manifest_sha256=candidate_release.input_manifest_sha256,
    )
    manifest = request / offline_release.INSTALL_MANIFEST_NAME
    outputs = tmp_path / "signatures"
    outputs.mkdir()
    signature = outputs / "INSTALL-SHA256SUMS.minisig"
    encoded = outputs / "INSTALL-SHA256SUMS.minisig.b64"

    with pytest.raises(offline_release.OfflineReleaseError, match="attested input manifest"):
        offline_release.verify_and_sign_request(
            kind="install",
            request_directory=request,
            signature=outputs / "wrong.minisig",
            base64_output=outputs / "wrong.minisig.b64",
            signing_key=secret,
            public_key=public,
            minisign=Path(executable),
            expected=replace(signed_source, input_manifest_sha256="0" * 64),
        )

    offline_release.verify_and_sign_request(
        kind="install",
        request_directory=request,
        signature=signature,
        base64_output=encoded,
        signing_key=secret,
        public_key=public,
        minisign=Path(executable),
        expected=signed_source,
    )

    subprocess.run(
        [
            executable,
            "-V",
            "-m",
            str(manifest),
            "-x",
            str(signature),
            "-p",
            str(public),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    encoded_payload = encoded.read_text(encoding="ascii")
    assert encoded_payload.endswith("\n")
    assert base64.b64decode(encoded_payload.strip(), validate=True) == signature.read_bytes()

    global_request = tmp_path / "global-signing-request"
    offline_release.stage_global_request(
        input_directory=request,
        output_directory=global_request,
        inner_signature=signature,
        public_key=public,
        minisign=Path(executable),
        expected=candidate_release,
    )
    global_signature = outputs / offline_release.GLOBAL_SIGNATURE_NAME
    global_encoded = outputs / f"{offline_release.GLOBAL_SIGNATURE_NAME}.b64"
    offline_release.verify_and_sign_request(
        kind="release",
        request_directory=global_request,
        signature=global_signature,
        base64_output=global_encoded,
        signing_key=secret,
        public_key=public,
        minisign=Path(executable),
        expected=candidate_release,
    )
    candidate = tmp_path / "signed-candidate"
    offline_release.finalize_candidate(
        request_directory=global_request,
        output_directory=candidate,
        global_signature=global_signature,
        expected=candidate_release,
        public_key=public,
        minisign=Path(executable),
    )

    assert offline_release.verify_candidate(
        directory=candidate,
        expected=candidate_release,
        public_key=public,
        minisign=Path(executable),
    )


def test_offline_release_cli_exposes_only_public_signature_handoffs() -> None:
    parser = offline_release._parser()
    help_text = parser.format_help()
    sign_help = parser._subparsers._group_actions[0].choices["sign-request"].format_help()

    assert "sign-request" in help_text
    assert "finalize-candidate" in help_text
    assert "secret" not in help_text.lower()
    assert "--request-directory" in sign_help
    assert "--manifest" not in sign_help
