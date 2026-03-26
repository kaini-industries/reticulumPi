"""Core reticulumPi application orchestrator."""

import logging
import os
import signal
import threading
from typing import Any

import RNS

from reticulumpi import identity_manager
from reticulumpi.config import AppConfig
from reticulumpi.plugin_base import PluginBase
from reticulumpi.plugin_loader import PluginLoader

log = logging.getLogger(__name__)


class ReticulumPiApp:
    """Main application that initializes Reticulum, loads plugins, and manages lifecycle."""

    def __init__(self, config_path: str | None = None, reticulum_config_dir: str | None = None):
        self.config = AppConfig(config_path)
        self._reticulum_config_dir = reticulum_config_dir or self.config.reticulum_config_dir
        self.reticulum: RNS.Reticulum | None = None
        self.identity: RNS.Identity | None = None
        self.plugins: dict[str, PluginBase] = {}
        self._shutdown_event = threading.Event()
        self._plugin_loader = PluginLoader()

    def start(self) -> None:
        """Initialize Reticulum, load identity, start plugins, and enter the run loop."""
        log.info("Starting ReticulumPi v%s", self._get_version())

        self.reticulum = RNS.Reticulum(
            configdir=self._reticulum_config_dir,
            loglevel=self.config.log_level,
        )
        log.info("Reticulum initialized")

        self.identity = identity_manager.load_or_create(self.config.identity_path)
        log.info("Node identity hash: %s", RNS.prettyhexrep(self.identity.hash))

        self._load_plugins()

        for name, plugin in self.plugins.items():
            try:
                plugin.start()
                log.info("Started plugin: %s", name)
            except Exception:
                log.exception("Failed to start plugin: %s", name)

        self._install_signal_handlers()
        log.info("ReticulumPi is running. Press Ctrl+C to stop.")
        self._shutdown_event.wait()

    def shutdown(self) -> None:
        """Gracefully stop all plugins and signal the run loop to exit."""
        log.info("Shutting down ReticulumPi...")
        for name, plugin in reversed(list(self.plugins.items())):
            try:
                plugin.stop()
                log.info("Stopped plugin: %s", name)
            except Exception:
                log.exception("Error stopping plugin: %s", name)
        self._shutdown_event.set()
        log.info("ReticulumPi stopped.")

    def get_plugin(self, name: str) -> PluginBase | None:
        """Get a running plugin by name, for inter-plugin communication."""
        return self.plugins.get(name)

    def _load_plugins(self) -> None:
        builtin_plugin_dir = os.path.join(os.path.dirname(__file__), "..", "..", "plugins")
        builtin_plugin_dir = os.path.normpath(builtin_plugin_dir)

        search_dirs = [builtin_plugin_dir] + self.config.plugin_paths
        available = self._plugin_loader.discover(search_dirs)

        for plugin_name, plugin_config in self.config.plugins.items():
            if not plugin_config.get("enabled", False):
                continue
            if plugin_name not in available:
                log.warning("Plugin '%s' is enabled but not found in plugin directories", plugin_name)
                continue
            plugin_cls = available[plugin_name]
            try:
                instance = plugin_cls(self, plugin_config)
                self.plugins[plugin_name] = instance
                log.info("Loaded plugin: %s v%s", plugin_name, plugin_cls.plugin_version)
            except Exception:
                log.exception("Failed to instantiate plugin: %s", plugin_name)

    def _install_signal_handlers(self) -> None:
        def _handle_signal(signum: int, frame: Any) -> None:
            log.info("Received signal %d", signum)
            self.shutdown()

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

    @staticmethod
    def _get_version() -> str:
        from reticulumpi import __version__
        return __version__
