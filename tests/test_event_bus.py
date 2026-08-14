"""Tests for the EventBus."""

import threading
from unittest.mock import MagicMock

import pytest

from reticulumpi.event_bus import EventBus, _DaemonWorkerPool


@pytest.fixture(scope="module", autouse=True)
def _no_eventbus_threads_survive_file():
    """Fail if this module adds a worker that survives its final teardown."""
    baseline_ids = {
        id(thread)
        for thread in threading.enumerate()
        if thread.name.startswith("eventbus-") and thread.is_alive()
    }
    yield
    leaked = [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("eventbus-")
        and thread.is_alive()
        and id(thread) not in baseline_ids
    ]
    assert not leaked, f"EventBus worker thread(s) survived the test file: {leaked!r}"


@pytest.fixture(autouse=True)
def _shutdown_test_event_buses_and_pools(monkeypatch):
    """Own and deterministically reap every bus and pool a test creates."""
    import time

    baseline_ids = {
        id(thread)
        for thread in threading.enumerate()
        if thread.name.startswith("eventbus-") and thread.is_alive()
    }
    buses = []
    pools = []
    original_bus_init = EventBus.__init__
    original_pool_init = _DaemonWorkerPool.__init__

    def _tracked_pool_init(self, *args, **kwargs):
        original_pool_init(self, *args, **kwargs)
        pools.append(self)

    def _tracked_bus_init(self, *args, **kwargs):
        original_bus_init(self, *args, **kwargs)
        buses.append(self)

    monkeypatch.setattr(_DaemonWorkerPool, "__init__", _tracked_pool_init)
    monkeypatch.setattr(EventBus, "__init__", _tracked_bus_init)
    yield

    for bus in reversed(buses):
        bus.shutdown()
    for pool in reversed(pools):
        pool.shutdown(cancel_pending=True)

    deadline = time.monotonic() + 5
    for pool in pools:
        for thread in pool._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

    leaked = [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("eventbus-")
        and thread.is_alive()
        and id(thread) not in baseline_ids
    ]
    assert not leaked, f"EventBus worker thread(s) survived test teardown: {leaked!r}"


def test_subscribe_and_publish():
    bus = EventBus()
    cb = MagicMock()
    bus.subscribe("test.event", cb)
    bus.publish("test.event", {"key": "value"})
    cb.assert_called_once_with("test.event", {"key": "value"})


def test_publish_no_subscribers():
    bus = EventBus()
    # Should not raise
    bus.publish("test.event", {"key": "value"})


def test_multiple_subscribers():
    bus = EventBus()
    cb1 = MagicMock()
    cb2 = MagicMock()
    bus.subscribe("test.event", cb1)
    bus.subscribe("test.event", cb2)
    bus.publish("test.event", {"x": 1})
    cb1.assert_called_once_with("test.event", {"x": 1})
    cb2.assert_called_once_with("test.event", {"x": 1})


def test_unsubscribe():
    bus = EventBus()
    cb = MagicMock()
    bus.subscribe("test.event", cb)
    bus.unsubscribe("test.event", cb)
    bus.publish("test.event", {})
    cb.assert_not_called()


def test_unsubscribe_nonexistent():
    bus = EventBus()
    cb = MagicMock()
    # Should not raise
    bus.unsubscribe("test.event", cb)


def test_publish_none_data():
    bus = EventBus()
    cb = MagicMock()
    bus.subscribe("test.event", cb)
    bus.publish("test.event")
    cb.assert_called_once_with("test.event", {})


def test_subscriber_exception_doesnt_block_others():
    bus = EventBus()

    def bad_cb(event_type, data):
        raise ValueError("boom")

    cb2 = MagicMock()
    bus.subscribe("test.event", bad_cb)
    bus.subscribe("test.event", cb2)
    bus.publish("test.event", {"x": 1})
    # cb2 should still be called
    cb2.assert_called_once_with("test.event", {"x": 1})


