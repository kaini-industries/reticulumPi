#!/usr/bin/env python3
"""Build the offline, independently installable ReticulumPi recovery administrator.

The recovery import surface is standard-library-only.  The builder therefore combines one
already validated ReticulumPi wheel with an explicitly empty private runtime and emits a
deterministic ARM64 Debian package plus a SHA-256 sidecar.  Dependency resolution, downloads,
wheelhouses, signing, and package installation are deliberately outside this tool.
"""

from __future__ import annotations

import argparse
import contextlib
import email.parser
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator


ARCHITECTURE = "arm64"
INSTALL_ROOT = PurePosixPath("usr/lib/reticulumpi-admin")
SITE_PACKAGES = INSTALL_ROOT / "site-packages"
WRAPPER_PATH = PurePosixPath("usr/sbin/reticulumpi-admin")
MAX_FILES = 50_000
MAX_BYTES = 2 * 1024 * 1024 * 1024
MAX_WHEEL_BYTES = 512 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
VERSION = re.compile(
    r"^(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*))*"
    r"(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?"
    r"(?:\+[a-z0-9]+(?:\.[a-z0-9]+)*)?$"
)
SUPPORTED_PLATFORM_PYTHON = {
    "linux-arm64-debian-bookworm-py311": ("3.11", "3.12"),
    "linux-arm64-ubuntu-noble-py312": ("3.12", "3.13"),
}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DIST_NORMALIZE = re.compile(r"[-_.]+")
EMPTY_MANIFEST_SHA256 = hashlib.sha256(b"").hexdigest()

WRAPPER = """#!/bin/sh
exec /usr/bin/python3 -I -S /usr/lib/reticulumpi-admin/launcher.py "$@"
"""

LAUNCHER = """# Installed by the independently released reticulumpi-admin OS package.
import os
import sys

PRIVATE_SITE = "/usr/lib/reticulumpi-admin/site-packages"

if not sys.flags.isolated or not sys.flags.no_site:
    raise SystemExit("reticulumpi-admin requires isolated Python with site disabled")

# Preserve only interpreter-owned standard-library paths, ahead of the immutable private
# payload.  In particular, do not import from cwd, PYTHONPATH, system site-packages, a
# ReticulumPi candidate, or the mutable current release. Standard-library precedence prevents
# a private payload file from shadowing modules used by the administrator.
stdlib = [
    entry
    for entry in sys.path
    if entry
    and "site-packages" not in entry
    and "dist-packages" not in entry
]
sys.path[:] = [*stdlib, PRIVATE_SITE]

from reticulumpi import admin_cli  # noqa: E402

module_path = os.path.realpath(admin_cli.__file__)
private_root = os.path.realpath(PRIVATE_SITE) + os.sep
if not module_path.startswith(private_root):
    raise SystemExit("refusing non-private reticulumpi administrator code")

raise SystemExit(admin_cli.main())
"""


class AdminDebError(ValueError):
    """Raised when an input or output violates the recovery-package contract."""


def admin_deb_filename(version: str, platform_profile: str) -> str:
    """Return the non-colliding release filename for one exact platform profile."""

    return f"reticulumpi-admin_{version}_{platform_profile}_{ARCHITECTURE}.deb"


@dataclass(frozen=True)
class AdminDebArtifacts:
    """Paths emitted by :func:`build_admin_deb`."""

    package: Path
    sha256: Path


@dataclass
class _Budget:
    files: int = 0
    bytes: int = 0

    def add(self, size: int) -> None:
        if size < 0:
            raise AdminDebError("input declares a negative file size")
        self.files += 1
        self.bytes += size
        if self.files > MAX_FILES:
            raise AdminDebError("administrator payload exceeds the file-count limit")
        if self.bytes > MAX_BYTES:
            raise AdminDebError("administrator payload exceeds the size limit")


