"""Tests for PluginBase helper methods."""

import asyncio
import io
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import RNS

from reticulumpi import events
from reticulumpi.event_bus import EventBus
from reticulumpi.plugin_base import (
    PluginBase,
    PluginHealth,
    PluginState,
    resolve_ready_plugin,
)


@pytest.fixture(autouse=True)
def _reset_thread_budget():
    """Save and restore class-level thread budget state between tests."""
    saved_count = PluginBase._global_thread_count
    saved_budget = PluginBase._global_thread_budget
    PluginBase._global_thread_count = 0
    yield
    PluginBase._global_thread_count = saved_count
    PluginBase._global_thread_budget = saved_budget


class FakePlugin(PluginBase):
    plugin_name = "fake"
    plugin_version = "1.0.0"

    def start(self):
        self._active = True

    def stop(self):
        self._active = False


def test_default_plugin_description(mock_app):
    plugin = FakePlugin(mock_app, {"enabled": True})
    assert plugin.plugin_description == "No description"


def test_get_status_default(mock_app):
    plugin = FakePlugin(mock_app, {"enabled": True})
    assert plugin.get_status()["active"] is False
    assert plugin.get_status()["_lifecycle"]["state"] == "discovered"
    plugin._active = True
    assert plugin.get_status()["active"] is True
    # Activity/cancellation is plugin-owned; lifecycle transitions are
    # orchestrator-owned and occur only after start()/cleanup boundaries.
    assert plugin.get_status()["_lifecycle"]["state"] == "discovered"


def test_logger_uses_plugin_name(mock_app):
    plugin = FakePlugin(mock_app, {"enabled": True})
    assert plugin.log.name == "reticulumpi.plugin.fake"


def test_ready_plugin_uses_legacy_host_compatibility(mock_app):
    dependency = object()
    mock_app.get_plugin.return_value = dependency
    plugin = FakePlugin(mock_app, {"enabled": True})

    assert plugin.get_ready_plugin("dependency") is dependency
    mock_app.get_plugin.assert_called_once_with("dependency")


def test_ready_plugin_uses_lifecycle_host_api(mock_app):
    class LifecycleHost:
        def get_ready_plugin(self, name):
            return ("ready", name)

    host = LifecycleHost()
    host.reticulum = mock_app.reticulum
    host.identity = mock_app.identity
    host.event_bus = mock_app.event_bus
    host.announce_dispatcher = mock_app.announce_dispatcher
    plugin = FakePlugin(host, {"enabled": True})

    assert plugin.get_ready_plugin("dependency") == ("ready", "dependency")


def test_sleep_while_active_exits_early(mock_app):
    plugin = FakePlugin(mock_app, {"enabled": True})
    plugin._active = False
    # Should return immediately since _active is False
    plugin._sleep_while_active(100)


def test_sleep_while_active_actually_sleeps(mock_app):
    plugin = FakePlugin(mock_app, {"enabled": True})
    plugin._active = True
    start = time.monotonic()
    plugin._sleep_while_active(0.2)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.15


def test_sleep_while_active_handles_float(mock_app):
    plugin = FakePlugin(mock_app, {"enabled": True})
    plugin._active = True
    start = time.monotonic()
    plugin._sleep_while_active(0.5)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.4


def test_start_thread(mock_app):
    plugin = FakePlugin(mock_app, {"enabled": True})
    results = []
    thread = plugin._start_thread(lambda: results.append(1), "test-thread")
    thread.join(timeout=2)
    assert results == [1]
    assert thread.daemon is True
    assert thread.name == "test-thread"


def test_start_thread_tracked(mock_app):
    plugin = FakePlugin(mock_app, {"enabled": True})
    release = __import__("threading").Event()
    thread = plugin._start_thread(lambda: release.wait(2), "tracked")
    assert thread in plugin._threads
    release.set()
    thread.join(timeout=2)
    assert thread not in plugin._threads


def test_join_threads(mock_app):
    plugin = FakePlugin(mock_app, {"enabled": True})
    plugin._active = True
    plugin._start_thread(lambda: plugin._sleep_while_active(2), "t1")
    plugin._start_thread(lambda: plugin._sleep_while_active(2), "t2")
    assert len(plugin._threads) == 2
    plugin._active = False
    plugin._join_threads()
    assert plugin._threads == []


