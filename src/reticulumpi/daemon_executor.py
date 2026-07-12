"""Bounded daemon-thread executor for isolation from permanently blocked work."""

from __future__ import annotations

import concurrent.futures
import queue
import threading
from collections.abc import Callable
from typing import Any


class BoundedDaemonExecutor(concurrent.futures.Executor):
    """A small Future-compatible pool whose workers cannot hold process exit."""

    _STOP = object()

    def __init__(self, *, max_workers: int, max_pending: int, thread_name_prefix: str) -> None:
        if max_workers < 1 or max_pending < 1:
            raise ValueError("executor limits must be positive")
        self._max_workers = max_workers
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max_pending)
        self._lock = threading.Lock()
        self._shutdown = False
        self._threads = [
            threading.Thread(
                target=self._worker,
                name=f"{thread_name_prefix}-{index}",
                daemon=True,
            )
            for index in range(max_workers)
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any):
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule work after executor shutdown")
            try:
                self._queue.put_nowait((future, fn, args, kwargs))
            except queue.Full as exc:
                raise RuntimeError("daemon executor is saturated") from exc
        return future

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                future, function, args, kwargs = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = function(*args, **kwargs)
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            finally:
                self._queue.task_done()

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        if cancel_futures:
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    if item is not self._STOP:
                        future, _function, _args, _kwargs = item
                        future.cancel()
                finally:
                    self._queue.task_done()
        for _thread in self._threads:
            try:
                self._queue.put_nowait(self._STOP)
            except queue.Full:
                break
        if wait:
            for thread in self._threads:
                thread.join(timeout=0.25)

    @property
    def abandoned_workers(self) -> int:
        return sum(thread.is_alive() for thread in self._threads) if self._shutdown else 0
