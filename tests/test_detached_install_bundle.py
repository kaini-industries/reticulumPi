"""Focused tests for detached install-bundle manifest signing."""

from __future__ import annotations

import io
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from tools import build_install_bundle


VERSION = "3.2.1"
SOURCE_DATE_EPOCH = 1_700_000_000
SIGNATURE = b"untrusted comment: fixture\nRWQfixture\ntrusted comment: fixture\nRWQtrusted\n"


def _tar_bytes(archive: tarfile.TarFile, name: str, payload: bytes, mode: int = 0o644) -> None:
    member = tarfile.TarInfo(name)
    member.mode = mode
    member.size = len(payload)
    archive.addfile(member, io.BytesIO(payload))


def _release_inputs(tmp_path: Path) -> tuple[Path, Path]:
    metadata = (f"Metadata-Version: 2.4\nName: reticulumpi\nVersion: {VERSION}\n").encode()
    wheel = tmp_path / f"reticulumpi-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr(f"reticulumpi-{VERSION}.dist-info/METADATA", metadata)
        archive.writestr("reticulumpi/__init__.py", b"")

    root = f"reticulumpi-{VERSION}"
    sources = {
        "PKG-INFO": metadata,
        "pyproject.toml": b"[project]\nname='reticulumpi'\ndynamic=['version']\n",
        "README.md": b"# ReticulumPi\n",
        "src/reticulumpi/__init__.py": b"",
        "src/reticulumpi/SHA256SUMS": b"nested source data\n",
        "systemd/reticulumpi.service": b"[Service]\nExecStart=/usr/bin/reticulumpi\n",
        "config/config.example.yaml": b"reticulumpi: {}\n",
        "scripts/bootstrap.sh": b"#!/bin/sh\nexit 0\n",
        "constraints/production-universal-core.txt": b"fixture\n",
        "constraints/production-universal-dashboard-nomadnet.txt": b"fixture\n",
        "constraints/production-universal-all-features.txt": b"fixture\n",
    }
    sdist = tmp_path / f"reticulumpi-{VERSION}.tar.gz"
    with tarfile.open(sdist, mode="w:gz") as archive:
        for name, payload in sources.items():
            _tar_bytes(archive, f"{root}/{name}", payload, 0o755 if name.endswith(".sh") else 0o644)
    return sdist, wheel


def _output(tmp_path: Path, parent: str) -> Path:
    return tmp_path / parent / f"reticulumpi-install-arm64-{VERSION}.tar.gz"


def _key(tmp_path: Path) -> Path:
    key = tmp_path / "release.key"
    key.write_text("fixture secret key\n", encoding="ascii")
    key.chmod(0o600)
    return key


def _fake_sign(
    manifest: Path,
    signature: Path,
    signing_key: Path,
    minisign: Path,
) -> None:
    del manifest, signing_key, minisign
    signature.write_bytes(SIGNATURE)


def test_rendered_manifest_is_deterministic_and_matches_the_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdist, wheel = _release_inputs(tmp_path)
    first = build_install_bundle.render_install_manifest(
        sdist=sdist,
        wheel=wheel,
        version=VERSION,
    )
    second = build_install_bundle.render_install_manifest(
        sdist=sdist,
        wheel=wheel,
        version=VERSION,
    )
    assert first == second
    assert b"  bundle.json\n" in first
    assert f"  {wheel.name}\n".encode() in first
    assert b"  src/reticulumpi/SHA256SUMS\n" in first
    assert b"  SHA256SUMS\n" not in first
    assert b"  SHA256SUMS.minisig\n" not in first

    monkeypatch.setattr(build_install_bundle, "sign_manifest", _fake_sign)
    output = _output(tmp_path, "signed")
    build_install_bundle.build_install_bundle(
        sdist=sdist,
        wheel=wheel,
        output=output,
        version=VERSION,
        signing_key=_key(tmp_path),
        minisign=Path("/explicit/minisign"),
        source_date_epoch=SOURCE_DATE_EPOCH,
    )
    with tarfile.open(output, mode="r:gz") as archive:
        manifest = archive.extractfile(f"reticulumpi-{VERSION}/SHA256SUMS")
        assert manifest is not None
        assert manifest.read() == first