def test_validate_config_called(mock_app):
    """validate_config is called during construction."""

    class ValidatingPlugin(PluginBase):
        plugin_name = "validating"
        plugin_version = "1.0.0"
        validated = False

        def validate_config(self):
            ValidatingPlugin.validated = True

        def start(self):
            pass

        def stop(self):
            pass

    ValidatingPlugin(mock_app, {"enabled": True})
    assert ValidatingPlugin.validated is True


def test_rejected_config_does_not_leave_base_event_subscriptions(mock_app):
    bus = EventBus()
    mock_app.event_bus = bus
    before = len(bus._handles)

    class InvalidPlugin(FakePlugin):
        def validate_config(self):
            raise ValueError("invalid")

    with pytest.raises(ValueError, match="invalid"):
        InvalidPlugin(mock_app, {})

    assert len(bus._handles) == before
    bus.shutdown()


def test_subclass_failure_after_super_does_not_leave_base_subscriptions(mock_app):
    bus = EventBus()
    mock_app.event_bus = bus
    before = len(bus._handles)

    class FailsAfterSuper(FakePlugin):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            raise RuntimeError("subclass initialization failed")

    with pytest.raises(RuntimeError, match="subclass initialization failed"):
        FailsAfterSuper(mock_app, {})

    assert len(bus._handles) == before
    bus.shutdown()


def test_base_event_subscriptions_are_managed_without_join_threads(mock_app):
    bus = EventBus()
    mock_app.event_bus = bus
    before = len(bus._handles)
    plugin = FakePlugin(mock_app, {})

    assert len(bus._handles) == before
    plugin.mark_starting()
    assert len(bus._handles) == before + 2
    plugin.cleanup_managed_resources()
    assert len(bus._handles) == before
    bus.shutdown()


def test_plugin_name_collision_first_trusted_definition_wins(tmp_path):
    """Later search paths cannot shadow a previously discovered plugin."""
    from reticulumpi.plugin_loader import PluginLoader

    plugin_a = tmp_path / "dir_a"
    plugin_a.mkdir()
    (plugin_a / "first.py").write_text(
        "from reticulumpi.plugin_base import PluginBase\n"
        "class First(PluginBase):\n"
        "    plugin_name = 'dupe'\n"
        "    plugin_version = '1.0.0'\n"
        "    def start(self): pass\n"
        "    def stop(self): pass\n"
    )
    plugin_b = tmp_path / "dir_b"
    plugin_b.mkdir()
    (plugin_b / "second.py").write_text(
        "from reticulumpi.plugin_base import PluginBase\n"
        "class Second(PluginBase):\n"
        "    plugin_name = 'dupe'\n"
        "    plugin_version = '2.0.0'\n"
        "    def start(self): pass\n"
        "    def stop(self): pass\n"
    )
    loader = PluginLoader()
    found = loader.discover([str(plugin_a), str(plugin_b)])
    assert "dupe" in found
    assert found["dupe"].plugin_version == "1.0.0"


# ── Thread budget tests ───────────────────────────────────────────


def test_start_thread_increments_global_count(mock_app):
    plugin = FakePlugin(mock_app, {"enabled": True})
    assert PluginBase.get_thread_count() == 0
    release = __import__("threading").Event()
    t = plugin._start_thread(lambda: release.wait(2), "budget-t1")
    assert PluginBase.get_thread_count() == 1
    release.set()
    t.join(timeout=2)
    assert PluginBase.get_thread_count() == 0


def test_join_threads_decrements_global_count(mock_app):
    plugin = FakePlugin(mock_app, {"enabled": True})
    plugin._active = True
    plugin._start_thread(lambda: plugin._sleep_while_active(2), "budget-t1")
    plugin._start_thread(lambda: plugin._sleep_while_active(2), "budget-t2")
    assert PluginBase.get_thread_count() == 2
    plugin._active = False
    plugin._join_threads()
    assert PluginBase.get_thread_count() == 0


def test_thread_budget_warning_logged(mock_app, caplog):
    PluginBase.set_thread_budget(0)
    plugin = FakePlugin(mock_app, {"enabled": True})
    with caplog.at_level(logging.WARNING):
        t = plugin._start_thread(lambda: None, "over-budget")
        t.join(timeout=2)
    assert "thread budget exceeded" in caplog.text.lower()


def test_set_thread_budget(mock_app):
    PluginBase.set_thread_budget(10)
    assert PluginBase._global_thread_budget == 10


def test_thread_count_never_negative(mock_app):
    PluginBase._global_thread_count = 0
    plugin = FakePlugin(mock_app, {"enabled": True})
    plugin._join_threads()
    assert PluginBase.get_thread_count() == 0


