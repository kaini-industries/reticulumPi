"""Application configuration loader."""

from __future__ import annotations

import logging
import os
import socket
import tempfile
import threading
from copy import deepcopy
from typing import Any

import yaml

from reticulumpi.external_artifacts import ArtifactPolicyError, ExternalArtifactPolicy

log = logging.getLogger(__name__)

PRODUCTION_CONFIG_PATH = "/etc/reticulumpi/config.yaml"
PRODUCTION_ARTIFACT_MANIFEST = "/etc/reticulumpi/external-artifacts.yaml"

VALID_KEYS = {
    "node_name",
    "reticulum_config_dir",
    "use_shared_instance",
    "identity_path",
    "log_level",
    "log_format",
    "plugin_paths",
    "plugins",
    "thread_budget",
    "internet",
    "external_artifacts",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "reticulum_config_dir": None,
    "use_shared_instance": True,
    # The systemd service receives HOME=/var/lib/reticulumpi and the installed
    # production example sets an explicit canonical path. Keeping the library
    # fallback user-relative preserves safe unconfigured development use.
    "identity_path": "~/.config/reticulumpi/identity",
    "log_level": 4,
    "plugin_paths": [],
    "plugins": {},
    "thread_budget": 50,
    # Source-checkout and test configurations keep normal PATH behavior.
    # The installed production example changes this to ``required`` and
    # supplies a root-owned manifest for MeshChat/native radio tools.
    "external_artifacts": {
        "mode": "development",
        "manifest_path": None,
    },
    "internet": {
        "force_offline": False,
        "probe_interval": 30,
        "probe_timeout": 3,
        "offline_threshold": 3,
        "targets": [
            {"host": "1.1.1.1", "port": 53},
            {"host": "8.8.8.8", "port": 53},
            {"host": "9.9.9.9", "port": 53},
        ],
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    """Recursively merge *override* into *base*, mutating *base* in place.

    For each key in *override*: if both ``base[key]`` and ``override[key]``
    are dicts, recurse; otherwise set ``base[key] = override[key]``.
    """
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


class ConfigError(Exception):
    """Raised when config is invalid."""


class AppConfig:
    """Loads and provides typed access to the reticulumPi YAML config."""

    def __init__(
        self,
        config_path: str | None = None,
        runtime_overrides_path: str | None = None,
    ):
        self._config_path = config_path
        self._runtime_overrides_path = self._resolve_runtime_overrides_path(
            config_path,
            runtime_overrides_path,
        )
        self._last_persistence_reason = "not_attempted"
        self._external_artifacts_explicit = False
        self._data: dict[str, Any] = deepcopy(DEFAULT_CONFIG)
        if config_path:
            self._load_file(config_path)
        self._load_runtime_overrides()
        self._validate()
        self._lock = threading.Lock()

    @staticmethod
    def _resolve_runtime_overrides_path(
        config_path: str | None,
        explicit_path: str | None,
    ) -> str | None:
        """Resolve the service-writable runtime override file."""
        configured = explicit_path or os.environ.get("RETICULUMPI_RUNTIME_OVERRIDES")
        if configured:
            return os.path.expanduser(configured)
        if not config_path:
            return None
        resolved = os.path.abspath(os.path.expanduser(config_path))
        if resolved == "/etc/reticulumpi/config.yaml":
            return "/var/lib/reticulumpi/runtime-overrides.yaml"
        return f"{resolved}.runtime.yaml"

    @property
    def config_path(self) -> str | None:
        """Return the resolved config file path, or None if using defaults."""
        if self._config_path:
            return os.path.expanduser(self._config_path)
        return None

    @property
    def runtime_overrides_path(self) -> str | None:
        """Return the allowlisted runtime override path, if configured."""
        return self._runtime_overrides_path

    @property
    def last_persistence_reason(self) -> str:
        """Return the result code from the latest persistence attempt."""
        return self._last_persistence_reason

    def _load_file(self, path: str) -> None:
        path = os.path.expanduser(path)
        if not os.path.isfile(path):
            raise ConfigError(f"Config file not found: {path}")
        try:
            with open(path, "r") as f:
                raw = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML in {path}: {e}") from e
        if raw is None:
            return
        if not isinstance(raw, dict):
            raise ConfigError(f"Config root in {path} must be a mapping")
        if "reticulumpi" not in raw:
            raise ConfigError(f"Config file {path} is missing the required 'reticulumpi:' section")
        app_section = raw["reticulumpi"]
        if app_section is not None and not isinstance(app_section, dict):
            raise ConfigError(f"reticulumpi section in {path} must be a mapping")
        if app_section:
            self._external_artifacts_explicit = "external_artifacts" in app_section
            _deep_merge(self._data, app_section)
        log.info("Loaded config from %s", path)

    def _load_runtime_overrides(self) -> None:
        """Load the small, service-writable runtime overlay if it exists."""
        path = self._runtime_overrides_path
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"Invalid runtime overrides in {path}: {exc}") from exc
        if raw is None:
            return
        if not isinstance(raw, dict):
            raise ConfigError(f"Runtime override root in {path} must be a mapping")
        internet = raw.get("internet", {})
        if not isinstance(internet, dict):
            raise ConfigError(f"Runtime override internet section in {path} must be a mapping")
        unknown_root = set(raw) - {"internet"}
        unknown_internet = set(internet) - {"force_offline"}
        if unknown_root or unknown_internet:
            unknown = sorted(unknown_root | {f"internet.{key}" for key in unknown_internet})
            raise ConfigError(f"Unsupported runtime override keys in {path}: {', '.join(unknown)}")
        if "force_offline" in internet:
            value = internet["force_offline"]
            if not isinstance(value, bool):
                raise ConfigError("runtime internet.force_offline must be a boolean")
            self._data.setdefault("internet", {})["force_offline"] = value

    def _validate(self) -> None:
        unknown = set(self._data.keys()) - VALID_KEYS
        if unknown:
            log.warning("Unknown config keys (ignored): %s", ", ".join(sorted(unknown)))

        level = self._data.get("log_level", 4)
        if not isinstance(level, int) or not 0 <= level <= 7:
            raise ConfigError(f"log_level must be an integer 0-7, got: {level!r}")

        paths = self._data.get("plugin_paths", [])
        if not isinstance(paths, list):
            raise ConfigError(f"plugin_paths must be a list, got: {type(paths).__name__}")

        plugins = self._data.get("plugins", {})
        if not isinstance(plugins, dict):
            raise ConfigError(f"plugins must be a mapping, got: {type(plugins).__name__}")
        invalid_plugins = [name for name, value in plugins.items() if not isinstance(value, dict)]
        if invalid_plugins:
            raise ConfigError(
                "plugin configurations must be mappings: " + ", ".join(sorted(invalid_plugins))
            )

        try:
            artifact_config = self._data.get("external_artifacts")
            configured_path = (
                os.path.abspath(os.path.expanduser(self._config_path))
                if self._config_path
                else None
            )
            if configured_path == PRODUCTION_CONFIG_PATH:
                if self._external_artifacts_explicit:
                    if (
                        not isinstance(artifact_config, dict)
                        or artifact_config.get("mode") != "required"
                    ):
                        raise ArtifactPolicyError(
                            "canonical production configuration may not disable required mode"
                        )
                else:
                    # Security migration default for legacy production files:
                    # preserve the config bytes while enforcing the new policy
                    # in memory. The manifest is only read if an affected
                    # first-party plugin is enabled.
                    artifact_config = {
                        "mode": "required",
                        "manifest_path": PRODUCTION_ARTIFACT_MANIFEST,
                    }
            self._external_artifact_policy = ExternalArtifactPolicy.from_config(
                artifact_config,
            )
            self._external_artifact_policy.preflight_enabled_plugins(plugins)
        except ArtifactPolicyError as exc:
            raise ConfigError(f"Invalid external artifact policy: {exc}") from exc

        internet = self._data.get("internet", {})
        if not isinstance(internet, dict):
            raise ConfigError(f"internet must be a mapping, got: {type(internet).__name__}")
        force_offline = internet.get("force_offline", False)
        if not isinstance(force_offline, bool):
            raise ConfigError("internet.force_offline must be a boolean")

        thread_budget = self._data.get("thread_budget", 50)
        if (
            not isinstance(thread_budget, int)
            or isinstance(thread_budget, bool)
            or thread_budget < 1
        ):
            raise ConfigError("thread_budget must be a positive integer")

    @property
    def reticulum_config_dir(self) -> str | None:
        val = self._data.get("reticulum_config_dir")
        return os.path.expanduser(val) if val else None

    @property
    def use_shared_instance(self) -> bool:
        return bool(self._data.get("use_shared_instance", True))

    @property
    def identity_path(self) -> str:
        return os.path.expanduser(self._data.get("identity_path", DEFAULT_CONFIG["identity_path"]))

    @property
    def log_level(self) -> int:
        return int(self._data.get("log_level", 4))

    @property
    def plugin_paths(self) -> list[str]:
        paths = self._data.get("plugin_paths", [])
        return [os.path.expanduser(p) for p in paths]

    @property
    def node_name(self) -> str:
        name = self._data.get("node_name")
        if name:
            return str(name)
        # Default to hostname so each node gets a unique name out of the box.
        hostname = socket.gethostname()
        return f"ReticulumPi-{hostname}"

    @property
    def thread_budget(self) -> int:
        val = self._data.get("thread_budget", 50)
        return int(val) if isinstance(val, (int, float)) else 50

    @property
    def internet(self) -> dict[str, Any]:
        return dict(self._data.get("internet", DEFAULT_CONFIG["internet"]))

    @property
    def offgrid_mode(self) -> bool:
        inet = self._data.get("internet", {})
        return bool(inet.get("force_offline", False))

    def set_internet_force_offline(self, value: bool) -> bool:
        """Update internet.force_offline and its allowlisted runtime overlay.

        Returns True if persisted to disk, False if in-memory only.
        """
        with self._lock:
            inet = self._data.setdefault("internet", dict(DEFAULT_CONFIG["internet"]))
            inet["force_offline"] = bool(value)
            try:
                persisted = self._persist_runtime_overrides()
                self._last_persistence_reason = "persisted" if persisted else "no_override_path"
                return persisted
            except (OSError, yaml.YAMLError, TypeError, ValueError) as exc:
                self._last_persistence_reason = "write_failed"
                log.warning("Could not persist offgrid state: %s", exc)
                return False

    @staticmethod
    def _atomic_write_yaml(path: str, data: dict[str, Any], mode: int = 0o600) -> None:
        """Atomically write and durably fsync a YAML mapping."""
        dir_name = os.path.dirname(path) or "."
        os.makedirs(dir_name, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dir_name, prefix=".reticulumpi_", suffix=".tmp")
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            os.chmod(path, mode)
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            dir_fd = os.open(dir_name, directory_flags)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _persist_runtime_overrides(self) -> bool:
        """Persist only service-authorized controls, never system config."""
        path = self._runtime_overrides_path
        if not path:
            return False
        data = {
            "internet": {
                "force_offline": bool(self._data.get("internet", {}).get("force_offline", False))
            }
        }
        self._atomic_write_yaml(path, data, mode=0o600)
        return True

    def _persist(self) -> None:
        """Compatibility wrapper for the former persistence hook."""
        if not self._persist_runtime_overrides():
            raise OSError("runtime override persistence is not configured")

    @property
    def plugins(self) -> dict[str, dict[str, Any]]:
        return dict(self._data.get("plugins", {}))

    @property
    def external_artifact_policy(self) -> ExternalArtifactPolicy:
        """Return the validated external-tool enforcement policy."""

        return self._external_artifact_policy
