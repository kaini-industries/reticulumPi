"""Core reticulumPi application orchestrator."""

from __future__ import annotations

import concurrent.futures
import logging
import os
import signal
import threading
import time
from typing import Any

import RNS

from reticulumpi import events, identity_manager
from reticulumpi.announce_dispatcher import AnnounceDispatcher
from reticulumpi.config import AppConfig
from reticulumpi.event_bus import EventBus
from reticulumpi.plugin_base import PluginBase
from reticulumpi.internet_probe import InternetProbe
from reticulumpi.plugin_loader import PluginLoader
from reticulumpi.sdr_scheduler import SdrScheduler

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
        self._log_level = (
            log_level_override if log_level_override is not None else self.config.log_level
        )
        self.node_name: str = self.config.node_name
        self.reticulum: RNS.Reticulum | None = None
        self.identity: RNS.Identity | None = None
        self.plugins: dict[str, PluginBase] = {}
        self._plugins_lock = threading.Lock()
        self._failed_plugins: list[tuple[str, str]] = []
        self._shutdown_event = threading.Event()
        self._shutting_down = threading.Event()
        self._plugin_loader = PluginLoader()
        self.event_bus = EventBus()
        self.announce_dispatcher = AnnounceDispatcher()
        self.internet_probe: InternetProbe | None = None
        self.sdr_scheduler = SdrScheduler(
            self.event_bus,
            config=self.config.plugins.get("sdr_scheduler", {}),
        )

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

        self.announce_dispatcher.start()
        self.sdr_scheduler.start()

        self.internet_probe = InternetProbe(self.event_bus, self.config.internet)
        self.internet_probe.start()
        log.info("Internet: %s", "online" if self.internet_probe.is_online else "offline")

        PluginBase.set_thread_budget(self.config.thread_budget)

        self._load_plugins()

        start_order = self._topo_sort_plugins()
        for name in start_order:
            plugin = self.plugins[name]
            try:
                self._start_plugin_with_timeout(name, plugin, self.PLUGIN_START_TIMEOUT)
                log.info("Started plugin: %s", name)
                self.event_bus.publish(events.PLUGIN_STARTED, {"name": name})
            except Exception as exc:
                reason = f"start() failed: {exc}"
                self._failed_plugins.append((name, reason))
                log.exception("Failed to start plugin: %s", name)
                self.event_bus.publish(
                    events.PLUGIN_CRASHED,
                    {"name": name, "error": reason},
                )
                try:
                    plugin.stop()
                except Exception:
                    log.debug("Cleanup after failed start of %s also failed", name)
                with self._plugins_lock:
                    del self.plugins[name]

        with self._plugins_lock:
            self.plugins = {n: self.plugins[n] for n in start_order if n in self.plugins}

        if self.internet_probe is not None and not self.internet_probe.is_online:
            for name, plugin in self.plugins.items():
                try:
                    plugin.on_internet_lost()
                except Exception:
                    log.warning("Plugin %s: initial offline delivery failed", name, exc_info=True)

        self._print_startup_report()
        self._install_signal_handlers()
        log.info("ReticulumPi is running. Press Ctrl+C to stop.")
        self._shutdown_event.wait()
        self.shutdown()

    # Leave 15s headroom under systemd's TimeoutStopSec=60
    SHUTDOWN_TIMEOUT: float = 45.0
    PLUGIN_STOP_TIMEOUT: float = 10.0
    PLUGIN_START_TIMEOUT: float = 30.0

    def _pre_stop_signal_subprocesses(self) -> None:
        """Send SIGTERM to all plugin subprocesses simultaneously.

        Lets child processes begin winding down in parallel *before* the
        sequential per-plugin stop loop, so each plugin's blocking wait
        finds its process already exited or nearly so.
        """
        signalled: list[str] = []
        for name, plugin in list(self.plugins.items()):
            proc = getattr(plugin, "_process", None)
            if proc is None:
                continue
            try:
                if proc.poll() is not None:
                    continue
                os.kill(proc.pid, signal.SIGTERM)
                signalled.append(name)
            except (OSError, ProcessLookupError):
                pass
        if signalled:
            log.info(
                "Pre-stop SIGTERM sent to %d subprocess(es): %s",
                len(signalled),
                ", ".join(signalled),
            )

    def shutdown(self) -> None:
        """Gracefully stop all plugins and signal the run loop to exit."""
        if self._shutting_down.is_set():
            return
        self._shutting_down.set()
        deadline = time.monotonic() + self.SHUTDOWN_TIMEOUT
        log.info("Shutting down ReticulumPi (%.0fs budget)...", self.SHUTDOWN_TIMEOUT)

        # Give plugins a chance to flush buffers, deregister from network, etc.
        try:
            self.event_bus.publish(events.SHUTDOWN_STARTING, {})
        except Exception:
            log.exception("Error publishing shutdown event")

        self._pre_stop_signal_subprocesses()

        # Stop plugins in reverse order with per-plugin timeout
        with self._plugins_lock:
            shutdown_order = list(reversed(list(self.plugins.items())))
        for name, plugin in shutdown_order:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning("Shutdown deadline reached — skipping remaining plugins")
                break
            timeout = min(self.PLUGIN_STOP_TIMEOUT, remaining)
            try:
                self._stop_plugin_with_timeout(name, plugin, timeout)
            except Exception:
                log.exception("Error stopping plugin: %s", name)

        if self.internet_probe:
            self.internet_probe.stop()

        self.sdr_scheduler.stop()
        self.announce_dispatcher.stop()

        # Shut down event bus thread pool so offloaded callbacks drain cleanly.
        self.event_bus.shutdown()

        # Clean up Reticulum instance
        self._cleanup_rns()

        self._shutdown_event.set()
        elapsed = self.SHUTDOWN_TIMEOUT - (deadline - time.monotonic())
        log.info("ReticulumPi stopped in %.1fs.", elapsed)

    def _start_plugin_with_timeout(self, name: str, plugin: PluginBase, timeout: float) -> None:
        """Start a single plugin, enforcing a wall-clock timeout.

        Runs ``start()`` in a worker thread so that a hung plugin doesn't
        block the entire boot.  If the plugin registers signal handlers
        (only valid on the main thread), the worker will raise
        ``ValueError`` — in that case we fall back to running ``start()``
        directly on the main thread without a timeout.

        Raises TimeoutError if the plugin does not start in time.
        """
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(plugin.start)
            future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"Plugin '{name}' did not start within {timeout:.0f}s") from None
        except Exception as exc:
            if "signal only works in main thread" in str(exc):
                log.debug(
                    "Plugin '%s' requires main thread — retrying without timeout",
                    name,
                )
                plugin.start()
            else:
                raise
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _stop_plugin_with_timeout(self, name: str, plugin: PluginBase, timeout: float) -> None:
        """Stop a single plugin, enforcing a wall-clock timeout."""
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(plugin.stop)
            future.result(timeout=timeout)
            log.info("Stopped plugin: %s", name)
            self.event_bus.publish(events.PLUGIN_STOPPED, {"name": name})
        except concurrent.futures.TimeoutError:
            log.warning("Plugin '%s' did not stop within %.1fs — moving on", name, timeout)
        except Exception:
            log.exception("Error stopping plugin: %s", name)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _cleanup_rns(self) -> None:
        """Tear down the Reticulum instance if we own it."""
        if self.reticulum is None:
            return
        try:
            RNS.Transport.exit_handler()
            log.info("Reticulum transport cleaned up")
        except Exception:
            log.warning("Reticulum transport cleanup failed", exc_info=True)
        self.reticulum = None

    @property
    def offgrid_mode(self) -> bool:
        if self.internet_probe is not None:
            return self.internet_probe.force_offline
        return self.config.offgrid_mode

    def set_offgrid_mode(self, enabled: bool) -> dict[str, Any]:
        """Toggle off-grid mode: force internet probe offline and persist."""
        was_enabled = self.offgrid_mode
        if enabled == was_enabled:
            return {"enabled": enabled, "persisted": True}
        persisted = self.config.set_internet_force_offline(enabled)
        if self.internet_probe is not None:
            self.internet_probe.set_force_offline(enabled)
        self.event_bus.publish(events.OFFGRID_MODE_CHANGED, {"enabled": enabled})
        log.info("Off-grid mode %s", "enabled" if enabled else "disabled")
        return {"enabled": enabled, "persisted": persisted}

    def get_plugin(self, name: str) -> PluginBase | None:
        """Get a running plugin by name, for inter-plugin communication."""
        return self.plugins.get(name)

    def get_status(self) -> dict[str, Any]:
        """Collect status from all running plugins."""
        status: dict[str, Any] = {
            "version": self._get_version(),
            "plugins": {},
            "failed_plugins": [
                {"name": name, "error": reason} for name, reason in self._failed_plugins
            ],
        }
        for name, plugin in list(self.plugins.items()):
            try:
                status["plugins"][name] = plugin.get_status()
            except Exception:
                status["plugins"][name] = {"error": "status collection failed"}
        return status

    def enable_plugin(self, name: str) -> None:
        """Instantiate, start, and register a plugin by name at runtime.

        Raises KeyError if the plugin is not discoverable, RuntimeError if
        it is already running, or propagates any exception from instantiation
        or start().

        The lock is released before calling start() so a slow plugin
        start does not block status queries or other plugin operations.
        """
        with self._plugins_lock:
            if name in self.plugins:
                raise RuntimeError(f"Plugin '{name}' is already running")

            available = self._plugin_loader.discover(self._get_plugin_search_dirs())
            if name not in available:
                raise KeyError(f"Plugin '{name}' not found in plugin directories")

            plugin_config = self.config.plugins.get(name, {})
            plugin_cls = available[name]
            instance = plugin_cls(self, plugin_config)

        # Call start() outside the lock so slow plugins don't block
        try:
            instance.start()
        except Exception:
            try:
                instance.stop()
            except Exception:
                log.debug("Cleanup after failed hot-load of %s also failed", name)
            raise

        with self._plugins_lock:
            # Re-check after releasing and reacquiring the lock
            if name in self.plugins:
                try:
                    instance.stop()
                except Exception:
                    log.debug("Cleanup of duplicate hot-load %s failed", name)
                raise RuntimeError(f"Plugin '{name}' was loaded concurrently")
            self.plugins[name] = instance

        log.info("Hot-loaded plugin: %s", name)
        self.event_bus.publish(events.PLUGIN_STARTED, {"name": name})

    def disable_plugin(self, name: str) -> None:
        """Stop and unregister a running plugin by name.

        Raises KeyError if the plugin is not currently running.
        """
        self.event_bus.publish(events.PLUGIN_STOPPING, {"name": name})
        with self._plugins_lock:
            dep_names = [
                other_name
                for other_name, other_plugin in self.plugins.items()
                if name in getattr(other_plugin, "plugin_dependencies", [])
            ]
            if dep_names:
                raise RuntimeError(f"cannot disable {name}: dependents still running: {dep_names}")
            plugin = self.plugins.pop(name, None)
            if plugin is None:
                raise KeyError(f"Plugin '{name}' is not running")

        try:
            plugin.stop()
        except Exception:
            log.exception("Error stopping plugin '%s' during disable", name)
        log.info("Disabled plugin: %s", name)
        self.event_bus.publish(events.PLUGIN_STOPPED, {"name": name})

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

        for name, instance in self.plugins.items():
            for dep in getattr(instance, "plugin_dependencies", []):
                if dep not in self.plugins:
                    log.warning(
                        "Plugin '%s' depends on '%s' which is not enabled",
                        name,
                        dep,
                    )

    def _topo_sort_plugins(self) -> list[str]:
        """Return plugin names in dependency order (dependees first).

        Raises RuntimeError if a dependency cycle is detected.
        """
        remaining = set(self.plugins)
        order: list[str] = []
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name not in remaining:
                return
            if name in visiting:
                cycle_members = sorted(visiting)
                log.error("dependency cycle detected: %s", cycle_members)
                raise RuntimeError(f"dependency cycle detected: {cycle_members}")
            if name in visited:
                return
            visiting.add(name)
            for dep in getattr(self.plugins[name], "plugin_dependencies", []):
                if dep in remaining:
                    visit(dep)
            visiting.discard(name)
            visited.add(name)
            order.append(name)

        for name in list(self.plugins):
            visit(name)
        return order

    def _print_startup_report(self) -> None:
        """Log a human-readable summary of the running system."""
        log.info("=== ReticulumPi v%s ===", self._get_version())
        log.info("Node name: %s", self.node_name)
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
            for name, plugin in list(self.plugins.items()):
                log.info(
                    "  Plugin: %s v%s — %s",
                    name,
                    plugin.plugin_version,
                    plugin.plugin_description,
                )
        else:
            log.info("  No plugins loaded")

        # Report failed plugins prominently
        if self._failed_plugins:
            log.error("  %d plugin(s) FAILED to load:", len(self._failed_plugins))
            for name, reason in self._failed_plugins:
                log.error("    - %s: %s", name, reason)

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
            name: cfg for name, cfg in self.config.plugins.items() if cfg.get("enabled", False)
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
            log.info("Received signal %d — requesting shutdown", signum)
            self._shutdown_event.set()

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

    @staticmethod
    def _get_version() -> str:
        from reticulumpi import __version__

        return __version__
