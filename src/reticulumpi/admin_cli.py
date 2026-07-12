"""Root-owned installation, rollback, diagnostics, and database administration.

The command intentionally uses only the Python standard library so it can run
from a release bundle before ReticulumPi's optional dependencies are installed.
Mutating commands default to a dry run and require both ``--apply`` and root.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import email.parser
import fcntl
import hashlib
import json
import os
import platform
import pwd
import re
import secrets
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import zipfile
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator

from reticulumpi.cli_help import StableHelpFormatter
from reticulumpi.platform_policy import (
    LEGACY_UNIVERSAL_DEPENDENCY_PROFILES,
    LEGACY_UNIVERSAL_HASH_LOCK_SET,
    UNIVERSAL_DEPENDENCY_PROFILES,
    UNIVERSAL_HASH_LOCK_SET,
    UnsupportedPlatformError,
    normalise_architecture,
    select_platform_profile,
)


DEFAULT_INSTALL_ROOT = Path("/srv/reticulumpi")
CONFIG_DIR = Path("/etc/reticulumpi")
CONFIG_FILE = CONFIG_DIR / "config.yaml"
DATA_DIR = Path("/var/lib/reticulumpi")
CACHE_DIR = Path("/var/cache/reticulumpi")
BACKUP_DIR = Path("/var/backups/reticulumpi")
ADMIN_STATE_DIR = BACKUP_DIR / "admin"
RUN_DIR = Path("/run/reticulumpi")
SYSTEMD_DIR = Path("/etc/systemd/system")
LIBEXEC_DIR = Path("/usr/libexec/reticulumpi")
SHARED_CONFIG_DIR = Path("/usr/share/reticulumpi/config")
SUDOERS_DIR = Path("/etc/sudoers.d")
CHRONY_CONFIG_FILE = Path("/etc/chrony/conf.d/reticulumpi-gps.conf")
CAPTIVE_DNSMASQ_CONFIG_FILE = Path("/etc/dnsmasq.d/reticulumpi-captive-portal.conf")
MANIFEST_FILE = CONFIG_DIR / "install.json"
JOURNAL_FILE = ADMIN_STATE_DIR / "transaction.json"
LOCK_FILE = Path("/run/lock/reticulumpi-maintenance.lock")
SERVICE_USER = "reticulumpi"
SYSTEMCTL = "/usr/bin/systemctl"
USERADD = "/usr/sbin/useradd"
MINISIGN = "/usr/bin/minisign"
RELEASE_PUBLIC_KEY_FILE = Path("/usr/share/reticulumpi/release.pub")
OS_RELEASE_FILE = Path("/etc/os-release")
BUNDLE_MANIFEST_NAME = "SHA256SUMS"
BUNDLE_SIGNATURE_NAME = "SHA256SUMS.minisig"
UNSIGNED_DEV_ENV = "RETICULUMPI_ADMIN_ALLOW_UNSIGNED_DEV"
DEFAULT_CURRENT_PREFIX = "/opt/reticulumpi/current"
_UNSAFE_ROOTS = {Path("/"), Path("/usr"), Path("/etc"), Path("/var"), Path("/home")}
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_INSTALL_ARCHIVE_PATTERN = re.compile(
    r"^reticulumpi-install-arm64-(?P<version>[A-Za-z0-9][A-Za-z0-9._+-]{0,63})\.tar\.gz$"
)
_RELEASE_BACKUP_PREFIX = "release-"
_DB_BACKUP_PREFIX = "db-"
_TERMINAL_TRANSACTION_STATES = frozenset({"complete", "rolled_back", "recovered"})
_DEPENDENCY_PROFILES = dict(UNIVERSAL_DEPENDENCY_PROFILES)
_LEGACY_DEPENDENCY_PROFILES = dict(LEGACY_UNIVERSAL_DEPENDENCY_PROFILES)
_PACKAGE_FEATURES = frozenset(
    {
        "dashboard",
        "nomadnet",
        "lora",
        "sensors",
        "meshtastic",
        "meshcore",
        "space",
        "gps",
        "adsb",
    }
)
_DASHBOARD_READY_FILE = "dashboard-ready"
_TRANSACTION_SERVICE_NAMES = (
    "reticulumpi.service",
    "rnsd.service",
    "rnsd-watchdog.timer",
    "reticulumpi-control.socket",
)
_MANAGED_UNIT_NAMES = {
    "reticulumpi.service",
    "reticulumpi-control.socket",
    "reticulumpi-control@.service",
    "rnsd.service",
    "rnsd-watchdog.service",
    "rnsd-watchdog.timer",
}
_MANAGED_HELPER_NAMES = {
    "restart_services.sh",
    "captive_portal_helper.sh",
    "simulate_offline.sh",
    "chrony_helper.sh",
}
_RNSD_DROPIN_RELATIVE = Path("reticulumpi.service.d/10-rnsd.conf")
_GPSD_DROPIN_RELATIVE = Path("reticulumpi.service.d/20-gpsd.conf")
_SAFE_LEGACY_SERVICE_DROPIN_KEYS = frozenset(
    {
        "CPUQuota",
        "IOWeight",
        "MemoryHigh",
        "MemoryLow",
        "MemoryMax",
        "MemoryMin",
        "Nice",
        "OOMScoreAdjust",
        "Restart",
        "RestartSec",
        "TasksMax",
        "TimeoutStartSec",
        "TimeoutStopSec",
    }
)
_LEGACY_SUDOERS_NAMES = {
    "reticulumpi-services",
    "reticulumpi-offline",
    "reticulumpi-captive-portal",
    "reticulumpi-chrony",
}


class AdminError(RuntimeError):
    """An expected, user-actionable administration failure."""


@dataclass(frozen=True)
class InstallManifest:
    schema: int
    version: str
    install_root: str
    release: str
    previous_release: str | None
    features: tuple[str, ...]
    installed_at: str
    bundle_sha256: str = ""
    legacy_bridge_backup: str | None = None
    legacy_bridge_roots: tuple[dict[str, str], ...] = ()
    legacy_bridge_services: dict[str, dict[str, bool]] | None = None
    platform_profile: dict[str, object] | None = None


@dataclass(frozen=True)
class FileSnapshot:
    """Pre-transaction state for one root-owned managed file."""

    path: Path
    data: bytes | None
    mode: int | None
    uid: int | None = None
    gid: int | None = None


@dataclass(frozen=True)
class StateRoot:
    """One durable directory participating in an administration transaction."""

    name: str
    path: Path


@dataclass
class RestoreStage:
    """Prepared atomic replacement for one durable state root."""

    root: StateRoot
    present: bool
    manifest: list[dict[str, object]]
    temporary: Path | None
    displaced: Path | None = None
    switched: bool = False


@dataclass(frozen=True)
class LegacyMigration:
    """Verified legacy service-home state copied into canonical durable storage."""

    source: Path
    destination: Path
    identity_hashes: dict[str, str]
    source_manifest: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class LegacyConfigSource:
    """A stable, installed-unit-discovered configuration to import."""

    path: Path
    sha256: str


@dataclass(frozen=True)
class FileTransferPolicyMigration:
    """One deterministic legacy file-transfer policy rewrite."""

    policy: str
    insertion_index: int
    indentation: str
    source_sha256: str


@dataclass(frozen=True)
class LegacyConfigPathMigration:
    """A redacted plan for rewriting legacy service-home path prefixes."""

    source_prefix: str
    destination_prefix: str
    replacement_count: int
    source_sha256: str


@dataclass(frozen=True)
class MeshChatStoragePathMigration:
    """A locked rewrite of only meshchat_server.storage_dir."""

    line_index: int
    source_path: str
    destination_path: str
    source_sha256: str


@dataclass(frozen=True)
class LegacyLayout:
    """Paths discovered from the installed service definition.

    The service account database is only a fallback.  Older installations
    frequently used a custom ``HOME`` or checkout path in the unit itself.
    """

    homes: tuple[Path, ...]
    install_roots: tuple[Path, ...]
    config_files: tuple[Path, ...]
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class DashboardCredentialMigration:
    """Redacted plan for rotating an exposed legacy dashboard credential."""

    reason: str
    secret_dir: Path
    config_sha256: str | None
    plaintext_line: int | None


_OPEN_FILE_TRANSFER_WARNING = (
    "SECURITY WARNING: legacy file_transfer.allowed_identities is explicitly empty; "
    "the migration preserves its public behavior with access_policy: open. Review this "
    "endpoint immediately."
)


def _run(command: list[str], *, check: bool = True, capture: bool = False) -> str:
    result = subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=capture,
    )
    return result.stdout.strip() if capture else ""


def _validate_version(value: object) -> str:
    version = str(value)
    if not _VERSION_PATTERN.fullmatch(version):
        raise AdminError(f"invalid release version: {version!r}")
    return version


def _generated_scm_version(bundle: Path) -> str | None:
    """Read setuptools-scm's generated version module without executing it."""
    version_file = bundle / "src/reticulumpi/_version.py"
    if version_file.is_symlink() or not version_file.is_file():
        return None
    try:
        tree = ast.parse(version_file.read_text(encoding="utf-8"), filename=str(version_file))
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise AdminError(f"invalid generated version metadata in {version_file}: {exc}") from exc

    versions: set[str] = set()
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or not isinstance(statement.value, ast.Constant):
            continue
        if not isinstance(statement.value.value, str):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            versions.add(_validate_version(statement.value.value))
    if len(versions) > 1:
        raise AdminError(f"conflicting generated version metadata in {version_file}")
    return next(iter(versions), None)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _reject_symlink_components(path: Path) -> None:
    """Reject any existing symlink in a privileged destination path."""
    candidate = path.expanduser().absolute()
    parts = candidate.parts
    current = Path(parts[0])
    for part in parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise AdminError(f"privileged path contains a symlink: {current}")


def _validate_install_root_ancestry(path: Path) -> None:
    """Require every existing install-root component to be immutable by non-root users."""

    candidate = path.expanduser().absolute()
    current = Path(candidate.parts[0])
    root_stat = current.lstat()
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != 0
        or stat.S_IMODE(root_stat.st_mode) & 0o022
    ):
        raise AdminError(f"install-root ancestor is not root-owned and immutable: {current}")
    missing = False
    for index, part in enumerate(candidate.parts[1:], 1):
        current /= part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            missing = True
            continue
        except OSError as exc:
            raise AdminError(f"cannot validate install-root ancestor {current}: {exc}") from exc
        if missing:
            raise AdminError(f"install-root path changed during validation: {current}")
        if stat.S_ISLNK(current_stat.st_mode):
            raise AdminError(f"install-root ancestor may not be a symlink: {current}")
        is_final = index == len(candidate.parts) - 1
        if not stat.S_ISDIR(current_stat.st_mode):
            label = "root" if is_final else "ancestor"
            raise AdminError(f"install {label} is not a directory: {current}")
        if current_stat.st_uid != 0 or stat.S_IMODE(current_stat.st_mode) & 0o022:
            raise AdminError(f"install-root ancestor is not root-owned and immutable: {current}")


def _ensure_journal_directory() -> None:
    """Create and validate the root-only administration evidence directory."""

    _validate_install_root_ancestry(JOURNAL_FILE.parent)
    _ensure_real_directory(JOURNAL_FILE.parent, mode=0o700)
    _validate_install_root_ancestry(JOURNAL_FILE.parent)
    directory_stat = JOURNAL_FILE.parent.lstat()
    expected_uid = 0 if os.geteuid() == 0 else os.geteuid()
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or directory_stat.st_uid != expected_uid
        or stat.S_IMODE(directory_stat.st_mode) & 0o077
    ):
        raise AdminError(
            f"transaction evidence directory ownership or permissions are unsafe: "
            f"{JOURNAL_FILE.parent}"
        )


