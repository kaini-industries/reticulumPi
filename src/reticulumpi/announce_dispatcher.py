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


class _Subscription:
    __slots__ = ("id", "aspect_filter", "callback", "consecutive_timeouts", "disabled_until")

    def __init__(self, sub_id: str, aspect_filter: str | None, callback: AnnounceCallback):
        self.id = sub_id
        self.aspect_filter = aspect_filter
        self.callback = callback
        self.consecutive_timeouts = 0
        self.disabled_until = 0.0


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
        log.info("Announce dispatcher stopped")

    def subscribe(
        self,
        aspect_filter: str | None,
        callback: AnnounceCallback,
    ) -> str:
        sub_id = uuid.uuid4().hex[:12]
        sub = _Subscription(sub_id, aspect_filter, callback)
        with self._lock:
            self._subscriptions[sub_id] = sub
        return sub_id

    def unsubscribe(self, sub_id: str) -> None:
        with self._lock:
            self._subscriptions.pop(sub_id, None)

    def _enqueue(
        self,
        destination_hash: bytes,
        announced_identity: Any,
        app_data: bytes | None,
    ) -> None:
        try:
            self._queue.put_nowait((destination_hash, announced_identity, app_data))
        except queue.Full:
            log.warning(
                "Announce dispatch queue full — dropped announce from %s",
                RNS.prettyhexrep(destination_hash),
            )

    def _dispatch_loop(self) -> None:
        while self._active:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            destination_hash, announced_identity, app_data = item
            now = time.monotonic()

            with self._lock:
                subs = list(self._subscriptions.values())

            matched_aspects: dict[str, bool] = {}
            for sub in subs:
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
                        t0 = time.monotonic()
                        sub.callback(destination_hash, announced_identity, app_data)
                        elapsed = time.monotonic() - t0

                        if elapsed > self._callback_deadline_s:
                            sub.consecutive_timeouts += 1
                            log.warning(
                                "Announce subscriber %s took %.0fms (limit %.0fms), "
                                "consecutive_slow=%d",
                                sub.id,
                                elapsed * 1000,
                                self._callback_deadline_s * 1000,
                                sub.consecutive_timeouts,
                            )
                            if sub.consecutive_timeouts >= self._breaker_threshold:
                                sub.disabled_until = time.monotonic() + self._breaker_cooldown
                                log.warning(
                                    "Announce subscriber %s circuit breaker TRIPPED "
                                    "(disabled for %.0fs)",
                                    sub.id,
                                    self._breaker_cooldown,
                                )
                        else:
                            sub.consecutive_timeouts = 0
                except Exception:
                    log.debug(
                        "Error in announce subscriber %s",
                        sub.id,
                        exc_info=True,
                    )

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
