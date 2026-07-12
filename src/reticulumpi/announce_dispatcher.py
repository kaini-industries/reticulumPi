"""Centralized announce dispatcher — one RNS handler replaces many.

RNS spawns a new daemon thread for every announce callback per registered
handler.  With many plugins each registering their own handler, tens of
thousands of short-lived threads accumulate over hours, fragmenting
CPython's memory allocator beyond recovery.

This module registers a single wildcard handler with RNS.  Incoming
announces are queued and dispatched to plugin subscribers from one
worker thread, eliminating the per-callback thread overhead.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from typing import Any, Callable

import RNS

log = logging.getLogger(__name__)

AnnounceCallback = Callable[[bytes, Any, "bytes | None"], None]

_DEFAULT_CALLBACK_DEADLINE_MS = 500
_DEFAULT_BREAKER_THRESHOLD = 3
_DEFAULT_BREAKER_COOLDOWN = 60.0
_DEFAULT_SUBSCRIBER_QUEUE = 64


class _Subscription:
    __slots__ = (
        "id",
        "aspect_filter",
        "callback",
        "consecutive_timeouts",
        "disabled_until",
        "queue",
        "active",
        "worker",
        "dropped",
        "last_warning",
    )

    def __init__(self, sub_id: str, aspect_filter: str | None, callback: AnnounceCallback):
        self.id = sub_id
        self.aspect_filter = aspect_filter
        self.callback = callback
        self.consecutive_timeouts = 0
        self.disabled_until = 0.0
        self.queue: queue.Queue[Any] = queue.Queue(maxsize=_DEFAULT_SUBSCRIBER_QUEUE)
        self.active = True
        self.worker: threading.Thread | None = None
        self.dropped = 0
        self.last_warning = 0.0


class AnnounceDispatcher:
    """Single-handler announce multiplexer.

    Plugins call ``subscribe(aspect_filter, callback)`` instead of
    ``RNS.Transport.register_announce_handler(handler)``.  The dispatcher
    handles RNS registration, aspect matching, and thread-safe dispatch.
    """

    def __init__(
        self,
        max_queue: int = 10_000,
        callback_deadline_ms: float = _DEFAULT_CALLBACK_DEADLINE_MS,
        breaker_threshold: int = _DEFAULT_BREAKER_THRESHOLD,
        breaker_cooldown: float = _DEFAULT_BREAKER_COOLDOWN,
    ) -> None:
        self._subscriptions: dict[str, _Subscription] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._active = False
        self._worker: threading.Thread | None = None
        self._callback_deadline_s = callback_deadline_ms / 1000.0
        self._breaker_threshold = breaker_threshold
        self._breaker_cooldown = breaker_cooldown
        self._rns_handler: _RNSHandler | None = None
        self._queue_dropped = 0
        self._last_queue_warning = 0.0
        self._abandoned_workers = 0
        self._subscriber_dropped_total = 0

    def start(self) -> None:
        self._active = True
        self._rns_handler = _RNSHandler(self)
        RNS.Transport.register_announce_handler(self._rns_handler)
        self._worker = threading.Thread(
            target=self._dispatch_loop,
            name="announce-dispatcher",
            daemon=True,
        )
        self._worker.start()
        log.info("Announce dispatcher started")

    def stop(self) -> None:
        self._active = False
        if self._rns_handler is not None:
            try:
                RNS.Transport.deregister_announce_handler(self._rns_handler)
            except Exception:
                log.debug("Failed to deregister announce handler", exc_info=True)
            self._rns_handler = None
        if self._worker is not None:
            self._worker.join(timeout=5.0)
            self._worker = None
        with self._lock:
            subscriptions = list(self._subscriptions.values())
            self._subscriptions.clear()
        for subscription in subscriptions:
            self._stop_subscription(subscription)
        log.info("Announce dispatcher stopped")

    def subscribe(
        self,
        aspect_filter: str | None,
        callback: AnnounceCallback,
    ) -> str:
        sub_id = uuid.uuid4().hex[:12]
        sub = _Subscription(sub_id, aspect_filter, callback)
        sub.worker = threading.Thread(
            target=self._subscriber_loop,
            args=(sub,),
            name=f"announce-subscriber-{sub_id}",
            daemon=True,
        )
        with self._lock:
            self._subscriptions[sub_id] = sub
        sub.worker.start()
        return sub_id

    def unsubscribe(self, sub_id: str) -> None:
        with self._lock:
            subscription = self._subscriptions.pop(sub_id, None)
        if subscription is not None:
            self._stop_subscription(subscription)

    def _stop_subscription(self, subscription: _Subscription) -> None:
        subscription.active = False
        try:
            subscription.queue.put_nowait(None)
        except queue.Full:
            pass
        worker = subscription.worker
        if worker is not None and worker is not threading.current_thread():
            # Permanently blocked callbacks are daemon-isolated and must not
            # hold application shutdown.
            worker.join(timeout=0.1)
            if worker.is_alive():
                with self._lock:
                    self._abandoned_workers += 1
        subscription.worker = None

    def _subscriber_loop(self, subscription: _Subscription) -> None:
        while subscription.active:
            try:
                item = subscription.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if item is None:
                    return
                if not subscription.active:
                    continue
                now = time.monotonic()
                if subscription.disabled_until > now:
                    continue
                destination_hash, announced_identity, app_data = item
                started = time.monotonic()
                subscription.callback(destination_hash, announced_identity, app_data)
                elapsed = time.monotonic() - started
                if elapsed > self._callback_deadline_s:
                    subscription.consecutive_timeouts += 1
                    log.warning(
                        "Announce subscriber %s took %.0fms (limit %.0fms), consecutive_slow=%d",
                        subscription.id,
                        elapsed * 1000,
                        self._callback_deadline_s * 1000,
                        subscription.consecutive_timeouts,
                    )
                    if subscription.consecutive_timeouts >= self._breaker_threshold:
                        subscription.disabled_until = time.monotonic() + self._breaker_cooldown
                        log.warning(
                            "Announce subscriber %s circuit breaker TRIPPED (disabled for %.0fs)",
                            subscription.id,
                            self._breaker_cooldown,
                        )
                else:
                    subscription.consecutive_timeouts = 0
            except Exception:
                log.debug(
                    "Error in announce subscriber %s",
                    subscription.id,
                    exc_info=True,
                )
            finally:
                subscription.queue.task_done()

    def _enqueue(
        self,
        destination_hash: bytes,
        announced_identity: Any,
        app_data: bytes | None,
    ) -> None:
        try:
            self._queue.put_nowait((destination_hash, announced_identity, app_data))
        except queue.Full:
            now = time.monotonic()
            with self._lock:
                self._queue_dropped += 1
                dropped = self._queue_dropped
                should_warn = now - self._last_queue_warning >= 10.0
                if should_warn:
                    self._last_queue_warning = now
            if should_warn:
                log.warning(
                    "Announce dispatch queue full — dropped=%d (latest from %s)",
                    dropped,
                    RNS.prettyhexrep(destination_hash),
                )

    def _dispatch_loop(self) -> None:
        while self._active:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            destination_hash, announced_identity, app_data = item
            with self._lock:
                subs = list(self._subscriptions.values())

            matched_aspects: dict[str, bool] = {}
            for sub in subs:
                now = time.monotonic()
                if sub.disabled_until > now:
                    continue

                try:
                    should_call = False
                    if sub.aspect_filter is None:
                        should_call = True
                    else:
                        af = sub.aspect_filter
                        if af not in matched_aspects:
                            matched_aspects[af] = self._aspect_matches(
                                af,
                                destination_hash,
                                announced_identity,
                            )
                        should_call = matched_aspects[af]

                    if should_call:
                        try:
                            sub.queue.put_nowait((destination_hash, announced_identity, app_data))
                        except queue.Full:
                            sub.dropped += 1
                            with self._lock:
                                self._subscriber_dropped_total += 1
                            sub.disabled_until = max(
                                sub.disabled_until,
                                time.monotonic() + self._breaker_cooldown,
                            )
                            if now - sub.last_warning >= 10.0:
                                sub.last_warning = now
                                log.warning(
                                    "Announce subscriber %s mailbox full; dropped=%d "
                                    "and circuit breaker tripped for %.0fs",
                                    sub.id,
                                    sub.dropped,
                                    self._breaker_cooldown,
                                )
                except Exception:
                    log.debug(
                        "Error in announce subscriber %s",
                        sub.id,
                        exc_info=True,
                    )
            self._queue.task_done()

    def get_stats(self) -> dict[str, int]:
        """Return secret-free queue, drop, and abandoned-worker counters."""

        now = time.monotonic()
        with self._lock:
            subscriptions = list(self._subscriptions.values())
            queue_dropped = self._queue_dropped
            abandoned_workers = self._abandoned_workers
            subscriber_dropped = self._subscriber_dropped_total
        return {
            "pending": self._queue.qsize(),
            "queue_dropped": queue_dropped,
            "subscribers": len(subscriptions),
            "subscriber_pending": sum(sub.queue.qsize() for sub in subscriptions),
            "subscriber_dropped": subscriber_dropped,
            "disabled_subscribers": sum(sub.disabled_until > now for sub in subscriptions),
            "abandoned_workers": abandoned_workers,
        }

    @staticmethod
    def _aspect_matches(
        aspect_filter: str,
        destination_hash: bytes,
        announced_identity: Any,
    ) -> bool:
        if announced_identity is None:
            return False
        try:
            expected = RNS.Destination.hash_from_name_and_identity(
                aspect_filter,
                announced_identity,
            )
            return destination_hash == expected
        except Exception:
            log.debug("Aspect hash match failed", exc_info=True)
            return False


class _RNSHandler:
    """The single handler registered with RNS.Transport."""

    aspect_filter = None

    def __init__(self, dispatcher: AnnounceDispatcher):
        self._dispatcher = dispatcher

    def received_announce(
        self,
        destination_hash: bytes,
        announced_identity: Any,
        app_data: bytes | None,
    ) -> None:
        self._dispatcher._enqueue(destination_hash, announced_identity, app_data)
