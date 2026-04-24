"""Tests for the EventBus."""

import threading
from unittest.mock import MagicMock

from reticulumpi.event_bus import EventBus


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
