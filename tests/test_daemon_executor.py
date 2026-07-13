"""Regressions for bounded daemon executor shutdown semantics."""

from __future__ import annotations

import threading
import time

import pytest

from reticulumpi.daemon_executor import BoundedDaemonExecutor


def test_hung_worker_does_not_make_shutdown_wait_indefinitely():
    entered = threading.Event()
    release = threading.Event()
    executor = BoundedDaemonExecutor(max_workers=1, max_pending=2, thread_name_prefix="test")

    def hang():
        entered.set()
        release.wait(timeout=2)

    executor.submit(hang)
    assert entered.wait(timeout=1)
    started = time.monotonic()
    executor.shutdown(wait=True, cancel_futures=True)

    assert time.monotonic() - started < 0.5
    assert executor.abandoned_workers == 1
    with pytest.raises(RuntimeError, match="after executor shutdown"):
        executor.submit(lambda: None)
    release.set()


def test_queue_saturation_rejects_without_unbounded_wait():
    entered = threading.Event()
    release = threading.Event()
    executor = BoundedDaemonExecutor(max_workers=1, max_pending=1, thread_name_prefix="test")

    def hang():
        entered.set()
        release.wait(timeout=2)

    executor.submit(hang)
    assert entered.wait(timeout=1)
    queued = executor.submit(lambda: None)
    with pytest.raises(RuntimeError, match="saturated"):
        executor.submit(lambda: None)

    executor.shutdown(wait=False, cancel_futures=True)
    assert queued.cancelled()
    release.set()
