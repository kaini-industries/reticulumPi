"""Tests for the AnnounceDispatcher centralized announce multiplexer."""

import threading
import time
from unittest.mock import MagicMock

import pytest

# All tests mock RNS at module level so the real library is never imported.
_rns_mock = MagicMock()
_rns_mock.Transport = MagicMock()
_rns_mock.Destination = MagicMock()
_rns_mock.prettyhexrep = lambda h: h.hex()


@pytest.fixture(autouse=True)
def _patch_rns(monkeypatch):
    """Ensure every test sees a fresh RNS mock and no real import."""
    import sys

    _rns_mock.Transport = MagicMock()
    _rns_mock.Destination = MagicMock()
    monkeypatch.setitem(sys.modules, "RNS", _rns_mock)
    if "reticulumpi.announce_dispatcher" in sys.modules:
        monkeypatch.setattr(sys.modules["reticulumpi.announce_dispatcher"], "RNS", _rns_mock)
    yield


def _make_dispatcher(**overrides):
    """Create an AnnounceDispatcher with fast test-friendly defaults."""
    from reticulumpi.announce_dispatcher import AnnounceDispatcher

    defaults = dict(
        max_queue=100,
        callback_deadline_ms=50,
        breaker_threshold=3,
        breaker_cooldown=0.1,
    )
    defaults.update(overrides)
    return AnnounceDispatcher(**defaults)


@pytest.fixture
def dispatcher():
    """Provide a started dispatcher that is stopped after the test."""
    d = _make_dispatcher()
    d.start()
    yield d
    d.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEST_HASH = b"\xaa" * 16
IDENTITY = MagicMock()
APP_DATA = b"hello"


def _wait_for_event(event: threading.Event, timeout: float = 5.0) -> None:
    assert event.wait(timeout), "Timed out waiting for callback"


# ===================================================================
# 1. Subscribe / unsubscribe lifecycle
# ===================================================================


class TestSubscribeUnsubscribe:
    def test_subscribe_returns_string_id(self, dispatcher):
        sub_id = dispatcher.subscribe(None, lambda *a: None)
        assert isinstance(sub_id, str)
        assert len(sub_id) == 12

    def test_subscribe_returns_unique_ids(self, dispatcher):
        ids = {dispatcher.subscribe(None, lambda *a: None) for _ in range(50)}
        assert len(ids) == 50

    def test_unsubscribe_removes_entry(self, dispatcher):
        cb = MagicMock()
        sub_id = dispatcher.subscribe(None, cb)
        dispatcher.unsubscribe(sub_id)
        # After unsubscribe the internal dict should not contain the id.
        assert sub_id not in dispatcher._subscriptions

    def test_double_unsubscribe_is_safe(self, dispatcher):
        sub_id = dispatcher.subscribe(None, lambda *a: None)
        dispatcher.unsubscribe(sub_id)
        dispatcher.unsubscribe(sub_id)  # must not raise

    def test_unsubscribe_unknown_id_is_safe(self, dispatcher):
        dispatcher.unsubscribe("nonexistent_id")  # must not raise


# ===================================================================
# 2. Dispatch filtering
# ===================================================================


