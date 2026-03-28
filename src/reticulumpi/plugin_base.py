"""Abstract base class for all reticulumPi plugins."""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

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

    def __init__(self, app: "ReticulumPiApp", plugin_config: dict[str, Any]):
        self.app = app
        self.config = plugin_config
        self.rns = app.reticulum
        self.identity = app.identity
        self.event_bus = app.event_bus
        self.log = logging.getLogger(f"reticulumpi.plugin.{self.plugin_name}")
        self._active = False
        self._threads: list[threading.Thread] = []
        self.validate_config()

    @abstractmethod
    def start(self) -> None:
        """Called when the app starts. Create destinations, register handlers, start threads."""

    @abstractmethod
    def stop(self) -> None:
        """Called on shutdown. Clean up resources, deregister handlers."""

    def validate_config(self) -> None:
        """Validate plugin config at construction time. Override to add checks."""

    def get_status(self) -> dict[str, Any]:
        """Return status info for monitoring. Override for richer status."""
        return {"active": self._active}

    def _join_threads(self, timeout: float = 5.0) -> None:
        """Wait for all tracked threads to finish."""
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()

    def _sleep_while_active(self, seconds: float) -> None:
        """Sleep for up to `seconds`, exiting early if the plugin is stopped."""
        deadline = time.monotonic() + float(seconds)
        while self._active:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 1.0))

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

    def _start_thread(self, target: Any, name: str | None = None) -> threading.Thread:
        """Start a daemon thread and return it."""
        thread = threading.Thread(
            target=target,
            daemon=True,
            name=name or self.plugin_name,
        )
        thread.start()
        self._threads.append(thread)
        return thread
