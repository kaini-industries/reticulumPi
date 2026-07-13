"""Security and transactional regression tests for ``reticulumpi-admin``."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import tarfile
from contextlib import closing, nullcontext
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

import reticulumpi.admin_cli as admin
from reticulumpi.external_artifacts import ArtifactPolicyError
from reticulumpi.migrations import Migration, MigrationError, MigrationTarget


@pytest.fixture
def admin_paths(tmp_path, monkeypatch):
    paths = SimpleNamespace(
        root=tmp_path / "opt/reticulumpi",
        config=tmp_path / "etc/reticulumpi",
        data=tmp_path / "var/lib/reticulumpi",
        cache=tmp_path / "var/cache/reticulumpi",
        backups=tmp_path / "var/backups/reticulumpi",
        run=tmp_path / "run/reticulumpi",
        systemd=tmp_path / "etc/systemd/system",
        libexec=tmp_path / "usr/libexec/reticulumpi",
        shared=tmp_path / "usr/share/reticulumpi/config",
        sudoers=tmp_path / "etc/sudoers.d",
        chrony_config=tmp_path / "etc/chrony/conf.d/reticulumpi-gps.conf",
        captive_dnsmasq=tmp_path / "etc/dnsmasq.d/reticulumpi-captive-portal.conf",
        home=tmp_path / "home/reticulumpi",
    )
    monkeypatch.setattr(admin, "CONFIG_DIR", paths.config)
    monkeypatch.setattr(admin, "CONFIG_FILE", paths.config / "config.yaml")
    monkeypatch.setattr(admin, "DATA_DIR", paths.data)
    monkeypatch.setattr(admin, "CACHE_DIR", paths.cache)
    monkeypatch.setattr(admin, "BACKUP_DIR", paths.backups)
    monkeypatch.setattr(admin, "RUN_DIR", paths.run)
    monkeypatch.setattr(admin, "SYSTEMD_DIR", paths.systemd)
    monkeypatch.setattr(admin, "LIBEXEC_DIR", paths.libexec)
    monkeypatch.setattr(admin, "SHARED_CONFIG_DIR", paths.shared)
    monkeypatch.setattr(admin, "SUDOERS_DIR", paths.sudoers)
    monkeypatch.setattr(admin, "CHRONY_CONFIG_FILE", paths.chrony_config)
    monkeypatch.setattr(admin, "CAPTIVE_DNSMASQ_CONFIG_FILE", paths.captive_dnsmasq)
    monkeypatch.setattr(admin, "MANIFEST_FILE", paths.config / "install.json")
    monkeypatch.setattr(admin, "ADMIN_STATE_DIR", paths.backups / "admin")
    monkeypatch.setattr(admin, "JOURNAL_FILE", paths.backups / "admin/transaction.json")
    monkeypatch.setattr(admin, "LOCK_FILE", tmp_path / "run/lock/maintenance.lock")
    monkeypatch.setattr(admin, "_preflight_platform", lambda: {})
    monkeypatch.setattr(admin, "_wait_service_inactive", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(admin, "_wait_dashboard_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(admin, "_validate_install_root_ancestry", lambda _path: None)
    monkeypatch.setattr(admin, "_validate_release_immutability", lambda _release: None)
    monkeypatch.setattr(admin, "_verify_minisign", lambda *_args: None)
    monkeypatch.setattr(admin.os, "fchown", lambda *_args: None)
    monkeypatch.setattr(
        admin.pwd,
        "getpwnam",
        lambda _name: SimpleNamespace(
            pw_dir=str(paths.home),
            pw_uid=os.getuid(),
            pw_gid=os.getgid(),
        ),
    )
    return paths


def _source_bundle(path: Path, version: str = "0.3.0", *, complete: bool = True) -> Path:
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "reticulumpi"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    if complete:
        for directory in ("src", "systemd", "config/reticulumpi", "scripts"):
            (path / directory).mkdir(parents=True, exist_ok=True)
        (path / "config/reticulumpi/config.example.yaml").write_text(
            "reticulumpi: {}\n", encoding="utf-8"
        )
        _write_fixture_dependency_profiles(path)
        (path / admin.BUNDLE_SIGNATURE_NAME).write_text("fixture signature\n", encoding="utf-8")
    return path


def _write_fixture_dependency_profiles(
    directory: Path, profiles: dict[str, str] | None = None
) -> None:
    profiles = admin._DEPENDENCY_PROFILES if profiles is None else profiles
    constraints = directory / "constraints"
    constraints.mkdir(exist_ok=True)
    entries = []
    for name in profiles.values():
        profile = constraints / name
        profile.write_text(
            f"fixture==1.0 --hash=sha256:{'a' * 64}\n",
            encoding="utf-8",
        )
        entries.append(f"{admin._sha256(profile)}  constraints/{name}")
    (directory / admin.BUNDLE_MANIFEST_NAME).write_text("\n".join(entries) + "\n", encoding="utf-8")


def _rename_fixture_dependency_profiles(
    directory: Path, source: dict[str, str], destination: dict[str, str]
) -> None:
    for profile_name, source_name in source.items():
        (directory / "constraints" / source_name).rename(
            directory / "constraints" / destination[profile_name]
        )


def _complete_source_bundle(path: Path, version: str = "0.3.0") -> Path:
    source = _source_bundle(path, version)
    (source / "config/reticulum").mkdir(parents=True)
    (source / "config/reticulum/config.example").write_text(
        "[reticulum]\n  enable_transport = No\n", encoding="utf-8"
    )
    (source / "config/reticulumpi/config.example.yaml").write_text(
        "reticulumpi:\n  plugins:\n    file_transfer:\n      enabled: false\n",
        encoding="utf-8",
    )
    (source / "config/reticulumpi/offline_profile.yaml").write_text(
        "offline:\n  forced: true\n", encoding="utf-8"
    )
    for name in admin._MANAGED_UNIT_NAMES:
        command = (
            f"ExecStart={admin.DEFAULT_CURRENT_PREFIX}/.venv/bin/python "
            "-I -m reticulumpi.control_broker"
            if name == "reticulumpi-control@.service"
            else f"ExecStart={admin.DEFAULT_CURRENT_PREFIX}/.venv/bin/reticulumpi"
        )
        (source / "systemd" / name).write_text(
            f"[Service]\n{command}\n",
            encoding="utf-8",
        )
    for name in admin._MANAGED_HELPER_NAMES:
        helper = source / "scripts" / name
        helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        helper.chmod(0o755)
    return source


def _sign_source(source: Path) -> None:
    entries = []
    for path in sorted(source.rglob("*")):
        if path.is_file() and path.name not in {
            admin.BUNDLE_MANIFEST_NAME,
            admin.BUNDLE_SIGNATURE_NAME,
        }:
            entries.append(f"{admin._sha256(path)}  {path.relative_to(source).as_posix()}")
    (source / admin.BUNDLE_MANIFEST_NAME).write_text("\n".join(entries) + "\n", encoding="utf-8")
    (source / admin.BUNDLE_SIGNATURE_NAME).write_text("trusted signature\n", encoding="utf-8")


def _valid_manifest(paths, version: str = "0.3.0") -> dict[str, object]:
    release = paths.root / "releases" / version
    return {
        "schema": 1,
        "version": version,
        "install_root": str(paths.root),
        "release": str(release),
        "previous_release": None,
        "features": ["dashboard"],
        "installed_at": "2026-07-11T00:00:00Z",
    }


def _wheel(
    path: Path,
    version: str = "0.3.0",
    *,
    name: str = "reticulumpi",
    dependency_profiles: dict[str, str] | None = None,
) -> Path:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            f"reticulumpi-{version}.dist-info/METADATA",
            f"Name: {name}\nVersion: {version}\n",
        )
    _write_fixture_dependency_profiles(path.parent, dependency_profiles)
    manifest = path.parent / admin.BUNDLE_MANIFEST_NAME
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + f"{admin._sha256(path)}  {path.name}\n",
        encoding="utf-8",
    )
    (path.parent / admin.BUNDLE_SIGNATURE_NAME).write_text("fixture signature\n", encoding="utf-8")
    return path


def _dashboard_wheel(path: Path, version: str = "0.3.0") -> Path:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            f"reticulumpi-{version}.dist-info/METADATA",
            f"Name: reticulumpi\nVersion: {version}\n",
        )
        for name in ("index.html", "style.css", "sw.js"):
            archive.writestr(f"reticulumpi/builtin_plugins/web_dashboard/static/{name}", name)
    return path


def _release(root: Path, version: str) -> Path:
    release = root / "releases" / version
    executable = release / ".venv/bin/reticulumpi"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    (release / "RELEASE").write_text(version + "\n", encoding="utf-8")
    return release


def _write_install_manifest(paths, release: Path, previous: Path | None, features=()) -> None:
    paths.config.mkdir(parents=True, exist_ok=True)
    admin._atomic_json(
        admin.MANIFEST_FILE,
        {
            "schema": 1,
            "version": (release / "RELEASE").read_text(encoding="utf-8").strip(),
            "install_root": str(paths.root),
            "release": str(release),
            "previous_release": str(previous) if previous else None,
            "features": list(features),
            "installed_at": "2026-07-11T00:00:00Z",
            "bundle_sha256": "a" * 64,
        },
    )


def _mock_apply_runtime(monkeypatch, wheel: Path, *, active=(), enabled=()):
    active_names = set(active)
    enabled_names = set(enabled)
    commands: list[list[str]] = []

    def run(command, **_kwargs):
        commands.append(command)
        if len(command) >= 4 and command[:3] == [admin.sys.executable, "-m", "venv"]:
            release = Path(command[3])
            bin_dir = release / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            for name in ("pip", "reticulumpi"):
                executable = bin_dir / name
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o755)
            (bin_dir / "python-link").symlink_to(bin_dir / "reticulumpi")
        return ""

    monkeypatch.setattr(admin, "_require_root", lambda: None)
    monkeypatch.setattr(admin, "_verify_signed_bundle", lambda *_args: None)
    monkeypatch.setattr(admin, "_ensure_install_space", lambda *_args: None)
    monkeypatch.setattr(admin, "_maintenance_lock", lambda: nullcontext())
    monkeypatch.setattr(admin, "_service_active", lambda name: name in active_names)
    monkeypatch.setattr(admin, "_unit_enabled", lambda name: name in enabled_names)
    monkeypatch.setattr(admin, "_build_wheel", lambda *_args: wheel)
    monkeypatch.setattr(admin, "_run", run)
    monkeypatch.setattr(admin, "_wait_service_active", lambda *_args: None)
    monkeypatch.setattr(admin, "_wait_service_inactive", lambda *_args: None)
    monkeypatch.setattr(admin, "_wait_dashboard_ready", lambda *_args: None)
    monkeypatch.setattr(admin, "_activate_application", lambda *_args: None)
    monkeypatch.setattr(admin.os, "chown", lambda *_args: None)
    return commands


def _migration_target(path: Path) -> MigrationTarget:
    return MigrationTarget(
        "records",
        path,
        (Migration(1, "create records", ("CREATE TABLE records(value TEXT)",)),),
    )


def _empty_backup_metadata(features=()) -> dict[str, object]:
    names = ["etc", "data", "legacy-home-reticulum", "legacy-home-config", "legacy-home-data"]
    if "nomadnet" in features:
        names.extend(("legacy-home-nomadnet", "legacy-home-nomadnet-tui"))
    return {
        "schema": 2,
        "features": list(features),
        "state_roots": [{"name": name, "present": False, "manifest": []} for name in names],
        "databases": [],
        "identity_hashes": {},
    }


def test_command_runner_and_json_helpers(tmp_path):
    assert admin._run(["/bin/echo", " ready "], capture=True) == "ready"
    path = tmp_path / "state/value.json"
    admin._atomic_json(path, {"ok": True}, 0o600)
    assert admin._read_json_object(path, "state") == {"ok": True}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    path.write_text("[]", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="JSON object"):
        admin._read_json_object(path, "state")
    path.write_text("{", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="invalid state"):
        admin._read_json_object(path, "state")


def test_privileged_file_helpers_reject_files_and_symlinks(tmp_path):
    regular = tmp_path / "regular"
    regular.write_text("not a directory", encoding="utf-8")
    with pytest.raises((admin.AdminError, FileExistsError)):
        admin._ensure_real_directory(regular)

    target = tmp_path / "target"
    target.write_text("old", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(admin.AdminError, match="symlink"):
        admin._atomic_write(link, b"new", 0o600)
    with pytest.raises(admin.AdminError, match="regular file"):
        admin._atomic_copy(tmp_path / "missing", tmp_path / "copy", 0o600)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.pop("version"), "missing"),
        (lambda value: value.update(schema=7), "schema"),
        (lambda value: value.update(features="dashboard"), "features"),
        (lambda value: value.update(installed_at=""), "installed_at"),
        (lambda value: value.update(features=["impossible"]), "unknown features"),
    ],
)
def test_manifest_validation_rejects_malformed_fields(admin_paths, mutation, message):
    value = _valid_manifest(admin_paths)
    mutation(value)
    with pytest.raises(admin.AdminError, match=message):
        admin._validate_manifest(value, admin_paths.root.resolve())


def test_manifest_validation_normalizes_and_checks_requested_root(admin_paths, tmp_path):
    value = _valid_manifest(admin_paths)
    value["features"] = ["dashboard", "dashboard"]
    value["previous_release"] = str(admin_paths.root / "releases/0.2.5")
    normalized = admin._validate_manifest(value, admin_paths.root.resolve())
    assert normalized["features"] == ("dashboard",)
    assert normalized["previous_release"].endswith("/releases/0.2.5")

    with pytest.raises(admin.AdminError, match="does not match"):
        admin._validate_manifest(value, (tmp_path / "other/root").resolve())
    value["previous_release"] = str(tmp_path / "outside")
    with pytest.raises(admin.AdminError, match="previous release is outside"):
        admin._validate_manifest(value)


def test_manifest_load_requires_regular_file(admin_paths):
    with pytest.raises(admin.AdminError, match="no valid installation manifest"):
        admin._load_manifest()
    admin_paths.config.mkdir(parents=True)
    admin.MANIFEST_FILE.symlink_to(admin_paths.config / "elsewhere")
    with pytest.raises(admin.AdminError, match="no valid installation manifest"):
        admin._load_manifest()


def test_maintenance_lock_and_root_enforcement(admin_paths, monkeypatch):
    with admin._maintenance_lock():
        assert admin.LOCK_FILE.exists()

    monkeypatch.setattr(
        admin.fcntl, "flock", lambda *_args: (_ for _ in ()).throw(BlockingIOError())
    )
    with pytest.raises(admin.AdminError, match="another ReticulumPi maintenance"):
        with admin._maintenance_lock():
            pass

    monkeypatch.setattr(admin.os, "geteuid", lambda: 1000)
    with pytest.raises(admin.AdminError, match="requires root"):
        admin._require_root()


def test_generated_scm_metadata_is_data_only(tmp_path):
    source = _source_bundle(tmp_path / "source")
    version_file = source / "src/reticulumpi/_version.py"
    version_file.parent.mkdir(parents=True, exist_ok=True)
    version_file.write_text(
        '__version__ = "0.3.2"\nraise RuntimeError("must not execute")\n', encoding="utf-8"
    )
    assert admin._generated_scm_version(source) == "0.3.2"

    version_file.write_text('__version__ = "0.3.1"\n__version__ = "0.3.2"\n', encoding="utf-8")
    with pytest.raises(admin.AdminError, match="conflicting"):
        admin._generated_scm_version(source)
    version_file.write_text("this is invalid python =", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="invalid generated"):
        admin._generated_scm_version(source)
    version_file.unlink()
    assert admin._generated_scm_version(source) is None


def test_source_metadata_dynamic_wheel_and_errors(tmp_path):
    source = _source_bundle(tmp_path / "dynamic")
    (source / "pyproject.toml").write_text(
        '[project]\nname = "reticulumpi"\ndynamic = ["version"]\n', encoding="utf-8"
    )
    generated = source / "src/reticulumpi/_version.py"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text('__version__ = "0.3.4"\n', encoding="utf-8")
    assert admin._source_metadata(source) == ("0.3.4", source)
    generated.unlink()
    with pytest.raises(admin.AdminError, match="no generated"):
        admin._source_metadata(source)

    (source / "pyproject.toml").write_text('[project]\nname = "other"\nversion = "1"\n')
    with pytest.raises(admin.AdminError, match="project name"):
        admin._source_metadata(source)
    (source / "pyproject.toml").write_text("not = [valid", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="invalid project metadata"):
        admin._source_metadata(source)

    wheel = tmp_path / "reticulumpi-0.3.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    assert admin._source_metadata(wheel) == ("0.3.0", None)
    bad_wheel = tmp_path / "other-0.3.0-py3-none-any.whl"
    bad_wheel.write_bytes(b"wheel")
    with pytest.raises(admin.AdminError, match="not a ReticulumPi wheel"):
        admin._source_metadata(bad_wheel)
    with pytest.raises(admin.AdminError, match="source directory or wheel"):
        admin._source_metadata(tmp_path / "missing")


def test_bundle_verification_requires_complete_signed_source(tmp_path, monkeypatch):
    incomplete = _source_bundle(tmp_path / "incomplete", complete=False)
    with pytest.raises(admin.AdminError, match="incomplete"):
        admin._verify_bundle(incomplete, incomplete)

    source = _source_bundle(tmp_path / "source")
    _sign_source(source)
    verified = []
    monkeypatch.setattr(
        admin,
        "_verify_minisign",
        lambda manifest, signature: verified.append((manifest, signature)),
    )
    admin._verify_bundle(source, source)
    assert verified

    (source / "pyproject.toml").write_text("tampered", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="checksum mismatch"):
        admin._verify_signed_bundle(source, source)


def test_signed_source_manifest_rejects_unlisted_and_missing_files(tmp_path, monkeypatch):
    source = _source_bundle(tmp_path / "source")
    _sign_source(source)
    monkeypatch.setattr(admin, "_verify_minisign", lambda *_args: None)
    (source / "extra").write_text("not signed", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="unlisted"):
        admin._verify_signed_bundle(source, source)
    (source / "extra").unlink()
    (source / "config/reticulumpi/config.example.yaml").unlink()
    with pytest.raises(admin.AdminError, match="missing"):
        admin._verify_signed_bundle(source, source)


def test_signed_wheel_manifest_success_missing_and_mismatch(tmp_path, monkeypatch):
    wheel = tmp_path / "reticulumpi-0.3.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    signature = tmp_path / admin.BUNDLE_SIGNATURE_NAME
    signature.write_text("signature", encoding="utf-8")
    manifest = tmp_path / admin.BUNDLE_MANIFEST_NAME
    manifest.write_text(f"{admin._sha256(wheel)}  {wheel.name}\n", encoding="utf-8")
    monkeypatch.setattr(admin, "_verify_minisign", lambda *_args: None)
    admin._verify_signed_bundle(wheel, None)
    manifest.write_text(f"{'0' * 64}  other.whl\n", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="does not contain"):
        admin._verify_signed_bundle(wheel, None)
    manifest.write_text(f"{'0' * 64}  {wheel.name}\n", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="checksum mismatch"):
        admin._verify_signed_bundle(wheel, None)


@pytest.mark.parametrize("line", ["", "no digest", f"{'a' * 64}  ../escape"])
def test_hash_manifest_rejects_empty_malformed_and_unsafe_paths(tmp_path, line):
    manifest = tmp_path / "SHA256SUMS"
    manifest.write_text(line + ("\n" if line else ""), encoding="utf-8")
    with pytest.raises(admin.AdminError, match="manifest|unsafe"):
        admin._read_hash_manifest(manifest)


def test_hash_manifest_normalizes_case_and_rejects_duplicates(tmp_path):
    manifest = tmp_path / "SHA256SUMS"
    manifest.write_text(f"{'A' * 64} *artifact\n", encoding="utf-8")
    assert admin._read_hash_manifest(manifest) == {"artifact": "a" * 64}
    manifest.write_text(f"{'a' * 64}  artifact\n{'b' * 64}  artifact\n", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="duplicate"):
        admin._read_hash_manifest(manifest)


def test_unsigned_escape_is_explicit_nonroot_and_never_apply(tmp_path, monkeypatch, capsys):
    source = _source_bundle(tmp_path / "source")
    monkeypatch.setenv(admin.UNSIGNED_DEV_ENV, "yes")
    with pytest.raises(admin.AdminError, match="exactly 1"):
        admin._unsigned_development_mode()
    monkeypatch.setenv(admin.UNSIGNED_DEV_ENV, "1")
    monkeypatch.setattr(admin.os, "geteuid", lambda: 0)
    with pytest.raises(admin.AdminError, match="forbidden"):
        admin._unsigned_development_mode()
    monkeypatch.setattr(admin.os, "geteuid", lambda: 1000)
    admin._verify_bundle(source, source)
    assert "unsigned bundle" in capsys.readouterr().err


def test_minisign_invocation_is_fixed_and_fail_closed(tmp_path, monkeypatch):
    manifest = tmp_path / "SHA256SUMS"
    signature = tmp_path / "SHA256SUMS.minisig"
    key = tmp_path / "release.pub"
    for path in (manifest, signature, key):
        path.write_text("value", encoding="utf-8")
    monkeypatch.setattr(admin, "_trusted_release_public_key", lambda: key)
    calls = []

    def succeed(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(admin.subprocess, "run", succeed)
    admin._verify_minisign(manifest, signature)
    command, kwargs = calls[0]
    assert command == [admin.MINISIGN, "-Vm", str(manifest), "-x", str(signature), "-p", str(key)]
    assert kwargs["env"] == {"LANG": "C", "PATH": "/usr/bin:/bin"}

    monkeypatch.setattr(
        admin.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing"))
    )
    with pytest.raises(admin.AdminError, match="cannot execute"):
        admin._verify_minisign(manifest, signature)
    failure = subprocess.CalledProcessError(1, [admin.MINISIGN], stderr="bad signature")
    monkeypatch.setattr(
        admin.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(failure)
    )
    with pytest.raises(admin.AdminError, match="bad signature"):
        admin._verify_minisign(manifest, signature)


def test_minisign_and_trusted_key_reject_unsafe_files(tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    with pytest.raises(admin.AdminError, match="missing or unsafe"):
        admin._verify_minisign(missing, missing)

    key = tmp_path / "release.pub"
    key.write_text("key", encoding="utf-8")
    key.chmod(0o666)
    monkeypatch.setattr(admin, "RELEASE_PUBLIC_KEY_FILE", key)
    with pytest.raises(admin.AdminError, match="ownership or permissions"):
        admin._trusted_release_public_key()
    key.unlink()
    key.symlink_to(tmp_path / "target")
    with pytest.raises(admin.AdminError, match="not a regular file"):
        admin._trusted_release_public_key()


def test_staged_signed_source_is_independent_and_reverified(tmp_path, monkeypatch):
    source = _source_bundle(tmp_path / "source")
    _sign_source(source)
    checks = []
    monkeypatch.setattr(
        admin, "_verify_bundle", lambda bundle, candidate: checks.append((bundle, candidate))
    )
    staging = tmp_path / "staging"
    staging.mkdir()
    staged = admin._stage_verified_source(source, staging)
    assert staged != source
    assert (staged / "pyproject.toml").read_bytes() == (source / "pyproject.toml").read_bytes()
    assert checks == [(staged, staged)]


def test_bundle_location_accepts_wheel_and_rejects_nested_layouts(tmp_path):
    root = tmp_path / "opt/reticulumpi"
    admin._validate_bundle_location(tmp_path / "release.whl", None, root)
    for source in (root, root / "checkout", tmp_path):
        with pytest.raises(admin.AdminError, match="must be separate"):
            admin._validate_bundle_location(source, source, root)


def test_wheel_validation_rejects_unsafe_metadata_and_missing_assets(tmp_path):
    unsafe = tmp_path / "unsafe.whl"
    with ZipFile(unsafe, "w") as archive:
        archive.writestr("../escape", "bad")
    with pytest.raises(admin.AdminError, match="unsafe member"):
        admin._validate_wheel(unsafe, "0.3.0", ())

    no_metadata = tmp_path / "none.whl"
    with ZipFile(no_metadata, "w") as archive:
        archive.writestr("reticulumpi/module.py", "")
    with pytest.raises(admin.AdminError, match="exactly one METADATA"):
        admin._validate_wheel(no_metadata, "0.3.0", ())

    wrong_name = _wheel(tmp_path / "wrong-name.whl", name="other")
    with pytest.raises(admin.AdminError, match="project name"):
        admin._validate_wheel(wrong_name, "0.3.0", ())

    missing_assets = _wheel(tmp_path / "missing-assets.whl")
    with pytest.raises(admin.AdminError, match="missing assets"):
        admin._validate_wheel(missing_assets, "0.3.0", ("dashboard",))

    corrupt = tmp_path / "corrupt.whl"
    corrupt.write_text("no zip", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="invalid wheel"):
        admin._validate_wheel(corrupt, "0.3.0", ())


def test_systemd_state_helpers_treat_execution_failure_as_inactive(monkeypatch):
    monkeypatch.setattr(
        admin.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0)
    )
    assert admin._service_active("unit")
    assert admin._unit_enabled("unit")
    monkeypatch.setattr(
        admin.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=3)
    )
    assert not admin._service_active("unit")
    assert not admin._unit_enabled("unit")
    monkeypatch.setattr(
        admin.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError())
    )
    assert not admin._service_active("unit")
    assert not admin._unit_enabled("unit")


def test_wait_service_active_handles_recovery_and_timeout(monkeypatch):
    states = iter([False, True, True])
    monkeypatch.setattr(admin, "_service_active", lambda _name: next(states))
    times = iter([0.0, 0.1, 0.2, 0.3, 0.5, 0.6])
    monkeypatch.setattr(admin.time, "monotonic", lambda: next(times, 1.1))
    monkeypatch.setattr(admin.time, "sleep", lambda _seconds: None)
    admin._wait_service_active("unit", timeout=1, stable_for=0)

    monkeypatch.setattr(admin, "_service_active", lambda _name: False)
    times = iter([0.0, 0.5, 1.1])
    monkeypatch.setattr(admin.time, "monotonic", lambda: next(times, 0.5))
    with pytest.raises(admin.AdminError, match="did not remain active"):
        admin._wait_service_active("unit", timeout=1, stable_for=0)


@pytest.mark.parametrize(
    ("mode", "content", "owner_delta", "message"),
    [
        (0o666, b"ready\n", 0, "writable"),
        (0o600, b"wrong\n", 0, "invalid content"),
        (0o600, b"ready\n", 1, "wrong owner"),
    ],
)
def test_readiness_marker_security(admin_paths, monkeypatch, mode, content, owner_delta, message):
    admin_paths.run.mkdir(parents=True)
    marker = admin_paths.run / "ready"
    marker.write_bytes(content)
    marker.chmod(mode)
    account = SimpleNamespace(pw_uid=marker.stat().st_uid + owner_delta)
    monkeypatch.setattr(admin, "_service_account", lambda: account)
    with pytest.raises(admin.AdminError, match=message):
        admin._readiness_marker_valid()


def test_activation_validates_action_and_identity_continuity(admin_paths, monkeypatch):
    with pytest.raises(admin.AdminError, match="unsupported"):
        admin._activate_application("reload")
    calls = []
    monkeypatch.setattr(admin, "_clear_application_readiness", lambda: calls.append("clear"))
    monkeypatch.setattr(admin, "_run", lambda command: calls.append(command))
    monkeypatch.setattr(admin, "_wait_application_ready", lambda: calls.append("ready"))
    admin._activate_application("start")
    assert calls == ["clear", [admin.SYSTEMCTL, "start", "reticulumpi.service"], "ready"]

    roots = (admin.StateRoot("data", admin_paths.data),)
    monkeypatch.setattr(admin, "_activate_application", lambda action: calls.append(action))
    monkeypatch.setattr(admin, "_identity_hashes", lambda _roots: {"data:identity": "a"})
    assert admin._activate_and_verify_identities("restart", {"data:identity": "a"}, roots)
    with pytest.raises(admin.AdminError, match="identity continuity"):
        admin._verify_identity_continuity({"one": "a", "two": "a"}, {"two": "b"})


def test_sqlite_validation_and_backup_are_verified(tmp_path):
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("CREATE TABLE records(value TEXT)")
        connection.execute("INSERT INTO records VALUES ('kept')")
    backup = tmp_path / "nested/backup.db"
    admin._sqlite_backup_file(source, backup)
    admin._verify_sqlite(backup)
    with closing(sqlite3.connect(backup)) as connection:
        assert connection.execute("SELECT value FROM records").fetchone()[0] == "kept"
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not sqlite")
    with pytest.raises(admin.AdminError, match="SQLite"):
        admin._verify_sqlite(corrupt)


def test_tree_copy_manifest_and_rejections(tmp_path):
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (source / "keep").write_text("kept", encoding="utf-8")
    (nested / "ignored").write_text("ignored", encoding="utf-8")
    destination = tmp_path / "copy"
    admin._copy_tree_verified(source, destination, ignored=frozenset({Path("nested/ignored")}))
    assert (destination / "keep").read_text(encoding="utf-8") == "kept"
    assert not (destination / "nested/ignored").exists()
    assert admin._tree_manifest(destination)[0]["type"] == "directory"
    with pytest.raises(admin.AdminError, match="already exists"):
        admin._copy_tree_verified(source, destination)
    with pytest.raises(admin.AdminError, match="does not exist"):
        admin._copy_tree_verified(tmp_path / "missing", tmp_path / "other")

    (source / "link").symlink_to(source / "keep")
    with pytest.raises(admin.AdminError, match="symlinks"):
        admin._tree_entries(source)
    (source / "link").unlink()
    fifo = source / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(admin.AdminError, match="special file"):
        admin._tree_entries(source)


def test_legacy_state_migration_merges_and_preserves_identity(admin_paths):
    legacy = admin_paths.home / ".config/reticulumpi"
    canonical = admin_paths.data / ".config/reticulumpi"
    legacy.mkdir(parents=True)
    canonical.mkdir(parents=True)
    (legacy / "identity").write_bytes(b"durable identity")
    (legacy / "legacy-only").write_text("legacy", encoding="utf-8")
    (canonical / "canonical-only").write_text("canonical", encoding="utf-8")

    migrations = admin._migrate_legacy_home_state(())
    assert len(migrations) == 1
    assert (canonical / "identity").read_bytes() == b"durable identity"
    assert (canonical / "legacy-only").read_text(encoding="utf-8") == "legacy"
    assert (canonical / "canonical-only").read_text(encoding="utf-8") == "canonical"
    admin._remove_migrated_legacy_state(migrations)
    assert not legacy.exists()
    admin._verify_migrated_identities(migrations)


def test_legacy_state_conflict_is_fail_closed(admin_paths):
    legacy = admin_paths.home / ".reticulum"
    canonical = admin_paths.data / ".reticulum"
    legacy.mkdir(parents=True)
    canonical.mkdir(parents=True)
    (legacy / "config").write_text("legacy", encoding="utf-8")
    (canonical / "config").write_text("canonical", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="conflict"):
        admin._merge_tree_atomically(legacy, canonical)
    assert (canonical / "config").read_text(encoding="utf-8") == "canonical"
    assert (legacy / "config").read_text(encoding="utf-8") == "legacy"


def test_legacy_merge_fsync_failure_restores_original_destination(admin_paths, monkeypatch):
    legacy = admin_paths.home / ".reticulum"
    canonical = admin_paths.data / ".reticulum"
    legacy.mkdir(parents=True)
    canonical.mkdir(parents=True)
    (legacy / "legacy").write_text("new", encoding="utf-8")
    (canonical / "canonical").write_text("old", encoding="utf-8")
    real_fsync = admin._fsync_state_directory
    calls = 0

    def fail_final_once(path):
        nonlocal calls
        calls += 1
        # Copy verification fsyncs several directories; fail the first fsync after
        # the candidate is installed, then allow rollback durability to complete.
        if path == canonical.parent and canonical.exists() and (canonical / "legacy").exists():
            monkeypatch.setattr(admin, "_fsync_state_directory", real_fsync)
            raise admin.AdminError("injected fsync failure")
        return real_fsync(path)

    monkeypatch.setattr(admin, "_fsync_state_directory", fail_final_once)
    with pytest.raises(admin.AdminError, match="injected fsync failure"):
        admin._merge_tree_atomically(legacy, canonical)
    assert (canonical / "canonical").read_text(encoding="utf-8") == "old"
    assert not (canonical / "legacy").exists()


def test_legacy_merge_cleanup_failure_keeps_committed_candidate(admin_paths, monkeypatch, capsys):
    legacy = admin_paths.home / ".reticulum"
    canonical = admin_paths.data / ".reticulum"
    legacy.mkdir(parents=True)
    canonical.mkdir(parents=True)
    (legacy / "legacy").write_text("new", encoding="utf-8")
    (canonical / "canonical").write_text("old", encoding="utf-8")
    real_discard = admin._discard_path

    def fail_displaced(path):
        if ".pre-legacy-" in path.name:
            raise OSError("injected cleanup failure")
        return real_discard(path)

    monkeypatch.setattr(admin, "_discard_path", fail_displaced)
    admin._merge_tree_atomically(legacy, canonical)
    assert (canonical / "canonical").read_text(encoding="utf-8") == "old"
    assert (canonical / "legacy").read_text(encoding="utf-8") == "new"
    assert "could not remove displaced canonical tree" in capsys.readouterr().err


def test_identity_path_parser_handles_quoted_relative_and_invalid_values(admin_paths):
    admin_paths.config.mkdir(parents=True)
    admin.CONFIG_FILE.write_text(
        'reticulumpi:\n  identity_path: "~/.config/reticulumpi/custom identity" # comment\n',
        encoding="utf-8",
    )
    assert (
        admin._configured_identity_path(admin_paths.data)
        == (admin_paths.data / ".config/reticulumpi/custom identity").resolve()
    )
    admin.CONFIG_FILE.write_text("reticulumpi:\n  identity_path: one two\n", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="one scalar"):
        admin._configured_identity_path(admin_paths.data)
    admin.CONFIG_FILE.write_text('reticulumpi:\n  identity_path: "unterminated\n', encoding="utf-8")
    with pytest.raises(admin.AdminError, match="cannot parse"):
        admin._configured_identity_path(admin_paths.data)


def test_identity_hashes_include_canonical_configured_identity(admin_paths):
    canonical = admin_paths.data / ".config/reticulumpi"
    canonical.mkdir(parents=True)
    (canonical / "identity").write_bytes(b"identity")
    roots = admin._state_roots(())
    hashes = admin._identity_hashes(roots)
    assert hashes["data:.config/reticulumpi/identity"] == admin._sha256(canonical / "identity")


def test_state_roots_never_nest_swaps_inside_canonical_data(admin_paths, monkeypatch):
    monkeypatch.setattr(admin, "_legacy_home_candidates", lambda: (admin.DATA_DIR,))

    roots = admin._state_roots(("nomadnet",))

    assert [(root.name, root.path) for root in roots] == [
        ("etc", admin.CONFIG_DIR),
        ("data", admin.DATA_DIR),
    ]


@pytest.mark.parametrize(
    ("allowlist", "expected"),
    [
        ("", "deny"),
        ("      allowed_identities: []\n", "open"),
        ("      allowed_identities: [abc, def]\n", "allowlist"),
        ("      allowed_identities:\n        - abc\n", "allowlist"),
        ("      allowed_identities:\n", "open"),
    ],
)
def test_file_transfer_policy_migration_preserves_legacy_intent(admin_paths, allowlist, expected):
    admin_paths.config.mkdir(parents=True)
    admin.CONFIG_FILE.write_text(
        "reticulumpi:\n  plugins:\n    file_transfer:\n"
        "      enabled: true\n"
        f"{allowlist}"
        "    messaging_hub:\n      enabled: false\n",
        encoding="utf-8",
    )
    admin.CONFIG_FILE.chmod(0o640)
    migration = admin._plan_file_transfer_policy_migration()
    assert migration is not None
    assert migration.policy == expected
    before_owner = (admin.CONFIG_FILE.stat().st_uid, admin.CONFIG_FILE.stat().st_gid)
    admin._apply_file_transfer_policy_migration(migration)
    text = admin.CONFIG_FILE.read_text(encoding="utf-8")
    assert f"      access_policy: {expected}\n" in text
    assert allowlist in text
    assert stat.S_IMODE(admin.CONFIG_FILE.stat().st_mode) == 0o640
    assert (admin.CONFIG_FILE.stat().st_uid, admin.CONFIG_FILE.stat().st_gid) == before_owner
    assert admin._plan_file_transfer_policy_migration() is None


def test_existing_file_transfer_policy_is_not_rewritten(admin_paths):
    admin_paths.config.mkdir(parents=True)
    original = (
        "reticulumpi:\n  plugins:\n    file_transfer:\n"
        "      access_policy: allowlist\n      allowed_identities: []\n"
    )
    admin.CONFIG_FILE.write_text(original, encoding="utf-8")
    assert admin._plan_file_transfer_policy_migration() is None
    assert admin.CONFIG_FILE.read_text(encoding="utf-8") == original


def test_file_transfer_policy_migration_rejects_ambiguous_yaml_and_changed_plan(
    admin_paths,
):
    admin_paths.config.mkdir(parents=True)
    admin.CONFIG_FILE.write_text(
        "reticulumpi:\n  plugins:\n    file_transfer: {allowed_identities: []}\n",
        encoding="utf-8",
    )
    with pytest.raises(admin.AdminError, match="inline"):
        admin._plan_file_transfer_policy_migration()

    admin.CONFIG_FILE.write_text(
        "reticulumpi:\n  plugins:\n    file_transfer:\n      allowed_identities: []\n",
        encoding="utf-8",
    )
    migration = admin._plan_file_transfer_policy_migration()
    assert migration is not None
    admin.CONFIG_FILE.write_text(
        admin.CONFIG_FILE.read_text(encoding="utf-8") + "# concurrent edit\n",
        encoding="utf-8",
    )
    with pytest.raises(admin.AdminError, match="changed after"):
        admin._apply_file_transfer_policy_migration(migration)


def test_legacy_config_paths_are_rewritten_atomically_without_touching_comments(
    admin_paths,
):
    admin_paths.config.mkdir(parents=True)
    legacy_home = str(admin_paths.home)
    original = (
        "reticulumpi:\n"
        f"  identity_path: {legacy_home}/.config/reticulumpi/identity\n"
        f"  database: '{legacy_home}/.local/share/reticulumpi/messages.db'\n"
        f"  unrelated: {legacy_home}-suffix\n"
        f"  # historical example: {legacy_home}/ignored\n"
    )
    admin.CONFIG_FILE.write_text(original, encoding="utf-8")
    admin.CONFIG_FILE.chmod(0o640)
    owner = (admin.CONFIG_FILE.stat().st_uid, admin.CONFIG_FILE.stat().st_gid)
    migration = admin._plan_legacy_config_path_migration()
    assert migration is not None
    assert migration.replacement_count == 2
    admin._apply_legacy_config_path_migration(migration)
    migrated = admin.CONFIG_FILE.read_text(encoding="utf-8")
    assert migrated.count(str(admin_paths.data)) == 2
    assert f"unrelated: {legacy_home}-suffix" in migrated
    assert f"# historical example: {legacy_home}/ignored" in migrated
    assert stat.S_IMODE(admin.CONFIG_FILE.stat().st_mode) == 0o640
    assert (admin.CONFIG_FILE.stat().st_uid, admin.CONFIG_FILE.stat().st_gid) == owner
    assert admin._plan_legacy_config_path_migration() is None


def test_legacy_config_path_plan_rejects_concurrent_and_incomplete_rewrites(
    admin_paths, monkeypatch
):
    admin_paths.config.mkdir(parents=True)
    content = f"reticulumpi:\n  identity_path: {admin_paths.home}/identity\n"
    admin.CONFIG_FILE.write_text(content, encoding="utf-8")
    plan = admin._plan_legacy_config_path_migration()
    assert plan is not None
    admin.CONFIG_FILE.write_text(content + "# concurrent edit\n", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="changed after"):
        admin._apply_legacy_config_path_migration(plan)

    admin.CONFIG_FILE.write_text(content, encoding="utf-8")
    plan = admin._plan_legacy_config_path_migration()
    assert plan is not None
    wrong_count = admin.LegacyConfigPathMigration(
        plan.source_prefix,
        plan.destination_prefix,
        plan.replacement_count + 1,
        plan.source_sha256,
    )
    with pytest.raises(admin.AdminError, match="path count changed"):
        admin._apply_legacy_config_path_migration(wrong_count)

    admin.CONFIG_FILE.write_text(content, encoding="utf-8")
    plan = admin._plan_legacy_config_path_migration()
    assert plan is not None
    monkeypatch.setattr(admin, "_plan_legacy_config_path_migration", lambda *_args: plan)
    with pytest.raises(admin.AdminError, match="did not validate"):
        admin._apply_legacy_config_path_migration(plan)


def test_legacy_config_path_plan_uses_locked_default_home_when_account_is_absent(
    admin_paths, monkeypatch
):
    admin_paths.config.mkdir(parents=True)
    admin.CONFIG_FILE.write_text(
        "reticulumpi:\n  identity_path: /home/reticulumpi/identity\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        admin.pwd, "getpwnam", lambda _name: (_ for _ in ()).throw(KeyError("not installed"))
    )
    plan = admin._plan_legacy_config_path_migration()
    assert plan is not None
    assert plan.source_prefix == "/home/reticulumpi"


def test_empty_allowlist_warning_is_redacted_and_dry_run_is_non_mutating(
    admin_paths, tmp_path, monkeypatch, capsys
):
    source = _source_bundle(tmp_path / "source")
    admin_paths.config.mkdir(parents=True)
    secret_identity = "deadbeef" * 8
    original = (
        "reticulumpi:\n  plugins:\n    file_transfer:\n"
        "      allowed_identities: []\n"
        f"    # unrelated secret must not be printed: {secret_identity}\n"
    )
    admin.CONFIG_FILE.write_text(original, encoding="utf-8")
    monkeypatch.setattr(admin, "_verify_signed_bundle", lambda *_args: None)
    args = SimpleNamespace(
        install_root=str(admin_paths.root),
        bundle=str(source),
        feature=[],
        apply=False,
        dry_run=True,
        start=False,
    )
    assert admin._apply_release(args, "install") == 0
    captured = capsys.readouterr()
    assert "SECURITY WARNING" in captured.err
    assert secret_identity not in captured.err
    assert '"value": "open"' in captured.out
    assert admin.CONFIG_FILE.read_text(encoding="utf-8") == original


def test_transactional_install_all_features_migrates_state_and_reaches_readiness(
    admin_paths, tmp_path, monkeypatch, capsys
):
    source = _complete_source_bundle(tmp_path / "signed-source")
    wheel = _dashboard_wheel(tmp_path / "reticulumpi-0.3.0-py3-none-any.whl")
    legacy_identity = admin_paths.home / ".config/reticulumpi/identity"
    legacy_identity.parent.mkdir(parents=True)
    legacy_identity.write_bytes(b"stable legacy identity")
    legacy_rns = admin_paths.home / ".reticulum/config"
    legacy_rns.parent.mkdir(parents=True)
    legacy_rns.write_text("legacy rns config", encoding="utf-8")
    for relative in (".nomadnet/config", ".nomadnet-tui/config"):
        path = admin_paths.home / relative
        path.parent.mkdir(parents=True)
        path.write_text(relative, encoding="utf-8")
    admin_paths.sudoers.mkdir(parents=True)
    for name in admin._LEGACY_SUDOERS_NAMES:
        (admin_paths.sudoers / name).write_text("obsolete", encoding="utf-8")

    commands = _mock_apply_runtime(monkeypatch, wheel)
    features = [
        "dashboard",
        "nomadnet",
        "shared-rnsd",
        "watchdog",
        "captive-portal",
        "offline-tools",
        "chrony-control",
        "sensors",
    ]
    args = SimpleNamespace(
        install_root=str(admin_paths.root),
        bundle=str(source),
        feature=features,
        apply=True,
        dry_run=False,
        start=True,
    )
    assert admin._apply_release(args, "install") == 0
    release = admin_paths.root / "releases/0.3.0"
    assert (admin_paths.root / "current").resolve() == release
    assert (admin_paths.data / ".config/reticulumpi/identity").read_bytes() == (
        b"stable legacy identity"
    )
    assert not legacy_identity.parent.exists()
    assert not (admin_paths.home / ".reticulum").exists()
    assert "access_policy: deny" in admin.CONFIG_FILE.read_text(encoding="utf-8")
    manifest = json.loads(admin.MANIFEST_FILE.read_text(encoding="utf-8"))
    assert manifest["version"] == "0.3.0"
    assert set(manifest["features"]) == set(features)
    journal = json.loads(admin.JOURNAL_FILE.read_text(encoding="utf-8"))
    assert journal["state"] == "complete"
    assert journal["configuration_migrations"] == [
        {"setting": "file_transfer.access_policy", "value": "deny"}
    ]
    assert len(journal["legacy_migrations"]) == 4
    for name in admin._MANAGED_HELPER_NAMES:
        assert (admin_paths.libexec / name).is_file()
    assert (admin_paths.shared / "offline_profile.yaml").is_file()
    assert not any((admin_paths.sudoers / name).exists() for name in admin._LEGACY_SUDOERS_NAMES)
    assert [admin.SYSTEMCTL, "restart", "rnsd.service"] in commands
    assert [admin.SYSTEMCTL, "enable", "--now", "rnsd-watchdog.timer"] in commands
    assert "Installed ReticulumPi 0.3.0" in capsys.readouterr().out


def test_transactional_wheel_upgrade_preserves_existing_configuration(
    admin_paths, tmp_path, monkeypatch
):
    current = _release(admin_paths.root, "0.2.5")
    (admin_paths.root / "current").symlink_to(current)
    _write_install_manifest(admin_paths, current, None)
    legacy_identity = admin_paths.home / ".config/reticulumpi/identity"
    legacy_identity.parent.mkdir(parents=True)
    legacy_identity.write_bytes(b"wheel-upgrade-identity")
    admin.CONFIG_FILE.write_text(
        f"reticulumpi:\n  identity_path: {legacy_identity}\n"
        "  plugins:\n    file_transfer:\n      access_policy: deny\n",
        encoding="utf-8",
    )
    admin_paths.data.mkdir(parents=True)
    wheel = _wheel(tmp_path / "reticulumpi-0.3.0-py3-none-any.whl")
    real_build_wheel = admin._build_wheel
    commands = _mock_apply_runtime(monkeypatch, wheel)
    # Exercise the wheel-copy path instead of replacing it with the source-build stub.
    monkeypatch.setattr(admin, "_build_wheel", real_build_wheel)
    args = SimpleNamespace(
        install_root=str(admin_paths.root),
        bundle=str(wheel),
        feature=[],
        apply=True,
        dry_run=False,
        start=False,
    )
    assert admin._apply_release(args, "upgrade") == 0
    assert (admin_paths.root / "current").resolve() == admin_paths.root / "releases/0.3.0"
    migrated_config = admin.CONFIG_FILE.read_text(encoding="utf-8")
    assert "access_policy: deny" in migrated_config
    assert str(admin_paths.data / ".config/reticulumpi/identity") in migrated_config
    assert str(admin_paths.home) not in migrated_config
    assert (admin_paths.data / ".config/reticulumpi/identity").read_bytes() == (
        b"wheel-upgrade-identity"
    )
    assert not legacy_identity.parent.exists()
    assert [admin.SYSTEMCTL, "enable", "reticulumpi.service"] in commands


@pytest.mark.parametrize(
    ("active_aux", "enabled_aux", "expected_command"),
    [
        (True, True, "start"),
        (False, True, "stop"),
        (False, False, "disable"),
    ],
)
def test_failed_upgrade_restores_config_pointer_and_prior_unit_states(
    admin_paths,
    tmp_path,
    monkeypatch,
    active_aux,
    enabled_aux,
    expected_command,
):
    current = _release(admin_paths.root, "0.2.5")
    (admin_paths.root / "current").symlink_to(current)
    _write_install_manifest(admin_paths, current, None)
    original_config = "reticulumpi:\n  plugins:\n    file_transfer:\n      allowed_identities: []\n"
    admin.CONFIG_FILE.write_text(original_config, encoding="utf-8")
    source = _complete_source_bundle(tmp_path / "source")
    wheel = _wheel(tmp_path / "reticulumpi-0.3.0-py3-none-any.whl")
    auxiliaries = {
        "rnsd.service",
        "rnsd-watchdog.timer",
        "reticulumpi-control.socket",
    }
    active = {"reticulumpi.service"} | (auxiliaries if active_aux else set())
    enabled = {"reticulumpi.service"} | (auxiliaries if enabled_aux else set())
    commands = _mock_apply_runtime(monkeypatch, wheel, active=active, enabled=enabled)
    activation_calls = 0

    def fail_candidate_once(_action):
        nonlocal activation_calls
        activation_calls += 1
        if activation_calls == 1:
            raise admin.AdminError("injected candidate failure")

    monkeypatch.setattr(admin, "_activate_application", fail_candidate_once)
    args = SimpleNamespace(
        install_root=str(admin_paths.root),
        bundle=str(source),
        feature=["shared-rnsd"],
        apply=True,
        dry_run=False,
        start=True,
    )
    with pytest.raises(admin.AdminError, match="injected candidate failure"):
        admin._apply_release(args, "upgrade")
    assert (admin_paths.root / "current").resolve() == current
    assert admin.CONFIG_FILE.read_text(encoding="utf-8") == original_config
    assert not (admin_paths.root / "releases/0.3.0").exists()
    journal = json.loads(admin.JOURNAL_FILE.read_text(encoding="utf-8"))
    assert journal["state"] == "rolled_back"
    matching = [
        command
        for command in commands
        if expected_command in command and any(name in command for name in auxiliaries)
    ]
    assert matching


def test_rollback_attempt_records_compound_failure():
    errors = []
    admin._rollback_attempt(
        errors,
        "restore state",
        lambda: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    assert errors == ["restore state: disk unavailable"]


def test_same_signed_install_artifact_is_an_idempotent_noop(
    admin_paths, tmp_path, monkeypatch, capsys
):
    source = _complete_source_bundle(tmp_path / "signed-source")
    wheel = _wheel(source / "reticulumpi-0.3.0-py3-none-any.whl")
    (source / "bundle.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "kind": "reticulumpi-install",
                "version": "0.3.0",
                "architecture": "arm64",
                "wheel": wheel.name,
            }
        ),
        encoding="utf-8",
    )
    _sign_source(source)
    release = _release(admin_paths.root, "0.3.0")
    (admin_paths.root / "current").symlink_to(release)
    _write_install_manifest(admin_paths, release, None)
    manifest = json.loads(admin.MANIFEST_FILE.read_text(encoding="utf-8"))
    manifest["bundle_sha256"] = admin._sha256(wheel)
    admin._atomic_json(admin.MANIFEST_FILE, manifest)

    monkeypatch.setattr(admin, "_require_root", lambda: None)
    monkeypatch.setattr(admin, "_maintenance_lock", lambda: nullcontext())
    monkeypatch.setattr(admin, "_verify_minisign", lambda *_args: None)
    monkeypatch.setattr(
        admin,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("idempotent install executed a subprocess"),
    )
    args = SimpleNamespace(
        install_root=str(admin_paths.root),
        bundle=str(source),
        bundle_origin=str(source),
        feature=[],
        apply=True,
        dry_run=False,
        start=True,
    )

    assert admin._apply_release_materialized(args, "install") == 0
    assert (admin_paths.root / "current").resolve() == release
    assert "already installed from this signed artifact" in capsys.readouterr().out


def test_backup_retention_keeps_three_newest_sets(admin_paths):
    admin_paths.config.mkdir(parents=True)
    admin_paths.data.mkdir(parents=True)
    old = []
    admin_paths.backups.mkdir(parents=True)
    for index in range(3):
        path = admin_paths.backups / f"release-old-{index}"
        path.mkdir()
        os.utime(path, (index + 1, index + 1))
        old.append(path)
    created = admin._backup_state("0.3.0")
    retained = sorted(admin_paths.backups.glob("release-*"))
    assert created in retained
    assert len(retained) == 3
    assert not old[0].exists()


def test_source_wheel_build_requires_exactly_one_distribution(tmp_path, monkeypatch):
    source = _source_bundle(tmp_path / "source")
    destination = tmp_path / "wheels"
    destination.mkdir()

    def make_one(_command, **_kwargs):
        (destination / "reticulumpi-0.3.0-py3-none-any.whl").write_bytes(b"wheel")
        return ""

    monkeypatch.setattr(admin, "_run", make_one)
    assert admin._build_wheel(source, source, destination).name.startswith("reticulumpi-")
    (destination / "reticulumpi-extra.whl").write_bytes(b"wheel")
    with pytest.raises(admin.AdminError, match="exactly one"):
        admin._build_wheel(source, source, destination)


def test_path_size_and_install_space_account_for_data(admin_paths, tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "file").write_bytes(b"1234")
    (bundle / "target").write_bytes(b"ignored")
    (bundle / "link").symlink_to(bundle / "target")
    assert admin._path_size(bundle) == 11
    assert admin._path_size(bundle / "file") == 4
    monkeypatch.setattr(admin.shutil, "disk_usage", lambda _path: SimpleNamespace(free=1))
    with pytest.raises(admin.AdminError, match="insufficient free space"):
        admin._ensure_install_space(admin_paths.root / "nested/root", bundle)


def test_feature_downgrade_removes_optional_units_helpers_and_profile(admin_paths, tmp_path):
    source = _complete_source_bundle(tmp_path / "source")
    previous = ("shared-rnsd", "watchdog", "offline-tools", "chrony-control")
    admin._render_units(source, admin_paths.root, previous)
    admin._install_helpers(source, previous)
    assert (admin_paths.systemd / "rnsd.service").exists()
    assert (admin_paths.shared / "offline_profile.yaml").exists()
    admin._render_units(source, admin_paths.root, (), previous)
    admin._install_helpers(source, (), previous)
    assert not (admin_paths.systemd / "rnsd.service").exists()
    assert not (admin_paths.systemd / admin._RNSD_DROPIN_RELATIVE).exists()
    assert not (admin_paths.libexec / "chrony_helper.sh").exists()
    assert not (admin_paths.shared / "offline_profile.yaml").exists()


def test_managed_file_and_bundle_asset_symlinks_are_rejected(admin_paths, tmp_path):
    source = _complete_source_bundle(tmp_path / "source")
    unit = source / "systemd/reticulumpi.service"
    unit.unlink()
    unit.symlink_to(source / "pyproject.toml")
    with pytest.raises(admin.AdminError, match="systemd unit"):
        admin._render_units(source, admin_paths.root, ())
    unit.unlink()
    unit.write_text("unit", encoding="utf-8")
    helper = source / "scripts/restart_services.sh"
    helper.unlink()
    helper.symlink_to(source / "pyproject.toml")
    with pytest.raises(admin.AdminError, match="required helper"):
        admin._install_helpers(source, ())

    managed = admin_paths.systemd / "managed"
    managed.parent.mkdir(parents=True, exist_ok=True)
    managed.symlink_to(source / "pyproject.toml")
    with pytest.raises(admin.AdminError, match="managed path"):
        admin._snapshot_files((managed,))
    managed.unlink()
    managed.mkdir()
    with pytest.raises(admin.AdminError, match="regular file"):
        admin._snapshot_files((managed,))


@pytest.mark.parametrize("raw", [None, "", "/absolute", "../escape", "a/../b"])
def test_backup_state_relative_paths_are_strict(raw):
    with pytest.raises(admin.AdminError, match="state path"):
        admin._safe_state_relative(raw)


def test_restore_records_supports_legacy_layout_and_rejects_unsafe_directory(admin_paths, tmp_path):
    backup = tmp_path / "backup"
    (backup / "etc").mkdir(parents=True)
    (backup / "etc/config").write_text("config", encoding="utf-8")
    records, allowed = admin._restore_records(backup, {})
    assert [(record[0].name, record[1]) for record in records] == [("etc", True)]
    assert "data" in allowed
    (backup / "data").symlink_to(backup / "etc", target_is_directory=True)
    with pytest.raises(admin.AdminError, match="invalid legacy backup"):
        admin._restore_records(backup, {})


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(schema=1), "unsupported"),
        (lambda value: value.update(state_roots={}), "unsupported"),
        (lambda value: value.update(features="nomadnet"), "feature metadata"),
        (lambda value: value["state_roots"].__setitem__(0, "bad"), "must be an object"),
        (
            lambda value: value["state_roots"][0].update(name="unknown"),
            "invalid state root",
        ),
        (
            lambda value: value["state_roots"][0].update(present="yes"),
            "metadata is invalid",
        ),
        (
            lambda value: value["state_roots"][0].update(manifest=["bad"]),
            "metadata is invalid",
        ),
        (
            lambda value: value["state_roots"][0].update(manifest=[{}]),
            "absent backup state root",
        ),
        (lambda value: value["state_roots"].pop(), "set is incomplete"),
    ],
)
def test_restore_records_rejects_malformed_transaction_metadata(
    admin_paths, tmp_path, mutate, message
):
    metadata = _empty_backup_metadata()
    mutate(metadata)
    with pytest.raises(admin.AdminError, match=message):
        admin._restore_records(tmp_path, metadata)


def test_restore_records_rejects_duplicates_and_feature_set_mismatch(admin_paths, tmp_path):
    metadata = _empty_backup_metadata()
    metadata["state_roots"].append(dict(metadata["state_roots"][0]))
    with pytest.raises(admin.AdminError, match="invalid state root"):
        admin._restore_records(tmp_path, metadata)
    metadata = _empty_backup_metadata(("nomadnet",))
    metadata["features"] = []
    with pytest.raises(admin.AdminError, match="extra"):
        admin._restore_records(tmp_path, metadata)


def test_restored_database_metadata_supports_old_and_new_records(admin_paths):
    admin_paths.data.mkdir(parents=True)
    database = admin_paths.data / "records.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE records(value TEXT)")
    roots = {"data": admin.StateRoot("data", admin_paths.data)}
    admin._verify_restored_databases(
        {"databases": ["records.db", {"state": "data", "path": "records.db"}]}, roots
    )


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (42, "record is invalid"),
        ({"state": 3, "path": "records.db"}, "state root is invalid"),
        ({"state": "unknown", "path": "records.db"}, "unknown state root"),
    ],
)
def test_restored_database_metadata_rejects_invalid_records(admin_paths, record, message):
    roots = {"data": admin.StateRoot("data", admin_paths.data)}
    with pytest.raises(admin.AdminError, match=message):
        admin._verify_restored_databases({"databases": [record]}, roots)
    with pytest.raises(admin.AdminError, match="database list"):
        admin._verify_restored_databases({"databases": {}}, roots)


def test_restore_transaction_with_absent_legacy_roots_removes_candidate_state(admin_paths):
    admin_paths.config.mkdir(parents=True)
    admin_paths.data.mkdir(parents=True)
    admin.CONFIG_FILE.write_text("original", encoding="utf-8")
    backup = admin._backup_state("0.3.0")
    candidate_legacy = admin_paths.home / ".config/reticulumpi"
    candidate_legacy.mkdir(parents=True)
    (candidate_legacy / "candidate").write_text("remove", encoding="utf-8")
    admin.CONFIG_FILE.write_text("candidate", encoding="utf-8")
    admin._restore_state_backup(backup)
    assert admin.CONFIG_FILE.read_text(encoding="utf-8") == "original"
    assert not candidate_legacy.exists()


def test_restore_transaction_requires_backup_and_valid_identity_metadata(admin_paths, tmp_path):
    with pytest.raises(admin.AdminError, match="backup is missing"):
        admin._restore_state_backup(tmp_path / "missing")
    admin_paths.config.mkdir(parents=True)
    admin_paths.data.mkdir(parents=True)
    backup = admin._backup_state("0.3.0")
    metadata_path = backup / "backup.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["identity_hashes"] = []
    admin._atomic_json(metadata_path, metadata, 0o600)
    with pytest.raises(admin.AdminError, match="identity hash metadata"):
        admin._restore_state_backup(backup)


@pytest.mark.parametrize("active", [False, True])
def test_rollback_success_preserves_identity_and_service_state(
    admin_paths, monkeypatch, active, capsys
):
    current = _release(admin_paths.root, "0.3.0")
    previous = _release(admin_paths.root, "0.2.5")
    (admin_paths.root / "current").symlink_to(current)
    _write_install_manifest(admin_paths, current, previous)
    admin.CONFIG_FILE.write_text("reticulumpi: {}\n", encoding="utf-8")
    admin_paths.data.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(admin, "_require_root", lambda: None)
    monkeypatch.setattr(admin, "_maintenance_lock", lambda: nullcontext())
    monkeypatch.setattr(admin, "_service_active", lambda _name: active)
    monkeypatch.setattr(admin, "_run", lambda command, **_kwargs: calls.append(command) or "")
    monkeypatch.setattr(
        admin,
        "_activate_and_verify_identities",
        lambda action, expected, roots, features=(): (
            calls.append([action, expected, roots, features]) or expected
        ),
    )
    assert admin._rollback(SimpleNamespace(to=None, apply=True, dry_run=False)) == 0
    assert (admin_paths.root / "current").resolve() == previous
    manifest = json.loads(admin.MANIFEST_FILE.read_text(encoding="utf-8"))
    assert manifest["version"] == "0.2.5"
    assert manifest["previous_release"] == str(current)
    if active:
        assert [admin.SYSTEMCTL, "stop", "reticulumpi.service"] in calls
        assert any(call[0] == "start" for call in calls if call and isinstance(call[0], str))
    assert "Rolled back" in capsys.readouterr().out


def test_rollback_dry_run_relative_target_and_validation_errors(admin_paths, capsys):
    current = _release(admin_paths.root, "0.3.0")
    previous = _release(admin_paths.root, "0.2.5")
    (admin_paths.root / "current").symlink_to(current)
    _write_install_manifest(admin_paths, current, None)
    args = SimpleNamespace(to="0.2.5", apply=False, dry_run=True)
    assert admin._rollback(args) == 0
    assert "Dry run only" in capsys.readouterr().out

    with pytest.raises(admin.AdminError, match="no previous release"):
        admin._rollback(SimpleNamespace(to=None, apply=False, dry_run=True))
    with pytest.raises(admin.AdminError, match="already active"):
        admin._rollback(SimpleNamespace(to="0.3.0", apply=False, dry_run=True))
    (admin_paths.root / "current").unlink()
    with pytest.raises(admin.AdminError, match="current release pointer"):
        admin._rollback(args)
    assert previous.is_dir()


def test_status_reports_manifest_and_unfinished_journal_in_both_formats(
    admin_paths, monkeypatch, capsys
):
    assert admin._status(SimpleNamespace(json=False)) == 0
    assert "manifest: None" in capsys.readouterr().out
    release = _release(admin_paths.root, "0.3.0")
    _write_install_manifest(admin_paths, release, None)
    admin_paths.data.mkdir(parents=True)
    admin._atomic_json(admin.JOURNAL_FILE, {"state": "switching"}, 0o600)
    monkeypatch.setattr(admin, "_service_active", lambda name: name == "reticulumpi.service")
    assert admin._status(SimpleNamespace(json=True)) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["manifest"]["version"] == "0.3.0"
    assert value["service_active"] is True
    assert value["unfinished_transaction"] is True


def test_doctor_reports_environment_failures_and_can_pass(admin_paths, monkeypatch, capsys):
    monkeypatch.setattr(admin.sys, "version_info", (3, 10))
    assert admin._doctor(SimpleNamespace()) == 1
    failures = capsys.readouterr().out
    assert "Python 3.11" in failures
    assert "missing configuration" in failures

    admin_paths.config.mkdir(parents=True)
    admin.CONFIG_FILE.write_text("reticulumpi: {}\n", encoding="utf-8")
    admin.CONFIG_FILE.chmod(0o600)
    monkeypatch.setattr(admin.sys, "version_info", (3, 11))
    real_stat = Path.stat

    def root_owned(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == admin.CONFIG_FILE:
            return SimpleNamespace(st_mode=result.st_mode, st_uid=0)
        return result

    monkeypatch.setattr(Path, "stat", root_owned)
    assert admin._doctor(SimpleNamespace()) == 0
    assert "checks passed" in capsys.readouterr().out


def test_doctor_checks_manifest_pointer_permissions_and_database_errors(
    admin_paths, monkeypatch, capsys
):
    admin_paths.config.mkdir(parents=True)
    admin.CONFIG_FILE.write_text("reticulumpi: {}\n", encoding="utf-8")
    admin.CONFIG_FILE.chmod(0o666)
    current = _release(admin_paths.root, "0.3.0")
    other = _release(admin_paths.root, "0.2.5")
    (admin_paths.root / "current").symlink_to(other)
    _write_install_manifest(admin_paths, current, None)
    database = admin_paths.data / "broken.db"
    admin_paths.data.mkdir(parents=True)
    database.write_bytes(b"not sqlite")
    real_stat = Path.stat

    def non_root_config(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == admin.CONFIG_FILE:
            return SimpleNamespace(st_mode=result.st_mode, st_uid=1)
        return result

    monkeypatch.setattr(Path, "stat", non_root_config)
    assert admin._doctor(SimpleNamespace()) == 1
    output = capsys.readouterr().out
    assert "permissions are broader" in output
    assert "not owned by root" in output
    assert "does not match install manifest" in output
    assert "cannot inspect database" in output


def test_database_discovery_and_readonly_connection(admin_paths):
    assert admin._databases() == []
    admin_paths.data.mkdir(parents=True)
    paths = [admin_paths.data / "a.db", admin_paths.data / "nested/b.sqlite3"]
    paths[1].parent.mkdir()
    for path in paths:
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE records(value TEXT)")
    assert admin._databases() == sorted(path.resolve() for path in paths)
    with closing(admin._connect_readonly(paths[0])) as connection:
        assert connection.execute("PRAGMA query_only").fetchone() is not None
    admin.shutil.rmtree(admin_paths.data)
    admin_paths.data.write_text("unsafe", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="unsafe data directory"):
        admin._databases()


def test_service_paths_and_migration_config_are_canonical(admin_paths):
    home = admin_paths.data.resolve()
    assert admin._service_home() == home
    assert admin._service_path("~", home) == str(home)
    assert admin._service_path("~/records.db", home) == str(home / "records.db")
    assert admin._service_path("relative.db", home) == "/relative.db"
    absolute = str(admin_paths.data / "absolute.db")
    assert admin._service_path(absolute, home) == absolute
    sensor = admin._normalize_migration_config("sensor_framework", {}, home)
    assert sensor["storage"]["path"].startswith(str(home))
    postgres = admin._normalize_migration_config(
        "sensor_framework", {"storage": {"type": "postgres"}}, home
    )
    assert "path" not in postgres["storage"]
    message = admin._normalize_migration_config("messaging_hub", {}, home)
    assert message["db_path"] == str(home / ".local/share/reticulumpi/messaging_hub.db")


def test_migration_target_validation_rejects_duplicate_name_path_and_directory(tmp_path):
    first = _migration_target(tmp_path / "one.db")
    names: set[str] = set()
    paths: set[Path] = set()
    admin._validate_migration_target(first, names, paths)
    with pytest.raises(admin.AdminError, match="duplicate migration target name"):
        admin._validate_migration_target(first, names, set())
    second = MigrationTarget("other", first.path, first.migrations)
    with pytest.raises(admin.AdminError, match="same database"):
        admin._validate_migration_target(second, set(), paths)
    directory = tmp_path / "directory"
    directory.mkdir()
    invalid = MigrationTarget("directory", directory, first.migrations)
    with pytest.raises(admin.AdminError, match="not a regular database"):
        admin._validate_migration_target(invalid, set(), set())


def test_migration_target_loading_rejects_missing_bad_config_and_catalog_validation(
    admin_paths, monkeypatch
):
    with pytest.raises(admin.AdminError, match="unavailable or unsafe"):
        admin._load_enabled_migration_targets()
    admin_paths.config.mkdir(parents=True)
    admin.CONFIG_FILE.write_text("reticulumpi: [", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="cannot load migration configuration"):
        admin._load_enabled_migration_targets()

    admin.CONFIG_FILE.write_text(
        "reticulumpi:\n  plugins:\n    node_location_tracker:\n      enabled: true\n",
        encoding="utf-8",
    )

    import reticulumpi.migration_catalog as migration_catalog

    monkeypatch.setattr(
        migration_catalog,
        "migration_targets",
        lambda *_args: (_ for _ in ()).throw(ValueError("invalid path")),
    )
    with pytest.raises(admin.AdminError, match="invalid migration configuration"):
        admin._load_enabled_migration_targets()


def test_database_plan_and_migrate_empty_and_error_paths(
    admin_paths, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(admin, "_load_enabled_migration_targets", lambda: ())
    assert admin._db_plan(SimpleNamespace()) == 0
    assert admin._db_migrate(SimpleNamespace(apply=False, dry_run=True)) == 0
    assert capsys.readouterr().out.count("No enabled") == 2

    target = _migration_target(tmp_path / "records.db")
    monkeypatch.setattr(admin, "_load_enabled_migration_targets", lambda: (target,))
    import reticulumpi.migrations as migrations

    monkeypatch.setattr(
        migrations,
        "plan_migrations",
        lambda _target: (_ for _ in ()).throw(MigrationError("bad history")),
    )
    with pytest.raises(admin.AdminError, match="planning failed"):
        admin._db_plan(SimpleNamespace())
    monkeypatch.setattr(
        admin,
        "_dry_run_migration",
        lambda _target: (_ for _ in ()).throw(OSError("clone failed")),
    )
    with pytest.raises(admin.AdminError, match="dry run failed"):
        admin._db_migrate(SimpleNamespace(apply=False, dry_run=True))


def test_existing_database_migration_preserves_owner_and_creates_backup(
    admin_paths, monkeypatch, capsys
):
    target = _migration_target(admin_paths.data / "records.db")
    target.path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(target.path)):
        pass
    monkeypatch.setattr(admin, "_load_enabled_migration_targets", lambda: (target,))
    monkeypatch.setattr(admin, "_require_root", lambda: None)
    monkeypatch.setattr(admin, "_maintenance_lock", lambda: nullcontext())
    monkeypatch.setattr(admin, "_service_active", lambda _name: False)
    monkeypatch.setattr(admin.os, "chown", lambda *_args: None)
    assert admin._db_migrate(SimpleNamespace(apply=True, dry_run=False)) == 0
    assert list((admin_paths.backups / "databases/records").glob("*.bak"))
    assert "backup=" in capsys.readouterr().out


def test_database_migration_missing_service_account_is_admin_error(
    admin_paths, tmp_path, monkeypatch
):
    target = _migration_target(tmp_path / "records.db")
    monkeypatch.setattr(admin, "_load_enabled_migration_targets", lambda: (target,))
    monkeypatch.setattr(admin, "_require_root", lambda: None)
    monkeypatch.setattr(admin, "_maintenance_lock", lambda: nullcontext())
    monkeypatch.setattr(admin, "_service_active", lambda _name: False)
    monkeypatch.setattr(
        admin.pwd, "getpwnam", lambda _name: (_ for _ in ()).throw(KeyError("missing"))
    )
    with pytest.raises(admin.AdminError, match="service user"):
        admin._db_migrate(SimpleNamespace(apply=True, dry_run=False))


def test_database_backup_cleanup_and_listing(admin_paths, monkeypatch, capsys):
    admin_paths.data.mkdir(parents=True)
    database = admin_paths.data / "records.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE records(value TEXT)")
    monkeypatch.setattr(admin, "_require_root", lambda: None)
    monkeypatch.setattr(admin, "_maintenance_lock", lambda: nullcontext())
    monkeypatch.setattr(
        admin,
        "_sqlite_backup_file",
        lambda *_args: (_ for _ in ()).throw(admin.AdminError("backup failed")),
    )
    with pytest.raises(admin.AdminError, match="backup failed"):
        admin._db_backup(SimpleNamespace(apply=True, dry_run=False))
    assert not list(admin_paths.backups.glob("db-*"))
    assert admin._db_backups(SimpleNamespace()) == 0
    assert capsys.readouterr().out == ""
    for name in ("db-2026", "db-safety-2026", "release-ignore"):
        (admin_paths.backups / name).mkdir()
    admin._db_backups(SimpleNamespace())
    output = capsys.readouterr().out
    assert "db-2026" in output and "db-safety-2026" in output
    assert "release-ignore" not in output


def test_database_restore_validation_service_and_migration_errors(
    admin_paths, tmp_path, monkeypatch, capsys
):
    missing = tmp_path / "missing.db"
    with pytest.raises(admin.AdminError, match="regular file"):
        admin._db_restore(
            SimpleNamespace(
                backup=str(missing),
                database=str(admin_paths.data / "target.db"),
                apply=False,
                dry_run=True,
            )
        )
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("CREATE TABLE records(value TEXT)")
    admin_paths.data.mkdir(parents=True)
    target = admin_paths.data / "target.db"
    target.symlink_to(source)
    with pytest.raises(admin.AdminError, match="target may not be a symlink"):
        admin._db_restore(
            SimpleNamespace(backup=str(source), database=str(target), apply=False, dry_run=True)
        )
    target.unlink()
    same = admin_paths.data / "same.db"
    admin.shutil.copy2(source, same)
    with pytest.raises(admin.AdminError, match="must be different"):
        admin._db_restore(
            SimpleNamespace(backup=str(same), database=str(same), apply=False, dry_run=True)
        )
    admin.shutil.copy2(source, target)
    assert (
        admin._db_restore(
            SimpleNamespace(backup=str(source), database=str(target), apply=False, dry_run=True)
        )
        == 0
    )
    assert "safety backup" in capsys.readouterr().out
    monkeypatch.setattr(admin, "_require_root", lambda: None)
    monkeypatch.setattr(admin, "_maintenance_lock", lambda: nullcontext())
    monkeypatch.setattr(admin, "_service_active", lambda _name: True)
    with pytest.raises(admin.AdminError, match="stop reticulumpi.service"):
        admin._db_restore(
            SimpleNamespace(backup=str(source), database=str(target), apply=True, dry_run=False)
        )

    monkeypatch.setattr(admin, "_service_active", lambda _name: False)
    import reticulumpi.migrations as migrations

    monkeypatch.setattr(
        migrations,
        "restore_database",
        lambda *_args: (_ for _ in ()).throw(MigrationError("restore failed")),
    )
    with pytest.raises(admin.AdminError, match="restore failed"):
        admin._db_restore(
            SimpleNamespace(backup=str(source), database=str(target), apply=True, dry_run=False)
        )


def test_main_dispatches_success_and_converts_expected_errors(admin_paths, monkeypatch, capsys):
    monkeypatch.setattr(admin, "_service_active", lambda _name: False)
    assert admin.main(["status", "--json"]) == 0
    assert "service_active" in capsys.readouterr().out
    with pytest.raises(SystemExit) as expected:
        admin.main(["rollback", "--dry-run"])
    assert expected.value.code == 2

    parser = SimpleNamespace()
    parser.parse_args = lambda _argv: SimpleNamespace(
        handler=lambda _args: (_ for _ in ()).throw(
            subprocess.CalledProcessError(7, ["systemctl", "start"])
        )
    )
    parser.error = lambda message: (_ for _ in ()).throw(RuntimeError(message))
    monkeypatch.setattr(admin, "_build_parser", lambda: parser)
    with pytest.raises(RuntimeError, match=r"command failed \(7\): systemctl start"):
        admin.main([])


def test_additional_version_bundle_and_root_security_branches(tmp_path, monkeypatch):
    source = _source_bundle(tmp_path / "source")
    version_file = source / "src/reticulumpi/_version.py"
    version_file.parent.mkdir(parents=True, exist_ok=True)
    version_file.write_text(
        '"module doc"\nOTHER = "ignored"\n__version__ = 3\n__version__ = "0.3.0"\n',
        encoding="utf-8",
    )
    assert admin._generated_scm_version(source) == "0.3.0"
    missing_project = tmp_path / "missing-project"
    missing_project.mkdir()
    with pytest.raises(admin.AdminError, match="no pyproject"):
        admin._source_metadata(missing_project)
    (source / "pyproject.toml").write_text(
        '[project]\nname = "reticulumpi"\ndynamic = []\n', encoding="utf-8"
    )
    with pytest.raises(admin.AdminError, match="declares no version"):
        admin._source_metadata(source)

    wheel = tmp_path / "reticulumpi-0.3.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    verified = []
    monkeypatch.setattr(
        admin,
        "_verify_signed_bundle",
        lambda bundle, candidate: verified.append((bundle, candidate)),
    )
    admin._verify_bundle(wheel, None)
    assert verified == [(wheel, None)]
    monkeypatch.setattr(admin.os, "geteuid", lambda: 0)
    admin._require_root()


def test_lock_key_manifest_and_directory_error_branches(admin_paths, tmp_path, monkeypatch):
    admin.LOCK_FILE.parent.mkdir(parents=True)
    target = tmp_path / "target"
    target.write_text("target", encoding="utf-8")
    admin.LOCK_FILE.symlink_to(target)
    with pytest.raises(admin.AdminError, match="lock may not be a symlink"):
        with admin._maintenance_lock():
            pass

    missing_key = tmp_path / "missing.pub"
    monkeypatch.setattr(admin, "RELEASE_PUBLIC_KEY_FILE", missing_key)
    with pytest.raises(admin.AdminError, match="unavailable"):
        admin._trusted_release_public_key()
    monkeypatch.setattr(admin, "RELEASE_PUBLIC_KEY_FILE", Path("/etc/hosts"))
    assert admin._trusted_release_public_key() == Path("/etc/hosts")
    with pytest.raises(admin.AdminError, match="cannot read bundle hash manifest"):
        admin._read_hash_manifest(tmp_path / "missing-manifest")

    directory = tmp_path / "directory"
    directory.mkdir()
    monkeypatch.setattr(Path, "mkdir", lambda *_args, **_kwargs: None)
    with pytest.raises(admin.AdminError, match="expected a real directory"):
        admin._ensure_real_directory(tmp_path / "not-created")


def test_readiness_account_and_wait_error_branches(admin_paths, monkeypatch):
    configured_getpwnam = admin.pwd.getpwnam
    monkeypatch.setattr(
        admin.pwd, "getpwnam", lambda _name: (_ for _ in ()).throw(KeyError("missing"))
    )
    with pytest.raises(admin.AdminError, match="service user"):
        admin._service_account()
    monkeypatch.setattr(admin.pwd, "getpwnam", configured_getpwnam)

    admin_paths.run.mkdir(parents=True)
    marker = admin_paths.run / "ready"
    target = admin_paths.run / "target"
    target.write_text("ready\n", encoding="utf-8")
    marker.symlink_to(target)
    with pytest.raises(admin.AdminError, match="may not be a symlink"):
        admin._clear_application_readiness()
    marker.unlink()
    assert admin._readiness_marker_valid() is False

    marker.write_text("ready\n", encoding="utf-8")
    marker.chmod(0o600)
    real_read_bytes = Path.read_bytes

    def fail_marker(path):
        if path == marker:
            raise OSError("read failed")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_marker)
    with pytest.raises(admin.AdminError, match="cannot read"):
        admin._readiness_marker_valid()


def test_wait_loops_cover_unstable_active_and_readiness_states(monkeypatch):
    monkeypatch.setattr(admin.time, "sleep", lambda _seconds: None)
    states = iter([True, True, False])
    monkeypatch.setattr(admin, "_service_active", lambda _name: next(states, False))
    times = iter([0.0, 0.1, 0.2, 0.3, 0.4, 1.1])
    monkeypatch.setattr(admin.time, "monotonic", lambda: next(times, 1.1))
    with pytest.raises(admin.AdminError, match="did not remain active"):
        admin._wait_service_active("unit", timeout=1, stable_for=10)

    service_states = iter([False, True, True])
    marker_states = iter([False, True])
    monkeypatch.setattr(admin, "_service_active", lambda _name: next(service_states))
    monkeypatch.setattr(admin, "_readiness_marker_valid", lambda: next(marker_states))
    times = iter([0.0, 0.1, 0.2, 0.3, 0.4, 0.5])
    monkeypatch.setattr(admin.time, "monotonic", lambda: next(times, 0.5))
    admin._wait_application_ready(timeout=1, stable_for=0)


def test_sqlite_state_root_and_fsync_fail_closed(admin_paths, tmp_path, monkeypatch):
    class BadIntegrity:
        def execute(self, _sql):
            return SimpleNamespace(fetchone=lambda: ("corrupt",))

        def close(self):
            pass

    real_connect = admin.sqlite3.connect
    monkeypatch.setattr(admin.sqlite3, "connect", lambda *_args, **_kwargs: BadIntegrity())
    with pytest.raises(admin.AdminError, match="integrity check failed"):
        admin._verify_sqlite(tmp_path / "database")
    monkeypatch.setattr(admin.sqlite3, "connect", real_connect)

    monkeypatch.setattr(
        admin.pwd,
        "getpwnam",
        lambda _name: SimpleNamespace(pw_dir="relative/home", pw_uid=1, pw_gid=1),
    )
    with pytest.raises(admin.AdminError, match="invalid home"):
        admin._state_roots(())

    real_open = admin.os.open
    real_fsync = admin.os.fsync
    monkeypatch.setattr(
        admin.os, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("open"))
    )
    with pytest.raises(admin.AdminError, match="cannot open durable state"):
        admin._fsync_state_directory(tmp_path)
    monkeypatch.setattr(admin.os, "open", real_open)
    descriptor = real_open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(admin.os, "open", lambda *_args, **_kwargs: descriptor)
    monkeypatch.setattr(admin.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("fsync")))
    with pytest.raises(admin.AdminError, match="cannot fsync durable state"):
        admin._fsync_state_directory(tmp_path)
    monkeypatch.setattr(admin.os, "open", real_open)
    monkeypatch.setattr(admin.os, "fsync", real_fsync)


def test_tree_hash_and_copy_race_guards(tmp_path, monkeypatch):
    regular = tmp_path / "regular"
    regular.write_text("payload", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="not a real directory"):
        admin._tree_entries(regular)
    with pytest.raises(admin.AdminError, match="cannot open durable state file"):
        admin._hash_regular_file(tmp_path / "missing")
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(admin.AdminError, match="not regular"):
        admin._hash_regular_file(directory)

    source_stat = regular.stat()
    wrong_stat = SimpleNamespace(
        st_ino=source_stat.st_ino + 1,
        st_uid=source_stat.st_uid,
        st_gid=source_stat.st_gid,
        st_mode=source_stat.st_mode,
        st_atime_ns=source_stat.st_atime_ns,
        st_mtime_ns=source_stat.st_mtime_ns,
    )
    with pytest.raises(admin.AdminError, match="changed before copy"):
        admin._copy_regular_file(regular, tmp_path / "copy", wrong_stat)

    real_write = admin.os.write
    monkeypatch.setattr(admin.os, "write", lambda _fd, _view: 0)
    with pytest.raises(admin.AdminError, match="short write"):
        admin._copy_regular_file(regular, tmp_path / "short", source_stat)
    monkeypatch.setattr(admin.os, "write", real_write)


def test_legacy_merge_type_conflicts_and_nested_directory_copy(admin_paths):
    legacy = admin_paths.home / ".reticulum"
    canonical = admin_paths.data / ".reticulum"
    (legacy / "nested").mkdir(parents=True)
    (legacy / "nested/value").write_text("value", encoding="utf-8")
    canonical.mkdir(parents=True)
    admin._merge_tree_atomically(legacy, canonical)
    assert (canonical / "nested/value").read_text(encoding="utf-8") == "value"

    admin.shutil.rmtree(legacy)
    admin.shutil.rmtree(canonical)
    (legacy / "conflict").mkdir(parents=True)
    canonical.mkdir(parents=True)
    (canonical / "conflict").write_text("file", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="type conflict"):
        admin._merge_tree_atomically(legacy, canonical)

    admin.shutil.rmtree(legacy)
    admin.shutil.rmtree(canonical)
    legacy.mkdir(parents=True)
    (legacy / "conflict").write_text("file", encoding="utf-8")
    (canonical / "conflict").mkdir(parents=True)
    with pytest.raises(admin.AdminError, match="type conflict"):
        admin._merge_tree_atomically(legacy, canonical)

    with pytest.raises(admin.AdminError, match="not a real directory"):
        admin._merge_tree_atomically(admin_paths.home / "missing", canonical)


def test_identity_and_legacy_cleanup_guard_branches(admin_paths, tmp_path):
    assert admin._identity_files(tmp_path / "missing") == {}
    missing = admin.LegacyMigration(
        tmp_path / "legacy", tmp_path / "canonical", {"identity": "0" * 64}
    )
    with pytest.raises(admin.AdminError, match="identity migration"):
        admin._verify_migrated_identities((missing,))
    admin._remove_migrated_legacy_state((missing,))
    target = tmp_path / "target"
    target.mkdir()
    missing.source.symlink_to(target, target_is_directory=True)
    with pytest.raises(admin.AdminError, match="cleanup path is unsafe"):
        admin._remove_migrated_legacy_state((missing,))

    admin_paths.config.mkdir(parents=True)
    for configured, expected in (
        ("~", admin_paths.data),
        ("relative/identity", Path("/relative/identity")),
        (str(tmp_path / "absolute-identity"), tmp_path / "absolute-identity"),
    ):
        admin.CONFIG_FILE.write_text(
            f"reticulumpi:\n  identity_path: {configured}\n", encoding="utf-8"
        )
        assert admin._configured_identity_path(admin_paths.data) == expected.resolve()
    assert admin._identity_key(tmp_path / "outside", ()) == f"absolute:{tmp_path / 'outside'}"
    admin.CONFIG_FILE.write_text(
        f"reticulumpi:\n  identity_path: {tmp_path / 'directory'}\n", encoding="utf-8"
    )
    (tmp_path / "directory").mkdir(exist_ok=True)
    with pytest.raises(admin.AdminError, match="configured identity"):
        admin._identity_hashes(())
    assert admin._state_databases(admin.StateRoot("missing", tmp_path / "missing-root")) == []


def test_obsolete_managed_symlinks_are_never_unlinked(admin_paths, tmp_path):
    source = _complete_source_bundle(tmp_path / "source")
    admin_paths.systemd.mkdir(parents=True)
    target = tmp_path / "target"
    target.write_text("target", encoding="utf-8")
    (admin_paths.systemd / "rnsd.service").symlink_to(target)
    with pytest.raises(admin.AdminError, match="managed unit"):
        admin._render_units(source, admin_paths.root, (), ("shared-rnsd",))
    (admin_paths.systemd / "rnsd.service").unlink()
    dropin = admin_paths.systemd / admin._RNSD_DROPIN_RELATIVE
    dropin.parent.mkdir(parents=True, exist_ok=True)
    dropin.symlink_to(target)
    with pytest.raises(admin.AdminError, match="managed drop-in"):
        admin._render_units(source, admin_paths.root, (), ("shared-rnsd",))

    admin_paths.libexec.mkdir(parents=True)
    (admin_paths.libexec / "chrony_helper.sh").symlink_to(target)
    with pytest.raises(admin.AdminError, match="managed helper"):
        admin._install_helpers(source, (), ("chrony-control",))
    (admin_paths.libexec / "chrony_helper.sh").unlink()
    admin_paths.shared.mkdir(parents=True)
    (admin_paths.shared / "offline_profile.yaml").symlink_to(target)
    with pytest.raises(admin.AdminError, match="managed offline profile"):
        admin._install_helpers(source, (), ("offline-tools",))


def test_file_transfer_yaml_ambiguities_and_atomic_validation(admin_paths, monkeypatch):
    admin_paths.config.mkdir(parents=True)
    invalid_configs = [
        (
            "reticulumpi:\n  plugins:\n    file_transfer:\n      access_policy: deny\n"
            "      access_policy: open\n",
            "duplicate file_transfer.access_policy",
        ),
        (
            "reticulumpi:\n  plugins:\n    file_transfer:\n      allowed_identities: []\n"
            "      allowed_identities: []\n",
            "duplicate file_transfer.allowed_identities",
        ),
        (
            "reticulumpi:\n  plugins:\n    file_transfer:\n      allowed_identities: invalid\n",
            "expected a YAML list",
        ),
        (
            "reticulumpi:\n  plugins:\n    file_transfer:\n      allowed_identities:\n"
            "        identity: invalid\n",
            "expected list items",
        ),
    ]
    for content, message in invalid_configs:
        admin.CONFIG_FILE.write_text(content, encoding="utf-8")
        with pytest.raises(admin.AdminError, match=message):
            admin._plan_file_transfer_policy_migration()

    admin.CONFIG_FILE.unlink()
    target = admin_paths.config / "target"
    target.write_text("config", encoding="utf-8")
    admin.CONFIG_FILE.symlink_to(target)
    with pytest.raises(admin.AdminError, match="symlink or special"):
        admin._plan_file_transfer_policy_migration()
    with pytest.raises(admin.AdminError, match="symlink or special"):
        admin._plan_legacy_config_path_migration()
    admin.CONFIG_FILE.unlink()
    admin.CONFIG_FILE.write_text("reticulumpi:\n  plugins:\n    file_transfer:\n", encoding="utf-8")
    migration = admin._plan_file_transfer_policy_migration()
    assert migration is not None
    invalid = admin.FileTransferPolicyMigration(
        migration.policy, 999, migration.indentation, migration.source_sha256
    )
    with pytest.raises(admin.AdminError, match="insertion point"):
        admin._apply_file_transfer_policy_migration(invalid)

    monkeypatch.setattr(admin, "_plan_file_transfer_policy_migration", lambda *_args: migration)
    with pytest.raises(admin.AdminError, match="did not validate"):
        admin._apply_file_transfer_policy_migration(migration)


@pytest.mark.parametrize(
    ("features", "expected"),
    [
        ((), "core"),
        (("dashboard",), "dashboard-nomadnet"),
        (("nomadnet", "dashboard", "watchdog"), "dashboard-nomadnet"),
        (("dashboard", "sensors"), "all-features"),
        (("meshtastic",), "all-features"),
    ],
)
def test_dependency_profile_selection_is_complete(features, expected):
    assert admin._dependency_profile_name(features) == expected


@pytest.mark.parametrize("scheme", ["canonical", "legacy"])
@pytest.mark.parametrize(
    ("features", "profile_name"),
    [
        ((), "core"),
        (("dashboard",), "dashboard-nomadnet"),
        (("sensors",), "all-features"),
    ],
)
def test_signed_dependency_profiles_accept_one_complete_filename_scheme(
    tmp_path, scheme, features, profile_name
):
    profiles = (
        admin._DEPENDENCY_PROFILES if scheme == "canonical" else admin._LEGACY_DEPENDENCY_PROFILES
    )
    _write_fixture_dependency_profiles(tmp_path, profiles)

    selected = admin._dependency_profile_path(tmp_path, tmp_path, features)

    assert selected.name == profiles[profile_name]


def test_dependency_profile_scheme_rejects_alias_duplicates_and_mixed_names(tmp_path):
    constraints = tmp_path / "constraints"
    constraints.mkdir()
    entries = []
    for profiles in (admin._DEPENDENCY_PROFILES, admin._LEGACY_DEPENDENCY_PROFILES):
        filename = profiles["core"]
        profile = constraints / filename
        profile.write_text(f"fixture==1 --hash=sha256:{'a' * 64}\n", encoding="utf-8")
        entries.append(f"{admin._sha256(profile)}  constraints/{filename}")
    (tmp_path / admin.BUNDLE_MANIFEST_NAME).write_text("\n".join(entries) + "\n", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="ambiguous canonical and legacy"):
        admin._dependency_profile_path(tmp_path, tmp_path, ())

    for path in constraints.iterdir():
        path.unlink()
    canonical_core = constraints / admin._DEPENDENCY_PROFILES["core"]
    legacy_dashboard = constraints / admin._LEGACY_DEPENDENCY_PROFILES["dashboard-nomadnet"]
    canonical_core.write_text(f"core==1 --hash=sha256:{'a' * 64}\n", encoding="utf-8")
    legacy_dashboard.write_text(f"dashboard==1 --hash=sha256:{'b' * 64}\n", encoding="utf-8")
    (tmp_path / admin.BUNDLE_MANIFEST_NAME).write_text(
        f"{admin._sha256(canonical_core)}  constraints/{canonical_core.name}\n"
        f"{admin._sha256(legacy_dashboard)}  constraints/{legacy_dashboard.name}\n",
        encoding="utf-8",
    )
    with pytest.raises(admin.AdminError, match="mixes canonical and legacy"):
        admin._dependency_profile_path(tmp_path, tmp_path, ("dashboard",))


def test_signed_canonical_profile_never_falls_back_to_unlisted_legacy_alias(tmp_path):
    constraints = tmp_path / "constraints"
    constraints.mkdir()
    canonical = constraints / admin._DEPENDENCY_PROFILES["core"]
    legacy = constraints / admin._LEGACY_DEPENDENCY_PROFILES["core"]
    legacy.write_text(f"fixture==1 --hash=sha256:{'a' * 64}\n", encoding="utf-8")
    (tmp_path / admin.BUNDLE_MANIFEST_NAME).write_text(
        f"{'b' * 64}  constraints/{canonical.name}\n", encoding="utf-8"
    )

    with pytest.raises(admin.AdminError, match="missing the core"):
        admin._dependency_profile_path(tmp_path, tmp_path, ())


def test_signed_dependency_profile_is_required_for_source_and_wheel(tmp_path, monkeypatch):
    source = _source_bundle(tmp_path / "source")
    _sign_source(source)
    monkeypatch.setattr(admin, "_verify_minisign", lambda *_args: None)
    selected = admin._dependency_profile_path(source, source, ("dashboard", "sensors"))
    assert selected.name == "production-universal-all-features.txt"

    selected.write_text(
        f"tampered==1 --hash=sha256:{'b' * 64}\n",
        encoding="utf-8",
    )
    with pytest.raises(admin.AdminError, match="checksum mismatch"):
        admin._dependency_profile_path(source, source, ("sensors",))

    wheel_directory = tmp_path / "wheel-bundle"
    wheel_directory.mkdir()
    wheel = _wheel(wheel_directory / "reticulumpi-0.3.0-py3-none-any.whl")
    manifest = wheel.parent / admin.BUNDLE_MANIFEST_NAME
    entries = admin._read_hash_manifest(manifest)
    entries[wheel.name] = admin._sha256(wheel)
    manifest.write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(entries.items())),
        encoding="utf-8",
    )
    (wheel.parent / admin.BUNDLE_SIGNATURE_NAME).write_text("signature\n", encoding="utf-8")
    admin._verify_signed_bundle(wheel, None)
    assert admin._dependency_profile_path(wheel, None, ()).name.endswith("core.txt")


def test_candidate_install_uses_hash_lock_then_no_deps_wheel(admin_paths, tmp_path, monkeypatch):
    source = _complete_source_bundle(tmp_path / "source")
    wheel = _dashboard_wheel(tmp_path / "reticulumpi-0.3.0-py3-none-any.whl")
    commands = _mock_apply_runtime(monkeypatch, wheel)
    platform_profile = admin.select_platform_profile(
        system="Linux",
        machine="aarch64",
        version_info=(3, 12, 3),
        os_release={"ID": "ubuntu", "VERSION_CODENAME": "noble", "VERSION_ID": "24.04"},
    ).as_metadata()
    monkeypatch.setattr(admin, "_preflight_platform", lambda: platform_profile)
    args = SimpleNamespace(
        install_root=str(admin_paths.root),
        bundle=str(source),
        feature=["dashboard", "sensors"],
        apply=True,
        dry_run=False,
        start=False,
    )
    assert admin._apply_release(args, "install") == 0
    installs = [command for command in commands if "install" in command]
    dependency = next(command for command in installs if "--require-hashes" in command)
    project = next(command for command in installs if "--no-deps" in command)
    assert "--requirement" in dependency
    assert "--only-binary" not in dependency
    assert project[-1].endswith(".whl")
    assert all("reticulumpi[" not in token for command in installs for token in command)
    assert commands.index(dependency) < commands.index(project)
    manifest = admin._load_manifest(admin_paths.root)
    assert manifest["platform_profile"] == platform_profile


def test_platform_preflight_is_explicit_and_testable(tmp_path):
    metadata = tmp_path / "os-release"
    metadata.write_text('ID=debian\nVERSION_CODENAME="bookworm"\n', encoding="utf-8")
    assert admin._read_os_release(metadata)["VERSION_CODENAME"] == "bookworm"
    result = admin._preflight_platform(
        system="Linux",
        machine="aarch64",
        version_info=(3, 11, 9),
        os_release={"ID": "debian", "VERSION_CODENAME": "bookworm"},
    )
    assert result["architecture"] == "arm64"
    assert result["python"] == "3.11.9"
    assert result["profile_key"] == "linux-arm64-debian-bookworm-py311"

    noble = admin._preflight_platform(
        system="Linux",
        machine="aarch64",
        version_info=(3, 12, 3),
        os_release={"ID": "ubuntu", "VERSION_CODENAME": "noble", "VERSION_ID": "24.04"},
    )
    assert noble["profile_key"] == "linux-arm64-ubuntu-noble-py312"
    assert noble["dependency_profiles"] == admin._DEPENDENCY_PROFILES

    invalid = [
        {"system": "Darwin"},
        {"machine": "x86_64"},
        {"version_info": (3, 12, 0)},
        {"os_release": {"ID": "debian", "VERSION_CODENAME": "trixie"}},
        {
            "os_release": {
                "ID": "ubuntu",
                "VERSION_CODENAME": "noble",
                "VERSION_ID": "24.04",
            }
        },
    ]
    baseline = {
        "system": "Linux",
        "machine": "arm64",
        "version_info": (3, 11, 9),
        "os_release": {"ID": "raspbian", "VERSION_CODENAME": "bookworm"},
    }
    for override in invalid:
        with pytest.raises(admin.AdminError):
            admin._preflight_platform(**{**baseline, **override})


def test_persisted_platform_metadata_accepts_coherent_legacy_locks_only():
    canonical = admin.select_platform_profile(
        system="Linux",
        machine="aarch64",
        version_info=(3, 12, 3),
        os_release={"ID": "ubuntu", "VERSION_CODENAME": "noble", "VERSION_ID": "24.04"},
    ).as_metadata()
    assert admin._validate_platform_metadata(canonical) == canonical

    legacy = {
        **canonical,
        "dependency_lock_set": admin.LEGACY_UNIVERSAL_HASH_LOCK_SET,
        "dependency_profiles": admin._LEGACY_DEPENDENCY_PROFILES,
    }
    assert admin._validate_platform_metadata(legacy) == legacy

    for invalid in (
        {**canonical, "dependency_lock_set": admin.LEGACY_UNIVERSAL_HASH_LOCK_SET},
        {
            **legacy,
            "dependency_lock_set": admin.UNIVERSAL_HASH_LOCK_SET,
        },
        {
            **canonical,
            "dependency_profiles": {
                **admin._DEPENDENCY_PROFILES,
                "extra": "production-universal-extra.txt",
            },
        },
    ):
        with pytest.raises(admin.AdminError, match="dependency locks are invalid"):
            admin._validate_platform_metadata(invalid)


def test_custom_legacy_layout_comes_from_installed_unit(admin_paths):
    custom_home = admin_paths.home.parent / "custom-service-state"
    custom_checkout = admin_paths.home.parent / "custom-checkout"
    custom_config = admin_paths.home.parent / "custom-config/config.yaml"
    (custom_home / ".reticulum").mkdir(parents=True)
    admin_paths.systemd.mkdir(parents=True)
    (admin_paths.systemd / "reticulumpi.service").write_text(
        "[Service]\n"
        f'Environment="HOME={custom_home}" "XDG_CONFIG_HOME={custom_home}/.config"\n'
        f"WorkingDirectory={custom_checkout}\n"
        f"ExecStart={custom_checkout}/.venv/bin/reticulumpi --config {custom_config}\n",
        encoding="utf-8",
    )
    layout = admin._discover_legacy_layout()
    assert layout.homes[0] == custom_home
    assert custom_checkout in layout.install_roots
    assert custom_config in layout.config_files
    mappings = admin._legacy_state_destinations(())
    assert (custom_home / ".reticulum", admin.DATA_DIR / ".reticulum") in mappings


def _inactive_service_evidence() -> dict[str, dict[str, bool]]:
    return {name: {"active": False, "enabled": False} for name in admin._TRANSACTION_SERVICE_NAMES}


def test_bootstrap_routes_manifestless_installed_legacy_layout_to_upgrade(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    source = (repository / "scripts/bootstrap.sh").read_text(encoding="utf-8")
    fake_etc = tmp_path / "etc"
    unit = fake_etc / "systemd/system/reticulumpi.service"
    config = fake_etc / "reticulumpi/config.yaml"
    manifest = fake_etc / "reticulumpi/install.json"
    fake_admin = tmp_path / "usr/sbin/reticulumpi-admin"
    fallback_admin = tmp_path / "usr/bin/reticulumpi-admin"
    bundle = tmp_path / "bundle"
    unit.parent.mkdir(parents=True)
    config.parent.mkdir(parents=True)
    fake_admin.parent.mkdir(parents=True)
    fallback_admin.parent.mkdir(parents=True)
    bundle.mkdir()
    unit.write_text("[Service]\nExecStart=/opt/reticulumpi/.venv/bin/reticulumpi\n")
    config.write_text("reticulumpi: {}\n", encoding="utf-8")
    fake_admin.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    fake_admin.chmod(0o755)

    replacements = {
        "/etc/reticulumpi/install.json": str(manifest),
        "/etc/systemd/system/reticulumpi.service": str(unit),
        "/etc/reticulumpi/config.yaml": str(config),
        "/usr/sbin/reticulumpi-admin": str(fake_admin),
        "/usr/bin/reticulumpi-admin": str(fallback_admin),
    }
    for old, new in replacements.items():
        source = source.replace(old, new)
    source = source.replace(
        'if is_trusted_admin "$candidate"; then',
        'if [ -x "$candidate" ]; then',
    )
    launcher = tmp_path / "bootstrap.sh"
    launcher.write_text(source, encoding="utf-8")
    launcher.chmod(0o755)

    def command() -> str:
        result = subprocess.run(
            [str(launcher), "--bundle", str(bundle), "--dry-run"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.splitlines()[0]

    assert command() == "upgrade"
    unit.unlink()
    config.unlink()
    assert command() == "install"
    manifest.write_text("{}\n", encoding="utf-8")
    assert command() == "upgrade"


def test_legacy_feature_validation_infers_production_privilege_and_lora_plugins(admin_paths):
    admin_paths.config.mkdir(parents=True)
    admin.CONFIG_FILE.write_text(
        "reticulumpi:\n"
        "  plugins:\n"
        "    lora_diagnostics:\n"
        "      enabled: true\n"
        "    captive_portal:\n"
        "      enabled: true\n"
        "    ntp_server:\n"
        "      enabled: true\n",
        encoding="utf-8",
    )

    with pytest.raises(admin.AdminError) as rejected:
        admin._validate_legacy_bridge_features((), admin.CONFIG_FILE)
    message = str(rejected.value)
    assert "lora" in message
    assert "captive-portal" in message
    assert "chrony-control" in message

    admin._validate_legacy_bridge_features(
        ("captive-portal", "chrony-control", "lora"),
        admin.CONFIG_FILE,
    )


def test_interrupted_switch_recovers_pointer_manifest_state_units_and_services(
    admin_paths, monkeypatch
):
    previous = _release(admin_paths.root, "0.2.5")
    candidate = _release(admin_paths.root, "0.3.0")
    (admin_paths.root / "current").symlink_to(previous)
    _write_install_manifest(admin_paths, previous, None)
    admin.CONFIG_FILE.write_text("old-config\n", encoding="utf-8")
    admin_paths.data.mkdir(parents=True)
    state = admin_paths.data / "state.txt"
    state.write_text("old-state\n", encoding="utf-8")
    admin_paths.systemd.mkdir(parents=True)
    unit = admin_paths.systemd / "reticulumpi.service"
    unit.write_text("old-unit\n", encoding="utf-8")
    snapshots = admin._snapshot_files(admin._managed_paths())
    backup = admin._backup_state("0.2.5")
    admin._persist_file_snapshots(backup, snapshots)

    admin._switch_release(admin_paths.root, candidate)
    _write_install_manifest(admin_paths, candidate, previous)
    admin.CONFIG_FILE.write_text("candidate-config\n", encoding="utf-8")
    state.write_text("candidate-state\n", encoding="utf-8")
    unit.write_text("candidate-unit\n", encoding="utf-8")
    admin._atomic_json(
        admin.JOURNAL_FILE,
        {
            "schema": 1,
            "operation": "upgrade",
            "install_root": str(admin_paths.root),
            "previous_release": str(previous),
            "new_release": str(candidate),
            "remove_candidate": True,
            "backup": str(backup),
            "services_before": _inactive_service_evidence(),
            "state": "switching",
        },
        0o600,
    )
    commands = []
    monkeypatch.setattr(admin, "_run", lambda command, **_kwargs: commands.append(command) or "")
    monkeypatch.setattr(admin, "_unit_enabled", lambda _name: False)

    recovered = admin._recover_interrupted_transaction(admin_paths.root.resolve())
    assert recovered is not None and recovered["state"] == "recovered"
    assert (admin_paths.root / "current").resolve() == previous
    assert json.loads(admin.MANIFEST_FILE.read_text(encoding="utf-8"))["release"] == str(previous)
    assert admin.CONFIG_FILE.read_text(encoding="utf-8") == "old-config\n"
    assert state.read_text(encoding="utf-8") == "old-state\n"
    assert unit.read_text(encoding="utf-8") == "old-unit\n"
    assert not candidate.exists()
    assert recovered["recovery_evidence"]["services_restored"] is True
    assert [admin.SYSTEMCTL, "daemon-reload"] in commands


def test_release_switch_repairs_service_traversal_on_install_root(admin_paths):
    candidate = _release(admin_paths.root, "0.3.0")
    admin_paths.root.chmod(0o700)

    admin._switch_release(admin_paths.root, candidate)

    assert stat.S_IMODE(admin_paths.root.stat().st_mode) == 0o755
    assert (admin_paths.root / "current").resolve() == candidate


def test_release_permissions_are_service_accessible_under_restrictive_umask(tmp_path):
    release = tmp_path / "release"
    package = release / ".venv/lib/python3.11/site-packages/reticulumpi"
    package.mkdir(parents=True)
    module = package / "app.py"
    module.write_text("pass\n", encoding="utf-8")
    executable = release / ".venv/bin/reticulumpi"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    for directory in (release, release / ".venv", package, executable.parent):
        directory.chmod(0o700)
    module.chmod(0o600)
    executable.chmod(0o700)

    admin._normalize_release_permissions(release)

    assert stat.S_IMODE(release.stat().st_mode) == 0o755
    assert stat.S_IMODE(package.stat().st_mode) == 0o755
    assert stat.S_IMODE(module.stat().st_mode) == 0o644
    assert stat.S_IMODE(executable.stat().st_mode) == 0o755


def test_prepare_paths_normalizes_every_service_state_ancestor(admin_paths, tmp_path, monkeypatch):
    source = _source_bundle(tmp_path / "source")
    chowns = []
    monkeypatch.setattr(admin.os, "chown", lambda *args: chowns.append(args))
    previous_umask = os.umask(0o077)
    try:
        admin._prepare_paths(source)
    finally:
        os.umask(previous_umask)

    for relative in (
        ".config",
        ".config/reticulumpi",
        ".local",
        ".local/share",
        ".local/share/reticulumpi",
        "meshchat",
    ):
        directory = admin_paths.data / relative
        assert stat.S_IMODE(directory.stat().st_mode) == 0o750
        assert directory.stat().st_uid == os.getuid()
    assert (admin_paths.data / "meshchat", os.getuid(), os.getgid()) in chowns


def test_interrupted_recovery_fails_closed_without_backup_or_on_tamper(admin_paths):
    previous = _release(admin_paths.root, "0.2.5")
    candidate = _release(admin_paths.root, "0.3.0")
    (admin_paths.root / "current").symlink_to(candidate)
    admin_paths.data.mkdir(parents=True)
    base = {
        "schema": 1,
        "operation": "upgrade",
        "install_root": str(admin_paths.root),
        "previous_release": str(previous),
        "new_release": str(candidate),
        "remove_candidate": True,
        "backup": None,
        "services_before": _inactive_service_evidence(),
        "state": "preparing",
    }
    admin._atomic_json(admin.JOURNAL_FILE, base, 0o600)
    with pytest.raises(admin.AdminError, match="current/previous mismatch"):
        admin._recover_interrupted_transaction(admin_paths.root.resolve())
    assert (admin_paths.root / "current").resolve() == candidate

    admin._switch_release(admin_paths.root, previous)
    base.update(state="switching", backup=str(admin_paths.backups / "release-missing"))
    admin._atomic_json(admin.JOURNAL_FILE, base, 0o600)
    with pytest.raises(admin.AdminError, match="missing or unsafe"):
        admin._recover_interrupted_transaction(admin_paths.root.resolve())


def test_status_and_doctor_surface_recovery_evidence(admin_paths, capsys, monkeypatch):
    admin_paths.data.mkdir(parents=True)
    admin._atomic_json(
        admin.JOURNAL_FILE,
        {
            "state": "recovered",
            "operation": "upgrade",
            "backup": "/verified/backup",
            "recovered_at": "2026-07-11T00:00:00Z",
            "recovery_evidence": {"services_restored": True},
        },
        0o600,
    )
    monkeypatch.setattr(admin, "_service_active", lambda _name: False)
    assert admin._status(SimpleNamespace(json=True)) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["unfinished_transaction"] is False
    assert status["transaction"]["state"] == "recovered"

    admin.JOURNAL_FILE.write_text('{"state":"switching"}', encoding="utf-8")
    monkeypatch.setattr(admin, "_databases", lambda: [])
    assert admin._doctor(SimpleNamespace()) == 1
    assert "requires automatic recovery" in capsys.readouterr().out


def test_legacy_dashboard_credential_rotation_is_redacted_and_invalidates_sessions(
    admin_paths, monkeypatch, capsys
):
    from reticulumpi.builtin_plugins.web_dashboard.auth import verify_password

    admin_paths.config.mkdir(parents=True)
    exposed = "historically-exposed-password"
    admin.CONFIG_FILE.write_text(
        "reticulumpi:\n  plugins:\n    web_dashboard:\n"
        "      enabled: true\n"
        f"      password: {exposed}\n",
        encoding="utf-8",
    )
    secret_dir = admin_paths.data / ".config/reticulumpi"
    secret_dir.mkdir(parents=True)
    for suffix in ("", "-wal", "-shm"):
        (secret_dir / f"sessions.db{suffix}").write_text("legacy session", encoding="utf-8")
    replacement = "new-bootstrap-value-that-is-never-logged"
    monkeypatch.setattr(admin.secrets, "token_urlsafe", lambda _length: replacement)
    monkeypatch.setattr(admin.os, "chown", lambda *_args: None)

    plan = admin._plan_dashboard_credential_migration(source_replaces_unit=True)
    assert plan is not None and plan.reason == "plaintext_config"
    assert exposed not in json.dumps(admin._describe_dashboard_credential_migration(plan))
    admin._apply_dashboard_credential_migration(plan)
    captured = capsys.readouterr()
    assert exposed not in captured.out + captured.err
    assert replacement not in captured.out + captured.err
    assert "password:" not in admin.CONFIG_FILE.read_text(encoding="utf-8")
    bootstrap = secret_dir / "dashboard_password.txt"
    secret = secret_dir / "dashboard_secret"
    assert bootstrap.read_text(encoding="utf-8").strip() == replacement
    assert stat.S_IMODE(bootstrap.stat().st_mode) == 0o600
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert verify_password(replacement, secret.read_text(encoding="utf-8").strip())
    assert not list(secret_dir.glob("sessions.db*"))


def test_modern_operator_dashboard_hash_is_preserved(admin_paths):
    admin_paths.config.mkdir(parents=True)
    operator_hash = f"scrypt:{'a' * 32}:16384:8:2:{'b' * 64}"
    admin.CONFIG_FILE.write_text(
        f'reticulumpi:\n  plugins:\n    web_dashboard:\n      password_hash: "{operator_hash}"\n',
        encoding="utf-8",
    )
    assert admin._plan_dashboard_credential_migration(source_replaces_unit=True) is None
    assert operator_hash in admin.CONFIG_FILE.read_text(encoding="utf-8")


def test_bootstrap_and_legacy_hash_are_rotated_but_plaintext_unit_needs_source(
    admin_paths, monkeypatch
):
    admin_paths.config.mkdir(parents=True)
    admin.CONFIG_FILE.write_text(
        "reticulumpi:\n  plugins:\n    web_dashboard:\n      enabled: true\n",
        encoding="utf-8",
    )
    secret_dir = admin_paths.data / ".config/reticulumpi"
    secret_dir.mkdir(parents=True)
    (secret_dir / "dashboard_secret").write_text(
        f"scrypt:{'a' * 32}:{'b' * 64}\n", encoding="utf-8"
    )
    plan = admin._plan_dashboard_credential_migration(source_replaces_unit=True)
    assert plan is not None and plan.reason == "legacy_password_hash"

    admin_paths.systemd.mkdir(parents=True)
    (admin_paths.systemd / "reticulumpi.service").write_text(
        '[Service]\nEnvironment="RETICULUMPI_DASHBOARD_PASSWORD=exposed"\n',
        encoding="utf-8",
    )
    with pytest.raises(admin.AdminError, match="complete signed source bundle"):
        admin._plan_dashboard_credential_migration(source_replaces_unit=False)

    monkeypatch.setattr(
        admin,
        "_installed_dashboard_environment",
        lambda: (True, True),
    )
    # An explicit hash override wins and is never silently rotated, even when
    # stale plaintext evidence is also present.
    assert admin._plan_dashboard_credential_migration(source_replaces_unit=False) is None


def test_dashboard_readiness_marker_is_fresh_owned_and_private(admin_paths):
    admin_paths.run.mkdir(parents=True)
    marker = admin._dashboard_readiness_path()
    marker.write_text("ready\n", encoding="ascii")
    marker.chmod(0o600)
    assert admin._dashboard_readiness_marker_valid() is True
    marker.chmod(0o664)
    with pytest.raises(admin.AdminError, match="writable by group/other"):
        admin._dashboard_readiness_marker_valid()
    marker.chmod(0o600)
    admin._clear_dashboard_readiness()
    assert not marker.exists()


def test_active_rnsd_is_stopped_before_backup_then_restarted_and_verified(
    admin_paths, tmp_path, monkeypatch
):
    current = _release(admin_paths.root, "0.2.5")
    (admin_paths.root / "current").symlink_to(current)
    _write_install_manifest(admin_paths, current, None, features=("shared-rnsd",))
    admin.CONFIG_FILE.write_text("reticulumpi: {}\n", encoding="utf-8")
    admin_paths.data.mkdir(parents=True)
    source = _complete_source_bundle(tmp_path / "source")
    wheel = _wheel(tmp_path / "reticulumpi-0.3.0-py3-none-any.whl")
    commands = _mock_apply_runtime(
        monkeypatch,
        wheel,
        active={"reticulumpi.service", "rnsd.service", "rnsd-watchdog.timer"},
        enabled={"reticulumpi.service", "rnsd.service", "rnsd-watchdog.timer"},
    )
    real_backup = admin._backup_state

    def record_backup(*args, **kwargs):
        commands.append(["BACKUP"])
        return real_backup(*args, **kwargs)

    monkeypatch.setattr(admin, "_backup_state", record_backup)
    args = SimpleNamespace(
        install_root=str(admin_paths.root),
        bundle=str(source),
        feature=["shared-rnsd", "watchdog"],
        apply=True,
        dry_run=False,
        start=True,
    )
    assert admin._apply_release(args, "upgrade") == 0
    stop_rnsd = [admin.SYSTEMCTL, "stop", "rnsd.service"]
    restart_rnsd = [admin.SYSTEMCTL, "restart", "rnsd.service"]
    assert commands.index(stop_rnsd) < commands.index(["BACKUP"])
    assert commands.index(["BACKUP"]) < commands.index(restart_rnsd)


def test_arm64_install_archive_contract_is_signed_nested_and_safe(
    admin_paths, tmp_path, monkeypatch, capsys
):
    version = "0.3.0"
    source = _complete_source_bundle(tmp_path / f"reticulumpi-{version}", version)
    wheel_name = f"reticulumpi-{version}-py3-none-any.whl"
    _dashboard_wheel(source / wheel_name, version)
    (source / "bundle.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "kind": "reticulumpi-install",
                "version": version,
                "architecture": "arm64",
                "wheel": wheel_name,
            }
        ),
        encoding="utf-8",
    )
    _sign_source(source)
    archive = tmp_path / f"reticulumpi-install-arm64-{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(source, arcname=source.name, recursive=True)
    (tmp_path / admin.BUNDLE_MANIFEST_NAME).write_text(
        f"{admin._sha256(archive)}  {archive.name}\n", encoding="utf-8"
    )
    (tmp_path / admin.BUNDLE_SIGNATURE_NAME).write_text("outer signature\n", encoding="utf-8")
    monkeypatch.setattr(admin, "_verify_minisign", lambda *_args: None)

    with admin._materialize_install_bundle(archive) as extracted:
        assert extracted.name == f"reticulumpi-{version}"
        assert (extracted / "bundle.json").is_file()
        assert (extracted / "constraints/production-universal-all-features.txt").is_file()
        destination = tmp_path / "copied-wheel"
        destination.mkdir()
        monkeypatch.setattr(
            admin,
            "_run",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("install archive must not rebuild its wheel")
            ),
        )
        copied = admin._build_wheel(extracted, extracted, destination)
        assert copied.name == wheel_name
        assert admin._sha256(copied) == admin._sha256(extracted / wheel_name)
    assert not extracted.exists()

    args = SimpleNamespace(
        install_root=str(admin_paths.root),
        bundle=str(archive),
        feature=["dashboard"],
        apply=False,
        dry_run=True,
        start=False,
    )
    assert admin._apply_release(args, "install") == 0
    dry_run = capsys.readouterr().out
    assert str(archive) in dry_run
    assert '"dependency_profile": "dashboard-nomadnet"' in dry_run

    unsafe = tmp_path / "reticulumpi-install-arm64-0.3.1.tar.gz"
    with tarfile.open(unsafe, "w:gz") as handle:
        member = tarfile.TarInfo("reticulumpi-0.3.1/escape")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside"
        handle.addfile(member)
    (tmp_path / admin.BUNDLE_MANIFEST_NAME).write_text(
        f"{admin._sha256(unsafe)}  {unsafe.name}\n", encoding="utf-8"
    )
    with pytest.raises(admin.AdminError, match="forbidden special member"):
        with admin._materialize_install_bundle(unsafe):
            pass


def test_new_administrator_materializes_legacy_profile_install_archive(tmp_path, monkeypatch):
    version = "0.3.0"
    source = _complete_source_bundle(tmp_path / f"reticulumpi-{version}", version)
    _rename_fixture_dependency_profiles(
        source, admin._DEPENDENCY_PROFILES, admin._LEGACY_DEPENDENCY_PROFILES
    )
    wheel_name = f"reticulumpi-{version}-py3-none-any.whl"
    _dashboard_wheel(source / wheel_name, version)
    (source / "bundle.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "kind": "reticulumpi-install",
                "version": version,
                "architecture": "arm64",
                "wheel": wheel_name,
            }
        ),
        encoding="utf-8",
    )
    _sign_source(source)
    archive = tmp_path / f"reticulumpi-install-arm64-{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(source, arcname=source.name, recursive=True)
    (tmp_path / admin.BUNDLE_MANIFEST_NAME).write_text(
        f"{admin._sha256(archive)}  {archive.name}\n", encoding="utf-8"
    )
    (tmp_path / admin.BUNDLE_SIGNATURE_NAME).write_text("outer signature\n", encoding="utf-8")
    monkeypatch.setattr(admin, "_verify_minisign", lambda *_args: None)

    with admin._materialize_install_bundle(archive) as extracted:
        selected = admin._dependency_profile_path(extracted, extracted, ("sensors",))
        assert selected.name == admin._LEGACY_DEPENDENCY_PROFILES["all-features"]
        assert not (extracted / "constraints" / admin._DEPENDENCY_PROFILES["all-features"]).exists()


def test_install_archive_rejects_empty_corrupt_unsafe_and_unsupported_members(tmp_path):
    destination = tmp_path / "extract"
    destination.mkdir()
    wrong_name = tmp_path / "bundle.tar.gz"
    wrong_name.write_bytes(b"not a tar")
    with pytest.raises(admin.AdminError, match="not a ReticulumPi ARM64"):
        admin._extract_install_archive(wrong_name, destination)

    corrupt = tmp_path / "reticulumpi-install-arm64-0.3.0.tar.gz"
    corrupt.write_bytes(b"not a gzip tar")
    with pytest.raises(admin.AdminError, match="invalid install bundle"):
        admin._extract_install_archive(corrupt, destination)

    empty = tmp_path / "reticulumpi-install-arm64-0.3.1.tar.gz"
    with tarfile.open(empty, "w:gz"):
        pass
    with pytest.raises(admin.AdminError, match="archive is empty"):
        admin._extract_install_archive(empty, destination)

    for version, member_name, member_type, message in (
        ("0.3.2", "../escape", tarfile.REGTYPE, "unsafe member"),
        ("0.3.3", "other-root/file", tarfile.REGTYPE, "unsafe member"),
        ("0.3.4", "reticulumpi-0.3.4/unknown", b"Z", "unsupported member"),
    ):
        archive = tmp_path / f"reticulumpi-install-arm64-{version}.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            member = tarfile.TarInfo(member_name)
            member.type = member_type
            handle.addfile(member)
        with pytest.raises(admin.AdminError, match=message):
            admin._extract_install_archive(archive, destination)


@pytest.mark.parametrize("wheel_value", [None, "../escape.whl", "not-a-wheel.txt"])
def test_install_archive_rejects_bad_metadata_and_declared_wheel(
    tmp_path, monkeypatch, wheel_value
):
    version = "0.3.0"
    source = _complete_source_bundle(tmp_path / f"source-{wheel_value!s}", version)
    metadata = {
        "schema": 1,
        "kind": "reticulumpi-install",
        "version": version,
        "architecture": "arm64",
        "wheel": wheel_value,
    }
    (source / "bundle.json").write_text(json.dumps(metadata), encoding="utf-8")
    _sign_source(source)
    archive = tmp_path / f"reticulumpi-install-arm64-{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(source, arcname=f"reticulumpi-{version}")
    monkeypatch.setattr(admin, "_verify_signed_bundle", lambda *_args: None)
    with pytest.raises(admin.AdminError, match="invalid wheel basename"):
        admin._extract_install_archive(archive, tmp_path / f"out-{wheel_value!s}")


def test_install_archive_rejects_metadata_mismatch_missing_wheel_and_source_version(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(admin, "_verify_signed_bundle", lambda *_args: None)

    def build(version, *, metadata_version=None, project_version=None, include_wheel=False):
        source = _complete_source_bundle(tmp_path / f"source-{version}", project_version or version)
        wheel_name = f"reticulumpi-{version}-py3-none-any.whl"
        if include_wheel:
            _wheel(source / wheel_name, version)
        (source / "bundle.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "kind": "reticulumpi-install",
                    "version": metadata_version or version,
                    "architecture": "arm64",
                    "wheel": wheel_name,
                }
            ),
            encoding="utf-8",
        )
        _sign_source(source)
        archive = tmp_path / f"reticulumpi-install-arm64-{version}.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(source, arcname=f"reticulumpi-{version}")
        return archive

    with pytest.raises(admin.AdminError, match="metadata does not match"):
        admin._extract_install_archive(
            build("0.3.5", metadata_version="0.3.4"), tmp_path / "out-metadata"
        )
    with pytest.raises(admin.AdminError, match="missing its declared"):
        admin._extract_install_archive(build("0.3.6"), tmp_path / "out-wheel")
    with pytest.raises(admin.AdminError, match="source metadata does not match"):
        admin._extract_install_archive(
            build("0.3.7", project_version="0.3.8", include_wheel=True),
            tmp_path / "out-version",
        )


def test_os_release_and_dependency_profile_failure_branches(tmp_path, monkeypatch):
    missing = tmp_path / "missing-os-release"
    with pytest.raises(admin.AdminError, match="unavailable"):
        admin._read_os_release(missing)
    target = tmp_path / "target-os-release"
    target.write_text("ID=debian\n", encoding="utf-8")
    link = tmp_path / "os-release"
    link.symlink_to(target)
    with pytest.raises(admin.AdminError, match="symlink is unsafe"):
        admin._read_os_release(link)
    target.write_text("# comment\ninvalid key=value\nEMPTY=\nBROKEN='unterminated\n")
    with pytest.raises(admin.AdminError, match="invalid operating-system metadata"):
        admin._read_os_release(target)
    assert admin._normalise_architecture("mips64") == "mips64"

    source = _source_bundle(tmp_path / "profiles")
    profile = source / "constraints/production-universal-core.txt"
    profile.unlink()
    with pytest.raises(admin.AdminError, match="missing the core"):
        admin._dependency_profile_path(source, source, ())
    profile.write_text("package==1\n", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="not hash locked"):
        admin._dependency_profile_path(source, source, ())
    profile.write_text(
        f"package==1 --hash=sha256:{'a' * 64}\n--index-url https://example.invalid\n"
    )
    with pytest.raises(admin.AdminError, match="forbidden external"):
        admin._dependency_profile_path(source, source, ())
    profile.write_text(f"package==1 --hash=sha256:{'a' * 64}\n")
    (source / admin.BUNDLE_MANIFEST_NAME).write_text(
        f"{'b' * 64}  constraints/production-universal-dashboard-nomadnet.txt\n"
    )
    with pytest.raises(admin.AdminError, match="does not contain dependency"):
        admin._dependency_profile_path(source, source, ())

    staging = tmp_path / "staging"
    staging.mkdir()
    hashes = iter(("a", "b", "c"))
    monkeypatch.setattr(admin, "_sha256", lambda _path: next(hashes))
    with pytest.raises(admin.AdminError, match="staged dependency"):
        admin._stage_dependency_profile(profile, staging)


def test_installed_layout_parser_handles_dropins_continuations_and_invalid_units(
    admin_paths, monkeypatch
):
    dropin = admin_paths.systemd / "reticulumpi.service.d"
    dropin.mkdir(parents=True)
    main = admin_paths.systemd / "reticulumpi.service"
    main.write_text(
        "# ignored\n; ignored too\n"
        'Environment="RETICULUMPI_STATE_DIR=/srv/state" \\\n'
        ' "XDG_DATA_HOME=/srv/xdg/.local/share" '
        '"RETICULUMPI_RNS_CONFIG_DIR=/srv/rns/.reticulum" NO_EQUALS\n'
        "ExecStart=/srv/app/venv/bin/reticulumpi --config=/srv/config.yaml\n",
        encoding="utf-8",
    )
    (dropin / "20-extra.conf").write_text("WorkingDirectory=/srv/working\n", encoding="utf-8")
    layout = admin._discover_legacy_layout()
    assert Path("/srv/state") in layout.homes
    assert Path("/srv/xdg") in layout.homes
    assert Path("/srv/rns") in layout.homes
    assert Path("/srv/app") in layout.install_roots
    assert Path("/srv/config.yaml") in layout.config_files
    assert admin._absolute_unit_value("~/unsafe") is None

    main.write_text('Environment="unterminated\n', encoding="utf-8")
    with pytest.raises(admin.AdminError, match="invalid Environment"):
        admin._discover_legacy_layout()
    main.write_text('ExecStart="unterminated\n', encoding="utf-8")
    with pytest.raises(admin.AdminError, match="invalid ExecStart"):
        admin._discover_legacy_layout()

    main.unlink()
    main.symlink_to(dropin / "20-extra.conf")
    with pytest.raises(admin.AdminError, match="service definition is unsafe"):
        admin._installed_service_fragments()
    main.unlink()
    dropin_target = admin_paths.systemd / "dropin-target"
    dropin_target.mkdir()
    for child in dropin.iterdir():
        child.unlink()
    dropin.rmdir()
    dropin.symlink_to(dropin_target, target_is_directory=True)
    with pytest.raises(admin.AdminError, match="drop-in directory is unsafe"):
        admin._installed_service_fragments()

    monkeypatch.setattr(
        admin.pwd,
        "getpwnam",
        lambda _name: (_ for _ in ()).throw(KeyError("absent")),
    )
    dropin.unlink()
    assert admin._discover_legacy_layout().homes == (Path("/home/reticulumpi"),)


def test_managed_file_snapshot_manifest_rejects_every_untrusted_shape(admin_paths, tmp_path):
    backup = tmp_path / "backup"
    files = backup / "managed-files"
    files.mkdir(parents=True)
    allowed = str(admin._managed_paths()[0])

    def write(value):
        admin._atomic_json(backup / "managed-files.json", value, 0o600)

    invalid_values = [
        {"schema": 2, "files": []},
        {"schema": 1, "files": ["not-an-object"]},
        {"schema": 1, "files": [{"path": "/outside", "present": False, "mode": None}]},
        {"schema": 1, "files": [{"path": allowed, "present": "no", "mode": None}]},
        {"schema": 1, "files": [{"path": allowed, "present": True, "mode": 0o10000}]},
    ]
    for value in invalid_values:
        write(value)
        with pytest.raises(admin.AdminError):
            admin._load_file_snapshots(backup)

    record = {"path": allowed, "present": True, "mode": 0o644}
    for blob_name in (None, "../escape.bin"):
        write({"schema": 1, "files": [{**record, "blob": blob_name}]})
        with pytest.raises(admin.AdminError, match="invalid managed-file recovery blob"):
            admin._load_file_snapshots(backup)
    write({"schema": 1, "files": [{**record, "blob": "000.bin"}]})
    with pytest.raises(admin.AdminError, match="missing or unsafe"):
        admin._load_file_snapshots(backup)
    blob = files / "000.bin"
    blob.write_bytes(b"payload")
    write(
        {
            "schema": 1,
            "files": [
                {
                    **record,
                    "blob": "000.bin",
                    "size": 999,
                    "sha256": admin._sha256(blob),
                }
            ],
        }
    )
    with pytest.raises(admin.AdminError, match="verification failed"):
        admin._load_file_snapshots(backup)
    write(
        {
            "schema": 1,
            "files": [
                {"path": allowed, "present": False, "mode": None},
                {"path": allowed, "present": False, "mode": None},
            ],
        }
    )
    with pytest.raises(admin.AdminError, match="invalid managed-file recovery record"):
        admin._load_file_snapshots(backup)


def test_recovery_journal_rejects_malformed_paths_states_and_service_evidence(
    admin_paths, monkeypatch
):
    previous = _release(admin_paths.root, "0.2.5")
    candidate = _release(admin_paths.root, "0.3.0")
    (admin_paths.root / "current").symlink_to(previous)
    admin_paths.data.mkdir(parents=True)
    base = {
        "schema": 1,
        "state": "preparing",
        "install_root": str(admin_paths.root),
        "previous_release": str(previous),
        "new_release": str(candidate),
        "backup": None,
        "services_before": _inactive_service_evidence(),
    }

    def rejected(overrides, match):
        value = {**base, **overrides}
        admin._atomic_json(admin.JOURNAL_FILE, value, 0o600)
        with pytest.raises(admin.AdminError, match=match):
            admin._recover_interrupted_transaction(admin_paths.root.resolve())

    rejected({"schema": 2}, "unsupported schema")
    rejected({"state": "invented"}, "unknown state")
    rejected({"previous_release": 7}, "previous-release evidence")
    rejected({"previous_release": str(admin_paths.root / "releases/nested/0.2.5")}, "previous")
    rejected({"new_release": None}, "candidate-release evidence")
    rejected({"new_release": "relative"}, "not absolute")
    rejected({"new_release": str(admin_paths.root / "releases/nested/0.3.0")}, "escapes")
    rejected({"state": "backed_up"}, "missing durable backup")
    rejected({"services_before": []}, "service-state evidence")
    rejected({"backup": 7}, "invalid backup evidence")
    rejected({"backup": "relative"}, "backup path is not absolute")

    other_root = admin_paths.root.parent / "other/reticulumpi"
    admin._atomic_json(admin.JOURNAL_FILE, base, 0o600)
    with pytest.raises(admin.AdminError, match="not requested install root"):
        admin._recover_interrupted_transaction(other_root.resolve())

    candidate_link = admin_paths.root / "releases/linked"
    candidate_link.symlink_to(candidate, target_is_directory=True)
    rejected({"new_release": str(candidate_link)}, "unsafe symlink")

    malformed_states = _inactive_service_evidence()
    malformed_states["rnsd.service"] = {"active": "yes", "enabled": False}
    rejected({"services_before": malformed_states}, "service-state evidence")

    # A consistent pre-backup journal can recover without a manifest; this is
    # the fresh-install interruption path.
    monkeypatch.setattr(admin, "_run", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(admin, "_unit_enabled", lambda _name: False)
    admin._atomic_json(admin.JOURNAL_FILE, base, 0o600)
    recovered = admin._recover_interrupted_transaction(admin_paths.root.resolve())
    assert recovered is not None and recovered["state"] == "recovered"


def test_dashboard_credential_parser_and_rotation_fail_closed_branches(
    admin_paths, tmp_path, monkeypatch
):
    admin_paths.config.mkdir(parents=True)
    assert admin._dashboard_config_fields(tmp_path / "missing") == ({}, "")
    target = tmp_path / "config-target"
    target.write_text("web_dashboard: {}\n", encoding="utf-8")
    link = tmp_path / "config-link"
    link.symlink_to(target)
    with pytest.raises(admin.AdminError, match="symlink or special"):
        admin._dashboard_config_fields(link)

    admin.CONFIG_FILE.write_text(
        "web_dashboard:\n  password: one\nweb_dashboard:\n", encoding="utf-8"
    )
    with pytest.raises(admin.AdminError, match="multiple web_dashboard"):
        admin._dashboard_config_fields(admin.CONFIG_FILE)
    admin.CONFIG_FILE.write_text(
        "web_dashboard:\n  # ignored\n\n  password: one\n  password: two\nnext: value\n",
        encoding="utf-8",
    )
    with pytest.raises(admin.AdminError, match="configured more than once"):
        admin._dashboard_config_fields(admin.CONFIG_FILE)
    admin.CONFIG_FILE.write_text("other: value\n", encoding="utf-8")
    fields, _text = admin._dashboard_config_fields(admin.CONFIG_FILE)
    assert fields == {}

    with pytest.raises(admin.AdminError, match="simple path"):
        admin._dashboard_secret_dir({"secret_dir": (0, "'unterminated")})
    with pytest.raises(admin.AdminError, match="must be absolute"):
        admin._dashboard_secret_dir({"secret_dir": (0, "relative/path")})
    assert admin._dashboard_secret_dir({"secret_dir": (0, '"/srv/secrets" # note')}) == Path(
        "/srv/secrets"
    )

    admin.CONFIG_FILE.write_text(
        "web_dashboard:\n  secret_dir: " + str(admin_paths.data / "secrets") + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(admin, "_installed_dashboard_environment", lambda: (False, True))
    plan = admin._plan_dashboard_credential_migration(source_replaces_unit=True)
    assert plan is not None and plan.reason == "plaintext_unit_environment"

    monkeypatch.setattr(admin, "_installed_dashboard_environment", lambda: (False, False))
    secret_dir = admin_paths.data / "secrets"
    secret_dir.mkdir(parents=True)
    bootstrap = secret_dir / "dashboard_password.txt"
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    bootstrap.symlink_to(outside)
    with pytest.raises(admin.AdminError, match="bootstrap file is unsafe"):
        admin._plan_dashboard_credential_migration(source_replaces_unit=True)
    bootstrap.unlink()
    secret = secret_dir / "dashboard_secret"
    secret.symlink_to(outside)
    with pytest.raises(admin.AdminError, match="dashboard secret is unsafe"):
        admin._plan_dashboard_credential_migration(source_replaces_unit=True)
    secret.unlink()
    secret.write_text(f"scrypt:{'a' * 32}:16384:8:2:{'b' * 64}\n", encoding="utf-8")
    assert admin._plan_dashboard_credential_migration(source_replaces_unit=True) is None

    admin.CONFIG_FILE.write_text("web_dashboard:\n  password: exposed\n", encoding="utf-8")
    rotation = admin._plan_dashboard_credential_migration(source_replaces_unit=True)
    assert rotation is not None
    admin.CONFIG_FILE.write_text("web_dashboard:\n  password: changed\n", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="configuration changed"):
        admin._apply_dashboard_credential_migration(rotation)

    moved = admin.DashboardCredentialMigration(
        reason="plaintext_config",
        secret_dir=secret_dir,
        config_sha256=admin._sha256(admin.CONFIG_FILE),
        plaintext_line=99,
    )
    with pytest.raises(admin.AdminError, match="credential moved"):
        admin._apply_dashboard_credential_migration(moved)

    session_link = secret_dir / "sessions.db"
    session_link.symlink_to(outside)
    no_config = admin.DashboardCredentialMigration(
        reason="bootstrap_credential",
        secret_dir=secret_dir,
        config_sha256=None,
        plaintext_line=None,
    )
    monkeypatch.setattr(admin.os, "chown", lambda *_args: None)
    with pytest.raises(admin.AdminError, match="session database is unsafe"):
        admin._apply_dashboard_credential_migration(no_config)


def test_admin_preflight_helpers_cover_fail_closed_edges(tmp_path, monkeypatch):
    with pytest.raises(admin.AdminError, match="unsafe install root"):
        admin._safe_install_root("/x")

    missing_archive = tmp_path / "reticulumpi-install-arm64-0.3.0.tar.gz"
    with pytest.raises(admin.AdminError, match="archive is missing or unsafe"):
        with admin._materialize_install_bundle(missing_archive):
            pytest.fail("a missing release archive must never be yielded")

    profile = tmp_path / "constraints" / admin._DEPENDENCY_PROFILES["core"]
    profile.parent.mkdir()
    profile.write_text(
        f"fixture==1.0 --hash=sha256:{'a' * 64}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(admin, "_unsigned_development_mode", lambda: True)
    assert admin._dependency_profile_path(tmp_path, tmp_path, ()) == profile


def test_wait_service_inactive_covers_success_after_poll_and_timeout(monkeypatch):
    activity = iter((True, False))
    ticks = iter((0.0, 0.1, 0.2))
    monkeypatch.setattr(admin, "_service_active", lambda _name: next(activity))
    monkeypatch.setattr(admin.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(admin.time, "sleep", lambda _seconds: None)
    admin._wait_service_inactive("reticulumpi.service", timeout=1.0)

    ticks = iter((0.0, 1.0))
    monkeypatch.setattr(admin.time, "monotonic", lambda: next(ticks))
    with pytest.raises(admin.AdminError, match="did not stop"):
        admin._wait_service_inactive("reticulumpi.service", timeout=0.5)


def test_dashboard_readiness_clear_rejects_symlink(admin_paths, tmp_path):
    admin_paths.run.mkdir(parents=True)
    marker = admin_paths.run / admin._DASHBOARD_READY_FILE
    marker.symlink_to(tmp_path / "attacker-controlled")
    with pytest.raises(admin.AdminError, match="may not be a symlink"):
        admin._clear_dashboard_readiness()


def test_layout_state_mapping_skips_canonical_source(admin_paths, monkeypatch):
    monkeypatch.setattr(admin, "_legacy_home_candidates", lambda: (admin.DATA_DIR,))
    mappings = admin._legacy_state_destinations(())
    assert (admin.DATA_DIR / ".reticulum", admin.DATA_DIR / ".reticulum") not in mappings


def test_restore_service_states_verifies_enablement(admin_paths, monkeypatch):
    states = _inactive_service_evidence()
    monkeypatch.setattr(admin, "_run", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        admin,
        "_unit_enabled",
        lambda name: name == "reticulumpi.service",
    )
    with pytest.raises(admin.AdminError, match="could not restore prior unit enablement"):
        admin._restore_service_states(states, (), {})


def test_preparing_journal_exists_before_candidate_build_failpoint(
    admin_paths, tmp_path, monkeypatch
):
    current = _release(admin_paths.root, "0.2.5")
    (admin_paths.root / "current").symlink_to(current)
    _write_install_manifest(admin_paths, current, None)
    admin.CONFIG_FILE.write_text("reticulumpi: {}\n", encoding="utf-8")
    source = _complete_source_bundle(tmp_path / "source")
    wheel = _wheel(tmp_path / "reticulumpi-0.3.0-py3-none-any.whl")
    _mock_apply_runtime(monkeypatch, wheel)
    mocked_run = admin._run
    observed = []

    def fail_candidate_build(command, **kwargs):
        if len(command) >= 4 and command[:3] == [admin.sys.executable, "-m", "venv"]:
            journal = json.loads(admin.JOURNAL_FILE.read_text(encoding="utf-8"))
            release = admin_paths.root / "releases/0.3.0"
            observed.append((journal["state"], journal["new_release"], release.is_dir()))
            raise admin.AdminError("candidate-build failpoint")
        return mocked_run(command, **kwargs)

    monkeypatch.setattr(admin, "_run", fail_candidate_build)
    args = SimpleNamespace(
        install_root=str(admin_paths.root),
        bundle=str(source),
        feature=[],
        apply=True,
        dry_run=False,
        start=False,
    )
    with pytest.raises(admin.AdminError, match="candidate-build failpoint"):
        admin._apply_release(args, "upgrade")

    release = admin_paths.root / "releases/0.3.0"
    assert observed == [("preparing", str(release), True)]
    assert not release.exists()
    assert json.loads(admin.JOURNAL_FILE.read_text(encoding="utf-8"))["state"] == "rolled_back"


def test_power_loss_in_preparing_removes_partial_release_without_touching_live_state(
    admin_paths, monkeypatch
):
    previous = _release(admin_paths.root, "0.2.5")
    partial = admin_paths.root / "releases/0.3.0"
    (partial / ".venv/lib").mkdir(parents=True)
    (partial / ".venv/lib/partial-install").write_text("incomplete\n", encoding="utf-8")
    (admin_paths.root / "current").symlink_to(previous)
    _write_install_manifest(admin_paths, previous, None)
    admin.CONFIG_FILE.write_text("live-config\n", encoding="utf-8")
    admin_paths.data.mkdir(parents=True, exist_ok=True)
    durable = admin_paths.data / "messages.db.sentinel"
    durable.write_text("live-state\n", encoding="utf-8")
    admin._atomic_json(
        admin.JOURNAL_FILE,
        {
            "schema": 1,
            "operation": "upgrade",
            "install_root": str(admin_paths.root),
            "previous_release": str(previous),
            "new_release": str(partial),
            "backup": None,
            "remove_candidate": True,
            "services_before": _inactive_service_evidence(),
            "state": "preparing",
        },
        0o600,
    )
    commands = []
    monkeypatch.setattr(admin, "_run", lambda command, **_kwargs: commands.append(command) or "")
    monkeypatch.setattr(admin, "_unit_enabled", lambda _name: False)

    recovered = admin._recover_interrupted_transaction(admin_paths.root.resolve())

    assert recovered is not None and recovered["state"] == "recovered"
    assert not partial.exists()
    assert (admin_paths.root / "current").resolve() == previous
    assert admin.CONFIG_FILE.read_text(encoding="utf-8") == "live-config\n"
    assert durable.read_text(encoding="utf-8") == "live-state\n"
    assert recovered["recovery_evidence"]["backup"] is None
    assert [admin.SYSTEMCTL, "stop", "reticulumpi.service"] in commands


def test_install_root_rejects_user_controlled_existing_ancestor(tmp_path):
    unsafe = tmp_path / "service-owned" / "reticulumpi"
    unsafe.parent.mkdir()
    unsafe.parent.chmod(0o777)
    with pytest.raises(admin.AdminError, match="root-owned and immutable"):
        admin._safe_install_root(str(unsafe))


def test_private_source_snapshot_ignores_external_manifest_replacement(tmp_path, monkeypatch):
    source = _complete_source_bundle(tmp_path / "source")
    _sign_source(source)
    original_manifest = (source / admin.BUNDLE_MANIFEST_NAME).read_bytes()
    monkeypatch.setattr(admin, "_verify_minisign", lambda *_args: None)

    def replace_manifest(label, external, _snapshot):
        if label == "after-manifest-verification":
            (external / admin.BUNDLE_MANIFEST_NAME).write_text(
                f"{'0' * 64}  pyproject.toml\n",
                encoding="utf-8",
            )

    monkeypatch.setattr(admin, "_bundle_snapshot_failpoint", replace_manifest)
    with admin._materialize_install_bundle(source) as snapshot:
        assert snapshot != source
        assert (snapshot / admin.BUNDLE_MANIFEST_NAME).read_bytes() == original_manifest
        admin._verify_signed_bundle(snapshot, snapshot)
    assert (source / admin.BUNDLE_MANIFEST_NAME).read_bytes() != original_manifest


def test_private_source_snapshot_accepts_exact_legacy_dependency_scheme(tmp_path, monkeypatch):
    source = _complete_source_bundle(tmp_path / "source")
    _rename_fixture_dependency_profiles(
        source, admin._DEPENDENCY_PROFILES, admin._LEGACY_DEPENDENCY_PROFILES
    )
    _sign_source(source)
    monkeypatch.setattr(admin, "_verify_minisign", lambda *_args: None)

    with admin._materialize_install_bundle(source) as snapshot:
        selected = admin._dependency_profile_path(snapshot, snapshot, ("dashboard",))
        assert selected.name == admin._LEGACY_DEPENDENCY_PROFILES["dashboard-nomadnet"]


def test_private_wheel_snapshot_is_only_artifact_used_after_external_swap(tmp_path, monkeypatch):
    wheel = _wheel(tmp_path / "reticulumpi-0.3.0-py3-none-any.whl")
    original = wheel.read_bytes()
    monkeypatch.setattr(admin, "_verify_minisign", lambda *_args: None)

    def replace_wheel(label, external, _snapshot):
        if label == "after-payload-verification":
            external.write_bytes(b"attacker replacement")

    monkeypatch.setattr(admin, "_bundle_snapshot_failpoint", replace_wheel)
    destination = tmp_path / "built"
    destination.mkdir()
    with admin._materialize_install_bundle(wheel) as snapshot:
        assert snapshot.read_bytes() == original
        copied = admin._build_wheel(snapshot, None, destination)
        assert copied.read_bytes() == original
    assert wheel.read_bytes() == b"attacker replacement"


def test_wheel_swap_before_snapshot_copy_fails_signed_digest(tmp_path, monkeypatch):
    wheel = _wheel(tmp_path / "reticulumpi-0.3.0-py3-none-any.whl")
    monkeypatch.setattr(admin, "_verify_minisign", lambda *_args: None)

    def replace_wheel(label, external, _snapshot):
        if label == "after-manifest-verification":
            (external / wheel.name).write_bytes(b"attacker replacement")

    monkeypatch.setattr(admin, "_bundle_snapshot_failpoint", replace_wheel)
    with pytest.raises(admin.AdminError, match="snapshot checksum mismatch"):
        with admin._materialize_install_bundle(wheel):
            pytest.fail("a wheel replaced after manifest verification must not be yielded")


@pytest.mark.parametrize(
    "content",
    [
        '[Service]\nEnvironment="RETICULUMPI_DASHBOARD_PASSWORD=exposed"\n',
        '[Service]\nEnvironment="RETICULUMPI_DASHBOARD_PASSWORD_HASH=scrypt:old"\n',
        "[Service]\nEnvironmentFile=/var/lib/reticulumpi/dashboard.env\n",
    ],
)
def test_dashboard_credential_dropins_are_snapshotted_removed_and_restorable(
    admin_paths, tmp_path, content
):
    dropin = admin_paths.systemd / "reticulumpi.service.d"
    dropin.mkdir(parents=True)
    credential = dropin / "90-legacy-dashboard.conf"
    credential.write_text(content, encoding="utf-8")
    clean = dropin / "20-safe.conf"
    clean.write_text("[Service]\nMemoryHigh=1G\n", encoding="utf-8")

    paths = admin._dashboard_credential_dropins()
    assert paths == (credential,)
    snapshots = admin._snapshot_files(paths)
    backup = tmp_path / "backup"
    backup.mkdir()
    admin._persist_file_snapshots(backup, snapshots)
    recovered_snapshots = admin._load_file_snapshots(backup)

    admin._remove_dashboard_credential_dropins(paths, snapshots)
    assert not credential.exists()
    assert clean.exists()
    admin._restore_files(recovered_snapshots)
    assert credential.read_text(encoding="utf-8") == content


def test_journal_survives_service_owned_state_unlink_and_swap(admin_paths):
    admin_paths.data.mkdir(parents=True)
    admin._atomic_json(admin.JOURNAL_FILE, {"schema": 1, "state": "preparing"}, 0o600)
    original = admin.JOURNAL_FILE.read_bytes()
    service_decoy = admin_paths.data / "admin-transaction.json"
    service_decoy.write_text("attacker controlled", encoding="utf-8")
    service_decoy.unlink()
    admin_paths.data.rename(admin_paths.data.with_name("reticulumpi.displaced"))
    admin_paths.data.mkdir()

    journal, unfinished = admin._journal_state()
    assert unfinished is True
    assert journal is not None and journal["state"] == "preparing"
    assert admin.JOURNAL_FILE.read_bytes() == original


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("bad-root", "root-owned and immutable"),
        ("inspection-error", "cannot validate install-root ancestor"),
        ("missing-then-present", "changed during validation"),
        ("symlink", "may not be a symlink"),
        ("ancestor-file", "ancestor is not a directory"),
        ("root-file", "install root is not a directory"),
    ],
)
def test_install_root_ancestry_fail_closed_branches(tmp_path, monkeypatch, case, message):
    candidate = tmp_path / "safe" / "root"
    special = {
        "inspection-error": tmp_path / "safe",
        "missing-then-present": tmp_path / "safe",
        "symlink": candidate,
        "ancestor-file": tmp_path / "safe",
        "root-file": candidate,
    }.get(case)

    def fake_lstat(path):
        if case == "bad-root" and path == Path(path.anchor):
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_uid=0)
        if path == special:
            if case == "inspection-error":
                raise PermissionError("blocked")
            if case == "missing-then-present":
                raise FileNotFoundError(str(path))
            if case == "symlink":
                return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0)
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0)
        return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    with pytest.raises(admin.AdminError, match=message):
        admin._validate_install_root_ancestry(candidate)


def test_journal_directory_rejects_preexisting_nonprivate_mode(admin_paths):
    admin.JOURNAL_FILE.parent.mkdir(parents=True)
    admin.JOURNAL_FILE.parent.chmod(0o755)
    with pytest.raises(admin.AdminError, match="ownership or permissions are unsafe"):
        admin._ensure_journal_directory()


def test_nofollow_hash_rejects_symlink_and_nonregular_file(tmp_path):
    regular = tmp_path / "regular"
    regular.write_bytes(b"content")
    link = tmp_path / "link"
    link.symlink_to(regular)
    with pytest.raises(admin.AdminError, match="cannot open file for hashing"):
        admin._sha256(link)
    with pytest.raises(admin.AdminError, match="is not regular"):
        admin._sha256(tmp_path)


def test_nofollow_hash_detects_in_place_change(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"stable bytes")
    real_fstat = admin.os.fstat
    calls = 0

    def changed_fstat(descriptor):
        nonlocal calls
        calls += 1
        result = real_fstat(descriptor)
        if calls == 2:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_dev=result.st_dev,
                st_ino=result.st_ino,
                st_size=result.st_size,
                st_mtime_ns=result.st_mtime_ns,
                st_ctime_ns=result.st_ctime_ns + 1,
            )
        return result

    monkeypatch.setattr(admin.os, "fstat", changed_fstat)
    with pytest.raises(admin.AdminError, match="changed while hashing"):
        admin._sha256(source)


def test_private_snapshot_rejects_existing_missing_and_nonregular_inputs(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"content")
    destination = tmp_path / "snapshot" / "payload"
    destination.parent.mkdir()
    destination.write_bytes(b"occupied")
    with pytest.raises(admin.AdminError, match="destination already exists"):
        admin._snapshot_regular_file(source, destination)

    destination.unlink()
    with pytest.raises(admin.AdminError, match="not a regular file"):
        admin._snapshot_regular_file(tmp_path, destination)
    with pytest.raises(admin.AdminError, match="cannot create private snapshot"):
        admin._snapshot_regular_file(tmp_path / "missing", destination)


def test_private_snapshot_detects_short_write_and_source_change(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"payload")
    destination = tmp_path / "snapshot" / "payload"
    monkeypatch.setattr(admin.os, "write", lambda _descriptor, _data: 0)
    with pytest.raises(admin.AdminError, match="short write"):
        admin._snapshot_regular_file(source, destination)
    assert not destination.exists()

    monkeypatch.undo()
    destination.parent.mkdir(exist_ok=True)
    real_fstat = admin.os.fstat
    calls = 0

    def changed_fstat(descriptor):
        nonlocal calls
        calls += 1
        result = real_fstat(descriptor)
        if calls == 2:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_dev=result.st_dev,
                st_ino=result.st_ino,
                st_size=result.st_size,
                st_mtime_ns=result.st_mtime_ns + 1,
                st_ctime_ns=result.st_ctime_ns,
            )
        return result

    monkeypatch.setattr(admin.os, "fstat", changed_fstat)
    with pytest.raises(admin.AdminError, match="changed while being copied"):
        admin._snapshot_regular_file(source, destination)
    assert not destination.exists()


def test_unsigned_metadata_and_source_snapshot_paths(tmp_path, monkeypatch):
    source = _complete_source_bundle(tmp_path / "source")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(admin, "_unsigned_development_mode", lambda: True)
    assert admin._snapshot_signed_metadata(source, workspace / "metadata") is None
    snapshot = admin._snapshot_source_bundle(source, workspace)
    assert snapshot != source
    assert (snapshot / "pyproject.toml").is_file()


def test_source_snapshot_rejects_missing_and_verification_drift(tmp_path, monkeypatch):
    with pytest.raises(admin.AdminError, match="does not exist"):
        admin._snapshot_source_tree(tmp_path / "missing", tmp_path / "snapshot", frozenset())

    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_text("content", encoding="utf-8")
    real_tree_entries = admin._tree_entries
    calls = 0

    def drifted_entries(root, ignored=frozenset()):
        nonlocal calls
        calls += 1
        if calls == 2:
            return [(Path("."), root.lstat())]
        return real_tree_entries(root, ignored)

    monkeypatch.setattr(admin, "_tree_entries", drifted_entries)
    with pytest.raises(admin.AdminError, match="snapshot verification failed"):
        admin._snapshot_source_tree(source, tmp_path / "drifted", frozenset())
    assert not (tmp_path / "drifted").exists()


def test_wheel_snapshot_requires_manifest_entry_and_skips_unlisted_profiles(tmp_path, monkeypatch):
    wheel = _wheel(tmp_path / "reticulumpi-0.3.0-py3-none-any.whl")
    monkeypatch.setattr(admin, "_verify_minisign", lambda *_args: None)
    manifest = tmp_path / admin.BUNDLE_MANIFEST_NAME
    manifest.write_text(
        f"{'a' * 64}  constraints/{admin._DEPENDENCY_PROFILES['core']}\n",
        encoding="utf-8",
    )
    with pytest.raises(admin.AdminError, match="does not contain bundle"):
        admin._snapshot_wheel_bundle(wheel, tmp_path / "missing-entry")

    manifest.write_text(f"{admin._sha256(wheel)}  {wheel.name}\n", encoding="utf-8")
    snapshot = admin._snapshot_wheel_bundle(wheel, tmp_path / "wheel-only")
    assert snapshot.read_bytes() == wheel.read_bytes()
    assert not (snapshot.parent / "constraints").exists()


@pytest.mark.parametrize("scheme", ["canonical", "legacy"])
def test_wheel_snapshot_copies_exactly_one_signed_dependency_scheme(tmp_path, monkeypatch, scheme):
    profiles = (
        admin._DEPENDENCY_PROFILES if scheme == "canonical" else admin._LEGACY_DEPENDENCY_PROFILES
    )
    other = (
        admin._LEGACY_DEPENDENCY_PROFILES if scheme == "canonical" else admin._DEPENDENCY_PROFILES
    )
    wheel = _wheel(
        tmp_path / f"reticulumpi-{scheme}.whl",
        dependency_profiles=profiles,
    )
    unlisted = tmp_path / "constraints" / other["core"]
    unlisted.write_text(f"unlisted==1 --hash=sha256:{'b' * 64}\n", encoding="utf-8")
    monkeypatch.setattr(admin, "_verify_minisign", lambda *_args: None)

    snapshot = admin._snapshot_wheel_bundle(wheel, tmp_path / f"snapshot-{scheme}")

    for filename in profiles.values():
        assert (snapshot.parent / "constraints" / filename).is_file()
    for filename in other.values():
        assert not (snapshot.parent / "constraints" / filename).exists()


def test_wheel_snapshot_rejects_signed_dependency_alias_duplicates(tmp_path, monkeypatch):
    wheel = _wheel(tmp_path / "reticulumpi-0.3.0-py3-none-any.whl")
    legacy = tmp_path / "constraints" / admin._LEGACY_DEPENDENCY_PROFILES["core"]
    legacy.write_text(f"legacy==1 --hash=sha256:{'a' * 64}\n", encoding="utf-8")
    manifest = tmp_path / admin.BUNDLE_MANIFEST_NAME
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + f"{admin._sha256(legacy)}  constraints/{legacy.name}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(admin, "_verify_minisign", lambda *_args: None)

    with pytest.raises(admin.AdminError, match="ambiguous canonical and legacy"):
        admin._snapshot_wheel_bundle(wheel, tmp_path / "snapshot-ambiguous")


def test_materialized_bundle_rejects_missing_symlink_and_special_file(tmp_path):
    missing = tmp_path / "missing.whl"
    with pytest.raises(admin.AdminError, match="bundle is missing or unsafe"):
        with admin._materialize_install_bundle(missing):
            pytest.fail("missing bundle must not be yielded")

    regular = tmp_path / "regular.whl"
    regular.write_bytes(b"content")
    link = tmp_path / "link.whl"
    link.symlink_to(regular)
    with pytest.raises(admin.AdminError, match="may not be a symlink"):
        with admin._materialize_install_bundle(link):
            pytest.fail("symlink bundle must not be yielded")

    fifo = tmp_path / "bundle.pipe"
    os.mkfifo(fifo)
    with pytest.raises(admin.AdminError, match="not a regular file or directory"):
        with admin._materialize_install_bundle(fifo):
            pytest.fail("special bundle must not be yielded")


def test_verified_private_input_digest_is_rechecked_before_execution(tmp_path):
    artifact = tmp_path / "artifact.whl"
    artifact.write_bytes(b"original")
    expected = admin._sha256(artifact)
    artifact.write_bytes(b"replacement")
    with pytest.raises(admin.AdminError, match="changed after private snapshot verification"):
        admin._require_unchanged_digest(artifact, expected, "wheel")


def test_release_immutability_requires_one_broker_and_available_interpreter(tmp_path, monkeypatch):
    release = tmp_path / "release"
    release.mkdir()
    monkeypatch.setattr(admin, "_validate_install_root_ancestry", lambda _path: None)
    monkeypatch.setattr(admin, "_validate_root_owned_regular_path", lambda *_args: None)
    with pytest.raises(admin.AdminError, match="exactly one immutable control broker"):
        admin._validate_release_immutability(release)

    broker = release / ".venv/lib/python3.11/site-packages/reticulumpi/control_broker.py"
    broker.parent.mkdir(parents=True)
    broker.write_text("# broker\n", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="interpreter is unavailable"):
        admin._validate_release_immutability(release)


def test_release_immutability_rejects_unowned_and_dangling_interpreter_symlinks(
    tmp_path, monkeypatch
):
    release = tmp_path / "release"
    broker = release / ".venv/lib/python3.11/site-packages/reticulumpi/control_broker.py"
    broker.parent.mkdir(parents=True)
    broker.write_text("# broker\n", encoding="utf-8")
    interpreter = release / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(tmp_path / "missing-python")
    monkeypatch.setattr(admin, "_validate_install_root_ancestry", lambda _path: None)
    monkeypatch.setattr(admin, "_validate_root_owned_regular_path", lambda *_args: None)
    real_lstat = Path.lstat

    def unowned_interpreter(path):
        if path == interpreter:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=1)
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", unowned_interpreter)
    with pytest.raises(admin.AdminError, match="symlink is not root-owned"):
        admin._validate_release_immutability(release)

    def root_owned_interpreter(path):
        if path == interpreter:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0)
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", root_owned_interpreter)
    with pytest.raises(admin.AdminError, match="symlink is invalid"):
        admin._validate_release_immutability(release)


def test_snapshot_destination_open_failure_closes_only_acquired_descriptor(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"payload")
    destination = tmp_path / "private" / "payload"
    real_open = admin.os.open

    def reject_destination(path, flags, *args):
        if os.fspath(path) == os.fspath(destination):
            raise PermissionError("destination rejected")
        return real_open(path, flags, *args)

    monkeypatch.setattr(admin.os, "open", reject_destination)
    with pytest.raises(admin.AdminError, match="cannot create private snapshot"):
        admin._snapshot_regular_file(source, destination)


def test_unsigned_wheel_snapshot_does_not_probe_absent_profiles(tmp_path, monkeypatch):
    wheel = tmp_path / "reticulumpi-0.3.0-py3-none-any.whl"
    wheel.write_bytes(b"development wheel")
    monkeypatch.setattr(admin, "_unsigned_development_mode", lambda: True)
    snapshot = admin._snapshot_wheel_bundle(wheel, tmp_path / "snapshot")
    assert snapshot.read_bytes() == wheel.read_bytes()
    assert not (snapshot.parent / "constraints").exists()


def test_materialized_bundle_rejects_unsafe_private_workspace(tmp_path, monkeypatch):
    wheel = tmp_path / "reticulumpi-0.3.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    real_lstat = Path.lstat

    def unsafe_workspace(path):
        result = real_lstat(path)
        if path.name.startswith("reticulumpi-input-snapshot-"):
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=result.st_uid + 1)
        return result

    monkeypatch.setattr(Path, "lstat", unsafe_workspace)
    with pytest.raises(admin.AdminError, match="snapshot directory is unsafe"):
        with admin._materialize_install_bundle(wheel):
            pytest.fail("unsafe snapshot workspace must never be yielded")


def test_layout_parser_handles_empty_relative_and_nonvenv_execstarts(admin_paths):
    admin_paths.systemd.mkdir(parents=True)
    (admin_paths.systemd / "reticulumpi.service").write_text(
        "[Service]\nExecStart=\nExecStart=reticulumpi\nExecStart=/usr/bin/reticulumpi\n",
        encoding="utf-8",
    )
    layout = admin._discover_legacy_layout()
    assert layout.install_roots == ()


def test_application_readiness_rejects_marker_that_never_stabilizes(monkeypatch):
    ticks = iter((0.0, 0.1, 0.2, 0.3, 2.0))
    monkeypatch.setattr(admin.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(admin.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(admin, "_service_active", lambda _name: True)
    monkeypatch.setattr(admin, "_readiness_marker_valid", lambda: True)
    with pytest.raises(admin.AdminError, match="fresh readiness marker"):
        admin._wait_application_ready(timeout=1.0, stable_for=2.0)


def test_identity_continuity_reports_missing_without_changed_detail():
    with pytest.raises(admin.AdminError, match=r"missing=identity$"):
        admin._verify_identity_continuity({"identity": "old"}, {})


def test_wheel_copy_requires_its_own_signed_manifest_entry(tmp_path):
    wheel = tmp_path / "reticulumpi-0.3.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    (tmp_path / admin.BUNDLE_MANIFEST_NAME).write_text(
        f"{'a' * 64}  unrelated.whl\n",
        encoding="utf-8",
    )
    destination = tmp_path / "destination"
    destination.mkdir()
    with pytest.raises(admin.AdminError, match="does not contain bundle"):
        admin._build_wheel(wheel, None, destination)


def test_database_restore_rejects_service_owned_parent_alias(admin_paths, tmp_path):
    source = tmp_path / "backup.db"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE restored(value TEXT)")
    admin_paths.data.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = admin_paths.data / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(admin.AdminError, match="may not contain a symlink"):
        admin._db_restore(
            SimpleNamespace(
                backup=str(source),
                database=str(alias / "target.db"),
                apply=False,
                dry_run=True,
            )
        )


def test_wheel_only_upgrade_cannot_bridge_missing_manifest_and_current(admin_paths, tmp_path):
    wheel = _wheel(tmp_path / "reticulumpi-0.3.0-py3-none-any.whl")
    args = SimpleNamespace(
        install_root=str(admin_paths.root),
        bundle=str(wheel),
        feature=[],
        apply=False,
        dry_run=True,
        start=False,
    )
    with pytest.raises(admin.AdminError, match="wheel-only upgrade cannot bridge"):
        admin._apply_release_materialized(args, "upgrade")


def test_custom_unit_config_is_imported_stably_when_canonical_is_missing(
    admin_paths, tmp_path, monkeypatch
):
    custom_config = tmp_path / "legacy/config/production.yaml"
    custom_config.parent.mkdir(parents=True)
    original = b"reticulumpi:\n  node_name: production\n"
    custom_config.write_bytes(original)
    admin_paths.systemd.mkdir(parents=True)
    (admin_paths.systemd / "reticulumpi.service").write_text(
        f"[Service]\nExecStart=/opt/legacy/.venv/bin/reticulumpi --config {custom_config}\n",
        encoding="utf-8",
    )
    source = _source_bundle(tmp_path / "source")
    discovered = admin._discover_legacy_config_source()
    assert discovered is not None and discovered.path == custom_config
    monkeypatch.setattr(admin.os, "chown", lambda *_args: None)
    admin._prepare_paths(source, discovered)
    assert admin.CONFIG_FILE.read_bytes() == original
    assert custom_config.read_bytes() == original

    admin.CONFIG_FILE.unlink()
    custom_config.write_bytes(b"changed\n")
    with pytest.raises(admin.AdminError, match="checksum mismatch"):
        admin._prepare_paths(source, discovered)


def test_legacy_cleanup_rechecks_complete_source_tree(admin_paths):
    legacy = admin_paths.home / ".config/reticulumpi"
    legacy.mkdir(parents=True)
    (legacy / "identity").write_bytes(b"identity")
    (legacy / "settings.yaml").write_text("before\n", encoding="utf-8")
    migrations = admin._migrate_legacy_home_state(())
    assert migrations
    (legacy / "settings.yaml").write_text("after\n", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="changed after migration"):
        admin._remove_migrated_legacy_state(migrations)
    assert legacy.is_dir()


def test_explicit_legacy_rollback_restores_mutable_environment_and_retains_evidence(
    admin_paths, monkeypatch, capsys
):
    admin_paths.config.mkdir(parents=True)
    admin_paths.data.mkdir(parents=True)
    admin_paths.systemd.mkdir(parents=True)
    admin_paths.sudoers.mkdir(parents=True)
    admin.CONFIG_FILE.write_text("legacy-config\n", encoding="utf-8")
    state = admin_paths.data / "state.txt"
    state.write_text("legacy-state\n", encoding="utf-8")
    unit = admin_paths.systemd / "reticulumpi.service"
    unit.write_text("legacy-unit\n", encoding="utf-8")
    sudoers = admin_paths.sudoers / "reticulumpi-chrony"
    sudoers.write_text("legacy-sudoers\n", encoding="utf-8")
    services = _inactive_service_evidence()
    legacy_backup = admin._backup_state("legacy", ())
    admin._persist_file_snapshots(legacy_backup, admin._snapshot_files(admin._managed_paths()))
    roots = admin._persist_legacy_bridge_evidence(legacy_backup, services)

    current = _release(admin_paths.root, "0.3.0")
    (admin_paths.root / "current").symlink_to(current)
    admin.CONFIG_FILE.write_text("candidate-config\n", encoding="utf-8")
    state.write_text("candidate-state\n", encoding="utf-8")
    unit.write_text("candidate-unit\n", encoding="utf-8")
    sudoers.unlink()
    _write_install_manifest(admin_paths, current, None)
    manifest = json.loads(admin.MANIFEST_FILE.read_text(encoding="utf-8"))
    manifest.update(
        {
            "legacy_bridge_backup": str(legacy_backup),
            "legacy_bridge_roots": list(roots),
            "legacy_bridge_services": services,
        }
    )
    admin._atomic_json(admin.MANIFEST_FILE, manifest)

    monkeypatch.setattr(admin, "_require_root", lambda: None)
    monkeypatch.setattr(admin, "_maintenance_lock", lambda: nullcontext())
    monkeypatch.setattr(admin, "_service_active", lambda _name: False)
    monkeypatch.setattr(admin, "_unit_enabled", lambda _name: False)
    monkeypatch.setattr(admin, "_run", lambda *_args, **_kwargs: "")

    dry_run = SimpleNamespace(to="legacy", apply=False, dry_run=True)
    assert admin._rollback(dry_run) == 0
    output = capsys.readouterr().out
    assert "restore mutable predecessor" in output
    assert "retained" in output
    assert state.read_text(encoding="utf-8") == "candidate-state\n"

    apply = SimpleNamespace(to="legacy", apply=True, dry_run=False)
    assert admin._rollback(apply) == 0
    assert not os.path.lexists(admin_paths.root / "current")
    assert not admin.MANIFEST_FILE.exists()
    assert admin.CONFIG_FILE.read_text(encoding="utf-8") == "legacy-config\n"
    assert state.read_text(encoding="utf-8") == "legacy-state\n"
    assert unit.read_text(encoding="utf-8") == "legacy-unit\n"
    assert sudoers.read_text(encoding="utf-8") == "legacy-sudoers\n"
    assert current.is_dir()
    assert legacy_backup.is_dir()


def test_immutable_upgrade_retains_bridge_evidence_for_later_legacy_rollback(admin_paths, capsys):
    admin_paths.config.mkdir(parents=True)
    admin_paths.data.mkdir(parents=True)
    admin.CONFIG_FILE.write_text("reticulumpi: {}\n", encoding="utf-8")
    services = _inactive_service_evidence()
    legacy_backup = admin._backup_state("legacy", ())
    admin._persist_file_snapshots(legacy_backup, admin._snapshot_files(admin._managed_paths()))
    roots = admin._persist_legacy_bridge_evidence(legacy_backup, services)

    bridged = _release(admin_paths.root, "0.3.0")
    upgraded = _release(admin_paths.root, "0.3.1")
    current = admin_paths.root / "current"
    current.symlink_to(upgraded)
    _write_install_manifest(admin_paths, bridged, None)
    predecessor_manifest = json.loads(admin.MANIFEST_FILE.read_text(encoding="utf-8"))
    predecessor_manifest.update(
        {
            "legacy_bridge_backup": str(legacy_backup),
            "legacy_bridge_roots": list(roots),
            "legacy_bridge_services": services,
        }
    )
    retained = admin._retained_legacy_bridge_evidence(
        predecessor_manifest,
        legacy_bridge=False,
        backup=None,
        bridge_roots=(),
        service_states=services,
    )
    _write_install_manifest(admin_paths, upgraded, bridged)
    upgraded_manifest = json.loads(admin.MANIFEST_FILE.read_text(encoding="utf-8"))
    upgraded_manifest.update(
        {
            "legacy_bridge_backup": retained[0],
            "legacy_bridge_roots": list(retained[1]),
            "legacy_bridge_services": retained[2],
        }
    )
    admin._atomic_json(admin.MANIFEST_FILE, upgraded_manifest)

    assert admin._rollback(SimpleNamespace(to="legacy", apply=False, dry_run=True)) == 0
    assert "restore mutable predecessor" in capsys.readouterr().out
    assert legacy_backup.is_dir()


def test_gps_feature_manages_gpsd_ordering_dropin(admin_paths, tmp_path):
    source = _complete_source_bundle(tmp_path / "source")
    admin._render_units(source, admin_paths.root, ("gps",))
    dropin = admin_paths.systemd / admin._GPSD_DROPIN_RELATIVE
    assert dropin.read_text(encoding="utf-8") == (
        "[Unit]\nWants=gpsd.service\nAfter=gpsd.service\n"
    )
    assert dropin in admin._managed_paths()
    admin._render_units(source, admin_paths.root, (), ("gps",))
    assert not dropin.exists()


def test_bridge_snapshots_restore_chrony_and_captive_dnsmasq_configuration(admin_paths):
    admin_paths.chrony_config.parent.mkdir(parents=True)
    admin_paths.captive_dnsmasq.parent.mkdir(parents=True)
    admin_paths.chrony_config.write_bytes(b"legacy chrony integration\n")
    admin_paths.captive_dnsmasq.write_bytes(b"legacy captive integration\n")
    admin_paths.chrony_config.chmod(0o640)
    admin_paths.captive_dnsmasq.chmod(0o644)
    backup = admin_paths.backups / "release-fixture"
    backup.mkdir(parents=True)

    snapshots = admin._snapshot_files(admin._managed_paths())
    admin._persist_file_snapshots(backup, snapshots)
    admin_paths.chrony_config.write_bytes(b"candidate chrony\n")
    admin_paths.captive_dnsmasq.unlink()

    admin._restore_files(admin._load_file_snapshots(backup))

    assert admin_paths.chrony_config.read_bytes() == b"legacy chrony integration\n"
    assert stat.S_IMODE(admin_paths.chrony_config.stat().st_mode) == 0o640
    assert admin_paths.captive_dnsmasq.read_bytes() == b"legacy captive integration\n"
    assert stat.S_IMODE(admin_paths.captive_dnsmasq.stat().st_mode) == 0o644


def test_legacy_meshchat_storage_is_backed_up_migrated_and_only_storage_scalar_rewritten(
    admin_paths, tmp_path, monkeypatch
):
    legacy_root = tmp_path / "opt/legacy-reticulumpi"
    storage = legacy_root / "meshchat/storage"
    storage.mkdir(parents=True)
    (storage / "identity").write_bytes(b"meshchat-identity")
    with closing(sqlite3.connect(storage / "meshchat.db")) as connection:
        connection.execute("CREATE TABLE messages(value TEXT)")
        connection.execute("INSERT INTO messages VALUES ('preserved')")
        connection.commit()
    admin_paths.systemd.mkdir(parents=True)
    admin_paths.systemd.joinpath("reticulumpi.service").write_text(
        "[Service]\n"
        f"WorkingDirectory={legacy_root}\n"
        f"ExecStart={legacy_root}/.venv/bin/reticulumpi --config {admin.CONFIG_FILE}\n",
        encoding="utf-8",
    )
    admin_paths.config.mkdir(parents=True)
    admin.CONFIG_FILE.write_text(
        "reticulumpi:\n"
        "  plugins:\n"
        "    meshchat_server:\n"
        "      enabled: true\n"
        f"      install_dir: {legacy_root}/meshchat\n"
        f"      storage_dir: {storage}  # durable state only\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(admin.os, "chown", lambda *_args: None)

    backup = admin._backup_state("legacy", ("dashboard",))
    metadata = json.loads((backup / "backup.json").read_text(encoding="utf-8"))
    mesh_record = next(
        record for record in metadata["state_roots"] if record["name"] == "legacy-meshchat-storage"
    )
    assert mesh_record["path"] == str(storage)
    assert mesh_record["present"] is True

    generic = admin._plan_legacy_config_path_migration()
    assert generic is None
    meshchat = admin._plan_meshchat_path_migration(legacy_bridge=False)
    assert meshchat is not None
    admin._apply_meshchat_path_migration(meshchat, legacy_bridge=False)
    migrated_config = admin.CONFIG_FILE.read_text(encoding="utf-8")
    assert f"install_dir: {legacy_root}/meshchat" in migrated_config
    assert (
        f"storage_dir: {admin.DATA_DIR}/meshchat/storage  # durable state only" in migrated_config
    )

    migrations = admin._migrate_legacy_home_state(("dashboard",))
    mesh_migration = next(item for item in migrations if item.source == storage)
    assert mesh_migration.destination == admin.DATA_DIR / "meshchat/storage"
    assert (mesh_migration.destination / "identity").read_bytes() == b"meshchat-identity"
    with closing(sqlite3.connect(mesh_migration.destination / "meshchat.db")) as connection:
        assert connection.execute("SELECT value FROM messages").fetchone() == ("preserved",)
    admin._remove_migrated_legacy_state(migrations)
    assert not storage.exists()


def test_live_sqlite_gate_clones_canonical_databases_and_records_changed_schema(
    admin_paths, monkeypatch
):
    legacy_data = admin_paths.home / ".local/share/reticulumpi"
    legacy_data.mkdir(parents=True)
    legacy_database = legacy_data / "messages.db"
    with closing(sqlite3.connect(legacy_database)) as connection, connection:
        connection.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, body TEXT)")
        connection.execute("PRAGMA user_version = 1")
    monkeypatch.setattr(admin, "_legacy_home_candidates", lambda: (admin_paths.home,))
    monkeypatch.setattr(admin, "_legacy_meshchat_storage_candidates", lambda: ())

    backup = admin._backup_state("legacy", ())
    migrations = admin._migrate_legacy_home_state(())
    live_database = admin_paths.data / ".local/share/reticulumpi/messages.db"
    with closing(sqlite3.connect(live_database)) as connection, connection:
        connection.execute("CREATE TABLE delivery(id INTEGER PRIMARY KEY, message_id INTEGER)")
        connection.execute("CREATE INDEX delivery_message ON delivery(message_id)")
        connection.execute(
            "CREATE TRIGGER delivery_insert AFTER INSERT ON delivery "
            "BEGIN UPDATE delivery SET message_id = NEW.message_id WHERE id = NEW.id; END"
        )
        connection.execute("CREATE VIEW message_bodies AS SELECT body FROM messages")
        connection.execute("PRAGMA user_version = 7")

    records = admin._validate_live_sqlite_state(
        backup,
        admin._state_roots(()),
        migrations,
    )

    assert len(records) == 1
    evidence = records[0]
    assert evidence["state"] == "data"
    assert evidence["path"] == ".local/share/reticulumpi/messages.db"
    assert evidence["preexisting"] is True
    assert evidence["user_version"] == 7
    assert {item["type"] for item in evidence["schema_objects"]} == {
        "index",
        "table",
        "trigger",
        "view",
    }
    evidence_file = backup / "validation/live-sqlite/evidence.json"
    assert evidence_file.is_file()
    assert stat.S_IMODE(evidence_file.stat().st_mode) == 0o600


def test_live_sqlite_gate_rejects_missing_preexisting_canonical_database(admin_paths):
    admin_paths.data.mkdir(parents=True)
    database = admin_paths.data / "required.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE required(value TEXT)")
    backup = admin._backup_state("current", ())
    database.unlink()

    with pytest.raises(admin.AdminError, match="omits pre-existing databases"):
        admin._validate_live_sqlite_state(backup, admin._state_roots(()), ())


def test_managed_current_release_does_not_become_a_legacy_meshchat_root(admin_paths):
    release = _release(admin_paths.root, "0.3.0")
    current = admin_paths.root / "current"
    current.symlink_to(release)
    admin_paths.systemd.mkdir(parents=True)
    admin_paths.systemd.joinpath("reticulumpi.service").write_text(
        "[Service]\n"
        f"Environment=HOME={admin.DATA_DIR}\n"
        f"WorkingDirectory={admin.DATA_DIR}\n"
        f"ExecStart={current}/.venv/bin/reticulumpi --config {admin.CONFIG_FILE}\n",
        encoding="utf-8",
    )

    roots = admin._state_roots(("dashboard",))

    assert all("current/meshchat/storage" not in str(root.path) for root in roots)
    assert admin._legacy_meshchat_storage_candidates() == ()


def test_prebackup_legacy_recovery_uses_journal_features_without_modern_readiness(
    admin_paths, monkeypatch
):
    candidate = admin_paths.root / "releases/0.3.0"
    candidate.mkdir(parents=True)
    admin_paths.config.mkdir(parents=True)
    admin.CONFIG_FILE.write_text("reticulumpi: {}\n", encoding="utf-8")
    services = _inactive_service_evidence()
    services["reticulumpi.service"] = {"active": True, "enabled": True}
    admin._atomic_json(
        admin.JOURNAL_FILE,
        {
            "schema": 1,
            "operation": "upgrade",
            "install_root": str(admin_paths.root),
            "previous_release": None,
            "new_release": str(candidate),
            "backup": None,
            "remove_candidate": True,
            "services_before": services,
            "features": ["dashboard"],
            "legacy_bridge": True,
            "state": "preparing",
        },
        0o600,
    )
    restored = []
    monkeypatch.setattr(
        admin,
        "_restore_service_states",
        lambda states, features, identities, roots, config_file, require_readiness: restored.append(
            (states, features, roots, config_file, require_readiness)
        ),
    )

    recovered = admin._recover_interrupted_transaction(admin_paths.root.resolve())

    assert recovered is not None and recovered["state"] == "recovered"
    assert restored and restored[0][1] == ("dashboard",)
    assert restored[0][4] is False
    assert not candidate.exists()


def test_prebackup_recovery_rejects_non_boolean_legacy_bridge_evidence(admin_paths):
    candidate = admin_paths.root / "releases/0.3.0"
    candidate.mkdir(parents=True)
    admin._atomic_json(
        admin.JOURNAL_FILE,
        {
            "schema": 1,
            "install_root": str(admin_paths.root),
            "previous_release": None,
            "new_release": str(candidate),
            "backup": None,
            "services_before": _inactive_service_evidence(),
            "features": [],
            "legacy_bridge": "yes",
            "state": "preparing",
        },
        0o600,
    )

    with pytest.raises(admin.AdminError, match="legacy-bridge evidence"):
        admin._recover_interrupted_transaction(admin_paths.root.resolve())


def test_installed_custom_config_overrides_stale_canonical_and_is_exactly_restorable(
    admin_paths, tmp_path, monkeypatch
):
    custom = tmp_path / "legacy-config/production.yaml"
    custom.parent.mkdir(parents=True)
    custom.parent.chmod(0o750)
    custom.write_bytes(b"reticulumpi:\n  node_name: active-custom\n")
    admin_paths.config.mkdir(parents=True)
    admin.CONFIG_FILE.write_bytes(b"reticulumpi:\n  node_name: stale-canonical\n")
    admin_paths.systemd.mkdir(parents=True)
    admin_paths.systemd.joinpath("reticulumpi.service").write_text(
        f"[Service]\nExecStart=/opt/legacy/.venv/bin/reticulumpi --config {custom}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        admin,
        "_validate_install_root_ancestry",
        lambda _path: (_ for _ in ()).throw(AssertionError("custom config is not an install root")),
    )
    discovered = admin._discover_legacy_config_source()
    assert discovered is not None and discovered.path == custom
    monkeypatch.setattr(admin, "_validate_install_root_ancestry", lambda _path: None)
    monkeypatch.setattr(admin.os, "chown", lambda *_args: None)
    admin._prepare_paths(_source_bundle(tmp_path / "source"), discovered)
    assert admin.CONFIG_FILE.read_bytes() == custom.read_bytes()

    backup = admin._backup_state(
        "legacy",
        config_file=custom,
        external_config_file=custom,
    )
    snapshots = admin._snapshot_files((custom,))
    admin._persist_file_snapshots(backup, snapshots)
    custom.write_bytes(b"changed while immutable\n")
    admin._restore_files(admin._load_file_snapshots(backup))
    assert custom.read_bytes() == b"reticulumpi:\n  node_name: active-custom\n"


def test_unsafe_legacy_dropins_are_removed_but_resource_limits_are_retained(admin_paths):
    dropin = admin_paths.systemd / "reticulumpi.service.d"
    dropin.mkdir(parents=True)
    safe = dropin / "30-resource-policy.conf"
    safe.write_text("[Service]\nMemoryHigh=1G\n", encoding="utf-8")
    unsafe = dropin / "90-exec-override.conf"
    unsafe.write_text(
        "[Service]\nExecStart=\nExecStart=/opt/legacy/.venv/bin/reticulumpi\n",
        encoding="utf-8",
    )
    admin_owned = admin.SYSTEMD_DIR / admin._RNSD_DROPIN_RELATIVE
    admin_owned.write_text("[Service]\nExecStartPre=/bin/true\n", encoding="utf-8")

    assert admin._unsafe_legacy_dropins() == (unsafe,)
    snapshots = admin._snapshot_files((unsafe,))
    admin._remove_legacy_dropins((unsafe,), snapshots)

    assert not unsafe.exists()
    assert safe.exists()
    assert admin_owned.exists()


def test_candidate_backup_records_legacy_only_roots_as_absent(admin_paths, tmp_path):
    admin_paths.config.mkdir(parents=True)
    admin_paths.data.mkdir(parents=True)
    admin.CONFIG_FILE.write_text("reticulumpi: {}\n", encoding="utf-8")
    legacy_storage = tmp_path / "legacy/meshchat/storage"
    legacy_storage.parent.mkdir(parents=True)
    current = (
        admin.StateRoot("etc", admin.CONFIG_DIR),
        admin.StateRoot("data", admin.DATA_DIR),
    )
    legacy = (
        *current,
        admin.StateRoot("legacy-meshchat-storage", legacy_storage),
    )
    exact = admin._merge_state_roots(current, legacy)

    backup = admin._backup_state("candidate", exact_roots=exact)
    metadata = json.loads((backup / "backup.json").read_text(encoding="utf-8"))
    meshchat = next(
        record for record in metadata["state_roots"] if record["name"] == "legacy-meshchat-storage"
    )
    assert meshchat["present"] is False

    legacy_storage.mkdir()
    (legacy_storage / "unexpected.db").write_bytes(b"legacy rollback residue")
    admin._restore_state_backup(backup)
    assert not legacy_storage.exists()


def test_admin_default_install_root_avoids_mutable_legacy_opt_tree():
    assert admin.DEFAULT_INSTALL_ROOT == Path("/srv/reticulumpi")


def _prepare_dual_meshchat_path_migration(admin_paths, tmp_path, monkeypatch):
    legacy_install = tmp_path / "opt/reticulumpi/meshchat"
    legacy_storage = legacy_install / "storage"
    legacy_storage.mkdir(parents=True)
    (legacy_install / "meshchat.py").write_text("legacy code\n", encoding="utf-8")
    external_install = tmp_path / "srv/reticulumpi-external/meshchat"
    external_install.mkdir(parents=True)
    (external_install / "meshchat.py").write_text("reviewed code\n", encoding="utf-8")
    admin_paths.config.mkdir(parents=True)
    admin.CONFIG_FILE.write_text(
        "reticulumpi:\n"
        "  plugins:\n"
        "    meshchat_server:\n"
        "      enabled: true\n"
        f"      install_dir: {legacy_install}  # independently staged below\n"
        f"      storage_dir: {legacy_storage}  # durable state\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(admin, "MESHCHAT_EXTERNAL_ROOT", external_install)
    monkeypatch.setattr(
        admin,
        "_legacy_meshchat_install_candidates",
        lambda: (legacy_install,),
    )
    monkeypatch.setattr(
        admin,
        "_legacy_meshchat_storage_candidates",
        lambda: (legacy_storage,),
    )
    monkeypatch.setattr(admin.os, "chown", lambda *_args: None)
    return legacy_install, legacy_storage, external_install


def test_meshchat_legacy_install_and_storage_paths_plan_and_apply_atomically(
    admin_paths, tmp_path, monkeypatch
):
    legacy_install, legacy_storage, external_install = _prepare_dual_meshchat_path_migration(
        admin_paths, tmp_path, monkeypatch
    )
    import reticulumpi.external_artifacts as external_artifacts

    digest = "d" * 64
    hash_calls: list[tuple[Path, bool]] = []

    def trusted_tree_hash(path, *, require_trusted=False):
        hash_calls.append((Path(path), require_trusted))
        return digest

    monkeypatch.setattr(external_artifacts, "tree_sha256", trusted_tree_hash)
    original_atomic_write = admin._atomic_write
    writes: list[bytes] = []

    def record_atomic_write(path, data, mode=0o600):
        writes.append(data)
        return original_atomic_write(path, data, mode)

    monkeypatch.setattr(admin, "_atomic_write", record_atomic_write)

    migration = admin._plan_meshchat_path_migration(legacy_bridge=True)

    assert migration is not None
    assert migration.external_tree_sha256 == digest
    assert [
        (rewrite.setting, rewrite.source_path, rewrite.destination_path)
        for rewrite in migration.rewrites
    ] == [
        ("install_dir", str(legacy_install), str(external_install.resolve())),
        (
            "storage_dir",
            str(legacy_storage),
            str((admin.DATA_DIR / "meshchat/storage").resolve()),
        ),
    ]

    admin._apply_meshchat_path_migration(migration, legacy_bridge=True)

    expected = (
        "reticulumpi:\n"
        "  plugins:\n"
        "    meshchat_server:\n"
        "      enabled: true\n"
        f"      install_dir: {external_install.resolve()}  # independently staged below\n"
        f"      storage_dir: {(admin.DATA_DIR / 'meshchat/storage').resolve()}  # durable state\n"
    )
    assert admin.CONFIG_FILE.read_text(encoding="utf-8") == expected
    assert writes == [expected.encode("utf-8")]
    assert hash_calls == [
        (external_install, True),
        (external_install, True),
    ]
    assert admin._plan_meshchat_path_migration(legacy_bridge=True) is None


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            ArtifactPolicyError("unsafe owner"),
            id="policy-error",
        ),
        pytest.param(OSError("tree unavailable"), id="os-error"),
    ],
)
def test_meshchat_plan_rejects_untrusted_or_unavailable_external_tree_before_write(
    admin_paths, tmp_path, monkeypatch, failure
):
    _prepare_dual_meshchat_path_migration(admin_paths, tmp_path, monkeypatch)
    import reticulumpi.external_artifacts as external_artifacts

    original = admin.CONFIG_FILE.read_bytes()
    monkeypatch.setattr(
        external_artifacts,
        "tree_sha256",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(admin.AdminError, match="MeshChat external tree is not trusted"):
        admin._plan_meshchat_path_migration(legacy_bridge=True)

    assert admin.CONFIG_FILE.read_bytes() == original


def test_meshchat_apply_rejects_external_tree_digest_toctou_before_atomic_write(
    admin_paths, tmp_path, monkeypatch
):
    _prepare_dual_meshchat_path_migration(admin_paths, tmp_path, monkeypatch)
    import reticulumpi.external_artifacts as external_artifacts

    digests = iter(("a" * 64, "b" * 64))
    monkeypatch.setattr(
        external_artifacts,
        "tree_sha256",
        lambda *_args, **_kwargs: next(digests),
    )
    migration = admin._plan_meshchat_path_migration(legacy_bridge=True)
    assert migration is not None
    original = admin.CONFIG_FILE.read_bytes()
    monkeypatch.setattr(
        admin,
        "_atomic_write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("configuration write must not begin")
        ),
    )

    with pytest.raises(admin.AdminError, match="external tree changed after migration planning"):
        admin._apply_meshchat_path_migration(migration, legacy_bridge=True)

    assert admin.CONFIG_FILE.read_bytes() == original


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            ArtifactPolicyError("permissions changed"),
            id="policy-error",
        ),
        pytest.param(OSError("tree disappeared"), id="os-error"),
    ],
)
def test_meshchat_apply_revalidates_external_tree_trust_before_atomic_write(
    admin_paths, tmp_path, monkeypatch, failure
):
    _prepare_dual_meshchat_path_migration(admin_paths, tmp_path, monkeypatch)
    import reticulumpi.external_artifacts as external_artifacts

    monkeypatch.setattr(external_artifacts, "tree_sha256", lambda *_args, **_kwargs: "a" * 64)
    migration = admin._plan_meshchat_path_migration(legacy_bridge=True)
    assert migration is not None
    original = admin.CONFIG_FILE.read_bytes()
    monkeypatch.setattr(
        external_artifacts,
        "tree_sha256",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )
    monkeypatch.setattr(
        admin,
        "_atomic_write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("configuration write must not begin")
        ),
    )

    with pytest.raises(admin.AdminError, match="MeshChat external tree is not trusted"):
        admin._apply_meshchat_path_migration(migration, legacy_bridge=True)

    assert admin.CONFIG_FILE.read_bytes() == original


def test_legacy_meshchat_install_candidates_exclude_managed_and_external_trees(
    tmp_path, monkeypatch
):
    legacy_root = tmp_path / "opt/legacy-reticulumpi"
    (legacy_root / "meshchat").mkdir(parents=True)
    release = tmp_path / "srv/reticulumpi/releases/0.3.2"
    (release / "meshchat").mkdir(parents=True)
    current = release.parents[1] / "current"
    current.symlink_to(release)
    external = tmp_path / "srv/reticulumpi-external/meshchat"
    external.mkdir(parents=True)
    missing_root = tmp_path / "opt/missing"
    layout = admin.LegacyLayout(
        homes=(),
        install_roots=(
            legacy_root,
            legacy_root,
            current,
            release,
            external.parent,
            missing_root,
        ),
        config_files=(),
        evidence=(),
    )
    monkeypatch.setattr(admin, "MESHCHAT_EXTERNAL_ROOT", external)
    monkeypatch.setattr(admin, "_discover_legacy_layout", lambda: layout)

    assert admin._legacy_meshchat_install_candidates() == (legacy_root / "meshchat",)


def test_external_artifact_digest_dispatches_file_and_tree_and_prints_only_digest(
    tmp_path, monkeypatch, capsys
):
    import reticulumpi.external_artifacts as external_artifacts

    file_path = tmp_path / "decoder"
    tree_path = tmp_path / "meshchat"
    calls: list[tuple[str, Path]] = []

    def file_digest(path):
        calls.append(("file", Path(path)))
        return "1" * 64

    def tree_digest(path):
        calls.append(("tree", Path(path)))
        return "2" * 64

    monkeypatch.setattr(external_artifacts, "file_sha256", file_digest)
    monkeypatch.setattr(external_artifacts, "tree_sha256", tree_digest)

    parser = admin._build_parser()
    file_args = parser.parse_args(["external-artifact", "digest", "--kind", "file", str(file_path)])
    tree_args = parser.parse_args(["external-artifact", "digest", "--kind", "tree", str(tree_path)])

    assert file_args.handler(file_args) == 0
    assert capsys.readouterr().out == "1" * 64 + "\n"
    assert tree_args.handler(tree_args) == 0
    assert capsys.readouterr().out == "2" * 64 + "\n"
    assert calls == [("file", file_path), ("tree", tree_path)]


@pytest.mark.parametrize(
    ("kind", "failure"),
    [
        pytest.param(
            "file",
            ArtifactPolicyError("unsafe decoder"),
            id="policy-error",
        ),
        pytest.param("tree", OSError("meshchat unavailable"), id="os-error"),
    ],
)
def test_external_artifact_digest_translates_hash_failures(kind, failure, tmp_path, monkeypatch):
    import reticulumpi.external_artifacts as external_artifacts

    monkeypatch.setattr(
        external_artifacts,
        "file_sha256",
        lambda *_args: (_ for _ in ()).throw(failure),
    )
    monkeypatch.setattr(
        external_artifacts,
        "tree_sha256",
        lambda *_args: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(admin.AdminError, match=str(failure)):
        admin._external_artifact_digest(SimpleNamespace(kind=kind, path=tmp_path / "artifact"))
