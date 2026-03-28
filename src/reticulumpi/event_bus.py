"""Thread-safe publish/subscribe event bus for inter-plugin communication."""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

log = logging.getLogger(__name__)

EventCallback = Callable[[str, dict[str, Any]], None]


class EventBus:
    """Simple in-process event bus.

    Plugins subscribe to event types and receive callbacks when events are
    published.  All callbacks run synchronously in the publisher's thread
    so subscribers should avoid blocking work.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # event_type -> list of callbacks
        self._subscribers: dict[str, list[EventCallback]] = {}

    def subscribe(self, event_type: str, callback: EventCallback) -> None:
        """Register *callback* to be called whenever *event_type* is published."""
        with self._lock:
            self._subscribers.setdefault(event_type, []).append(callback)

    def unsubscribe(self, event_type: str, callback: EventCallback) -> None:
        """Remove a previously registered callback."""
        with self._lock:
            listeners = self._subscribers.get(event_type)
            if listeners:
                try:
                    listeners.remove(callback)
                except ValueError:
                    pass

    def publish(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Dispatch *event_type* to all registered subscribers.

        Each callback receives ``(event_type, data)``.  If a callback raises
        an exception it is logged and remaining subscribers still execute.
        """
        with self._lock:
            listeners = list(self._subscribers.get(event_type, []))
        if not listeners:
            return

        payload = data or {}
        for cb in listeners:
            try:
                cb(event_type, payload)
            except Exception:
                log.exception(
                    "Event subscriber %s raised an exception for event '%s'",
                    getattr(cb, "__qualname__", cb),
                    event_type,
                )