def test_fast_threads_unregister_without_leaking_budget(mock_app):
    plugin = FakePlugin(mock_app, {"enabled": True})
    threads = [plugin._start_thread(lambda: None, f"fast-{i}") for i in range(100)]
    for thread in threads:
        thread.join(timeout=2)
    assert plugin._threads == []
    assert PluginBase.get_thread_count() == 0


def test_lifecycle_v2_requires_explicit_readiness(mock_app):
    class V2Plugin(FakePlugin):
        plugin_lifecycle_api = 2

    plugin = V2Plugin(mock_app, {})
    plugin.mark_starting()
    plugin._active = True
    assert plugin.plugin_state == PluginState.STARTING
    assert plugin.plugin_health == PluginHealth.UNAVAILABLE
    plugin.mark_ready()
    assert plugin.wait_until_ready(timeout=0.1)
    assert plugin.plugin_state == PluginState.READY


def test_lifecycle_metrics_measure_readiness_and_count_hung_once(mock_app):
    plugin = FakePlugin(mock_app, {})
    plugin.mark_starting()
    time.sleep(0.001)
    plugin.mark_ready()

    metrics = plugin.get_lifecycle_metrics()
    assert metrics["readiness_seconds"] > 0
    assert metrics["state_age_seconds"] >= 0
    assert metrics["hung_total"] == 0

    plugin.mark_hung("stuck")
    plugin.mark_hung("still stuck")
    assert plugin.get_lifecycle_metrics()["hung_total"] == 1


def test_managed_process_group_is_stopped_once(mock_app):
    plugin = FakePlugin(mock_app, {"enabled": True})
    group = __import__("unittest.mock").mock.MagicMock()
    assert plugin.manage_process_group(group) is group
    plugin.cleanup_managed_resources()
    plugin.cleanup_managed_resources()
    group.stop.assert_called_once()


def test_managed_executor_shutdown_is_nonblocking_and_idempotent(mock_app):
    plugin = FakePlugin(mock_app, {"enabled": True})
    executor = MagicMock()

    assert plugin.manage_executor(executor) is executor
    plugin.cleanup_managed_resources()
    plugin.cleanup_managed_resources()

    executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)


def test_managed_async_task_cancels_on_owning_loop_thread_safely(mock_app):
    plugin = FakePlugin(mock_app, {"enabled": True})
    task = MagicMock()
    loop = MagicMock()
    task.done.return_value = False
    task.get_loop.return_value = loop
    loop.is_running.return_value = True
    loop.is_closed.return_value = False

    plugin.manage_async_task(task)
    with patch("asyncio.get_running_loop", side_effect=RuntimeError):
        plugin.cleanup_managed_resources()

    loop.call_soon_threadsafe.assert_called_once_with(task.cancel)
    task.cancel.assert_not_called()


def test_managed_request_handler_is_deregistered_in_reverse_order(mock_app):
    plugin = FakePlugin(mock_app, {"enabled": True})
    destination = MagicMock()
    order = []
    destination.deregister_request_handler.side_effect = lambda path: order.append(path)

    assert plugin.manage_request_handler(destination, "/first") == "/first"
    assert plugin.manage_request_handler(destination, "/second") == "/second"
    plugin.cleanup_managed_resources()
    plugin.cleanup_managed_resources()

    assert order == ["/second", "/first"]


# ── Internet connectivity hooks ──────────────────────────────────


class InternetHookPlugin(PluginBase):
    plugin_name = "internet_hook_test"
    plugin_version = "1.0.0"

    def __init__(self, *args, **kwargs):
        self.internet_available_calls = 0
        self.internet_lost_calls = 0
        super().__init__(*args, **kwargs)

    def start(self):
        self._active = True

    def stop(self):
        self._active = False

    def on_internet_available(self):
        self.internet_available_calls += 1

    def on_internet_lost(self):
        self.internet_lost_calls += 1


@pytest.fixture
def real_event_bus_app(mock_app):
    """Mock app with a real EventBus for testing event subscriptions."""
    mock_app.event_bus = EventBus()
    return mock_app


def test_internet_available_default_true(real_event_bus_app):
    plugin = InternetHookPlugin(real_event_bus_app, {"enabled": True})
    assert plugin.internet_available is True


def test_internet_available_set_on_offline_event(real_event_bus_app):
    plugin = InternetHookPlugin(real_event_bus_app, {"enabled": True})
    plugin.mark_starting()
    plugin.start()
    real_event_bus_app.event_bus.publish(events.INTERNET_OFFLINE, {})
    assert plugin.internet_available is False