class TestDispatchFiltering:
    def test_wildcard_receives_all_announces(self, dispatcher):
        received = threading.Event()
        results = []

        def cb(dest, ident, data):
            results.append((dest, ident, data))
            received.set()

        dispatcher.subscribe(None, cb)
        dispatcher._enqueue(DEST_HASH, IDENTITY, APP_DATA)
        _wait_for_event(received)
        assert len(results) == 1
        assert results[0] == (DEST_HASH, IDENTITY, APP_DATA)

    def test_aspect_filter_match(self, dispatcher):
        received = threading.Event()
        results = []

        _rns_mock.Destination.hash_from_name_and_identity.return_value = DEST_HASH

        def cb(dest, ident, data):
            results.append(dest)
            received.set()

        dispatcher.subscribe("test.aspect", cb)
        dispatcher._enqueue(DEST_HASH, IDENTITY, APP_DATA)
        _wait_for_event(received)
        assert len(results) == 1

    def test_aspect_filter_mismatch(self, dispatcher):
        cb = MagicMock()

        # Return a different hash so the filter does not match.
        _rns_mock.Destination.hash_from_name_and_identity.return_value = b"\xbb" * 16

        dispatcher.subscribe("wrong.aspect", cb)
        dispatcher._enqueue(DEST_HASH, IDENTITY, APP_DATA)
        # Give the dispatch loop time to process.
        time.sleep(0.3)
        cb.assert_not_called()

    def test_aspect_filter_with_none_identity_returns_no_match(self, dispatcher):
        cb = MagicMock()
        dispatcher.subscribe("some.aspect", cb)
        dispatcher._enqueue(DEST_HASH, None, APP_DATA)
        time.sleep(0.3)
        cb.assert_not_called()

    def test_multiple_subscribers_all_receive(self, dispatcher):
        event_a = threading.Event()
        event_b = threading.Event()

        dispatcher.subscribe(None, lambda *a: event_a.set())
        dispatcher.subscribe(None, lambda *a: event_b.set())
        dispatcher._enqueue(DEST_HASH, IDENTITY, APP_DATA)
        assert event_a.wait(5.0), "cb_a not called"
        assert event_b.wait(5.0), "cb_b not called"

    def test_mixed_wildcard_and_filtered(self, dispatcher):
        received_wildcard = threading.Event()
        received_filtered = threading.Event()

        _rns_mock.Destination.hash_from_name_and_identity.return_value = DEST_HASH

        dispatcher.subscribe(None, lambda *a: received_wildcard.set())
        dispatcher.subscribe("matching.aspect", lambda *a: received_filtered.set())
        dispatcher._enqueue(DEST_HASH, IDENTITY, APP_DATA)
        _wait_for_event(received_wildcard)
        _wait_for_event(received_filtered)

    def test_unsubscribed_callback_not_called(self, dispatcher):
        cb = MagicMock()
        sentinel = threading.Event()

        sub_id = dispatcher.subscribe(None, cb)
        dispatcher.unsubscribe(sub_id)
        # Use a second subscriber as a sentinel to know dispatch finished.
        dispatcher.subscribe(None, lambda *a: sentinel.set())
        dispatcher._enqueue(DEST_HASH, IDENTITY, APP_DATA)
        _wait_for_event(sentinel)
        cb.assert_not_called()


# ===================================================================
# 3. Circuit breaker
# ===================================================================


