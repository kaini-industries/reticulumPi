"""Focused tests for the independently installed recovery-administrator package."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from tools import build_admin_deb


VERSION = "3.2.1"
DEV_VERSION = "0.post1.dev215+unknown.g9e100ad40.d20260711"
PROFILE = "linux-arm64-debian-bookworm-py311"
EPOCH = 1_700_000_000


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _zip_file(archive: zipfile.ZipFile, name: str, content: bytes, mode: int = 0o644) -> None:
    member = zipfile.ZipInfo(name)
    member.create_system = 3
    member.external_attr = (stat.S_IFREG | mode) << 16
    member.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(member, content)


def _wheel(
    directory: Path,
    distribution: str,
    version: str,
    files: dict[str, bytes],
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    normalized = distribution.replace("-", "_")
    path = directory / f"{normalized}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, mode="w") as archive:
        for name, content in files.items():
            _zip_file(archive, name, content)
        metadata = (f"Metadata-Version: 2.4\nName: {distribution}\nVersion: {version}\n").encode(
            "ascii"
        )
        _zip_file(archive, f"{normalized}-{version}.dist-info/METADATA", metadata)
        _zip_file(
            archive,
            f"{normalized}-{version}.dist-info/WHEEL",
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
    return path


def _reticulumpi_wheel(directory: Path, version: str = VERSION) -> Path:
    return _wheel(
        directory,
        "reticulumpi",
        version,
        {
            "reticulumpi/__init__.py": b"",
            "reticulumpi/admin_cli.py": b"def main(): return 0\n",
            "reticulumpi/cli_help.py": b"class StableHelpFormatter: pass\n",
            # admin_cli imports this stdlib-only module in the independently packaged runtime.
            "reticulumpi/platform_policy.py": b"PROFILE = 'bookworm'\n",
        },
    )


def test_recovery_extractor_ignores_hash_pinned_wheel_data_scheme(tmp_path: Path) -> None:
    wheel = _wheel(
        tmp_path / "python",
        "reticulumpi",
        VERSION,
        {
            "reticulumpi/__init__.py": b"",
            "reticulumpi/admin_cli.py": b"def main(): return 0\n",
            "reticulumpi/cli_help.py": b"class StableHelpFormatter: pass\n",
            "reticulumpi/platform_policy.py": b"PROFILE = 'bookworm'\n",
            f"reticulumpi-{VERSION}.data/data/share/reticulumpi/page.mu": b"fixture\n",
        },
    )
    artifact = _build(tmp_path, parent="wheel-data-scheme", wheel=wheel)
    data = _tar_members(_ar_members(artifact.package)["data.tar.gz"])
    assert "usr/share/reticulumpi/page.mu" not in data


def _manifest(root: Path, destination: Path) -> Path:
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            entries.append(f"{_sha256(path)}  {path.relative_to(root).as_posix()}")
    destination.write_text("\n".join(entries) + ("\n" if entries else ""), encoding="ascii")
    return destination


def _ar_members(package: Path) -> dict[str, bytes]:
    raw = package.read_bytes()
    assert raw[:8] == b"!<arch>\n"
    offset = 8
    members: dict[str, bytes] = {}
    while offset < len(raw):
        header = raw[offset : offset + 60]
        assert len(header) == 60
        assert header[58:] == b"`\n"
        name = header[:16].decode("ascii").strip().removesuffix("/")
        size = int(header[48:58].decode("ascii").strip())
        offset += 60
        members[name] = raw[offset : offset + size]
        offset += size + (size % 2)
    return members


def _tar_members(payload: bytes) -> dict[str, tuple[tarfile.TarInfo, bytes]]:
    result: dict[str, tuple[tarfile.TarInfo, bytes]] = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive:
            name = member.name.removeprefix("./")
            content = b""
            if member.isfile():
                extracted = archive.extractfile(member)
                assert extracted is not None
                content = extracted.read()
            result[name] = (member, content)
    return result


def _tar_typeflags(payload: bytes) -> list[bytes]:
    raw = gzip.decompress(payload)
    result: list[bytes] = []
    offset = 0
    while offset + tarfile.BLOCKSIZE <= len(raw):
        header = raw[offset : offset + tarfile.BLOCKSIZE]
        if not any(header):
            break
        result.append(header[156:157])
        size_field = header[124:136].rstrip(b"\0 ") or b"0"
        size = int(size_field, 8)
        offset += (
            tarfile.BLOCKSIZE
            + ((size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE
        )
    return result


def _build(
    tmp_path: Path,
    *,
    parent: str,
    runtime_kind: str = "site-packages",
    runtime_source: Path | None = None,
    runtime_manifest: Path | None = None,
    wheel: Path | None = None,
    platform_profile: str = PROFILE,
    source_date_epoch: int = EPOCH,
    version: str = VERSION,
) -> build_admin_deb.AdminDebArtifacts:
    wheel = wheel or _reticulumpi_wheel(tmp_path / "python", version)
    if runtime_source is None:
        runtime_source = tmp_path / "site"
        runtime_source.mkdir(exist_ok=True)
    if runtime_manifest is None:
        runtime_manifest = _manifest(runtime_source, tmp_path / f"{parent}.SHA256SUMS")
    output_directory = tmp_path / parent
    output_directory.mkdir()
    output = output_directory / build_admin_deb.admin_deb_filename(version, platform_profile)
    return build_admin_deb.build_admin_deb(
        wheel=wheel,
        wheel_sha256=_sha256(wheel),
        runtime_source=runtime_source,
        runtime_manifest=runtime_manifest,
        runtime_kind=runtime_kind,
        output=output,
        version=version,
        platform_profile=platform_profile,
        source_date_epoch=source_date_epoch,
    )


def test_admin_deb_is_deterministic_isolated_and_minisign_ready(tmp_path: Path) -> None:
    wheel = _reticulumpi_wheel(tmp_path / "python")
    runtime = tmp_path / "site"
    runtime.mkdir()
    manifest = _manifest(runtime, tmp_path / "runtime.SHA256SUMS")

    first = _build(
        tmp_path,
        parent="first",
        runtime_source=runtime,
        runtime_manifest=manifest,
        wheel=wheel,
    )
    second = _build(
        tmp_path,
        parent="second",
        runtime_source=runtime,
        runtime_manifest=manifest,
        wheel=wheel,
    )

    assert first.package.read_bytes() == second.package.read_bytes()
    assert first.sha256.read_text(encoding="ascii") == (
        f"{_sha256(first.package)}  {first.package.name}\n"
    )
    assert not list(first.package.parent.glob("*.minisig"))
    assert not list(first.package.parent.glob(".*.tmp"))
    assert not list(second.package.parent.glob(".*.tmp"))

    ar = _ar_members(first.package)
    assert list(ar) == ["debian-binary", "control.tar.gz", "data.tar.gz"]
    assert ar["debian-binary"] == b"2.0\n"
    control = _tar_members(ar["control.tar.gz"])
    control_text = control["control"][1].decode("utf-8")
    assert "Package: reticulumpi-admin\n" in control_text
    assert f"Version: {VERSION}\n" in control_text
    assert "Architecture: arm64\n" in control_text
    assert f"X-ReticulumPi-Platform-Profile: {PROFILE}\n" in control_text
    assert (
        "Maintainer: ReticulumPi Release Engineering "
        "<reticulumpi@users.noreply.github.com>\n" in control_text
    )
    assert "Depends: python3 (>= 3.11), python3 (<< 3.12), python3-venv, minisign\n" in control_text

    data = _tar_members(ar["data.tar.gz"])
    wrapper_info, wrapper_bytes = data["usr/sbin/reticulumpi-admin"]
    wrapper = wrapper_bytes.decode("utf-8")
    assert wrapper == (
        '#!/bin/sh\nexec /usr/bin/python3 -I -S /usr/lib/reticulumpi-admin/launcher.py "$@"\n'
    )
    assert wrapper_info.mode == 0o755
    assert wrapper_info.uid == wrapper_info.gid == 0
    assert wrapper_info.uname == wrapper_info.gname == "root"
    assert "PYTHONPATH" not in wrapper
    assert "$PATH" not in wrapper
    assert "/opt/reticulumpi/current" not in wrapper

    launcher = data["usr/lib/reticulumpi-admin/launcher.py"][1].decode("utf-8")
    assert 'PRIVATE_SITE = "/usr/lib/reticulumpi-admin/site-packages"' in launcher
    assert "sys.flags.isolated" in launcher
    assert "sys.flags.no_site" in launcher
    assert "from reticulumpi import admin_cli" in launcher
    assert "sys.path[:] = [*stdlib, PRIVATE_SITE]" in launcher
    assert "current" in launcher  # explicit documented exclusion, never an import path
    assert (
        data["usr/lib/reticulumpi-admin/site-packages/reticulumpi/platform_policy.py"][1]
        == b"PROFILE = 'bookworm'\n"
    )
    assert (
        data["usr/lib/reticulumpi-admin/site-packages/reticulumpi/admin_cli.py"][1]
        == b"def main(): return 0\n"
    )
    assert "usr/lib/reticulumpi-admin/site-packages/offline_dependency.py" not in data
    metadata = json.loads(data["usr/lib/reticulumpi-admin/build.json"][1])
    assert metadata["architecture"] == "arm64"
    assert metadata["platform_profile"] == PROFILE
    assert metadata["reticulumpi_wheel"] == {
        "filename": wheel.name,
        "sha256": _sha256(wheel),
    }
    assert metadata["runtime_source"] == {
        "kind": "site-packages",
        "sha256_manifest": hashlib.sha256(b"").hexdigest(),
    }
    assert all(member.mtime == EPOCH for member, _content in data.values())


def test_debian_tar_members_never_use_pax_extended_headers(tmp_path: Path) -> None:
    wheel = _reticulumpi_wheel(tmp_path / "python")
    long_member = f"reticulumpi/{'long_module_name_' * 8}.py"
    with zipfile.ZipFile(wheel, mode="a") as archive:
        _zip_file(archive, long_member, b"VALUE = 1\n")

    artifact = _build(tmp_path, parent="debian-compatible-tar", wheel=wheel)
    ar = _ar_members(artifact.package)

    for name in ("control.tar.gz", "data.tar.gz"):
        typeflags = _tar_typeflags(ar[name])
        assert b"x" not in typeflags
        assert b"g" not in typeflags
    assert b"L" in _tar_typeflags(ar["data.tar.gz"])
    data = _tar_members(ar["data.tar.gz"])
    installed_member = f"usr/lib/reticulumpi-admin/site-packages/{long_member}"
    assert data[installed_member][1] == b"VALUE = 1\n"


@pytest.mark.parametrize(
    ("profile", "dependency"),
    [
        (
            "linux-arm64-debian-bookworm-py311",
            "python3 (>= 3.11), python3 (<< 3.12), python3-venv, minisign",
        ),
        (
            "linux-arm64-ubuntu-noble-py312",
            "python3 (>= 3.12), python3 (<< 3.13), python3-venv, minisign",
        ),
    ],
)
def test_debian_python_dependency_matches_platform_profile(
    tmp_path: Path, profile: str, dependency: str
) -> None:
    artifact = _build(tmp_path, parent=profile, platform_profile=profile)
    control = _tar_members(_ar_members(artifact.package)["control.tar.gz"])["control"][1]
    assert f"Depends: {dependency}\n".encode() in control


def test_unknown_platform_profile_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(build_admin_deb.AdminDebError, match="unsupported platform profile"):
        _build(tmp_path, parent="unknown-profile", platform_profile="linux-arm64-unknown-py399")


def test_platform_profiles_have_noncolliding_filenames() -> None:
    names = {
        build_admin_deb.admin_deb_filename(VERSION, profile)
        for profile in build_admin_deb.SUPPORTED_PLATFORM_PYTHON
    }
    assert len(names) == len(build_admin_deb.SUPPORTED_PLATFORM_PYTHON)
    assert all(
        profile in build_admin_deb.admin_deb_filename(VERSION, profile)
        for profile in build_admin_deb.SUPPORTED_PLATFORM_PYTHON
    )


def test_normalized_setuptools_scm_development_version_builds(tmp_path: Path) -> None:
    wheel = _reticulumpi_wheel(tmp_path / "python", DEV_VERSION)
    artifact = _build(
        tmp_path,
        parent="development-version",
        wheel=wheel,
        version=DEV_VERSION,
    )

    assert artifact.package.name == build_admin_deb.admin_deb_filename(DEV_VERSION, PROFILE)
    data = _tar_members(_ar_members(artifact.package)["data.tar.gz"])
    metadata = json.loads(data["usr/lib/reticulumpi-admin/build.json"][1])
    assert metadata["version"] == DEV_VERSION
    control = _tar_members(_ar_members(artifact.package)["control.tar.gz"])["control"][1]
    assert f"Version: {DEV_VERSION}\n".encode() in control


@pytest.mark.parametrize(
    "version",
    ["v1.2.3", "1/2", "1.2.3\nInjected: yes", "1.2.3+LOCAL", "1.2.3+local-label"],
)
def test_noncanonical_or_unsafe_version_is_rejected(tmp_path: Path, version: str) -> None:
    wheel = _reticulumpi_wheel(tmp_path / "python", VERSION)
    with pytest.raises(build_admin_deb.AdminDebError, match="normalized PEP 440"):
        _build(
            tmp_path,
            parent="invalid-version",
            wheel=wheel,
            version=version,
        )


@pytest.mark.parametrize("entry_kind", ["file", "directory", "symlink"])
def test_runtime_source_must_be_exactly_empty(tmp_path: Path, entry_kind: str) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    if entry_kind == "file":
        (runtime / "dependency.py").write_text("VALUE = 1\n", encoding="ascii")
    elif entry_kind == "directory":
        (runtime / "empty-directory").mkdir()
    else:
        (runtime / "alias").symlink_to(tmp_path)
    manifest = tmp_path / "runtime.SHA256SUMS"
    manifest.write_bytes(b"")

    with pytest.raises(build_admin_deb.AdminDebError, match="runtime source must be exactly empty"):
        _build(
            tmp_path,
            parent=f"nonempty-runtime-{entry_kind}",
            runtime_source=runtime,
            runtime_manifest=manifest,
        )


def test_runtime_manifest_must_be_exactly_empty(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    manifest = tmp_path / "runtime.SHA256SUMS"
    manifest.write_text(f"{'0' * 64}  dependency.py\n", encoding="ascii")

    with pytest.raises(build_admin_deb.AdminDebError, match="manifest must be exactly empty"):
        _build(
            tmp_path,
            parent="nonempty-manifest",
            runtime_source=runtime,
            runtime_manifest=manifest,
        )


def test_wheelhouse_runtime_kind_is_rejected_even_when_empty(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    manifest = _manifest(runtime, tmp_path / "runtime.SHA256SUMS")

    with pytest.raises(build_admin_deb.AdminDebError, match="wheelhouse inputs are unsupported"):
        _build(
            tmp_path,
            parent="wheelhouse",
            runtime_source=runtime,
            runtime_manifest=manifest,
            runtime_kind="wheelhouse",
        )


@pytest.mark.parametrize("unsafe_name", ["../escape.py", "/absolute.py", "dir\\escape.py"])
def test_wheel_rejects_unsafe_member_paths(tmp_path: Path, unsafe_name: str) -> None:
    wheel = _reticulumpi_wheel(tmp_path / "python")
    with zipfile.ZipFile(wheel, mode="a") as archive:
        _zip_file(archive, unsafe_name, b"unsafe\n")

    with pytest.raises(build_admin_deb.AdminDebError, match="unsafe path"):
        _build(tmp_path, parent="unsafe-wheel", wheel=wheel)


def test_wheel_rejects_symlink_members(tmp_path: Path) -> None:
    wheel = _reticulumpi_wheel(tmp_path / "python")
    member = zipfile.ZipInfo("reticulumpi/linked.py")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(wheel, mode="a") as archive:
        archive.writestr(member, "../../candidate/admin_cli.py")

    with pytest.raises(build_admin_deb.AdminDebError, match="symlink or special"):
        _build(tmp_path, parent="symlink-wheel", wheel=wheel)


def test_wheel_size_limit_is_checked_before_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = _reticulumpi_wheel(tmp_path / "python")
    monkeypatch.setattr(build_admin_deb, "MAX_WHEEL_BYTES", 1)

    with pytest.raises(build_admin_deb.AdminDebError, match="wheel exceeds the size limit"):
        _build(tmp_path, parent="oversize-wheel", wheel=wheel)


def test_wheel_metadata_has_a_bounded_read(tmp_path: Path) -> None:
    wheel = tmp_path / "python" / f"reticulumpi-{VERSION}-py3-none-any.whl"
    wheel.parent.mkdir()
    dist_info = f"reticulumpi-{VERSION}.dist-info"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        for name, content in {
            "reticulumpi/__init__.py": b"",
            "reticulumpi/admin_cli.py": b"def main(): return 0\n",
            "reticulumpi/cli_help.py": b"class StableHelpFormatter: pass\n",
            "reticulumpi/platform_policy.py": b"PROFILE = 'bookworm'\n",
        }.items():
            _zip_file(archive, name, content)
        metadata = (
            f"Metadata-Version: 2.4\nName: reticulumpi\nVersion: {VERSION}\nSummary: ".encode()
            + b"x" * build_admin_deb.MAX_METADATA_BYTES
        )
        _zip_file(archive, f"{dist_info}/METADATA", metadata)
        _zip_file(
            archive,
            f"{dist_info}/WHEEL",
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )

    with pytest.raises(build_admin_deb.AdminDebError, match="metadata exceeds the size limit"):
        _build(tmp_path, parent="oversize-metadata", wheel=wheel)


def test_wheel_must_contain_the_complete_admin_import_surface(tmp_path: Path) -> None:
    wheel = _wheel(
        tmp_path / "python",
        "reticulumpi",
        VERSION,
        {
            "reticulumpi/__init__.py": b"",
            "reticulumpi/admin_cli.py": b"def main(): return 0\n",
            "reticulumpi/cli_help.py": b"class StableHelpFormatter: pass\n",
        },
    )

    with pytest.raises(build_admin_deb.AdminDebError, match="platform_policy.py"):
        _build(tmp_path, parent="incomplete-wheel", wheel=wheel)


def test_builder_rejects_wheel_hash_or_output_reuse(tmp_path: Path) -> None:
    wheel = _reticulumpi_wheel(tmp_path / "python")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    manifest = _manifest(runtime, tmp_path / "runtime.SHA256SUMS")
    output_directory = tmp_path / "output"
    output_directory.mkdir()
    output = output_directory / build_admin_deb.admin_deb_filename(VERSION, PROFILE)
    arguments = {
        "wheel": wheel,
        "wheel_sha256": "0" * 64,
        "runtime_source": runtime,
        "runtime_manifest": manifest,
        "runtime_kind": "site-packages",
        "output": output,
        "version": VERSION,
        "platform_profile": PROFILE,
        "source_date_epoch": EPOCH,
    }
    with pytest.raises(build_admin_deb.AdminDebError, match="expected SHA-256"):
        build_admin_deb.build_admin_deb(**arguments)

    arguments["wheel_sha256"] = _sha256(wheel)
    output.write_bytes(b"do not replace")
    with pytest.raises(build_admin_deb.AdminDebError, match="already exists"):
        build_admin_deb.build_admin_deb(**arguments)
    assert output.read_bytes() == b"do not replace"

    output.unlink()
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_bytes(b"unrelated in-progress artifact")
    with pytest.raises(build_admin_deb.AdminDebError, match="temporary output already exists"):
        build_admin_deb.build_admin_deb(**arguments)
    assert temporary.read_bytes() == b"unrelated in-progress artifact"
    assert not output.exists()


def test_source_date_epoch_accepts_gzip_boundary_and_rejects_overflow(tmp_path: Path) -> None:
    artifact = _build(
        tmp_path,
        parent="gzip-boundary",
        source_date_epoch=0xFFFFFFFF,
    )
    assert artifact.package.is_file()

    with pytest.raises(build_admin_deb.AdminDebError, match="gzip timestamp range"):
        _build(
            tmp_path,
            parent="gzip-overflow",
            source_date_epoch=0x100000000,
        )


def test_checksum_publication_race_rolls_back_only_our_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = _reticulumpi_wheel(tmp_path / "python")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    manifest = _manifest(runtime, tmp_path / "runtime.SHA256SUMS")
    output_directory = tmp_path / "output"
    output_directory.mkdir()
    output = output_directory / build_admin_deb.admin_deb_filename(VERSION, PROFILE)
    checksum = output.with_name(f"{output.name}.sha256")
    real_link = os.link
    calls = 0

    def race_checksum(source: Path, destination: Path, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            checksum.write_bytes(b"racing checksum")
        real_link(source, destination, **kwargs)

    monkeypatch.setattr(build_admin_deb.os, "link", race_checksum)
    with pytest.raises(build_admin_deb.AdminDebError, match="cannot publish"):
        build_admin_deb.build_admin_deb(
            wheel=wheel,
            wheel_sha256=_sha256(wheel),
            runtime_source=runtime,
            runtime_manifest=manifest,
            runtime_kind="site-packages",
            output=output,
            version=VERSION,
            platform_profile=PROFILE,
            source_date_epoch=EPOCH,
        )
    assert not output.exists()
    assert checksum.read_bytes() == b"racing checksum"
    assert not output.with_name(f".{output.name}.tmp").exists()
    assert not checksum.with_name(f".{checksum.name}.tmp").exists()


def test_package_publication_race_never_overwrites_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = _reticulumpi_wheel(tmp_path / "python")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    manifest = _manifest(runtime, tmp_path / "runtime.SHA256SUMS")
    output_directory = tmp_path / "output"
    output_directory.mkdir()
    output = output_directory / build_admin_deb.admin_deb_filename(VERSION, PROFILE)
    replacement = b"destination created after preflight"
    real_link = os.link

    def race_package(source: Path, destination: Path, **kwargs: object) -> None:
        destination.write_bytes(replacement)
        real_link(source, destination, **kwargs)

    monkeypatch.setattr(build_admin_deb.os, "link", race_package)
    with pytest.raises(build_admin_deb.AdminDebError, match="cannot publish"):
        build_admin_deb.build_admin_deb(
            wheel=wheel,
            wheel_sha256=_sha256(wheel),
            runtime_source=runtime,
            runtime_manifest=manifest,
            runtime_kind="site-packages",
            output=output,
            version=VERSION,
            platform_profile=PROFILE,
            source_date_epoch=EPOCH,
        )
    assert output.read_bytes() == replacement
    assert not output.with_name(f"{output.name}.sha256").exists()
    assert not output.with_name(f".{output.name}.tmp").exists()


def test_checksum_race_does_not_remove_a_replacement_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = _reticulumpi_wheel(tmp_path / "python")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    manifest = _manifest(runtime, tmp_path / "runtime.SHA256SUMS")
    output_directory = tmp_path / "output"
    output_directory.mkdir()
    output = output_directory / build_admin_deb.admin_deb_filename(VERSION, PROFILE)
    checksum = output.with_name(f"{output.name}.sha256")
    replacement = b"racing replacement package"
    real_link = os.link
    calls = 0

    def replace_before_checksum(source: Path, destination: Path, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            output.unlink()
            output.write_bytes(replacement)
            checksum.write_bytes(b"racing checksum")
        real_link(source, destination, **kwargs)

    monkeypatch.setattr(build_admin_deb.os, "link", replace_before_checksum)
    with pytest.raises(build_admin_deb.AdminDebError, match="cannot publish"):
        build_admin_deb.build_admin_deb(
            wheel=wheel,
            wheel_sha256=_sha256(wheel),
            runtime_source=runtime,
            runtime_manifest=manifest,
            runtime_kind="site-packages",
            output=output,
            version=VERSION,
            platform_profile=PROFILE,
            source_date_epoch=EPOCH,
        )
    assert output.read_bytes() == replacement
    assert checksum.read_bytes() == b"racing checksum"
    assert not output.with_name(f".{output.name}.tmp").exists()
    assert not checksum.with_name(f".{checksum.name}.tmp").exists()