def test_detached_signature_is_verified_copied_and_reconstructs_exact_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdist, wheel = _release_inputs(tmp_path)
    monkeypatch.setattr(build_install_bundle, "sign_manifest", _fake_sign)
    signed_output = _output(tmp_path, "signed")
    build_install_bundle.build_install_bundle(
        sdist=sdist,
        wheel=wheel,
        output=signed_output,
        version=VERSION,
        signing_key=_key(tmp_path),
        minisign=Path("/explicit/minisign"),
        source_date_epoch=SOURCE_DATE_EPOCH,
    )

    detached = tmp_path / "inner.minisig"
    detached.write_bytes(SIGNATURE)
    public_key = tmp_path / "release.pub"
    public_key.write_bytes(b"untrusted comment: fixture public key\nRWQpublic\n")
    invocation: dict[str, object] = {}

    def fake_verify(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        invocation["command"] = command
        invocation.update(kwargs)
        invocation["manifest"] = Path(command[3]).read_bytes()
        invocation["signature"] = Path(command[5]).read_bytes()
        invocation["public_key"] = Path(command[7]).read_bytes()
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(build_install_bundle.subprocess, "run", fake_verify)
    detached_output = _output(tmp_path, "detached")
    build_install_bundle.build_install_bundle(
        sdist=sdist,
        wheel=wheel,
        output=detached_output,
        version=VERSION,
        manifest_signature=detached,
        public_key=public_key,
        minisign=Path("/explicit/minisign"),
        source_date_epoch=SOURCE_DATE_EPOCH,
    )

    command = invocation["command"]
    assert isinstance(command, list)
    assert command[0:3] == ["/explicit/minisign", "-V", "-m"]
    assert command[4] == "-x"
    assert command[6] == "-p"
    assert invocation["signature"] == SIGNATURE
    assert invocation["public_key"] == public_key.read_bytes()
    assert invocation["stdin"] is subprocess.DEVNULL
    assert invocation["stdout"] is subprocess.DEVNULL
    assert invocation["env"] == {"LANG": "C", "PATH": "/usr/bin:/bin"}
    assert detached_output.read_bytes() == signed_output.read_bytes()
    with tarfile.open(detached_output, mode="r:gz") as archive:
        signature = archive.extractfile(f"reticulumpi-{VERSION}/SHA256SUMS.minisig")
        assert signature is not None
        assert signature.read() == SIGNATURE


@pytest.mark.parametrize(
    ("signing_key", "manifest_signature", "public_key", "message"),
    [
        (None, None, None, "exactly one"),
        ("key", "signature", "public", "exactly one"),
        (None, "signature", None, "public key is required"),
        ("key", None, "public", "only valid with a detached signature"),
    ],
)
def test_signing_modes_fail_closed(
    tmp_path: Path,
    signing_key: str | None,
    manifest_signature: str | None,
    public_key: str | None,
    message: str,
) -> None:
    sdist, wheel = _release_inputs(tmp_path)
    paths = {
        "key": _key(tmp_path),
        "signature": tmp_path / "inner.minisig",
        "public": tmp_path / "release.pub",
    }
    paths["signature"].write_bytes(SIGNATURE)
    paths["public"].write_text("fixture public key\n", encoding="ascii")
    with pytest.raises(build_install_bundle.InstallBundleError, match=message):
        build_install_bundle.build_install_bundle(
            sdist=sdist,
            wheel=wheel,
            output=_output(tmp_path, "invalid"),
            version=VERSION,
            signing_key=paths[signing_key] if signing_key else None,
            manifest_signature=paths[manifest_signature] if manifest_signature else None,
            public_key=paths[public_key] if public_key else None,
            minisign=Path("/explicit/minisign"),
            source_date_epoch=SOURCE_DATE_EPOCH,
        )


def test_detached_signature_failure_never_creates_an_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdist, wheel = _release_inputs(tmp_path)
    signature = tmp_path / "inner.minisig"
    signature.write_bytes(SIGNATURE)
    public_key = tmp_path / "release.pub"
    public_key.write_text("fixture public key\n", encoding="ascii")

    def reject(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        raise subprocess.CalledProcessError(1, command, stderr="Signature verification failed")

    monkeypatch.setattr(build_install_bundle.subprocess, "run", reject)
    output = _output(tmp_path, "rejected")
    with pytest.raises(
        build_install_bundle.InstallBundleError,
        match="Minisign signature verification failed",
    ):
        build_install_bundle.build_install_bundle(
            sdist=sdist,
            wheel=wheel,
            output=output,
            version=VERSION,
            manifest_signature=signature,
            public_key=public_key,
            minisign=Path("/explicit/minisign"),
            source_date_epoch=SOURCE_DATE_EPOCH,
        )
    assert not output.exists()


def test_detached_signature_symlink_is_rejected_before_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdist, wheel = _release_inputs(tmp_path)
    signature_target = tmp_path / "signature-target"
    signature_target.write_bytes(SIGNATURE)
    signature = tmp_path / "inner.minisig"
    signature.symlink_to(signature_target)
    public_key = tmp_path / "release.pub"
    public_key.write_text("fixture public key\n", encoding="ascii")

    def unexpected(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("Minisign must not run for an unsafe signature path")

    monkeypatch.setattr(build_install_bundle.subprocess, "run", unexpected)
    output = _output(tmp_path, "unsafe-signature")
    with pytest.raises(build_install_bundle.InstallBundleError, match="safely copy"):
        build_install_bundle.build_install_bundle(
            sdist=sdist,
            wheel=wheel,
            output=output,
            version=VERSION,
            manifest_signature=signature,
            public_key=public_key,
            minisign=Path("/explicit/minisign"),
            source_date_epoch=SOURCE_DATE_EPOCH,
        )
    assert not output.exists()


def test_signing_key_requires_exact_mode_0600(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "SHA256SUMS"
    manifest.write_text("0" * 64 + "  fixture\n", encoding="ascii")
    key = _key(tmp_path)
    key.chmod(0o400)

    def unexpected(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("Minisign must not run for an unsafe key mode")

    monkeypatch.setattr(build_install_bundle.subprocess, "run", unexpected)
    with pytest.raises(build_install_bundle.InstallBundleError, match="mode 0600"):
        build_install_bundle.sign_manifest(
            manifest,
            tmp_path / "SHA256SUMS.minisig",
            key,
            Path("/explicit/minisign"),
        )


def test_cli_accepts_only_one_detached_signing_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    output = _output(tmp_path, "cli")

    def fake_build(**kwargs: object) -> Path:
        captured.update(kwargs)
        return output

    monkeypatch.setattr(build_install_bundle, "build_install_bundle", fake_build)
    result = build_install_bundle.main(
        [
            "--sdist",
            "source.tar.gz",
            "--wheel",
            "package.whl",
            "--output",
            str(output),
            "--version",
            VERSION,
            "--manifest-signature",
            "inner.minisig",
            "--public-key",
            "release.pub",
            "--minisign",
            "/explicit/minisign",
            "--source-date-epoch",
            str(SOURCE_DATE_EPOCH),
        ]
    )
    assert result == 0
    assert captured["signing_key"] is None
    assert captured["manifest_signature"] == Path("inner.minisig")
    assert captured["public_key"] == Path("release.pub")
    assert captured["minisign"] == Path("/explicit/minisign")

    with pytest.raises(SystemExit):
        build_install_bundle.main(
            [
                "--sdist",
                "source.tar.gz",
                "--wheel",
                "package.whl",
                "--output",
                str(output),
                "--version",
                VERSION,
                "--signing-key",
                "release.key",
                "--manifest-signature",
                "inner.minisig",
                "--source-date-epoch",
                str(SOURCE_DATE_EPOCH),
            ]
        )