def test_internet_available_set_on_online_event(real_event_bus_app):
    plugin = InternetHookPlugin(real_event_bus_app, {"enabled": True})
    plugin.mark_starting()
    plugin.start()
    plugin._internet_available = False
    real_event_bus_app.event_bus.publish(events.INTERNET_ONLINE, {})
    assert plugin.internet_available is True


def test_on_internet_available_called(real_event_bus_app):
    plugin = InternetHookPlugin(real_event_bus_app, {"enabled": True})
    plugin.mark_starting()
    plugin.start()
    plugin._internet_available = False
    real_event_bus_app.event_bus.publish(events.INTERNET_ONLINE, {})
    assert plugin.internet_available_calls == 1


def test_on_internet_lost_called(real_event_bus_app):
    plugin = InternetHookPlugin(real_event_bus_app, {"enabled": True})
    plugin.mark_starting()
    plugin.start()
    real_event_bus_app.event_bus.publish(events.INTERNET_OFFLINE, {})
    assert plugin.internet_lost_calls == 1


def test_hooks_not_called_when_stopped(real_event_bus_app):
    plugin = InternetHookPlugin(real_event_bus_app, {"enabled": True})
    # Don't call start() — _active remains False
    real_event_bus_app.event_bus.publish(events.INTERNET_OFFLINE, {})
    assert plugin.internet_lost_calls == 0
    assert plugin.internet_available is True


def test_no_duplicate_hook_calls(real_event_bus_app):
    plugin = InternetHookPlugin(real_event_bus_app, {"enabled": True})
    plugin.mark_starting()
    plugin.start()
    real_event_bus_app.event_bus.publish(events.INTERNET_OFFLINE, {})
    real_event_bus_app.event_bus.publish(events.INTERNET_OFFLINE, {})
    assert plugin.internet_lost_calls == 1


def test_cleanup_unsubscribes(real_event_bus_app):
    plugin = InternetHookPlugin(real_event_bus_app, {"enabled": True})
    plugin.mark_starting()
    plugin.start()
    plugin._join_threads()
    plugin._internet_available = True
    real_event_bus_app.event_bus.publish(events.INTERNET_OFFLINE, {})
    assert plugin.internet_available is True


def test_resolve_ready_plugin_supports_owner_host_legacy_and_missing_apis():
    class ReadyOwner:
        def get_ready_plugin(self, name):
            return ("owner", name)

    class ReadyHost:
        def get_ready_plugin(self, name):
            return ("host", name)

    class LegacyHost:
        def get_plugin(self, name):
            return ("legacy", name)

    assert resolve_ready_plugin(ReadyOwner(), "dep") == ("owner", "dep")
    assert resolve_ready_plugin(SimpleNamespace(app=ReadyHost()), "dep") == ("host", "dep")
    assert resolve_ready_plugin(SimpleNamespace(app=LegacyHost()), "dep") == ("legacy", "dep")
    assert resolve_ready_plugin(SimpleNamespace(app=object()), "dep") is None


def test_plugin_host_without_dependency_api_returns_none(mock_app):
    host = SimpleNamespace(
        reticulum=mock_app.reticulum,
        identity=mock_app.identity,
        event_bus=mock_app.event_bus,
        announce_dispatcher=mock_app.announce_dispatcher,
    )
    plugin = FakePlugin(host, {})
    assert plugin.get_ready_plugin("missing") is None
    assert plugin.get_migration_targets() == ()


def test_hung_plugin_cannot_be_reactivated_or_restarted(mock_app):
    plugin = FakePlugin(mock_app, {})
    plugin.mark_hung("timed out")

    plugin._active = True
    assert plugin._active is False
    with pytest.raises(RuntimeError, match="hung and cannot be restarted"):
        plugin.mark_starting()


def test_internet_hook_exceptions_are_isolated(mock_app, caplog):
    class RaisingHooks(FakePlugin):
        def on_internet_available(self):
            raise RuntimeError("online hook failed")

        def on_internet_lost(self):
            raise RuntimeError("offline hook failed")

    plugin = RaisingHooks(mock_app, {})
    plugin._active = True
    plugin._internet_available = False
    with caplog.at_level(logging.ERROR):
        plugin._on_internet_event(events.INTERNET_ONLINE, {})
        plugin._on_internet_event(events.INTERNET_OFFLINE, {})

    assert "Error in on_internet_available" in caplog.text
    assert "Error in on_internet_lost" in caplog.text