def _sha256_stream(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


@contextlib.contextmanager
def _open_regular(path: Path, label: str) -> Iterator[BinaryIO]:
    """Open a regular file without following a final-component symlink."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AdminDebError(f"{label} is unavailable or unsafe: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AdminDebError(f"{label} must be a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            yield handle
    finally:
        os.close(descriptor)


def _sha256(path: Path, label: str = "file", *, max_bytes: int | None = None) -> str:
    with _open_regular(path, label) as handle:
        if max_bytes is not None and os.fstat(handle.fileno()).st_size > max_bytes:
            raise AdminDebError(f"{label} exceeds the size limit")
        return _sha256_stream(handle)


def _safe_relative(raw: str, label: str) -> PurePosixPath:
    relative = PurePosixPath(raw)
    if (
        not raw
        or raw != relative.as_posix()
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in raw
    ):
        raise AdminDebError(f"{label} contains an unsafe path: {raw!r}")
    return relative


def _inventory_tree(root: Path, label: str) -> tuple[list[Path], list[Path]]:
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise AdminDebError(f"{label} is unavailable: {root}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise AdminDebError(f"{label} must be a real directory: {root}")

    directories: list[Path] = []
    files: list[Path] = []
    try:
        candidates = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
        for path in candidates:
            entry = path.lstat()
            if stat.S_ISDIR(entry.st_mode):
                directories.append(path)
            elif stat.S_ISREG(entry.st_mode):
                files.append(path)
            else:
                raise AdminDebError(f"{label} contains a symlink or special file: {path}")
    except OSError as exc:
        raise AdminDebError(f"cannot inspect {label}: {exc}") from exc
    return directories, files


def _validate_empty_runtime_source(root: Path, manifest: Path) -> str:
    """Require an empty real directory and an exactly zero-byte manifest."""

    try:
        metadata = root.lstat()
    except OSError as exc:
        raise AdminDebError(f"offline runtime source is unavailable: {root}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise AdminDebError(f"offline runtime source must be a real directory: {root}")
    try:
        with os.scandir(root) as entries:
            if next(entries, None) is not None:
                raise AdminDebError("offline runtime source must be exactly empty")
    except AdminDebError:
        raise
    except OSError as exc:
        raise AdminDebError(f"cannot inspect offline runtime source: {root}") from exc

    with _open_regular(manifest, "runtime SHA-256 manifest") as handle:
        if os.fstat(handle.fileno()).st_size != 0 or handle.read(1):
            raise AdminDebError("runtime SHA-256 manifest must be exactly empty")
    return EMPTY_MANIFEST_SHA256


def _snapshot_verified_file(source: Path, target: Path, expected: str, label: str) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with _open_regular(source, label) as incoming:
        if os.fstat(incoming.fileno()).st_size > MAX_WHEEL_BYTES:
            raise AdminDebError(f"{label} exceeds the size limit")
        try:
            with target.open("xb") as outgoing:
                copied = 0
                for block in iter(lambda: incoming.read(1024 * 1024), b""):
                    copied += len(block)
                    if copied > MAX_WHEEL_BYTES:
                        raise AdminDebError(f"{label} exceeds the size limit")
                    digest.update(block)
                    outgoing.write(block)
                outgoing.flush()
                os.fsync(outgoing.fileno())
        except OSError as exc:
            raise AdminDebError(f"cannot snapshot {label}: {source}") from exc
    target.chmod(0o600)
    if digest.hexdigest() != expected:
        raise AdminDebError(f"{label} changed before it could be privately snapshotted")


def _zip_member_path(member: zipfile.ZipInfo) -> tuple[PurePosixPath, bool]:
    raw = member.filename.rstrip("/")
    relative = _safe_relative(raw, "wheel")
    is_directory = member.is_dir()
    unix_mode = (member.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    expected_type = stat.S_IFDIR if is_directory else stat.S_IFREG
    if file_type not in {0, expected_type}:
        raise AdminDebError(f"wheel contains a symlink or special member: {member.filename}")
    return relative, is_directory


def _wheel_target(relative: PurePosixPath) -> PurePosixPath | None:
    parts = relative.parts
    if parts[0].endswith(".data"):
        if len(parts) == 1:
            return None
        if len(parts) == 2 and parts[1] in {"data", "purelib", "platlib"}:
            return None
        if len(parts) >= 3 and parts[1] == "data":
            # Recovery administration does not consume a wheel's platform data
            # scheme (for ReticulumPi this is NomadNet documentation).  The
            # complete wheel is already hash-pinned and every declared member
            # still participates in the aggregate safety limit below.
            return None
        if len(parts) < 3 or parts[1] not in {"purelib", "platlib"}:
            raise AdminDebError(f"wheel uses an unsupported install scheme: {relative.as_posix()}")
        return PurePosixPath(*parts[2:])
    return relative


def _wheel_identity(archive: zipfile.ZipFile) -> tuple[str, str]:
    metadata_members: list[zipfile.ZipInfo] = []
    seen: set[str] = set()
    members = archive.infolist()
    if len(members) > MAX_FILES:
        raise AdminDebError("wheel exceeds the member-count limit")
    if sum(member.file_size for member in members) > MAX_BYTES:
        raise AdminDebError("wheel exceeds the declared uncompressed-size limit")
    for member in members:
        relative, is_directory = _zip_member_path(member)
        name = relative.as_posix()
        if name in seen:
            raise AdminDebError(f"wheel contains a duplicate member: {name}")
        seen.add(name)
        if (
            not is_directory
            and len(relative.parts) == 2
            and relative.parts[0].endswith(".dist-info")
            and relative.parts[1] == "METADATA"
        ):
            metadata_members.append(member)
    if len(metadata_members) != 1:
        raise AdminDebError(
            f"wheel must contain exactly one top-level dist-info/METADATA, found "
            f"{len(metadata_members)}"
        )
    metadata_member = metadata_members[0]
    if metadata_member.file_size > MAX_METADATA_BYTES:
        raise AdminDebError("wheel metadata exceeds the size limit")
    try:
        with archive.open(metadata_member) as metadata_handle:
            raw_metadata = metadata_handle.read(MAX_METADATA_BYTES + 1)
        if len(raw_metadata) > MAX_METADATA_BYTES:
            raise AdminDebError("wheel metadata exceeds the size limit")
        parsed = email.parser.BytesParser().parsebytes(raw_metadata)
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        raise AdminDebError("cannot read wheel metadata") from exc
    name = parsed.get("Name", "").strip()
    version = parsed.get("Version", "").strip()
    if not name or not version:
        raise AdminDebError("wheel metadata is missing Name or Version")
    return DIST_NORMALIZE.sub("-", name).lower(), version


def _same_file(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    return _sha256(left, "staged payload file") == _sha256(right, "staged wheel file")


def _extract_wheel(path: Path, target: Path, budget: _Budget) -> tuple[str, str]:
    try:
        with _open_regular(path, "wheel") as wheel_handle, zipfile.ZipFile(wheel_handle) as archive:
            identity = _wheel_identity(archive)
            for member in sorted(archive.infolist(), key=lambda item: item.filename):
                relative, is_directory = _zip_member_path(member)
                mapped = _wheel_target(relative)
                if mapped is None:
                    continue
                destination = target.joinpath(*mapped.parts)
                if is_directory:
                    if destination.exists() and not destination.is_dir():
                        raise AdminDebError(f"wheel directory collides with a file: {mapped}")
                    destination.mkdir(mode=0o755, parents=True, exist_ok=True)
                    continue
                budget.add(member.file_size)
                destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                unix_mode = (member.external_attr >> 16) & 0xFFFF
                temporary_name: str | None = None
                try:
                    with archive.open(member) as incoming:
                        with tempfile.NamedTemporaryFile(
                            mode="wb", dir=destination.parent, prefix=".wheel-", delete=False
                        ) as outgoing:
                            temporary_name = outgoing.name
                            copied = 0
                            for block in iter(lambda: incoming.read(1024 * 1024), b""):
                                outgoing.write(block)
                                copied += len(block)
                                if copied > member.file_size:
                                    raise AdminDebError(
                                        f"wheel member exceeds its declared size: {member.filename}"
                                    )
                            if copied != member.file_size:
                                raise AdminDebError(
                                    f"wheel member ended before its declared size: {member.filename}"
                                )
                            outgoing.flush()
                            os.fsync(outgoing.fileno())
                    temporary = Path(temporary_name)
                    temporary.chmod(0o755 if unix_mode & 0o111 else 0o644)
                    if destination.exists():
                        if not destination.is_file() or not _same_file(destination, temporary):
                            raise AdminDebError(f"wheel payload collision: {mapped.as_posix()}")
                        temporary.unlink()
                    else:
                        os.replace(temporary, destination)
                finally:
                    if temporary_name is not None:
                        Path(temporary_name).unlink(missing_ok=True)
    except AdminDebError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise AdminDebError(f"cannot inspect or extract wheel {path.name}: {exc}") from exc
    return identity


def _write_text(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise AdminDebError(f"cannot stage administrator package file: {path}") from exc
    path.chmod(mode)


def _validate_admin_payload(private_site: Path) -> None:
    required = (
        "reticulumpi/__init__.py",
        "reticulumpi/admin_cli.py",
        "reticulumpi/cli_help.py",
        "reticulumpi/platform_policy.py",
    )
    for relative in required:
        path = private_site / relative
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise AdminDebError(
                f"ReticulumPi wheel is missing administrator module: {relative}"
            ) from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise AdminDebError(f"ReticulumPi administrator module is unsafe: {relative}")


def _tar_gz(root: Path, output: Path, source_date_epoch: int) -> None:
    directories, files = _inventory_tree(root, "Debian package staging tree")
    paths = [*directories, *files]
    try:
        with output.open("xb") as raw:
            with gzip.GzipFile(
                filename="", mode="wb", compresslevel=9, fileobj=raw, mtime=source_date_epoch
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT
                ) as archive:
                    for path in paths:
                        metadata = path.lstat()
                        relative = path.relative_to(root).as_posix()
                        info = tarfile.TarInfo(f"./{relative}")
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
                            info.type = tarfile.REGTYPE
                            info.mode = 0o755 if stat.S_IMODE(metadata.st_mode) & 0o111 else 0o644
                            info.size = metadata.st_size
                            with _open_regular(path, "Debian package payload") as payload:
                                archive.addfile(info, payload)
                        else:  # pragma: no cover - inventory already rejects this
                            raise AdminDebError(
                                f"Debian package staging contains a special file: {path}"
                            )
            raw.flush()
            os.fsync(raw.fileno())
    except OSError as exc:
        raise AdminDebError(f"cannot create deterministic tar member: {output}") from exc
    output.chmod(0o644)


def _ar_header(name: str, size: int, source_date_epoch: int) -> bytes:
    values = (
        f"{name + '/':<16}",
        f"{source_date_epoch:<12}",
        f"{0:<6}",
        f"{0:<6}",
        f"{'100644':<8}",
        f"{size:<10}",
        "`\n",
    )
    header = "".join(values).encode("ascii")
    if len(header) != 60:
        raise AdminDebError(f"Debian ar member cannot be represented safely: {name}")
    return header


def _write_deb(
    output: Path,
    control_archive: Path,
    data_archive: Path,
    source_date_epoch: int,
) -> None:
    members: tuple[tuple[str, bytes | Path], ...] = (
        ("debian-binary", b"2.0\n"),
        ("control.tar.gz", control_archive),
        ("data.tar.gz", data_archive),
    )
    try:
        with output.open("xb") as package:
            package.write(b"!<arch>\n")
            for name, source in members:
                if isinstance(source, bytes):
                    size = len(source)
                    package.write(_ar_header(name, size, source_date_epoch))
                    package.write(source)
                else:
                    size = source.stat().st_size
                    package.write(_ar_header(name, size, source_date_epoch))
                    with _open_regular(source, "Debian ar member") as member:
                        shutil.copyfileobj(member, package, length=1024 * 1024)
                if size % 2:
                    package.write(b"\n")
            package.flush()
            os.fsync(package.fileno())
    except OSError as exc:
        raise AdminDebError(f"cannot create Debian package: {output}") from exc
    output.chmod(0o644)


def _md5sums(data_root: Path) -> str:
    _directories, files = _inventory_tree(data_root, "Debian data tree")
    entries: list[str] = []
    for path in files:
        digest = hashlib.md5(usedforsecurity=False)  # noqa: S324 - Debian's non-security metadata
        with _open_regular(path, "Debian data file") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        entries.append(f"{digest.hexdigest()}  {path.relative_to(data_root).as_posix()}")
    return "\n".join(entries) + "\n"


def _control(version: str, profile: str, installed_size: int) -> str:
    minimum_python, maximum_python = SUPPORTED_PLATFORM_PYTHON[profile]
    return (
        "Package: reticulumpi-admin\n"
        f"Version: {version}\n"
        "Section: admin\n"
        "Priority: optional\n"
        f"Architecture: {ARCHITECTURE}\n"
        "Maintainer: ReticulumPi Release Engineering <reticulumpi@users.noreply.github.com>\n"
        f"Depends: python3 (>= {minimum_python}), python3 (<< {maximum_python}), "
        "python3-venv, minisign\n"
        f"Installed-Size: {installed_size}\n"
        f"X-ReticulumPi-Platform-Profile: {profile}\n"
        "Description: isolated ReticulumPi recovery administrator\n"
        " Verifies and transactionally installs independently signed ReticulumPi releases.\n"
    )


@contextlib.contextmanager
def _publish(source: Path, destination: Path) -> Iterator[tuple[int, int]]:
    """Publish without replacement and pin the created inode for the caller's scope."""

    temporary = destination.with_name(f".{destination.name}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise AdminDebError(f"administrator artifact temporary output already exists: {temporary}")
    created = False
    linked = False
    identity: tuple[int, int] | None = None
    guard: BinaryIO | None = None
    try:
        with _open_regular(source, "built administrator artifact") as incoming:
            guard = temporary.open("xb")
            created = True
            shutil.copyfileobj(incoming, guard, length=1024 * 1024)
            guard.flush()
            os.fsync(guard.fileno())
        temporary.chmod(0o644)
        # The temporary is deliberately in the destination directory. A hard link is an atomic,
        # no-replace publication primitive: a destination created after preflight makes link(2)
        # fail instead of being overwritten.
        metadata = os.fstat(guard.fileno())
        identity = metadata.st_dev, metadata.st_ino
        os.link(temporary, destination, follow_symlinks=False)
        linked = True
        temporary.unlink()
        published = destination.lstat()
        if (published.st_dev, published.st_ino) != identity:
            raise OSError("published administrator artifact was replaced during publication")
    except OSError as exc:
        try:
            if created:
                temporary.unlink(missing_ok=True)
            if linked and identity is not None:
                _unlink_if_same(destination, identity)
        finally:
            if guard is not None:
                guard.close()
        raise AdminDebError(f"cannot publish administrator artifact: {destination}") from exc
    if identity is None:  # pragma: no cover - successful copy always records an identity
        raise AdminDebError(f"cannot identify published administrator artifact: {destination}")
    if guard is None:  # pragma: no cover - successful publication always opens the guard
        raise AdminDebError(f"cannot guard published administrator artifact: {destination}")
    try:
        yield identity
    finally:
        guard.close()


def _unlink_if_same(path: Path, identity: tuple[int, int]) -> None:
    """Remove only the exact inode this build published, never a racing replacement."""

    try:
        metadata = path.lstat()
        if (metadata.st_dev, metadata.st_ino) == identity and stat.S_ISREG(metadata.st_mode):
            path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AdminDebError(f"cannot roll back partial administrator artifact: {path}") from exc


def build_admin_deb(
    *,
    wheel: Path,
    wheel_sha256: str,
    runtime_source: Path,
    runtime_manifest: Path,
    runtime_kind: str,
    output: Path,
    version: str,
    platform_profile: str,
    source_date_epoch: int,
) -> AdminDebArtifacts:
    """Build and return the unsigned administrator package and checksum sidecar."""

    if len(version) > 128 or VERSION.fullmatch(version) is None:
        raise AdminDebError(f"version must be normalized PEP 440, got {version!r}")
    if platform_profile not in SUPPORTED_PLATFORM_PYTHON:
        raise AdminDebError(f"unsupported platform profile: {platform_profile!r}")
    if runtime_kind != "site-packages":
        raise AdminDebError(
            "recovery administrator runtime kind must be site-packages; "
            "wheelhouse inputs are unsupported"
        )
    if source_date_epoch < 0 or source_date_epoch > 0xFFFFFFFF:
        raise AdminDebError("SOURCE_DATE_EPOCH is outside the gzip timestamp range")
    if SHA256.fullmatch(wheel_sha256) is None:
        raise AdminDebError("ReticulumPi wheel SHA-256 must be 64 lowercase hexadecimal digits")
    expected_name = admin_deb_filename(version, platform_profile)
    if output.name != expected_name:
        raise AdminDebError(f"Debian package must be named {expected_name}")
    checksum = output.with_name(f"{output.name}.sha256")
    if output.exists() or output.is_symlink() or checksum.exists() or checksum.is_symlink():
        raise AdminDebError("administrator package output or checksum sidecar already exists")
    output.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if output.parent.is_symlink() or not output.parent.is_dir():
        raise AdminDebError(f"administrator package output directory is unsafe: {output.parent}")

    actual_wheel_sha256 = _sha256(
        wheel, "prevalidated ReticulumPi wheel", max_bytes=MAX_WHEEL_BYTES
    )
    if actual_wheel_sha256 != wheel_sha256:
        raise AdminDebError("prevalidated ReticulumPi wheel does not match its expected SHA-256")
    runtime_manifest_digest = _validate_empty_runtime_source(runtime_source, runtime_manifest)

    with tempfile.TemporaryDirectory(prefix="reticulumpi-admin-deb-") as raw_staging:
        staging = Path(raw_staging)
        data_root = staging / "data"
        control_root = staging / "control"
        private_site = data_root.joinpath(*SITE_PACKAGES.parts)
        private_site.mkdir(mode=0o755, parents=True)
        budget = _Budget()

        wheel_snapshot = staging / "wheel-snapshot" / wheel.name
        _snapshot_verified_file(
            wheel,
            wheel_snapshot,
            wheel_sha256,
            "prevalidated ReticulumPi wheel",
        )
        wheel_name, wheel_version = _extract_wheel(wheel_snapshot, private_site, budget)
        if wheel_name != "reticulumpi" or wheel_version != version:
            raise AdminDebError(
                "prevalidated wheel metadata must name ReticulumPi and match the package version"
            )
        _validate_admin_payload(private_site)

        _write_text(data_root.joinpath(*WRAPPER_PATH.parts), WRAPPER, 0o755)
        _write_text(data_root.joinpath(*INSTALL_ROOT.parts, "launcher.py"), LAUNCHER, 0o644)
        build_metadata = {
            "architecture": ARCHITECTURE,
            "kind": "reticulumpi-recovery-administrator",
            "platform_profile": platform_profile,
            "reticulumpi_wheel": {"filename": wheel.name, "sha256": wheel_sha256},
            "runtime_source": {
                "kind": runtime_kind,
                "sha256_manifest": runtime_manifest_digest,
            },
            "schema": 1,
            "source_date_epoch": source_date_epoch,
            "version": version,
        }
        _write_text(
            data_root.joinpath(*INSTALL_ROOT.parts, "build.json"),
            json.dumps(build_metadata, sort_keys=True, separators=(",", ":")) + "\n",
            0o644,
        )

        _directories, data_files = _inventory_tree(data_root, "Debian data tree")
        installed_size = max(1, sum((path.stat().st_size + 1023) // 1024 for path in data_files))
        _write_text(
            control_root / "control", _control(version, platform_profile, installed_size), 0o644
        )
        _write_text(control_root / "md5sums", _md5sums(data_root), 0o644)

        control_archive = staging / "control.tar.gz"
        data_archive = staging / "data.tar.gz"
        _tar_gz(control_root, control_archive, source_date_epoch)
        _tar_gz(data_root, data_archive, source_date_epoch)
        built_package = staging / output.name
        _write_deb(built_package, control_archive, data_archive, source_date_epoch)
        built_checksum = staging / checksum.name
        built_checksum.write_text(
            f"{_sha256(built_package, 'built administrator package')}  {output.name}\n",
            encoding="ascii",
        )
        built_checksum.chmod(0o644)

        with _publish(built_package, output) as output_identity:
            try:
                with _publish(built_checksum, checksum):
                    pass
            except AdminDebError:
                _unlink_if_same(output, output_identity)
                raise
    return AdminDebArtifacts(package=output, sha256=checksum)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--runtime-source", required=True, type=Path)
    parser.add_argument("--runtime-manifest", required=True, type=Path)
    parser.add_argument("--runtime-kind", required=True, choices=("site-packages",))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--platform-profile", required=True)
    parser.add_argument("--source-date-epoch", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        artifacts = build_admin_deb(
            wheel=args.wheel,
            wheel_sha256=args.wheel_sha256,
            runtime_source=args.runtime_source,
            runtime_manifest=args.runtime_manifest,
            runtime_kind=args.runtime_kind,
            output=args.output,
            version=args.version,
            platform_profile=args.platform_profile,
            source_date_epoch=args.source_date_epoch,
        )
    except AdminDebError as exc:
        parser.error(str(exc))
    print(artifacts.package)
    print(artifacts.sha256)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
