"""Tests for PluginBase helper methods."""

import logging
import time

import pytest

from reticulumpi.plugin_base import PluginBase


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
    assert plugin.get_status() == {"active": False}
    plugin._active = True
    assert plugin.get_status() == {"active": True}


def test_logger_uses_plugin_name(mock_app):
    plugin = FakePlugin(mock_app, {"enabled": True})
    assert plugin.log.name == "reticulumpi.plugin.fake"


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
    thread = plugin._start_thread(lambda: None, "tracked")
    thread.join(timeout=2)
    assert thread in plugin._threads


def test_join_threads(mock_app):
    plugin = FakePlugin(mock_app, {"enabled": True})
    plugin._start_thread(lambda: None, "t1")
    plugin._start_thread(lambda: None, "t2")
    assert len(plugin._threads) == 2
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

        def start(self): pass
        def stop(self): pass

    ValidatingPlugin(mock_app, {"enabled": True})
    assert ValidatingPlugin.validated is True


def test_plugin_name_collision_last_wins(tmp_path):
    """When two plugins define the same plugin_name, the second one overwrites."""
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
    assert found["dupe"].plugin_version == "2.0.0"


# ── Thread budget tests ───────────────────────────────────────────


def test_start_thread_increments_global_count(mock_app):
    plugin = FakePlugin(mock_app, {"enabled": True})
    assert PluginBase.get_thread_count() == 0
    t = plugin._start_thread(lambda: None, "budget-t1")
    t.join(timeout=2)
    assert PluginBase.get_thread_count() == 1


def test_join_threads_decrements_global_count(mock_app):
    plugin = FakePlugin(mock_app, {"enabled": True})
    t1 = plugin._start_thread(lambda: None, "budget-t1")
    t2 = plugin._start_thread(lambda: None, "budget-t2")
    t1.join(timeout=2)
    t2.join(timeout=2)
    assert PluginBase.get_thread_count() == 2
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