class TestCircuitBreaker:
    def test_slow_callback_increments_timeout_count(self, dispatcher):
        received = threading.Event()

        def slow_cb(dest, ident, data):
            time.sleep(0.08)  # exceeds 50ms deadline
            received.set()

        sub_id = dispatcher.subscribe(None, slow_cb)
        dispatcher._enqueue(DEST_HASH, IDENTITY, APP_DATA)
        _wait_for_event(received)
        time.sleep(0.05)  # let dispatch loop record the timing
        sub = dispatcher._subscriptions[sub_id]
        assert sub.consecutive_timeouts >= 1

    def test_fast_callback_resets_timeout_count(self, dispatcher):
        call_count = {"n": 0}
        done = threading.Event()

        def cb(dest, ident, data):
            call_count["n"] += 1
            if call_count["n"] == 1:
                time.sleep(0.08)  # first call is slow
            # second call is fast
            if call_count["n"] == 2:
                done.set()

        sub_id = dispatcher.subscribe(None, cb)
        dispatcher._enqueue(DEST_HASH, IDENTITY, APP_DATA)
        time.sleep(0.2)
        dispatcher._enqueue(DEST_HASH, IDENTITY, APP_DATA)
        _wait_for_event(done)
        time.sleep(0.05)
        sub = dispatcher._subscriptions[sub_id]
        assert sub.consecutive_timeouts == 0

    def test_breaker_trips_after_threshold(self, dispatcher):
        call_count = {"n": 0}
        last_call = threading.Event()

        def slow_cb(dest, ident, data):
            call_count["n"] += 1
            time.sleep(0.08)  # always slow
            if call_count["n"] == 3:
                last_call.set()

        sub_id = dispatcher.subscribe(None, slow_cb)
        # Send 3 announces to hit the threshold (breaker_threshold=3).
        for _ in range(3):
            dispatcher._enqueue(DEST_HASH, IDENTITY, APP_DATA)
        _wait_for_event(last_call)
        time.sleep(0.05)
        sub = dispatcher._subscriptions[sub_id]
        assert sub.disabled_until > 0

    def test_disabled_subscriber_skipped(self, dispatcher):
        call_count = {"n": 0}
        tripped = threading.Event()

        def slow_cb(dest, ident, data):
            call_count["n"] += 1
            time.sleep(0.08)
            if call_count["n"] == 3:
                tripped.set()

        dispatcher.subscribe(None, slow_cb)
        for _ in range(3):
            dispatcher._enqueue(DEST_HASH, IDENTITY, APP_DATA)
        _wait_for_event(tripped)
        time.sleep(0.05)

        # Now send more announces -- the tripped subscriber should be skipped.
        sentinel = threading.Event()
        dispatcher.subscribe(None, lambda *a: sentinel.set())
        pre_trip_count = call_count["n"]
        dispatcher._enqueue(DEST_HASH, IDENTITY, APP_DATA)
        _wait_for_event(sentinel)
        assert call_count["n"] == pre_trip_count

    def test_recovery_after_cooldown(self, dispatcher):
        call_count = {"n": 0}
        tripped = threading.Event()
        recovered = threading.Event()

        def slow_then_fast_cb(dest, ident, data):
            call_count["n"] += 1
            if call_count["n"] <= 3:
                time.sleep(0.08)
                if call_count["n"] == 3:
                    tripped.set()
            else:
                recovered.set()

        dispatcher.subscribe(None, slow_then_fast_cb)
        for _ in range(3):
            dispatcher._enqueue(DEST_HASH, IDENTITY, APP_DATA)
        _wait_for_event(tripped)

        # Wait for cooldown (0.1s) to expire.
        time.sleep(0.2)

        dispatcher._enqueue(DEST_HASH, IDENTITY, APP_DATA)
        _wait_for_event(recovered)
        assert call_count["n"] == 4

    def test_callback_exception_does_not_crash_loop(self, dispatcher):
        sentinel = threading.Event()

        def bad_cb(dest, ident, data):
            raise RuntimeError("boom")

        dispatcher.subscribe(None, bad_cb)
        dispatcher.subscribe(None, lambda *a: sentinel.set())
        dispatcher._enqueue(DEST_HASH, IDENTITY, APP_DATA)
        _wait_for_event(sentinel)  # loop survived

    def test_hung_subscriber_does_not_block_healthy_subscriber(self, dispatcher):
        entered = threading.Event()
        never = threading.Event()
        healthy = threading.Event()

        def hung(*_args):
            entered.set()
            never.wait()

        dispatcher.subscribe(None, hung)
        dispatcher.subscribe(None, lambda *_args: healthy.set())
        dispatcher._enqueue(DEST_HASH, IDENTITY, APP_DATA)
        _wait_for_event(entered)
        _wait_for_event(healthy)


# ===================================================================
# 4. Queue overflow
# ===================================================================


class TestQueueOverflow:
    def test_full_queue_drops_without_raising(self):
        d = _make_dispatcher(max_queue=2)
        # Don't start the dispatcher so nothing drains the queue.
        d._enqueue(b"\x01" * 16, IDENTITY, b"a")
        d._enqueue(b"\x02" * 16, IDENTITY, b"b")
        # Queue is now full.  This must not raise.
        d._enqueue(b"\x03" * 16, IDENTITY, b"c")
        assert d._queue.qsize() == 2
        assert d.get_stats()["queue_dropped"] == 1

    def test_stats_report_subscription_pressure_without_callback_data(self):
        d = _make_dispatcher()
        subscription_id = d.subscribe(None, lambda *_args: None)
        try:
            stats = d.get_stats()
            assert stats["subscribers"] == 1
            assert stats["subscriber_pending"] == 0
            assert stats["subscriber_dropped"] == 0
            assert stats["abandoned_workers"] == 0
            assert set(stats) == {
                "pending",
                "queue_dropped",
                "subscribers",
                "subscriber_pending",
                "subscriber_dropped",
                "disabled_subscribers",
                "abandoned_workers",
            }
        finally:
            d.unsubscribe(subscription_id)

    def test_subscriber_drop_total_survives_unsubscribe(self, dispatcher):
        entered = threading.Event()
        release = threading.Event()

        def blocked(*_args):
            entered.set()
            release.wait()

        subscription_id = dispatcher.subscribe(None, blocked)
        dispatcher._enqueue(DEST_HASH, IDENTITY, APP_DATA)
        _wait_for_event(entered)
        subscription = dispatcher._subscriptions[subscription_id]
        for _ in range(subscription.queue.maxsize):
            subscription.queue.put_nowait((DEST_HASH, IDENTITY, APP_DATA))

        dispatcher._enqueue(DEST_HASH, IDENTITY, APP_DATA)
        deadline = time.monotonic() + 2.0
        while dispatcher.get_stats()["subscriber_dropped"] == 0:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        dispatcher.unsubscribe(subscription_id)
        assert dispatcher.get_stats()["subscriber_dropped"] == 1
        release.set()

    def test_full_queue_logs_warning(self, caplog):
        import logging

        d = _make_dispatcher(max_queue=1)
        d._enqueue(b"\x01" * 16, IDENTITY, b"a")
        with caplog.at_level(logging.WARNING, logger="reticulumpi.announce_dispatcher"):
            d._enqueue(b"\x02" * 16, IDENTITY, b"b")
        assert "queue full" in caplog.text.lower()