def test_lifecycle_failure_block_degrade_hung_and_stop_transitions(mock_app):
    plugin = FakePlugin(mock_app, {})
    plugin.mark_starting()
    plugin.mark_start_failed("start failed")
    assert plugin.get_lifecycle_status() == {
        "api": 1,
        "state": "failed",
        "health": "unavailable",
        "reason": "start failed",
    }
    plugin.mark_ready()
    assert plugin.plugin_state == PluginState.FAILED

    # Terminal failure cannot be overwritten by a later dependency callback.
    plugin.mark_blocked("late dependency callback")
    assert plugin.plugin_state == PluginState.FAILED

    blocked = FakePlugin(mock_app, {})
    blocked.mark_blocked("dependency unavailable")
    assert blocked.plugin_state == PluginState.BLOCKED
    assert blocked.wait_until_ready(timeout=0) is False

    ready = FakePlugin(mock_app, {})
    ready.mark_starting()
    ready.mark_degraded("radio noisy")
    ready.mark_ready()
    assert ready.plugin_health == PluginHealth.DEGRADED
    assert ready.get_lifecycle_status()["reason"] == "radio noisy"
    ready.mark_ready()
    assert ready.plugin_state == PluginState.READY
    assert ready.plugin_health == PluginHealth.HEALTHY
    assert ready.get_lifecycle_status()["reason"] is None

    ready.mark_hung("worker stuck")
    ready.mark_ready()
    ready.mark_start_failed("late failure")
    ready.mark_stopped()
    assert ready.plugin_state == PluginState.HUNG
    assert ready.plugin_health == PluginHealth.UNAVAILABLE


def test_request_stop_calls_hook_and_preserves_failed_state(mock_app):
    class StopAwarePlugin(FakePlugin):
        def __init__(self, *args, **kwargs):
            self.stop_requests = 0
            self.stop_requested = threading.Event()
            super().__init__(*args, **kwargs)

        def on_stop_requested(self):
            self.stop_requests += 1
            self.stop_requested.set()

    plugin = StopAwarePlugin(mock_app, {})
    plugin.mark_starting()
    plugin.mark_ready()
    plugin.request_stop()
    assert plugin.plugin_state == PluginState.STOPPING
    assert plugin.plugin_health == PluginHealth.UNAVAILABLE
    assert plugin.stop_requested.wait(timeout=1)
    assert plugin.stop_requests == 1

    plugin.mark_start_failed("failed")
    plugin.request_stop()
    assert plugin.plugin_state == PluginState.FAILED
    assert plugin.stop_requests == 1

    plugin.mark_stopped()
    assert plugin.plugin_state == PluginState.FAILED


def test_request_stop_hook_failure_is_isolated(mock_app, caplog):
    class BrokenStopHook(FakePlugin):
        hook_finished = threading.Event()

        def on_stop_requested(self):
            try:
                raise RuntimeError("cannot interrupt")
            finally:
                self.hook_finished.set()

    plugin = BrokenStopHook(mock_app, {})
    with caplog.at_level(logging.ERROR):
        plugin.request_stop()
        assert plugin.hook_finished.wait(timeout=1)
        deadline = time.monotonic() + 1
        while "Error in on_stop_requested" not in caplog.text and time.monotonic() < deadline:
            time.sleep(0.001)
    assert plugin.plugin_state == PluginState.STOPPING
    assert plugin._active is False
    assert "Error in on_stop_requested" in caplog.text


def test_hung_stop_request_hook_cannot_block_lifecycle_caller(mock_app):
    class HungStopHook(FakePlugin):
        hook_entered = threading.Event()
        release_hook = threading.Event()

        def on_stop_requested(self):
            self.hook_entered.set()
            self.release_hook.wait()

    plugin = HungStopHook(mock_app, {})
    plugin.mark_starting()
    started = time.monotonic()

    plugin.request_stop()

    assert time.monotonic() - started < 0.1
    assert plugin.hook_entered.wait(timeout=1)
    assert plugin.plugin_state == PluginState.STOPPING
    assert plugin._active is False
    plugin.release_hook.set()