def test_different_event_types():
    bus = EventBus()
    cb_a = MagicMock()
    cb_b = MagicMock()
    bus.subscribe("event.a", cb_a)
    bus.subscribe("event.b", cb_b)
    bus.publish("event.a", {"a": 1})
    cb_a.assert_called_once()
    cb_b.assert_not_called()


def test_thread_safety():
    bus = EventBus()
    results = []
    barrier = threading.Barrier(3)

    def subscriber(event_type, data):
        results.append(data["id"])

    bus.subscribe("test.event", subscriber)

    def publisher(pub_id):
        barrier.wait()
        for i in range(100):
            bus.publish("test.event", {"id": f"{pub_id}-{i}"})

    threads = [threading.Thread(target=publisher, args=(t,)) for t in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 300


# ---------------------------------------------------------------------------
# subscribe_offloaded tests
# ---------------------------------------------------------------------------


def test_subscribe_offloaded_calls_callback():
    bus = EventBus()
    called = threading.Event()

    def cb(evt, data):
        called.set()

    bus.subscribe_offloaded("test.event", cb)
    bus.publish("test.event", {"x": 1})
    assert called.wait(timeout=5), "Offloaded callback was not called"


def test_subscribe_offloaded_does_not_block_publisher():
    import time

    bus = EventBus()
    started = threading.Event()

    def slow_cb(evt, data):
        started.set()
        time.sleep(2)

    bus.subscribe_offloaded("test.event", slow_cb)
    t0 = time.monotonic()
    bus.publish("test.event", {})
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"publish() blocked for {elapsed:.2f}s"
    started.wait(timeout=5)


def test_subscribe_offloaded_logs_exceptions(caplog):
    import logging

    bus = EventBus()
    done = threading.Event()

    def bad_cb(evt, data):
        try:
            raise RuntimeError("boom")
        finally:
            done.set()

    bus.subscribe_offloaded("test.event", bad_cb)
    with caplog.at_level(logging.ERROR, logger="reticulumpi.event_bus"):
        bus.publish("test.event", {})
        done.wait(timeout=5)
        # Give the executor a moment to flush the log
        import time

        time.sleep(0.1)
    assert "boom" in caplog.text


def test_unsubscribe_offloaded_callback():
    bus = EventBus()
    cb = MagicMock()
    bus.subscribe_offloaded("test.event", cb)
    bus.unsubscribe("test.event", cb)
    bus.publish("test.event", {})
    import time

    time.sleep(0.2)
    cb.assert_not_called()


def test_unsubscribe_all_offloaded_callback():
    bus = EventBus()
    cb = MagicMock()
    bus.subscribe_offloaded("evt.a", cb)
    bus.subscribe_offloaded("evt.b", cb)
    removed = bus.unsubscribe_all(cb)
    assert removed == 2
    bus.publish("evt.a", {})
    bus.publish("evt.b", {})
    import time

    time.sleep(0.2)
    cb.assert_not_called()


def test_unsubscribe_all_removes_handles_for_equivalent_bound_method():
    bus = EventBus()

    class Owner:
        def callback(self, _evt, _data):
            raise AssertionError("callback should have been removed")

    owner = Owner()
    bus.subscribe("evt.a", owner.callback)
    bus.subscribe("evt.b", owner.callback)

    assert bus.unsubscribe_all(owner.callback) == 2
    assert bus._handles == {}
    bus.publish("evt.a", {})
    bus.publish("evt.b", {})


def test_offloaded_callback_can_unsubscribe_each_event_independently():
    """Removing one binding must not orphan the wrapper for another event."""

    bus = EventBus()
    cb = MagicMock()
    bus.subscribe_offloaded("evt.a", cb)
    bus.subscribe_offloaded("evt.b", cb)
    assert bus.unsubscribe("evt.a", cb) is True
    assert bus.unsubscribe("evt.b", cb) is True
    bus.publish("evt.a", {})
    bus.publish("evt.b", {})
    import time

    time.sleep(0.1)
    cb.assert_not_called()


def test_subscription_handle_cancels_exact_registration():
    bus = EventBus()
    cb = MagicMock()
    first = bus.subscribe("evt", cb)
    bus.subscribe("evt", cb)
    assert first.cancel() is True
    bus.publish("evt", {})
    cb.assert_called_once()


def test_shutdown_does_not_wait_for_hung_offloaded_callback():
    import time

    bus = EventBus()
    started = threading.Event()
    never = threading.Event()

    def hung(_evt, _data):
        started.set()
        never.wait()

    bus.subscribe_offloaded("evt", hung)
    bus.publish("evt", {})
    try:
        assert started.wait(1)
        begin = time.monotonic()
        bus.shutdown()
        assert time.monotonic() - begin < 1.5
        stats = bus.get_stats()
        assert stats["abandoned_workers"] == 1
        assert "dropped" in stats and "pending" in stats
    finally:
        # Preserve the bounded-shutdown assertion, then release and reap the
        # deliberately blocked callback so it cannot contaminate later tests.
        never.set()
        bus.shutdown()
        for thread in bus._offload_pool._threads:
            thread.join(timeout=1)

    assert not any(thread.is_alive() for thread in bus._offload_pool._threads)


def test_bounded_pool_drops_work_cancels_pending_and_rejects_after_shutdown():
    pool = _DaemonWorkerPool(max_workers=0, max_pending=1)
    assert pool.submit(lambda: None) is True
    assert pool.submit(lambda: None) is False
    assert pool.stats() == {
        "pending": 1,
        "dropped": 1,
        "workers": 0,
        "abandoned_workers": 0,
    }
    pool.shutdown(cancel_pending=True)
    assert pool.stats()["pending"] == 0
    assert pool.submit(lambda: None) is False
    pool.shutdown()


def test_pool_shutdown_handles_full_queue_without_blocking():
    pool = _DaemonWorkerPool(max_workers=1, max_pending=1)
    started = threading.Event()
    release = threading.Event()

    def blocked():
        started.set()
        release.wait()

    try:
        assert pool.submit(blocked) is True
        assert started.wait(1)
        assert pool.submit(lambda: None) is True
        pool.shutdown(cancel_pending=False)
        assert pool.stats()["abandoned_workers"] == 1
    finally:
        # Let the daemon drain, then supply a stop token if the queue-full
        # shutdown branch could not enqueue one.
        release.set()
        for thread in pool._threads:
            thread.join(timeout=0.5)
        for thread in pool._threads:
            if thread.is_alive():
                pool._queue.put(pool._STOP, timeout=1)
                thread.join(timeout=1)

    assert not pool._threads[0].is_alive()


def test_subscription_context_cancels_and_shutdown_rejects_new_subscribers():
    bus = EventBus()
    callback = MagicMock()
    with bus.subscribe("event", callback) as subscription:
        assert subscription.event_type == "event"
        bus.publish("event", {})
    bus.publish("event", {})
    callback.assert_called_once()

    bus.shutdown()
    bus.shutdown()
    for subscribe in (bus.subscribe, bus.subscribe_offloaded):
        with pytest.raises(RuntimeError, match="shut down"):
            subscribe("event", callback)


def test_missing_listener_paths_return_false_without_corrupting_registry():
    bus = EventBus()
    callback = MagicMock()
    handle = bus.subscribe("event", callback)
    bus._subscribers["event"].clear()
    assert handle.cancel() is False

    bus.subscribe("event", callback)
    bus._subscribers["event"].clear()
    assert bus.unsubscribe("event", callback) is False


def test_offload_backpressure_warning_is_rate_limited(caplog):
    import logging

    bus = EventBus()
    callback = MagicMock()
    bus._offload_pool.submit = MagicMock(return_value=False)
    bus._offload_pool.stats = MagicMock(
        return_value={"pending": 64, "dropped": 2, "workers": 8, "abandoned_workers": 0}
    )
    bus.subscribe_offloaded("event", callback)

    with caplog.at_level(logging.WARNING, logger="reticulumpi.event_bus"):
        bus.publish("event", {})
        bus.publish("event", {})

    assert caplog.text.count("Event bus backpressure") == 1
