"""Core reticulumPi application orchestrator."""

from __future__ import annotations

import logging
import os
import re
import signal
import sqlite3
import threading
import time
import weakref
from typing import Any, Sequence

import RNS

from reticulumpi import events, identity_manager
from reticulumpi.announce_dispatcher import AnnounceDispatcher
from reticulumpi.builtin_plugins.web_dashboard import get_dashboard_operational_metrics
from reticulumpi.config import AppConfig
from reticulumpi.event_bus import EventBus
from reticulumpi.internet_probe import InternetProbe
from reticulumpi.migrations import get_migration_metrics, migrate_target
from reticulumpi.plugin_base import PluginBase, PluginHealth, PluginState
from reticulumpi.plugin_loader import PluginLoader
from reticulumpi.rtlsdr import get_lease_metrics
from reticulumpi.runtime_metrics import get_runtime_metrics
from reticulumpi.sdr_scheduler import SdrScheduler
from reticulumpi.systemd_notify import ready as systemd_ready
from reticulumpi.systemd_notify import set_readiness_file
from reticulumpi.systemd_notify import stopping as systemd_stopping

log = logging.getLogger(__name__)


def run_db_migrations(
    conn: sqlite3.Connection,
    migrations: Sequence[str],
    *,
    logger: logging.Logger | None = None,
) -> int:
    """Apply pending PRAGMA user_version-based migrations to *conn*.

    Each entry in *migrations* is a SQL string (may contain multiple
    statements separated by ``;``).  ``migrations[0]`` is version 1,
    ``migrations[1]`` is version 2, etc.

    Returns the new user_version after all pending migrations have run.
    Raises on SQL errors — callers should handle rollback if needed.

    Usage from a plugin::

        from reticulumpi.app import run_db_migrations

        MIGRATIONS = [
            "CREATE TABLE IF NOT EXISTS foo (id INTEGER PRIMARY KEY);",
            "ALTER TABLE foo ADD COLUMN bar TEXT DEFAULT '';",
        ]
        run_db_migrations(conn, MIGRATIONS, logger=self.log)
    """
    _log = logger or log
    cur = conn.execute("PRAGMA user_version")
    current = cur.fetchone()[0]
    if current >= len(migrations):
        return current
    forbidden = re.compile(
        r"(?:^|;)\s*(?:BEGIN|COMMIT|END|ROLLBACK|VACUUM|ATTACH|DETACH)\b",
        re.IGNORECASE | re.MULTILINE,
    )
    for idx in range(current, len(migrations)):
        version = idx + 1
        _log.info("Applying DB migration %d/%d", version, len(migrations))
        script = migrations[idx]
        if forbidden.search(script):
            raise ValueError(
                f"Migration {version} contains transaction control or an "
                "operation that cannot run atomically"
            )
        if conn.in_transaction:
            raise RuntimeError("Cannot start a migration inside an active transaction")
        try:
            # sqlite3.executescript() commits an existing transaction before
            # executing.  Put BEGIN and COMMIT inside the script itself so the
            # migration statements and version marker are one atomic unit.  A
            # standalone separator keeps the documented optional trailing
            # semicolon from joining the script to PRAGMA user_version.
            conn.executescript(
                f"BEGIN IMMEDIATE;\n{script}\n;\nPRAGMA user_version = {version};\nCOMMIT;"
            )
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    return len(migrations)


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
        self._plugin_transition_lock = threading.RLock()
        self._plugin_state_history: dict[str, tuple[PluginState, str | None]] = {}
        self._failed_plugins: list[tuple[str, str]] = []
        self._plugin_dependency_cycles: dict[str, tuple[str, ...]] = {}
        self._operational_metrics_lock = threading.Lock()
        self._retired_plugins: weakref.WeakSet[PluginBase] = weakref.WeakSet()
        self._retired_lifecycle_metrics: dict[str, int | float] = {
            "readiness_count": 0,
            "readiness_total_seconds": 0.0,
            "readiness_max_seconds": 0.0,
            "hung_total": 0,
            "cleanup_failures_total": 0,
        }
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
        startup_deadline = time.monotonic() + self.STARTUP_TIMEOUT
        set_readiness_file(False)
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
        self._block_invalid_plugin_metadata()

        start_order = self._topo_sort_plugins()
        self._block_dependency_cycles()
        for name in start_order:
            plugin = self.plugins[name]
            if plugin.plugin_state == PluginState.BLOCKED:
                continue
            blocked_reason = self._hard_dependency_problem(plugin)
            if blocked_reason is not None:
                plugin.mark_blocked(blocked_reason)
                self._record_plugin_state(name, plugin)
                self._failed_plugins.append((name, blocked_reason))
                log.error("Blocked plugin '%s': %s", name, blocked_reason)
                self.event_bus.publish(
                    events.PLUGIN_BLOCKED,
                    {"name": name, "reason": blocked_reason},
                )
                continue
            try:
                self._migrate_plugin(name, plugin)
            except Exception as exc:
                reason = f"migration failed: {exc}"
                plugin.mark_blocked(reason)
                plugin.cleanup_managed_resources()
                self._record_plugin_state(name, plugin)
                self._failed_plugins.append((name, reason))
                log.exception("Blocked plugin after migration failure: %s", name)
                self.event_bus.publish(
                    events.PLUGIN_BLOCKED,
                    {"name": name, "reason": reason},
                )
                continue
            remaining_startup = startup_deadline - time.monotonic()
            if remaining_startup <= 0:
                reason = f"global startup deadline of {self.STARTUP_TIMEOUT:.0f}s exceeded"
                plugin.mark_blocked(reason)
                plugin.cleanup_managed_resources()
                self._record_plugin_state(name, plugin)
                self._failed_plugins.append((name, reason))
                self.event_bus.publish(
                    events.PLUGIN_BLOCKED,
                    {"name": name, "reason": reason},
                )
                continue
            try:
                self._start_plugin_with_timeout(
                    name,
                    plugin,
                    min(self.PLUGIN_START_TIMEOUT, remaining_startup),
                )
                log.info("Started plugin: %s", name)
                self._record_plugin_state(name, plugin)
                self.event_bus.publish(events.PLUGIN_STARTED, {"name": name})
            except Exception as exc:
                reason = f"start() failed: {exc}"
                self._failed_plugins.append((name, reason))
                log.exception("Failed to start plugin: %s", name)
                self.event_bus.publish(
                    events.PLUGIN_CRASHED,
                    {"name": name, "error": reason},
                )
                if plugin.plugin_state != PluginState.HUNG:
                    self._stop_plugin_with_timeout(
                        name,
                        plugin,
                        min(5.0, self.PLUGIN_STOP_TIMEOUT),
                        publish_event=False,
                    )
                    plugin.mark_start_failed(reason)
                self._record_plugin_state(name, plugin)
                if plugin.plugin_state != PluginState.HUNG:
                    self._archive_plugin_operational_metrics(plugin)
                    with self._plugins_lock:
                        self.plugins.pop(name, None)

        with self._plugins_lock:
            self.plugins = {n: self.plugins[n] for n in start_order if n in self.plugins}

        if self.internet_probe is not None and not self.internet_probe.is_online:
            for name, plugin in self.plugins.items():
                if plugin.plugin_state != PluginState.READY:
                    continue
                try:
                    plugin.on_internet_lost()
                except Exception:
                    log.warning("Plugin %s: initial offline delivery failed", name, exc_info=True)

        required_failures = self._required_plugin_failures()
        if required_failures:
            detail = "; ".join(required_failures)
            log.error("Required plugin readiness failed: %s", detail)
            self.shutdown()
            raise RuntimeError(f"required plugin readiness failed: {detail}")

        self._print_startup_report()
        self._install_signal_handlers()
        set_readiness_file(True)
        systemd_ready("RNS and required ReticulumPi plugins are ready")
        log.info("ReticulumPi is running. Press Ctrl+C to stop.")
        self._shutdown_event.wait()
        self.shutdown()

    # Leave 15s headroom under systemd's TimeoutStopSec=60
    SHUTDOWN_TIMEOUT: float = 45.0
    # Leave systemd ten seconds to collect diagnostics before its 120s limit.
    STARTUP_TIMEOUT: float = 110.0
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
            process_group = getattr(plugin, "_process_group", None)
            request_group_stop = getattr(process_group, "request_stop", None)
            if callable(request_group_stop):
                try:
                    if request_group_stop():
                        signalled.append(name)
                    continue
                except Exception:
                    log.debug(
                        "Managed process pre-stop failed for %s",
                        name,
                        exc_info=True,
                    )
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
        # Serialize the shutdown boundary with hot transitions. A transition
        # already in progress completes before the shutdown snapshot; every
        # later enable/disable observes the flag and is rejected.
        with self._plugin_transition_lock:
            if self._shutting_down.is_set():
                return
            self._shutting_down.set()
        set_readiness_file(False)
        systemd_stopping("ReticulumPi graceful shutdown in progress")
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
        for index, (name, plugin) in enumerate(shutdown_order):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pending = shutdown_order[index:]
                log.warning(
                    "Shutdown deadline reached — cancelling %d remaining plugins",
                    len(pending),
                )
                for pending_name, pending_plugin in pending:
                    request_stop = getattr(pending_plugin, "request_stop", None)
                    mark_hung = getattr(pending_plugin, "mark_hung", None)
                    cleanup = getattr(pending_plugin, "cleanup_managed_resources", None)
                    if callable(request_stop):
                        request_stop()
                    if callable(mark_hung):
                        mark_hung("global shutdown deadline reached")
                    if isinstance(pending_plugin, PluginBase):
                        self._record_plugin_state(pending_name, pending_plugin)
                    if callable(cleanup):
                        threading.Thread(
                            target=cleanup,
                            kwargs={"timeout": 0.0},
                            name=f"plugin-deadline-cleanup-{pending_name}",
                            daemon=True,
                        ).start()
                break
            if plugin.plugin_state == PluginState.HUNG:
                log.warning("Skipping concurrent stop of hung plugin '%s'", name)
                request_stop = getattr(plugin, "request_stop", None)
                cleanup = getattr(plugin, "cleanup_managed_resources", None)
                if callable(request_stop):
                    request_stop()
                if callable(cleanup):
                    threading.Thread(
                        target=cleanup,
                        kwargs={"timeout": remaining},
                        name=f"plugin-hung-cleanup-{name}",
                        daemon=True,
                    ).start()
                continue
            if plugin.plugin_state == PluginState.BLOCKED:
                plugin.cleanup_managed_resources(timeout=remaining)
                plugin.mark_stopped()
                continue
            timeout = min(self.PLUGIN_STOP_TIMEOUT, remaining)
            try:
                self._stop_plugin_with_timeout(name, plugin, timeout)
            except Exception:
                log.exception("Error stopping plugin: %s", name)

        def _bounded_shared_stop(label: str, callback: Any) -> None:
            remaining = max(0.0, deadline - time.monotonic())
            done = threading.Event()

            def _stop() -> None:
                try:
                    callback()
                except Exception:
                    log.exception("Error stopping %s", label)
                finally:
                    done.set()

            threading.Thread(
                target=_stop,
                name=f"shutdown-{label}",
                daemon=True,
            ).start()
            if not done.wait(remaining):
                log.warning("Shutdown deadline abandoned %s cleanup", label)

        if self.internet_probe:
            _bounded_shared_stop("internet-probe", self.internet_probe.stop)
        _bounded_shared_stop("sdr-scheduler", self.sdr_scheduler.stop)
        _bounded_shared_stop("announce-dispatcher", self.announce_dispatcher.stop)
        _bounded_shared_stop("event-bus", self.event_bus.shutdown)
        _bounded_shared_stop("reticulum", self._cleanup_rns)

        self._shutdown_event.set()
        elapsed = self.SHUTDOWN_TIMEOUT - (deadline - time.monotonic())
        log.info("ReticulumPi shutdown sequence ended in %.1fs.", elapsed)

    def _required_plugin_failures(self) -> list[str]:
        """Return enabled, explicitly-required plugins that are not ready."""

        failures: list[str] = []
        for name, config in self.config.plugins.items():
            if not isinstance(config, dict):
                continue
            if not config.get("enabled", False) or not config.get("required", False):
                continue
            state = self.get_plugin_state(name)
            if state != PluginState.READY:
                value = state.value if state is not None else "not discovered"
                failures.append(f"{name} is {value}")
        return failures

    def _start_plugin_with_timeout(self, name: str, plugin: PluginBase, timeout: float) -> None:
        """Start a single plugin, enforcing a wall-clock timeout.

        The lifecycle worker is daemonized so a permanently blocked third-party
        plugin cannot hold interpreter exit.  On timeout only cooperative stop
        is requested; ``stop()`` is never invoked concurrently with ``start()``.
        """
        plugin.mark_starting()
        self._record_plugin_state(name, plugin)
        self.event_bus.publish(events.PLUGIN_STARTING, {"name": name})
        done = threading.Event()
        failure: list[BaseException] = []

        def _start() -> None:
            try:
                plugin.start()
            except BaseException as exc:
                failure.append(exc)
            finally:
                done.set()

        started = time.monotonic()
        worker = threading.Thread(
            target=_start,
            name=f"plugin-start-{name}",
            daemon=True,
        )

        def _request_timeout_cleanup() -> None:
            # Managed cleanup is one-shot. Run it on a daemon so a defective
            # third-party cleanup cannot extend the lifecycle deadline or hold
            # interpreter exit. ``stop()`` is not called here and therefore
            # never overlaps an unfinished start.
            threading.Thread(
                target=plugin.cleanup_managed_resources,
                name=f"plugin-timeout-cleanup-{name}",
                daemon=True,
            ).start()

        worker.start()
        if not done.wait(timeout=timeout):
            reason = f"Plugin '{name}' did not start within {timeout:.0f}s"
            plugin.request_stop()
            plugin.mark_hung(reason)
            self._record_plugin_state(name, plugin)
            _request_timeout_cleanup()
            raise TimeoutError(reason)
        if failure:
            reason = str(failure[0])
            plugin.mark_start_failed(reason)
            self._record_plugin_state(name, plugin)
            raise failure[0]

        remaining = max(0.0, timeout - (time.monotonic() - started))
        if plugin.plugin_lifecycle_api >= 2:
            if not plugin.wait_until_ready(timeout=remaining):
                if plugin.plugin_state == PluginState.FAILED:
                    raise RuntimeError(
                        plugin.get_lifecycle_status().get("reason") or "plugin start failed"
                    )
                reason = f"Plugin '{name}' did not become ready within {timeout:.0f}s"
                plugin.request_stop()
                plugin.mark_hung(reason)
                self._record_plugin_state(name, plugin)
                _request_timeout_cleanup()
                raise TimeoutError(reason)
        else:
            plugin.mark_ready()

    def _stop_plugin_with_timeout(
        self,
        name: str,
        plugin: PluginBase,
        timeout: float,
        *,
        publish_event: bool = True,
    ) -> bool:
        """Cooperatively stop a plugin within a hard caller deadline."""

        plugin.request_stop()
        done = threading.Event()
        failure: list[BaseException] = []

        def _stop() -> None:
            try:
                plugin.stop()
            except BaseException as exc:
                failure.append(exc)
            finally:
                try:
                    plugin.cleanup_managed_resources()
                finally:
                    done.set()

        worker = threading.Thread(
            target=_stop,
            name=f"plugin-stop-{name}",
            daemon=True,
        )
        worker.start()
        if not done.wait(timeout=timeout):
            reason = f"Plugin '{name}' did not stop within {timeout:.1f}s"
            plugin.mark_hung(reason)
            self._record_plugin_state(name, plugin)
            threading.Thread(
                target=plugin.cleanup_managed_resources,
                name=f"plugin-timeout-cleanup-{name}",
                daemon=True,
            ).start()
            log.warning("%s", reason)
            return False
        if failure:
            reason = f"stop() failed: {failure[0]}"
            plugin.mark_start_failed(reason)
            self._record_plugin_state(name, plugin)
            log.exception("Error stopping plugin: %s", name, exc_info=failure[0])
            return False
        plugin.mark_stopped()
        self._record_plugin_state(name, plugin)
        log.info("Stopped plugin: %s", name)
        if publish_event:
            self.event_bus.publish(events.PLUGIN_STOPPED, {"name": name})
        return True

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
            return {
                "enabled": enabled,
                "applied": False,
                "persisted": True,
                "reason": "unchanged",
            }
        persisted = self.config.set_internet_force_offline(enabled)
        if self.internet_probe is not None:
            self.internet_probe.set_force_offline(enabled)
        self.event_bus.publish(events.OFFGRID_MODE_CHANGED, {"enabled": enabled})
        log.info("Off-grid mode %s", "enabled" if enabled else "disabled")
        return {
            "enabled": enabled,
            "applied": True,
            "persisted": persisted,
            "reason": self.config.last_persistence_reason,
        }

    def get_plugin(self, name: str) -> PluginBase | None:
        """Get a registered plugin (legacy API preserved through 0.4.x)."""

        with self._plugins_lock:
            return self.plugins.get(name)

    def get_ready_plugin(self, name: str) -> PluginBase | None:
        """Return a plugin only after its lifecycle reaches READY."""

        with self._plugins_lock:
            plugin = self.plugins.get(name)
        if plugin is None or plugin.plugin_state != PluginState.READY:
            return None
        return plugin

    def get_plugin_state(self, name: str) -> PluginState | None:
        """Return current or most recently recorded lifecycle state."""

        with self._plugins_lock:
            plugin = self.plugins.get(name)
        if plugin is not None:
            return plugin.plugin_state
        historic = self._plugin_state_history.get(name)
        return historic[0] if historic is not None else None

    def _record_plugin_state(self, name: str, plugin: PluginBase) -> None:
        lifecycle = plugin.get_lifecycle_status()
        self._plugin_state_history[name] = (
            plugin.plugin_state,
            lifecycle.get("reason") if isinstance(lifecycle.get("reason"), str) else None,
        )

    def _hard_dependency_problem(self, plugin: PluginBase) -> str | None:
        try:
            dependencies = self._dependency_names(plugin, "plugin_dependencies")
        except ValueError as exc:
            return str(exc)
        for dependency in dependencies:
            with self._plugins_lock:
                provider = self.plugins.get(dependency)
            if provider is None:
                return f"hard dependency '{dependency}' is not enabled"
            if provider.plugin_state != PluginState.READY:
                return (
                    f"hard dependency '{dependency}' is not ready ({provider.plugin_state.value})"
                )
        return None

    @staticmethod
    def _dependency_names(plugin: PluginBase, attribute: str) -> tuple[str, ...]:
        raw = getattr(plugin, attribute, ())
        if not isinstance(raw, (tuple, list)):
            raise ValueError(f"{attribute} must be a tuple or list of plugin names")
        names: list[str] = []
        for dependency in raw:
            if (
                not isinstance(dependency, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", dependency) is None
            ):
                raise ValueError(f"{attribute} contains an invalid plugin name")
            if dependency not in names:
                names.append(dependency)
        return tuple(names)

    def _plugin_metadata_problem(self, name: str, plugin: PluginBase) -> str | None:
        declared_name = getattr(plugin, "plugin_name", None)
        if (
            not isinstance(declared_name, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", declared_name) is None
            or declared_name != name
        ):
            return f"invalid plugin_name metadata for registry key '{name}'"
        for attribute in ("plugin_dependencies", "plugin_soft_dependencies"):
            try:
                self._dependency_names(plugin, attribute)
            except (AttributeError, TypeError, ValueError) as exc:
                return str(exc)
        return None

    def _block_invalid_plugin_metadata(self) -> None:
        for name, plugin in tuple(self.plugins.items()):
            reason = self._plugin_metadata_problem(name, plugin)
            if reason is None:
                continue
            plugin.mark_blocked(reason)
            self._record_plugin_state(name, plugin)
            self._failed_plugins.append((name, reason))
            log.error("Blocked plugin '%s': %s", name, reason)
            self.event_bus.publish(events.PLUGIN_BLOCKED, {"name": name, "reason": reason})

    def _on_plugin_ready_lost(self, provider: PluginBase) -> None:
        """Transitively block and clean ready hard dependents."""

        if self._shutting_down.is_set():
            return
        with self._plugins_lock:
            snapshot = dict(self.plugins)
        provider_name = next((name for name, value in snapshot.items() if value is provider), None)
        if provider_name is None:
            return

        unavailable = {provider_name}
        blocked: list[tuple[str, PluginBase, str]] = []
        changed = True
        while changed:
            changed = False
            for name, plugin in snapshot.items():
                if name in unavailable or plugin.plugin_state != PluginState.READY:
                    continue
                try:
                    dependencies = self._dependency_names(plugin, "plugin_dependencies")
                except ValueError:
                    continue
                lost = next(
                    (dependency for dependency in dependencies if dependency in unavailable), None
                )
                if lost is None:
                    continue
                reason = f"hard dependency '{lost}' lost readiness"
                if plugin._mark_dependency_blocked(reason):
                    unavailable.add(name)
                    blocked.append((name, plugin, reason))
                    changed = True

        for name, plugin, reason in blocked:
            self._record_plugin_state(name, plugin)
            self.event_bus.publish(events.PLUGIN_BLOCKED, {"name": name, "reason": reason})

            def _stop_dependent(dependent: PluginBase = plugin, label: str = name) -> None:
                try:
                    dependent.stop()
                except Exception:
                    log.exception("Dependency-blocked plugin stop failed: %s", label)

            threading.Thread(
                target=_stop_dependent,
                name=f"plugin-dependency-stop-{name}",
                daemon=True,
            ).start()
            threading.Thread(
                target=plugin.cleanup_managed_resources,
                name=f"plugin-dependency-cleanup-{name}",
                daemon=True,
            ).start()

    @staticmethod
    def _migrate_plugin(name: str, plugin: PluginBase) -> None:
        """Apply all plugin-declared SQLite migrations before startup."""

        for target in plugin.get_migration_targets():
            result = migrate_target(target, dry_run=False)
            if result.applied:
                log.info(
                    "Migrated plugin %s database %s from v%d to v%d",
                    name,
                    result.target,
                    result.from_version,
                    result.to_version,
                )

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
                plugin_status = plugin.get_status()
                if isinstance(plugin, PluginBase):
                    plugin_status["_lifecycle"] = plugin.get_lifecycle_status()
                status["plugins"][name] = plugin_status
            except Exception:
                plugin_status = {"error": "status collection failed"}
                if isinstance(plugin, PluginBase):
                    plugin_status["_lifecycle"] = plugin.get_lifecycle_status()
                status["plugins"][name] = plugin_status
        status["operational_metrics"] = self._get_operational_metrics()
        return status

    def _archive_plugin_operational_metrics(self, plugin: PluginBase) -> None:
        """Retain lifetime counters when a plugin instance leaves the registry."""

        lifecycle_metrics = plugin.get_lifecycle_metrics()
        readiness = lifecycle_metrics.get("readiness_seconds")
        with self._operational_metrics_lock:
            if plugin in self._retired_plugins:
                return
            self._retired_plugins.add(plugin)
            if isinstance(readiness, (int, float)) and not isinstance(readiness, bool):
                duration = max(0.0, float(readiness))
                self._retired_lifecycle_metrics["readiness_count"] += 1
                self._retired_lifecycle_metrics["readiness_total_seconds"] += duration
                self._retired_lifecycle_metrics["readiness_max_seconds"] = max(
                    float(self._retired_lifecycle_metrics["readiness_max_seconds"]),
                    duration,
                )
            self._retired_lifecycle_metrics["hung_total"] += max(
                0,
                int(lifecycle_metrics.get("hung_total", 0)),
            )
            self._retired_lifecycle_metrics["cleanup_failures_total"] += max(
                0,
                int(lifecycle_metrics.get("cleanup_failures_total", 0)),
            )

    def _get_operational_metrics(self) -> dict[str, Any]:
        """Collect aggregate counters without names, paths, identities, or payloads."""

        with self._plugins_lock:
            plugins = [plugin for plugin in self.plugins.values() if isinstance(plugin, PluginBase)]
        with self._operational_metrics_lock:
            retired_lifecycle = dict(self._retired_lifecycle_metrics)

        states = {state.value: 0 for state in PluginState}
        health = {value.value: 0 for value in PluginHealth}
        managed_groups = 0
        managed_processes = 0
        raw_processes = 0
        process_restarts = 0
        readiness_durations: list[float] = []
        state_ages: list[float] = []
        hung_total = int(retired_lifecycle["hung_total"])
        cleanup_failures_total = int(retired_lifecycle["cleanup_failures_total"])
        rns_resources = {"links": 0, "destinations": 0, "request_handlers": 0}

        def _process_running(process: Any) -> bool:
            poll = getattr(process, "poll", None)
            if not callable(poll):
                return False
            try:
                return poll() is None
            except (OSError, ProcessLookupError):
                return False

        for plugin in plugins:
            states[plugin.plugin_state.value] += 1
            health[plugin.plugin_health.value] += 1
            lifecycle_metrics = plugin.get_lifecycle_metrics()
            readiness = lifecycle_metrics.get("readiness_seconds")
            if isinstance(readiness, (int, float)):
                readiness_durations.append(max(0.0, float(readiness)))
            state_age = lifecycle_metrics.get("state_age_seconds")
            if isinstance(state_age, (int, float)):
                state_ages.append(max(0.0, float(state_age)))
            hung_total += max(0, int(lifecycle_metrics.get("hung_total", 0)))
            cleanup_failures_total += max(
                0,
                int(lifecycle_metrics.get("cleanup_failures_total", 0)),
            )
            resources = lifecycle_metrics.get("rns_resources", {})
            if isinstance(resources, dict):
                for resource in rns_resources:
                    rns_resources[resource] += max(0, int(resources.get(resource, 0)))
            group = getattr(plugin, "_process_group", None)
            if group is not None:
                try:
                    processes = tuple(group.processes)
                    running = bool(group.running)
                    restarts = int(group.restart_count)
                except (AttributeError, TypeError, ValueError):
                    processes = ()
                    running = False
                    restarts = 0
                if running or processes:
                    managed_groups += 1
                managed_processes += sum(_process_running(process) for process in processes)
                process_restarts += max(0, restarts)
                continue
            process = getattr(plugin, "_process", None)
            raw_processes += _process_running(process)

        def _stats(component: Any) -> dict[str, int]:
            getter = getattr(type(component), "get_stats", None)
            if not callable(getter):
                return {}
            try:
                result = getter(component)
            except Exception:
                log.debug("Operational metrics collection failed", exc_info=True)
                return {}
            return {
                str(key): int(value)
                for key, value in result.items()
                if isinstance(key, str) and isinstance(value, int)
            }

        event_bus_metrics = _stats(self.event_bus)
        announce_metrics = _stats(self.announce_dispatcher)

        sdr_metrics: dict[str, int] = {}
        sdr_getter = getattr(type(self.sdr_scheduler), "get_metrics", None)
        if callable(sdr_getter):
            try:
                raw_sdr_metrics = sdr_getter(self.sdr_scheduler)
            except Exception:
                log.debug("SDR operational metrics collection failed", exc_info=True)
            else:
                if isinstance(raw_sdr_metrics, dict):
                    for key in ("dongles", "active_leases", "active_slots", "suspended_slots"):
                        value = raw_sdr_metrics.get(key)
                        if isinstance(value, int) and not isinstance(value, bool):
                            sdr_metrics[key] = max(0, value)
        try:
            lease_metrics = get_lease_metrics()
        except Exception:
            log.debug("RTL-SDR claim metrics collection failed", exc_info=True)
            lease_metrics = {"canonical_claims": 0}
        canonical_claims = max(0, int(lease_metrics.get("canonical_claims", 0)))
        sdr_metrics["canonical_claims"] = canonical_claims
        sdr_metrics["active_leases"] = max(
            canonical_claims,
            sdr_metrics.get("active_leases", 0),
        )
        dashboard_plugin = next(
            (plugin for plugin in plugins if plugin.plugin_name == "web_dashboard"),
            None,
        )
        dashboard_metrics = get_dashboard_operational_metrics(dashboard_plugin)
        migration_metrics = get_migration_metrics()
        runtime_metrics = get_runtime_metrics()
        runtime_threads = threading.enumerate()
        plugin_threads = PluginBase.get_thread_count()
        callback_drops = {
            "event_bus": max(0, event_bus_metrics.get("dropped", 0)),
            "announce_queue": max(0, announce_metrics.get("queue_dropped", 0)),
            "announce_subscribers": max(
                0,
                announce_metrics.get("subscriber_dropped", 0),
            ),
        }
        dashboard_workers = dashboard_metrics.get("workers", {})
        broadcast_hung = (
            max(0, dashboard_workers.get("broadcast_hung_total", 0))
            if isinstance(dashboard_workers, dict)
            else 0
        )
        worker_failures = {
            "lifecycle_hung": hung_total,
            "detected_runtime_hung": max(
                0,
                runtime_metrics.get("hung_workers_total", 0),
            ),
            "event_bus_abandoned": max(
                0,
                event_bus_metrics.get("abandoned_workers", 0),
            ),
            "announce_abandoned": max(
                0,
                announce_metrics.get("abandoned_workers", 0),
            ),
            "dashboard_broadcast_hung": broadcast_hung,
        }
        return {
            "lifecycle": {
                "states": states,
                "health": health,
                "readiness": {
                    "count": int(retired_lifecycle["readiness_count"]) + len(readiness_durations),
                    "total_seconds": float(retired_lifecycle["readiness_total_seconds"])
                    + sum(readiness_durations),
                    "max_seconds": max(
                        float(retired_lifecycle["readiness_max_seconds"]),
                        max(readiness_durations, default=0.0),
                    ),
                },
                "max_state_age_seconds": max(state_ages, default=0.0),
                "hung_total": hung_total,
                "cleanup_failures_total": cleanup_failures_total,
            },
            "rns_resources": rns_resources,
            "threads": {
                # ``live`` is retained as the compatibility name for
                # PluginBase-managed workers.
                "live": plugin_threads,
                "runtime_live": len(runtime_threads),
                "runtime_daemon": sum(thread.daemon for thread in runtime_threads),
            },
            "event_bus": event_bus_metrics,
            "announce_dispatcher": announce_metrics,
            "callbacks": {
                **callback_drops,
                "dropped_total": sum(callback_drops.values()),
            },
            "workers": {
                **worker_failures,
                "hung_or_abandoned_total": sum(worker_failures.values()),
            },
            "processes": {
                "managed_groups": managed_groups,
                "managed_processes": managed_processes,
                "raw_processes": raw_processes,
                "total_live": managed_processes + raw_processes,
                "restarts": process_restarts,
                "restarts_total": max(
                    0,
                    runtime_metrics.get("process_restarts_total", 0),
                ),
            },
            "sdr": sdr_metrics,
            "migrations": migration_metrics,
            "sqlite": {
                "failures": max(0, runtime_metrics.get("sqlite_failures_total", 0)),
                "migration_failures": max(
                    0,
                    int(migration_metrics.get("sqlite_failures", 0)),
                ),
            },
            "dashboard": dashboard_metrics,
        }

    def enable_plugin(self, name: str) -> None:
        """Instantiate, start, and register a plugin by name at runtime.

        Raises KeyError if the plugin is not discoverable, RuntimeError if
        it is already running, or propagates any exception from instantiation
        or start().

        The lock is released before calling start() so a slow plugin
        start does not block status queries or other plugin operations.
        """
        with self._plugin_transition_lock:
            if self._shutting_down.is_set():
                raise RuntimeError(f"Cannot enable plugin '{name}' during shutdown")
            with self._plugins_lock:
                if name in self.plugins:
                    state = self.plugins[name].plugin_state.value
                    raise RuntimeError(f"Plugin '{name}' is already running ({state})")
            # Discovery imports and executes third-party module top-level code;
            # never hold the registry lock while doing so.
            available = self._plugin_loader.discover(self._get_plugin_search_dirs())
            if name not in available:
                raise KeyError(f"Plugin '{name}' not found in plugin directories")
            plugin_config = self.config.plugins.get(name, {})
            plugin_cls = available[name]
            with self._plugins_lock:
                if name in self.plugins:
                    state = self.plugins[name].plugin_state.value
                    raise RuntimeError(f"Plugin '{name}' is already running ({state})")
            instance = plugin_cls(self, plugin_config)

            metadata_problem = self._plugin_metadata_problem(name, instance)
            if metadata_problem is not None:
                instance.mark_blocked(metadata_problem)
                instance.cleanup_managed_resources()
                with self._plugins_lock:
                    self.plugins[name] = instance
                self._record_plugin_state(name, instance)
                self.event_bus.publish(
                    events.PLUGIN_BLOCKED,
                    {"name": name, "reason": metadata_problem},
                )
                raise RuntimeError(f"Cannot enable plugin '{name}': {metadata_problem}")

            problem = self._hard_dependency_problem(instance)
            if problem is not None:
                instance.mark_blocked(problem)
                self._record_plugin_state(name, instance)
                self.event_bus.publish(
                    events.PLUGIN_BLOCKED,
                    {"name": name, "reason": problem},
                )
                raise RuntimeError(f"Cannot enable plugin '{name}': {problem}")

            try:
                self._migrate_plugin(name, instance)
            except Exception as exc:
                reason = f"migration failed: {exc}"
                instance.mark_blocked(reason)
                instance.cleanup_managed_resources()
                with self._plugins_lock:
                    self.plugins[name] = instance
                self._record_plugin_state(name, instance)
                self.event_bus.publish(
                    events.PLUGIN_BLOCKED,
                    {"name": name, "reason": reason},
                )
                raise RuntimeError(f"Cannot enable plugin '{name}': {reason}") from exc
            try:
                self._start_plugin_with_timeout(name, instance, self.PLUGIN_START_TIMEOUT)
            except Exception as exc:
                reason = f"start() failed: {exc}"
                self.event_bus.publish(
                    events.PLUGIN_CRASHED,
                    {"name": name, "error": reason},
                )
                if instance.plugin_state == PluginState.HUNG:
                    with self._plugins_lock:
                        self.plugins[name] = instance
                else:
                    stopped = self._stop_plugin_with_timeout(
                        name,
                        instance,
                        min(5.0, self.PLUGIN_STOP_TIMEOUT),
                        publish_event=False,
                    )
                    if not stopped or instance.plugin_state == PluginState.HUNG:
                        # Keep the sentinel registered. Dropping it would let
                        # a second instance start while the first one's hung
                        # start/stop worker still owns resources.
                        with self._plugins_lock:
                            self.plugins[name] = instance
                    else:
                        self._archive_plugin_operational_metrics(instance)
                raise

            with self._plugins_lock:
                self.plugins[name] = instance
            self._record_plugin_state(name, instance)
            log.info("Hot-loaded plugin: %s", name)
            self.event_bus.publish(events.PLUGIN_STARTED, {"name": name})

    def disable_plugin(self, name: str) -> None:
        """Stop and unregister a running plugin by name.

        Raises KeyError if the plugin is not currently running.
        """
        with self._plugin_transition_lock:
            if self._shutting_down.is_set():
                raise RuntimeError(f"Cannot disable plugin '{name}' during shutdown")
            with self._plugins_lock:
                plugin = self.plugins.get(name)
                if plugin is None:
                    raise KeyError(f"Plugin '{name}' is not running")
                if plugin.plugin_state == PluginState.HUNG:
                    raise RuntimeError(f"Plugin '{name}' is hung and cannot be disabled safely")
                dep_names = [
                    other_name
                    for other_name, other_plugin in self.plugins.items()
                    if other_plugin.plugin_state == PluginState.READY
                    and name in self._dependency_names(other_plugin, "plugin_dependencies")
                ]
                if dep_names:
                    raise RuntimeError(
                        f"cannot disable {name}: dependents still running: {dep_names}"
                    )

            # Validation is complete and state is STOPPING before observers
            # are told to detach, so readiness lookups fail synchronously.
            plugin.request_stop()
            self.event_bus.publish(events.PLUGIN_STOPPING, {"name": name})
            if not self._stop_plugin_with_timeout(
                name,
                plugin,
                self.PLUGIN_STOP_TIMEOUT,
            ):
                raise RuntimeError(
                    f"Plugin '{name}' did not stop cleanly; it remains registered "
                    f"as {plugin.plugin_state.value}"
                )
            self._archive_plugin_operational_metrics(plugin)
            with self._plugins_lock:
                self.plugins.pop(name, None)
            log.info("Disabled plugin: %s", name)

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
            try:
                hard_dependencies = self._dependency_names(instance, "plugin_dependencies")
                soft_dependencies = self._dependency_names(instance, "plugin_soft_dependencies")
            except ValueError:
                continue
            for dep in hard_dependencies:
                if dep not in self.plugins:
                    log.warning(
                        "Plugin '%s' depends on '%s' which is not enabled",
                        name,
                        dep,
                    )
            for dep in soft_dependencies:
                if dep not in self.plugins:
                    log.info(
                        "Plugin '%s' soft-depends on '%s' which is not enabled"
                        " — degraded functionality possible",
                        name,
                        dep,
                    )

    def _topo_sort_plugins(self) -> list[str]:
        """Return a best-effort dependency order and record hard cycles.

        Hard dependency cycles are lifecycle failures local to the affected
        plugins.  They must not abort startup of unrelated plugins.  Soft
        dependencies influence ordering only, so a soft cycle is ignored.
        """
        names = tuple(self.plugins)
        name_set = set(names)
        hard_dependencies = {
            name: tuple(
                dep
                for dep in (
                    self._dependency_names(self.plugins[name], "plugin_dependencies")
                    if getattr(self.plugins[name], "plugin_state", PluginState.DISCOVERED)
                    != PluginState.BLOCKED
                    else ()
                )
                if dep in name_set
            )
            for name in names
        }

        # Tarjan's algorithm identifies the actual cycle participants.  A
        # simple Kahn residual would also include innocent hard dependents.
        index = 0
        indices: dict[str, int] = {}
        lowlinks: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        cycle_map: dict[str, tuple[str, ...]] = {}

        def strong_connect(name: str) -> None:
            nonlocal index
            indices[name] = index
            lowlinks[name] = index
            index += 1
            stack.append(name)
            on_stack.add(name)

            for dependency in hard_dependencies[name]:
                if dependency not in indices:
                    strong_connect(dependency)
                    lowlinks[name] = min(lowlinks[name], lowlinks[dependency])
                elif dependency in on_stack:
                    lowlinks[name] = min(lowlinks[name], indices[dependency])

            if lowlinks[name] != indices[name]:
                return
            component: list[str] = []
            while stack:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == name:
                    break
            members = tuple(sorted(component))
            if len(members) > 1 or name in hard_dependencies[name]:
                for member in members:
                    cycle_map[member] = members

        for name in names:
            if name not in indices:
                strong_connect(name)

        self._plugin_dependency_cycles = cycle_map
        for members in sorted(set(cycle_map.values())):
            log.error("hard dependency cycle detected: %s", list(members))

        order: list[str] = []
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name not in name_set or name in visited:
                return
            if name in visiting:
                # Only soft edges can still form a back-edge here; hard cycle
                # members were already identified and are skipped below.
                return
            visiting.add(name)
            plugin = self.plugins[name]
            hard = (
                self._dependency_names(plugin, "plugin_dependencies")
                if getattr(plugin, "plugin_state", PluginState.DISCOVERED) != PluginState.BLOCKED
                else ()
            )
            soft = (
                self._dependency_names(plugin, "plugin_soft_dependencies")
                if getattr(plugin, "plugin_state", PluginState.DISCOVERED) != PluginState.BLOCKED
                else ()
            )
            for dep in hard:
                if dep in name_set and dep not in cycle_map:
                    visit(dep)
            # Soft dependencies influence start order but don't block
            for dep in soft:
                if dep in name_set and dep not in cycle_map:
                    visit(dep)
            visiting.discard(name)
            visited.add(name)
            order.append(name)

        # Cycle members come first so startup can mark them BLOCKED before
        # evaluating their transitive hard dependents.
        for name in names:
            if name in cycle_map:
                visited.add(name)
                order.append(name)
        for name in names:
            visit(name)
        return order

    def _block_dependency_cycles(self) -> None:
        """Mark recorded hard-cycle participants BLOCKED and publish events."""

        for name, members in self._plugin_dependency_cycles.items():
            plugin = self.plugins.get(name)
            if plugin is None:
                continue
            reason = f"hard dependency cycle: {' -> '.join((*members, members[0]))}"
            plugin.mark_blocked(reason)
            self._record_plugin_state(name, plugin)
            self._failed_plugins.append((name, reason))
            self.event_bus.publish(
                events.PLUGIN_BLOCKED,
                {"name": name, "reason": reason},
            )

    def _print_startup_report(self) -> None:
        """Log a human-readable summary of the running system."""
        log.info("=== ReticulumPi v%s ===", self._get_version())
        log.info("Node name: %s", self.node_name)
        log.info("Config: %s", self.config.config_path or "(defaults, no config file)")
        log.info(
            "Reticulum config: %s",
            self._reticulum_config_dir or "(default $HOME/.reticulum)",
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
        print(f"  Reticulum config: {self._reticulum_config_dir or '(default $HOME/.reticulum)'}")
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
