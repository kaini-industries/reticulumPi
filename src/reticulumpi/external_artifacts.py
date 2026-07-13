"""Fail-closed production policy for externally installed executables.

ReticulumPi's Python dependencies are installed from hash-locked release
profiles.  MeshChat and the native radio decoder tools are deliberately
provisioned outside that environment, so production activation requires a
separate root-owned manifest which binds each path to an immutable version
label and a SHA-256 digest.

Development configurations remain compatible with ordinary ``PATH`` lookup.
The policy is enforced centrally by :class:`reticulumpi.config.AppConfig`
before an affected first-party plugin is constructed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TRUSTED_OWNER_UID = 0
MAX_MANIFEST_BYTES = 1_048_576
MAX_TREE_ENTRIES = 100_000
MAX_TREE_BYTES = 2 * 1024 * 1024 * 1024

_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MUTABLE_VERSIONS = {
    "dev",
    "development",
    "head",
    "latest",
    "main",
    "master",
    "nightly",
    "snapshot",
    "unstable",
}
_TREE_CACHE_NAMES = frozenset({".git", ".pytest_cache", "__pycache__"})


class ArtifactPolicyError(ValueError):
    """Raised when an external artifact is untrusted or does not match policy."""


@dataclass(frozen=True)
class ArtifactRecord:
    """One reviewed external artifact."""

    name: str
    kind: str
    version: str
    path: Path
    sha256: str


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_trusted_ancestry(path: Path) -> None:
    """Require the resolved artifact ancestry to be root-owned and immutable."""

    resolved = path.resolve(strict=True)
    current = Path(resolved.anchor)
    for part in resolved.parts[1:]:
        current /= part
        value = current.lstat()
        if value.st_uid != TRUSTED_OWNER_UID:
            raise ArtifactPolicyError(f"production artifact path is not root-owned: {current}")
        if not stat.S_ISLNK(value.st_mode) and stat.S_IMODE(value.st_mode) & 0o022:
            raise ArtifactPolicyError(
                f"production artifact path is group/world-writable: {current}"
            )


def _open_regular_file(path: Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ArtifactPolicyError(f"cannot open external artifact {path}: {exc}") from exc
    value = os.fstat(descriptor)
    if not stat.S_ISREG(value.st_mode):
        os.close(descriptor)
        raise ArtifactPolicyError(f"external artifact is not a regular file: {path}")
    return descriptor, value


def _validate_trusted_stat(value: os.stat_result, path: Path) -> None:
    if value.st_uid != TRUSTED_OWNER_UID:
        raise ArtifactPolicyError(f"production artifact is not root-owned: {path}")
    if stat.S_IMODE(value.st_mode) & 0o022:
        raise ArtifactPolicyError(f"production artifact is group/world-writable: {path}")


def _hash_descriptor(descriptor: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return digest.hexdigest(), size
        size += len(chunk)
        digest.update(chunk)


def file_sha256(path: str | os.PathLike[str]) -> str:
    """Return a no-follow SHA-256 digest for one regular file."""

    candidate = Path(path).expanduser().absolute()
    descriptor, _value = _open_regular_file(candidate)
    try:
        digest, _size = _hash_descriptor(descriptor)
        return digest
    finally:
        os.close(descriptor)


def _feed_tree_record(
    digest: Any,
    kind: str,
    relative: Path,
    mode: int,
    payload: bytes,
) -> None:
    name = os.fsencode(relative.as_posix())
    for value in (kind.encode("ascii"), name, f"{mode:o}".encode("ascii"), payload):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)


def tree_sha256(
    path: str | os.PathLike[str],
    *,
    require_trusted: bool = False,
) -> str:
    """Hash a complete immutable deployment tree deterministically.

    Git metadata and generated Python/test caches are excluded.  The MeshChat
    virtual environment is intentionally included.  Directory symlinks must
    stay inside the tree and may not resolve into an excluded tree; their
    logical and resolved targets are both bound into the digest.  File symlinks
    include both their link target and the digest of the resolved immutable
    file.
    """

    requested = Path(path).expanduser().absolute()
    if requested.is_symlink():
        raise ArtifactPolicyError(f"external artifact tree may not be a symlink: {requested}")
    try:
        root = requested.resolve(strict=True)
    except OSError as exc:
        raise ArtifactPolicyError(f"external artifact tree is unavailable: {requested}") from exc
    if not root.is_dir():
        raise ArtifactPolicyError(f"external artifact tree is not a directory: {root}")
    if require_trusted:
        _validate_trusted_ancestry(root)

    digest = hashlib.sha256()
    entries = 0
    total_bytes = 0
    for current_raw, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_raw)
        relative_current = current.relative_to(root)
        directory_names[:] = sorted(
            name for name in directory_names if name not in _TREE_CACHE_NAMES
        )
        file_names = sorted(name for name in file_names if name not in _TREE_CACHE_NAMES)

        current_stat = current.lstat()
        if not stat.S_ISDIR(current_stat.st_mode):
            raise ArtifactPolicyError(f"external artifact tree changed during hashing: {current}")
        if require_trusted:
            _validate_trusted_stat(current_stat, current)
        _feed_tree_record(
            digest,
            "directory",
            relative_current,
            stat.S_IMODE(current_stat.st_mode),
            b"",
        )
        entries += 1

        retained_directories: list[str] = []
        for name in directory_names:
            child = current / name
            child_stat = child.lstat()
            if stat.S_ISLNK(child_stat.st_mode):
                target = child.resolve(strict=True)
                if not target.is_dir() or not _is_relative_to(target, root):
                    raise ArtifactPolicyError(
                        f"artifact directory symlink escapes deployment tree: {child}"
                    )
                target_relative = target.relative_to(root)
                if any(part in _TREE_CACHE_NAMES for part in target_relative.parts):
                    raise ArtifactPolicyError(
                        f"artifact directory symlink targets an excluded tree: {child}"
                    )
                if require_trusted:
                    _validate_trusted_ancestry(target)
                payload = (
                    os.fsencode(os.readlink(child))
                    + b"\0"
                    + os.fsencode(target_relative.as_posix())
                )
                _feed_tree_record(
                    digest,
                    "directory-link",
                    child.relative_to(root),
                    0,
                    payload,
                )
                entries += 1
            else:
                retained_directories.append(name)
        directory_names[:] = retained_directories

        for name in file_names:
            child = current / name
            child_stat = child.lstat()
            relative = child.relative_to(root)
            if stat.S_ISLNK(child_stat.st_mode):
                target = child.resolve(strict=True)
                if not target.is_file():
                    raise ArtifactPolicyError(
                        f"artifact file symlink does not resolve to a file: {child}"
                    )
                if require_trusted:
                    _validate_trusted_ancestry(target)
                descriptor, target_stat = _open_regular_file(target)
                try:
                    if require_trusted:
                        _validate_trusted_stat(target_stat, target)
                    target_digest, target_size = _hash_descriptor(descriptor)
                finally:
                    os.close(descriptor)
                payload = os.fsencode(os.readlink(child)) + b"\0" + target_digest.encode("ascii")
                _feed_tree_record(digest, "file-link", relative, 0, payload)
                total_bytes += target_size
            elif stat.S_ISREG(child_stat.st_mode):
                descriptor, opened_stat = _open_regular_file(child)
                try:
                    if require_trusted:
                        _validate_trusted_stat(opened_stat, child)
                    child_digest, child_size = _hash_descriptor(descriptor)
                finally:
                    os.close(descriptor)
                _feed_tree_record(
                    digest,
                    "file",
                    relative,
                    stat.S_IMODE(opened_stat.st_mode),
                    child_digest.encode("ascii"),
                )
                total_bytes += child_size
            else:
                raise ArtifactPolicyError(f"special file in external artifact tree: {child}")
            entries += 1
            if entries > MAX_TREE_ENTRIES or total_bytes > MAX_TREE_BYTES:
                raise ArtifactPolicyError("external artifact tree exceeds safety limits")
    return digest.hexdigest()


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise ArtifactPolicyError("PyYAML is required to read an artifact manifest") from exc

    class _UniqueKeyLoader(yaml.SafeLoader):
        """Safe YAML loader which rejects ambiguous duplicate mapping keys."""

    def construct_unique_mapping(
        loader: _UniqueKeyLoader,
        node,
        deep: bool = False,
    ) -> dict[Any, Any]:
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise ArtifactPolicyError(f"duplicate manifest key: {key!r}")
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    _UniqueKeyLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        construct_unique_mapping,
    )

    if not path.is_absolute():
        raise ArtifactPolicyError("production artifact manifest path must be absolute")
    _validate_trusted_ancestry(path)
    descriptor, value = _open_regular_file(path)
    try:
        _validate_trusted_stat(value, path)
        if value.st_size > MAX_MANIFEST_BYTES:
            raise ArtifactPolicyError("external artifact manifest exceeds 1 MiB")
        chunks: list[bytes] = []
        remaining = MAX_MANIFEST_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    if sum(map(len, chunks)) > MAX_MANIFEST_BYTES:
        raise ArtifactPolicyError("external artifact manifest exceeds 1 MiB")
    try:
        raw = yaml.load(b"".join(chunks), Loader=_UniqueKeyLoader)
    except (yaml.YAMLError, UnicodeError) as exc:
        raise ArtifactPolicyError(f"invalid external artifact manifest: {exc}") from exc
    if not isinstance(raw, dict):
        raise ArtifactPolicyError("external artifact manifest root must be a mapping")
    return raw


def load_manifest(path: str | os.PathLike[str]) -> tuple[ArtifactRecord, ...]:
    """Load and validate a trusted schema-1 artifact manifest."""

    raw = _read_manifest(Path(path).expanduser())
    if set(raw) != {"schema", "artifacts"} or raw.get("schema") != 1:
        raise ArtifactPolicyError("external artifact manifest must use schema 1")
    artifacts = raw.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ArtifactPolicyError("external artifact manifest artifacts must be a mapping")

    records: list[ArtifactRecord] = []
    canonical_paths: set[tuple[str, str]] = set()
    for name, value in artifacts.items():
        if not isinstance(name, str) or not _ARTIFACT_NAME.fullmatch(name):
            raise ArtifactPolicyError(f"invalid external artifact name: {name!r}")
        if not isinstance(value, dict) or set(value) != {"kind", "version", "path", "sha256"}:
            raise ArtifactPolicyError(f"external artifact {name!r} has invalid fields")
        kind = value["kind"]
        if kind not in {"file", "tree"}:
            raise ArtifactPolicyError(f"external artifact {name!r} kind must be file or tree")
        version = value["version"]
        if (
            not isinstance(version, str)
            or not version.strip()
            or len(version) > 128
            or version.strip().lower() in _MUTABLE_VERSIONS
        ):
            raise ArtifactPolicyError(
                f"external artifact {name!r} requires an immutable version label"
            )
        raw_path = value["path"]
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
            raise ArtifactPolicyError(f"external artifact {name!r} path must be absolute")
        expected = value["sha256"]
        if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
            raise ArtifactPolicyError(f"external artifact {name!r} has invalid SHA-256")
        canonical_key = (kind, os.path.realpath(raw_path))
        if canonical_key in canonical_paths:
            raise ArtifactPolicyError(f"duplicate external artifact path: {raw_path}")
        canonical_paths.add(canonical_key)
        records.append(ArtifactRecord(name, kind, version.strip(), Path(raw_path), expected))
    return tuple(records)


@dataclass(frozen=True)
class ExternalArtifactPolicy:
    """Configured enforcement mode and trusted manifest location."""

    mode: str = "development"
    manifest_path: Path | None = None

    @classmethod
    def from_config(cls, raw: Any) -> "ExternalArtifactPolicy":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ArtifactPolicyError("external_artifacts must be a mapping")
        if set(raw) - {"mode", "manifest_path"}:
            unknown = ", ".join(sorted(set(raw) - {"mode", "manifest_path"}))
            raise ArtifactPolicyError(f"unsupported external_artifacts keys: {unknown}")
        mode = raw.get("mode", "development")
        if mode not in {"development", "required"}:
            raise ArtifactPolicyError("external_artifacts.mode must be development or required")
        manifest_value = raw.get("manifest_path")
        if manifest_value is not None and not isinstance(manifest_value, str):
            raise ArtifactPolicyError("external_artifacts.manifest_path must be a path or null")
        manifest = Path(manifest_value).expanduser() if manifest_value else None
        if mode == "required" and (manifest is None or not manifest.is_absolute()):
            raise ArtifactPolicyError(
                "required external artifact policy needs an absolute manifest_path"
            )
        return cls(mode=mode, manifest_path=manifest)

    @property
    def required(self) -> bool:
        return self.mode == "required"

    def _records(self) -> tuple[ArtifactRecord, ...]:
        if not self.required:
            return ()
        if self.manifest_path is None:  # defensive: constructor is public
            raise ArtifactPolicyError("production artifact manifest is not configured")
        return load_manifest(self.manifest_path)

    def verify_path(self, path: str | os.PathLike[str], *, kind: str) -> Path:
        """Verify a resolved file/tree against the configured production manifest."""

        requested = Path(path).expanduser().absolute()
        if not self.required:
            return requested
        try:
            resolved = requested.resolve(strict=True)
        except OSError as exc:
            raise ArtifactPolicyError(f"external artifact is unavailable: {requested}") from exc
        candidates = [
            record
            for record in self._records()
            if record.kind == kind and os.path.realpath(record.path) == str(resolved)
        ]
        if len(candidates) != 1:
            raise ArtifactPolicyError(f"no unique {kind} artifact manifest entry for {resolved}")
        record = candidates[0]
        _validate_trusted_ancestry(resolved)
        if kind == "file":
            descriptor, value = _open_regular_file(resolved)
            try:
                _validate_trusted_stat(value, resolved)
                actual, _size = _hash_descriptor(descriptor)
            finally:
                os.close(descriptor)
        elif kind == "tree":
            actual = tree_sha256(resolved, require_trusted=True)
        else:
            raise ArtifactPolicyError(f"unsupported external artifact kind: {kind}")
        if actual != record.sha256:
            raise ArtifactPolicyError(
                f"external artifact {record.name!r} does not match its reviewed SHA-256"
            )
        return resolved

    def verify_executable(self, command: str) -> Path:
        """Resolve and verify one external executable in required mode."""

        resolved = shutil.which(command)
        if not resolved:
            raise ArtifactPolicyError(f"required external executable not found: {command}")
        if not self.required:
            return Path(resolved)
        return self.verify_path(resolved, kind="file")

    def preflight_enabled_plugins(self, plugins: dict[str, dict[str, Any]]) -> None:
        """Block unsafe first-party external-tool activation before construction."""

        if not self.required:
            return
        for plugin_name, config in plugins.items():
            if not config.get("enabled", False):
                continue
            if plugin_name == "meshchat_server":
                install_dir = config.get("install_dir")
                if not isinstance(install_dir, str) or not Path(install_dir).is_absolute():
                    raise ArtifactPolicyError(
                        "production meshchat_server requires an absolute install_dir"
                    )
                self.verify_path(install_dir, kind="tree")
                continue
            for command in _radio_commands(plugin_name, config):
                self.verify_executable(command)


def _radio_commands(plugin_name: str, config: dict[str, Any]) -> tuple[str, ...]:
    commands: list[str]
    if plugin_name == "radiosonde_tracker":
        commands = ["rtl_test", "rtl_fm", str(config.get("decoder_bin", "rs41mod"))]
    elif plugin_name == "weather_alert":
        commands = ["rtl_test", "rtl_fm", "multimon-ng"]
    elif plugin_name == "acars_decoder":
        commands = ["rtl_test", str(config.get("decoder_bin", "acarsdec"))]
    elif plugin_name == "ais_receiver":
        commands = ["rtl_test", str(config.get("decoder_bin", "AIS-catcher"))]
    elif plugin_name == "ism_decoder":
        commands = ["rtl_test", str(config.get("decoder_bin", "rtl_433"))]
    elif plugin_name == "noaa_apt_decoder":
        commands = [
            "rtl_test",
            "rtl_fm",
            "sox",
            str(config.get("decoder_bin", "noaa-apt")),
        ]
    elif plugin_name == "adsb_radar":
        commands = ["rtl_test", str(config.get("dump1090_bin", "dump1090"))]
        if config.get("enable_bias_tee", False):
            commands.append("rtl_biast")
    elif plugin_name == "spectrum_scanner":
        commands = ["rtl_test", str(config.get("power_command", "rtl_power"))]
    elif plugin_name == "fm_receiver":
        commands = ["rtl_test", "rtl_fm"]
    else:
        return ()
    return tuple(dict.fromkeys(commands))


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute a ReticulumPi external-artifact manifest digest",
    )
    parser.add_argument("path")
    parser.add_argument("--kind", choices=("file", "tree"), required=True)
    args = parser.parse_args(argv)
    digest = file_sha256(args.path) if args.kind == "file" else tree_sha256(args.path)
    print(digest)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the documented CLI
    raise SystemExit(_main())
