"""Application configuration loader."""

import logging
import os
from typing import Any

import yaml

log = logging.getLogger(__name__)

DEFAULT_CONFIG: dict[str, Any] = {
    "reticulum_config_dir": None,
    "use_shared_instance": True,
    "identity_path": "~/.config/reticulumpi/identity",
    "log_level": 4,
    "plugin_paths": [],
    "plugins": {},
}


class AppConfig:
    """Loads and provides typed access to the reticulumPi YAML config."""

    def __init__(self, config_path: str | None = None):
        self._data: dict[str, Any] = dict(DEFAULT_CONFIG)
        if config_path:
            self._load_file(config_path)

    def _load_file(self, path: str) -> None:
        path = os.path.expanduser(path)
        if not os.path.isfile(path):
            log.warning("Config file not found: %s, using defaults", path)
            return
        with open(path, "r") as f:
            raw = yaml.safe_load(f)
        if raw and isinstance(raw, dict):
            app_section = raw.get("reticulumpi", raw)
            self._data.update(app_section)
            log.info("Loaded config from %s", path)

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
    def plugins(self) -> dict[str, dict[str, Any]]:
        return self._data.get("plugins", {})