# ===================================================================
# 5. Start / stop lifecycle
# ===================================================================


class TestStartStop:
    def test_start_creates_worker_thread(self):
        d = _make_dispatcher()
        d.start()
        try:
            assert d._worker is not None
            assert d._worker.is_alive()
            assert d._worker.name == "announce-dispatcher"
            assert d._worker.daemon is True
        finally:
            d.stop()

    def test_start_registers_rns_handler(self):
        d = _make_dispatcher()
        d.start()
        try:
            _rns_mock.Transport.register_announce_handler.assert_called_once()
            handler = _rns_mock.Transport.register_announce_handler.call_args[0][0]
            assert handler.aspect_filter is None
        finally:
            d.stop()

    def test_stop_joins_worker(self):
        d = _make_dispatcher()
        d.start()
        worker = d._worker
        d.stop()
        assert d._worker is None
        assert not worker.is_alive()

    def test_stop_deregisters_rns_handler(self):
        d = _make_dispatcher()
        d.start()
        d.stop()
        _rns_mock.Transport.deregister_announce_handler.assert_called_once()

    def test_double_stop_is_safe(self):
        d = _make_dispatcher()
        d.start()
        d.stop()
        d.stop()  # must not raise

    def test_rns_handler_forwards_to_enqueue(self):
        from reticulumpi.announce_dispatcher import _RNSHandler

        d = _make_dispatcher()
        handler = _RNSHandler(d)
        handler.received_announce(DEST_HASH, IDENTITY, APP_DATA)
        assert d._queue.qsize() == 1
        item = d._queue.get_nowait()
        assert item == (DEST_HASH, IDENTITY, APP_DATA)


# ===================================================================
# 6. Aspect matching edge cases
# ===================================================================


class TestAspectMatching:
    def test_hash_from_name_raises_returns_false(self, dispatcher):
        """When RNS.Destination.hash_from_name_and_identity raises, aspect
        match should return False and the subscriber should not fire."""
        _rns_mock.Destination.hash_from_name_and_identity.side_effect = Exception("bad")
        cb = MagicMock()
        sentinel = threading.Event()

        dispatcher.subscribe("broken.aspect", cb)
        dispatcher.subscribe(None, lambda *a: sentinel.set())
        dispatcher._enqueue(DEST_HASH, IDENTITY, APP_DATA)
        _wait_for_event(sentinel)
        cb.assert_not_called()

    def test_aspect_cache_reused_for_same_filter(self, dispatcher):
        """Two subscribers with the same aspect filter should trigger only one
        call to hash_from_name_and_identity per announce (caching)."""
        _rns_mock.Destination.hash_from_name_and_identity.return_value = DEST_HASH
        done = threading.Event()
        count = {"n": 0}

        def _cb(*a):
            count["n"] += 1
            if count["n"] == 2:
                done.set()

        dispatcher.subscribe("shared.aspect", _cb)
        dispatcher.subscribe("shared.aspect", _cb)
        dispatcher._enqueue(DEST_HASH, IDENTITY, APP_DATA)
        assert done.wait(5.0), "Both callbacks not called"

        calls = [
            c
            for c in _rns_mock.Destination.hash_from_name_and_identity.call_args_list
            if c[0][0] == "shared.aspect"
        ]
        assert len(calls) == 1
