#!/usr/bin/env python3
"""Build a deterministic, signed ARM64 install bundle from validated distributions."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

if __package__:
    from tools.verify_release_tag import artifact_version
else:
    from verify_release_tag import artifact_version


MAX_FILES = 20_000
MAX_BYTES = 2 * 1024 * 1024 * 1024
REQUIRED_DIRECTORIES = ("src", "systemd", "config", "scripts", "constraints")
REQUIRED_FILES = (
    "pyproject.toml",
    "README.md",
    "constraints/production-universal-core.txt",
    "constraints/production-universal-dashboard-nomadnet.txt",
    "constraints/production-universal-all-features.txt",
)
VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
LEGACY_CONSTRAINT_GLOB = "bookworm-py311-*"
MAX_MINISIGN_FILE_BYTES = 16 * 1024


class InstallBundleError(ValueError):
    """Raised when an install-bundle input violates the signed archive contract."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _regular_file(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise InstallBundleError(f"{label} is unavailable: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise InstallBundleError(f"{label} must be a regular file: {path}")
    return path


def _copy_regular_file(
    source: Path,
    destination: Path,
    *,
    label: str,
    mode: int,
    maximum_bytes: int | None = None,
) -> Path:
    """Copy one regular file through no-follow descriptors into a new file."""

    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    source_descriptor: int | None = None
    destination_descriptor: int | None = None
    destination_created = False
    copied = False
    try:
        source_descriptor = os.open(source, source_flags)
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise InstallBundleError(f"{label} must be a regular file: {source}")
        if maximum_bytes is not None and before.st_size > maximum_bytes:
            raise InstallBundleError(f"{label} exceeds the size limit")
        destination_descriptor = os.open(destination, destination_flags, mode)
        destination_created = True
        os.fchmod(destination_descriptor, mode)
        total = 0
        while block := os.read(source_descriptor, 1024 * 1024):
            total += len(block)
            if maximum_bytes is not None and total > maximum_bytes:
                raise InstallBundleError(f"{label} exceeds the size limit")
            remaining = memoryview(block)
            while remaining:
                written = os.write(destination_descriptor, remaining)
                if written <= 0:
                    raise InstallBundleError(f"short write while copying {label}")
                remaining = remaining[written:]
        os.fsync(destination_descriptor)
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
        ):
            raise InstallBundleError(f"{label} changed while being copied: {source}")
        copied = True
    except OSError as exc:
        raise InstallBundleError(f"cannot safely copy {label}: {source}") from exc
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        if source_descriptor is not None:
            os.close(source_descriptor)
        if destination_created and not copied:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
    return destination


def _safe_member(member: tarfile.TarInfo, expected_root: str) -> PurePosixPath:
    raw_name = member.name.rstrip("/")
    relative = PurePosixPath(raw_name)
    if (
        not raw_name
        or raw_name != relative.as_posix()
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in raw_name
        or relative.parts[0] != expected_root
    ):
        raise InstallBundleError(f"sdist contains an unsafe member: {member.name!r}")
    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
        raise InstallBundleError(f"sdist contains a forbidden special member: {member.name}")
    if not (member.isdir() or member.isfile()):
        raise InstallBundleError(f"sdist contains an unsupported member: {member.name}")
    return relative


