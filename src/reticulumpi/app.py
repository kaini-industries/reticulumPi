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

    def __init__(
        self,
        config_path: str | None = None,
        reticulum_config_dir: str | None = None,
        log_level_override: int | None = None,
    ):
        self.config = AppConfig(config_path)
        self._reticulum_config_dir = reticulum_config_dir or self.config.reticulum_config_dir
        self._log_level = log_level_override if log_level_override is not None else self.config.log_level
        self.reticulum: RNS.Reticulum | None = None
        self.identity: RNS.Identity | None = None
        self.plugins: dict[str, PluginBase] = {}
        self._failed_plugins: list[tuple[str, str]] = []
        self._shutdown_event = threading.Event()
        self._plugin_loader = PluginLoader()

    def start(self) -> None:
        """Initialize Reticulum, load identity, start plugins, and enter the run loop."""
        log.info("Starting ReticulumPi v%s", self._get_version())

        self.reticulum = RNS.Reticulum(
            configdir=self._reticulum_config_dir,
            loglevel=self._log_level,
            require_shared_instance=self.config.use_shared_instance,
        )
        log.info("Reticulum initialized")

        self.identity = identity_manager.load_or_create(self.config.identity_path)
        log.info("Node identity hash: %s", RNS.prettyhexrep(self.identity.hash))

        self._load_plugins()

        for name, plugin in list(self.plugins.items()):
            try:
                plugin.start()
                log.info("Started plugin: %s", name)
            except Exception as exc:
                reason = f"start() failed: {exc}"
                self._failed_plugins.append((name, reason))
                log.exception("Failed to start plugin: %s", name)
                del self.plugins[name]

        self._print_startup_report()
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

    def get_status(self) -> dict[str, Any]:
        """Collect status from all running plugins."""
        status: dict[str, Any] = {
            "version": self._get_version(),
            "plugins": {},
            "failed_plugins": [
                {"name": name, "error": reason}
                for name, reason in self._failed_plugins
            ],
        }
        for name, plugin in self.plugins.items():
            try:
                status["plugins"][name] = plugin.get_status()
            except Exception:
                status["plugins"][name] = {"error": "status collection failed"}
        return status

    def _get_plugin_search_dirs(self) -> list[str]:
        """Return the list of directories to search for plugins."""
        # Built-in plugins ship inside the package (always available)
        builtin_dir = os.path.join(os.path.dirname(__file__), "builtin_plugins")
        # Also check the top-level plugins/ dir (development editable installs)
        dev_plugin_dir = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..", "plugins")
        )
        dirs: list[str] = []
        for d in [builtin_dir, dev_plugin_dir]:
            if os.path.isdir(d) and d not in dirs:
                dirs.append(d)
        return dirs + self.config.plugin_paths

    def _load_plugins(self) -> None:
        search_dirs = self._get_plugin_search_dirs()
        available = self._plugin_loader.discover(search_dirs)

        for plugin_name, plugin_config in self.config.plugins.items():
            if not plugin_config.get("enabled", False):
                continue
            if plugin_name not in available:
                reason = "not found in plugin directories"
                self._failed_plugins.append((plugin_name, reason))
                log.warning("Plugin '%s' is enabled but %s", plugin_name, reason)
                continue
            plugin_cls = available[plugin_name]
            try:
                instance = plugin_cls(self, plugin_config)
                self.plugins[plugin_name] = instance
                log.info("Loaded plugin: %s v%s", plugin_name, plugin_cls.plugin_version)
            except Exception as exc:
                reason = f"instantiation failed: {exc}"
                self._failed_plugins.append((plugin_name, reason))
                log.exception("Failed to instantiate plugin: %s", plugin_name)

    def _print_startup_report(self) -> None:
        """Log a human-readable summary of the running system."""
        log.info("=== ReticulumPi v%s ===", self._get_version())
        log.info("Config: %s", self.config.config_path or "(defaults, no config file)")
        log.info(
            "Reticulum config: %s",
            self._reticulum_config_dir or "(default ~/.reticulum)",
        )
        if self.identity:
            log.info("Identity: %s", RNS.prettyhexrep(self.identity.hash))

        # Report active Reticulum interfaces
        try:
            interfaces = RNS.Transport.interfaces
            if interfaces:
                for iface in interfaces:
                    log.info("  Interface: %s", iface)
            else:
                log.info("  No active interfaces (may still be initializing)")
        except Exception:
            log.info("  Interfaces: unavailable")

        # Report loaded plugins
        if self.plugins:
            for name, plugin in self.plugins.items():
                log.info(
                    "  Plugin: %s v%s — %s",
                    name,
                    plugin.plugin_version,
                    plugin.plugin_description,
                )
        else:
            log.info("  No plugins loaded")

        # Report failed plugins prominently
        for name, reason in self._failed_plugins:
            log.warning("  FAILED plugin: %s — %s", name, reason)

    def check(self) -> bool:
        """Dry-run validation: check config, discover plugins, report status.

        Returns True if all checks pass, False otherwise.
        """
        ok = True
        print(f"ReticulumPi v{self._get_version()} — config check")
        print(f"  App config:       {self.config.config_path or '(defaults, no config file)'}")
        print(f"  Reticulum config: {self._reticulum_config_dir or '(default ~/.reticulum)'}")
        print("  Config validation: OK")
        print()

        search_dirs = self._get_plugin_search_dirs()
        available = self._plugin_loader.discover(search_dirs)

        if available:
            print("Discovered plugins:")
            for name, cls in sorted(available.items()):
                print(f"  {name:<24} v{cls.plugin_version:<8} {cls.plugin_description}")
        else:
            print(f"No plugins found in: {', '.join(search_dirs)}")
        print()

        enabled = {
            name: cfg
            for name, cfg in self.config.plugins.items()
            if cfg.get("enabled", False)
        }
        if enabled:
            print("Enabled plugin check:")
            for name in sorted(enabled):
                if name in available:
                    print(f"  {name}: OK")
                else:
                    print(f"  {name}: MISSING — not found in plugin directories")
                    ok = False
        else:
            print("No plugins enabled in config.")

        return ok

    def list_plugins(self) -> None:
        """Print all discoverable plugins with name, version, and description."""
        search_dirs = self._get_plugin_search_dirs()
        available = self._plugin_loader.discover(search_dirs)

        if not available:
            print(f"No plugins found in: {', '.join(search_dirs)}")
            return

        print("Available plugins:")
        for name, cls in sorted(available.items()):
            print(f"  {name:<24} v{cls.plugin_version:<8} {cls.plugin_description}")

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
