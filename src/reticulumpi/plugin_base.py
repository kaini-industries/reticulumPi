"""Abstract base class for all reticulumPi plugins."""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from reticulumpi import events

if TYPE_CHECKING:
    from reticulumpi.app import ReticulumPiApp


class PluginBase(ABC):
    """Base class all reticulumPi plugins must inherit from.

    Subclasses must set `plugin_name` and `plugin_version` as class attributes,
    and implement the `start()` and `stop()` methods.
    """

    plugin_name: str = "unnamed"
    plugin_version: str = "0.0.0"
    plugin_description: str = "No description"
    plugin_dependencies: tuple[str, ...] = ()

    broadcast_tier: int | None = None
    broadcast_keys: str | list[str] | None = None

    _global_thread_count: int = 0
    _global_thread_budget: int = 50
    _global_thread_lock: threading.Lock = threading.Lock()

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
        self._internet_available: bool = True
        self.event_bus.subscribe(events.INTERNET_ONLINE, self._on_internet_event)
        self.event_bus.subscribe(events.INTERNET_OFFLINE, self._on_internet_event)
        self.validate_config()

    @property
    def _active(self) -> bool:
        return not self._stop_event.is_set()

    @_active.setter
    def _active(self, value: bool) -> None:
        if value:
            self._stop_event.clear()
        else:
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

    def get_status(self) -> dict[str, Any]:
        """Return status info for monitoring. Override for richer status."""
        return {"active": self._active}

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
        thread_count = len(self._threads)
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.log.warning(
                    "Thread join deadline reached — %d thread(s) still alive",
                    sum(1 for t in self._threads if t.is_alive()),
                )
                break
            thread.join(timeout=remaining)
            if thread.is_alive():
                self.log.warning("Thread '%s' did not exit in time", thread.name)
        self._threads.clear()
        self.event_bus.unsubscribe_all(self._on_internet_event)
        with PluginBase._global_thread_lock:
            PluginBase._global_thread_count = max(
                0, PluginBase._global_thread_count - thread_count,
            )

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
        try:
            self._threads.remove(thread)
        except ValueError:
            return
        with PluginBase._global_thread_lock:
            PluginBase._global_thread_count = max(
                0, PluginBase._global_thread_count - 1,
            )

    def _start_thread(self, target: Any, name: str | None = None) -> threading.Thread:
        """Start a daemon thread and return it."""
        thread = threading.Thread(
            target=target,
            daemon=True,
            name=name or self.plugin_name,
        )
        thread.start()
        self._threads.append(thread)
        with PluginBase._global_thread_lock:
            PluginBase._global_thread_count += 1
            count = PluginBase._global_thread_count
            budget = PluginBase._global_thread_budget
        if count > budget:
            self.log.warning(
                "Plugin thread budget exceeded: %d/%d active (started '%s')",
                count, budget, thread.name,
            )
        return thread