def test_cleanup_is_reverse_order_idempotent_and_isolates_failures(mock_app, caplog):
    plugin = FakePlugin(mock_app, {})
    order = []

    def cleanup(label, *, fail=False):
        order.append(label)
        if fail:
            raise RuntimeError(f"{label} failed")

    assert plugin.register_cleanup(cleanup, "first") is cleanup
    plugin.register_cleanup(cleanup, "broken", fail=True)
    plugin.register_cleanup(cleanup, "last")
    with caplog.at_level(logging.ERROR):
        plugin.cleanup_managed_resources()
        plugin.cleanup_managed_resources()

    assert order == ["last", "broken", "first"]
    assert "Managed resource cleanup failed" in caplog.text
    assert plugin.get_lifecycle_metrics()["cleanup_failures_total"] == 1
    with pytest.raises(RuntimeError, match="after cleanup completed"):
        plugin.register_cleanup(cleanup, "too late")


def test_hung_cleanup_is_abandoned_so_earlier_resources_still_clean(mock_app, caplog):
    plugin = FakePlugin(mock_app, {})
    plugin.MANAGED_CLEANUP_CALLBACK_TIMEOUT = 0.02
    plugin.MANAGED_CLEANUP_TOTAL_TIMEOUT = 0.1
    earlier_cleaned = threading.Event()
    hung_entered = threading.Event()
    release_hung = threading.Event()

    def hung_cleanup():
        hung_entered.set()
        release_hung.wait(timeout=2)

    plugin.register_cleanup(earlier_cleaned.set)
    plugin.register_cleanup(hung_cleanup)
    started = time.monotonic()

    with caplog.at_level(logging.WARNING):
        plugin.cleanup_managed_resources()

    assert time.monotonic() - started < 0.2
    assert hung_entered.is_set()
    assert earlier_cleaned.wait(timeout=0.2)
    assert plugin.get_lifecycle_metrics()["cleanup_failures_total"] == 1
    assert "was abandoned" in caplog.text
    release_hung.set()


def test_managed_subscription_supports_handles_and_legacy_values(mock_app):
    bus = EventBus()
    mock_app.event_bus = bus
    plugin = FakePlugin(mock_app, {})
    received = []
    handle = bus.subscribe("managed", lambda event, _data: received.append(event))

    assert plugin.manage_subscription(handle) is handle
    legacy_value = object()
    assert plugin.manage_subscription(legacy_value) is legacy_value
    bus.publish("managed", {})
    plugin.cleanup_managed_resources()
    bus.publish("managed", {})
    bus.shutdown()

    assert received == ["managed"]
    assert handle.cancel() is False


def test_managed_links_prefer_teardown_then_fall_back_to_close(mock_app):
    plugin = FakePlugin(mock_app, {})
    with_teardown = MagicMock()
    with_close_only = SimpleNamespace(close=MagicMock())
    unmanaged = object()

    assert plugin.manage_link(with_teardown) is with_teardown
    assert plugin.manage_link(with_close_only) is with_close_only
    assert plugin.manage_link(unmanaged) is unmanaged
    assert plugin.get_lifecycle_metrics()["rns_resources"]["links"] == 2
    plugin.cleanup_managed_resources()

    assert plugin.get_lifecycle_metrics()["rns_resources"]["links"] == 0

    with_teardown.teardown.assert_called_once_with()
    with_teardown.close.assert_not_called()
    with_close_only.close.assert_called_once_with()


def test_managed_destinations_use_direct_and_transport_deregistration(mock_app, monkeypatch):
    plugin = FakePlugin(mock_app, {})
    direct = MagicMock()
    fallback = object()
    transport = MagicMock()
    monkeypatch.setattr(RNS, "Transport", transport)

    assert plugin.manage_destination(direct) is direct
    assert plugin.manage_destination(fallback) is fallback
    assert plugin.get_lifecycle_metrics()["rns_resources"]["destinations"] == 2
    plugin.cleanup_managed_resources()

    assert plugin.get_lifecycle_metrics()["rns_resources"]["destinations"] == 0

    direct.deregister.assert_called_once_with()
    transport.deregister_destination.assert_called_once_with(fallback)


def test_managed_destination_without_deregister_api_is_best_effort(mock_app, monkeypatch):
    plugin = FakePlugin(mock_app, {})
    monkeypatch.setattr(RNS, "Transport", None)
    destination = object()
    plugin.manage_destination(destination)
    plugin.cleanup_managed_resources()


