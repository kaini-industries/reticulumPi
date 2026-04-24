"""Thread-safe publish/subscribe event bus for inter-plugin communication."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

log = logging.getLogger(__name__)

EventCallback = Callable[[str, dict[str, Any]], None]

_offload_executor = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="eventbus",
)


def _safe_offload(callback: EventCallback, evt: str, data: dict[str, Any]) -> None:
    try:
        callback(evt, data)
    except Exception:
        log.exception(
            "Offloaded subscriber %s raised for '%s'",
            getattr(callback, "__qualname__", callback),
            evt,
        )


class EventBus:
    """Simple in-process event bus.

    Plugins subscribe to event types and receive callbacks when events are
    published.  All callbacks run synchronously in the publisher's thread
    so subscribers should avoid blocking work.  Use ``subscribe_offloaded``
    for callbacks that do I/O.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # event_type -> list of callbacks
        self._subscribers: dict[str, list[EventCallback]] = {}
        self._offload_map: dict[EventCallback, EventCallback] = {}

    def subscribe(self, event_type: str, callback: EventCallback) -> None:
        """Register *callback* to be called whenever *event_type* is published."""
        with self._lock:
            self._subscribers.setdefault(event_type, []).append(callback)

    def subscribe_offloaded(self, event_type: str, callback: EventCallback) -> None:
        """Like subscribe(), but callback runs in a background thread.

        Use for callbacks that do I/O (database, network) to avoid
        blocking the publisher's thread.
        """
        with self._lock:
            wrapper = self._offload_map.get(callback)
            if wrapper is None:
                def _wrapper(evt: str, data: dict[str, Any]) -> None:
                    _offload_executor.submit(_safe_offload, callback, evt, data)
                self._offload_map[callback] = _wrapper
                wrapper = _wrapper
        self.subscribe(event_type, wrapper)

    def unsubscribe(self, event_type: str, callback: EventCallback) -> bool:
        """Remove a previously registered callback.  Returns True if removed."""
        with self._lock:
            actual = self._offload_map.get(callback, callback)
            listeners = self._subscribers.get(event_type)
            if listeners:
                try:
                    listeners.remove(actual)
                    return True
                except ValueError:
                    pass
        return False

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

    def unsubscribe_all(self, callback: EventCallback) -> int:
        """Remove *callback* from every event type it is subscribed to.

        Useful during plugin shutdown to prevent stale callbacks from
        accumulating across hot-reload cycles.  Returns the number of
        subscriptions removed.
        """
        removed = 0
        with self._lock:
            actual = self._offload_map.pop(callback, callback)
            for listeners in self._subscribers.values():
                while actual in listeners:
                    listeners.remove(actual)
                    removed += 1
        return removed
