"""Filesystem-based plugin discovery and loading."""

from __future__ import annotations

import glob
import importlib.util
import logging
import os
import re
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
        builtin_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "builtin_plugins"
        )
        for directory in plugin_dirs:
            if not os.path.isdir(directory):
                log.warning("Plugin directory does not exist: %s", directory)
                continue
            if os.path.abspath(directory) != os.path.abspath(builtin_dir):
                log.warning(
                    "Loading plugins from external directory: %s "
                    "— ensure only trusted code is present",
                    directory,
                )
            for filepath in sorted(glob.glob(os.path.join(directory, "*.py"))):
                basename = os.path.basename(filepath)
                if basename.startswith("_"):
                    continue
                try:
                    module = self._load_module_from_path(filepath)
                except Exception:
                    log.exception("Failed to load plugin module: %s", filepath)
                    continue
                candidates = [
                    getattr(module, n)
                    for n in dir(module)
                    if isinstance(getattr(module, n), type)
                    and issubclass(getattr(module, n), PluginBase)
                    and getattr(module, n) is not PluginBase
                    and getattr(module, n).plugin_name != "unnamed"
                ]
                for attr in candidates:
                    if any(issubclass(o, attr) and o is not attr for o in candidates):
                        continue
                    if attr.plugin_name in found:
                        log.warning(
                            "Duplicate plugin name '%s' from %s (overrides previous)",
                            attr.plugin_name,
                            filepath,
                        )
                    found[attr.plugin_name] = attr
                    log.info("Discovered plugin: %s (from %s)", attr.plugin_name, filepath)
        return found

    def _load_module_from_path(self, filepath: str) -> Any:
        dir_part = re.sub(r"[^a-zA-Z0-9_]", "_", os.path.basename(os.path.dirname(filepath)))
        file_part = re.sub(r"[^a-zA-Z0-9_]", "_", os.path.basename(filepath).replace(".py", ""))
        module_name = f"reticulumpi_plugin_{dir_part}_{file_part}"
        spec = importlib.util.spec_from_file_location(module_name, filepath)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot create module spec for {filepath}")
        module = importlib.util.module_from_spec(spec)
        import sys

        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