def test_late_rns_acquisitions_are_immediately_released(mock_app):
    plugin = FakePlugin(mock_app, {})
    plugin.cleanup_managed_resources()

    link = MagicMock()
    destination = MagicMock()
    handler_destination = MagicMock()

    with pytest.raises(RuntimeError, match="after cleanup completed"):
        plugin.manage_link(link)
    with pytest.raises(RuntimeError, match="after cleanup completed"):
        plugin.manage_destination(destination)
    with pytest.raises(RuntimeError, match="after cleanup completed"):
        plugin.manage_request_handler(handler_destination, "/late")

    link.teardown.assert_called_once_with()
    destination.deregister.assert_called_once_with()
    handler_destination.deregister_request_handler.assert_called_once_with("/late")
    assert plugin.get_lifecycle_metrics()["rns_resources"] == {
        "links": 0,
        "destinations": 0,
        "request_handlers": 0,
    }


def test_every_late_managed_resource_is_immediately_released(mock_app):
    plugin = FakePlugin(mock_app, {})
    plugin.cleanup_managed_resources()
    process = MagicMock()
    process.poll.return_value = None
    group = MagicMock()
    executor = MagicMock()
    subscription = MagicMock()

    with pytest.raises(RuntimeError, match="after cleanup completed"):
        plugin.manage_process(process, timeout=0)
    with pytest.raises(RuntimeError, match="after cleanup completed"):
        plugin.manage_process_group(group)
    with pytest.raises(RuntimeError, match="after cleanup completed"):
        plugin.manage_executor(executor)
    with pytest.raises(RuntimeError, match="after cleanup completed"):
        plugin.manage_subscription(subscription)

    process.terminate.assert_called_once_with()
    process.kill.assert_not_called()
    group.stop.assert_called_once_with()
    executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
    subscription.cancel.assert_called_once_with()


def test_hung_late_cleanup_cannot_block_the_caller_or_interpreter(mock_app):
    plugin = FakePlugin(mock_app, {})
    plugin.MANAGED_CLEANUP_CALLBACK_TIMEOUT = 0.02
    plugin.cleanup_managed_resources()
    entered = threading.Event()
    release = threading.Event()

    def hung_cleanup():
        entered.set()
        release.wait(timeout=2)

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="after cleanup completed"):
        plugin.register_cleanup(hung_cleanup)

    assert time.monotonic() - started < 0.2
    assert entered.is_set()
    assert plugin.get_lifecycle_metrics()["cleanup_failures_total"] == 1
    release.set()


def test_managed_process_handles_exited_graceful_kill_and_raced_exit(mock_app):
    plugin = FakePlugin(mock_app, {})
    exited = MagicMock()
    exited.poll.return_value = 0

    graceful = MagicMock()
    graceful.poll.return_value = None

    killed = MagicMock()
    killed.poll.side_effect = [None, None]
    killed.wait.side_effect = [TimeoutError("stuck"), None]

    raced = MagicMock()
    raced.poll.side_effect = [None, 0]
    raced.wait.side_effect = TimeoutError("exited while waiting")

    plugin.manage_process(exited, timeout=0)
    plugin.manage_process(graceful, timeout=0)
    plugin.manage_process(killed, timeout=0)
    plugin.manage_process(raced, timeout=0)
    plugin.cleanup_managed_resources()

    exited.terminate.assert_not_called()
    graceful.terminate.assert_called_once_with()
    graceful.kill.assert_not_called()
    killed.terminate.assert_called_once_with()
    killed.kill.assert_called_once_with()
    assert killed.wait.call_count == 2
    raced.kill.assert_not_called()


def test_managed_process_group_executor_task_and_handler_validate_protocols(mock_app):
    plugin = FakePlugin(mock_app, {})
    with pytest.raises(TypeError, match="process group must provide stop"):
        plugin.manage_process_group(object())
    with pytest.raises(TypeError, match="executor must provide shutdown"):
        plugin.manage_executor(object())
    with pytest.raises(TypeError, match="async task must provide cancel"):
        plugin.manage_async_task(object())
    with pytest.raises(TypeError, match="must provide deregister_request_handler"):
        plugin.manage_request_handler(object(), "/path")


def test_managed_executor_supports_legacy_shutdown_signature(mock_app):
    plugin = FakePlugin(mock_app, {})

    class LegacyExecutor:
        def __init__(self):
            self.calls = []

        def shutdown(self, *, wait):
            self.calls.append(wait)

    executor = LegacyExecutor()
    plugin.manage_executor(executor)
    plugin.cleanup_managed_resources()
    assert executor.calls == [False]


