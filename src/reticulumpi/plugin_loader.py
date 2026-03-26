"""Filesystem-based plugin discovery and loading."""

import glob
import importlib.util
import logging
import os
from typing import Any

from reticulumpi.plugin_base import PluginBase

log = logging.getLogger(__name__)


class PluginLoader:
    """Discovers and loads PluginBase subclasses from directories."""

    def discover(self, plugin_dirs: list[str]) -> dict[str, type[PluginBase]]:
        """Scan directories for .py files containing PluginBase subclasses.

        Returns a dict mapping plugin_name -> plugin class.
        """
        found: dict[str, type[PluginBase]] = {}
        for directory in plugin_dirs:
            if not os.path.isdir(directory):
                log.warning("Plugin directory does not exist: %s", directory)
                continue
            for filepath in sorted(glob.glob(os.path.join(directory, "*.py"))):
                basename = os.path.basename(filepath)
                if basename.startswith("_"):
                    continue
                try:
                    module = self._load_module_from_path(filepath)
                except Exception:
                    log.exception("Failed to load plugin module: %s", filepath)
                    continue
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, PluginBase)
                        and attr is not PluginBase
                        and attr.plugin_name != "unnamed"
                    ):
                        found[attr.plugin_name] = attr
                        log.info("Discovered plugin: %s (from %s)", attr.plugin_name, filepath)
        return found

    def _load_module_from_path(self, filepath: str) -> Any:
        module_name = os.path.basename(filepath).replace(".py", "")
        spec = importlib.util.spec_from_file_location(module_name, filepath)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot create module spec for {filepath}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