def _extract_sdist(sdist: Path, destination: Path, version: str) -> Path:
    expected_root = f"reticulumpi-{version}"
    entry_count = 0
    total_size = 0
    seen: set[str] = set()
    try:
        with tarfile.open(sdist, mode="r:gz") as archive:
            for member in archive:
                entry_count += 1
                if entry_count > MAX_FILES:
                    raise InstallBundleError("sdist exceeds the extraction entry limit")
                relative = _safe_member(member, expected_root)
                name = relative.as_posix()
                if name in seen:
                    raise InstallBundleError(f"sdist contains a duplicate member: {name}")
                seen.add(name)
                if member.isfile():
                    total_size += member.size
                    if total_size > MAX_BYTES:
                        raise InstallBundleError("sdist exceeds the extraction safety limit")
                target = destination.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(mode=0o755, parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise InstallBundleError(f"cannot read sdist member: {member.name}")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                descriptor = os.open(target, flags, 0o755 if member.mode & 0o111 else 0o644)
                try:
                    with extracted, os.fdopen(descriptor, "wb", closefd=False) as handle:
                        remaining = member.size
                        while remaining:
                            block = extracted.read(min(remaining, 1024 * 1024))
                            if not block:
                                raise InstallBundleError(f"sdist member ended early: {member.name}")
                            handle.write(block)
                            remaining -= len(block)
                        if extracted.read(1):
                            raise InstallBundleError(
                                f"sdist member exceeds its declared size: {member.name}"
                            )
                        handle.flush()
                        os.fsync(handle.fileno())
                finally:
                    os.close(descriptor)
            if entry_count == 0:
                raise InstallBundleError("sdist is empty")
    except (OSError, tarfile.TarError) as exc:
        raise InstallBundleError(f"cannot extract validated sdist: {exc}") from exc
    return destination / expected_root


def _validate_source_root(root: Path) -> None:
    for name in REQUIRED_DIRECTORIES:
        path = root / name
        if path.is_symlink() or not path.is_dir():
            raise InstallBundleError(f"sdist is missing required directory: {name}")
    for name in REQUIRED_FILES:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise InstallBundleError(f"sdist is missing required file: {name}")
    legacy_constraints = sorted(
        path.name for path in (root / "constraints").glob(LEGACY_CONSTRAINT_GLOB)
    )
    if legacy_constraints:
        raise InstallBundleError(
            "sdist contains retired dependency profile aliases: " + ", ".join(legacy_constraints)
        )
    for reserved in ("bundle.json", "SHA256SUMS", "SHA256SUMS.minisig"):
        if os.path.lexists(root / reserved):
            raise InstallBundleError(f"sdist contains reserved install-bundle path: {reserved}")
    if any(root.glob("*.whl")):
        raise InstallBundleError("sdist must not contain a top-level wheel")


def _render_inner_manifest(root: Path) -> bytes:
    entries: list[str] = []
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not (
            stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
        ):
            raise InstallBundleError(f"install source contains a special file: {path}")
        relative = path.relative_to(root).as_posix()
        if stat.S_ISREG(metadata.st_mode) and relative not in {
            "SHA256SUMS",
            "SHA256SUMS.minisig",
        }:
            entries.append(f"{_sha256(path)}  {relative}")
    if not entries:
        raise InstallBundleError("install source contains no files")
    return ("\n".join(entries) + "\n").encode("utf-8")


def _write_inner_manifest(root: Path) -> Path:
    manifest = root / "SHA256SUMS"
    payload = _render_inner_manifest(root)
    with manifest.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    manifest.chmod(0o644)
    return manifest


def sign_manifest(manifest: Path, signature: Path, signing_key: Path, minisign: Path) -> None:
    """Sign *manifest* with an explicitly passwordless protected offline key."""

    _regular_file(signing_key, "Minisign secret key")
    key_mode = stat.S_IMODE(signing_key.stat().st_mode)
    if key_mode != 0o600:
        raise InstallBundleError(
            "Minisign secret key must have mode 0600 and not be group- or world-accessible"
        )
    if signature.exists() or signature.is_symlink():
        raise InstallBundleError(f"signature output already exists: {signature}")
    try:
        subprocess.run(
            [
                str(minisign),
                "-S",
                "-W",
                "-t",
                "ReticulumPi install-bundle SHA256SUMS",
                "-s",
                str(signing_key),
                "-m",
                str(manifest),
                "-x",
                str(signature),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env={"LANG": "C", "PATH": "/usr/bin:/bin"},
        )
    except OSError as exc:
        raise InstallBundleError(f"cannot execute Minisign signer {minisign}: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "signature failed").strip()
        raise InstallBundleError(f"Minisign signing failed: {detail}") from exc
    _regular_file(signature, "Minisign signature")


def _verify_manifest_signature(
    manifest: Path,
    signature: Path,
    public_key: Path,
    minisign: Path,
) -> None:
    try:
        subprocess.run(
            [
                str(minisign),
                "-V",
                "-m",
                str(manifest),
                "-x",
                str(signature),
                "-p",
                str(public_key),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env={"LANG": "C", "PATH": "/usr/bin:/bin"},
        )
    except OSError as exc:
        raise InstallBundleError(f"cannot execute Minisign verifier {minisign}: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "signature verification failed").strip()
        raise InstallBundleError(f"Minisign signature verification failed: {detail}") from exc


def _validate_release_inputs(sdist: Path, wheel: Path, version: str) -> None:
    if VERSION.fullmatch(version) is None:
        raise InstallBundleError(f"version must be MAJOR.MINOR.PATCH, got {version!r}")
    _regular_file(sdist, "sdist")
    _regular_file(wheel, "wheel")
    if artifact_version(sdist) != version or artifact_version(wheel) != version:
        raise InstallBundleError("wheel and sdist metadata must match the install-bundle version")


def _prepare_install_tree(
    *,
    sdist: Path,
    wheel: Path,
    version: str,
    destination: Path,
) -> Path:
    _validate_release_inputs(sdist, wheel, version)
    root = _extract_sdist(sdist, destination, version)
    _validate_source_root(root)
    _copy_regular_file(
        wheel,
        root / wheel.name,
        label="wheel",
        mode=0o644,
        maximum_bytes=MAX_BYTES,
    )
    metadata = {
        "schema": 1,
        "kind": "reticulumpi-install",
        "version": version,
        "architecture": "arm64",
        "wheel": wheel.name,
    }
    metadata_path = root / "bundle.json"
    with metadata_path.open("xb") as handle:
        handle.write(
            (json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        )
        handle.flush()
        os.fsync(handle.fileno())
    metadata_path.chmod(0o644)
    return root


def render_install_manifest(*, sdist: Path, wheel: Path, version: str) -> bytes:
    """Render the exact install-tree ``SHA256SUMS`` for validated inputs."""

    with tempfile.TemporaryDirectory(prefix="reticulumpi-install-manifest-") as raw:
        root = _prepare_install_tree(
            sdist=sdist,
            wheel=wheel,
            version=version,
            destination=Path(raw),
        )
        return _render_inner_manifest(root)


def _write_deterministic_archive(root: Path, output: Path, source_date_epoch: int) -> None:
    temporary = output.with_name(f".{output.name}.tmp")
    if output.exists() or output.is_symlink() or temporary.exists() or temporary.is_symlink():
        raise InstallBundleError(f"install archive output already exists: {output}")
    output.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    try:
        with temporary.open("xb") as raw:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=9,
                fileobj=raw,
                mtime=source_date_epoch,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
                ) as archive:
                    paths = [root, *sorted(root.rglob("*"))]
                    file_count = 0
                    total_size = 0
                    for path in paths:
                        metadata = path.lstat()
                        relative = path.relative_to(root.parent).as_posix()
                        info = tarfile.TarInfo(relative)
                        info.uid = 0
                        info.gid = 0
                        info.uname = "root"
                        info.gname = "root"
                        info.mtime = source_date_epoch
                        if stat.S_ISDIR(metadata.st_mode):
                            info.type = tarfile.DIRTYPE
                            info.mode = 0o755
                            archive.addfile(info)
                        elif stat.S_ISREG(metadata.st_mode):
                            file_count += 1
                            total_size += metadata.st_size
                            if file_count > MAX_FILES or total_size > MAX_BYTES:
                                raise InstallBundleError(
                                    "install bundle exceeds the archive safety limit"
                                )
                            info.type = tarfile.REGTYPE
                            info.mode = 0o755 if stat.S_IMODE(metadata.st_mode) & 0o111 else 0o644
                            info.size = metadata.st_size
                            with path.open("rb") as payload:
                                archive.addfile(info, payload)
                        else:
                            raise InstallBundleError(
                                f"install source contains a forbidden special file: {path}"
                            )
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def build_install_bundle(
    *,
    sdist: Path,
    wheel: Path,
    output: Path,
    version: str,
    signing_key: Path | None = None,
    manifest_signature: Path | None = None,
    public_key: Path | None = None,
    minisign: Path,
    source_date_epoch: int,
) -> Path:
    if (signing_key is None) == (manifest_signature is None):
        raise InstallBundleError(
            "exactly one of a Minisign signing key or detached manifest signature is required"
        )
    if manifest_signature is not None and public_key is None:
        raise InstallBundleError("Minisign public key is required with a detached signature")
    if signing_key is not None and public_key is not None:
        raise InstallBundleError("Minisign public key is only valid with a detached signature")
    expected_name = f"reticulumpi-install-arm64-{version}.tar.gz"
    if output.name != expected_name:
        raise InstallBundleError(f"install archive must be named {expected_name}")
    if source_date_epoch < 0:
        raise InstallBundleError("SOURCE_DATE_EPOCH must be non-negative")

    with tempfile.TemporaryDirectory(prefix="reticulumpi-install-build-") as raw:
        staging = Path(raw)
        root = _prepare_install_tree(
            sdist=sdist,
            wheel=wheel,
            version=version,
            destination=staging,
        )
        manifest = _write_inner_manifest(root)
        signature_target = root / "SHA256SUMS.minisig"
        if signing_key is not None:
            sign_manifest(manifest, signature_target, signing_key, minisign)
        else:
            assert manifest_signature is not None
            assert public_key is not None
            _copy_regular_file(
                manifest_signature,
                signature_target,
                label="detached Minisign signature",
                mode=0o644,
                maximum_bytes=MAX_MINISIGN_FILE_BYTES,
            )
            public_key_copy = _copy_regular_file(
                public_key,
                staging / "release.pub",
                label="Minisign public key",
                mode=0o644,
                maximum_bytes=MAX_MINISIGN_FILE_BYTES,
            )
            _verify_manifest_signature(manifest, signature_target, public_key_copy, minisign)
        _write_deterministic_archive(root, output, source_date_epoch)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdist", required=True, type=Path)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    signing_mode = parser.add_mutually_exclusive_group(required=True)
    signing_mode.add_argument("--signing-key", type=Path)
    signing_mode.add_argument("--manifest-signature", type=Path)
    parser.add_argument("--public-key", type=Path)
    parser.add_argument("--minisign", default=Path("/usr/bin/minisign"), type=Path)
    parser.add_argument("--source-date-epoch", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        output = build_install_bundle(
            sdist=args.sdist,
            wheel=args.wheel,
            output=args.output,
            version=args.version,
            signing_key=args.signing_key,
            manifest_signature=args.manifest_signature,
            public_key=args.public_key,
            minisign=args.minisign,
            source_date_epoch=args.source_date_epoch,
        )
    except InstallBundleError as exc:
        parser.error(str(exc))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