def test_managed_async_task_skips_done_and_closed_loop_tasks(mock_app):
    plugin = FakePlugin(mock_app, {})
    done_task = MagicMock()
    done_task.done.return_value = True

    closed_task = MagicMock()
    closed_task.done.return_value = False
    loop = MagicMock()
    loop.is_running.return_value = False
    loop.is_closed.return_value = True
    closed_task.get_loop.return_value = loop

    plugin.manage_async_task(done_task)
    plugin.manage_async_task(closed_task)
    with patch("asyncio.get_running_loop", side_effect=RuntimeError):
        plugin.cleanup_managed_resources()

    done_task.cancel.assert_not_called()
    closed_task.cancel.assert_not_called()


def test_managed_async_task_without_loop_uses_legacy_cancel(mock_app):
    plugin = FakePlugin(mock_app, {})

    class LegacyTask:
        def __init__(self):
            self.cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

    task = LegacyTask()
    plugin.manage_async_task(task)
    plugin.cleanup_managed_resources()
    assert task.cancelled is True


def test_managed_async_task_cancels_safely_on_current_event_loop(mock_app):
    plugin = FakePlugin(mock_app, {})

    async def exercise():
        blocker = asyncio.Event()
        task = asyncio.create_task(blocker.wait())
        plugin.manage_async_task(task)
        plugin.cleanup_managed_resources()
        with pytest.raises(asyncio.CancelledError):
            await task
        return task.cancelled()

    assert asyncio.run(exercise()) is True


def test_broadcast_snapshot_uses_optional_snapshot_provider(mock_app):
    plugin = FakePlugin(mock_app, {})
    assert plugin.broadcast_snapshot() is None

    class SnapshotPlugin(FakePlugin):
        def get_snapshot(self):
            return {"value": 42}

    assert SnapshotPlugin(mock_app, {}).broadcast_snapshot(cycle_count=3) == {"value": 42}


def test_join_threads_reports_deadline_and_alive_worker(mock_app, caplog):
    plugin = FakePlugin(mock_app, {})
    deadline_worker = MagicMock(name="deadline-worker")
    deadline_worker.is_alive.return_value = True
    plugin._threads = [deadline_worker]
    with caplog.at_level(logging.WARNING):
        plugin._join_threads(timeout=0)
    deadline_worker.join.assert_not_called()
    assert "Thread join deadline reached" in caplog.text

    caplog.clear()
    alive_worker = MagicMock(name="alive-worker")
    alive_worker.name = "alive-worker"
    alive_worker.is_alive.return_value = True
    plugin._threads = [alive_worker]
    with caplog.at_level(logging.WARNING):
        plugin._join_threads(timeout=0.1)
    alive_worker.join.assert_called_once()
    assert "did not exit in time" in caplog.text


def test_jittered_sleep_uses_bounded_randomized_timeout(mock_app):
    plugin = FakePlugin(mock_app, {})
    plugin._stop_event = MagicMock()
    with patch("random.random", return_value=1.0):
        plugin._jittered_sleep(10, jitter_pct=0.2)
    plugin._stop_event.wait.assert_called_once_with(timeout=12.0)


def test_log_readers_drain_stdout_stderr_and_tolerate_closed_stream(mock_app, caplog):
    plugin = FakePlugin(mock_app, {})
    stdout_process = SimpleNamespace(stdout=io.BytesIO(b"first\n\n\xffsecond\n"))
    stderr_process = SimpleNamespace(stderr=io.BytesIO(b"error line\n"))
    no_stream = SimpleNamespace(stdout=None)

    class ClosedStream:
        def __iter__(self):
            raise OSError("closed")

    closed_stream = SimpleNamespace(stdout=ClosedStream())
    with caplog.at_level(logging.INFO):
        threads = [
            plugin._start_log_reader(stdout_process, prefix="out"),
            plugin._start_stderr_reader(stderr_process, prefix="err"),
            plugin._start_log_reader(no_stream),
            plugin._start_log_reader(closed_stream),
        ]
        for thread in threads:
            thread.join(timeout=2)

    assert "[out] first" in caplog.text
    assert "second" in caplog.text
    assert "[err] error line" in caplog.text
    assert PluginBase.get_thread_count() == 0


def test_failed_thread_start_rolls_back_tracking_and_budget(mock_app, monkeypatch):
    plugin = FakePlugin(mock_app, {})

    def fail_start(_thread):
        raise RuntimeError("injected thread start failure")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="injected thread start failure"):
        plugin._start_thread(lambda: None, "will-not-start")

    assert plugin._threads == []
    assert PluginBase.get_thread_count() == 0
