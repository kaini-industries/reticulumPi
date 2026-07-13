#!/usr/bin/env python3
"""Prepare and verify ReticulumPi's two-stage offline release signature envelope."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

if __package__:
    from tools import build_install_bundle, prepare_release_assets
    from tools.build_admin_deb import SUPPORTED_PLATFORM_PYTHON, admin_deb_filename
    from tools.verify_release_tag import verify_artifacts, version_from_tag
else:
    import build_install_bundle
    import prepare_release_assets
    from build_admin_deb import SUPPORTED_PLATFORM_PYTHON, admin_deb_filename
    from verify_release_tag import verify_artifacts, version_from_tag


INPUT_PROVENANCE_KIND = "reticulumpi-release-inputs"
CANDIDATE_PROVENANCE_KIND = "reticulumpi-release-candidate"
INPUT_MANIFEST_NAME = "RELEASE-INPUTS.SHA256SUMS"
INSTALL_MANIFEST_NAME = "INSTALL-SHA256SUMS"
PROVENANCE_NAME = prepare_release_assets.RELEASE_PROVENANCE_NAME
GLOBAL_MANIFEST_NAME = "SHA256SUMS"
GLOBAL_SIGNATURE_NAME = "SHA256SUMS.minisig"
SOURCE_WORKFLOW = ".github/workflows/ci.yml"
CANDIDATE_WORKFLOW = ".github/workflows/release-candidate.yml"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_SIGNATURE_BYTES = build_install_bundle.MAX_MINISIGN_FILE_BYTES
COMMIT = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TREE_MANIFEST_LINE = re.compile(r"^(?P<digest>[0-9a-f]{64})  (?P<name>[^\\\r\n]+)\n$")


class OfflineReleaseError(ValueError):
    """Raised when an offline signing request or candidate fails closed."""


@dataclass(frozen=True, slots=True)
class ExpectedRelease:
    """Identity values that must match signed release provenance."""

    tag: str
    repository: str
    commit: str
    source_run_id: int
    source_run_attempt: int
    input_manifest_sha256: str | None = None
    candidate_run_id: int | None = None
    candidate_run_attempt: int | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_identity(expected: ExpectedRelease) -> str:
    version = version_from_tag(expected.tag)
    if REPOSITORY.fullmatch(expected.repository) is None:
        raise OfflineReleaseError("repository must be OWNER/NAME")
    if COMMIT.fullmatch(expected.commit) is None:
        raise OfflineReleaseError("commit must be a lowercase 40-character Git object ID")
    for label, value in (
        ("source run ID", expected.source_run_id),
        ("source run attempt", expected.source_run_attempt),
    ):
        if value <= 0:
            raise OfflineReleaseError(f"{label} must be positive")
    if (expected.candidate_run_id is None) != (expected.candidate_run_attempt is None):
        raise OfflineReleaseError("candidate run ID and attempt must be supplied together")
    if expected.candidate_run_id is not None and expected.input_manifest_sha256 is None:
        raise OfflineReleaseError("candidate identity requires the input manifest digest")
    for label, value in (
        ("candidate run ID", expected.candidate_run_id),
        ("candidate run attempt", expected.candidate_run_attempt),
    ):
        if value is not None and value <= 0:
            raise OfflineReleaseError(f"{label} must be positive")
    if (
        expected.input_manifest_sha256 is not None
        and re.fullmatch(r"[0-9a-f]{64}", expected.input_manifest_sha256) is None
    ):
        raise OfflineReleaseError("input manifest digest must be lowercase SHA-256")
    return version


def _regular_file(path: Path, label: str, *, maximum: int | None = None) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise OfflineReleaseError(f"{label} is unavailable: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise OfflineReleaseError(f"{label} must be a regular file: {path}")
    if maximum is not None and metadata.st_size > maximum:
        raise OfflineReleaseError(f"{label} exceeds the size limit")
    return path


def _safe_tree_files(root: Path, label: str) -> dict[str, Path]:
    try:
        metadata = root.lstat()
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise OfflineReleaseError(f"{label} directory is unavailable: {root}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise OfflineReleaseError(f"{label} must be a directory: {root}")
    files: dict[str, Path] = {}
    directories: set[str] = set()
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not (
            stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
        ):
            raise OfflineReleaseError(f"{label} contains an unsafe entry: {path}")
        try:
            relative = path.relative_to(root).as_posix()
            path.resolve(strict=True).relative_to(resolved_root)
        except (OSError, ValueError) as exc:
            raise OfflineReleaseError(f"{label} entry escapes its root: {path}") from exc
        if stat.S_ISREG(metadata.st_mode):
            if relative in files:
                raise OfflineReleaseError(f"{label} repeats a path: {relative}")
            files[relative] = path
        else:
            directories.add(relative)
    implied_directories = {
        parent.as_posix()
        for name in files
        for parent in PurePosixPath(name).parents
        if parent != PurePosixPath(".")
    }
    unsigned_directories = directories - implied_directories
    if unsigned_directories:
        rendered = ", ".join(sorted(unsigned_directories))
        raise OfflineReleaseError(f"{label} contains unsigned empty directories: {rendered}")
    return files


def _empty_output_directory(path: Path, label: str) -> Path:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir() or any(path.iterdir()):
            raise OfflineReleaseError(f"{label} must not already contain files: {path}")
    else:
        path.mkdir(mode=0o755, parents=True)
    return path


def _canonical_json(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _write_exclusive(path: Path, payload: bytes, *, mode: int = 0o644) -> Path:
    if path.exists() or path.is_symlink():
        raise OfflineReleaseError(f"refusing to replace existing output: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
    except OSError as exc:
        raise OfflineReleaseError(f"cannot write release output: {path}") from exc
    return path


def _copy_exclusive(source: Path, destination: Path) -> Path:
    if destination.exists() or destination.is_symlink():
        raise OfflineReleaseError(f"refusing to replace existing output: {destination}")
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    destination_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    source_descriptor: int | None = None
    destination_descriptor: int | None = None
    destination_created = False
    copied = False
    try:
        source_descriptor = os.open(source, source_flags)
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OfflineReleaseError(f"release input must be a regular file: {source}")
        destination_descriptor = os.open(destination, destination_flags, 0o644)
        destination_created = True
        os.fchmod(destination_descriptor, 0o644)
        source_digest = hashlib.sha256()
        total = 0
        while block := os.read(source_descriptor, 1024 * 1024):
            source_digest.update(block)
            total += len(block)
            remaining = memoryview(block)
            while remaining:
                written = os.write(destination_descriptor, remaining)
                if written <= 0:
                    raise OfflineReleaseError(f"short write while copying release input: {source}")
                remaining = remaining[written:]
        after = os.fstat(source_descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or total != before.st_size:
            raise OfflineReleaseError(f"release input changed while being copied: {source}")
        os.fsync(destination_descriptor)
        os.lseek(destination_descriptor, 0, os.SEEK_SET)
        destination_digest = hashlib.sha256()
        while block := os.read(destination_descriptor, 1024 * 1024):
            destination_digest.update(block)
        if destination_digest.digest() != source_digest.digest():
            raise OfflineReleaseError(f"release input copy verification failed: {source}")
        copied = True
    except OfflineReleaseError:
        raise
    except OSError as exc:
        raise OfflineReleaseError(f"cannot copy release input: {source}") from exc
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        if source_descriptor is not None:
            os.close(source_descriptor)
        if destination_created and not copied:
            destination.unlink(missing_ok=True)
    return destination


def _snapshot_tree(source: Path, destination: Path, label: str) -> Path:
    """Copy one untrusted request into a private tree that can be verified and signed."""

    files = _safe_tree_files(source, label)
    if destination.exists() or destination.is_symlink():
        raise OfflineReleaseError(f"snapshot destination already exists: {destination}")
    destination.mkdir(mode=0o700)
    try:
        for name, path in sorted(files.items()):
            relative = PurePosixPath(name)
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _copy_exclusive(path, target)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination


def _parse_canonical_json(path: Path, label: str) -> dict[str, Any]:
    _regular_file(path, label, maximum=prepare_release_assets.MAX_RELEASE_PROVENANCE_BYTES)
    try:
        payload = path.read_bytes()
        document = json.loads(
            payload.decode("ascii"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant {value}")
            ),
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise OfflineReleaseError(f"{label} is not valid canonical JSON: {exc}") from exc
    if not isinstance(document, dict) or payload != _canonical_json(document):
        raise OfflineReleaseError(f"{label} is not canonical sorted compact JSON")
    return document


def _safe_manifest_name(name: str, *, nested: bool) -> str:
    relative = PurePosixPath(name)
    if (
        not name
        or name != relative.as_posix()
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in name
        or (not nested and len(relative.parts) != 1)
    ):
        raise OfflineReleaseError(f"unsafe manifest path: {name!r}")
    return name


def _read_manifest(path: Path, *, nested: bool) -> dict[str, str]:
    _regular_file(path, "SHA-256 manifest", maximum=MAX_MANIFEST_BYTES)
    try:
        payload = path.read_bytes()
        text = payload.decode("ascii")
    except (OSError, UnicodeError) as exc:
        raise OfflineReleaseError(f"cannot read SHA-256 manifest: {path}") from exc
    if not payload or not text.endswith("\n"):
        raise OfflineReleaseError("SHA-256 manifest must be non-empty and newline terminated")
    entries: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        match = TREE_MANIFEST_LINE.fullmatch(line)
        if match is None:
            raise OfflineReleaseError(f"invalid SHA-256 manifest line {number}")
        name = _safe_manifest_name(match.group("name"), nested=nested)
        if name in entries:
            raise OfflineReleaseError(f"duplicate SHA-256 manifest path: {name}")
        entries[name] = match.group("digest")
    if list(entries) != sorted(entries):
        raise OfflineReleaseError("SHA-256 manifest paths must be sorted")
    return entries


def _write_tree_manifest(root: Path, output: Path) -> Path:
    files = _safe_tree_files(root, "release input")
    relative_output = output.relative_to(root).as_posix()
    files.pop(relative_output, None)
    payload = "".join(f"{_sha256(path)}  {name}\n" for name, path in sorted(files.items()))
    if not payload:
        raise OfflineReleaseError("release input contains no files")
    return _write_exclusive(output, payload.encode("ascii"))


def _verify_tree_manifest(root: Path, manifest: Path) -> dict[str, Path]:
    files = _safe_tree_files(root, "release input")
    relative_manifest = manifest.relative_to(root).as_posix()
    entries = _read_manifest(manifest, nested=True)
    actual = {name: path for name, path in files.items() if name != relative_manifest}
    if set(entries) != set(actual):
        raise OfflineReleaseError("release input manifest is not an exact tree manifest")
    for name, expected_digest in entries.items():
        if _sha256(actual[name]) != expected_digest:
            raise OfflineReleaseError(f"release input checksum mismatch: {name}")
    return actual


def _input_provenance(expected: ExpectedRelease, *, source_date_epoch: int) -> dict[str, Any]:
    _validate_identity(expected)
    if expected.candidate_run_id is not None:
        raise OfflineReleaseError("input provenance cannot contain a candidate run")
    if source_date_epoch <= 0:
        raise OfflineReleaseError("source date epoch must be positive")
    return {
        "artifact": f"release-signing-input-{expected.tag}",
        "commit": expected.commit,
        "kind": INPUT_PROVENANCE_KIND,
        "repository": expected.repository,
        "schema": 1,
        "source_date_epoch": source_date_epoch,
        "source_run_attempt": expected.source_run_attempt,
        "source_run_id": expected.source_run_id,
        "source_workflow": SOURCE_WORKFLOW,
        "tag": expected.tag,
    }


def _require_input_provenance(
    document: dict[str, Any], expected: ExpectedRelease
) -> dict[str, Any]:
    epoch = document.get("source_date_epoch")
    if type(epoch) is not int or epoch <= 0:
        raise OfflineReleaseError("release input provenance has an invalid source date epoch")
    required = _input_provenance(expected, source_date_epoch=epoch)
    if document != required:
        raise OfflineReleaseError("release input provenance does not match the expected run")
    return document


def _candidate_provenance(
    expected: ExpectedRelease,
    *,
    source_date_epoch: int,
    public_key_sha256: str,
) -> dict[str, Any]:
    _validate_identity(expected)
    if (
        expected.candidate_run_id is None
        or expected.candidate_run_attempt is None
        or expected.input_manifest_sha256 is None
    ):
        raise OfflineReleaseError("candidate provenance requires the candidate run")
    return {
        "candidate_run_attempt": expected.candidate_run_attempt,
        "candidate_run_id": expected.candidate_run_id,
        "candidate_workflow": CANDIDATE_WORKFLOW,
        "commit": expected.commit,
        "global_request_artifact": f"global-signing-request-{expected.tag}",
        "input_artifact": f"release-signing-input-{expected.tag}",
        "input_manifest_sha256": expected.input_manifest_sha256,
        "kind": CANDIDATE_PROVENANCE_KIND,
        "minisign_public_key_sha256": public_key_sha256,
        "repository": expected.repository,
        "schema": 1,
        "source_date_epoch": source_date_epoch,
        "source_run_attempt": expected.source_run_attempt,
        "source_run_id": expected.source_run_id,
        "source_workflow": SOURCE_WORKFLOW,
        "tag": expected.tag,
    }


def _require_candidate_provenance(
    document: dict[str, Any], expected: ExpectedRelease, public_key: Path
) -> dict[str, Any]:
    epoch = document.get("source_date_epoch")
    if type(epoch) is not int or epoch <= 0:
        raise OfflineReleaseError("candidate provenance has an invalid source date epoch")
    required = _candidate_provenance(
        expected,
        source_date_epoch=epoch,
        public_key_sha256=_sha256(_regular_file(public_key, "Minisign public key")),
    )
    if document != required:
        raise OfflineReleaseError("candidate provenance does not match the expected runs")
    return document


def _raw_input_paths(input_directory: Path) -> dict[str, Path]:
    _safe_tree_files(input_directory, "release input")
    expected_names = {"python", "images", "recovery-admin"}
    actual_names = {
        path.name
        for path in input_directory.iterdir()
        if path.name not in {PROVENANCE_NAME, INSTALL_MANIFEST_NAME, INPUT_MANIFEST_NAME}
    }
    if actual_names != expected_names:
        raise OfflineReleaseError("release input has unexpected or missing top-level paths")
    images = input_directory / "images"
    image_names = {path.name for path in images.iterdir()} if images.is_dir() else set()
    if image_names != set(prepare_release_assets.ARCHITECTURES):
        raise OfflineReleaseError("release input has unexpected or missing image architectures")
    return {
        "python": input_directory / "python",
        "amd64": images / "amd64",
        "arm64": images / "arm64",
        "recovery-admin": input_directory / "recovery-admin",
    }


def prepare_inputs(
    *,
    input_directory: Path,
    expected: ExpectedRelease,
    source_date_epoch: int,
) -> list[Path]:
    """Validate gated CI outputs and create the exact install signing request."""

    paths = _raw_input_paths(input_directory)
    provenance = input_directory / PROVENANCE_NAME
    _write_exclusive(
        provenance,
        _canonical_json(_input_provenance(expected, source_date_epoch=source_date_epoch)),
    )
    inputs = prepare_release_assets.validate_release_inputs(
        tag=expected.tag,
        python_directory=paths["python"],
        image_directories={"amd64": paths["amd64"], "arm64": paths["arm64"]},
        recovery_admin_directory=paths["recovery-admin"],
        provenance=provenance,
    )
    install_manifest = input_directory / INSTALL_MANIFEST_NAME
    _write_exclusive(
        install_manifest,
        build_install_bundle.render_install_manifest(
            sdist=inputs.sdist,
            wheel=inputs.wheel,
            version=inputs.version,
        ),
    )
    input_manifest = _write_tree_manifest(
        input_directory,
        input_directory / INPUT_MANIFEST_NAME,
    )
    verify_inputs(input_directory=input_directory, expected=expected)
    return [provenance, install_manifest, input_manifest]


def verify_inputs(
    *, input_directory: Path, expected: ExpectedRelease
) -> prepare_release_assets.ValidatedReleaseInputs:
    """Verify one exact CI release-input artifact without trusting its archive wrapper."""

    paths = _raw_input_paths(input_directory)
    provenance = input_directory / PROVENANCE_NAME
    _require_input_provenance(
        _parse_canonical_json(provenance, "release input provenance"),
        expected,
    )
    _verify_tree_manifest(input_directory, input_directory / INPUT_MANIFEST_NAME)
    inputs = prepare_release_assets.validate_release_inputs(
        tag=expected.tag,
        python_directory=paths["python"],
        image_directories={"amd64": paths["amd64"], "arm64": paths["arm64"]},
        recovery_admin_directory=paths["recovery-admin"],
        provenance=provenance,
    )
    rendered = build_install_bundle.render_install_manifest(
        sdist=inputs.sdist,
        wheel=inputs.wheel,
        version=inputs.version,
    )
    if (input_directory / INSTALL_MANIFEST_NAME).read_bytes() != rendered:
        raise OfflineReleaseError("install signing request does not match the gated inputs")
    return inputs


def _run_minisign(
    minisign: Path,
    manifest: Path,
    signature: Path,
    public_key: Path,
) -> None:
    _regular_file(manifest, "signed manifest", maximum=MAX_MANIFEST_BYTES)
    _regular_file(signature, "Minisign signature", maximum=MAX_SIGNATURE_BYTES)
    _regular_file(public_key, "Minisign public key", maximum=MAX_SIGNATURE_BYTES)
    command = [
        str(minisign),
        "-V",
        "-m",
        str(manifest),
        "-x",
        str(signature),
        "-p",
        str(public_key),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env={"LANG": "C", "PATH": "/usr/bin:/bin"},
        )
    except OSError as exc:
        raise OfflineReleaseError(f"cannot execute Minisign verifier: {minisign}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "signature verification failed").strip()
        raise OfflineReleaseError(f"Minisign signature verification failed: {detail}") from exc


def _verify_install_signature(
    bundle: Path,
    *,
    version: str,
    public_key: Path,
    minisign: Path,
) -> None:
    root = f"reticulumpi-{version}"
    selected = {
        f"{root}/SHA256SUMS": "SHA256SUMS",
        f"{root}/SHA256SUMS.minisig": "SHA256SUMS.minisig",
    }
    with tempfile.TemporaryDirectory(prefix="reticulumpi-inner-signature-") as raw:
        temporary = Path(raw)
        try:
            with tarfile.open(bundle, mode="r:gz") as archive:
                for member_name, destination_name in selected.items():
                    member = archive.getmember(member_name)
                    maximum = (
                        MAX_SIGNATURE_BYTES
                        if destination_name.endswith(".minisig")
                        else MAX_MANIFEST_BYTES
                    )
                    if not member.isfile() or member.size > maximum:
                        raise OfflineReleaseError("install bundle signature member is unsafe")
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise OfflineReleaseError("cannot read install bundle signature member")
                    payload = stream.read(member.size + 1)
                    if len(payload) != member.size:
                        raise OfflineReleaseError("install bundle signature member changed size")
                    _write_exclusive(temporary / destination_name, payload)
        except (KeyError, OSError, tarfile.TarError) as exc:
            raise OfflineReleaseError("cannot extract install bundle signature") from exc
        _run_minisign(
            minisign,
            temporary / "SHA256SUMS",
            temporary / "SHA256SUMS.minisig",
            public_key,
        )


def _release_files(directory: Path) -> dict[str, Path]:
    files = _safe_tree_files(directory, "release candidate")
    if any(not path.is_file() or path.is_symlink() for path in directory.iterdir()) or any(
        "/" in name for name in files
    ):
        raise OfflineReleaseError("release candidate must contain only top-level files")
    return files


def _verify_release_payload(
    *,
    directory: Path,
    expected: ExpectedRelease,
    public_key: Path,
    minisign: Path,
    require_global_signature: bool,
) -> dict[str, Path]:
    version = _validate_identity(expected)
    files = _release_files(directory)
    manifest = directory / GLOBAL_MANIFEST_NAME
    entries = _read_manifest(manifest, nested=False)
    signature_names = {GLOBAL_SIGNATURE_NAME} if require_global_signature else set()
    if set(files) != set(entries) | {GLOBAL_MANIFEST_NAME} | signature_names:
        raise OfflineReleaseError("global manifest is not an exact release asset manifest")
    for name, digest in entries.items():
        if _sha256(files[name]) != digest:
            raise OfflineReleaseError(f"release asset checksum mismatch: {name}")
    if require_global_signature:
        _run_minisign(
            minisign,
            manifest,
            directory / GLOBAL_SIGNATURE_NAME,
            public_key,
        )

    wheels = [path for name, path in files.items() if name.endswith(".whl")]
    if len(wheels) != 1:
        raise OfflineReleaseError("release candidate must contain exactly one wheel")
    wheel = wheels[0]
    sdist = directory / f"reticulumpi-{version}.tar.gz"
    sbom = directory / f"reticulumpi-{version}.cdx.json"
    install_bundle = directory / f"reticulumpi-install-arm64-{version}.tar.gz"
    provenance = directory / PROVENANCE_NAME
    expected_names = {
        wheel.name,
        sdist.name,
        sbom.name,
        install_bundle.name,
        PROVENANCE_NAME,
        *(
            f"reticulumpi-container-{version}-{arch}.tar.gz"
            for arch in prepare_release_assets.ARCHITECTURES
        ),
    }
    for profile in SUPPORTED_PLATFORM_PYTHON:
        package = admin_deb_filename(version, profile)
        expected_names.update({package, f"{package}.sha256"})
    if set(entries) != expected_names:
        raise OfflineReleaseError("release candidate has unexpected or missing signed assets")

    verify_artifacts([wheel, sdist], version)
    prepare_release_assets._verify_sbom_version(sbom, version)
    for architecture in prepare_release_assets.ARCHITECTURES:
        prepare_release_assets.inspect_image_archive(
            directory / f"reticulumpi-container-{version}-{architecture}.tar.gz",
            architecture,
        )
    prepare_release_assets.inspect_install_bundle(install_bundle, version, wheel)
    _verify_install_signature(
        install_bundle,
        version=version,
        public_key=public_key,
        minisign=minisign,
    )
    for profile in SUPPORTED_PLATFORM_PYTHON:
        package = directory / admin_deb_filename(version, profile)
        sidecar = package.with_name(f"{package.name}.sha256")
        prepare_release_assets._verify_sidecar(package, sidecar)
        prepare_release_assets.inspect_admin_deb(
            package,
            version=version,
            profile=profile,
            wheel=wheel,
        )
    _require_candidate_provenance(
        _parse_canonical_json(provenance, "release candidate provenance"),
        expected,
        public_key,
    )
    return files


def stage_global_request(
    *,
    input_directory: Path,
    output_directory: Path,
    inner_signature: Path,
    public_key: Path,
    minisign: Path,
    expected: ExpectedRelease,
) -> list[Path]:
    """Build the exact install bundle and global manifest from an inner signature."""

    inputs = verify_inputs(
        input_directory=input_directory,
        expected=ExpectedRelease(
            tag=expected.tag,
            repository=expected.repository,
            commit=expected.commit,
            source_run_id=expected.source_run_id,
            source_run_attempt=expected.source_run_attempt,
        ),
    )
    _regular_file(inner_signature, "inner Minisign signature", maximum=MAX_SIGNATURE_BYTES)
    _run_minisign(
        minisign,
        input_directory / INSTALL_MANIFEST_NAME,
        inner_signature,
        public_key,
    )
    input_document = _parse_canonical_json(
        input_directory / PROVENANCE_NAME,
        "release input provenance",
    )
    input_manifest_digest = _sha256(input_directory / INPUT_MANIFEST_NAME)
    if input_manifest_digest != expected.input_manifest_sha256:
        raise OfflineReleaseError(
            "release input manifest does not match the expected offline-verified digest"
        )
    candidate_document = _candidate_provenance(
        expected,
        source_date_epoch=input_document["source_date_epoch"],
        public_key_sha256=_sha256(_regular_file(public_key, "Minisign public key")),
    )
    output_directory = _empty_output_directory(output_directory, "global signing request")
    with tempfile.TemporaryDirectory(prefix="reticulumpi-global-request-") as raw:
        staging = Path(raw)
        provenance = _write_exclusive(
            staging / PROVENANCE_NAME,
            _canonical_json(candidate_document),
        )
        bundle = staging / f"reticulumpi-install-arm64-{inputs.version}.tar.gz"
        build_install_bundle.build_install_bundle(
            sdist=inputs.sdist,
            wheel=inputs.wheel,
            output=bundle,
            version=inputs.version,
            manifest_signature=inner_signature,
            public_key=public_key,
            minisign=minisign,
            source_date_epoch=input_document["source_date_epoch"],
        )
        paths = prepare_release_assets.prepare_release_assets(
            tag=expected.tag,
            python_directory=input_directory / "python",
            image_directories={
                architecture: input_directory / "images" / architecture
                for architecture in prepare_release_assets.ARCHITECTURES
            },
            recovery_admin_directory=input_directory / "recovery-admin",
            provenance=provenance,
            install_bundle=bundle,
            output_directory=output_directory,
        )
    verify_global_request(
        directory=output_directory,
        expected=expected,
        public_key=public_key,
        minisign=minisign,
    )
    return paths


def verify_global_request(
    *,
    directory: Path,
    expected: ExpectedRelease,
    public_key: Path,
    minisign: Path,
) -> dict[str, Path]:
    """Verify an attested global-manifest signing request."""

    return _verify_release_payload(
        directory=directory,
        expected=expected,
        public_key=public_key,
        minisign=minisign,
        require_global_signature=False,
    )


def finalize_candidate(
    *,
    request_directory: Path,
    output_directory: Path,
    global_signature: Path,
    expected: ExpectedRelease,
    public_key: Path,
    minisign: Path,
) -> list[Path]:
    """Attach and verify the public global signature without changing payload bytes."""

    request_files = verify_global_request(
        directory=request_directory,
        expected=expected,
        public_key=public_key,
        minisign=minisign,
    )
    _run_minisign(
        minisign,
        request_directory / GLOBAL_MANIFEST_NAME,
        global_signature,
        public_key,
    )
    output_directory = _empty_output_directory(output_directory, "signed release candidate")
    copied = [
        _copy_exclusive(path, output_directory / name)
        for name, path in sorted(request_files.items())
    ]
    copied.append(_copy_exclusive(global_signature, output_directory / GLOBAL_SIGNATURE_NAME))
    verify_candidate(
        directory=output_directory,
        expected=expected,
        public_key=public_key,
        minisign=minisign,
    )
    return copied


def verify_candidate(
    *,
    directory: Path,
    expected: ExpectedRelease,
    public_key: Path,
    minisign: Path,
) -> dict[str, Path]:
    """Verify the complete nested/global signed candidate and exact asset set."""

    return _verify_release_payload(
        directory=directory,
        expected=expected,
        public_key=public_key,
        minisign=minisign,
        require_global_signature=True,
    )


def _public_key_material(path: Path) -> str:
    _regular_file(path, "Minisign public key", maximum=MAX_SIGNATURE_BYTES)
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise OfflineReleaseError("cannot read Minisign public key") from exc
    if (
        len(lines) != 2
        or not lines[0].startswith("untrusted comment: ")
        or re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", lines[1]) is None
    ):
        raise OfflineReleaseError("Minisign public key has an unexpected format")
    return lines[1]


def _private_signing_key(path: Path) -> Path:
    _regular_file(path, "Minisign secret key", maximum=MAX_SIGNATURE_BYTES)
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
        raise OfflineReleaseError("Minisign secret key ownership or link count is unsafe")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise OfflineReleaseError("Minisign secret key must have mode 0600")
    parent = path.parent
    parent_metadata = parent.lstat()
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.getuid()
        or stat.S_IMODE(parent_metadata.st_mode) & 0o077
    ):
        raise OfflineReleaseError("Minisign secret key directory must be private to its owner")
    return path


def sign_request(
    *,
    kind: str,
    tag: str,
    manifest: Path,
    signature: Path,
    base64_output: Path,
    signing_key: Path,
    public_key: Path,
    minisign: Path,
) -> tuple[Path, Path]:
    """Sign one verified request locally; this function performs no network operations."""

    if kind not in {"install", "release"}:
        raise OfflineReleaseError(f"unsupported signing request kind: {kind}")
    version = version_from_tag(tag)
    expected_name = INSTALL_MANIFEST_NAME if kind == "install" else GLOBAL_MANIFEST_NAME
    if manifest.name != expected_name:
        raise OfflineReleaseError(f"{kind} signing request must be named {expected_name}")
    _regular_file(manifest, f"{kind} signing request", maximum=MAX_MANIFEST_BYTES)
    signing_key = _private_signing_key(signing_key)
    expected_public_material = _public_key_material(public_key)
    if signature.exists() or signature.is_symlink():
        raise OfflineReleaseError(f"signature output already exists: {signature}")
    if base64_output.exists() or base64_output.is_symlink():
        raise OfflineReleaseError(f"base64 output already exists: {base64_output}")
    trusted_comment = (
        "ReticulumPi install-bundle SHA256SUMS"
        if kind == "install"
        else f"ReticulumPi {version} release asset manifest"
    )
    with tempfile.TemporaryDirectory(prefix="reticulumpi-derived-public-key-") as raw:
        derived = Path(raw) / "release.pub"
        try:
            subprocess.run(
                [str(minisign), "-R", "-s", str(signing_key), "-p", str(derived)],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                env={"LANG": "C", "PATH": "/usr/bin:/bin"},
            )
        except OSError as exc:
            raise OfflineReleaseError(f"cannot execute Minisign signer: {minisign}") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "public-key derivation failed").strip()
            raise OfflineReleaseError(f"cannot derive Minisign public key: {detail}") from exc
        if _public_key_material(derived) != expected_public_material:
            raise OfflineReleaseError("Minisign secret key does not match the trusted public key")
    command = [
        str(minisign),
        "-S",
        "-W",
        "-t",
        trusted_comment,
        "-s",
        str(signing_key),
        "-m",
        str(manifest),
        "-x",
        str(signature),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env={"LANG": "C", "PATH": "/usr/bin:/bin"},
        )
    except OSError as exc:
        raise OfflineReleaseError(f"cannot execute Minisign signer: {minisign}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "signature failed").strip()
        raise OfflineReleaseError(f"Minisign signing failed: {detail}") from exc
    _run_minisign(minisign, manifest, signature, public_key)
    encoded = base64.b64encode(_regular_file(signature, "Minisign signature").read_bytes()) + b"\n"
    _write_exclusive(base64_output, encoded)
    return signature, base64_output


def verify_and_sign_request(
    *,
    kind: str,
    request_directory: Path,
    signature: Path,
    base64_output: Path,
    signing_key: Path,
    public_key: Path,
    minisign: Path,
    expected: ExpectedRelease,
) -> tuple[Path, Path]:
    """Verify an exact request, snapshot its manifest, and only then sign it offline."""

    try:
        root = request_directory.resolve(strict=True)
    except OSError as exc:
        raise OfflineReleaseError(f"signing request is unavailable: {request_directory}") from exc
    for output in (signature, base64_output):
        if output.exists() or output.is_symlink():
            raise OfflineReleaseError(f"signing output already exists: {output}")
        try:
            output.parent.resolve(strict=True).relative_to(root)
        except ValueError:
            pass
        except OSError as exc:
            raise OfflineReleaseError(
                f"signing output directory is unavailable: {output.parent}"
            ) from exc
        else:
            raise OfflineReleaseError("signature outputs must remain outside the verified request")

    with tempfile.TemporaryDirectory(prefix="reticulumpi-signing-request-") as raw:
        snapshot = _snapshot_tree(
            request_directory,
            Path(raw) / "request",
            f"{kind} signing request",
        )
        if kind == "install":
            if expected.input_manifest_sha256 is None:
                raise OfflineReleaseError(
                    "install signing requires the attested input manifest digest"
                )
            if _sha256(snapshot / INPUT_MANIFEST_NAME) != expected.input_manifest_sha256:
                raise OfflineReleaseError(
                    "install signing request does not match the attested input manifest digest"
                )
            verify_inputs(input_directory=snapshot, expected=expected)
            manifest = snapshot / INSTALL_MANIFEST_NAME
            live_manifest = request_directory / INSTALL_MANIFEST_NAME
        elif kind == "release":
            verify_global_request(
                directory=snapshot,
                expected=expected,
                public_key=public_key,
                minisign=minisign,
            )
            manifest = snapshot / GLOBAL_MANIFEST_NAME
            live_manifest = request_directory / GLOBAL_MANIFEST_NAME
        else:
            raise OfflineReleaseError(f"unsupported signing request kind: {kind}")
        try:
            paths = sign_request(
                kind=kind,
                tag=expected.tag,
                manifest=manifest,
                signature=signature,
                base64_output=base64_output,
                signing_key=signing_key,
                public_key=public_key,
                minisign=minisign,
            )
            if _sha256(_regular_file(live_manifest, "live signing manifest")) != _sha256(manifest):
                raise OfflineReleaseError("signing request changed while it was being signed")
            _run_minisign(minisign, live_manifest, signature, public_key)
        except Exception:
            signature.unlink(missing_ok=True)
            base64_output.unlink(missing_ok=True)
            raise
    return paths


def _expected_from_args(args: argparse.Namespace, *, candidate: bool) -> ExpectedRelease:
    return ExpectedRelease(
        tag=args.tag,
        repository=args.repository,
        commit=args.commit,
        source_run_id=args.source_run_id,
        source_run_attempt=args.source_run_attempt,
        input_manifest_sha256=getattr(args, "input_manifest_sha256", None),
        candidate_run_id=args.candidate_run_id if candidate else None,
        candidate_run_attempt=args.candidate_run_attempt if candidate else None,
    )


def _add_identity_arguments(parser: argparse.ArgumentParser, *, candidate: bool) -> None:
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--source-run-id", required=True, type=int)
    parser.add_argument("--source-run-attempt", required=True, type=int)
    if candidate:
        parser.add_argument("--input-manifest-sha256", required=True)
        parser.add_argument("--candidate-run-id", required=True, type=int)
        parser.add_argument("--candidate-run-attempt", required=True, type=int)


def _add_verifier_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--public-key", required=True, type=Path)
    parser.add_argument("--minisign", default=Path("/usr/bin/minisign"), type=Path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare-inputs")
    _add_identity_arguments(prepare, candidate=False)
    prepare.add_argument("--source-date-epoch", required=True, type=int)
    prepare.add_argument("--input-directory", required=True, type=Path)

    verify_input = subparsers.add_parser("verify-inputs")
    _add_identity_arguments(verify_input, candidate=False)
    verify_input.add_argument("--input-directory", required=True, type=Path)

    stage = subparsers.add_parser("stage-global-request")
    _add_identity_arguments(stage, candidate=True)
    _add_verifier_arguments(stage)
    stage.add_argument("--input-directory", required=True, type=Path)
    stage.add_argument("--output-directory", required=True, type=Path)
    stage.add_argument("--inner-signature", required=True, type=Path)

    verify_global = subparsers.add_parser("verify-global-request")
    _add_identity_arguments(verify_global, candidate=True)
    _add_verifier_arguments(verify_global)
    verify_global.add_argument("--directory", required=True, type=Path)

    finalize = subparsers.add_parser("finalize-candidate")
    _add_identity_arguments(finalize, candidate=True)
    _add_verifier_arguments(finalize)
    finalize.add_argument("--request-directory", required=True, type=Path)
    finalize.add_argument("--output-directory", required=True, type=Path)
    finalize.add_argument("--global-signature", required=True, type=Path)

    verify = subparsers.add_parser("verify-candidate")
    _add_identity_arguments(verify, candidate=True)
    _add_verifier_arguments(verify)
    verify.add_argument("--directory", required=True, type=Path)

    sign = subparsers.add_parser("sign-request")
    _add_identity_arguments(sign, candidate=False)
    sign.add_argument("--kind", required=True, choices=("install", "release"))
    sign.add_argument("--input-manifest-sha256", required=True)
    sign.add_argument("--candidate-run-id", type=int)
    sign.add_argument("--candidate-run-attempt", type=int)
    sign.add_argument("--request-directory", required=True, type=Path)
    sign.add_argument("--signature", required=True, type=Path)
    sign.add_argument("--base64-output", required=True, type=Path)
    sign.add_argument("--signing-key", required=True, type=Path)
    sign.add_argument("--public-key", required=True, type=Path)
    sign.add_argument("--minisign", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare-inputs":
            paths = prepare_inputs(
                input_directory=args.input_directory,
                expected=_expected_from_args(args, candidate=False),
                source_date_epoch=args.source_date_epoch,
            )
        elif args.command == "verify-inputs":
            inputs = verify_inputs(
                input_directory=args.input_directory,
                expected=_expected_from_args(args, candidate=False),
            )
            paths = [inputs.provenance]
        elif args.command == "stage-global-request":
            paths = stage_global_request(
                input_directory=args.input_directory,
                output_directory=args.output_directory,
                inner_signature=args.inner_signature,
                public_key=args.public_key,
                minisign=args.minisign,
                expected=_expected_from_args(args, candidate=True),
            )
        elif args.command == "verify-global-request":
            paths = list(
                verify_global_request(
                    directory=args.directory,
                    expected=_expected_from_args(args, candidate=True),
                    public_key=args.public_key,
                    minisign=args.minisign,
                ).values()
            )
        elif args.command == "finalize-candidate":
            paths = finalize_candidate(
                request_directory=args.request_directory,
                output_directory=args.output_directory,
                global_signature=args.global_signature,
                expected=_expected_from_args(args, candidate=True),
                public_key=args.public_key,
                minisign=args.minisign,
            )
        elif args.command == "verify-candidate":
            paths = list(
                verify_candidate(
                    directory=args.directory,
                    expected=_expected_from_args(args, candidate=True),
                    public_key=args.public_key,
                    minisign=args.minisign,
                ).values()
            )
        else:
            paths = list(
                verify_and_sign_request(
                    kind=args.kind,
                    request_directory=args.request_directory,
                    signature=args.signature,
                    base64_output=args.base64_output,
                    signing_key=args.signing_key,
                    public_key=args.public_key,
                    minisign=args.minisign,
                    expected=_expected_from_args(
                        args,
                        candidate=args.kind == "release",
                    ),
                )
            )
    except (
        OfflineReleaseError,
        build_install_bundle.InstallBundleError,
        prepare_release_assets.ReleaseAssetError,
        ValueError,
    ) as exc:
        parser.error(str(exc))
    print("\n".join(str(path) for path in paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
