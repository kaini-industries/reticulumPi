"""Abstract base class for all reticulumPi plugins."""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from enum import Enum
from functools import partial
from typing import TYPE_CHECKING, Any, Callable

from reticulumpi import events

if TYPE_CHECKING:
    from reticulumpi.app import ReticulumPiApp
    from reticulumpi.migrations import MigrationTarget


class PluginState(str, Enum):
    """Observable lifecycle states for API v1 and v2 plugins."""

    DISCOVERED = "discovered"
    BLOCKED = "blocked"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    HUNG = "hung"


class PluginHealth(str, Enum):
    """Health is separate from lifecycle readiness."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


def resolve_ready_plugin(owner: Any, name: str) -> "PluginBase | None":
    """Resolve a plugin dependency through lifecycle v2 when available.

    ``owner`` may be a real :class:`PluginBase` or a lightweight compatibility
    object used by an external plugin/test harness. Looking up methods on the
    class prevents dynamic mock attributes from being mistaken for lifecycle
    support.
    """

    owner_getter = getattr(type(owner), "get_ready_plugin", None)
    if callable(owner_getter):
        return owner_getter(owner, name)
    app = getattr(owner, "app", None)
    app_getter = getattr(type(app), "get_ready_plugin", None)
    if callable(app_getter):
        return app_getter(app, name)
    legacy_getter = getattr(app, "get_plugin", None)
    if callable(legacy_getter):
        return legacy_getter(name)
    return None


class PluginBase(ABC):
    """Base class all reticulumPi plugins must inherit from.

    Subclasses must set `plugin_name` and `plugin_version` as class attributes,
    and implement the `start()` and `stop()` methods.
    """

    plugin_name: str = "unnamed"
    plugin_version: str = "0.0.0"
    plugin_description: str = "No description"
    plugin_dependencies: tuple[str, ...] = ()
    plugin_soft_dependencies: tuple[str, ...] = ()
    # Existing plugins are lifecycle API v1: returning from start() means
    # ready.  API v2 plugins must call mark_ready() explicitly.
    plugin_lifecycle_api: int = 1
    # Plugins with a legitimately longer, bounded hardware initialization may
    # opt into a larger host-side start budget.  The application still clamps
    # this value to its global startup deadline.
    plugin_start_timeout_seconds: float | None = None

    broadcast_tier: int | None = None
    broadcast_keys: str | list[str] | None = None

    _global_thread_count: int = 0
    _global_thread_budget: int = 50
    _global_thread_lock: threading.Lock = threading.Lock()
    MANAGED_CLEANUP_CALLBACK_TIMEOUT: float = 0.5
    MANAGED_CLEANUP_TOTAL_TIMEOUT: float = 5.0

    def __init__(self, app: "ReticulumPiApp", plugin_config: dict[str, Any]):
        self.app = app
        self.config = plugin_config
        self.rns = app.reticulum
        self.identity = app.identity
        self.event_bus = app.event_bus
        self.announce_dispatcher = app.announce_dispatcher
        self.log = logging.getLogger(f"reticulumpi.plugin.{self.plugin_name}")
        self._stop_event = threading.Event()
        self._stop_event.set()  # starts "stopped"
        self._threads: list[threading.Thread] = []
        self._threads_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._ready_event = threading.Event()
        self._plugin_state = PluginState.DISCOVERED
        self._plugin_health = PluginHealth.UNAVAILABLE
        self._lifecycle_reason: str | None = None
        self._state_changed_monotonic = time.monotonic()
        self._start_requested_monotonic: float | None = None
        self._readiness_duration_seconds: float | None = None
        self._hung_total = 0
        self._cleanup_failures_total = 0
        self._managed_resource_counts = {
            "links": 0,
            "destinations": 0,
            "request_handlers": 0,
        }
        self._cleanup_lock = threading.Lock()
        self._managed_cleanups: list[Callable[[], None]] = []
        self._cleanup_complete = False
        self._stop_hook_lock = threading.Lock()
        self._stop_hook_started = False
        probe = getattr(app, "internet_probe", None)
        self._internet_available: bool = probe.is_online if probe is not None else True
        # Resource acquisition begins in mark_starting(), not construction.
        # A subclass may still raise after super().__init__ returns; keeping
        # constructors side-effect free prevents those failures from leaking
        # base EventBus registrations.
        self.validate_config()

    @property
    def _active(self) -> bool:
        return not self._stop_event.is_set()

    @_active.setter
    def _active(self, value: bool) -> None:
        if value:
            with self._lifecycle_lock:
                if self._plugin_state in {
                    PluginState.BLOCKED,
                    PluginState.FAILED,
                    PluginState.HUNG,
                    PluginState.STOPPING,
                    PluginState.STOPPED,
                }:
                    # A timed-out start may return later; it must not undo the
                    # cancellation request or resurrect the plugin.
                    return
                self._stop_event.clear()
        else:
            with self._lifecycle_lock:
                self._stop_event.set()

    @abstractmethod
    def start(self) -> None:
        """Called when the app starts. Create destinations, register handlers, start threads."""

    @abstractmethod
    def stop(self) -> None:
        """Called on shutdown. Clean up resources, deregister handlers."""

    @property
    def internet_available(self) -> bool:
        """Whether internet is currently available."""
        return self._internet_available

    def _on_internet_event(self, event_type: str, data: dict[str, Any]) -> None:
        was = self._internet_available
        now = event_type == events.INTERNET_ONLINE
        self._internet_available = now
        if not self._active:
            return
        if now and not was:
            try:
                self.on_internet_available()
            except Exception:
                self.log.exception("Error in on_internet_available")
        elif not now and was:
            try:
                self.on_internet_lost()
            except Exception:
                self.log.exception("Error in on_internet_lost")

    def on_internet_available(self) -> None:
        """Called when internet connectivity is restored. Override to react."""

    def on_internet_lost(self) -> None:
        """Called when internet connectivity is lost. Override to react."""

    def validate_config(self) -> None:
        """Validate plugin config at construction time. Override to add checks."""

    def get_migration_targets(self) -> tuple["MigrationTarget", ...]:
        """Declare SQLite targets before startup; API v1 defaults to none."""

        return ()

    def get_ready_plugin(self, name: str) -> "PluginBase | None":
        """Resolve a dependency only when the application reports it ready.

        The class-level lookup avoids treating dynamically-created attributes
        on test doubles or older third-party host applications as lifecycle-v2
        support. External plugin hosts that predate readiness keep their
        legacy behavior through the 0.4.x compatibility window.
        """

        ready_getter = getattr(type(self.app), "get_ready_plugin", None)
        if callable(ready_getter):
            return ready_getter(self.app, name)
        legacy_getter = getattr(self.app, "get_plugin", None)
        if callable(legacy_getter):
            return legacy_getter(name)
        return None

    def get_status(self) -> dict[str, Any]:
        """Return status info for monitoring. Override for richer status."""
        return {"active": self._active, "_lifecycle": self.get_lifecycle_status()}

    @property
    def plugin_state(self) -> PluginState:
        with self._lifecycle_lock:
            return self._plugin_state

    @property
    def plugin_health(self) -> PluginHealth:
        with self._lifecycle_lock:
            return self._plugin_health

    def get_lifecycle_status(self) -> dict[str, str | int | None]:
        """Return the stable lifecycle object added to status payloads."""

        with self._lifecycle_lock:
            return {
                "api": self.plugin_lifecycle_api,
                "state": self._plugin_state.value,
                "health": self._plugin_health.value,
                "reason": self._lifecycle_reason,
            }

    def get_lifecycle_metrics(self) -> dict[str, Any]:
        """Return bounded, secret-free lifecycle and resource measurements."""

        with self._lifecycle_lock:
            return {
                "state_age_seconds": max(0.0, time.monotonic() - self._state_changed_monotonic),
                "readiness_seconds": self._readiness_duration_seconds,
                "hung_total": self._hung_total,
                "cleanup_failures_total": self._cleanup_failures_total,
                "rns_resources": dict(self._managed_resource_counts),
            }

    def mark_starting(self) -> None:
        """Mark this plugin as starting and reset one-shot cleanup state."""

        with self._lifecycle_lock:
            if self._plugin_state == PluginState.HUNG:
                raise RuntimeError(f"Plugin '{self.plugin_name}' is hung and cannot be restarted")
            if self._plugin_state in {
                PluginState.STARTING,
                PluginState.READY,
                PluginState.STOPPING,
            }:
                raise RuntimeError(
                    f"Plugin '{self.plugin_name}' cannot start from {self._plugin_state.value}"
                )
            self._plugin_state = PluginState.STARTING
            self._plugin_health = PluginHealth.UNAVAILABLE
            self._lifecycle_reason = None
            now = time.monotonic()
            self._state_changed_monotonic = now
            self._start_requested_monotonic = now
            self._readiness_duration_seconds = None
            self._ready_event.clear()
            self._stop_event.set()
        with self._cleanup_lock:
            self._cleanup_complete = False
        with self._stop_hook_lock:
            self._stop_hook_started = False
        self.manage_subscription(
            self.event_bus.subscribe(events.INTERNET_ONLINE, self._on_internet_event)
        )
        self.manage_subscription(
            self.event_bus.subscribe(events.INTERNET_OFFLINE, self._on_internet_event)
        )

    def mark_ready(self) -> None:
        """Declare that all resources needed to serve work are usable."""

        with self._lifecycle_lock:
            if self._plugin_state == PluginState.READY:
                # READY -> READY is the recovery path from a transient
                # degraded condition; it must not alter cancellation state.
                self._plugin_health = PluginHealth.HEALTHY
                self._lifecycle_reason = None
                self._state_changed_monotonic = time.monotonic()
                return
            if self._plugin_state != PluginState.STARTING:
                return
            now = time.monotonic()
            self._plugin_state = PluginState.READY
            if self._plugin_health != PluginHealth.DEGRADED:
                self._plugin_health = PluginHealth.HEALTHY
                self._lifecycle_reason = None
            self._state_changed_monotonic = now
            if self._start_requested_monotonic is not None:
                self._readiness_duration_seconds = max(
                    0.0,
                    now - self._start_requested_monotonic,
                )
            self._stop_event.clear()
            self._ready_event.set()

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        """Wait for API v2 readiness without polling plugin-owned state."""

        self._ready_event.wait(timeout=timeout)
        return self.plugin_state == PluginState.READY

    def mark_start_failed(self, reason: str) -> None:
        with self._lifecycle_lock:
            if self._plugin_state not in {
                PluginState.STARTING,
                PluginState.FAILED,
                PluginState.STOPPING,
            }:
                return
            self._plugin_state = PluginState.FAILED
            self._plugin_health = PluginHealth.UNAVAILABLE
            self._lifecycle_reason = str(reason)
            self._state_changed_monotonic = time.monotonic()
            self._stop_event.set()
            self._ready_event.set()

    def mark_blocked(self, reason: str) -> None:
        with self._lifecycle_lock:
            if self._plugin_state not in {PluginState.DISCOVERED, PluginState.BLOCKED}:
                return
            self._plugin_state = PluginState.BLOCKED
            self._plugin_health = PluginHealth.UNAVAILABLE
            self._lifecycle_reason = str(reason)
            self._state_changed_monotonic = time.monotonic()
            self._stop_event.set()
            self._ready_event.set()

    def mark_degraded(self, reason: str) -> None:
        with self._lifecycle_lock:
            if self._plugin_state in {
                PluginState.BLOCKED,
                PluginState.FAILED,
                PluginState.HUNG,
                PluginState.STOPPING,
                PluginState.STOPPED,
            }:
                return
            self._plugin_health = PluginHealth.DEGRADED
            self._lifecycle_reason = str(reason)

    def mark_hung(self, reason: str) -> None:
        with self._lifecycle_lock:
            previous_state = self._plugin_state
            if self._plugin_state != PluginState.HUNG:
                self._hung_total += 1
            self._plugin_state = PluginState.HUNG
            self._plugin_health = PluginHealth.UNAVAILABLE
            self._lifecycle_reason = str(reason)
            self._state_changed_monotonic = time.monotonic()
            self._stop_event.set()
            self._ready_event.set()
        self._notify_ready_lost(previous_state)

    def request_stop(self) -> None:
        """Request cancellation and invoke the optional hook without blocking.

        The stop event and lifecycle state change synchronously. The optional
        third-party hook runs once per lifecycle on a daemon worker: a broken
        hook therefore cannot defeat the caller's lifecycle timeout or keep
        interpreter shutdown alive.
        """

        with self._lifecycle_lock:
            previous_state = self._plugin_state
            if self._plugin_state not in {PluginState.FAILED, PluginState.HUNG}:
                self._plugin_state = PluginState.STOPPING
                self._plugin_health = PluginHealth.UNAVAILABLE
                self._state_changed_monotonic = time.monotonic()
            self._stop_event.set()
            self._ready_event.set()
        self._notify_ready_lost(previous_state)
        with self._stop_hook_lock:
            if self._stop_hook_started:
                return
            self._stop_hook_started = True

        def _invoke_hook() -> None:
            try:
                self.on_stop_requested()
            except Exception:
                self.log.exception("Error in on_stop_requested")

        threading.Thread(
            target=_invoke_hook,
            name=f"plugin-stop-request-{self.plugin_name}",
            daemon=True,
        ).start()

    def on_stop_requested(self) -> None:
        """Optional API v2 hook for interrupting blocking start/work calls."""

    def mark_stopped(self) -> None:
        with self._lifecycle_lock:
            previous_state = self._plugin_state
            if self._plugin_state not in {PluginState.FAILED, PluginState.HUNG}:
                self._plugin_state = PluginState.STOPPED
                self._plugin_health = PluginHealth.UNAVAILABLE
                self._lifecycle_reason = None
                self._state_changed_monotonic = time.monotonic()
            self._stop_event.set()
            self._ready_event.set()
        self._notify_ready_lost(previous_state)

    def _notify_ready_lost(self, previous_state: PluginState) -> None:
        if previous_state != PluginState.READY:
            return
        callback = getattr(type(self.app), "_on_plugin_ready_lost", None)
        if callable(callback):
            try:
                callback(self.app, self)
            except Exception:
                self.log.exception("Could not propagate plugin readiness loss")

    def _mark_dependency_blocked(self, reason: str) -> bool:
        """Atomically remove readiness after a hard provider is lost."""

        with self._lifecycle_lock:
            if self._plugin_state in {PluginState.FAILED, PluginState.HUNG}:
                return False
            if self._plugin_state == PluginState.BLOCKED and self._lifecycle_reason == str(reason):
                return False
            self._plugin_state = PluginState.BLOCKED
            self._plugin_health = PluginHealth.UNAVAILABLE
            self._lifecycle_reason = str(reason)
            self._state_changed_monotonic = time.monotonic()
            self._stop_event.set()
            self._ready_event.set()
            return True

    def register_cleanup(
        self,
        callback: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Callable[..., Any]:
        """Register an idempotent, reverse-order cleanup callback."""

        cleanup = partial(callback, *args, **kwargs)
        with self._cleanup_lock:
            cleanup_complete = self._cleanup_complete
            if not cleanup_complete:
                self._managed_cleanups.append(cleanup)
        if cleanup_complete:
            # A timed-out start can resume after lifecycle cleanup has already
            # run. Undo every kind of late acquisition immediately—not only
            # RNS resources—so processes, executors, tasks, and subscriptions
            # cannot escape a HUNG/STOPPED plugin.
            done = threading.Event()
            failure: list[BaseException] = []

            def _run_late_cleanup() -> None:
                try:
                    cleanup()
                except BaseException as cleanup_error:
                    failure.append(cleanup_error)
                finally:
                    done.set()

            threading.Thread(
                target=_run_late_cleanup,
                name=f"plugin-late-cleanup-{self.plugin_name}",
                daemon=True,
            ).start()
            if not done.wait(self.MANAGED_CLEANUP_CALLBACK_TIMEOUT):
                with self._lifecycle_lock:
                    self._cleanup_failures_total += 1
                self.log.warning(
                    "Late managed resource cleanup exceeded %.2fs and was abandoned",
                    self.MANAGED_CLEANUP_CALLBACK_TIMEOUT,
                )
            elif failure:
                with self._lifecycle_lock:
                    self._cleanup_failures_total += 1
                cleanup_error = failure[0]
                self.log.error(
                    "Late managed resource cleanup failed",
                    exc_info=(type(cleanup_error), cleanup_error, cleanup_error.__traceback__),
                )
            raise RuntimeError("Cannot register a resource after cleanup completed")
        return callback

    def manage_subscription(self, subscription: Any) -> Any:
        cancel = getattr(subscription, "cancel", None)
        if callable(cancel):
            self.register_cleanup(cancel)
        return subscription

    def manage_link(self, link: Any) -> Any:
        close = getattr(link, "teardown", None) or getattr(link, "close", None)
        if callable(close):
            self._register_resource_cleanup("links", close)
        return link

    def manage_destination(self, destination: Any) -> Any:
        """Register best-effort RNS destination deregistration."""

        def _deregister() -> None:
            deregister = getattr(destination, "deregister", None)
            if callable(deregister):
                deregister()
                return
            transport = getattr(__import__("RNS"), "Transport", None)
            fn = getattr(transport, "deregister_destination", None)
            if callable(fn):
                fn(destination)

        self._register_resource_cleanup("destinations", _deregister)
        return destination

    def manage_process(self, process: Any, timeout: float = 5.0) -> Any:
        """Terminate, then kill, a subprocess during managed cleanup."""

        def _stop_process() -> None:
            if process.poll() is not None:
                return
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except Exception:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=timeout)

        self.register_cleanup(_stop_process)
        return process

    def manage_process_group(self, process_group: Any) -> Any:
        """Own a ManagedProcessGroup-compatible object for lifecycle cleanup."""

        stop = getattr(process_group, "stop", None)
        if not callable(stop):
            raise TypeError("managed process group must provide stop()")
        self.register_cleanup(stop)
        return process_group

    def manage_executor(self, executor: Any) -> Any:
        """Cancel queued executor work without blocking lifecycle cleanup.

        Executor workers are never joined from this callback.  That keeps
        cleanup safe when a plugin is stopped from an asyncio event-loop
        thread while still preventing queued work from starting.
        """

        shutdown = getattr(executor, "shutdown", None)
        if not callable(shutdown):
            raise TypeError("managed executor must provide shutdown()")

        def _shutdown_executor() -> None:
            try:
                shutdown(wait=False, cancel_futures=True)
            except TypeError:
                # Compatibility with executor-like implementations that do
                # not yet expose the Python 3.9 cancel_futures argument.
                shutdown(wait=False)

        self.register_cleanup(_shutdown_executor)
        return executor

    def manage_async_task(self, task: Any) -> Any:
        """Cancel an asyncio task on its owning loop without waiting for it."""

        cancel = getattr(task, "cancel", None)
        done = getattr(task, "done", None)
        if not callable(cancel) or not callable(done):
            raise TypeError("managed async task must provide cancel() and done()")

        def _cancel_task() -> None:
            if done():
                return
            get_loop = getattr(task, "get_loop", None)
            loop = get_loop() if callable(get_loop) else None
            if loop is None:
                cancel()
                return

            import asyncio

            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if loop.is_running() and running_loop is not loop:
                loop.call_soon_threadsafe(cancel)
            elif not loop.is_closed():
                cancel()

        self.register_cleanup(_cancel_task)
        return task

    def manage_request_handler(self, destination: Any, path: str) -> str:
        """Deregister an RNS request handler during reverse-order cleanup."""

        deregister = getattr(destination, "deregister_request_handler", None)
        if not callable(deregister):
            raise TypeError(
                "managed request-handler destination must provide deregister_request_handler()"
            )
        self._register_resource_cleanup("request_handlers", partial(deregister, path))
        return path

    def _register_resource_cleanup(self, kind: str, cleanup: Callable[[], Any]) -> None:
        """Track one managed RNS resource until its cleanup has run."""

        with self._lifecycle_lock:
            self._managed_resource_counts[kind] += 1

        def _cleanup_and_account() -> None:
            try:
                cleanup()
            finally:
                with self._lifecycle_lock:
                    self._managed_resource_counts[kind] = max(
                        0,
                        self._managed_resource_counts[kind] - 1,
                    )

        self.register_cleanup(_cleanup_and_account)

    def cleanup_managed_resources(self, timeout: float | None = None) -> None:
        """Run registered cleanup once in reverse order with hard deadlines.

        Each callback runs on a daemon worker. A defective third-party
        callback can be abandoned after its short budget while earlier-owned
        handlers, destinations, links, and subscriptions still receive their
        own cleanup opportunity.
        """

        with self._cleanup_lock:
            if self._cleanup_complete:
                return
            self._cleanup_complete = True
            callbacks = list(reversed(self._managed_cleanups))
            self._managed_cleanups.clear()
        total_timeout = self.MANAGED_CLEANUP_TOTAL_TIMEOUT
        if timeout is not None:
            total_timeout = min(total_timeout, max(0.0, float(timeout)))
        deadline = time.monotonic() + total_timeout
        abandoned = 0
        for index, cleanup in enumerate(callbacks):
            done = threading.Event()
            failure: list[BaseException] = []

            def _run_cleanup(callback: Callable[[], None] = cleanup) -> None:
                try:
                    callback()
                except BaseException as exc:
                    failure.append(exc)
                finally:
                    done.set()

            threading.Thread(
                target=_run_cleanup,
                name=f"plugin-cleanup-{self.plugin_name}-{index}",
                daemon=True,
            ).start()
            remaining = max(0.0, deadline - time.monotonic())
            wait_for = min(self.MANAGED_CLEANUP_CALLBACK_TIMEOUT, remaining)
            if not done.wait(wait_for):
                with self._lifecycle_lock:
                    self._cleanup_failures_total += 1
                abandoned += 1
                if abandoned <= 3:
                    self.log.warning(
                        "Managed resource cleanup exceeded %.2fs and was abandoned",
                        wait_for,
                    )
                continue
            if failure:
                with self._lifecycle_lock:
                    self._cleanup_failures_total += 1
                error = failure[0]
                self.log.error(
                    "Managed resource cleanup failed",
                    exc_info=(type(error), error, error.__traceback__),
                )
        if abandoned > 3:
            self.log.warning("Abandoned %d additional managed cleanup callbacks", abandoned - 3)

    def broadcast_snapshot(self, cycle_count: int = 0) -> dict[str, Any] | None:
        """Return data for the WebSocket broadcast payload.

        Override to provide broadcast data. The default calls get_snapshot()
        if it exists. Plugins that set broadcast_tier and broadcast_keys
        participate in the broadcast automatically.
        """
        if hasattr(self, "get_snapshot"):
            return self.get_snapshot()
        return None

    def _join_threads(self, timeout: float = 5.0) -> None:
        """Wait for all tracked threads to finish within a shared deadline.

        The *timeout* is a total wall-clock budget shared across all threads,
        not a per-thread allowance.  This prevents a plugin with many threads
        from consuming N * timeout seconds.
        """
        deadline = time.monotonic() + timeout
        with self._threads_lock:
            threads = list(self._threads)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with self._threads_lock:
                    alive = sum(1 for t in self._threads if t.is_alive())
                self.log.warning(
                    "Thread join deadline reached — %d thread(s) still alive",
                    alive,
                )
                break
            thread.join(timeout=remaining)
            if thread.is_alive():
                self.log.warning("Thread '%s' did not exit in time", thread.name)
            else:
                self._remove_thread(thread)
        self.event_bus.unsubscribe_all(self._on_internet_event)

    def _sleep_while_active(self, seconds: float) -> None:
        """Sleep for up to `seconds`, exiting early if the plugin is stopped."""
        # Event-based: wakes instantly when _active flips to False, vs. the prior
        # 1-second busy-poll that delayed shutdown by up to ~1s per sleeping thread.
        self._stop_event.wait(timeout=float(seconds))

    def _jittered_sleep(self, seconds: float, jitter_pct: float = 0.1) -> None:
        """Sleep with random jitter to desynchronize periodic network tasks.

        Applies +/- *jitter_pct* (default 10 %) uniform random offset.
        """
        import random

        offset = seconds * jitter_pct * (2 * random.random() - 1)
        self._stop_event.wait(timeout=max(0.0, float(seconds + offset)))

    def _start_log_reader(self, process: Any, prefix: str = "") -> threading.Thread:
        """Start a daemon thread that reads process stdout line-by-line and logs it.

        The process must have been created with ``stdout=subprocess.PIPE`` and
        ``stderr=subprocess.STDOUT`` so all output appears on stdout.
        """
        import io

        def _reader() -> None:
            stream: io.BufferedReader | None = getattr(process, "stdout", None)
            if stream is None:
                return
            tag = f"[{prefix}] " if prefix else ""
            try:
                for raw_line in stream:
                    line = raw_line.decode("utf-8", errors="replace").rstrip()
                    if line:
                        self.log.info("%s%s", tag, line)
            except (ValueError, OSError):
                # Stream closed
                pass

        return self._start_thread(_reader, name=f"{prefix}-log-reader" if prefix else "log-reader")

    def _start_stderr_reader(self, process: Any, prefix: str = "") -> threading.Thread:
        """Start a log reader that drains the process's stderr pipe."""

        class _StderrProxy:
            stdout = process.stderr

        return self._start_log_reader(_StderrProxy(), prefix=prefix)

    @classmethod
    def get_thread_count(cls) -> int:
        """Return the current global plugin thread count."""
        with cls._global_thread_lock:
            return cls._global_thread_count

    @classmethod
    def set_thread_budget(cls, budget: int) -> None:
        """Set the global soft thread budget. Logged when exceeded."""
        with cls._global_thread_lock:
            cls._global_thread_budget = budget

    def _remove_thread(self, thread: threading.Thread) -> None:
        """Remove a finished thread from tracking and decrement the global count.

        Call after joining a thread locally (e.g. in a supervisor restart cycle)
        to prevent stale entries from accumulating in self._threads.
        """
        with self._threads_lock:
            try:
                self._threads.remove(thread)
            except ValueError:
                return
        with PluginBase._global_thread_lock:
            PluginBase._global_thread_count = max(
                0,
                PluginBase._global_thread_count - 1,
            )

    def _start_thread(self, target: Any, name: str | None = None) -> threading.Thread:
        """Start and account for a daemon thread without a fast-exit race."""

        def _run() -> None:
            try:
                target()
            finally:
                self._remove_thread(threading.current_thread())

        thread = threading.Thread(
            target=_run,
            daemon=True,
            name=name or self.plugin_name,
        )
        with self._threads_lock:
            self._threads.append(thread)
        with PluginBase._global_thread_lock:
            PluginBase._global_thread_count += 1
            count = PluginBase._global_thread_count
            budget = PluginBase._global_thread_budget
        try:
            thread.start()
        except BaseException:
            self._remove_thread(thread)
            raise
        if count > budget:
            self.log.warning(
                "Plugin thread budget exceeded: %d/%d active (started '%s')",
                count,
                budget,
                thread.name,
            )
        return thread
