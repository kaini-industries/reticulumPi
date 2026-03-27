"""Application configuration loader."""

import logging
import os
import socket
from typing import Any

import yaml

log = logging.getLogger(__name__)

VALID_KEYS = {
    "node_name",
    "reticulum_config_dir",
    "use_shared_instance",
    "identity_path",
    "log_level",
    "plugin_paths",
    "plugins",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "reticulum_config_dir": None,
    "use_shared_instance": True,
    "identity_path": "~/.config/reticulumpi/identity",
    "log_level": 4,
    "plugin_paths": [],
    "plugins": {},
}


class ConfigError(Exception):
    """Raised when config is invalid."""


class AppConfig:
    """Loads and provides typed access to the reticulumPi YAML config."""

    def __init__(self, config_path: str | None = None):
        self._config_path = config_path
        self._data: dict[str, Any] = {
            k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
            for k, v in DEFAULT_CONFIG.items()
        }
        if config_path:
            self._load_file(config_path)
        self._validate()

    @property
    def config_path(self) -> str | None:
        """Return the resolved config file path, or None if using defaults."""
        if self._config_path:
            return os.path.expanduser(self._config_path)
        return None

    def _load_file(self, path: str) -> None:
        path = os.path.expanduser(path)
        if not os.path.isfile(path):
            log.warning("Config file not found: %s, using defaults", path)
            return
        try:
            with open(path, "r") as f:
                raw = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML in {path}: {e}") from e
        if raw and isinstance(raw, dict):
            if "reticulumpi" not in raw:
                log.warning("Config file missing 'reticulumpi:' section, ignoring contents")
                return
            app_section = raw["reticulumpi"]
            if app_section and isinstance(app_section, dict):
                self._data.update(app_section)
            log.info("Loaded config from %s", path)

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
    def plugins(self) -> dict[str, dict[str, Any]]:
        return dict(self._data.get("plugins", {}))
