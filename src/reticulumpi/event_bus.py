"""Thread-safe publish/subscribe event bus for inter-plugin communication."""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

EventCallback = Callable[[str, dict[str, Any]], None]

_OFFLOAD_MAX_PENDING = 64
_OFFLOAD_WORKERS = 8


class _DaemonWorkerPool:
    """Small bounded pool whose blocked callbacks cannot hold process exit."""

    _STOP = object()

    def __init__(self, max_workers: int, max_pending: int) -> None:
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max_pending)
        self._max_workers = max_workers
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._active = True
        self._dropped = 0
        self._abandoned = 0

    def _ensure_started(self) -> None:
        with self._lock:
            if self._threads or not self._active:
                return
            for index in range(self._max_workers):
                thread = threading.Thread(
                    target=self._worker,
                    name=f"eventbus-{index}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()

    def submit(self, callback: Callable[..., Any], *args: Any) -> bool:
        self._ensure_started()
        with self._lock:
            if not self._active:
                return False
        try:
            self._queue.put_nowait((callback, args))
            return True
        except queue.Full:
            with self._lock:
                self._dropped += 1
            return False

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                callback, args = item
                callback(*args)
            finally:
                self._queue.task_done()

    def shutdown(self, *, cancel_pending: bool = True) -> None:
        with self._lock:
            if not self._active:
                return
            self._active = False
            threads = list(self._threads)
        if cancel_pending:
            while True:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    break
        for _thread in threads:
            try:
                self._queue.put_nowait(self._STOP)
            except queue.Full:
                break
        # Healthy workers exit promptly; a permanently blocked callback is a
        # daemon and cannot hold interpreter or application shutdown.
        for thread in threads:
            thread.join(timeout=0.1)
        with self._lock:
            self._abandoned += sum(thread.is_alive() for thread in threads)

    def stats(self) -> dict[str, int]:
        with self._lock:
            dropped = self._dropped
            workers = len(self._threads)
        return {
            "pending": self._queue.qsize(),
            "dropped": dropped,
            "workers": workers,
            "abandoned_workers": self._abandoned,
        }


def _safe_offload(callback: EventCallback, evt: str, data: dict[str, Any]) -> None:
    try:
        callback(evt, data)
    except Exception:
        log.exception(
            "Offloaded subscriber %s raised for '%s'",
            getattr(callback, "__qualname__", callback),
            evt,
        )


@dataclass(frozen=True)
class Subscription:
    """Cancellable subscription handle returned by EventBus registrations."""

    _bus: "EventBus"
    event_type: str
    callback: EventCallback
    id: str

    def cancel(self) -> bool:
        return self._bus._unsubscribe_handle(self)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.cancel()


class EventBus:
    """In-process event bus with synchronous and bounded offloaded delivery."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Keep callback lists for compatibility with existing plugin diagnostics.
        self._subscribers: dict[str, list[EventCallback]] = {}
        self._handles: dict[str, tuple[str, EventCallback, EventCallback]] = {}
        self._offload_map: dict[EventCallback, EventCallback] = {}
        self._offload_pool = _DaemonWorkerPool(
            max_workers=_OFFLOAD_WORKERS,
            max_pending=_OFFLOAD_MAX_PENDING,
        )
        self._is_shutdown = False
        self._last_drop_warning = 0.0
        self._drop_warning_lock = threading.Lock()

    def subscribe(self, event_type: str, callback: EventCallback) -> Subscription:
        """Register *callback* and return a cancellable handle."""

        handle = Subscription(self, event_type, callback, uuid.uuid4().hex)
        with self._lock:
            if self._is_shutdown:
                raise RuntimeError("EventBus is shut down")
            self._subscribers.setdefault(event_type, []).append(callback)
            self._handles[handle.id] = (event_type, callback, callback)
        return handle

    def subscribe_offloaded(
        self,
        event_type: str,
        callback: EventCallback,
    ) -> Subscription:
        """Register a callback on the bounded daemon worker pool."""

        with self._lock:
            if self._is_shutdown:
                raise RuntimeError("EventBus is shut down")
            wrapper = self._offload_map.get(callback)
            if wrapper is None:
                pool = self._offload_pool

                def _wrapper(evt: str, data: dict[str, Any]) -> None:
                    if not pool.submit(_safe_offload, callback, evt, data):
                        now = time.monotonic()
                        with self._drop_warning_lock:
                            should_warn = now - self._last_drop_warning >= 10.0
                            if should_warn:
                                self._last_drop_warning = now
                        if should_warn:
                            log.warning(
                                "Event bus backpressure: dropping '%s' for %s; total_dropped=%d",
                                evt,
                                getattr(callback, "__qualname__", callback),
                                pool.stats()["dropped"],
                            )

                self._offload_map[callback] = _wrapper
                wrapper = _wrapper
            handle = Subscription(self, event_type, callback, uuid.uuid4().hex)
            self._subscribers.setdefault(event_type, []).append(wrapper)
            self._handles[handle.id] = (event_type, callback, wrapper)
        return handle

    def _remove_wrapper_if_unused(self, original: EventCallback, actual: EventCallback) -> None:
        if original is actual:
            return
        if not any(actual in listeners for listeners in self._subscribers.values()):
            self._offload_map.pop(original, None)

    def _unsubscribe_handle(self, handle: Subscription) -> bool:
        with self._lock:
            registration = self._handles.pop(handle.id, None)
            if registration is None:
                return False
            event_type, original, actual = registration
            listeners = self._subscribers.get(event_type, [])
            try:
                listeners.remove(actual)
            except ValueError:
                return False
            self._remove_wrapper_if_unused(original, actual)
            return True

    def unsubscribe(self, event_type: str, callback: EventCallback) -> bool:
        """Remove one matching registration, preserving other event bindings."""

        with self._lock:
            actual = self._offload_map.get(callback, callback)
            listeners = self._subscribers.get(event_type)
            if not listeners:
                return False
            try:
                listeners.remove(actual)
            except ValueError:
                return False
            for handle_id, registration in list(self._handles.items()):
                if registration == (event_type, callback, actual):
                    self._handles.pop(handle_id, None)
                    break
            self._remove_wrapper_if_unused(callback, actual)
            return True

    def publish(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Dispatch an event; subscriber exceptions do not stop delivery."""

        if self._is_shutdown:
            return
        with self._lock:
            listeners = list(self._subscribers.get(event_type, []))
        payload = data or {}
        for callback in listeners:
            try:
                callback(event_type, payload)
            except Exception:
                log.exception(
                    "Event subscriber %s raised an exception for event '%s'",
                    getattr(callback, "__qualname__", callback),
                    event_type,
                )

    def shutdown(self) -> None:
        """Stop accepting events without waiting indefinitely on callbacks."""

        with self._lock:
            if self._is_shutdown:
                return
            self._is_shutdown = True
            self._subscribers.clear()
            self._handles.clear()
            self._offload_map.clear()
        self._offload_pool.shutdown(cancel_pending=True)

    def unsubscribe_all(self, callback: EventCallback) -> int:
        """Remove *callback* from every event type."""

        removed = 0
        with self._lock:
            actual = self._offload_map.get(callback, callback)
            for listeners in self._subscribers.values():
                while actual in listeners:
                    listeners.remove(actual)
                    removed += 1
            for handle_id, (_event, original, registered) in list(self._handles.items()):
                # Accessing an instance method creates a fresh bound-method
                # object each time.  Listener removal already uses equality;
                # handle cleanup must use the same semantics or lifecycle
                # teardown leaves stale registrations behind.
                if original == callback or registered == callback:
                    self._handles.pop(handle_id, None)
            self._offload_map.pop(callback, None)
        return removed

    def get_stats(self) -> dict[str, int]:
        """Return secret-free offload pressure counters."""

        return self._offload_pool.stats()
