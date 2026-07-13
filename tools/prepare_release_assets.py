#!/usr/bin/env python3
"""Validate and stage the exact artifacts consumed by tag-only publication."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tarfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import BinaryIO

if __package__:
    from tools.build_admin_deb import SUPPORTED_PLATFORM_PYTHON, admin_deb_filename
    from tools.build_install_bundle import MAX_BYTES, MAX_FILES
    from tools.verify_release_tag import verify_artifacts, version_from_tag
    from tools.verify_sbom import validate_sbom
else:
    from build_admin_deb import SUPPORTED_PLATFORM_PYTHON, admin_deb_filename
    from build_install_bundle import MAX_BYTES, MAX_FILES
    from verify_release_tag import verify_artifacts, version_from_tag
    from verify_sbom import validate_sbom


ARCHITECTURES = ("amd64", "arm64")
SHA256_LINE = re.compile(r"^(?P<digest>[0-9a-f]{64})  (?P<name>[^/\r\n]+)\n$")
EMPTY_MANIFEST_SHA256 = hashlib.sha256(b"").hexdigest()


class ReleaseAssetError(ValueError):
    """Raised when a validated-build artifact set is incomplete or ambiguous."""


def _hash_stream(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return _hash_stream(handle)


def _regular_file(path: Path, root: Path, label: str) -> Path:
    try:
        relative = path.relative_to(root)
        metadata = path.lstat()
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ReleaseAssetError(f"{label} escapes its artifact directory: {path}") from exc
    if not relative.parts or not stat.S_ISREG(metadata.st_mode):
        raise ReleaseAssetError(f"{label} must be a regular file: {path}")
    return path


def _artifact_files(root: Path, label: str) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise ReleaseAssetError(f"{label} directory is missing or unsafe: {root}")
    files: list[Path] = []
    for path in root.rglob("*"):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not (
            stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
        ):
            raise ReleaseAssetError(f"{label} contains a special file: {path}")
        if stat.S_ISREG(metadata.st_mode):
            files.append(_regular_file(path, root, label))
    return sorted(files)


def _one_matching(files: list[Path], predicate: Callable[[Path], bool], label: str) -> Path:
    matches = [path for path in files if predicate(path)]
    if len(matches) != 1:
        raise ReleaseAssetError(f"expected exactly one {label}, found {len(matches)}")
    return matches[0]


def _verify_sidecar(archive: Path, sidecar: Path) -> None:
    try:
        content = sidecar.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise ReleaseAssetError(f"cannot read checksum sidecar: {sidecar}") from exc
    match = SHA256_LINE.fullmatch(content)
    if match is None or match.group("name") != archive.name:
        raise ReleaseAssetError(f"checksum sidecar is malformed: {sidecar}")
    if match.group("digest") != _sha256(archive):
        raise ReleaseAssetError(f"checksum sidecar does not match: {archive}")


def _ar_members(path: Path) -> dict[str, bytes]:
    """Read the three small deterministic members of a recovery .deb."""

    try:
        size = path.stat().st_size
        if size > MAX_BYTES:
            raise ReleaseAssetError("recovery administrator package exceeds the safety limit")
        raw = path.read_bytes()
    except OSError as exc:
        raise ReleaseAssetError(f"cannot read recovery administrator package: {path}") from exc
    if not raw.startswith(b"!<arch>\n"):
        raise ReleaseAssetError("recovery administrator package has an invalid ar signature")
    offset = 8
    members: dict[str, bytes] = {}
    while offset < len(raw):
        if len(raw) - offset < 60:
            raise ReleaseAssetError("recovery administrator package has a truncated ar header")
        header = raw[offset : offset + 60]
        if header[58:] != b"`\n":
            raise ReleaseAssetError("recovery administrator package has a malformed ar header")
        try:
            raw_name = header[:16].decode("ascii").strip()
            raw_size = header[48:58].decode("ascii").strip()
            member_size = int(raw_size)
        except (UnicodeError, ValueError) as exc:
            raise ReleaseAssetError(
                "recovery administrator package has non-canonical ar metadata"
            ) from exc
        if not raw_name.endswith("/") or member_size < 0:
            raise ReleaseAssetError("recovery administrator package has non-canonical ar metadata")
        name = raw_name.removesuffix("/")
        if name in members:
            raise ReleaseAssetError(f"recovery administrator package repeats ar member: {name}")
        offset += 60
        end = offset + member_size
        if end > len(raw):
            raise ReleaseAssetError("recovery administrator package has a truncated ar member")
        members[name] = raw[offset:end]
        offset = end
        if member_size % 2:
            if offset >= len(raw) or raw[offset : offset + 1] != b"\n":
                raise ReleaseAssetError("recovery administrator package has invalid ar padding")
            offset += 1
    expected = {"debian-binary", "control.tar.gz", "data.tar.gz"}
    if set(members) != expected or members.get("debian-binary") != b"2.0\n":
        raise ReleaseAssetError("recovery administrator package has an unexpected ar structure")
    return members


def _inspect_deb_tar(
    payload: bytes,
    *,
    label: str,
    selected: set[str],
) -> tuple[set[str], dict[str, bytes]]:
    paths: set[str] = set()
    contents: dict[str, bytes] = {}
    total_size = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            for index, member in enumerate(archive, 1):
                if index > MAX_FILES:
                    raise ReleaseAssetError(f"{label} exceeds the inspection entry limit")
                name = _safe_tar_name(member.name.removeprefix("./")).as_posix()
                if name in paths:
                    raise ReleaseAssetError(f"{label} repeats archive member: {name}")
                paths.add(name)
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise ReleaseAssetError(f"{label} contains a special member: {name}")
                if not (member.isfile() or member.isdir()):
                    raise ReleaseAssetError(f"{label} contains an unsupported member: {name}")
                if member.isfile():
                    total_size += member.size
                    if total_size > MAX_BYTES:
                        raise ReleaseAssetError(f"{label} exceeds the inspection safety limit")
                    if name in selected:
                        if member.size > 1024 * 1024:
                            raise ReleaseAssetError(f"{label} metadata is oversized: {name}")
                        stream = archive.extractfile(member)
                        if stream is None:
                            raise ReleaseAssetError(f"cannot read {label} member: {name}")
                        contents[name] = stream.read(member.size + 1)
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseAssetError(f"cannot inspect {label}: {exc}") from exc
    return paths, contents


def _control_fields(raw: bytes) -> dict[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ReleaseAssetError("recovery administrator control metadata is not UTF-8") from exc
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith(" "):
            continue
        if ": " not in line:
            raise ReleaseAssetError("recovery administrator control metadata is malformed")
        name, value = line.split(": ", 1)
        if name in fields:
            raise ReleaseAssetError(f"recovery administrator control repeats field: {name}")
        fields[name] = value
    return fields


def inspect_admin_deb(path: Path, *, version: str, profile: str, wheel: Path) -> None:
    """Fail closed unless *path* is the expected profile-specific recovery package."""

    if profile not in SUPPORTED_PLATFORM_PYTHON:
        raise ReleaseAssetError(f"unsupported recovery administrator profile: {profile}")
    expected_name = admin_deb_filename(version, profile)
    if path.name != expected_name:
        raise ReleaseAssetError(f"recovery administrator package must be named {expected_name}")
    members = _ar_members(path)
    control_paths, control = _inspect_deb_tar(
        members["control.tar.gz"],
        label="recovery administrator control archive",
        selected={"control", "md5sums"},
    )
    if control_paths != {"control", "md5sums"} or set(control) != control_paths:
        raise ReleaseAssetError("recovery administrator control archive is incomplete")
    fields = _control_fields(control["control"])
    minimum, maximum = SUPPORTED_PLATFORM_PYTHON[profile]
    expected_fields = {
        "Package": "reticulumpi-admin",
        "Version": version,
        "Section": "admin",
        "Priority": "optional",
        "Architecture": "arm64",
        "Maintainer": ("ReticulumPi Release Engineering <reticulumpi@users.noreply.github.com>"),
        "Depends": (f"python3 (>= {minimum}), python3 (<< {maximum}), python3-venv, minisign"),
        "X-ReticulumPi-Platform-Profile": profile,
        "Description": "isolated ReticulumPi recovery administrator",
    }
    expected_control_names = {
        "Package",
        "Version",
        "Section",
        "Priority",
        "Architecture",
        "Maintainer",
        "Depends",
        "Installed-Size",
        "X-ReticulumPi-Platform-Profile",
        "Description",
    }
    if set(fields) != expected_control_names:
        raise ReleaseAssetError("recovery administrator control fields are not canonical")
    for name, expected in expected_fields.items():
        if fields.get(name) != expected:
            raise ReleaseAssetError(
                f"recovery administrator control field {name} does not match {profile}"
            )
    if not fields.get("Installed-Size", "").isdigit():
        raise ReleaseAssetError("recovery administrator Installed-Size is invalid")

    required_data = {
        "usr/sbin/reticulumpi-admin",
        "usr/lib/reticulumpi-admin/launcher.py",
        "usr/lib/reticulumpi-admin/build.json",
        "usr/lib/reticulumpi-admin/site-packages/reticulumpi/admin_cli.py",
        "usr/lib/reticulumpi-admin/site-packages/reticulumpi/cli_help.py",
        "usr/lib/reticulumpi-admin/site-packages/reticulumpi/platform_policy.py",
    }
    data_paths, data = _inspect_deb_tar(
        members["data.tar.gz"],
        label="recovery administrator data archive",
        selected=required_data,
    )
    missing = required_data - data_paths
    if missing or set(data) != required_data:
        raise ReleaseAssetError(
            f"recovery administrator data archive is missing {sorted(missing)[0]}"
        )
    try:
        metadata = json.loads(data["usr/lib/reticulumpi-admin/build.json"])
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseAssetError("recovery administrator build metadata is invalid") from exc
    expected_metadata = {
        "architecture": "arm64",
        "kind": "reticulumpi-recovery-administrator",
        "platform_profile": profile,
        "reticulumpi_wheel": {"filename": wheel.name, "sha256": _sha256(wheel)},
        "runtime_source": {
            "kind": "site-packages",
            "sha256_manifest": EMPTY_MANIFEST_SHA256,
        },
        "schema": 1,
        "source_date_epoch": metadata.get("source_date_epoch")
        if isinstance(metadata, dict)
        else None,
        "version": version,
    }
    if (
        not isinstance(metadata, dict)
        or type(metadata.get("source_date_epoch")) is not int
        or metadata.get("source_date_epoch", -1) < 0
        or metadata != expected_metadata
    ):
        raise ReleaseAssetError(
            "recovery administrator build metadata does not match release inputs"
        )


def _safe_tar_name(name: str) -> PurePosixPath:
    relative = PurePosixPath(name.rstrip("/"))
    if (
        not name.rstrip("/")
        or name.rstrip("/") != relative.as_posix()
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in name
    ):
        raise ReleaseAssetError(f"archive contains an unsafe member: {name!r}")
    return relative


def inspect_image_archive(path: Path, architecture: str) -> None:
    """Verify a Docker save archive identifies one Linux image of *architecture*."""

    expected_tag = f"reticulumpi:{architecture}"
    members: dict[str, tarfile.TarInfo] = {}
    entry_count = 0
    total_size = 0
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            for member in archive:
                entry_count += 1
                if entry_count > MAX_FILES:
                    raise ReleaseAssetError("image archive exceeds the inspection entry limit")
                name = _safe_tar_name(member.name).as_posix()
                if name in members:
                    raise ReleaseAssetError(f"image archive contains duplicate member: {name}")
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise ReleaseAssetError(f"image archive contains special member: {name}")
                if not (member.isfile() or member.isdir()):
                    raise ReleaseAssetError(f"image archive contains unsupported member: {name}")
                if member.isfile():
                    total_size += member.size
                    if total_size > 10 * MAX_BYTES:
                        raise ReleaseAssetError("image archive exceeds the inspection safety limit")
                members[name] = member

            manifest_member = members.get("manifest.json")
            if manifest_member is None or not manifest_member.isfile():
                raise ReleaseAssetError("image archive has no regular manifest.json")
            if manifest_member.size > 1024 * 1024:
                raise ReleaseAssetError("image archive manifest.json is oversized")
            extracted = archive.extractfile(manifest_member)
            if extracted is None:
                raise ReleaseAssetError("cannot read image manifest")
            manifest = json.loads(extracted.read(1024 * 1024 + 1))
            if not isinstance(manifest, list) or len(manifest) != 1:
                raise ReleaseAssetError("image archive must contain exactly one image manifest")
            record = manifest[0]
            if not isinstance(record, dict) or record.get("RepoTags") != [expected_tag]:
                raise ReleaseAssetError(f"image archive must carry only tag {expected_tag}")
            config_name = record.get("Config")
            layers = record.get("Layers")
            if not isinstance(config_name, str) or config_name not in members:
                raise ReleaseAssetError("image archive references a missing config")
            if not isinstance(layers, list) or not layers:
                raise ReleaseAssetError("image archive has no layers")
            if any(
                not isinstance(name, str) or name not in members or not members[name].isfile()
                for name in layers
            ):
                raise ReleaseAssetError("image archive references a missing layer")
            config_member = members[config_name]
            if not config_member.isfile() or config_member.size > 16 * 1024 * 1024:
                raise ReleaseAssetError("image config is missing or oversized")
            config_stream = archive.extractfile(config_member)
            if config_stream is None:
                raise ReleaseAssetError("cannot read image config")
            config = json.loads(config_stream.read(config_member.size + 1))
    except (OSError, tarfile.TarError, json.JSONDecodeError) as exc:
        raise ReleaseAssetError(f"cannot inspect image archive {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ReleaseAssetError("image config root must be an object")
    if config.get("architecture") != architecture or config.get("os") != "linux":
        raise ReleaseAssetError(
            f"image config is {config.get('os')}/{config.get('architecture')}, "
            f"expected linux/{architecture}"
        )


def _reticulumpi_component_versions(value: object) -> list[str]:
    versions: list[str] = []
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            if str(current.get("name", "")).casefold() == "reticulumpi":
                version = current.get("version")
                if isinstance(version, str):
                    versions.append(version)
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return versions


def _verify_sbom_version(sbom: Path, version: str) -> None:
    validate_sbom(sbom)
    try:
        document = json.loads(sbom.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseAssetError(f"cannot read validated SBOM: {exc}") from exc
    versions = _reticulumpi_component_versions(document)
    if not versions or set(versions) != {version}:
        raise ReleaseAssetError(
            f"SBOM reticulumpi components must all identify version {version}, found {versions}"
        )


def inspect_install_bundle(bundle: Path, version: str, wheel: Path) -> None:
    expected_root = f"reticulumpi-{version}"
    expected_name = f"reticulumpi-install-arm64-{version}.tar.gz"
    if bundle.name != expected_name:
        raise ReleaseAssetError(f"install bundle must be named {expected_name}")
    members: dict[str, tarfile.TarInfo] = {}
    file_digests: dict[str, str] = {}
    entry_count = 0
    total_size = 0
    try:
        with tarfile.open(bundle, mode="r:gz") as archive:
            for member in archive:
                entry_count += 1
                if entry_count > MAX_FILES:
                    raise ReleaseAssetError("install bundle exceeds the inspection entry limit")
                relative = _safe_tar_name(member.name)
                if relative.parts[0] != expected_root:
                    raise ReleaseAssetError("install bundle contains more than one root")
                name = relative.as_posix()
                if name in members:
                    raise ReleaseAssetError(f"install bundle contains duplicate member: {name}")
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise ReleaseAssetError(f"install bundle contains special member: {name}")
                if not (member.isfile() or member.isdir()):
                    raise ReleaseAssetError(f"install bundle contains unsupported member: {name}")
                members[name] = member
                if member.isfile():
                    total_size += member.size
                    if total_size > MAX_BYTES:
                        raise ReleaseAssetError(
                            "install bundle exceeds the inspection safety limit"
                        )
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ReleaseAssetError(f"cannot read install bundle member: {name}")
                    file_digests[name] = _hash_stream(stream)

            metadata_name = f"{expected_root}/bundle.json"
            metadata_member = members.get(metadata_name)
            if metadata_member is None or not metadata_member.isfile():
                raise ReleaseAssetError("install bundle has no bundle.json")
            if metadata_member.size > 16 * 1024:
                raise ReleaseAssetError("install bundle metadata is oversized")
            metadata_stream = archive.extractfile(metadata_member)
            if metadata_stream is None:
                raise ReleaseAssetError("cannot read install bundle metadata")
            metadata = json.loads(metadata_stream.read(metadata_member.size + 1))
            required = {
                "schema": 1,
                "kind": "reticulumpi-install",
                "version": version,
                "architecture": "arm64",
                "wheel": wheel.name,
            }
            if metadata != required:
                raise ReleaseAssetError("install bundle metadata does not match release inputs")
            wheel_name = f"{expected_root}/{wheel.name}"
            if file_digests.get(wheel_name) != _sha256(wheel):
                raise ReleaseAssetError("install bundle does not contain the exact validated wheel")

            manifest_name = f"{expected_root}/SHA256SUMS"
            signature_name = f"{expected_root}/SHA256SUMS.minisig"
            manifest_member = members.get(manifest_name)
            if manifest_member is None or signature_name not in file_digests:
                raise ReleaseAssetError("install bundle is missing its inner signed manifest")
            manifest_stream = archive.extractfile(manifest_member)
            if manifest_stream is None:
                raise ReleaseAssetError("cannot read install bundle manifest")
            manifest_text = manifest_stream.read(manifest_member.size + 1).decode("utf-8")
    except (OSError, UnicodeError, tarfile.TarError, json.JSONDecodeError) as exc:
        raise ReleaseAssetError(f"cannot inspect install bundle {bundle}: {exc}") from exc

    expected_hashes: dict[str, str] = {}
    for line_number, line in enumerate(manifest_text.splitlines(), 1):
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match is None:
            raise ReleaseAssetError(f"invalid inner manifest line {line_number}")
        digest, name = match.groups()
        relative = _safe_tar_name(name).as_posix()
        if relative in expected_hashes:
            raise ReleaseAssetError(f"duplicate inner manifest path: {relative}")
        expected_hashes[relative] = digest
    actual_hashes = {
        name.removeprefix(f"{expected_root}/"): digest
        for name, digest in file_digests.items()
        if name not in {manifest_name, signature_name}
    }
    if expected_hashes != actual_hashes:
        raise ReleaseAssetError("install bundle inner manifest is not an exact tree manifest")


def _copy_exact(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise ReleaseAssetError(f"release output already exists: {destination}")
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    destination.chmod(0o644)
    if _sha256(source) != _sha256(destination):
        destination.unlink(missing_ok=True)
        raise ReleaseAssetError(f"release artifact copy verification failed: {source}")


def prepare_release_assets(
    *,
    tag: str,
    python_directory: Path,
    image_directories: dict[str, Path],
    recovery_admin_directory: Path,
    install_bundle: Path,
    output_directory: Path,
) -> list[Path]:
    version = version_from_tag(tag)
    python_files = _artifact_files(python_directory, "Python distribution artifact")
    wheel = _one_matching(python_files, lambda path: path.suffix == ".whl", "wheel")
    sdist = _one_matching(
        python_files,
        lambda path: path.name.startswith("reticulumpi-") and path.name.endswith(".tar.gz"),
        "sdist",
    )
    sbom = _one_matching(
        python_files,
        lambda path: path.name == "reticulumpi.cdx.json",
        "CycloneDX SBOM",
    )
    if set(python_files) != {wheel, sdist, sbom}:
        raise ReleaseAssetError("Python distribution artifact contains unexpected files")
    verify_artifacts([wheel, sdist], version)
    _verify_sbom_version(sbom, version)

    images: dict[str, Path] = {}
    for architecture in ARCHITECTURES:
        directory = image_directories.get(architecture)
        if directory is None:
            raise ReleaseAssetError(f"missing {architecture} image artifact directory")
        files = _artifact_files(directory, f"{architecture} image artifact")
        expected_archive = f"reticulumpi-{architecture}.tar.gz"
        archive = _one_matching(files, lambda path: path.name == expected_archive, "image archive")
        sidecar = _one_matching(
            files,
            lambda path: path.name == f"{expected_archive}.sha256",
            "image checksum",
        )
        if set(files) != {archive, sidecar}:
            raise ReleaseAssetError(f"{architecture} image artifact contains unexpected files")
        _verify_sidecar(archive, sidecar)
        inspect_image_archive(archive, architecture)
        images[architecture] = archive

    bundle_root = install_bundle.parent
    _regular_file(install_bundle, bundle_root, "ARM64 install bundle")
    inspect_install_bundle(install_bundle, version, wheel)

    recovery_files = _artifact_files(recovery_admin_directory, "recovery administrator artifact")
    recovery_packages: list[tuple[Path, Path]] = []
    expected_recovery_files: set[Path] = set()
    for profile in SUPPORTED_PLATFORM_PYTHON:
        expected_name = admin_deb_filename(version, profile)
        package = _one_matching(
            recovery_files,
            lambda path, name=expected_name: path.name == name,
            f"{profile} recovery administrator package",
        )
        sidecar = _one_matching(
            recovery_files,
            lambda path, name=f"{expected_name}.sha256": path.name == name,
            f"{profile} recovery administrator checksum",
        )
        _verify_sidecar(package, sidecar)
        inspect_admin_deb(package, version=version, profile=profile, wheel=wheel)
        recovery_packages.append((package, sidecar))
        expected_recovery_files.update({package, sidecar})
    if set(recovery_files) != expected_recovery_files:
        raise ReleaseAssetError("recovery administrator artifact contains unexpected files")
    if output_directory.exists():
        if (
            output_directory.is_symlink()
            or not output_directory.is_dir()
            or any(output_directory.iterdir())
        ):
            raise ReleaseAssetError(f"release output directory is not empty: {output_directory}")
    output_directory.mkdir(mode=0o755, parents=True, exist_ok=True)

    staged: list[Path] = []
    destinations = [
        (wheel, wheel.name),
        (sdist, sdist.name),
        (sbom, f"reticulumpi-{version}.cdx.json"),
        (images["amd64"], f"reticulumpi-container-{version}-amd64.tar.gz"),
        (images["arm64"], f"reticulumpi-container-{version}-arm64.tar.gz"),
        (install_bundle, install_bundle.name),
    ]
    destinations.extend((path, path.name) for pair in recovery_packages for path in pair)
    for source, name in destinations:
        destination = output_directory / name
        _copy_exact(source, destination)
        staged.append(destination)

    manifest = output_directory / "SHA256SUMS"
    manifest.write_text(
        "".join(f"{_sha256(path)}  {path.name}\n" for path in sorted(staged)),
        encoding="ascii",
    )
    manifest.chmod(0o644)
    return [*staged, manifest]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--python-directory", required=True, type=Path)
    parser.add_argument("--amd64-image-directory", required=True, type=Path)
    parser.add_argument("--arm64-image-directory", required=True, type=Path)
    parser.add_argument("--recovery-admin-directory", required=True, type=Path)
    parser.add_argument("--install-bundle", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        paths = prepare_release_assets(
            tag=args.tag,
            python_directory=args.python_directory,
            image_directories={
                "amd64": args.amd64_image_directory,
                "arm64": args.arm64_image_directory,
            },
            recovery_admin_directory=args.recovery_admin_directory,
            install_bundle=args.install_bundle,
            output_directory=args.output_directory,
        )
    except (ReleaseAssetError, ValueError) as exc:
        parser.error(str(exc))
    print("\n".join(str(path) for path in paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