def _ensure_real_directory(path: Path, *, mode: int | None = 0o755) -> None:
    _reject_symlink_components(path)
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir() or path.is_symlink():
        raise AdminError(f"expected a real directory: {path}")
    if mode is not None and not existed:
        path.chmod(mode)


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    _ensure_real_directory(path.parent, mode=None)
    if path.is_symlink():
        raise AdminError(f"refusing to replace symlink: {path}")
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_copy(source: Path, destination: Path, mode: int) -> None:
    if source.is_symlink() or not source.is_file():
        raise AdminError(f"required source must be a regular file: {source}")
    _atomic_write(destination, source.read_bytes(), mode)


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdminError(f"invalid {label} at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AdminError(f"invalid {label} at {path}: expected a JSON object")
    return value


def _validate_platform_metadata(value: object) -> dict[str, object] | None:
    """Validate the persisted supported-platform selection without probing the host."""

    if value is None:
        return None
    if not isinstance(value, dict):
        raise AdminError("manifest platform_profile must be an object or null")
    required_strings = {
        "profile_key",
        "system",
        "architecture",
        "distribution",
        "codename",
        "version_id",
        "python",
        "python_series",
        "dependency_lock_set",
        "dependency_lock_scope",
    }
    if not required_strings.issubset(value) or not all(
        isinstance(value[key], str) and value[key] for key in required_strings
    ):
        raise AdminError("manifest platform_profile has missing or invalid metadata")
    profiles = value.get("dependency_profiles")
    lock_set = value.get("dependency_lock_set")
    supported_lock_metadata = (
        profiles == _DEPENDENCY_PROFILES and lock_set == UNIVERSAL_HASH_LOCK_SET
    ) or (profiles == _LEGACY_DEPENDENCY_PROFILES and lock_set == LEGACY_UNIVERSAL_HASH_LOCK_SET)
    if not supported_lock_metadata:
        raise AdminError("manifest platform_profile dependency locks are invalid")
    common = {
        "system": "Linux",
        "architecture": "arm64",
        "dependency_lock_scope": "shared-universal",
    }
    if any(value[key] != expected for key, expected in common.items()):
        raise AdminError("manifest platform_profile does not describe a supported lane")
    profile_key = value["profile_key"]
    if profile_key == "linux-arm64-debian-bookworm-py311":
        supported = (
            value["distribution"] in {"debian", "raspbian"}
            and value["codename"] == "bookworm"
            and value["version_id"] == "12"
            and value["python_series"] == "3.11"
            and str(value["python"]).startswith("3.11.")
        )
    elif profile_key == "linux-arm64-ubuntu-noble-py312":
        supported = (
            value["distribution"] == "ubuntu"
            and value["codename"] == "noble"
            and value["version_id"] == "24.04"
            and value["python_series"] == "3.12"
            and str(value["python"]).startswith("3.12.")
        )
    else:
        supported = False
    if not supported:
        raise AdminError("manifest platform_profile does not describe a supported lane")
    return dict(value)


def _validate_manifest(value: dict[str, object], expected_root: Path | None = None) -> dict:
    required = {
        "schema",
        "version",
        "install_root",
        "release",
        "previous_release",
        "features",
        "installed_at",
    }
    missing = required - value.keys()
    if missing:
        raise AdminError(f"installation manifest is missing: {', '.join(sorted(missing))}")
    if value["schema"] != 1:
        raise AdminError(f"unsupported installation manifest schema: {value['schema']!r}")
    root = _safe_install_root(str(value["install_root"]))
    if expected_root is not None and root != expected_root:
        raise AdminError(
            f"installation manifest root {root} does not match requested root {expected_root}"
        )
    version = _validate_version(value["version"])
    release = Path(str(value["release"])).expanduser().resolve()
    releases = (root / "releases").resolve()
    if not _is_within(release, releases):
        raise AdminError(f"manifest release is outside {releases}: {release}")
    previous_raw = value["previous_release"]
    previous: str | None
    if previous_raw is None:
        previous = None
    elif isinstance(previous_raw, str):
        resolved_previous = Path(previous_raw).expanduser().resolve()
        if not _is_within(resolved_previous, releases):
            raise AdminError(f"manifest previous release is outside {releases}")
        previous = str(resolved_previous)
    else:
        raise AdminError("manifest previous_release must be a path or null")
    raw_features = value["features"]
    if not isinstance(raw_features, (list, tuple)) or not all(
        isinstance(feature, str) for feature in raw_features
    ):
        raise AdminError("manifest features must be a list of strings")
    features = tuple(sorted(set(raw_features)))
    _extras(features)
    installed_at = value["installed_at"]
    if not isinstance(installed_at, str) or not installed_at:
        raise AdminError("manifest installed_at must be a non-empty string")
    bridge_backup_raw = value.get("legacy_bridge_backup")
    bridge_backup: str | None = None
    bridge_roots_raw = value.get("legacy_bridge_roots", [])
    bridge_services_raw = value.get("legacy_bridge_services")
    platform_profile = _validate_platform_metadata(value.get("platform_profile"))
    if bridge_backup_raw is not None:
        if not isinstance(bridge_backup_raw, str):
            raise AdminError("manifest legacy bridge backup must be a path or null")
        requested_backup = Path(bridge_backup_raw).expanduser()
        if not requested_backup.is_absolute():
            raise AdminError("manifest legacy bridge backup must be absolute")
        backup = requested_backup.resolve()
        if (
            not _is_within(backup, BACKUP_DIR.resolve())
            or backup.parent != BACKUP_DIR.resolve()
            or not backup.name.startswith(_RELEASE_BACKUP_PREFIX)
        ):
            raise AdminError("manifest legacy bridge backup is outside the managed backup root")
        bridge_backup = str(backup)
        if not isinstance(bridge_roots_raw, (list, tuple)) or not bridge_roots_raw:
            raise AdminError("manifest legacy bridge root evidence is missing")
        bridge_roots = _validate_backup_root_evidence(bridge_roots_raw)
        bridge_services = _validate_service_state_snapshot(bridge_services_raw)
    else:
        if bridge_roots_raw not in ([], ()) or bridge_services_raw is not None:
            raise AdminError("manifest has legacy bridge evidence without a backup")
        bridge_roots = ()
        bridge_services = None
    normalized = dict(value)
    normalized.update(
        {
            "version": version,
            "install_root": str(root),
            "release": str(release),
            "previous_release": previous,
            "features": features,
            "legacy_bridge_backup": bridge_backup,
            "legacy_bridge_roots": bridge_roots,
            "legacy_bridge_services": bridge_services,
            "platform_profile": platform_profile,
        }
    )
    return normalized


def _load_manifest(expected_root: Path | None = None) -> dict:
    if not MANIFEST_FILE.is_file() or MANIFEST_FILE.is_symlink():
        raise AdminError(f"no valid installation manifest found at {MANIFEST_FILE}")
    return _validate_manifest(
        _read_json_object(MANIFEST_FILE, "installation manifest"),
        expected_root,
    )


def _atomic_json(path: Path, value: object, mode: int = 0o640) -> None:
    if path == JOURNAL_FILE:
        _ensure_journal_directory()
        mode = 0o600
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write(path, payload, mode)


@contextlib.contextmanager
def _maintenance_lock() -> Iterator[None]:
    _ensure_real_directory(LOCK_FILE.parent)
    if LOCK_FILE.is_symlink():
        raise AdminError(f"maintenance lock may not be a symlink: {LOCK_FILE}")
    with LOCK_FILE.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AdminError("another ReticulumPi maintenance operation is active") from exc
        yield


def _require_root() -> None:
    if os.geteuid() != 0:
        raise AdminError("--apply requires root; rerun with sudo or use --dry-run")


def _safe_install_root(raw_path: str) -> Path:
    requested = Path(raw_path).expanduser()
    if requested.absolute() in {
        Path("/"),
        Path("/usr"),
        Path("/etc"),
        Path("/var"),
        Path("/home"),
        Path("/tmp"),
    }:
        raise AdminError(f"unsafe install root: {requested}")
    _reject_symlink_components(requested)
    _validate_install_root_ancestry(requested)
    root = requested.resolve()
    if root in _UNSAFE_ROOTS or len(root.parts) < 3:
        raise AdminError(f"unsafe install root: {root}")
    return root


def _read_os_release(path: Path = OS_RELEASE_FILE) -> dict[str, str]:
    if path.is_symlink():
        resolved = path.resolve()
        if (
            resolved != Path("/usr/lib/os-release")
            or not resolved.is_file()
            or resolved.is_symlink()
        ):
            raise AdminError(f"operating-system metadata symlink is unsafe: {path}")
        path = resolved
    if not path.is_file():
        raise AdminError(f"operating-system metadata is unavailable or unsafe: {path}")
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise AdminError(f"cannot read operating-system metadata {path}: {exc}") from exc
    for line in lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        if not re.fullmatch(r"[A-Z0-9_]+", key):
            continue
        try:
            parsed = shlex.split(raw, posix=True)
        except ValueError as exc:
            raise AdminError(f"invalid operating-system metadata value for {key}") from exc
        values[key] = parsed[0] if parsed else ""
    return values


def _normalise_architecture(machine: str) -> str:
    """Compatibility wrapper for callers and tests using the old helper name."""

    return normalise_architecture(machine)


def _preflight_platform(
    *,
    system: str | None = None,
    machine: str | None = None,
    version_info: tuple[int, ...] | None = None,
    os_release: dict[str, str] | None = None,
) -> dict[str, object]:
    """Fail closed unless the host matches a complete supported production lane.

    Injectable inputs keep unit tests architecture-independent; production
    callers intentionally use the live host values.
    """

    live_system = system if system is not None else platform.system()
    live_machine = machine if machine is not None else platform.machine()
    python = version_info if version_info is not None else tuple(sys.version_info[:3])
    release = os_release if os_release is not None else _read_os_release()
    try:
        profile = select_platform_profile(
            system=live_system,
            machine=live_machine,
            version_info=python,
            os_release=release,
        )
    except UnsupportedPlatformError as exc:
        raise AdminError(str(exc)) from exc
    return profile.as_metadata()


def _source_metadata(bundle: Path) -> tuple[str, Path | None]:
    if bundle.is_dir():
        pyproject = bundle / "pyproject.toml"
        if pyproject.is_symlink() or not pyproject.is_file():
            raise AdminError(f"source bundle has no pyproject.toml: {bundle}")
        try:
            with pyproject.open("rb") as handle:
                metadata = tomllib.load(handle)
            project = metadata["project"]
            if str(project["name"]).lower() != "reticulumpi":
                raise AdminError("source bundle project name is not reticulumpi")
            if "version" in project:
                # Compatibility for pre-setuptools-scm and test fixture bundles.
                version = _validate_version(project["version"])
            elif "version" in project.get("dynamic", []):
                version = _generated_scm_version(bundle)
                if version is None:
                    raise AdminError(
                        "dynamic source bundle has no generated _version.py; "
                        "install the release wheel or an unpacked release sdist"
                    )
            else:
                raise AdminError("project metadata declares no version")
        except (KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
            raise AdminError(f"invalid project metadata in {pyproject}: {exc}") from exc
        return version, bundle
    if bundle.suffix == ".whl" and bundle.is_file():
        parts = bundle.name.split("-")
        if len(parts) < 2 or parts[0].replace("_", "-").lower() != "reticulumpi":
            raise AdminError(f"not a ReticulumPi wheel: {bundle}")
        return _validate_version(parts[1]), None
    raise AdminError("bundle must be a ReticulumPi source directory or wheel")


def _install_archive_version(bundle: Path) -> str | None:
    match = _INSTALL_ARCHIVE_PATTERN.fullmatch(bundle.name)
    return _validate_version(match.group("version")) if match else None


def _extract_install_archive(bundle: Path, destination: Path) -> tuple[str, Path]:
    """Safely extract one signed ARM64 install-bundle root."""

    filename_version = _install_archive_version(bundle)
    if filename_version is None:
        raise AdminError(f"not a ReticulumPi ARM64 install bundle: {bundle.name}")
    expected_root = f"reticulumpi-{filename_version}"
    total_size = 0
    file_count = 0
    try:
        with tarfile.open(bundle, mode="r:gz") as archive:
            members = archive.getmembers()
            if not members:
                raise AdminError("install bundle archive is empty")
            for member in members:
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
                    raise AdminError(f"install bundle contains an unsafe member: {member.name!r}")
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise AdminError(
                        f"install bundle contains a forbidden special member: {member.name}"
                    )
                if not (member.isdir() or member.isfile()):
                    raise AdminError(
                        f"install bundle contains an unsupported member: {member.name}"
                    )
                if member.isfile():
                    file_count += 1
                    total_size += member.size
                    if file_count > 20_000 or total_size > 2 * 1024 * 1024 * 1024:
                        raise AdminError("install bundle exceeds the extraction safety limit")
            for member in members:
                relative = Path(*PurePosixPath(member.name.rstrip("/")).parts)
                target = destination / relative
                if member.isdir():
                    _ensure_real_directory(target, mode=0o755)
                    continue
                _ensure_real_directory(target.parent, mode=0o755)
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise AdminError(f"cannot read install bundle member: {member.name}")
                with extracted:
                    payload = extracted.read(member.size + 1)
                if len(payload) != member.size:
                    raise AdminError(f"install bundle member size changed: {member.name}")
                mode = 0o755 if stat.S_IMODE(member.mode) & 0o111 else 0o644
                _atomic_write(target, payload, mode)
    except (OSError, tarfile.TarError) as exc:
        raise AdminError(f"invalid install bundle archive {bundle}: {exc}") from exc

    source = destination / expected_root
    metadata_path = source / "bundle.json"
    metadata = _read_json_object(metadata_path, "install bundle metadata")
    required = {
        "schema": 1,
        "kind": "reticulumpi-install",
        "version": filename_version,
        "architecture": "arm64",
    }
    if any(metadata.get(key) != value for key, value in required.items()):
        raise AdminError("install bundle metadata does not match its filename or ARM64 contract")
    wheel_name = metadata.get("wheel")
    if (
        not isinstance(wheel_name, str)
        or Path(wheel_name).name != wheel_name
        or not wheel_name.endswith(".whl")
    ):
        raise AdminError("install bundle metadata has an invalid wheel basename")
    bundled_wheel = source / wheel_name
    if bundled_wheel.is_symlink() or not bundled_wheel.is_file():
        raise AdminError("install bundle is missing its declared prebuilt wheel")
    version, extracted_source = _source_metadata(source)
    if extracted_source is None or version != filename_version:
        raise AdminError("install bundle source metadata does not match bundle metadata")
    _verify_bundle(source, source)
    return version, source


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AdminError(f"cannot open file for hashing {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise AdminError(f"file for hashing is not regular: {path}")
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
        after = os.fstat(descriptor)
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
            raise AdminError(f"file changed while hashing: {path}")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _snapshot_regular_file(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str | None = None,
    mode: int = 0o600,
) -> str:
    """Copy one untrusted external file through stable no-follow descriptors."""

    _ensure_real_directory(destination.parent, mode=0o700)
    if os.path.lexists(destination):
        raise AdminError(f"private snapshot destination already exists: {destination}")
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    source_fd: int | None = None
    destination_fd: int | None = None
    digest = hashlib.sha256()
    try:
        source_fd = os.open(source, source_flags)
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise AdminError(f"snapshot source is not a regular file: {source}")
        destination_fd = os.open(destination, destination_flags, mode)
        os.fchmod(destination_fd, mode)
        while block := os.read(source_fd, 1024 * 1024):
            digest.update(block)
            view = memoryview(block)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise AdminError(f"short write while snapshotting {source}")
                view = view[written:]
        os.fsync(destination_fd)
        after = os.fstat(source_fd)
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
            raise AdminError(f"snapshot source changed while being copied: {source}")
        actual = digest.hexdigest()
        if expected_sha256 is not None and actual != expected_sha256:
            raise AdminError(f"private snapshot checksum mismatch: {source}")
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise AdminError(f"cannot create private snapshot of {source}: {exc}") from exc
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
    _fsync_directory(destination.parent)
    return actual


def _bundle_snapshot_failpoint(label: str, external: Path, snapshot: Path) -> None:
    """Test hook for deterministic external-input replacement attacks."""


def _snapshot_signed_metadata(external: Path, destination: Path) -> dict[str, str] | None:
    if _unsigned_development_mode():
        return None
    _ensure_real_directory(destination, mode=0o700)
    manifest = destination / BUNDLE_MANIFEST_NAME
    signature = destination / BUNDLE_SIGNATURE_NAME
    _snapshot_regular_file(external / BUNDLE_MANIFEST_NAME, manifest)
    _snapshot_regular_file(external / BUNDLE_SIGNATURE_NAME, signature)
    _verify_minisign(manifest, signature)
    expected = _read_hash_manifest(manifest)
    _bundle_snapshot_failpoint("after-manifest-verification", external, destination)
    return expected


def _snapshot_source_tree(
    source: Path,
    destination: Path,
    ignored: frozenset[Path],
) -> None:
    """Copy an untrusted source tree into a normalized private root-only tree."""

    entries = _tree_entries(source, ignored)
    if not entries:
        raise AdminError(f"source bundle does not exist: {source}")
    destination.mkdir(mode=0o700)
    expected_files: dict[Path, str] = {}
    expected_directories: set[Path] = {Path(".")}
    try:
        for relative, entry_stat in entries:
            if relative == Path("."):
                continue
            target = destination / relative
            if stat.S_ISDIR(entry_stat.st_mode):
                target.mkdir(mode=0o700)
                expected_directories.add(relative)
            else:
                expected_files[relative] = _snapshot_regular_file(
                    source / relative,
                    target,
                    mode=0o600,
                )
        actual_entries = _tree_entries(destination)
        actual_directories = {
            relative for relative, entry_stat in actual_entries if stat.S_ISDIR(entry_stat.st_mode)
        }
        actual_files = {
            relative: _sha256(destination / relative)
            for relative, entry_stat in actual_entries
            if stat.S_ISREG(entry_stat.st_mode)
        }
        if actual_directories != expected_directories or actual_files != expected_files:
            raise AdminError(f"private source snapshot verification failed: {source}")
        _fsync_state_directory(destination)
        _fsync_directory(destination.parent)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _snapshot_source_bundle(bundle: Path, workspace: Path) -> Path:
    metadata = workspace / "metadata"
    expected = _snapshot_signed_metadata(bundle, metadata)
    ignored = frozenset({Path(BUNDLE_MANIFEST_NAME), Path(BUNDLE_SIGNATURE_NAME)})
    snapshot = workspace / "source"
    _snapshot_source_tree(bundle, snapshot, ignored)
    if expected is not None:
        _snapshot_regular_file(
            metadata / BUNDLE_MANIFEST_NAME,
            snapshot / BUNDLE_MANIFEST_NAME,
            expected_sha256=_sha256(metadata / BUNDLE_MANIFEST_NAME),
        )
        _snapshot_regular_file(
            metadata / BUNDLE_SIGNATURE_NAME,
            snapshot / BUNDLE_SIGNATURE_NAME,
            expected_sha256=_sha256(metadata / BUNDLE_SIGNATURE_NAME),
        )
    _verify_bundle(snapshot, snapshot)
    _bundle_snapshot_failpoint("after-payload-verification", bundle, snapshot)
    return snapshot


def _snapshot_wheel_bundle(bundle: Path, workspace: Path) -> Path:
    directory = bundle.parent
    expected = _snapshot_signed_metadata(directory, workspace)
    scheme_entries = (
        expected if expected is not None else _unsigned_dependency_profile_entries(directory)
    )
    scheme = _dependency_profile_scheme(scheme_entries)
    snapshot = workspace / bundle.name
    wheel_digest = expected.get(bundle.name) if expected is not None else None
    if expected is not None and wheel_digest is None:
        raise AdminError(f"signed hash manifest does not contain bundle: {bundle.name}")
    _snapshot_regular_file(bundle, snapshot, expected_sha256=wheel_digest)
    constraints = workspace / "constraints"
    profiles = _dependency_profiles_for_scheme(scheme) if scheme is not None else {}
    for profile_name in profiles.values():
        relative = Path("constraints") / profile_name
        digest = expected.get(relative.as_posix()) if expected is not None else None
        external_profile = directory / relative
        if digest is None and expected is not None:
            continue
        if external_profile.is_file() and not external_profile.is_symlink():
            _snapshot_regular_file(
                external_profile,
                constraints / profile_name,
                expected_sha256=digest,
            )
    _verify_bundle(snapshot, None)
    _bundle_snapshot_failpoint("after-payload-verification", bundle, snapshot)
    return snapshot


@contextlib.contextmanager
def _materialize_install_bundle(bundle: Path) -> Iterator[Path]:
    """Yield only a verified private snapshot; never yield the external input path."""

    try:
        bundle_stat = bundle.lstat()
    except OSError as exc:
        if _install_archive_version(bundle) is not None:
            raise AdminError(f"install bundle archive is missing or unsafe: {bundle}") from exc
        raise AdminError(f"install bundle is missing or unsafe: {bundle}") from exc
    if stat.S_ISLNK(bundle_stat.st_mode):
        raise AdminError(f"install bundle may not be a symlink: {bundle}")
    temporary_root = Path(tempfile.gettempdir()).resolve()
    with tempfile.TemporaryDirectory(
        prefix="reticulumpi-input-snapshot-",
        dir=temporary_root,
    ) as raw:
        workspace = Path(raw)
        workspace.chmod(0o700)
        workspace_stat = workspace.lstat()
        if workspace_stat.st_uid != os.geteuid() or stat.S_IMODE(workspace_stat.st_mode) & 0o077:
            raise AdminError(f"private bundle snapshot directory is unsafe: {workspace}")
        if stat.S_ISDIR(bundle_stat.st_mode):
            yield _snapshot_source_bundle(bundle, workspace)
            return
        if not stat.S_ISREG(bundle_stat.st_mode):
            raise AdminError(f"install bundle is not a regular file or directory: {bundle}")
        if _install_archive_version(bundle) is None:
            yield _snapshot_wheel_bundle(bundle, workspace)
            return
        staged_archive = _snapshot_wheel_bundle(bundle, workspace)
        extracted = workspace / "extracted"
        extracted.mkdir(mode=0o700)
        _version, source = _extract_install_archive(staged_archive, extracted)
        yield source


def _verify_bundle(bundle: Path, source: Path | None) -> None:
    if source is not None:
        required_files = ["pyproject.toml"]
        required_directories = ["src", "systemd", "config", "scripts"]
        missing = [
            name
            for name in required_files
            if (source / name).is_symlink() or not (source / name).is_file()
        ]
        missing.extend(
            name
            for name in required_directories
            if (source / name).is_symlink() or not (source / name).is_dir()
        )
        if missing:
            raise AdminError(f"source bundle is incomplete: {', '.join(missing)}")

    if _unsigned_development_mode():
        print(
            "WARNING: accepting an unsigned bundle for a non-root development dry run only",
            file=sys.stderr,
        )
        return
    _verify_signed_bundle(bundle, source)


def _unsigned_development_mode() -> bool:
    value = os.environ.get(UNSIGNED_DEV_ENV)
    if value is None:
        return False
    if value != "1":
        raise AdminError(f"{UNSIGNED_DEV_ENV} must be exactly 1 when used")
    if os.geteuid() == 0:
        raise AdminError("unsigned development bundles are forbidden when running as root")
    return True


def _trusted_release_public_key() -> Path:
    path = RELEASE_PUBLIC_KEY_FILE
    try:
        key_stat = path.lstat()
    except OSError as exc:
        raise AdminError(f"trusted release public key is unavailable: {path}: {exc}") from exc
    if stat.S_ISLNK(key_stat.st_mode) or not stat.S_ISREG(key_stat.st_mode):
        raise AdminError(f"trusted release public key is not a regular file: {path}")
    if key_stat.st_uid != 0 or stat.S_IMODE(key_stat.st_mode) & 0o022:
        raise AdminError(f"trusted release public key ownership or permissions are unsafe: {path}")
    return path


def _verify_minisign(manifest: Path, signature: Path) -> None:
    for path, label in ((manifest, "hash manifest"), (signature, "Minisign signature")):
        if path.is_symlink() or not path.is_file():
            raise AdminError(f"bundle {label} is missing or unsafe: {path}")
    public_key = _trusted_release_public_key()
    try:
        subprocess.run(
            [
                MINISIGN,
                "-Vm",
                str(manifest),
                "-x",
                str(signature),
                "-p",
                str(public_key),
            ],
            check=True,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env={"LANG": "C", "PATH": "/usr/bin:/bin"},
        )
    except OSError as exc:
        raise AdminError(f"cannot execute trusted Minisign verifier {MINISIGN}: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "signature verification failed").strip()
        raise AdminError(f"bundle Minisign verification failed: {detail}") from exc


def _read_hash_manifest(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise AdminError(f"cannot read bundle hash manifest {path}: {exc}") from exc
    entries: dict[str, str] = {}
    for line_number, line in enumerate(lines, 1):
        match = re.fullmatch(r"([0-9a-fA-F]{64}) ([ *])(.+)", line)
        if match is None:
            raise AdminError(f"invalid hash manifest line {line_number} in {path}")
        digest, _marker, raw_name = match.groups()
        relative = PurePosixPath(raw_name)
        if (
            raw_name != relative.as_posix()
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or "\\" in raw_name
        ):
            raise AdminError(f"unsafe hash manifest path at line {line_number}: {raw_name!r}")
        if raw_name in entries:
            raise AdminError(f"duplicate hash manifest path: {raw_name}")
        entries[raw_name] = digest.lower()
    if not entries:
        raise AdminError(f"bundle hash manifest is empty: {path}")
    return entries


def _verify_signed_bundle(bundle: Path, source: Path | None) -> None:
    directory = source if source is not None else bundle.parent
    manifest = directory / BUNDLE_MANIFEST_NAME
    signature = directory / BUNDLE_SIGNATURE_NAME
    _verify_minisign(manifest, signature)
    expected = _read_hash_manifest(manifest)
    _dependency_profile_scheme(expected)

    if source is None:
        digest = expected.get(bundle.name)
        if digest is None:
            raise AdminError(f"signed hash manifest does not contain bundle: {bundle.name}")
        if _sha256(bundle) != digest:
            raise AdminError(f"signed bundle checksum mismatch: {bundle}")
        return

    actual: dict[str, str] = {}
    excluded = {BUNDLE_MANIFEST_NAME, BUNDLE_SIGNATURE_NAME}
    for relative, entry_stat in _tree_entries(source):
        if relative == Path(".") or stat.S_ISDIR(entry_stat.st_mode):
            continue
        name = relative.as_posix()
        if name not in excluded:
            actual[name] = _hash_regular_file(source / relative)
    expected_source = {name: digest for name, digest in expected.items() if name not in excluded}
    if actual.keys() != expected_source.keys():
        missing = sorted(actual.keys() - expected_source.keys())
        unexpected = sorted(expected_source.keys() - actual.keys())
        raise AdminError(
            "signed source manifest does not exactly match bundle files: "
            f"unlisted={missing}; missing={unexpected}"
        )
    mismatched = sorted(name for name, digest in actual.items() if expected_source[name] != digest)
    if mismatched:
        raise AdminError("signed source bundle checksum mismatch: " + ", ".join(mismatched))


def _dependency_profile_name(features: tuple[str, ...]) -> str:
    """Select the smallest complete, hash-locked production dependency set."""

    selected = set(features) & _PACKAGE_FEATURES
    if not selected:
        return "core"
    if selected <= {"dashboard", "nomadnet"}:
        return "dashboard-nomadnet"
    return "all-features"


def _dependency_profile_scheme(entries: dict[str, str]) -> str | None:
    """Return the one coherent dependency-filename scheme declared by *entries*."""

    schemes: set[str] = set()
    for profile_name in _DEPENDENCY_PROFILES:
        canonical = (Path("constraints") / _DEPENDENCY_PROFILES[profile_name]).as_posix()
        legacy = (Path("constraints") / _LEGACY_DEPENDENCY_PROFILES[profile_name]).as_posix()
        if canonical in entries and legacy in entries:
            raise AdminError(
                "bundle declares ambiguous canonical and legacy dependency profiles: "
                f"{profile_name}"
            )
        if canonical in entries:
            schemes.add("canonical")
        if legacy in entries:
            schemes.add("legacy")
    if len(schemes) > 1:
        raise AdminError("bundle mixes canonical and legacy dependency profile filenames")
    return next(iter(schemes), None)


def _unsigned_dependency_profile_entries(directory: Path) -> dict[str, str]:
    """Inventory only recognized dependency aliases in an unsigned development input."""

    entries: dict[str, str] = {}
    for profiles in (_DEPENDENCY_PROFILES, _LEGACY_DEPENDENCY_PROFILES):
        for filename in profiles.values():
            relative = Path("constraints") / filename
            path = directory / relative
            if not os.path.lexists(path):
                continue
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise AdminError(f"cannot inspect dependency profile {path}: {exc}") from exc
            if not stat.S_ISREG(metadata.st_mode):
                raise AdminError(f"dependency profile path is unsafe: {path}")
            entries[relative.as_posix()] = "unsigned"
    return entries


def _dependency_profiles_for_scheme(scheme: str) -> dict[str, str]:
    if scheme == "canonical":
        return _DEPENDENCY_PROFILES
    if scheme == "legacy":
        return _LEGACY_DEPENDENCY_PROFILES
    raise AdminError(f"unsupported dependency profile filename scheme: {scheme}")


def _signed_release_wheel_digest(bundle: Path, source: Path | None) -> str | None:
    """Return the signed wheel digest for an immutable release bundle.

    A same-version apply is a safe no-op only when the signed wheel is exactly
    the artifact recorded in ``install.json``.  Legacy source trees without a
    prebuilt-wheel manifest deliberately return ``None`` and retain the
    existing fail-closed reinstall behavior.
    """

    directory = source if source is not None else bundle.parent
    manifest = _read_hash_manifest(directory / BUNDLE_MANIFEST_NAME)
    if source is None:
        return manifest.get(bundle.name)
    metadata_path = source / "bundle.json"
    if metadata_path.is_symlink() or not metadata_path.is_file():
        return None
    metadata = _read_json_object(metadata_path, "install bundle metadata")
    wheel_name = metadata.get("wheel")
    if (
        metadata.get("kind") != "reticulumpi-install"
        or not isinstance(wheel_name, str)
        or Path(wheel_name).name != wheel_name
        or not wheel_name.endswith(".whl")
    ):
        return None
    return manifest.get(wheel_name)


def _dependency_profile_path(
    bundle: Path,
    source: Path | None,
    features: tuple[str, ...],
) -> Path:
    profile_name = _dependency_profile_name(features)
    directory = source if source is not None else bundle.parent
    expected: dict[str, str] | None = None
    if _unsigned_development_mode():
        scheme_entries = _unsigned_dependency_profile_entries(directory)
    else:
        expected = _read_hash_manifest(directory / BUNDLE_MANIFEST_NAME)
        scheme_entries = expected
    scheme = _dependency_profile_scheme(scheme_entries)
    if scheme is None:
        relative = Path("constraints") / _DEPENDENCY_PROFILES[profile_name]
        raise AdminError(
            f"bundle is missing the {profile_name} hash-locked dependency profile: {relative}"
        )
    relative = Path("constraints") / _dependency_profiles_for_scheme(scheme)[profile_name]
    profile = directory / relative
    if profile.is_symlink() or not profile.is_file():
        raise AdminError(
            f"bundle is missing the {profile_name} hash-locked dependency profile: {relative}"
        )
    try:
        content = profile.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AdminError(f"cannot read dependency profile {profile}: {exc}") from exc
    if "--hash=sha256:" not in content:
        raise AdminError(f"dependency profile is not hash locked: {profile}")
    forbidden = (
        "--editable",
        "-e ",
        "--index-url",
        "--extra-index-url",
        "--find-links",
        "--trusted-host",
        "git+",
        "http://",
        "https://",
    )
    if any(token in content for token in forbidden):
        raise AdminError(f"dependency profile contains a forbidden external source: {profile}")

    # Source verification is exact, but checking the selected entry explicitly
    # also protects wheel bundles, whose sibling directory may contain unrelated
    # unsigned files.
    if expected is not None:
        key = relative.as_posix()
        digest = expected.get(key)
        if digest is None:
            raise AdminError(f"signed hash manifest does not contain dependency profile: {key}")
        if _sha256(profile) != digest:
            raise AdminError(f"signed dependency profile checksum mismatch: {profile}")
    return profile


def _stage_dependency_profile(profile: Path, staging: Path) -> Path:
    """Copy a verified profile into root-owned staging and recheck its digest."""

    destination = staging / profile.name
    expected = _sha256(profile)
    try:
        _snapshot_regular_file(profile, destination, expected_sha256=expected, mode=0o644)
    except AdminError as exc:
        raise AdminError(f"staged dependency profile checksum mismatch: {profile}") from exc
    return destination


def _stage_verified_source(source: Path, staging: Path) -> Path:
    candidate = staging / "verified-source"
    _copy_tree_verified(source, candidate)
    _verify_bundle(candidate, candidate)
    return candidate


def _validate_bundle_location(bundle: Path, source: Path | None, root: Path) -> None:
    if source is None:
        return
    source = source.resolve()
    if source == root or _is_within(source, root) or _is_within(root, source):
        raise AdminError("source bundle and production install root must be separate")


def _validate_wheel(wheel: Path, version: str, features: tuple[str, ...]) -> str:
    """Validate built distribution identity and required runtime assets."""
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            if any(
                name.startswith("/") or ".." in Path(name).parts or "\\" in name for name in names
            ):
                raise AdminError(f"wheel contains an unsafe member path: {wheel}")
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1:
                raise AdminError("wheel must contain exactly one METADATA file")
            metadata = email.parser.Parser().parsestr(
                archive.read(metadata_names[0]).decode("utf-8")
            )
            if metadata.get("Name", "").lower() != "reticulumpi":
                raise AdminError("built wheel project name is not reticulumpi")
            if metadata.get("Version") != version:
                raise AdminError(
                    f"built wheel version {metadata.get('Version')!r} does not match {version}"
                )
            if "dashboard" in features:
                required = {
                    "reticulumpi/builtin_plugins/web_dashboard/static/index.html",
                    "reticulumpi/builtin_plugins/web_dashboard/static/style.css",
                    "reticulumpi/builtin_plugins/web_dashboard/static/sw.js",
                }
                missing = required - set(names)
                if missing:
                    raise AdminError(
                        "dashboard wheel is missing assets: " + ", ".join(sorted(missing))
                    )
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise AdminError(f"invalid wheel {wheel}: {exc}") from exc
    return _sha256(wheel)


def _require_unchanged_digest(path: Path, expected: str, label: str) -> None:
    if _sha256(path) != expected:
        raise AdminError(f"{label} changed after private snapshot verification: {path}")


def _service_active(name: str) -> bool:
    try:
        return (
            subprocess.run(
                [SYSTEMCTL, "is-active", "--quiet", name],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
    except OSError:
        return False


def _unit_enabled(name: str) -> bool:
    try:
        return (
            subprocess.run(
                [SYSTEMCTL, "is-enabled", "--quiet", name],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
    except OSError:
        return False


def _wait_service_active(name: str, timeout: float = 120.0, stable_for: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    stable_since: float | None = None
    while time.monotonic() < deadline:
        if _service_active(name):
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= stable_for:
                return
        else:
            stable_since = None
        time.sleep(0.2)
    raise AdminError(f"{name} did not remain active within {timeout:.0f} seconds")


def _wait_service_inactive(name: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _service_active(name):
            return
        time.sleep(0.2)
    raise AdminError(f"{name} did not stop within {timeout:.0f} seconds")


def _restart_and_wait(name: str) -> None:
    _run([SYSTEMCTL, "restart", name])
    _wait_service_active(name)


def _service_account():
    try:
        return pwd.getpwnam(SERVICE_USER)
    except KeyError as exc:
        raise AdminError(f"service user {SERVICE_USER!r} does not exist") from exc


def _installed_service_fragments() -> tuple[Path, ...]:
    """Return installed unit fragments without consulting a checkout."""

    candidates = [SYSTEMD_DIR / "reticulumpi.service"]
    dropin = SYSTEMD_DIR / "reticulumpi.service.d"
    if dropin.exists():
        if dropin.is_symlink() or not dropin.is_dir():
            raise AdminError(f"installed service drop-in directory is unsafe: {dropin}")
        candidates.extend(sorted(dropin.glob("*.conf")))
    fragments: list[Path] = []
    for path in candidates:
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise AdminError(f"installed service definition is unsafe: {path}")
        fragments.append(path)
    return tuple(fragments)


def _absolute_unit_value(value: str) -> Path | None:
    expanded = os.path.expandvars(value)
    if expanded.startswith("~"):
        return None
    path = Path(expanded)
    return path if path.is_absolute() else None


def _discover_legacy_layout() -> LegacyLayout:
    """Inspect unit Environment/ExecStart/WorkingDirectory layout evidence."""

    try:
        account_home = Path(pwd.getpwnam(SERVICE_USER).pw_dir).expanduser()
    except KeyError:
        account_home = Path(f"/home/{SERVICE_USER}")
    if not account_home.is_absolute():
        raise AdminError(f"service user has an invalid home directory: {account_home}")
    homes: list[Path] = []
    install_roots: list[Path] = []
    config_files: list[Path] = []
    evidence: list[str] = []

    def add(values: list[Path], path: Path | None, label: str) -> None:
        if path is None or not path.is_absolute() or path in values:
            return
        values.append(path)
        evidence.append(f"{label}={path}")

    for fragment in _installed_service_fragments():
        try:
            raw_lines = fragment.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise AdminError(
                f"cannot inspect installed service definition {fragment}: {exc}"
            ) from exc
        logical: list[str] = []
        pending = ""
        for raw_line in raw_lines:
            stripped = raw_line.strip()
            if not stripped or stripped.startswith(("#", ";")):
                continue
            pending += stripped
            if pending.endswith("\\"):
                pending = pending[:-1] + " "
                continue
            logical.append(pending)
            pending = ""
        if pending:
            logical.append(pending)
        environment: dict[str, str] = {}
        for line in logical:
            if line.startswith("Environment="):
                try:
                    assignments = shlex.split(line.split("=", 1)[1])
                except ValueError as exc:
                    raise AdminError(f"invalid Environment directive in {fragment}") from exc
                for assignment in assignments:
                    if "=" in assignment:
                        key, value = assignment.split("=", 1)
                        environment[key] = value
            elif line.startswith("WorkingDirectory="):
                add(
                    install_roots,
                    _absolute_unit_value(line.split("=", 1)[1]),
                    "WorkingDirectory",
                )
            elif line.startswith("ExecStart="):
                try:
                    command = shlex.split(line.split("=", 1)[1].lstrip("-@:+!"))
                except ValueError as exc:
                    raise AdminError(f"invalid ExecStart directive in {fragment}") from exc
                for index, token in enumerate(command):
                    if token == "--config" and index + 1 < len(command):
                        add(
                            config_files,
                            _absolute_unit_value(command[index + 1]),
                            "ExecStart --config",
                        )
                    elif token.startswith("--config="):
                        add(
                            config_files,
                            _absolute_unit_value(token.split("=", 1)[1]),
                            "ExecStart --config",
                        )
                if command:
                    executable = _absolute_unit_value(command[0])
                    if executable is not None:
                        parts = executable.parts
                        for marker in (".venv", "venv"):
                            if marker in parts:
                                marker_index = parts.index(marker)
                                add(
                                    install_roots,
                                    Path(*parts[:marker_index]),
                                    "ExecStart root",
                                )
                                break
        add(homes, _absolute_unit_value(environment.get("HOME", "")), "Environment HOME")
        state_root = _absolute_unit_value(environment.get("RETICULUMPI_STATE_DIR", ""))
        add(homes, state_root, "Environment RETICULUMPI_STATE_DIR")
        xdg_config = _absolute_unit_value(environment.get("XDG_CONFIG_HOME", ""))
        if xdg_config is not None and xdg_config.name == ".config":
            add(homes, xdg_config.parent, "Environment XDG_CONFIG_HOME")
        xdg_data = _absolute_unit_value(environment.get("XDG_DATA_HOME", ""))
        if xdg_data is not None and xdg_data.name == "share" and xdg_data.parent.name == ".local":
            add(homes, xdg_data.parent.parent, "Environment XDG_DATA_HOME")
        rns_config = _absolute_unit_value(environment.get("RETICULUMPI_RNS_CONFIG_DIR", ""))
        if rns_config is not None and rns_config.name == ".reticulum":
            add(homes, rns_config.parent, "Environment RETICULUMPI_RNS_CONFIG_DIR")

    # An older unit may only reveal its checkout through ExecStart.  Treat an
    # install root as a home candidate only when it actually contains known
    # durable state; this avoids copying arbitrary release trees.
    for install_root in install_roots:
        if any(
            (install_root / relative).exists()
            for relative in (".reticulum", ".config/reticulumpi", ".nomadnet")
        ):
            add(homes, install_root, "ExecStart state root")
    add(homes, account_home, "service account fallback")
    return LegacyLayout(tuple(homes), tuple(install_roots), tuple(config_files), tuple(evidence))


def _legacy_home_candidates() -> tuple[Path, ...]:
    return _discover_legacy_layout().homes


def _legacy_meshchat_storage_candidates() -> tuple[Path, ...]:
    found: list[Path] = []
    for install_root in _discover_legacy_layout().install_roots:
        # A rendered immutable unit executes through ``<root>/current``.  That
        # pointer is release plumbing, never a mutable predecessor root.  Direct
        # release paths are excluded for the same reason.  Requiring the storage
        # entry to exist also prevents an ordinary managed unit from inventing a
        # nonexistent legacy root that later fails symlink validation.
        if (install_root.name == "current" and install_root.is_symlink()) or (
            install_root.parent.name == "releases"
        ):
            continue
        candidate = install_root / "meshchat/storage"
        if not os.path.lexists(candidate):
            continue
        if candidate.resolve() == (DATA_DIR / "meshchat/storage").resolve():
            continue
        if candidate not in found:
            found.append(candidate)
    return tuple(found)


def _validate_external_config_path(raw_path: Path) -> Path:
    """Constrain a unit-authoritative config without requiring a root-owned home."""

    requested = raw_path.expanduser()
    if not requested.is_absolute() or len(requested.parts) < 3:
        raise AdminError("external configuration-file path is unsafe")
    _reject_symlink_components(requested)
    path = requested.resolve()
    forbidden_roots = (
        Path("/dev"),
        Path("/proc"),
        Path("/run"),
        Path("/sys"),
        BACKUP_DIR.resolve(),
        LIBEXEC_DIR.resolve(),
        SUDOERS_DIR.resolve(),
        SYSTEMD_DIR.resolve(),
    )
    if any(path == root or _is_within(path, root) for root in forbidden_roots):
        raise AdminError(f"external configuration-file path overlaps a protected root: {path}")
    if path in {MANIFEST_FILE.resolve(), JOURNAL_FILE.resolve(), LOCK_FILE.resolve()}:
        raise AdminError(f"external configuration-file path overlaps administration state: {path}")
    return path


def _discover_legacy_config_source() -> LegacyConfigSource | None:
    """Resolve the configuration actually named by the installed service.

    Multiple unit fragments can repeat the same effective path.  Distinct
    surviving paths are ambiguous because this small parser does not attempt to
    reproduce all of systemd's directive-reset semantics, so fail closed.  A
    pre-existing canonical file is not authoritative when the installed unit
    explicitly names a different file.
    """

    if os.path.lexists(CONFIG_FILE):
        if CONFIG_FILE.is_symlink() or not CONFIG_FILE.is_file():
            raise AdminError(f"configuration may not be a symlink or special file: {CONFIG_FILE}")
    found: dict[Path, str] = {}
    configured = _discover_legacy_layout().config_files
    for candidate in configured:
        if not os.path.lexists(candidate):
            raise AdminError(f"installed legacy configuration is missing: {candidate}")
        if candidate.is_symlink() or not candidate.is_file():
            raise AdminError(f"installed legacy configuration is unsafe: {candidate}")
        resolved = _validate_external_config_path(candidate)
        found[resolved] = _sha256(candidate)
    if not found:
        return None
    if len(found) != 1:
        raise AdminError(
            "installed service definitions expose multiple legacy configurations: "
            + ", ".join(str(path) for path in sorted(found, key=str))
        )
    path, digest = next(iter(found.items()))
    if path == CONFIG_FILE.resolve():
        return None
    return LegacyConfigSource(path, digest)


def _readiness_path() -> Path:
    return RUN_DIR / "ready"


def _dashboard_readiness_path() -> Path:
    return RUN_DIR / _DASHBOARD_READY_FILE


def _clear_application_readiness() -> None:
    marker = _readiness_path()
    _ensure_real_directory(marker.parent, mode=None)
    if marker.is_symlink():
        raise AdminError(f"application readiness marker may not be a symlink: {marker}")
    marker.unlink(missing_ok=True)
    _fsync_directory(marker.parent)


def _clear_dashboard_readiness() -> None:
    marker = _dashboard_readiness_path()
    _ensure_real_directory(marker.parent, mode=None)
    if marker.is_symlink():
        raise AdminError(f"dashboard readiness marker may not be a symlink: {marker}")
    marker.unlink(missing_ok=True)
    _fsync_directory(marker.parent)


def _owned_readiness_marker_valid(marker: Path, label: str) -> bool:
    try:
        marker_stat = marker.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(marker_stat.st_mode) or not stat.S_ISREG(marker_stat.st_mode):
        raise AdminError(f"{label} readiness marker is unsafe: {marker}")
    account = _service_account()
    if marker_stat.st_uid != account.pw_uid:
        raise AdminError(f"{label} readiness marker has the wrong owner: {marker}")
    if stat.S_IMODE(marker_stat.st_mode) & 0o022:
        raise AdminError(f"{label} readiness marker is writable by group/other: {marker}")
    try:
        content = marker.read_bytes()
    except OSError as exc:
        raise AdminError(f"cannot read {label} readiness marker {marker}: {exc}") from exc
    if content != b"ready\n":
        raise AdminError(f"{label} readiness marker has invalid content: {marker}")
    return True


def _readiness_marker_valid() -> bool:
    return _owned_readiness_marker_valid(_readiness_path(), "application")


def _dashboard_readiness_marker_valid() -> bool:
    return _owned_readiness_marker_valid(_dashboard_readiness_path(), "dashboard")


def _wait_application_ready(timeout: float = 120.0, stable_for: float = 2.0) -> None:
    """Require both systemd activity and the application's fresh marker."""

    deadline = time.monotonic() + timeout
    stable_since: float | None = None
    while time.monotonic() < deadline:
        if _service_active("reticulumpi.service") and _readiness_marker_valid():
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= stable_for:
                return
        else:
            stable_since = None
        time.sleep(0.2)
    raise AdminError(
        f"reticulumpi.service did not create a fresh readiness marker within {timeout:.0f} seconds"
    )


def _wait_dashboard_ready(timeout: float = 120.0, stable_for: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    stable_since: float | None = None
    while time.monotonic() < deadline:
        if _service_active("reticulumpi.service") and _dashboard_readiness_marker_valid():
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= stable_for:
                return
        else:
            stable_since = None
        time.sleep(0.2)
    raise AdminError(
        f"dashboard did not create a fresh readiness marker within {timeout:.0f} seconds"
    )


def _activate_application(action: str) -> None:
    if action not in {"start", "restart"}:
        raise AdminError(f"unsupported application activation action: {action}")
    _clear_application_readiness()
    _run([SYSTEMCTL, action, "reticulumpi.service"])
    _wait_application_ready()


def _activate_required_features(action: str, features: tuple[str, ...]) -> None:
    if "dashboard" in features:
        _clear_dashboard_readiness()
    _activate_application(action)
    if "dashboard" in features:
        _wait_dashboard_ready()


def _activate_and_verify_identities(
    action: str,
    expected: dict[str, str],
    roots: tuple[StateRoot, ...],
    features: tuple[str, ...] = (),
) -> dict[str, str]:
    _activate_required_features(action, features)
    actual = _identity_hashes(roots)
    _verify_identity_continuity(expected, actual)
    return actual


def _activate_legacy_and_verify_identities(
    expected: dict[str, str],
    roots: tuple[StateRoot, ...],
    config_file: Path | None,
) -> dict[str, str]:
    _run([SYSTEMCTL, "start", "reticulumpi.service"])
    _wait_service_active("reticulumpi.service")
    actual = _identity_hashes(roots, config_file)
    _verify_identity_continuity(expected, actual)
    return actual


def _verify_sqlite(path: Path) -> None:
    try:
        with contextlib.closing(sqlite3.connect(path)) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        raise AdminError(f"SQLite validation failed for {path}: {exc}") from exc
    if not result or result[0] != "ok":
        raise AdminError(f"SQLite integrity check failed for {path}: {result}")


def _sqlite_backup_file(source: Path, destination: Path) -> None:
    _ensure_real_directory(destination.parent)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        with (
            contextlib.closing(sqlite3.connect(source)) as source_connection,
            contextlib.closing(sqlite3.connect(temporary)) as backup_connection,
        ):
            source_connection.backup(backup_connection)
        _verify_sqlite(temporary)
        temporary.chmod(0o600)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except sqlite3.Error as exc:
        raise AdminError(f"could not back up SQLite database {source}: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _state_roots(features: tuple[str, ...]) -> tuple[StateRoot, ...]:
    roots = [
        StateRoot("etc", CONFIG_DIR),
        StateRoot("data", DATA_DIR),
    ]
    covered = [CONFIG_DIR.resolve(), DATA_DIR.resolve()]
    for index, legacy_home in enumerate(_legacy_home_candidates(), 1):
        prefix = "legacy-home" if index == 1 else f"legacy-layout-{index}"
        candidates = [
            StateRoot(f"{prefix}-reticulum", legacy_home / ".reticulum"),
            StateRoot(f"{prefix}-config", legacy_home / ".config/reticulumpi"),
            StateRoot(f"{prefix}-data", legacy_home / ".local/share/reticulumpi"),
        ]
        if "nomadnet" in features:
            candidates.extend(
                (
                    StateRoot(f"{prefix}-nomadnet", legacy_home / ".nomadnet"),
                    StateRoot(f"{prefix}-nomadnet-tui", legacy_home / ".nomadnet-tui"),
                )
            )
        for candidate in candidates:
            resolved = candidate.path.resolve()
            # One atomic directory swap must never contain another swap's
            # staging path.  Some installed units legitimately expose the
            # canonical DATA_DIR as a historical service home; in that case
            # every XDG/Reticulum candidate is already covered by the data
            # snapshot and must not become a second, nested state root.
            if any(
                _is_within(resolved, existing) or _is_within(existing, resolved)
                for existing in covered
            ):
                continue
            covered.append(resolved)
            roots.append(candidate)
    for index, storage in enumerate(_legacy_meshchat_storage_candidates(), 1):
        name = "legacy-meshchat-storage" if index == 1 else f"legacy-meshchat-{index}-storage"
        resolved = storage.resolve()
        if any(
            _is_within(resolved, existing) or _is_within(existing, resolved) for existing in covered
        ):
            continue
        covered.append(resolved)
        roots.append(StateRoot(name, storage))
    for root in roots:
        _reject_symlink_components(root.path)
    return tuple(roots)


def _merge_state_roots(
    primary: tuple[StateRoot, ...],
    additional: tuple[StateRoot, ...],
) -> tuple[StateRoot, ...]:
    """Add exact rollback roots without introducing nested atomic swaps."""

    merged = list(primary)
    covered = [root.path.resolve() for root in primary]
    names = {root.name for root in primary}
    for root in additional:
        resolved = root.path.resolve()
        if any(
            _is_within(resolved, existing) or _is_within(existing, resolved) for existing in covered
        ):
            continue
        if root.name in names:
            raise AdminError(f"state-root name refers to multiple exact paths: {root.name}")
        _reject_symlink_components(root.path)
        merged.append(root)
        covered.append(resolved)
        names.add(root.name)
    return tuple(merged)


def _validate_backup_state_root(name: object, raw_path: object) -> StateRoot:
    """Validate one persisted state-root path without rediscovering live units."""

    if not isinstance(name, str) or not isinstance(raw_path, str):
        raise AdminError("backup state-root path evidence is invalid")
    requested = Path(raw_path).expanduser()
    if not requested.is_absolute():
        raise AdminError(f"backup state-root path is not absolute: {raw_path!r}")
    _reject_symlink_components(requested)
    path = requested.resolve()
    if name == "etc":
        if path != CONFIG_DIR.resolve():
            raise AdminError("backup canonical configuration root does not match this system")
    elif name == "data":
        if path != DATA_DIR.resolve():
            raise AdminError("backup canonical data root does not match this system")
    else:
        suffixes = {
            "meshchat-storage": Path("meshchat/storage"),
            "reticulum": Path(".reticulum"),
            "config": Path(".config/reticulumpi"),
            "data": Path(".local/share/reticulumpi"),
            "nomadnet": Path(".nomadnet"),
            "nomadnet-tui": Path(".nomadnet-tui"),
        }
        matched_suffix = next(
            (suffix for label, suffix in suffixes.items() if name.endswith(f"-{label}")),
            None,
        )
        if matched_suffix is None or len(path.parts) <= len(matched_suffix.parts):
            raise AdminError(f"backup contains an invalid legacy state root: {name!r}")
        if Path(*path.parts[-len(matched_suffix.parts) :]) != matched_suffix:
            raise AdminError(f"backup legacy state-root path does not match its name: {name}")
    if path in _UNSAFE_ROOTS:
        raise AdminError(f"backup state-root path is unsafe: {path}")
    return StateRoot(name, path)


def _validate_backup_root_evidence(raw: object) -> tuple[dict[str, str], ...]:
    if not isinstance(raw, (list, tuple)):
        raise AdminError("legacy bridge root evidence must be a list")
    validated: list[dict[str, str]] = []
    seen_names: set[str] = set()
    seen_paths: list[Path] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"name", "path"}:
            raise AdminError("legacy bridge root evidence is invalid")
        root = _validate_backup_state_root(item.get("name"), item.get("path"))
        resolved = root.path.resolve()
        if root.name in seen_names or any(
            _is_within(resolved, existing) or _is_within(existing, resolved)
            for existing in seen_paths
        ):
            raise AdminError("legacy bridge root evidence contains duplicates or overlaps")
        seen_names.add(root.name)
        seen_paths.append(resolved)
        validated.append({"name": root.name, "path": str(resolved)})
    if not {"etc", "data"}.issubset(seen_names):
        raise AdminError("legacy bridge root evidence omits canonical state roots")
    return tuple(validated)


def _backup_roots_from_metadata(metadata: dict[str, object]) -> tuple[StateRoot, ...]:
    raw_records = metadata.get("state_roots")
    if not isinstance(raw_records, list):
        raise AdminError("backup state-root records are missing")
    evidence = [
        {"name": record.get("name"), "path": record.get("path")}
        for record in raw_records
        if isinstance(record, dict) and "path" in record
    ]
    if len(evidence) != len(raw_records):
        # Older schema-2 backups lacked paths and must use installed discovery.
        raw_features = metadata.get("features", [])
        if not isinstance(raw_features, list) or not all(
            isinstance(feature, str) for feature in raw_features
        ):
            raise AdminError("backup feature metadata is invalid")
        return _state_roots(tuple(raw_features))
    validated = _validate_backup_root_evidence(evidence)
    return tuple(StateRoot(item["name"], Path(item["path"])) for item in validated)


def _backup_features(metadata: dict[str, object]) -> tuple[str, ...]:
    raw = metadata.get("features", [])
    if not isinstance(raw, list) or not all(isinstance(feature, str) for feature in raw):
        raise AdminError("backup feature metadata is invalid")
    features = tuple(sorted(set(raw)))
    _extras(features)
    return features


def _backup_configuration_file(metadata: dict[str, object]) -> Path | None:
    raw = metadata.get("configuration_file")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise AdminError("backup configuration-file evidence is invalid")
    requested = Path(raw).expanduser()
    if not requested.is_absolute():
        raise AdminError("backup configuration-file path is not absolute")
    path = requested.resolve()
    if path in _UNSAFE_ROOTS:
        raise AdminError("backup configuration-file path is unsafe")
    # The exact legacy path may not exist until its state roots have been
    # restored, and candidate units cannot rediscover it before that switch.
    # The root-owned backup metadata binds the path; callers only read it.
    return path


def _backup_external_configuration_file(metadata: dict[str, object]) -> Path | None:
    """Return the one unit-authoritative external config eligible for file restore."""

    raw = metadata.get("external_configuration_file")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise AdminError("backup external configuration-file evidence is invalid")
    path = _validate_external_config_path(Path(raw))
    if path == CONFIG_FILE.resolve() or path in _UNSAFE_ROOTS:
        raise AdminError("backup external configuration-file path is unsafe")
    return path


def _enabled_legacy_plugins(path: Path) -> set[str]:
    """Extract explicitly enabled top-level plugin blocks from simple YAML."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise AdminError(f"cannot inspect legacy feature configuration {path}: {exc}") from exc
    plugin_header: tuple[int, int] | None = None
    for index, line in enumerate(lines):
        parsed = _yaml_key_line(line, "plugins")
        if parsed is not None and not parsed[1].split("#", 1)[0].strip():
            if plugin_header is not None:
                raise AdminError("legacy configuration contains multiple plugins blocks")
            plugin_header = (index, parsed[0])
    if plugin_header is None:
        return set()
    start, base_indent = plugin_header
    active: list[tuple[int, int, str]] = []
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indentation = len(line) - len(line.lstrip(" "))
        if indentation <= base_indent:
            break
        active.append((index, indentation, line))
    if not active:
        return set()
    plugin_indent = min(item[1] for item in active)
    blocks: list[tuple[str, list[tuple[int, str]]]] = []
    current: tuple[str, list[tuple[int, str]]] | None = None
    for _index, indentation, line in active:
        if indentation == plugin_indent:
            match = re.match(r"^\s*([A-Za-z0-9_-]+)\s*:\s*(?:#.*)?$", line)
            current = (match.group(1), []) if match else None
            if current is not None:
                blocks.append(current)
        elif current is not None:
            current[1].append((indentation, line))
    enabled: set[str] = set()
    for name, children in blocks:
        if not children:
            continue
        direct_indent = min(indentation for indentation, _line in children)
        values = []
        for indentation, line in children:
            if indentation != direct_indent:
                continue
            parsed = _yaml_key_line(line, "enabled")
            if parsed is not None:
                values.append(parsed[1].split("#", 1)[0].strip().lower())
        if len(values) > 1:
            raise AdminError(f"legacy plugin {name!r} has duplicate enabled settings")
        if values == ["true"]:
            enabled.add(name)
    return enabled


def _validate_legacy_bridge_features(
    features: tuple[str, ...],
    config_file: Path | None,
    services: dict[str, dict[str, bool]] | None = None,
) -> None:
    """Require bridge extras implied by active legacy config/unit evidence."""

    required: set[str] = set()
    if config_file is not None:
        plugin_features = {
            "nomadnet_server": "nomadnet",
            "web_dashboard": "dashboard",
            "lora_diagnostics": "lora",
            "sensor_framework": "sensors",
            "meshtastic_gateway": "meshtastic",
            "meshtastic_responder": "meshtastic",
            "lora_link_tester": "meshtastic",
            "meshcore_gateway": "meshcore",
            "meshcore_observer": "meshcore",
            "space_tracker": "space",
            "gps_telemetry": "gps",
            "adsb_radar": "adsb",
            "captive_portal": "captive-portal",
            "ntp_server": "chrony-control",
        }
        required.update(
            plugin_features[name]
            for name in _enabled_legacy_plugins(config_file)
            if name in plugin_features
        )
        try:
            config_text = config_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise AdminError(f"cannot inspect legacy shared-RNS configuration: {exc}") from exc
        if re.search(r"^\s*use_shared_instance\s*:\s*true(?:\s|#|$)", config_text, re.MULTILINE):
            required.add("shared-rnsd")
    sudoers_features = {
        "reticulumpi-offline": "offline-tools",
        "reticulumpi-captive-portal": "captive-portal",
        "reticulumpi-chrony": "chrony-control",
    }
    for name, feature in sudoers_features.items():
        path = SUDOERS_DIR / name
        if path.is_symlink():
            raise AdminError(f"legacy sudoers path may not be a symlink: {path}")
        if path.is_file():
            required.add(feature)
    if services is not None:
        if services["rnsd.service"]["active"] or services["rnsd.service"]["enabled"]:
            if not {"shared-rnsd", "nomadnet"} & set(features):
                required.add("shared-rnsd")
        watchdog = services["rnsd-watchdog.timer"]
        if watchdog["active"] or watchdog["enabled"]:
            required.add("watchdog")
    missing = sorted(required - set(features))
    if missing:
        raise AdminError(
            "legacy bridge requires explicit features matching active production state: "
            + ", ".join(missing)
        )


def _fsync_state_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise AdminError(f"cannot open durable state directory for fsync {path}: {exc}") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise AdminError(f"cannot fsync durable state directory {path}: {exc}") from exc
    finally:
        os.close(descriptor)


def _tree_entries(
    root: Path,
    ignored: frozenset[Path] = frozenset(),
) -> list[tuple[Path, os.stat_result]]:
    """List a regular-file/directory tree without following any symlink."""

    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return []
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise AdminError(f"durable state root is not a real directory: {root}")
    entries: list[tuple[Path, os.stat_result]] = [(Path("."), root_stat)]

    def visit(directory: Path, relative: Path) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise AdminError(f"cannot inspect durable state directory {directory}: {exc}") from exc
        for child in children:
            child_relative = relative / child.name
            if child_relative in ignored:
                continue
            try:
                child_stat = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise AdminError(f"cannot inspect durable state path {child.path}: {exc}") from exc
            if stat.S_ISLNK(child_stat.st_mode):
                raise AdminError(f"durable state may not contain symlinks: {child.path}")
            if stat.S_ISDIR(child_stat.st_mode):
                entries.append((child_relative, child_stat))
                visit(Path(child.path), child_relative)
            elif stat.S_ISREG(child_stat.st_mode):
                entries.append((child_relative, child_stat))
            else:
                raise AdminError(f"durable state contains a special file: {child.path}")

    visit(root, Path("."))
    return entries


def _hash_regular_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AdminError(f"cannot open durable state file {path}: {exc}") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise AdminError(f"durable state file is not regular: {path}")
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
        after = os.fstat(descriptor)
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise AdminError(f"durable state changed while hashing: {path}")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _copy_regular_file(source: Path, destination: Path, source_stat: os.stat_result) -> None:
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, source_flags)
    destination_fd: int | None = None
    source_digest = hashlib.sha256()
    destination_digest = hashlib.sha256()
    try:
        opened_stat = os.fstat(source_fd)
        if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_ino != source_stat.st_ino:
            raise AdminError(f"durable state file changed before copy: {source}")
        destination_fd = os.open(destination, destination_flags, 0o600)
        os.fchown(destination_fd, source_stat.st_uid, source_stat.st_gid)
        os.fchmod(destination_fd, stat.S_IMODE(source_stat.st_mode))
        while block := os.read(source_fd, 1024 * 1024):
            source_digest.update(block)
            destination_digest.update(block)
            view = memoryview(block)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise AdminError(f"short write while copying durable state: {destination}")
                view = view[written:]
        os.utime(
            destination_fd,
            ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
        )
        os.fsync(destination_fd)
        after = os.fstat(source_fd)
        if (opened_stat.st_ino, opened_stat.st_size, opened_stat.st_mtime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise AdminError(f"durable state changed while copying: {source}")
        if source_digest.digest() != destination_digest.digest():
            raise AdminError(f"durable state copy verification failed: {source}")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)


def _tree_manifest(root: Path) -> list[dict[str, object]]:
    manifest: list[dict[str, object]] = []
    for relative, entry_stat in _tree_entries(root):
        value: dict[str, object] = {
            "path": relative.as_posix(),
            "type": "directory" if stat.S_ISDIR(entry_stat.st_mode) else "file",
            "mode": stat.S_IMODE(entry_stat.st_mode),
            "uid": entry_stat.st_uid,
            "gid": entry_stat.st_gid,
        }
        if stat.S_ISREG(entry_stat.st_mode):
            value["size"] = entry_stat.st_size
            value["sha256"] = _hash_regular_file(root / relative)
        manifest.append(value)
    return manifest


def _copy_tree_verified(
    source: Path,
    destination: Path,
    *,
    ignored: frozenset[Path] = frozenset(),
) -> None:
    if os.path.lexists(destination):
        raise AdminError(f"durable state copy destination already exists: {destination}")
    entries = _tree_entries(source, ignored)
    if not entries:
        raise AdminError(f"durable state source does not exist: {source}")
    source_manifest: list[dict[str, object]] = []
    destination.mkdir(mode=0o700)
    directories: list[tuple[Path, os.stat_result]] = []
    try:
        for relative, entry_stat in entries:
            target = destination if relative == Path(".") else destination / relative
            source_path = source if relative == Path(".") else source / relative
            if stat.S_ISDIR(entry_stat.st_mode):
                if relative != Path("."):
                    target.mkdir(mode=0o700)
                directories.append((target, entry_stat))
            else:
                _copy_regular_file(source_path, target, entry_stat)
        for directory, directory_stat in reversed(directories):
            os.chown(directory, directory_stat.st_uid, directory_stat.st_gid)
            directory.chmod(stat.S_IMODE(directory_stat.st_mode))
            os.utime(
                directory,
                ns=(directory_stat.st_atime_ns, directory_stat.st_mtime_ns),
                follow_symlinks=False,
            )
            _fsync_state_directory(directory)
        _fsync_state_directory(destination.parent)
        for relative, entry_stat in entries:
            value: dict[str, object] = {
                "path": relative.as_posix(),
                "type": "directory" if stat.S_ISDIR(entry_stat.st_mode) else "file",
                "mode": stat.S_IMODE(entry_stat.st_mode),
                "uid": entry_stat.st_uid,
                "gid": entry_stat.st_gid,
            }
            if stat.S_ISREG(entry_stat.st_mode):
                value["size"] = entry_stat.st_size
                value["sha256"] = _hash_regular_file(source / relative)
            source_manifest.append(value)
        if _tree_manifest(destination) != source_manifest:
            raise AdminError(f"durable state tree verification failed: {source}")
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _legacy_state_destinations(features: tuple[str, ...]) -> tuple[tuple[Path, Path], ...]:
    mappings: list[tuple[Path, Path]] = []
    seen: set[tuple[Path, Path]] = set()
    for legacy_home in _legacy_home_candidates():
        candidates = [
            (legacy_home / ".reticulum", DATA_DIR / ".reticulum"),
            (legacy_home / ".config/reticulumpi", DATA_DIR / ".config/reticulumpi"),
            (legacy_home / ".local/share/reticulumpi", DATA_DIR / ".local/share/reticulumpi"),
        ]
        if "nomadnet" in features:
            candidates.extend(
                (
                    (legacy_home / ".nomadnet", DATA_DIR / ".nomadnet"),
                    (legacy_home / ".nomadnet-tui", DATA_DIR / ".nomadnet-tui"),
                )
            )
        for source, destination in candidates:
            pair = (source.resolve(), destination.resolve())
            if pair[0] == pair[1] or pair in seen:
                continue
            seen.add(pair)
            mappings.append((source, destination))
    for source in _legacy_meshchat_storage_candidates():
        destination = DATA_DIR / "meshchat/storage"
        pair = (source.resolve(), destination.resolve())
        if pair[0] == pair[1] or pair in seen:
            continue
        seen.add(pair)
        mappings.append((source, destination))
    return tuple(mappings)


def _identity_files(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        relative.as_posix(): _hash_regular_file(root / relative)
        for relative, entry_stat in _tree_entries(root)
        if stat.S_ISREG(entry_stat.st_mode) and relative.name == "identity"
    }


def _merge_tree_atomically(source: Path, destination: Path) -> None:
    """Merge a legacy tree without overwriting conflicting canonical files."""
    if source.is_symlink() or not source.is_dir():
        raise AdminError(f"legacy state source is not a real directory: {source}")
    _ensure_real_directory(destination.parent, mode=None)
    slot = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.legacy-merge-", dir=destination.parent)
    )
    slot.rmdir()
    displaced: Path | None = None
    installed = False
    try:
        if destination.exists():
            if destination.is_symlink() or not destination.is_dir():
                raise AdminError(f"canonical state destination is unsafe: {destination}")
            _copy_tree_verified(destination, slot)
            new_directories: list[tuple[Path, os.stat_result]] = []
            for relative, entry_stat in _tree_entries(source):
                if relative == Path("."):
                    continue
                source_path = source / relative
                target = slot / relative
                if stat.S_ISDIR(entry_stat.st_mode):
                    if os.path.lexists(target):
                        if target.is_symlink() or not target.is_dir():
                            raise AdminError(f"legacy state type conflict: {source_path}")
                    else:
                        target.mkdir(mode=0o700)
                        new_directories.append((target, entry_stat))
                    continue
                if os.path.lexists(target):
                    if target.is_symlink() or not target.is_file():
                        raise AdminError(f"legacy state type conflict: {source_path}")
                    if _hash_regular_file(source_path) != _hash_regular_file(target):
                        raise AdminError(f"legacy and canonical state conflict: {source_path}")
                else:
                    _copy_regular_file(source_path, target, entry_stat)
            for directory, directory_stat in reversed(new_directories):
                os.chown(directory, directory_stat.st_uid, directory_stat.st_gid)
                directory.chmod(stat.S_IMODE(directory_stat.st_mode))
                _fsync_state_directory(directory)
            for relative, entry_stat in _tree_entries(source):
                target = slot if relative == Path(".") else slot / relative
                if stat.S_ISREG(entry_stat.st_mode) and (
                    _hash_regular_file(source / relative) != _hash_regular_file(target)
                ):
                    raise AdminError(f"legacy state merge verification failed: {source / relative}")
            _fsync_state_directory(slot)
        else:
            _copy_tree_verified(source, slot)

        if os.path.lexists(destination):
            displaced = destination.parent / (
                f".{destination.name}.pre-legacy-{os.getpid()}-{time.time_ns()}"
            )
            if os.path.lexists(displaced):
                raise AdminError(f"legacy migration displacement path exists: {displaced}")
            os.replace(destination, displaced)
        try:
            os.replace(slot, destination)
            installed = True
        except BaseException:
            if displaced is not None and os.path.lexists(displaced):
                os.replace(displaced, destination)
                displaced = None
            raise
        try:
            _fsync_state_directory(destination.parent)
        except BaseException:
            rejected = destination.parent / (
                f".{destination.name}.rejected-legacy-{os.getpid()}-{time.time_ns()}"
            )
            if installed and os.path.lexists(destination):
                os.replace(destination, rejected)
            if displaced is not None and os.path.lexists(displaced):
                os.replace(displaced, destination)
                displaced = None
            _discard_path(rejected)
            _fsync_state_directory(destination.parent)
            raise
        if displaced is not None:
            stale = displaced
            displaced = None
            try:
                _discard_path(stale)
            except Exception as exc:
                print(
                    f"Warning: migrated legacy state but could not remove displaced "
                    f"canonical tree {stale}: {exc}",
                    file=sys.stderr,
                )
    finally:
        _discard_path(slot)
        if displaced is not None:
            _discard_path(displaced)


def _migrate_legacy_home_state(features: tuple[str, ...]) -> tuple[LegacyMigration, ...]:
    migrations: list[LegacyMigration] = []
    for source, destination in _legacy_state_destinations(features):
        if not os.path.lexists(source):
            continue
        identities = _identity_files(source)
        source_manifest = tuple(_tree_manifest(source))
        _merge_tree_atomically(source, destination)
        migration = LegacyMigration(source, destination, identities, source_manifest)
        _verify_migrated_identities((migration,))
        migrations.append(migration)
    return tuple(migrations)


def _verify_migrated_identities(migrations: tuple[LegacyMigration, ...]) -> None:
    for migration in migrations:
        for relative, expected in migration.identity_hashes.items():
            target = migration.destination / _safe_state_relative(relative)
            if (
                not target.is_file()
                or target.is_symlink()
                or _hash_regular_file(target) != expected
            ):
                raise AdminError(f"legacy identity migration verification failed: {target}")


def _remove_migrated_legacy_state(migrations: tuple[LegacyMigration, ...]) -> None:
    for migration in migrations:
        source = migration.source
        if not os.path.lexists(source):
            continue
        if source.is_symlink() or not source.is_dir():
            raise AdminError(f"legacy state cleanup path is unsafe: {source}")
        if not migration.source_manifest:
            raise AdminError(f"legacy state cleanup lacks source manifest evidence: {source}")
        if tuple(_tree_manifest(source)) != migration.source_manifest:
            raise AdminError(f"legacy state changed after migration and was retained: {source}")
        shutil.rmtree(source)
        _fsync_state_directory(source.parent)


def _configured_identity_path(home: Path, config_file: Path | None = None) -> Path:
    default = home / ".config/reticulumpi/identity"
    config_file = CONFIG_FILE if config_file is None else config_file
    if not config_file.is_file() or config_file.is_symlink():
        return default
    candidates: list[tuple[int, str]] = []
    for line in config_file.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^(\s+)identity_path\s*:\s*(.*?)\s*$", line)
        if match:
            candidates.append((len(match.group(1).expandtabs(8)), match.group(2)))
    if not candidates:
        return default
    raw = min(candidates, key=lambda candidate: candidate[0])[1]
    try:
        values = shlex.split(raw, comments=True, posix=True)
    except ValueError as exc:
        raise AdminError(f"cannot parse identity_path in {config_file}: {exc}") from exc
    if len(values) != 1:
        raise AdminError(f"identity_path in {config_file} must be one scalar path")
    value = values[0]
    if value == "~":
        requested = home
    elif value.startswith("~/"):
        requested = home / value[2:]
    else:
        requested = Path(value).expanduser()
        if not requested.is_absolute():
            requested = Path("/") / requested
    _reject_symlink_components(requested)
    return requested.resolve()


def _identity_key(path: Path, roots: tuple[StateRoot, ...]) -> str:
    resolved = path.resolve()
    for root in roots:
        root_path = root.path.resolve()
        if _is_within(resolved, root_path):
            return f"{root.name}:{resolved.relative_to(root_path).as_posix()}"
    return f"absolute:{resolved}"


def _identity_hashes(
    roots: tuple[StateRoot, ...],
    config_file: Path | None = None,
) -> dict[str, str]:
    identities: dict[Path, str] = {}
    for root in roots:
        if not root.path.exists():
            continue
        for relative, entry_stat in _tree_entries(root.path):
            if stat.S_ISREG(entry_stat.st_mode) and relative.name == "identity":
                path = (root.path / relative).resolve()
                identities[path] = _hash_regular_file(path)
    configured = _configured_identity_path(DATA_DIR.resolve(), config_file)
    if os.path.lexists(configured):
        if configured.is_symlink() or not configured.is_file():
            raise AdminError(f"configured identity is not a regular file: {configured}")
        identities[configured] = _hash_regular_file(configured)
    return {
        _identity_key(path, roots): digest
        for path, digest in sorted(identities.items(), key=lambda item: str(item[0]))
    }


def _verify_identity_continuity(before: dict[str, str], after: dict[str, str]) -> None:
    missing = sorted(set(before) - set(after))
    changed = sorted(key for key in before.keys() & after.keys() if before[key] != after[key])
    if missing or changed:
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if changed:
            details.append("changed=" + ",".join(changed))
        raise AdminError("identity continuity verification failed: " + "; ".join(details))


def _state_databases(root: StateRoot) -> list[tuple[Path, Path]]:
    found: list[tuple[Path, Path]] = []
    if not root.path.exists():
        return found
    for relative, entry_stat in _tree_entries(root.path):
        if stat.S_ISREG(entry_stat.st_mode) and Path(relative.name).suffix.lower() in {
            ".db",
            ".sqlite",
            ".sqlite3",
        }:
            found.append((relative, root.path / relative))
    return found


def _backup_state(
    version: str,
    features: tuple[str, ...] = (),
    *,
    config_file: Path | None = None,
    exact_roots: tuple[StateRoot, ...] | None = None,
    external_config_file: Path | None = None,
) -> Path:
    _ensure_real_directory(BACKUP_DIR, mode=0o700)
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    destination = Path(
        tempfile.mkdtemp(
            prefix=f"{_RELEASE_BACKUP_PREFIX}{timestamp}-{version}-",
            dir=BACKUP_DIR,
        )
    )
    destination.chmod(0o700)
    try:
        roots = _state_roots(features) if exact_roots is None else exact_roots
        identity_hashes = _identity_hashes(roots, config_file)
        state_metadata: list[dict[str, object]] = []
        state_directory = destination / "state"
        state_directory.mkdir(mode=0o700)
        database_names: list[dict[str, str]] = []
        for root in roots:
            present = root.path.exists()
            entry: dict[str, object] = {
                "name": root.name,
                "path": str(root.path.resolve()),
                "present": present,
            }
            if present:
                ignored = (
                    frozenset({Path("admin-transaction.json")})
                    if root.name == "data"
                    else frozenset()
                )
                snapshot_root = state_directory / root.name
                _copy_tree_verified(root.path, snapshot_root, ignored=ignored)
                for relative, database in _state_databases(root):
                    snapshot = snapshot_root / relative
                    database_stat = database.stat()
                    snapshot.unlink(missing_ok=True)
                    snapshot.with_name(snapshot.name + "-wal").unlink(missing_ok=True)
                    snapshot.with_name(snapshot.name + "-shm").unlink(missing_ok=True)
                    _sqlite_backup_file(database, snapshot)
                    os.chown(snapshot, database_stat.st_uid, database_stat.st_gid)
                    snapshot.chmod(stat.S_IMODE(database_stat.st_mode))
                    with snapshot.open("rb") as handle:
                        os.fsync(handle.fileno())
                    _fsync_state_directory(snapshot.parent)
                    database_names.append({"state": root.name, "path": relative.as_posix()})
                entry["manifest"] = _tree_manifest(snapshot_root)
            else:
                entry["manifest"] = []
            state_metadata.append(entry)
        _fsync_state_directory(state_directory)
        _atomic_json(
            destination / "backup.json",
            {
                "schema": 2,
                "version": version,
                "created_at": timestamp,
                "databases": database_names,
                "features": list(features),
                "configuration_file": str(
                    (CONFIG_FILE if config_file is None else config_file).resolve()
                ),
                "external_configuration_file": (
                    str(external_config_file.resolve())
                    if external_config_file is not None
                    else None
                ),
                "identity_hashes": identity_hashes,
                "state_roots": state_metadata,
            },
            0o600,
        )
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    backups = sorted(
        (
            path
            for path in BACKUP_DIR.iterdir()
            if path.is_dir() and path.name.startswith(_RELEASE_BACKUP_PREFIX)
        ),
        key=lambda path: path.stat().st_mtime,
    )
    protected: Path | None = None
    if MANIFEST_FILE.is_file() and not MANIFEST_FILE.is_symlink():
        manifest_value = _read_json_object(MANIFEST_FILE, "installation manifest")
        raw_protected = manifest_value.get("legacy_bridge_backup")
        if isinstance(raw_protected, str):
            candidate = Path(raw_protected).expanduser().resolve()
            if _is_within(candidate, BACKUP_DIR.resolve()):
                protected = candidate
    removable = [path for path in backups if protected is None or path.resolve() != protected]
    for expired in removable[:-3]:
        shutil.rmtree(expired)
    return destination


def _database_record_path(
    value: object,
    roots: dict[str, StateRoot],
) -> Path:
    if isinstance(value, str):
        root_name = "data"
        relative = _safe_state_relative(value)
    elif isinstance(value, dict):
        root_name = value.get("state")
        relative = _safe_state_relative(value.get("path"))
        if not isinstance(root_name, str):
            raise AdminError("backup database state root is invalid")
    else:
        raise AdminError("backup database record is invalid")
    root = roots.get(root_name)
    if root is None:
        raise AdminError(f"backup database uses an unknown state root: {root_name}")
    path = (root.path / relative).resolve()
    if not _is_within(path, root.path.resolve()):
        raise AdminError(f"backup database path escapes state root: {relative}")
    return path


def _sqlite_schema_evidence(path: Path) -> dict[str, object]:
    """Describe a validated SQLite clone without depending on application code."""

    try:
        with contextlib.closing(
            sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        ) as connection:
            user_version_row = connection.execute("PRAGMA user_version").fetchone()
            schema_objects = connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' "
                "AND type IN ('table', 'index', 'trigger', 'view') "
                "ORDER BY type, name"
            ).fetchall()
    except sqlite3.Error as exc:
        raise AdminError(f"SQLite schema inspection failed for {path}: {exc}") from exc
    return {
        "user_version": int(user_version_row[0]) if user_version_row else 0,
        "schema_objects": [
            {
                "type": str(kind),
                "name": str(name),
                "tbl_name": str(table_name),
                "sql": str(definition) if definition is not None else None,
            }
            for kind, name, table_name, definition in schema_objects
        ],
    }


def _map_preexisting_database_to_live(
    path: Path,
    migrations: tuple[LegacyMigration, ...],
    canonical_roots: tuple[StateRoot, ...],
) -> Path:
    resolved = path.resolve()
    for migration in migrations:
        source = migration.source.resolve()
        if _is_within(resolved, source):
            return (migration.destination.resolve() / resolved.relative_to(source)).resolve()
    for root in canonical_roots:
        if _is_within(resolved, root.path.resolve()):
            return resolved
    raise AdminError(f"pre-existing database has no canonical migration destination: {path}")


def _validate_live_sqlite_state(
    backup: Path,
    transaction_roots: tuple[StateRoot, ...],
    migrations: tuple[LegacyMigration, ...],
) -> list[dict[str, object]]:
    """Clone and validate every live canonical database before legacy cleanup.

    The transaction backup already contains pre-activation SQLite snapshots.
    This second evidence set proves that every pre-existing database reached a
    canonical destination and that the candidate's active view is structurally
    readable after any intentional additive schema migration.
    """

    canonical_roots = tuple(
        root
        for root in transaction_roots
        if root.path.resolve() in {CONFIG_DIR.resolve(), DATA_DIR.resolve()}
    )
    if {root.path.resolve() for root in canonical_roots} != {
        CONFIG_DIR.resolve(),
        DATA_DIR.resolve(),
    }:
        raise AdminError("transaction roots omit canonical SQLite validation roots")

    metadata = _read_json_object(backup / "backup.json", "backup metadata")
    raw_databases = metadata.get("databases", [])
    if not isinstance(raw_databases, list):
        raise AdminError("backup database list is invalid")
    backup_roots = {root.name: root for root in _backup_roots_from_metadata(metadata)}
    required = {
        _map_preexisting_database_to_live(
            _database_record_path(value, backup_roots),
            migrations,
            canonical_roots,
        )
        for value in raw_databases
    }

    validation_root = backup / "validation/live-sqlite"
    if os.path.lexists(validation_root):
        raise AdminError(f"live SQLite validation evidence already exists: {validation_root}")
    _ensure_real_directory(validation_root, mode=0o700)
    validation_root.parent.chmod(0o700)

    actual: set[Path] = set()
    records: list[dict[str, object]] = []
    for root in canonical_roots:
        for relative, database in _state_databases(root):
            resolved = database.resolve()
            actual.add(resolved)
            clone = validation_root / root.name / relative
            _sqlite_backup_file(database, clone)
            _verify_sqlite(clone)
            records.append(
                {
                    "state": root.name,
                    "path": relative.as_posix(),
                    "validation_copy": str(clone.relative_to(backup)),
                    "preexisting": resolved in required,
                    **_sqlite_schema_evidence(clone),
                }
            )

    missing = sorted(str(path) for path in required - actual)
    if missing:
        raise AdminError(
            "candidate live SQLite coverage omits pre-existing databases: " + ", ".join(missing)
        )
    records.sort(key=lambda item: (str(item["state"]), str(item["path"])))
    _atomic_json(
        validation_root / "evidence.json",
        {"schema": 1, "databases": records},
        0o600,
    )
    _fsync_directory(validation_root)
    _fsync_directory(validation_root.parent)
    return records


def _build_wheel(bundle: Path, source: Path | None, destination: Path) -> Path:
    if source is None:
        wheel = destination / bundle.name
        manifest = bundle.parent / BUNDLE_MANIFEST_NAME
        expected = _read_hash_manifest(manifest).get(bundle.name)
        if expected is None:
            raise AdminError(f"signed hash manifest does not contain bundle: {bundle.name}")
        _snapshot_regular_file(bundle, wheel, expected_sha256=expected, mode=0o600)
        return wheel
    bundle_metadata = source / "bundle.json"
    if bundle_metadata.is_file() and not bundle_metadata.is_symlink():
        metadata = _read_json_object(bundle_metadata, "install bundle metadata")
        if metadata.get("kind") == "reticulumpi-install":
            wheel_name = metadata.get("wheel")
            if (
                not isinstance(wheel_name, str)
                or Path(wheel_name).name != wheel_name
                or not wheel_name.endswith(".whl")
            ):
                raise AdminError("install bundle metadata has an invalid wheel basename")
            bundled_wheel = source / wheel_name
            if bundled_wheel.is_symlink() or not bundled_wheel.is_file():
                raise AdminError("install bundle is missing its declared prebuilt wheel")
            wheel = destination / wheel_name
            expected = _read_hash_manifest(source / BUNDLE_MANIFEST_NAME).get(wheel_name)
            if expected is None:
                raise AdminError(
                    f"signed hash manifest does not contain bundle wheel: {wheel_name}"
                )
            _snapshot_regular_file(
                bundled_wheel,
                wheel,
                expected_sha256=expected,
                mode=0o600,
            )
            return wheel
    _run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(destination),
            str(source),
        ]
    )
    wheels = list(destination.glob("reticulumpi-*.whl"))
    if len(wheels) != 1:
        raise AdminError("bundle build did not produce exactly one ReticulumPi wheel")
    return wheels[0]


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for candidate in path.rglob("*"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        total += candidate.stat().st_size
    return total


def _ensure_install_space(root: Path, bundle: Path) -> None:
    ancestor = root
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    bundle_size = _path_size(bundle)
    data_size = _path_size(DATA_DIR) if DATA_DIR.exists() else 0
    required = max(256 * 1024 * 1024, bundle_size * 2 + data_size * 2)
    free = shutil.disk_usage(ancestor).free
    if free < required:
        raise AdminError(
            f"insufficient free space: need {required} bytes for candidate and backup, have {free}"
        )


def _extras(features: tuple[str, ...]) -> str:
    supported = _PACKAGE_FEATURES | {
        "shared-rnsd",
        "watchdog",
        "captive-portal",
        "offline-tools",
        "chrony-control",
    }
    unknown = set(features) - supported
    if unknown:
        raise AdminError(f"unknown features: {', '.join(sorted(unknown))}")
    selected = sorted(set(features) & _PACKAGE_FEATURES)
    return f"[{','.join(selected)}]" if selected else ""


def _selected_unit_names(features: tuple[str, ...]) -> set[str]:
    unit_names = {
        "reticulumpi.service",
        "reticulumpi-control.socket",
        "reticulumpi-control@.service",
    }
    if {"nomadnet", "shared-rnsd"} & set(features):
        unit_names.add("rnsd.service")
    if "watchdog" in features:
        unit_names.update({"rnsd-watchdog.service", "rnsd-watchdog.timer"})
    return unit_names


def _render_units(
    source: Path,
    root: Path,
    features: tuple[str, ...],
    previous_features: tuple[str, ...] = (),
) -> None:
    _validate_install_root_ancestry(root)
    _ensure_real_directory(SYSTEMD_DIR)
    unit_names = _selected_unit_names(features)
    units = [source / "systemd" / name for name in sorted(unit_names)]
    for unit in units:
        if unit.is_symlink() or not unit.is_file():
            raise AdminError(f"bundle systemd unit must be a regular file: {unit.name}")
        rendered = unit.read_text(encoding="utf-8").replace(
            DEFAULT_CURRENT_PREFIX, str(root / "current")
        )
        if unit.name == "reticulumpi-control@.service":
            expected_exec = (
                f"ExecStart={root}/current/.venv/bin/python -I -m reticulumpi.control_broker"
            )
            exec_lines = [line for line in rendered.splitlines() if line.startswith("ExecStart=")]
            if exec_lines != [expected_exec]:
                raise AdminError(
                    "control broker unit must execute the isolated interpreter from current"
                )
        _atomic_write(SYSTEMD_DIR / unit.name, rendered.encode("utf-8"), 0o644)
    previously_managed = _selected_unit_names(previous_features)
    for obsolete in previously_managed - unit_names:
        path = SYSTEMD_DIR / obsolete
        if path.is_symlink():
            raise AdminError(f"managed unit may not be a symlink: {path}")
        path.unlink(missing_ok=True)
    dropin = SYSTEMD_DIR / _RNSD_DROPIN_RELATIVE
    if {"nomadnet", "shared-rnsd"} & set(features):
        content = """[Unit]
Requires=rnsd.service
After=rnsd.service

[Service]
ExecStartPre=/bin/bash -c 'for i in $(seq 1 60); do ss -xa 2>/dev/null | grep -q "@rns/default" && exit 0; sleep 1; done; exit 1'
"""
        _atomic_write(dropin, content.encode("utf-8"), 0o644)
    else:
        if dropin.is_symlink():
            raise AdminError(f"managed drop-in may not be a symlink: {dropin}")
        dropin.unlink(missing_ok=True)
    gpsd_dropin = SYSTEMD_DIR / _GPSD_DROPIN_RELATIVE
    if "gps" in features:
        _atomic_write(
            gpsd_dropin,
            b"[Unit]\nWants=gpsd.service\nAfter=gpsd.service\n",
            0o644,
        )
    else:
        if gpsd_dropin.is_symlink():
            raise AdminError(f"managed drop-in may not be a symlink: {gpsd_dropin}")
        gpsd_dropin.unlink(missing_ok=True)


def _selected_helper_names(features: tuple[str, ...]) -> set[str]:
    helper_names = {"restart_services.sh"}
    if "captive-portal" in features:
        helper_names.add("captive_portal_helper.sh")
    if "offline-tools" in features:
        helper_names.add("simulate_offline.sh")
    if "chrony-control" in features:
        helper_names.add("chrony_helper.sh")
    return helper_names


def _install_helpers(
    source: Path,
    features: tuple[str, ...],
    previous_features: tuple[str, ...] = (),
) -> None:
    _ensure_real_directory(LIBEXEC_DIR)
    helper_names = _selected_helper_names(features)
    for name in helper_names:
        helper = source / "scripts" / name
        if not helper.is_file() or helper.is_symlink():
            raise AdminError(f"bundle is missing required helper: {name}")
        _atomic_copy(helper, LIBEXEC_DIR / name, 0o755)
    previously_managed = _selected_helper_names(previous_features)
    for obsolete in previously_managed - helper_names:
        path = LIBEXEC_DIR / obsolete
        if path.is_symlink():
            raise AdminError(f"managed helper may not be a symlink: {path}")
        path.unlink(missing_ok=True)
    if "offline-tools" in features:
        _ensure_real_directory(SHARED_CONFIG_DIR)
        offline = source / "config/reticulumpi/offline_profile.yaml"
        _atomic_copy(offline, SHARED_CONFIG_DIR / offline.name, 0o644)
    elif "offline-tools" in previous_features:
        offline = SHARED_CONFIG_DIR / "offline_profile.yaml"
        if offline.is_symlink():
            raise AdminError(f"managed offline profile may not be a symlink: {offline}")
        offline.unlink(missing_ok=True)


def _remove_legacy_sudoers() -> None:
    if not SUDOERS_DIR.exists():
        return
    _reject_symlink_components(SUDOERS_DIR)
    for name in _LEGACY_SUDOERS_NAMES:
        path = SUDOERS_DIR / name
        if path.is_symlink():
            raise AdminError(f"legacy sudoers path may not be a symlink: {path}")
        path.unlink(missing_ok=True)


def _managed_paths() -> tuple[Path, ...]:
    paths = [*(SYSTEMD_DIR / name for name in _MANAGED_UNIT_NAMES)]
    paths.extend(LIBEXEC_DIR / name for name in _MANAGED_HELPER_NAMES)
    paths.extend(SUDOERS_DIR / name for name in _LEGACY_SUDOERS_NAMES)
    paths.append(SHARED_CONFIG_DIR / "offline_profile.yaml")
    paths.append(SYSTEMD_DIR / _RNSD_DROPIN_RELATIVE)
    paths.append(SYSTEMD_DIR / _GPSD_DROPIN_RELATIVE)
    paths.append(CHRONY_CONFIG_FILE)
    paths.append(CAPTIVE_DNSMASQ_CONFIG_FILE)
    return tuple(sorted(paths))


def _read_file_snapshot(path: Path) -> tuple[bytes, os.stat_result]:
    _reject_symlink_components(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AdminError(f"cannot open managed file snapshot {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise AdminError(f"managed path is not a regular file: {path}")
        blocks: list[bytes] = []
        while block := os.read(descriptor, 1024 * 1024):
            blocks.append(block)
        after = os.fstat(descriptor)
        current = path.lstat()
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
        ) or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise AdminError(f"managed file changed while snapshotting: {path}")
        return b"".join(blocks), before
    finally:
        os.close(descriptor)


def _snapshot_files(paths: tuple[Path, ...]) -> tuple[FileSnapshot, ...]:
    snapshots: list[FileSnapshot] = []
    for path in paths:
        if path.is_symlink():
            raise AdminError(f"managed path may not be a symlink: {path}")
        if path.exists():
            if not path.is_file():
                raise AdminError(f"managed path is not a regular file: {path}")
            data, path_stat = _read_file_snapshot(path)
            snapshots.append(
                FileSnapshot(
                    path,
                    data,
                    stat.S_IMODE(path_stat.st_mode),
                    path_stat.st_uid,
                    path_stat.st_gid,
                )
            )
        else:
            snapshots.append(FileSnapshot(path, None, None))
    return tuple(snapshots)


def _restore_files(snapshots: tuple[FileSnapshot, ...]) -> None:
    for snapshot in snapshots:
        if snapshot.data is None:
            if snapshot.path.is_symlink():
                raise AdminError(f"refusing to remove managed symlink: {snapshot.path}")
            snapshot.path.unlink(missing_ok=True)
        else:
            _atomic_write(snapshot.path, snapshot.data, snapshot.mode or 0o600)
            if snapshot.uid is not None and snapshot.gid is not None:
                os.chown(snapshot.path, snapshot.uid, snapshot.gid)


def _persist_file_snapshots(
    backup: Path,
    snapshots: tuple[FileSnapshot, ...],
) -> Path:
    """Persist managed-file snapshots so power-loss recovery can restore units."""

    directory = backup / "managed-files"
    directory.mkdir(mode=0o700)
    records: list[dict[str, object]] = []
    for index, snapshot in enumerate(snapshots):
        record: dict[str, object] = {
            "path": str(snapshot.path),
            "present": snapshot.data is not None,
            "mode": snapshot.mode,
            "uid": snapshot.uid,
            "gid": snapshot.gid,
        }
        if snapshot.data is not None:
            blob = directory / f"{index:03d}.bin"
            _atomic_write(blob, snapshot.data, 0o600)
            record.update(
                {
                    "blob": blob.name,
                    "size": len(snapshot.data),
                    "sha256": hashlib.sha256(snapshot.data).hexdigest(),
                }
            )
        records.append(record)
    manifest = backup / "managed-files.json"
    _atomic_json(manifest, {"schema": 1, "files": records}, 0o600)
    _fsync_directory(backup)
    return manifest


def _persist_legacy_bridge_evidence(
    backup: Path,
    services: dict[str, dict[str, bool]],
) -> tuple[dict[str, str], ...]:
    metadata_path = backup / "backup.json"
    metadata = _read_json_object(metadata_path, "backup metadata")
    roots = _backup_roots_from_metadata(metadata)
    validated_services = _validate_service_state_snapshot(services)
    metadata["legacy_bridge"] = {
        "services_before": validated_services,
        "state_roots": [{"name": root.name, "path": str(root.path.resolve())} for root in roots],
    }
    _atomic_json(metadata_path, metadata, 0o600)
    _fsync_directory(backup)
    return tuple({"name": root.name, "path": str(root.path.resolve())} for root in roots)


def _retained_legacy_bridge_evidence(
    installed_manifest: dict[str, object] | None,
    *,
    legacy_bridge: bool,
    backup: Path | None,
    bridge_roots: tuple[dict[str, str], ...],
    service_states: dict[str, dict[str, bool]],
) -> tuple[
    str | None,
    tuple[dict[str, str], ...],
    dict[str, dict[str, bool]] | None,
]:
    """Keep the mutable predecessor recoverable across immutable upgrades.

    There is intentionally no automatic expiry.  Production operators may need
    the predecessor throughout hardware qualification and the soak period, and
    losing this pointer would also make backup pruning treat the evidence as an
    ordinary old transaction.
    """

    if legacy_bridge:
        if backup is None or not bridge_roots:
            raise AdminError("legacy bridge completion is missing durable predecessor evidence")
        return str(backup), bridge_roots, _validate_service_state_snapshot(service_states)
    if installed_manifest is None:
        return None, (), None
    raw_backup = installed_manifest.get("legacy_bridge_backup")
    if raw_backup is None:
        return None, (), None
    retained_backup = str(raw_backup)
    retained_roots = _validate_backup_root_evidence(installed_manifest.get("legacy_bridge_roots"))
    retained_services = _validate_service_state_snapshot(
        installed_manifest.get("legacy_bridge_services")
    )
    return retained_backup, retained_roots, retained_services


def _allowed_managed_snapshot_path(raw_path: str, backup: Path | None = None) -> Path | None:
    static = {str(path): path for path in _managed_paths()}
    if raw_path in static:
        return static[raw_path]
    path = Path(raw_path)
    credential_dropin = SYSTEMD_DIR / "reticulumpi.service.d"
    if (
        path.is_absolute()
        and path.parent == credential_dropin
        and re.fullmatch(r"[A-Za-z0-9_.-]+\.conf", path.name)
    ):
        return path
    if backup is not None and path.is_absolute():
        metadata = _read_json_object(backup / "backup.json", "backup metadata")
        external_config = _backup_external_configuration_file(metadata)
        if external_config is not None and path.resolve() == external_config:
            return external_config
    return None


def _load_file_snapshots(backup: Path) -> tuple[FileSnapshot, ...]:
    manifest = backup / "managed-files.json"
    value = _read_json_object(manifest, "managed-file recovery manifest")
    if value.get("schema") != 1 or not isinstance(value.get("files"), list):
        raise AdminError("unsupported managed-file recovery manifest")
    seen: set[str] = set()
    snapshots: list[FileSnapshot] = []
    for raw in value["files"]:
        if not isinstance(raw, dict):
            raise AdminError("managed-file recovery record must be an object")
        raw_path = raw.get("path")
        allowed_path = (
            _allowed_managed_snapshot_path(raw_path, backup) if isinstance(raw_path, str) else None
        )
        present = raw.get("present")
        mode = raw.get("mode")
        uid = raw.get("uid")
        gid = raw.get("gid")
        if (
            not isinstance(raw_path, str)
            or allowed_path is None
            or raw_path in seen
            or not isinstance(present, bool)
            or (mode is not None and (not isinstance(mode, int) or mode < 0 or mode > 0o7777))
            or (uid is not None and (not isinstance(uid, int) or uid < 0))
            or (gid is not None and (not isinstance(gid, int) or gid < 0))
            or ((uid is None) != (gid is None))
        ):
            raise AdminError(f"invalid managed-file recovery record: {raw_path!r}")
        seen.add(raw_path)
        if not present:
            snapshots.append(FileSnapshot(allowed_path, None, None))
            continue
        blob_name = raw.get("blob")
        if not isinstance(blob_name, str) or not re.fullmatch(r"[0-9]{3}\.bin", blob_name):
            raise AdminError(f"invalid managed-file recovery blob for {raw_path}")
        blob = backup / "managed-files" / blob_name
        if blob.is_symlink() or not blob.is_file():
            raise AdminError(f"managed-file recovery blob is missing or unsafe: {blob}")
        data = blob.read_bytes()
        if raw.get("size") != len(data) or raw.get("sha256") != hashlib.sha256(data).hexdigest():
            raise AdminError(f"managed-file recovery blob verification failed: {blob}")
        snapshots.append(FileSnapshot(allowed_path, data, mode, uid, gid))
    return tuple(snapshots)


def _safe_state_relative(raw: object) -> Path:
    if not isinstance(raw, str) or not raw:
        raise AdminError("backup state path must be a non-empty string")
    relative = Path(raw)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise AdminError(f"backup state path is unsafe: {raw!r}")
    return relative


def _discard_path(path: Path) -> None:
    if not os.path.lexists(path):
        return
    mode = path.lstat().st_mode
    if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _restore_records(
    backup: Path,
    metadata: dict[str, object],
) -> tuple[list[tuple[StateRoot, bool, list[dict[str, object]]]], dict[str, StateRoot]]:
    raw_records = metadata.get("state_roots")
    records: list[tuple[StateRoot, bool, list[dict[str, object]]]] = []
    if raw_records is None:
        allowed = {root.name: root for root in _state_roots(("nomadnet",))}
        # Compatibility with the pre-schema backup layout. It can be restored
        # safely, but it did not capture service-home state.
        for name in ("etc", "data"):
            source = backup / name
            if source.exists():
                if source.is_symlink() or not source.is_dir():
                    raise AdminError(f"invalid legacy backup directory: {source}")
                records.append((allowed[name], True, _tree_manifest(source)))
        return records, allowed
    if metadata.get("schema") != 2 or not isinstance(raw_records, list):
        raise AdminError("unsupported transaction backup schema")
    _backup_features(metadata)
    has_persisted_paths = all(
        isinstance(record, dict) and "path" in record for record in raw_records
    )
    if has_persisted_paths:
        exact_roots = _backup_roots_from_metadata(metadata)
        allowed = {root.name: root for root in exact_roots}
        expected_names = set(allowed)
    else:
        # Compatibility with early schema-2 backups. These can only recover
        # paths still discoverable from the installed unit/account layout.
        raw_features = _backup_features(metadata)
        allowed = {root.name: root for root in _state_roots(("nomadnet",))}
        expected_names = {root.name for root in _state_roots(raw_features)}
    seen: set[str] = set()
    for raw_record in raw_records:
        if not isinstance(raw_record, dict):
            raise AdminError("backup state-root record must be an object")
        name = raw_record.get("name")
        present = raw_record.get("present")
        manifest = raw_record.get("manifest")
        raw_path = raw_record.get("path")
        if not isinstance(name, str) or name not in allowed or name in seen:
            raise AdminError(f"backup contains an invalid state root: {name!r}")
        if has_persisted_paths and str(allowed[name].path.resolve()) != raw_path:
            raise AdminError(f"backup state-root path evidence changed: {name}")
        if (
            not isinstance(present, bool)
            or not isinstance(manifest, list)
            or not all(isinstance(item, dict) for item in manifest)
        ):
            raise AdminError(f"backup state-root metadata is invalid: {name}")
        if not present and manifest:
            raise AdminError(f"absent backup state root has a manifest: {name}")
        seen.add(name)
        records.append((allowed[name], present, manifest))
    if seen != expected_names:
        missing = ", ".join(sorted(expected_names - seen)) or "none"
        extra = ", ".join(sorted(seen - expected_names)) or "none"
        raise AdminError(f"backup state-root set is incomplete: missing={missing}; extra={extra}")
    return records, allowed


def _verify_restored_databases(
    metadata: dict[str, object],
    roots: dict[str, StateRoot],
) -> None:
    raw_databases = metadata.get("databases", [])
    if not isinstance(raw_databases, list):
        raise AdminError("backup database list is invalid")
    for value in raw_databases:
        if isinstance(value, str):
            root_name = "data"
            relative = _safe_state_relative(value)
        elif isinstance(value, dict):
            root_name = value.get("state")
            relative = _safe_state_relative(value.get("path"))
            if not isinstance(root_name, str):
                raise AdminError("backup database state root is invalid")
        else:
            raise AdminError("backup database record is invalid")
        root = roots.get(root_name)
        if root is None:
            raise AdminError(f"backup database uses an unknown state root: {root_name}")
        database = (root.path / relative).resolve()
        if not _is_within(database, root.path.resolve()):
            raise AdminError(f"backup database path escapes state root: {relative}")
        _verify_sqlite(database)


def _restore_state_backup(backup: Path) -> None:
    """Atomically restore every verified durable-state root as one transaction."""

    requested_backup = backup
    _reject_symlink_components(requested_backup)
    backup = requested_backup.resolve()
    if not backup.is_dir():
        raise AdminError(f"transaction backup is missing or unsafe: {backup}")
    metadata = _read_json_object(backup / "backup.json", "backup metadata")
    records, allowed_roots = _restore_records(backup, metadata)
    stages: list[RestoreStage] = []
    switched: list[RestoreStage] = []
    try:
        for root, present, manifest in records:
            _ensure_real_directory(root.path.parent, mode=None)
            temporary: Path | None = None
            if present:
                source = (
                    backup / "state" / root.name
                    if metadata.get("schema") == 2
                    else backup / root.name
                )
                if source.is_symlink() or not source.is_dir():
                    raise AdminError(f"backup state root is missing or unsafe: {source}")
                if _tree_manifest(source) != manifest:
                    raise AdminError(f"backup state-root manifest mismatch: {root.name}")
                slot = Path(
                    tempfile.mkdtemp(
                        prefix=f".{root.path.name}.restore-",
                        dir=root.path.parent,
                    )
                )
                slot.rmdir()
                temporary = slot
                _copy_tree_verified(source, temporary)
                if _tree_manifest(temporary) != manifest:
                    raise AdminError(f"staged state-root manifest mismatch: {root.name}")
            stages.append(RestoreStage(root, present, manifest, temporary))

        for stage in stages:
            destination = stage.root.path
            displaced = destination.parent / (
                f".{destination.name}.pre-restore-{os.getpid()}-{time.time_ns()}"
            )
            if os.path.lexists(displaced):
                raise AdminError(f"restore displacement path already exists: {displaced}")
            if os.path.lexists(destination):
                destination_stat = destination.lstat()
                if stat.S_ISLNK(destination_stat.st_mode) or not stat.S_ISDIR(
                    destination_stat.st_mode
                ):
                    raise AdminError(f"unsafe restore destination: {destination}")
                os.replace(destination, displaced)
                stage.displaced = displaced
            stage.switched = True
            switched.append(stage)
            if stage.temporary is not None:
                os.replace(stage.temporary, destination)
                stage.temporary = None
            _fsync_state_directory(destination.parent)

        for stage in stages:
            destination = stage.root.path
            if stage.present:
                if _tree_manifest(destination) != stage.manifest:
                    raise AdminError(f"restored state-root manifest mismatch: {stage.root.name}")
            elif os.path.lexists(destination):
                raise AdminError(f"absent state root was unexpectedly restored: {stage.root.name}")
        _verify_restored_databases(metadata, allowed_roots)
        expected_identities = metadata.get("identity_hashes", {})
        if not isinstance(expected_identities, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in expected_identities.items()
        ):
            raise AdminError("backup identity hash metadata is invalid")
        restored_roots = tuple(stage.root for stage in stages)
        _verify_identity_continuity(
            expected_identities,
            _identity_hashes(restored_roots, _backup_configuration_file(metadata)),
        )
    except BaseException as original_error:
        restoration_errors: list[str] = []
        for stage in reversed(switched):
            destination = stage.root.path
            quarantine = destination.parent / (
                f".{destination.name}.rejected-{os.getpid()}-{time.time_ns()}"
            )
            try:
                if os.path.lexists(destination):
                    os.replace(destination, quarantine)
                if stage.displaced is not None and os.path.lexists(stage.displaced):
                    os.replace(stage.displaced, destination)
                    stage.displaced = None
                _discard_path(quarantine)
                _fsync_state_directory(destination.parent)
            except Exception as exc:
                restoration_errors.append(f"{stage.root.name}: {exc}")
        for stage in stages:
            if stage.temporary is not None:
                try:
                    _discard_path(stage.temporary)
                except Exception as exc:
                    restoration_errors.append(f"discard {stage.root.name}: {exc}")
            if stage.displaced is not None:
                try:
                    _discard_path(stage.displaced)
                except Exception as exc:
                    restoration_errors.append(f"discard displaced {stage.root.name}: {exc}")
        if restoration_errors:
            raise AdminError(
                "state restore failed and atomic restoration was incomplete: "
                + "; ".join(restoration_errors)
            ) from original_error
        raise
    for stage in stages:
        if stage.displaced is None:
            continue
        try:
            _discard_path(stage.displaced)
            stage.displaced = None
            _fsync_state_directory(stage.root.path.parent)
        except OSError as exc:
            print(
                f"Warning: restored state but could not remove displaced {stage.root.name}: {exc}",
                file=sys.stderr,
            )


def _meshchat_server_block(
    lines: list[str],
) -> tuple[int, int, int] | None:
    plugin_headers = [
        (index, parsed[0])
        for index, line in enumerate(lines)
        if (parsed := _yaml_key_line(line, "plugins")) is not None
        and not parsed[1].split("#", 1)[0].strip()
    ]
    if not plugin_headers:
        return None
    if len(plugin_headers) != 1:
        raise AdminError("configuration contains multiple plugins blocks")
    plugins_index, plugins_indent = plugin_headers[0]
    active: list[tuple[int, int, str]] = []
    for index in range(plugins_index + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indentation = len(line) - len(line.lstrip(" "))
        if indentation <= plugins_indent:
            break
        active.append((index, indentation, line))
    if not active:
        return None
    plugin_indent = min(item[1] for item in active)
    starts = [
        index
        for index, indentation, line in active
        if indentation == plugin_indent
        and re.fullmatch(r"\s*meshchat_server\s*:\s*(?:#.*)?", line.rstrip("\r\n"))
    ]
    if len(starts) > 1:
        raise AdminError("configuration contains multiple meshchat_server blocks")
    if not starts:
        return None
    start = starts[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indentation = len(line) - len(line.lstrip(" "))
        if indentation <= plugin_indent:
            end = index
            break
    return start, end, plugin_indent


def _meshchat_storage_scalar(lines: list[str]) -> tuple[int, str] | None:
    block = _meshchat_server_block(lines)
    if block is None:
        return None
    start, end, plugin_indent = block
    active = [
        (index, len(line) - len(line.lstrip(" ")), line)
        for index, line in enumerate(lines[start + 1 : end], start + 1)
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not active:
        return None
    direct_indent = min(item[1] for item in active)
    values = [
        (index, parsed[1])
        for index, indentation, line in active
        if indentation == direct_indent
        and indentation > plugin_indent
        and (parsed := _yaml_key_line(line, "storage_dir")) is not None
    ]
    if len(values) > 1:
        raise AdminError("meshchat_server.storage_dir is configured more than once")
    if not values:
        return None
    index, raw = values[0]
    try:
        parsed_values = shlex.split(raw, comments=True, posix=True)
    except ValueError as exc:
        raise AdminError("meshchat_server.storage_dir is not a simple path") from exc
    if len(parsed_values) != 1:
        raise AdminError("meshchat_server.storage_dir is not a simple path")
    return index, parsed_values[0]


def _plan_meshchat_storage_path_migration(
    path: Path | None = None,
) -> MeshChatStoragePathMigration | None:
    path = CONFIG_FILE if path is None else path
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise AdminError(f"configuration may not be a symlink or special file: {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeError) as exc:
        raise AdminError(f"cannot inspect MeshChat storage in {path}: {exc}") from exc
    scalar = _meshchat_storage_scalar(lines)
    if scalar is None:
        return None
    line_index, configured = scalar
    configured_path = Path(os.path.expanduser(configured))
    if not configured_path.is_absolute():
        raise AdminError("meshchat_server.storage_dir must be absolute for migration")
    sources = {candidate.resolve() for candidate in _legacy_meshchat_storage_candidates()}
    if configured_path.resolve() not in sources:
        return None
    return MeshChatStoragePathMigration(
        line_index=line_index,
        source_path=str(configured_path.resolve()),
        destination_path=str((DATA_DIR / "meshchat/storage").resolve()),
        source_sha256=_sha256(path),
    )


def _apply_meshchat_storage_path_migration(
    migration: MeshChatStoragePathMigration,
    path: Path | None = None,
) -> None:
    path = CONFIG_FILE if path is None else path
    if path.is_symlink() or not path.is_file() or _sha256(path) != migration.source_sha256:
        raise AdminError("configuration changed after the MeshChat storage migration was planned")
    original_stat = path.stat()
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    scalar = _meshchat_storage_scalar(lines)
    if scalar != (migration.line_index, migration.source_path):
        raise AdminError("meshchat_server.storage_dir changed before migration")
    line = lines[migration.line_index]
    if line.count(migration.source_path) != 1:
        raise AdminError("meshchat_server.storage_dir source is ambiguous")
    lines[migration.line_index] = line.replace(
        migration.source_path,
        migration.destination_path,
        1,
    )
    _atomic_write(path, "".join(lines).encode("utf-8"), stat.S_IMODE(original_stat.st_mode))
    os.chown(path, original_stat.st_uid, original_stat.st_gid)
    if _plan_meshchat_storage_path_migration(path) is not None:
        raise AdminError("MeshChat storage path migration did not validate")


def _describe_meshchat_storage_path_migration(
    migration: MeshChatStoragePathMigration | None,
) -> list[dict[str, object]]:
    if migration is None:
        return []
    return [
        {
            "setting": "plugins.meshchat_server.storage_dir",
            "value": migration.destination_path,
        }
    ]


def _legacy_config_path_pattern(source_prefix: str) -> re.Pattern[str]:
    return re.compile(
        rf"{re.escape(source_prefix)}(?=$|[/\s'\"#,:;\]\}}])",
        re.MULTILINE,
    )


def _installed_dashboard_environment() -> tuple[bool, bool]:
    """Return (modern hash override, legacy plaintext override) without values."""

    has_hash = False
    has_plaintext = False
    for fragment in _installed_service_fragments():
        try:
            text = fragment.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise AdminError(f"cannot inspect dashboard environment in {fragment}: {exc}") from exc
        pending = ""
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            pending += line
            if pending.endswith("\\"):
                pending = pending[:-1] + " "
                continue
            if pending.startswith("EnvironmentFile="):
                # The referenced file is outside the signed unit and may contain
                # a legacy plaintext credential. Rotate conservatively.
                has_plaintext = True
            elif pending.startswith("Environment="):
                try:
                    assignments = shlex.split(pending.split("=", 1)[1])
                except ValueError as exc:
                    raise AdminError(
                        f"invalid dashboard Environment directive in {fragment}"
                    ) from exc
                names = {value.split("=", 1)[0] for value in assignments if "=" in value}
                has_hash = has_hash or "RETICULUMPI_DASHBOARD_PASSWORD_HASH" in names
                has_plaintext = has_plaintext or "RETICULUMPI_DASHBOARD_PASSWORD" in names
            pending = ""
    return has_hash, has_plaintext


def _dashboard_credential_dropins() -> tuple[Path, ...]:
    dropin = SYSTEMD_DIR / "reticulumpi.service.d"
    if not dropin.exists():
        return ()
    if dropin.is_symlink() or not dropin.is_dir():
        raise AdminError(f"installed service drop-in directory is unsafe: {dropin}")
    credential_names = {
        "RETICULUMPI_DASHBOARD_PASSWORD",
        "RETICULUMPI_DASHBOARD_PASSWORD_HASH",
    }
    found: list[Path] = []
    for fragment in sorted(dropin.glob("*.conf")):
        if fragment.is_symlink() or not fragment.is_file():
            raise AdminError(f"installed service drop-in is unsafe: {fragment}")
        try:
            text = fragment.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise AdminError(f"cannot inspect dashboard drop-in {fragment}: {exc}") from exc
        pending = ""
        contains_credential = False
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            pending += line
            if pending.endswith("\\"):
                pending = pending[:-1] + " "
                continue
            if pending.startswith("EnvironmentFile="):
                contains_credential = True
            elif pending.startswith("Environment="):
                try:
                    assignments = shlex.split(pending.split("=", 1)[1])
                except ValueError as exc:
                    raise AdminError(f"invalid Environment directive in {fragment}") from exc
                names = {value.split("=", 1)[0] for value in assignments if "=" in value}
                contains_credential = bool(names & credential_names)
            pending = ""
            if contains_credential:
                break
        if pending:
            raise AdminError(f"unterminated Environment directive in {fragment}")
        if contains_credential:
            found.append(fragment)
    return tuple(found)


def _legacy_dropin_is_safe(fragment: Path) -> bool:
    """Allow only inert resource-policy overrides to survive unit replacement."""

    try:
        raw_lines = fragment.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise AdminError(f"cannot inspect legacy service drop-in {fragment}: {exc}") from exc
    logical: list[str] = []
    pending = ""
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        pending += line
        if pending.endswith("\\"):
            pending = pending[:-1] + " "
            continue
        logical.append(pending)
        pending = ""
    if pending:
        raise AdminError(f"unterminated directive in legacy service drop-in: {fragment}")
    section: str | None = None
    for line in logical:
        if re.fullmatch(r"\[[A-Za-z][A-Za-z0-9]*\]", line):
            section = line[1:-1]
            continue
        if "=" not in line:
            return False
        key = line.split("=", 1)[0].strip()
        if section != "Service" or key not in _SAFE_LEGACY_SERVICE_DROPIN_KEYS:
            return False
    return True


def _unsafe_legacy_dropins() -> tuple[Path, ...]:
    dropin = SYSTEMD_DIR / "reticulumpi.service.d"
    if not dropin.exists():
        return ()
    if dropin.is_symlink() or not dropin.is_dir():
        raise AdminError(f"installed service drop-in directory is unsafe: {dropin}")
    admin_owned = {
        SYSTEMD_DIR / _RNSD_DROPIN_RELATIVE,
        SYSTEMD_DIR / _GPSD_DROPIN_RELATIVE,
    }
    unsafe: list[Path] = []
    for fragment in sorted(dropin.glob("*.conf")):
        if fragment.is_symlink() or not fragment.is_file():
            raise AdminError(f"installed service drop-in is unsafe: {fragment}")
        if fragment in admin_owned or _legacy_dropin_is_safe(fragment):
            continue
        unsafe.append(fragment)
    return tuple(unsafe)


def _describe_dashboard_credential_dropins(paths: tuple[Path, ...]) -> list[dict[str, object]]:
    if not paths:
        return []
    return [
        {
            "setting": "web_dashboard.systemd_credential_fragments",
            "value": "remove",
            "count": len(paths),
        }
    ]


def _describe_unsafe_legacy_dropins(paths: tuple[Path, ...]) -> list[dict[str, object]]:
    if not paths:
        return []
    return [
        {
            "setting": "reticulumpi.service.unsafe_legacy_dropins",
            "value": "remove",
            "count": len(paths),
        }
    ]


def _remove_dashboard_credential_dropins(
    paths: tuple[Path, ...], snapshots: tuple[FileSnapshot, ...]
) -> None:
    _remove_legacy_dropins(paths, snapshots)


def _remove_legacy_dropins(paths: tuple[Path, ...], snapshots: tuple[FileSnapshot, ...]) -> None:
    saved = {snapshot.path: snapshot for snapshot in snapshots}
    for path in paths:
        snapshot = saved.get(path)
        if snapshot is None or snapshot.data is None:
            raise AdminError(f"legacy service drop-in was not snapshotted: {path}")
        if path.is_symlink() or not path.is_file() or path.read_bytes() != snapshot.data:
            raise AdminError(f"legacy service drop-in changed before removal: {path}")
        path.unlink()
        _fsync_directory(path.parent)


def _dashboard_config_fields(path: Path) -> tuple[dict[str, tuple[int, str]], str]:
    if not path.exists():
        return {}, ""
    if path.is_symlink() or not path.is_file():
        raise AdminError(f"configuration may not be a symlink or special file: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AdminError(f"cannot inspect dashboard credentials in {path}: {exc}") from exc
    lines = text.splitlines()
    blocks: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)web_dashboard\s*:\s*(?:#.*)?$", line)
        if match and not line.lstrip().startswith("#"):
            blocks.append((index, len(match.group(1).expandtabs(8))))
    if len(blocks) > 1:
        raise AdminError("configuration contains multiple web_dashboard blocks")
    if not blocks:
        return {}, text
    start, block_indent = blocks[0]
    fields: dict[str, tuple[int, str]] = {}
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        if len(line[:indent].expandtabs(8)) <= block_indent:
            break
        match = re.match(r"^\s*(password_hash|password|secret_dir)\s*:\s*(.*?)\s*$", line)
        if match:
            name, value = match.groups()
            if name in fields:
                raise AdminError(f"web_dashboard.{name} is configured more than once")
            fields[name] = (index, value)
    return fields, text


def _dashboard_secret_dir(fields: dict[str, tuple[int, str]]) -> Path:
    raw = fields.get("secret_dir", (-1, ""))[1]
    if not raw:
        return DATA_DIR / ".config/reticulumpi"
    # Strip only a trailing YAML comment; quoted values are handled as data.
    value = raw.split(" #", 1)[0].strip()
    try:
        parsed = ast.literal_eval(value) if value[:1] in {'"', "'"} else value
    except (SyntaxError, ValueError) as exc:
        raise AdminError("web_dashboard.secret_dir is not a simple path") from exc
    if not isinstance(parsed, str) or not parsed:
        raise AdminError("web_dashboard.secret_dir is not a simple path")
    expanded = Path(os.path.expanduser(parsed))
    if not expanded.is_absolute():
        raise AdminError("web_dashboard.secret_dir must be absolute for production")
    _reject_symlink_components(expanded)
    return expanded


def _plan_dashboard_credential_migration(
    *,
    source_replaces_unit: bool,
    path: Path | None = None,
) -> DashboardCredentialMigration | None:
    path = CONFIG_FILE if path is None else path
    fields, text = _dashboard_config_fields(path)
    env_hash, env_plaintext = _installed_dashboard_environment()
    configured_hash = fields.get("password_hash", (-1, ""))[1].strip()
    if configured_hash or env_hash:
        # Explicit modern credentials belong to the operator and are never
        # silently replaced by an upgrade.
        return None
    if env_plaintext and not source_replaces_unit:
        raise AdminError(
            "legacy plaintext dashboard password remains in the installed unit; "
            "a complete signed source bundle is required to replace that unit safely"
        )
    secret_dir = _dashboard_secret_dir(fields)
    reason: str | None = None
    plaintext_line: int | None = None
    if "password" in fields and fields["password"][1].strip():
        reason = "plaintext_config"
        plaintext_line = fields["password"][0]
    elif env_plaintext:
        reason = "plaintext_unit_environment"
    else:
        candidates = [secret_dir]
        for home in _legacy_home_candidates():
            candidate = home / ".config/reticulumpi"
            if candidate not in candidates:
                candidates.append(candidate)
        for candidate in candidates:
            bootstrap = candidate / "dashboard_password.txt"
            if bootstrap.exists() or bootstrap.is_symlink():
                if bootstrap.is_symlink() or not bootstrap.is_file():
                    raise AdminError(f"legacy dashboard bootstrap file is unsafe: {bootstrap}")
                reason = "bootstrap_credential"
                break
            secret = candidate / "dashboard_secret"
            if not secret.exists():
                continue
            if secret.is_symlink() or not secret.is_file():
                raise AdminError(f"legacy dashboard secret is unsafe: {secret}")
            try:
                parts = secret.read_text(encoding="utf-8").strip().split(":")
            except (OSError, UnicodeError) as exc:
                raise AdminError(f"cannot inspect legacy dashboard secret {secret}: {exc}") from exc
            if len(parts) != 6 or parts[0] != "scrypt":
                reason = "legacy_password_hash"
                break
    if reason is None:
        return None
    return DashboardCredentialMigration(
        reason=reason,
        secret_dir=secret_dir,
        config_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None,
        plaintext_line=plaintext_line,
    )


def _hash_dashboard_bootstrap(password: str) -> str:
    salt = os.urandom(16)
    n, r, parallelism = 2**14, 8, 2
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=parallelism,
        dklen=32,
    )
    return f"scrypt:{salt.hex()}:{n}:{r}:{parallelism}:{derived.hex()}"


def _apply_dashboard_credential_migration(plan: DashboardCredentialMigration) -> None:
    if plan.plaintext_line is not None:
        if (
            not CONFIG_FILE.is_file()
            or CONFIG_FILE.is_symlink()
            or plan.config_sha256 is None
            or _sha256(CONFIG_FILE) != plan.config_sha256
        ):
            raise AdminError(
                "configuration changed after dashboard credential rotation was planned"
            )
        original_stat = CONFIG_FILE.stat()
        lines = CONFIG_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
        if plan.plaintext_line >= len(lines) or not re.match(
            r"^\s*password\s*:", lines[plan.plaintext_line]
        ):
            raise AdminError("dashboard plaintext credential moved during rotation")
        del lines[plan.plaintext_line]
        _atomic_write(
            CONFIG_FILE, "".join(lines).encode("utf-8"), stat.S_IMODE(original_stat.st_mode)
        )
        os.chown(CONFIG_FILE, original_stat.st_uid, original_stat.st_gid)

    session_paths = tuple(
        plan.secret_dir / name for name in ("sessions.db", "sessions.db-wal", "sessions.db-shm")
    )
    for session in session_paths:
        if session.is_symlink():
            raise AdminError(f"dashboard session database is unsafe: {session}")

    _ensure_real_directory(plan.secret_dir, mode=0o700)
    account = _service_account()
    os.chown(plan.secret_dir, account.pw_uid, account.pw_gid)
    password = secrets.token_urlsafe(24)
    password_hash = _hash_dashboard_bootstrap(password)
    hash_file = plan.secret_dir / "dashboard_secret"
    bootstrap_file = plan.secret_dir / "dashboard_password.txt"
    _atomic_write(hash_file, f"{password_hash}\n".encode("utf-8"), 0o600)
    os.chown(hash_file, account.pw_uid, account.pw_gid)
    _atomic_write(bootstrap_file, f"{password}\n".encode("utf-8"), 0o600)
    os.chown(bootstrap_file, account.pw_uid, account.pw_gid)
    for session in session_paths:
        session.unlink(missing_ok=True)
    _fsync_directory(plan.secret_dir)


def _describe_dashboard_credential_migration(
    plan: DashboardCredentialMigration | None,
) -> list[dict[str, str]]:
    if plan is None:
        return []
    return [
        {
            "setting": "web_dashboard.credentials",
            "value": "rotate_bootstrap_required",
            "reason": plan.reason,
        }
    ]


def _plan_legacy_config_path_migration(
    path: Path | None = None,
) -> LegacyConfigPathMigration | None:
    """Plan canonical path rewrites while leaving comments and unknown keys intact."""

    path = CONFIG_FILE if path is None else path
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise AdminError(f"configuration may not be a symlink or special file: {path}")
    destination_prefix = str(DATA_DIR.resolve())
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AdminError(f"cannot inspect legacy paths in {path}: {exc}") from exc
    lines = text.splitlines()
    meshchat_block = _meshchat_server_block(lines)
    excluded = (
        set(range(meshchat_block[0], meshchat_block[1])) if meshchat_block is not None else set()
    )
    for legacy_home in _legacy_home_candidates():
        if not legacy_home.is_absolute():
            raise AdminError(f"service user has an invalid home directory: {legacy_home}")
        source_prefix = str(legacy_home)
        if source_prefix == destination_prefix:
            continue
        pattern = _legacy_config_path_pattern(source_prefix)
        replacement_count = sum(
            len(pattern.findall(line))
            for index, line in enumerate(lines)
            if index not in excluded and line.strip() and not line.lstrip().startswith("#")
        )
        if replacement_count:
            return LegacyConfigPathMigration(
                source_prefix=source_prefix,
                destination_prefix=destination_prefix,
                replacement_count=replacement_count,
                source_sha256=_sha256(path),
            )
    return None


def _apply_legacy_config_path_migration(
    migration: LegacyConfigPathMigration,
    path: Path | None = None,
) -> None:
    path = CONFIG_FILE if path is None else path
    if path.is_symlink() or not path.is_file() or _sha256(path) != migration.source_sha256:
        raise AdminError("configuration changed after the legacy-path migration was planned")
    original_stat = path.stat()
    pattern = _legacy_config_path_pattern(migration.source_prefix)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    meshchat_block = _meshchat_server_block(lines)
    excluded = (
        set(range(meshchat_block[0], meshchat_block[1])) if meshchat_block is not None else set()
    )
    replacements = 0
    migrated: list[str] = []
    for index, line in enumerate(lines):
        if index not in excluded and line.strip() and not line.lstrip().startswith("#"):
            line, count = pattern.subn(migration.destination_prefix, line)
            replacements += count
        migrated.append(line)
    if replacements != migration.replacement_count:
        raise AdminError("legacy configuration path count changed during migration")
    _atomic_write(path, "".join(migrated).encode("utf-8"), stat.S_IMODE(original_stat.st_mode))
    os.chown(path, original_stat.st_uid, original_stat.st_gid)
    if _plan_legacy_config_path_migration(path) is not None:
        raise AdminError("legacy configuration path migration did not validate")


def _describe_legacy_config_path_migration(
    migration: LegacyConfigPathMigration | None,
) -> list[dict[str, object]]:
    if migration is None:
        return []
    return [
        {
            "setting": "legacy_service_home_paths",
            "value": migration.destination_prefix,
            "count": migration.replacement_count,
        }
    ]


def _yaml_key_line(line: str, key: str) -> tuple[int, str] | None:
    """Return indentation and raw scalar for an active block-style YAML key."""

    if not line.strip() or line.lstrip().startswith("#"):
        return None
    match = re.fullmatch(rf"( *){re.escape(key)}\s*:\s*(.*?)(?:\r?\n)?", line)
    if match is None:
        return None
    return len(match.group(1)), match.group(2)


def _legacy_allowlist_is_empty(
    lines: list[str],
    line_index: int,
    indentation: int,
    scalar: str,
) -> bool:
    value = scalar.split("#", 1)[0].strip()
    if value:
        if value.startswith("[") and value.endswith("]"):
            return not value[1:-1].strip()
        raise AdminError(
            "cannot safely migrate file_transfer.allowed_identities: expected a YAML list"
        )
    found_item = False
    for line in lines[line_index + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        child_indentation = len(line) - len(line.lstrip(" "))
        if child_indentation <= indentation:
            break
        stripped = line.strip()
        if not stripped.startswith("-"):
            raise AdminError(
                "cannot safely migrate file_transfer.allowed_identities: expected list items"
            )
        found_item = True
    return not found_item


def _plan_file_transfer_policy_migration(
    path: Path | None = None,
) -> FileTransferPolicyMigration | None:
    """Plan the locked legacy allowlist migration without exposing identities."""

    path = CONFIG_FILE if path is None else path
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise AdminError(f"configuration may not be a symlink or special file: {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeError) as exc:
        raise AdminError(f"cannot inspect file-transfer policy in {path}: {exc}") from exc
    blocks = [
        (index, parsed)
        for index, line in enumerate(lines)
        if (parsed := _yaml_key_line(line, "file_transfer")) is not None
    ]
    if not blocks:
        return None
    if len(blocks) != 1:
        raise AdminError("cannot safely migrate duplicate file_transfer configuration blocks")
    block_index, (block_indentation, block_scalar) = blocks[0]
    if block_scalar.split("#", 1)[0].strip():
        raise AdminError("cannot safely migrate an inline file_transfer configuration block")

    block_end = len(lines)
    child_lines: list[tuple[int, int, str]] = []
    for index in range(block_index + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indentation = len(line) - len(line.lstrip(" "))
        if indentation <= block_indentation:
            block_end = index
            break
        child_lines.append((index, indentation, line))
    direct_indentation = min(
        (indentation for _index, indentation, _line in child_lines),
        default=block_indentation + 2,
    )
    access_lines = [
        index
        for index, indentation, line in child_lines
        if indentation == direct_indentation and _yaml_key_line(line, "access_policy") is not None
    ]
    if len(access_lines) > 1:
        raise AdminError("duplicate file_transfer.access_policy settings are unsafe")
    if access_lines:
        return None
    allowlist_lines = [
        (index, parsed)
        for index, indentation, line in child_lines
        if indentation == direct_indentation
        and (parsed := _yaml_key_line(line, "allowed_identities")) is not None
    ]
    if len(allowlist_lines) > 1:
        raise AdminError("duplicate file_transfer.allowed_identities settings are unsafe")
    policy = "deny"
    if allowlist_lines:
        allowlist_index, (indentation, scalar) = allowlist_lines[0]
        policy = (
            "open"
            if _legacy_allowlist_is_empty(lines[:block_end], allowlist_index, indentation, scalar)
            else "allowlist"
        )
    return FileTransferPolicyMigration(
        policy=policy,
        insertion_index=block_index + 1,
        indentation=" " * direct_indentation,
        source_sha256=_sha256(path),
    )


def _apply_file_transfer_policy_migration(
    migration: FileTransferPolicyMigration,
    path: Path | None = None,
) -> None:
    """Apply a previously planned policy migration atomically and preserve metadata."""

    path = CONFIG_FILE if path is None else path
    if path.is_symlink() or not path.is_file() or _sha256(path) != migration.source_sha256:
        raise AdminError("configuration changed after the file-transfer migration was planned")
    original_stat = path.stat()
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if migration.insertion_index > len(lines):
        raise AdminError("file-transfer migration insertion point is no longer valid")
    previous = lines[migration.insertion_index - 1]
    newline = "\r\n" if previous.endswith("\r\n") else "\n"
    lines.insert(
        migration.insertion_index,
        f"{migration.indentation}access_policy: {migration.policy}{newline}",
    )
    _atomic_write(path, "".join(lines).encode("utf-8"), stat.S_IMODE(original_stat.st_mode))
    os.chown(path, original_stat.st_uid, original_stat.st_gid)
    if _plan_file_transfer_policy_migration(path) is not None:
        raise AdminError("file-transfer access policy migration did not validate")


def _describe_file_transfer_policy_migration(
    migration: FileTransferPolicyMigration | None,
) -> list[dict[str, str]]:
    if migration is None:
        return []
    return [{"setting": "file_transfer.access_policy", "value": migration.policy}]


def _prepare_paths(
    source: Path | None,
    legacy_config: LegacyConfigSource | None = None,
) -> None:
    try:
        account = pwd.getpwnam(SERVICE_USER)
    except KeyError:
        _run(
            [
                USERADD,
                "--system",
                "--create-home",
                "--home-dir",
                f"/home/{SERVICE_USER}",
                "--shell",
                "/usr/sbin/nologin",
                SERVICE_USER,
            ]
        )
        account = pwd.getpwnam(SERVICE_USER)
    _ensure_real_directory(CONFIG_DIR, mode=0o750)
    _ensure_real_directory(DATA_DIR, mode=0o750)
    _ensure_real_directory(CACHE_DIR, mode=0o750)
    _ensure_real_directory(BACKUP_DIR, mode=0o700)
    _ensure_real_directory(RUN_DIR, mode=0o750)
    os.chown(CONFIG_DIR, 0, account.pw_gid)
    CONFIG_DIR.chmod(0o750)
    for path in (DATA_DIR, CACHE_DIR, RUN_DIR):
        os.chown(path, account.pw_uid, account.pw_gid)
        path.chmod(0o750)
    home = DATA_DIR
    runtime_directories = (
        home / ".reticulum",
        home / ".config",
        home / ".config/reticulumpi",
        home / ".local",
        home / ".local/share",
        home / ".local/share/reticulumpi",
        home / ".nomadnet",
        home / ".nomadnet-tui",
        home / "meshchat",
    )
    for path in runtime_directories:
        _ensure_real_directory(path, mode=0o750)
        os.chown(path, account.pw_uid, account.pw_gid)
        path.chmod(0o750)
    BACKUP_DIR.chmod(0o700)
    if legacy_config is not None:
        imported = CONFIG_FILE.with_name(
            f".{CONFIG_FILE.name}.legacy-import-{os.getpid()}-{time.time_ns()}"
        )
        try:
            _snapshot_regular_file(
                legacy_config.path,
                imported,
                expected_sha256=legacy_config.sha256,
                mode=0o640,
            )
            if CONFIG_FILE.is_symlink():
                raise AdminError(f"configuration may not be a symlink: {CONFIG_FILE}")
            os.replace(imported, CONFIG_FILE)
            _fsync_directory(CONFIG_DIR)
        finally:
            imported.unlink(missing_ok=True)
    elif not CONFIG_FILE.exists() and source is not None:
        example = source / "config/reticulumpi/config.example.yaml"
        _atomic_copy(example, CONFIG_FILE, 0o640)
    elif CONFIG_FILE.is_symlink():
        raise AdminError(f"configuration may not be a symlink: {CONFIG_FILE}")
    if CONFIG_FILE.exists():
        os.chown(CONFIG_FILE, 0, account.pw_gid)
        CONFIG_FILE.chmod(0o640)
    if source is not None:
        reticulum_config = home / ".reticulum/config"
        legacy_reticulum_config = Path(account.pw_dir) / ".reticulum/config"
        example = source / "config/reticulum/config.example"
        if (
            not reticulum_config.exists()
            and not legacy_reticulum_config.exists()
            and example.is_file()
            and not example.is_symlink()
        ):
            _atomic_copy(example, reticulum_config, 0o600)
            os.chown(reticulum_config, account.pw_uid, account.pw_gid)


def _switch_release(root: Path, release: Path) -> None:
    _ensure_install_root_directory(root)
    fd, raw_temporary = tempfile.mkstemp(prefix=".current.", dir=root)
    os.close(fd)
    temporary = Path(raw_temporary)
    temporary.unlink()
    try:
        temporary.symlink_to(release)
        os.replace(temporary, root / "current")
        _fsync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)


def _ensure_install_root_directory(root: Path) -> None:
    """Create a root-owned release root that the service can traverse.

    ``Path.mkdir(parents=True)`` applies its explicit mode only to the final
    component.  Under the administrator's restrictive umask, creating
    ``<root>/releases`` could therefore leave ``<root>`` at ``0700`` and make
    every otherwise-valid release executable inaccessible to the service
    account.  Normalize the canonical boundary explicitly on every switch so
    fresh installs, upgrades, and rollbacks share the same contract.
    """

    _ensure_real_directory(root, mode=0o755)
    _validate_install_root_ancestry(root)
    root.chmod(0o755)
    if stat.S_IMODE(root.lstat().st_mode) != 0o755:
        raise AdminError(f"install root is not service-traversable: {root}")
    _fsync_directory(root.parent)


def _restore_current(root: Path, previous: Path | None) -> None:
    if previous is None:
        (root / "current").unlink(missing_ok=True)
        _fsync_directory(root)
    else:
        _switch_release(root, previous)


def _validate_root_owned_regular_path(path: Path, label: str) -> None:
    candidate = path.absolute()
    current = Path(candidate.parts[0])
    for index, part in enumerate(candidate.parts[1:], 1):
        current /= part
        try:
            current_stat = current.lstat()
        except OSError as exc:
            raise AdminError(f"{label} path is unavailable: {current}: {exc}") from exc
        if stat.S_ISLNK(current_stat.st_mode):
            raise AdminError(f"{label} path contains a symlink: {current}")
        final = index == len(candidate.parts) - 1
        expected_type = stat.S_ISREG if final else stat.S_ISDIR
        if not expected_type(current_stat.st_mode):
            raise AdminError(f"{label} path has an unsafe type: {current}")
        if current_stat.st_uid != 0 or stat.S_IMODE(current_stat.st_mode) & 0o022:
            raise AdminError(f"{label} path is not root-owned and immutable: {current}")


def _validate_release_immutability(release: Path) -> None:
    """Verify every root-executed broker component is outside non-root control."""

    _validate_install_root_ancestry(release)
    executable = release / ".venv/bin/reticulumpi"
    marker = release / "RELEASE"
    _validate_root_owned_regular_path(executable, "release executable")
    _validate_root_owned_regular_path(marker, "release marker")
    broker_candidates = list(
        (release / ".venv/lib").glob("python*/site-packages/reticulumpi/control_broker.py")
    )
    if len(broker_candidates) != 1:
        raise AdminError("release must contain exactly one immutable control broker module")
    _validate_root_owned_regular_path(broker_candidates[0], "control broker")
    interpreter = release / ".venv/bin/python"
    try:
        interpreter_stat = interpreter.lstat()
    except OSError as exc:
        raise AdminError(
            f"control broker interpreter is unavailable: {interpreter}: {exc}"
        ) from exc
    if stat.S_ISLNK(interpreter_stat.st_mode):
        if interpreter_stat.st_uid != 0:
            raise AdminError(f"control broker interpreter symlink is not root-owned: {interpreter}")
        try:
            resolved_interpreter = interpreter.resolve(strict=True)
        except OSError as exc:
            raise AdminError(
                f"control broker interpreter symlink is invalid: {interpreter}"
            ) from exc
        _validate_root_owned_regular_path(resolved_interpreter, "control broker interpreter")
    else:
        _validate_root_owned_regular_path(interpreter, "control broker interpreter")


def _normalize_release_permissions(release: Path) -> None:
    """Make a root-installed release readable and traversable by the service.

    Installation intentionally runs with a restrictive umask, so permissions
    inherited from ``venv`` and ``pip`` are not a reliable runtime contract.
    Releases contain code and public package data only; secrets live in the
    separately protected configuration and state roots.
    """

    release.chmod(0o755)
    for installed_path in release.rglob("*"):
        if installed_path.is_symlink():
            continue
        installed_stat = installed_path.stat()
        if stat.S_ISDIR(installed_stat.st_mode):
            installed_path.chmod(0o755)
        elif stat.S_ISREG(installed_stat.st_mode):
            executable = bool(stat.S_IMODE(installed_stat.st_mode) & 0o111)
            installed_path.chmod(0o755 if executable else 0o644)
        else:
            raise AdminError(f"release contains an unsupported file type: {installed_path}")


def _validate_release(root: Path, release: Path) -> Path:
    releases = (root / "releases").resolve()
    if release.is_symlink():
        raise AdminError(f"release directory may not be a symlink: {release}")
    resolved = release.resolve()
    if not _is_within(resolved, releases) or not resolved.is_dir():
        raise AdminError(f"release is outside the managed release directory: {release}")
    executable = resolved / ".venv/bin/reticulumpi"
    marker = resolved / "RELEASE"
    if executable.is_symlink() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise AdminError(f"release has no trusted executable: {resolved}")
    if marker.is_symlink() or not marker.is_file():
        raise AdminError(f"release has no RELEASE marker: {resolved}")
    _validate_version(marker.read_text(encoding="utf-8").strip())
    _validate_release_immutability(resolved)
    return resolved


def _prune_releases(root: Path, current: Path, retain: int = 3) -> None:
    releases = root / "releases"
    if not releases.exists():
        return
    candidates = sorted(
        (
            path
            for path in releases.iterdir()
            if path.is_dir() and not path.is_symlink() and path.resolve() != current.resolve()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for expired in candidates[max(0, retain - 1) :]:
        shutil.rmtree(expired)


def _rollback_attempt(errors: list[str], label: str, action) -> None:
    try:
        action()
    except Exception as exc:
        errors.append(f"{label}: {exc}")


def _service_state_snapshot() -> dict[str, dict[str, bool]]:
    return {
        name: {"active": _service_active(name), "enabled": _unit_enabled(name)}
        for name in _TRANSACTION_SERVICE_NAMES
    }


def _validate_service_state_snapshot(raw: object) -> dict[str, dict[str, bool]]:
    expected = set(_TRANSACTION_SERVICE_NAMES)
    if not isinstance(raw, dict) or set(raw) != expected:
        raise AdminError("transaction journal has invalid prior service-state evidence")
    result: dict[str, dict[str, bool]] = {}
    for name, state in raw.items():
        if (
            not isinstance(name, str)
            or not isinstance(state, dict)
            or set(state) != {"active", "enabled"}
            or not isinstance(state["active"], bool)
            or not isinstance(state["enabled"], bool)
        ):
            raise AdminError("transaction journal has invalid prior service-state evidence")
        result[name] = {"active": state["active"], "enabled": state["enabled"]}
    return result


def _restore_service_states(
    states: dict[str, dict[str, bool]],
    features: tuple[str, ...],
    expected_identities: dict[str, str],
    roots: tuple[StateRoot, ...] | None = None,
    config_file: Path | None = None,
    require_readiness: bool = True,
) -> None:
    # Stop everything first so no candidate process observes partially restored
    # files.  Restore enablement independently from runtime state.
    for name in (
        "reticulumpi.service",
        "rnsd-watchdog.timer",
        "rnsd.service",
        "reticulumpi-control.socket",
    ):
        _run([SYSTEMCTL, "stop", name], check=False)
        _wait_service_inactive(name)
    for name, state in states.items():
        _run([SYSTEMCTL, "enable" if state["enabled"] else "disable", name], check=False)
    mismatched_enablement = [
        name for name, state in states.items() if _unit_enabled(name) is not state["enabled"]
    ]
    if mismatched_enablement:
        raise AdminError(
            "could not restore prior unit enablement: " + ", ".join(sorted(mismatched_enablement))
        )
    if states["reticulumpi-control.socket"]["active"]:
        _run([SYSTEMCTL, "start", "reticulumpi-control.socket"])
        _wait_service_active("reticulumpi-control.socket")
    if states["rnsd.service"]["active"]:
        _run([SYSTEMCTL, "restart", "rnsd.service"])
        _wait_service_active("rnsd.service")
    if states["reticulumpi.service"]["active"]:
        verification_roots = _state_roots(features) if roots is None else roots
        if require_readiness:
            _activate_required_features("start", features)
        else:
            _run([SYSTEMCTL, "start", "reticulumpi.service"])
            _wait_service_active("reticulumpi.service")
        _verify_identity_continuity(
            expected_identities,
            _identity_hashes(verification_roots, config_file),
        )
    if states["rnsd-watchdog.timer"]["active"]:
        _run([SYSTEMCTL, "start", "rnsd-watchdog.timer"])
        _wait_service_active("rnsd-watchdog.timer")


def _journal_state() -> tuple[dict[str, object] | None, bool]:
    if JOURNAL_FILE.parent.exists() or JOURNAL_FILE.parent.is_symlink():
        _reject_symlink_components(JOURNAL_FILE.parent)
        directory_stat = JOURNAL_FILE.parent.lstat()
        expected_uid = 0 if os.geteuid() == 0 else os.geteuid()
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != expected_uid
            or stat.S_IMODE(directory_stat.st_mode) & 0o077
        ):
            raise AdminError(
                "transaction evidence directory ownership or permissions are unsafe: "
                f"{JOURNAL_FILE.parent}"
            )
    if not (JOURNAL_FILE.exists() or JOURNAL_FILE.is_symlink()):
        return None, False
    if JOURNAL_FILE.is_symlink() or not JOURNAL_FILE.is_file():
        raise AdminError(f"transaction journal is missing or unsafe: {JOURNAL_FILE}")
    journal_stat = JOURNAL_FILE.stat()
    if journal_stat.st_uid not in {0, os.geteuid()} or stat.S_IMODE(journal_stat.st_mode) & 0o077:
        raise AdminError(f"transaction journal ownership or permissions are unsafe: {JOURNAL_FILE}")
    journal = _read_json_object(JOURNAL_FILE, "transaction journal")
    state = journal.get("state")
    if not isinstance(state, str):
        raise AdminError("transaction journal has no valid state")
    return journal, state not in _TERMINAL_TRANSACTION_STATES


def _recover_interrupted_transaction(expected_root: Path | None = None) -> dict[str, object] | None:
    """Restore a power-interrupted apply transaction from durable evidence.

    Recovery is deliberately fail closed: once switching could have begun, a
    missing backup, unit snapshot, or service-state record prevents any new
    mutation rather than guessing which half of the transaction is live.
    """

    journal, unfinished = _journal_state()
    if journal is None or not unfinished:
        return journal
    if journal.get("schema") != 1:
        raise AdminError("unfinished transaction journal has an unsupported schema")
    state = journal.get("state")
    if state not in {"preparing", "backed_up", "switching"}:
        raise AdminError(f"unfinished transaction journal has an unknown state: {state!r}")
    root = _safe_install_root(str(journal.get("install_root", "")))
    if expected_root is not None and root != expected_root:
        raise AdminError(
            f"unfinished transaction belongs to {root}, not requested install root {expected_root}"
        )
    releases = (root / "releases").resolve()

    previous_raw = journal.get("previous_release")
    previous: Path | None
    if previous_raw is None:
        previous = None
    elif isinstance(previous_raw, str):
        previous = Path(previous_raw).expanduser().resolve()
        if (
            not _is_within(previous, releases)
            or previous.parent != releases
            or not previous.is_dir()
        ):
            raise AdminError("unfinished transaction has an invalid previous release")
    else:
        raise AdminError("unfinished transaction has invalid previous-release evidence")
    new_raw = journal.get("new_release")
    if not isinstance(new_raw, str):
        raise AdminError("unfinished transaction has no candidate-release evidence")
    requested_new_release = Path(new_raw).expanduser()
    if not requested_new_release.is_absolute():
        raise AdminError("unfinished transaction candidate is not absolute")
    if os.path.lexists(requested_new_release) and requested_new_release.is_symlink():
        raise AdminError("unfinished transaction candidate is an unsafe symlink")
    new_release = requested_new_release.resolve()
    if not _is_within(new_release, releases) or new_release.parent != releases:
        raise AdminError("unfinished transaction candidate escapes the release directory")

    backup_raw = journal.get("backup")
    current = root / "current"
    if backup_raw is None:
        if state != "preparing":
            raise AdminError("post-backup transaction is missing durable backup evidence")
        # No durable state was mutated before the backup pointer was committed.
        # A changed release pointer contradicts that evidence and is not safe to
        # infer around.
        active = current.resolve() if current.is_symlink() else None
        if active != previous:
            raise AdminError(
                "unfinished pre-backup transaction has a current/previous mismatch; "
                "manual recovery is required"
            )
        states = _validate_service_state_snapshot(journal.get("services_before"))
        legacy_bridge_raw = journal.get("legacy_bridge", False)
        if not isinstance(legacy_bridge_raw, bool):
            raise AdminError("unfinished transaction has invalid legacy-bridge evidence")
        features = _backup_features(journal)
        if MANIFEST_FILE.exists() or MANIFEST_FILE.is_symlink():
            manifest = _load_manifest(root)
            features = tuple(manifest["features"])
        roots = _state_roots(features)
        legacy_config = _discover_legacy_config_source() if legacy_bridge_raw else None
        config_file = legacy_config.path if legacy_config is not None else None
        identities = _identity_hashes(roots, config_file)
        _restore_service_states(
            states,
            features,
            identities,
            roots,
            config_file,
            require_readiness=not legacy_bridge_raw,
        )
    else:
        if not isinstance(backup_raw, str):
            raise AdminError("unfinished transaction has invalid backup evidence")
        requested_backup = Path(backup_raw).expanduser()
        if not requested_backup.is_absolute():
            raise AdminError("unfinished transaction backup path is not absolute")
        _reject_symlink_components(requested_backup)
        backup = requested_backup.resolve()
        backup_root = BACKUP_DIR.resolve()
        if not _is_within(backup, backup_root) or not backup.name.startswith(
            _RELEASE_BACKUP_PREFIX
        ):
            raise AdminError("unfinished transaction backup is outside the managed backup root")
        if backup.is_symlink() or not backup.is_dir():
            raise AdminError("unfinished transaction backup is missing or unsafe")
        states = _validate_service_state_snapshot(journal.get("services_before"))
        snapshots = _load_file_snapshots(backup)
        for name in ("reticulumpi.service", "rnsd-watchdog.timer", "rnsd.service"):
            _run([SYSTEMCTL, "stop", name], check=False)
        _restore_files(snapshots)
        _restore_current(root, previous)
        _restore_state_backup(backup)
        _run([SYSTEMCTL, "daemon-reload"])
        restored_manifest: dict[str, object] | None = None
        features: tuple[str, ...] = ()
        if MANIFEST_FILE.exists() or MANIFEST_FILE.is_symlink():
            restored_manifest = _load_manifest(root)
            features = tuple(restored_manifest["features"])
            if previous is None or Path(restored_manifest["release"]) != previous:
                raise AdminError("recovered manifest and release pointer do not agree")
        elif previous is not None:
            raise AdminError("recovered installation manifest is missing")
        backup_metadata = _read_json_object(backup / "backup.json", "backup metadata")
        raw_identities = backup_metadata.get("identity_hashes", {})
        if not isinstance(raw_identities, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in raw_identities.items()
        ):
            raise AdminError("unfinished transaction backup has invalid identity evidence")
        backup_features = _backup_features(backup_metadata)
        if restored_manifest is None:
            features = backup_features
        elif tuple(sorted(features)) != backup_features:
            raise AdminError("recovered manifest and backup feature evidence do not agree")
        _restore_service_states(
            states,
            features,
            dict(raw_identities),
            _backup_roots_from_metadata(backup_metadata),
            _backup_configuration_file(backup_metadata),
            require_readiness=restored_manifest is not None,
        )

    if (
        journal.get("remove_candidate", True) is not False
        and os.path.lexists(new_release)
        and new_release != previous
    ):
        if new_release.is_symlink() or not new_release.is_dir():
            raise AdminError("interrupted candidate release is unsafe and was not removed")
        shutil.rmtree(new_release)
        _fsync_directory(new_release.parent)
    journal["state"] = "recovered"
    journal["recovered_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    journal["recovery_evidence"] = {
        "release": str(previous) if previous else None,
        "backup": backup_raw,
        "services_restored": True,
    }
    _atomic_json(JOURNAL_FILE, journal, 0o600)
    return journal


def _apply_release_materialized(args: argparse.Namespace, operation: str) -> int:
    root = _safe_install_root(args.install_root)
    platform_profile: dict[str, object] | None = None
    if args.apply:
        _require_root()
        platform_profile = _preflight_platform() or None
        with _maintenance_lock():
            _recover_interrupted_transaction(root)
    bundle = Path(args.bundle).expanduser().resolve()
    version, source = _source_metadata(bundle)
    _validate_bundle_location(bundle, source, root)
    if operation == "install" and source is None:
        raise AdminError("a first install requires a complete source bundle, not only a wheel")

    installed_manifest: dict | None = None
    if MANIFEST_FILE.exists() or MANIFEST_FILE.is_symlink():
        installed_manifest = _load_manifest(root)
    requested_features = tuple(sorted(set(args.feature)))
    if operation == "upgrade" and not requested_features and installed_manifest is not None:
        requested_features = tuple(installed_manifest["features"])
    features = requested_features
    previous_features = (
        tuple(installed_manifest["features"]) if installed_manifest is not None else ()
    )
    transaction_features = tuple(sorted(set(features) | set(previous_features)))
    _extras(features)
    _verify_bundle(bundle, source)
    dependency_profile = _dependency_profile_path(bundle, source, features)
    legacy_config = _discover_legacy_config_source()
    planning_config = legacy_config.path if legacy_config is not None else CONFIG_FILE

    current = root / "current"
    if os.path.lexists(current):
        if not current.is_symlink():
            raise AdminError(f"current release pointer is not a symlink: {current}")
        previous = _validate_release(root, current.resolve())
    else:
        previous = None
    legacy_bridge = operation == "upgrade" and installed_manifest is None and previous is None
    if source is None and (installed_manifest is None or previous is None):
        raise AdminError(
            "a wheel-only upgrade cannot bridge a mutable installation; "
            "install.json and the current release pointer are both required"
        )
    if installed_manifest is not None:
        recorded = Path(installed_manifest["release"])
        if previous is None or recorded != previous:
            raise AdminError("installation manifest and current release pointer disagree")
    releases = root / "releases"
    release = releases / version
    if not _is_within(release.resolve(), releases.resolve()):
        raise AdminError(f"release path escapes install root: {release}")
    if legacy_bridge:
        _validate_legacy_bridge_features(features, planning_config, None)
    planned_path_migration = _plan_legacy_config_path_migration(planning_config)
    planned_meshchat_migration = _plan_meshchat_storage_path_migration(planning_config)
    planned_config_migration = _plan_file_transfer_policy_migration(planning_config)
    planned_credential_migration = _plan_dashboard_credential_migration(
        source_replaces_unit=source is not None,
        path=planning_config,
    )
    planned_credential_dropins = _dashboard_credential_dropins()
    planned_unsafe_dropins = _unsafe_legacy_dropins() if source is not None else ()
    planned_noncredential_unsafe = tuple(
        path for path in planned_unsafe_dropins if path not in set(planned_credential_dropins)
    )
    signed_wheel_digest = _signed_release_wheel_digest(bundle, source)
    already_current = bool(
        installed_manifest is not None
        and previous == release
        and installed_manifest["version"] == version
        and tuple(installed_manifest["features"]) == features
        and signed_wheel_digest is not None
        and installed_manifest.get("bundle_sha256") == signed_wheel_digest
        and planned_path_migration is None
        and planned_meshchat_migration is None
        and planned_config_migration is None
        and planned_credential_migration is None
        and not planned_credential_dropins
        and not planned_unsafe_dropins
    )
    if operation == "install" and installed_manifest is not None and not already_current:
        raise AdminError("ReticulumPi is already installed; use the upgrade command")
    summary = {
        "operation": operation,
        "bundle": str(getattr(args, "bundle_origin", bundle)),
        "version": version,
        "features": features,
        "install_root": str(root),
        "previous_release": str(previous) if previous else None,
        "new_release": str(release),
        "dependency_profile": _dependency_profile_name(features),
        "platform_profile": platform_profile,
        "legacy_bridge": legacy_bridge,
        "legacy_config_source": str(legacy_config.path) if legacy_config is not None else None,
        "configuration_migrations": [
            *_describe_legacy_config_path_migration(planned_path_migration),
            *_describe_meshchat_storage_path_migration(planned_meshchat_migration),
            *_describe_file_transfer_policy_migration(planned_config_migration),
            *_describe_dashboard_credential_migration(planned_credential_migration),
            *_describe_dashboard_credential_dropins(planned_credential_dropins),
            *_describe_unsafe_legacy_dropins(planned_noncredential_unsafe),
        ],
        "already_current": already_current,
    }
    if planned_config_migration is not None and planned_config_migration.policy == "open":
        print(_OPEN_FILE_TRANSFER_WARNING, file=sys.stderr)
    if not args.apply:
        print(json.dumps(summary, indent=2))
        print("Dry run only; rerun with --apply to change the system.")
        return 0

    if already_current:
        print(f"ReticulumPi {version} is already installed from this signed artifact")
        return 0

    if _unsigned_development_mode():
        raise AdminError("unsigned development bundles can never be applied")
    if os.path.lexists(release):
        raise AdminError(f"release already exists: {release}")
    _ensure_install_space(root, bundle)

    with _maintenance_lock():
        _recover_interrupted_transaction(root)
        _validate_install_root_ancestry(root)
        service_states = _service_state_snapshot()
        if legacy_bridge:
            _validate_legacy_bridge_features(features, planning_config, service_states)
        was_active = service_states["reticulumpi.service"]["active"]
        app_was_enabled = service_states["reticulumpi.service"]["enabled"]
        rnsd_was_active = service_states["rnsd.service"]["active"]
        rnsd_was_enabled = service_states["rnsd.service"]["enabled"]
        watchdog_was_active = service_states["rnsd-watchdog.timer"]["active"]
        watchdog_was_enabled = service_states["rnsd-watchdog.timer"]["enabled"]
        control_was_active = service_states["reticulumpi-control.socket"]["active"]
        control_was_enabled = service_states["reticulumpi-control.socket"]["enabled"]
        backup: Path | None = None
        identity_before: dict[str, str] = {}
        identity_after: dict[str, str] = {}
        transaction_roots: tuple[StateRoot, ...] = ()
        legacy_migrations: tuple[LegacyMigration, ...] = ()
        snapshots: tuple[FileSnapshot, ...] = ()
        live_credential_dropins: tuple[Path, ...] = ()
        live_unsafe_dropins: tuple[Path, ...] = ()
        live_dropins_to_remove: tuple[Path, ...] = ()
        bridge_roots: tuple[dict[str, str], ...] = ()
        switched = False
        activation_attempted = False
        journal: dict[str, object] = {
            **summary,
            "schema": 1,
            "backup": None,
            "remove_candidate": True,
            "services_before": service_states,
            "state": "preparing",
        }
        # This is the transaction's first durable mutation.  Candidate release
        # creation, virtualenv/package installation, path preparation, and
        # configuration changes must all be recoverable from this record.
        _ensure_journal_directory()
        _atomic_json(JOURNAL_FILE, journal, 0o600)
        staging = Path(tempfile.mkdtemp(prefix="reticulumpi-admin-")).resolve()
        staging.chmod(0o700)
        try:
            _ensure_install_root_directory(root)
            _ensure_real_directory(releases)
            if source is not None:
                source = _stage_verified_source(source, staging)
                dependency_profile = _dependency_profile_path(bundle, source, features)
            staged_profile = _stage_dependency_profile(dependency_profile, staging)
            profile_sha256 = _sha256(staged_profile)
            wheel = _build_wheel(bundle, source, staging)
            wheel_sha256 = _validate_wheel(wheel, version, features)
            release.mkdir(parents=True, mode=0o755)
            _run([sys.executable, "-m", "venv", str(release / ".venv")])
            pip = str(release / ".venv/bin/pip")
            dependency_command = [pip, "install", "--no-cache-dir"]
            if _dependency_profile_name(features) != "all-features":
                dependency_command.extend(("--only-binary", ":all:"))
            dependency_command.extend(("--require-hashes", "--requirement", str(staged_profile)))
            _require_unchanged_digest(
                staged_profile,
                profile_sha256,
                "dependency profile",
            )
            _run(dependency_command)
            _require_unchanged_digest(wheel, wheel_sha256, "release wheel")
            _run([pip, "install", "--no-cache-dir", "--no-deps", str(wheel)])
            _run([pip, "check"])
            _atomic_write(release / "RELEASE", f"{version}\n".encode(), 0o644)
            _normalize_release_permissions(release)
            _validate_release_immutability(release)
            if source is None and not CONFIG_FILE.exists():
                raise AdminError("wheel-only install requires an existing system configuration")
            live_credential_dropins = _dashboard_credential_dropins()
            live_unsafe_dropins = _unsafe_legacy_dropins() if source is not None else ()
            live_dropins_to_remove = tuple(
                sorted(set(live_credential_dropins) | set(live_unsafe_dropins))
            )
            external_config_paths = {legacy_config.path} if legacy_config is not None else set()
            snapshot_paths = tuple(
                sorted(
                    (set(_managed_paths()) if source is not None else set())
                    | set(live_dropins_to_remove)
                    | external_config_paths
                )
            )
            snapshots = _snapshot_files(snapshot_paths)
            if watchdog_was_active:
                _run([SYSTEMCTL, "stop", "rnsd-watchdog.timer"])
            if was_active:
                _run([SYSTEMCTL, "stop", "reticulumpi.service"])
                _wait_service_inactive("reticulumpi.service")
            if rnsd_was_active:
                _run([SYSTEMCTL, "stop", "rnsd.service"])
                _wait_service_inactive("rnsd.service")
            backup = _backup_state(
                version,
                transaction_features,
                config_file=legacy_config.path if legacy_config is not None else None,
                external_config_file=(legacy_config.path if legacy_config is not None else None),
            )
            _persist_file_snapshots(backup, snapshots)
            if legacy_bridge:
                bridge_roots = _persist_legacy_bridge_evidence(backup, service_states)
            journal["backup"] = str(backup)
            journal["state"] = "backed_up"
            _atomic_json(JOURNAL_FILE, journal, 0o600)
            if source is not None:
                if legacy_config is None:
                    _prepare_paths(source)
                else:
                    _prepare_paths(source, legacy_config)
            backup_metadata = _read_json_object(backup / "backup.json", "backup metadata")
            raw_identity_before = backup_metadata.get("identity_hashes", {})
            if not isinstance(raw_identity_before, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in raw_identity_before.items()
            ):
                raise AdminError("transaction backup identity hashes are invalid")
            identity_before = dict(raw_identity_before)
            transaction_roots = _state_roots(transaction_features)
            live_path_migration = _plan_legacy_config_path_migration()
            if live_path_migration is not None:
                _apply_legacy_config_path_migration(live_path_migration)
            live_meshchat_migration = _plan_meshchat_storage_path_migration()
            if live_meshchat_migration is not None:
                _apply_meshchat_storage_path_migration(live_meshchat_migration)
            live_config_migration = _plan_file_transfer_policy_migration()
            if live_config_migration is not None:
                if live_config_migration.policy == "open" and (
                    planned_config_migration is None or planned_config_migration.policy != "open"
                ):
                    print(_OPEN_FILE_TRANSFER_WARNING, file=sys.stderr)
                _apply_file_transfer_policy_migration(live_config_migration)
            legacy_migrations = _migrate_legacy_home_state(transaction_features)
            live_credential_migration = _plan_dashboard_credential_migration(
                source_replaces_unit=source is not None
            )
            if live_credential_migration is not None:
                _apply_dashboard_credential_migration(live_credential_migration)
            _remove_legacy_dropins(live_dropins_to_remove, snapshots)
            journal["identity_hashes_before"] = identity_before
            journal["legacy_migrations"] = [
                {"source": str(item.source), "destination": str(item.destination)}
                for item in legacy_migrations
            ]
            journal["configuration_migrations"] = [
                *_describe_legacy_config_path_migration(live_path_migration),
                *_describe_meshchat_storage_path_migration(live_meshchat_migration),
                *_describe_file_transfer_policy_migration(live_config_migration),
                *_describe_dashboard_credential_migration(live_credential_migration),
                *_describe_dashboard_credential_dropins(live_credential_dropins),
                *_describe_unsafe_legacy_dropins(
                    tuple(
                        path
                        for path in live_unsafe_dropins
                        if path not in set(live_credential_dropins)
                    )
                ),
            ]
            journal["state"] = "switching"
            _atomic_json(JOURNAL_FILE, journal, 0o600)
            _switch_release(root, release)
            switched = True
            if source is not None:
                _render_units(source, root, features, previous_features)
                _install_helpers(source, features, previous_features)
                _remove_legacy_sudoers()
            (
                retained_bridge_backup,
                retained_bridge_roots,
                retained_bridge_services,
            ) = _retained_legacy_bridge_evidence(
                installed_manifest,
                legacy_bridge=legacy_bridge,
                backup=backup,
                bridge_roots=bridge_roots,
                service_states=service_states,
            )
            manifest = InstallManifest(
                schema=1,
                version=version,
                install_root=str(root),
                release=str(release),
                previous_release=str(previous) if previous else None,
                features=features,
                installed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                bundle_sha256=wheel_sha256,
                legacy_bridge_backup=retained_bridge_backup,
                legacy_bridge_roots=retained_bridge_roots,
                legacy_bridge_services=retained_bridge_services,
                platform_profile=platform_profile,
            )
            _atomic_json(MANIFEST_FILE, asdict(manifest))
            _run([SYSTEMCTL, "daemon-reload"])
            _run([SYSTEMCTL, "enable", "reticulumpi.service"])
            _run([SYSTEMCTL, "enable", "--now", "reticulumpi-control.socket"])
            if {"nomadnet", "shared-rnsd"} & set(features):
                _run([SYSTEMCTL, "enable", "rnsd.service"])
                if rnsd_was_active or was_active or args.start:
                    _restart_and_wait("rnsd.service")
            if "watchdog" in features:
                _run([SYSTEMCTL, "enable", "--now", "rnsd-watchdog.timer"])
            elif watchdog_was_enabled:
                _run([SYSTEMCTL, "disable", "--now", "rnsd-watchdog.timer"])
            if was_active or args.start:
                activation_attempted = True
                _activate_required_features("restart", features)
                identity_after = _identity_hashes(transaction_roots)
            else:
                identity_after = _identity_hashes(transaction_roots)
            journal["identity_hashes_after"] = identity_after
            _atomic_json(JOURNAL_FILE, journal, 0o600)
            _verify_identity_continuity(identity_before, identity_after)
            _verify_migrated_identities(legacy_migrations)
            if backup is None:  # defensive: activation is always post-backup
                raise AdminError("candidate activation has no transaction backup")
            journal["sqlite_validation"] = _validate_live_sqlite_state(
                backup,
                transaction_roots,
                legacy_migrations,
            )
            _atomic_json(JOURNAL_FILE, journal, 0o600)
            _remove_migrated_legacy_state(legacy_migrations)
            _verify_migrated_identities(legacy_migrations)
            journal["state"] = "complete"
            _atomic_json(JOURNAL_FILE, journal, 0o600)
        except BaseException as original_error:
            rollback_errors: list[str] = []
            journal["failure"] = {
                "type": type(original_error).__name__,
                "reason": str(original_error),
            }
            if switched or activation_attempted or _service_active("reticulumpi.service"):
                _rollback_attempt(
                    rollback_errors,
                    "stop failed candidate",
                    lambda: _run([SYSTEMCTL, "stop", "reticulumpi.service"], check=False),
                )
            if switched:
                _rollback_attempt(
                    rollback_errors,
                    "restore release pointer",
                    lambda: _restore_current(root, previous),
                )
            if snapshots:
                _rollback_attempt(
                    rollback_errors,
                    "restore managed files",
                    lambda: _restore_files(snapshots),
                )
            if backup is not None:
                _rollback_attempt(
                    rollback_errors,
                    "restore configuration and data",
                    lambda: _restore_state_backup(backup),
                )
            _rollback_attempt(
                rollback_errors,
                "reload restored units",
                lambda: _run([SYSTEMCTL, "daemon-reload"], check=False),
            )
            if not app_was_enabled:
                _rollback_attempt(
                    rollback_errors,
                    "restore app enablement",
                    lambda: _run([SYSTEMCTL, "disable", "reticulumpi.service"], check=False),
                )
            if not rnsd_was_enabled:
                _rollback_attempt(
                    rollback_errors,
                    "disable newly managed rnsd",
                    lambda: _run([SYSTEMCTL, "disable", "--now", "rnsd.service"], check=False),
                )
            elif rnsd_was_active:
                _rollback_attempt(
                    rollback_errors,
                    "restart previous rnsd",
                    lambda: _restart_and_wait("rnsd.service"),
                )
            else:
                _rollback_attempt(
                    rollback_errors,
                    "restore stopped rnsd state",
                    lambda: _run([SYSTEMCTL, "stop", "rnsd.service"], check=False),
                )
            if not watchdog_was_enabled:
                _rollback_attempt(
                    rollback_errors,
                    "disable newly managed watchdog",
                    lambda: _run(
                        [SYSTEMCTL, "disable", "--now", "rnsd-watchdog.timer"],
                        check=False,
                    ),
                )
            elif watchdog_was_active:
                _rollback_attempt(
                    rollback_errors,
                    "restart previous watchdog",
                    lambda: _run([SYSTEMCTL, "start", "rnsd-watchdog.timer"], check=False),
                )
            else:
                _rollback_attempt(
                    rollback_errors,
                    "restore stopped watchdog state",
                    lambda: _run([SYSTEMCTL, "stop", "rnsd-watchdog.timer"], check=False),
                )
            if not control_was_enabled:
                _rollback_attempt(
                    rollback_errors,
                    "disable newly managed control socket",
                    lambda: _run(
                        [SYSTEMCTL, "disable", "--now", "reticulumpi-control.socket"],
                        check=False,
                    ),
                )
            elif control_was_active:
                _rollback_attempt(
                    rollback_errors,
                    "restart previous control socket",
                    lambda: _run([SYSTEMCTL, "start", "reticulumpi-control.socket"], check=False),
                )
            else:
                _rollback_attempt(
                    rollback_errors,
                    "restore stopped control socket state",
                    lambda: _run([SYSTEMCTL, "stop", "reticulumpi-control.socket"], check=False),
                )
            if was_active:
                _rollback_attempt(
                    rollback_errors,
                    "restart previous application",
                    lambda: (
                        _activate_legacy_and_verify_identities(
                            identity_before,
                            transaction_roots,
                            legacy_config.path if legacy_config is not None else None,
                        )
                        if legacy_bridge
                        else _activate_and_verify_identities(
                            "restart",
                            identity_before,
                            transaction_roots,
                            previous_features,
                        )
                    ),
                )
            shutil.rmtree(release, ignore_errors=True)
            journal["state"] = "rolled_back"
            if rollback_errors:
                journal["rollback_errors"] = rollback_errors
            _rollback_attempt(
                rollback_errors,
                "write rollback journal",
                lambda: _atomic_json(JOURNAL_FILE, journal, 0o600),
            )
            if rollback_errors:
                raise AdminError(
                    "upgrade failed and rollback was incomplete: " + "; ".join(rollback_errors)
                ) from original_error
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    _prune_releases(root, release)
    print(f"Installed ReticulumPi {version} at {release}")
    return 0


def _apply_release(args: argparse.Namespace, operation: str) -> int:
    requested = Path(args.bundle).expanduser()
    if not requested.is_absolute():
        requested = requested.absolute()
    try:
        requested_stat = requested.lstat()
    except OSError:
        requested_stat = None
    if requested_stat is not None and stat.S_ISDIR(requested_stat.st_mode):
        _validate_bundle_location(
            requested,
            requested,
            _safe_install_root(args.install_root),
        )
    with _materialize_install_bundle(requested) as materialized:
        effective = argparse.Namespace(**vars(args))
        effective.bundle = str(materialized)
        effective.bundle_origin = str(requested)
        return _apply_release_materialized(effective, operation)


def _legacy_bridge_backup_evidence(
    manifest: dict[str, object],
) -> tuple[
    Path,
    dict[str, object],
    dict[str, dict[str, bool]],
    tuple[StateRoot, ...],
    tuple[str, ...],
    Path | None,
    dict[str, str],
]:
    raw_backup = manifest.get("legacy_bridge_backup")
    if not isinstance(raw_backup, str):
        raise AdminError("no mutable legacy predecessor is recorded")
    backup = Path(raw_backup).resolve()
    if backup.is_symlink() or not backup.is_dir():
        raise AdminError(f"legacy bridge backup is missing or unsafe: {backup}")
    metadata = _read_json_object(backup / "backup.json", "legacy bridge backup metadata")
    raw_bridge = metadata.get("legacy_bridge")
    if not isinstance(raw_bridge, dict):
        raise AdminError("legacy bridge backup has no predecessor evidence")
    services = _validate_service_state_snapshot(raw_bridge.get("services_before"))
    manifest_services = _validate_service_state_snapshot(manifest.get("legacy_bridge_services"))
    if services != manifest_services:
        raise AdminError("legacy bridge service-state evidence does not match install.json")
    roots = _backup_roots_from_metadata(metadata)
    backup_root_evidence = tuple(
        {"name": root.name, "path": str(root.path.resolve())} for root in roots
    )
    bridge_root_evidence = _validate_backup_root_evidence(raw_bridge.get("state_roots"))
    manifest_root_evidence = _validate_backup_root_evidence(manifest.get("legacy_bridge_roots"))
    if (
        backup_root_evidence != bridge_root_evidence
        or backup_root_evidence != manifest_root_evidence
    ):
        raise AdminError("legacy bridge root-path evidence does not agree")
    features = _backup_features(metadata)
    config_file = _backup_configuration_file(metadata)
    raw_identities = metadata.get("identity_hashes")
    if not isinstance(raw_identities, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw_identities.items()
    ):
        raise AdminError("legacy bridge backup has invalid identity evidence")
    _load_file_snapshots(backup)
    return (
        backup,
        metadata,
        services,
        roots,
        features,
        config_file,
        dict(raw_identities),
    )


def _rollback_legacy(
    args: argparse.Namespace,
    manifest: dict[str, object],
    root: Path,
    current: Path,
) -> int:
    (
        legacy_backup,
        _legacy_metadata,
        legacy_services,
        legacy_roots,
        legacy_features,
        legacy_config,
        legacy_identities,
    ) = _legacy_bridge_backup_evidence(manifest)
    if not args.apply:
        print(f"Would stop the immutable release at {current}.")
        print(f"Would restore mutable predecessor state from {legacy_backup}.")
        print("Would restore captured systemd units, helpers, and legacy sudoers files.")
        print(f"Would remove {root / 'current'} and {MANIFEST_FILE}.")
        print("Would restore the predecessor's exact enabled and active service states.")
        print("The immutable release and bridge backup would be retained.")
        print("Dry run only; rerun with --apply to change the system.")
        return 0

    with _maintenance_lock():
        _recover_interrupted_transaction(root)
        locked_manifest = _load_manifest(root)
        if locked_manifest != manifest:
            raise AdminError("installation manifest changed before legacy rollback")
        current_pointer = root / "current"
        if not current_pointer.is_symlink() or current_pointer.resolve() != current:
            raise AdminError("current release changed before legacy rollback")
        current_services = _service_state_snapshot()
        current_features = tuple(manifest.get("features", ()))
        current_roots = _state_roots(current_features)
        current_identities = _identity_hashes(current_roots)
        candidate_roots = _merge_state_roots(current_roots, legacy_roots)
        external_config_paths = {legacy_config} if legacy_config is not None else set()
        current_credential_dropins = _dashboard_credential_dropins()
        current_unsafe_dropins = _unsafe_legacy_dropins()
        current_dropins_to_remove = tuple(
            sorted(set(current_credential_dropins) | set(current_unsafe_dropins))
        )
        current_snapshots = _snapshot_files(
            tuple(
                sorted(
                    set(_managed_paths()) | set(current_dropins_to_remove) | external_config_paths
                )
            )
        )
        candidate_backup: Path | None = None
        journal: dict[str, object] = {
            "schema": 1,
            "operation": "rollback-legacy",
            "install_root": str(root),
            "version": manifest["version"],
            "features": current_features,
            "previous_release": str(current),
            # Generic power-loss recovery restores the immutable candidate.
            "new_release": str(current),
            "remove_candidate": False,
            "backup": None,
            "services_before": current_services,
            "legacy_bridge_backup": str(legacy_backup),
            "state": "preparing",
        }
        _atomic_json(JOURNAL_FILE, journal, 0o600)
        try:
            for name in (
                "rnsd-watchdog.timer",
                "reticulumpi.service",
                "rnsd.service",
                "reticulumpi-control.socket",
            ):
                if current_services[name]["active"]:
                    _run([SYSTEMCTL, "stop", name])
                    _wait_service_inactive(name)
            candidate_backup = _backup_state(
                str(manifest["version"]),
                current_features,
                exact_roots=candidate_roots,
                external_config_file=legacy_config,
            )
            _persist_file_snapshots(candidate_backup, current_snapshots)
            journal["backup"] = str(candidate_backup)
            journal["state"] = "backed_up"
            _atomic_json(JOURNAL_FILE, journal, 0o600)

            _remove_legacy_dropins(current_dropins_to_remove, current_snapshots)
            _restore_files(_load_file_snapshots(legacy_backup))
            _restore_current(root, None)
            _restore_state_backup(legacy_backup)
            if MANIFEST_FILE.exists() or MANIFEST_FILE.is_symlink():
                raise AdminError("legacy backup unexpectedly restored an installation manifest")
            _run([SYSTEMCTL, "daemon-reload"])
            _restore_service_states(
                legacy_services,
                legacy_features,
                legacy_identities,
                legacy_roots,
                legacy_config,
                require_readiness=False,
            )
            journal["state"] = "complete"
            journal["restored_legacy_backup"] = str(legacy_backup)
            _atomic_json(JOURNAL_FILE, journal, 0o600)
        except BaseException as original_error:
            errors: list[str] = []
            journal["failure"] = {
                "type": type(original_error).__name__,
                "reason": str(original_error),
            }
            for name in _TRANSACTION_SERVICE_NAMES:
                _rollback_attempt(
                    errors,
                    f"stop restored legacy service {name}",
                    lambda name=name: _run([SYSTEMCTL, "stop", name], check=False),
                )
            _rollback_attempt(
                errors,
                "restore immutable managed files",
                lambda: _restore_files(current_snapshots),
            )
            _rollback_attempt(
                errors,
                "restore immutable release pointer",
                lambda: _switch_release(root, current),
            )
            if candidate_backup is not None:
                _rollback_attempt(
                    errors,
                    "restore immutable configuration and state",
                    lambda: _restore_state_backup(candidate_backup),
                )
            _rollback_attempt(
                errors,
                "restore immutable installation manifest",
                lambda: _atomic_json(MANIFEST_FILE, manifest),
            )
            _rollback_attempt(
                errors,
                "reload immutable units",
                lambda: _run([SYSTEMCTL, "daemon-reload"], check=False),
            )
            _rollback_attempt(
                errors,
                "restore immutable service states",
                lambda: _restore_service_states(
                    current_services,
                    current_features,
                    current_identities,
                    current_roots,
                ),
            )
            journal["state"] = "rolled_back"
            if errors:
                journal["rollback_errors"] = errors
            _rollback_attempt(
                errors,
                "write legacy rollback journal",
                lambda: _atomic_json(JOURNAL_FILE, journal, 0o600),
            )
            if errors:
                raise AdminError(
                    "legacy rollback failed and immutable restoration was incomplete: "
                    + "; ".join(errors)
                ) from original_error
            raise
    print(f"Restored mutable legacy predecessor from {legacy_backup}")
    return 0


def _rollback(args: argparse.Namespace) -> int:
    if args.apply:
        _require_root()
        _preflight_platform()
        with _maintenance_lock():
            _recover_interrupted_transaction()
    manifest = _load_manifest()
    original_manifest = dict(manifest)
    root = _safe_install_root(manifest["install_root"])
    requested_target = args.to or manifest.get("previous_release")
    if not requested_target:
        raise AdminError("no previous release is recorded; provide --to VERSION or --to legacy")
    current_pointer = root / "current"
    if not current_pointer.is_symlink():
        raise AdminError(f"current release pointer is missing or invalid: {current_pointer}")
    current = _validate_release(root, current_pointer.resolve())
    if requested_target == "legacy":
        return _rollback_legacy(args, manifest, root, current)
    target = Path(str(requested_target))
    if not target.is_absolute():
        target = root / "releases" / target
    target = _validate_release(root, target)
    if target == current:
        raise AdminError(f"rollback target is already active: {target}")
    if not args.apply:
        print(f"Would switch {root / 'current'} to {target}")
        print("Dry run only; rerun with --apply to change the system.")
        return 0
    with _maintenance_lock():
        _recover_interrupted_transaction(root)
        service_states = _service_state_snapshot()
        was_active = service_states["reticulumpi.service"]["active"]
        rnsd_was_active = service_states["rnsd.service"]["active"]
        watchdog_was_active = service_states["rnsd-watchdog.timer"]["active"]
        features = tuple(manifest.get("features", ()))
        roots = _state_roots(features)
        backup: Path | None = None
        identity_before: dict[str, str] = {}
        journal: dict[str, object] = {
            "schema": 1,
            "operation": "rollback",
            "install_root": str(root),
            "version": manifest["version"],
            "features": features,
            "previous_release": str(current),
            "new_release": str(target),
            "remove_candidate": False,
            "backup": None,
            "services_before": service_states,
            "state": "preparing",
        }
        _atomic_json(JOURNAL_FILE, journal, 0o600)
        if watchdog_was_active:
            _run([SYSTEMCTL, "stop", "rnsd-watchdog.timer"])
        if was_active:
            _run([SYSTEMCTL, "stop", "reticulumpi.service"])
            _wait_service_inactive("reticulumpi.service")
        if rnsd_was_active:
            _run([SYSTEMCTL, "stop", "rnsd.service"])
            _wait_service_inactive("rnsd.service")
        try:
            backup = _backup_state(str(manifest["version"]), features)
            _persist_file_snapshots(backup, ())
            journal["backup"] = str(backup)
            journal["state"] = "backed_up"
            _atomic_json(JOURNAL_FILE, journal, 0o600)
            backup_metadata = _read_json_object(backup / "backup.json", "backup metadata")
            raw_identity_before = backup_metadata.get("identity_hashes", {})
            if not isinstance(raw_identity_before, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in raw_identity_before.items()
            ):
                raise AdminError("rollback backup identity hashes are invalid")
            identity_before = dict(raw_identity_before)
            _switch_release(root, target)
            manifest["previous_release"] = str(current)
            manifest["release"] = str(target)
            manifest["version"] = _validate_version(
                (target / "RELEASE").read_text(encoding="utf-8").strip()
            )
            manifest["installed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _atomic_json(MANIFEST_FILE, manifest)
            _run([SYSTEMCTL, "daemon-reload"])
            if rnsd_was_active:
                _restart_and_wait("rnsd.service")
            if was_active:
                _activate_and_verify_identities("start", identity_before, roots, features)
            else:
                _verify_identity_continuity(identity_before, _identity_hashes(roots))
            if watchdog_was_active:
                _run([SYSTEMCTL, "start", "rnsd-watchdog.timer"])
                _wait_service_active("rnsd-watchdog.timer")
            journal["state"] = "complete"
            journal["identity_hashes_after"] = _identity_hashes(roots)
            _atomic_json(JOURNAL_FILE, journal, 0o600)
        except BaseException as original_error:
            rollback_errors: list[str] = []
            journal["failure"] = {
                "type": type(original_error).__name__,
                "reason": str(original_error),
            }
            _rollback_attempt(
                rollback_errors,
                "stop failed rollback target",
                lambda: _run([SYSTEMCTL, "stop", "reticulumpi.service"], check=False),
            )
            _rollback_attempt(
                rollback_errors,
                "restore release pointer",
                lambda: _switch_release(root, current),
            )
            _rollback_attempt(
                rollback_errors,
                "restore installation manifest",
                lambda: _atomic_json(MANIFEST_FILE, original_manifest),
            )
            if backup is not None:
                _rollback_attempt(
                    rollback_errors,
                    "restore configuration and durable state",
                    lambda: _restore_state_backup(backup),
                )
            _rollback_attempt(
                rollback_errors,
                "reload restored units",
                lambda: _run([SYSTEMCTL, "daemon-reload"], check=False),
            )
            _rollback_attempt(
                rollback_errors,
                "restore prior service states",
                lambda: _restore_service_states(service_states, features, identity_before),
            )
            journal["state"] = "rolled_back"
            if rollback_errors:
                journal["rollback_errors"] = rollback_errors
            _rollback_attempt(
                rollback_errors,
                "write rollback journal",
                lambda: _atomic_json(JOURNAL_FILE, journal, 0o600),
            )
            if rollback_errors:
                raise AdminError(
                    "rollback target failed and restoration was incomplete: "
                    + "; ".join(rollback_errors)
                ) from original_error
            raise
    print(f"Rolled back to {target}")
    return 0


def _status(args: argparse.Namespace) -> int:
    manifest = None
    if MANIFEST_FILE.exists() or MANIFEST_FILE.is_symlink():
        manifest = _load_manifest()
    journal, unfinished = _journal_state()
    result = {
        "manifest": manifest,
        "service_active": _service_active("reticulumpi.service"),
        "rnsd_active": _service_active("rnsd.service"),
        "config_exists": CONFIG_FILE.exists(),
        "data_dir": str(DATA_DIR),
        "unfinished_transaction": unfinished,
        "transaction": None
        if journal is None
        else {
            "state": journal.get("state"),
            "operation": journal.get("operation"),
            "backup": journal.get("backup"),
            "recovered_at": journal.get("recovered_at"),
            "recovery_evidence": journal.get("recovery_evidence"),
        },
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    return 0


def _doctor(_args: argparse.Namespace) -> int:
    failures: list[str] = []
    if sys.version_info < (3, 11):
        failures.append("Python 3.11 or newer is required")
    if CONFIG_FILE.is_symlink() or not CONFIG_FILE.is_file():
        failures.append(f"missing configuration: {CONFIG_FILE}")
    else:
        config_stat = CONFIG_FILE.stat()
        if config_stat.st_mode & 0o027:
            failures.append(f"configuration permissions are broader than 0640: {CONFIG_FILE}")
        if config_stat.st_uid != 0:
            failures.append(f"configuration is not owned by root: {CONFIG_FILE}")
    if MANIFEST_FILE.exists() or MANIFEST_FILE.is_symlink():
        try:
            manifest = _load_manifest()
            root = Path(manifest["install_root"])
            current = root / "current"
            active = _validate_release(root, current.resolve()) if current.is_symlink() else None
            if active is None or active != Path(manifest["release"]):
                failures.append("current release does not match install manifest")
        except AdminError as exc:
            failures.append(str(exc))
    try:
        journal, unfinished = _journal_state()
        if unfinished:
            failures.append(
                "interrupted administration transaction requires automatic recovery "
                "before the next apply operation"
            )
        elif journal is not None and journal.get("state") == "recovered":
            evidence = journal.get("recovery_evidence")
            if not isinstance(evidence, dict) or evidence.get("services_restored") is not True:
                failures.append("recovered transaction is missing durable recovery evidence")
    except AdminError as exc:
        failures.append(str(exc))
    for database in _databases():
        try:
            with contextlib.closing(
                sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            ) as connection:
                result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                failures.append(f"database integrity failed: {database}: {result}")
        except sqlite3.Error as exc:
            failures.append(f"cannot inspect database {database}: {exc}")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("All local ReticulumPi administration checks passed.")
    return 0


def _databases() -> list[Path]:
    if not DATA_DIR.exists():
        return []
    if DATA_DIR.is_symlink() or not DATA_DIR.is_dir():
        raise AdminError(f"unsafe data directory: {DATA_DIR}")
    root = DATA_DIR.resolve()
    found: set[Path] = set()
    for pattern in ("*.db", "*.sqlite", "*.sqlite3"):
        for candidate in DATA_DIR.rglob(pattern):
            if candidate.is_symlink():
                raise AdminError(f"database may not be a symlink: {candidate}")
            resolved = candidate.resolve()
            if not candidate.is_file() or not _is_within(resolved, root):
                raise AdminError(f"unsafe database path: {candidate}")
            found.add(resolved)
    return sorted(found)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


def _migration_plugin_classes() -> dict[str, type]:
    """Return the fixed built-in migration registry.

    Administration deliberately does not discover external plugins: importing
    arbitrary plugin paths as root would turn configuration into code execution.
    """

    try:
        from reticulumpi.builtin_plugins.messaging_hub import MessagingHubPlugin
        from reticulumpi.builtin_plugins.network_map import NetworkMapPlugin
        from reticulumpi.builtin_plugins.node_location_tracker import (
            NodeLocationTrackerPlugin,
        )
        from reticulumpi.builtin_plugins.sensor_framework import SensorFrameworkPlugin
        from reticulumpi.builtin_plugins.transport_health import TransportHealthPlugin
    except ImportError as exc:
        raise AdminError(f"cannot load built-in migration declarations: {exc}") from exc
    return {
        "messaging_hub": MessagingHubPlugin,
        "network_map": NetworkMapPlugin,
        "node_location_tracker": NodeLocationTrackerPlugin,
        "sensor_framework": SensorFrameworkPlugin,
        "transport_health": TransportHealthPlugin,
    }


def _service_home() -> Path:
    _service_account()
    home = DATA_DIR.resolve()
    if not home.is_absolute():
        raise AdminError(f"service user has an invalid home directory: {home}")
    return home


def _service_path(value: object, home: Path) -> str:
    raw = str(value)
    if raw == "~":
        return str(home)
    if raw.startswith("~/"):
        return str(home / raw[2:])
    path = Path(raw).expanduser()
    # systemd's default working directory for a system service is '/'. Match
    # that behavior so relative legacy paths do not resolve in the admin shell.
    if not path.is_absolute():
        path = Path("/") / path
    return str(path.absolute())


def _normalize_migration_config(name: str, value: dict, home: Path) -> dict:
    config = deepcopy(value)
    if name == "sensor_framework":
        storage = config.setdefault("storage", {})
        if isinstance(storage, dict) and storage.get("type", "sqlite") == "sqlite":
            storage["path"] = _service_path(
                storage.get("path", "~/.local/share/reticulumpi/sensor_data.db"),
                home,
            )
    else:
        defaults = {
            "messaging_hub": "~/.local/share/reticulumpi/messaging_hub.db",
            "network_map": "~/.local/share/reticulumpi/network_map.db",
            "node_location_tracker": "~/.local/share/reticulumpi/node_positions.db",
            "transport_health": "~/.local/share/reticulumpi/transport_health.db",
        }
        config["db_path"] = _service_path(config.get("db_path", defaults[name]), home)
    return config


def _validate_migration_target(target, names: set[str], paths: set[Path]) -> None:
    if target.name in names:
        raise AdminError(f"duplicate migration target name: {target.name}")
    path = Path(target.path)
    _reject_symlink_components(path)
    try:
        target_stat = path.lstat()
    except FileNotFoundError:
        target_stat = None
    except OSError as exc:
        raise AdminError(f"cannot inspect migration target {path}: {exc}") from exc
    if target_stat is not None and (
        stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(target_stat.st_mode)
    ):
        raise AdminError(f"migration target is not a regular database file: {path}")
    if path in paths:
        raise AdminError(f"multiple migration targets refer to the same database: {path}")
    names.add(target.name)
    paths.add(path)


def _load_enabled_migration_targets() -> tuple:
    if CONFIG_FILE.is_symlink() or not CONFIG_FILE.is_file():
        raise AdminError(f"system configuration is missing or unsafe: {CONFIG_FILE}")
    try:
        from reticulumpi.config import AppConfig, ConfigError

        configured = AppConfig(str(CONFIG_FILE))
    except (ConfigError, OSError) as exc:
        raise AdminError(f"cannot load migration configuration: {exc}") from exc

    classes = _migration_plugin_classes()
    home = _service_home()
    targets = []
    names: set[str] = set()
    paths: set[Path] = set()
    for name, plugin_config in configured.plugins.items():
        if not plugin_config.get("enabled", False) or name not in classes:
            continue
        instance = object.__new__(classes[name])
        instance.config = _normalize_migration_config(name, plugin_config, home)
        try:
            instance.validate_config()
            declared = instance.get_migration_targets()
        except (OSError, TypeError, ValueError) as exc:
            raise AdminError(f"invalid migration configuration for {name}: {exc}") from exc
        for target in declared:
            _validate_migration_target(target, names, paths)
            targets.append(target)
    return tuple(sorted(targets, key=lambda target: target.name))


def _db_plan(_args: argparse.Namespace) -> int:
    from reticulumpi.migrations import MigrationError, plan_migrations

    targets = _load_enabled_migration_targets()
    if not targets:
        print("No enabled built-in plugins declare database migrations.")
        return 0
    try:
        for target in targets:
            pending = plan_migrations(target)
            current = len(target.migrations) - len(pending)
            versions = ",".join(str(item.version) for item in pending) or "none"
            checksums = ",".join(item.stable_checksum for item in pending) or "none"
            print(
                f"{target.name}: path={target.path} current={current} "
                f"target={len(target.migrations)} pending={versions} checksums={checksums}"
            )
    except (MigrationError, OSError, sqlite3.Error) as exc:
        raise AdminError(f"migration planning failed: {exc}") from exc
    return 0


def _dry_run_migration(target):
    """Exercise a target on a temporary clone without touching its directory."""

    from reticulumpi.migrations import MigrationTarget, migrate_target

    with tempfile.TemporaryDirectory(prefix=f"reticulumpi-{target.name}-admin-") as raw:
        clone = Path(raw) / target.path.name
        if target.path.exists():
            _sqlite_backup_file(target.path, clone)
        clone_target = MigrationTarget(target.name, clone, target.migrations)
        return migrate_target(clone_target, dry_run=True)


def _db_migrate(args: argparse.Namespace) -> int:
    from reticulumpi.migrations import MigrationError, migrate_target

    targets = _load_enabled_migration_targets()
    if not targets:
        print("No enabled built-in plugins declare database migrations.")
        return 0
    if not args.apply:
        try:
            for target in targets:
                result = _dry_run_migration(target)
                versions = ",".join(str(version) for version in result.applied) or "none"
                print(
                    f"{target.name}: path={target.path} from={result.from_version} "
                    f"to={result.to_version} dry_run=true pending={versions}"
                )
        except (MigrationError, OSError, sqlite3.Error) as exc:
            raise AdminError(f"migration dry run failed: {exc}") from exc
        print("Dry run only; stop the service and rerun with --apply to migrate.")
        return 0

    _require_root()
    with _maintenance_lock():
        if _service_active("reticulumpi.service"):
            raise AdminError("stop reticulumpi.service before applying database migrations")
        account = _service_account()
        try:
            for target in targets:
                owner = None
                if target.path.exists():
                    target_stat = target.path.stat()
                    owner = (target_stat.st_uid, target_stat.st_gid)
                else:
                    _ensure_real_directory(target.path.parent, mode=0o750)
                    os.chown(target.path.parent, account.pw_uid, account.pw_gid)
                backup_dir = BACKUP_DIR / "databases" / target.name
                _ensure_real_directory(backup_dir, mode=0o700)
                result = migrate_target(
                    target,
                    dry_run=False,
                    backup_dir=backup_dir,
                    retain=3,
                )
                if target.path.exists():
                    os.chown(target.path, *(owner or (account.pw_uid, account.pw_gid)))
                    target.path.chmod(0o600)
                versions = ",".join(str(version) for version in result.applied) or "none"
                backup = str(result.backup_path) if result.backup_path else "none"
                print(
                    f"{target.name}: from={result.from_version} to={result.to_version} "
                    f"applied={versions} backup={backup}"
                )
        except (KeyError, MigrationError, OSError, sqlite3.Error) as exc:
            raise AdminError(f"migration failed: {exc}") from exc
    return 0


def _db_backup(args: argparse.Namespace) -> int:
    databases = _databases()
    if not args.apply:
        print(f"Would create verified backups for {len(databases)} SQLite database(s).")
        print("Dry run only; rerun with --apply to create the backup set.")
        return 0
    _require_root()
    with _maintenance_lock():
        _ensure_real_directory(BACKUP_DIR, mode=0o700)
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        destination = Path(
            tempfile.mkdtemp(prefix=f"{_DB_BACKUP_PREFIX}{timestamp}-", dir=BACKUP_DIR)
        )
        destination.chmod(0o700)
        try:
            for database in databases:
                relative = database.relative_to(DATA_DIR.resolve())
                _sqlite_backup_file(database, destination / relative)
            _atomic_json(
                destination / "backup.json",
                {
                    "created_at": timestamp,
                    "databases": [str(path.relative_to(DATA_DIR.resolve())) for path in databases],
                },
                0o600,
            )
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        print(destination)
    return 0


def _db_backups(_args: argparse.Namespace) -> int:
    if not BACKUP_DIR.exists():
        return 0
    for path in sorted(BACKUP_DIR.iterdir(), reverse=True):
        if path.is_dir() and path.name.startswith((_DB_BACKUP_PREFIX, "db-safety-")):
            print(path)
    return 0


def _db_restore(args: argparse.Namespace) -> int:
    from reticulumpi.migrations import (
        MigrationError,
        _canonicalize_trusted_ancestors,
    )

    requested_source = Path(args.backup).expanduser()
    requested_target = Path(args.database).expanduser()
    if requested_source.is_symlink() or not requested_source.is_file():
        raise AdminError(f"backup must be a regular file: {requested_source}")
    if requested_target.is_symlink():
        raise AdminError(f"restore target may not be a symlink: {requested_target}")
    try:
        source = _canonicalize_trusted_ancestors(requested_source)
        target = _canonicalize_trusted_ancestors(requested_target)
    except MigrationError as exc:
        raise AdminError(str(exc)) from exc
    if not source.is_file():
        raise AdminError(f"backup does not exist: {source}")
    if not _is_within(target, DATA_DIR.resolve()):
        raise AdminError(f"restore target must be under {DATA_DIR}")
    if source == target:
        raise AdminError("backup and restore target must be different files")
    _verify_sqlite(source)
    if not args.apply:
        print(f"Would replace {target} with verified backup {source}.")
        if target.exists():
            print("A verified safety backup of the current database will be retained.")
        print("Dry run only; rerun with --apply to restore the database.")
        return 0
    _require_root()
    with _maintenance_lock():
        if _service_active("reticulumpi.service"):
            raise AdminError("stop reticulumpi.service before restoring a database")
        owner: tuple[int, int] | None = None
        if target.exists():
            if not target.is_file():
                raise AdminError(f"restore target is not a regular file: {target}")
            target_stat = target.stat()
            owner = (target_stat.st_uid, target_stat.st_gid)
            _ensure_real_directory(BACKUP_DIR, mode=0o700)
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            safety_dir = Path(tempfile.mkdtemp(prefix=f"db-safety-{stamp}-", dir=BACKUP_DIR))
            safety_dir.chmod(0o700)
            _sqlite_backup_file(target, safety_dir / target.name)
        if owner is None:
            try:
                account = pwd.getpwnam(SERVICE_USER)
            except KeyError as exc:
                raise AdminError(f"service user {SERVICE_USER!r} does not exist") from exc
            owner = (account.pw_uid, account.pw_gid)
        from reticulumpi.migrations import restore_database

        try:
            restore_database(source, target)
        except MigrationError as exc:
            raise AdminError(str(exc)) from exc
        os.chown(target, *owner)
        target.chmod(0o600)
        _verify_sqlite(target)
    print(f"Restored {target} from {source}")
    return 0


def _add_apply_flags(parser: argparse.ArgumentParser) -> None:
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show changes without applying them")
    mode.add_argument("--apply", action="store_true", help="apply changes (requires root)")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reticulumpi-admin",
        formatter_class=StableHelpFormatter,
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in ("install", "upgrade"):
        action = subcommands.add_parser(command)
        action.add_argument("--bundle", required=True)
        action.add_argument("--install-root", "--install-dir", default=str(DEFAULT_INSTALL_ROOT))
        action.add_argument("--feature", action="append", default=[])
        action.add_argument("--start", action="store_true")
        _add_apply_flags(action)
        action.set_defaults(handler=lambda args, name=command: _apply_release(args, name))
    rollback = subcommands.add_parser("rollback")
    rollback.add_argument("--to")
    _add_apply_flags(rollback)
    rollback.set_defaults(handler=_rollback)
    status = subcommands.add_parser("status")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=_status)
    doctor = subcommands.add_parser("doctor")
    doctor.set_defaults(handler=_doctor)

    db = subcommands.add_parser("db")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    plan = db_commands.add_parser("plan")
    plan.set_defaults(handler=_db_plan)
    migrate = db_commands.add_parser("migrate")
    _add_apply_flags(migrate)
    migrate.set_defaults(handler=_db_migrate)
    backup = db_commands.add_parser("backup")
    _add_apply_flags(backup)
    backup.set_defaults(handler=_db_backup)
    backups = db_commands.add_parser("backups")
    backups.set_defaults(handler=_db_backups)
    restore = db_commands.add_parser("restore")
    restore.add_argument("backup")
    restore.add_argument("--database", required=True)
    _add_apply_flags(restore)
    restore.set_defaults(handler=_db_restore)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except AdminError as exc:
        parser.error(str(exc))
    except subprocess.CalledProcessError as exc:
        parser.error(f"command failed ({exc.returncode}): {' '.join(exc.cmd)}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
