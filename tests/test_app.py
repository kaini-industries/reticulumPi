"""Tests for the ReticulumPiApp orchestrator."""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi import events
from reticulumpi.app import ReticulumPiApp, run_db_migrations
from reticulumpi.migrations import Migration, MigrationTarget
from reticulumpi.plugin_base import PluginBase, PluginState


@pytest.fixture
def app_with_config(tmp_path):
    """Create an app instance with a minimal config (no plugins enabled)."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "reticulumpi:\n  log_level: 4\n  identity_path: {identity}\n  plugins: {{}}\n".format(
            identity=str(tmp_path / "identity")
        )
    )
    return ReticulumPiApp(config_path=str(config_file))


def test_constructor_defaults():
    app = ReticulumPiApp()
    assert app.config is not None
    assert app.reticulum is None
    assert app.identity is None
    assert app.plugins == {}


def test_constructor_log_level_override(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("reticulumpi:\n  log_level: 4\n")
    app = ReticulumPiApp(config_path=str(config_file), log_level_override=7)
    assert app._log_level == 7


def test_constructor_uses_config_log_level(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("reticulumpi:\n  log_level: 2\n")
    app = ReticulumPiApp(config_path=str(config_file))
    assert app._log_level == 2


def test_get_plugin_returns_none_for_missing():
    app = ReticulumPiApp()
    assert app.get_plugin("nonexistent") is None


def test_get_plugin_returns_plugin():
    app = ReticulumPiApp()
    mock_plugin = MagicMock()
    app.plugins["test"] = mock_plugin
    assert app.get_plugin("test") is mock_plugin


def test_get_status_with_no_plugins():
    app = ReticulumPiApp()
    status = app.get_status()
    assert "version" in status
    assert status["plugins"] == {}


def test_get_status_collects_from_plugins():
    app = ReticulumPiApp()
    plugin_a = MagicMock()
    plugin_a.get_status.return_value = {"active": True}
    plugin_b = MagicMock()
    plugin_b.get_status.side_effect = RuntimeError("broken")
    app.plugins["a"] = plugin_a
    app.plugins["b"] = plugin_b
    status = app.get_status()
    assert status["plugins"]["a"] == {"active": True}
    assert status["plugins"]["b"] == {"error": "status collection failed"}


def test_operational_metrics_are_aggregate_and_secret_free():
    import json

    class MetricsPlugin(PluginBase):
        plugin_name = "sensitive_plugin_name"

        def start(self):
            self._active = True

        def stop(self):
            self._active = False

    class Process:
        @staticmethod
        def poll():
            return None

    class Group:
        running = True
        restart_count = 3
        processes = (Process(), Process())

    app = ReticulumPiApp()
    plugin = MetricsPlugin(app, {"secret": "do-not-export"})
    plugin.mark_starting()
    plugin.mark_ready()
    plugin.manage_link(MagicMock())
    plugin._process_group = Group()
    app.plugins["private-name"] = plugin
    app.sdr_scheduler.register(
        "private-serial",
        "private-caller",
        2,
        MagicMock(),
        MagicMock(),
        continuous=True,
    )
    slot = app.sdr_scheduler._dongles["private-serial"].slots["private-caller"]
    slot.is_active = True
    slot.device_lease = object()

    metrics = app.get_status()["operational_metrics"]

    assert metrics["lifecycle"]["states"]["ready"] == 1
    assert metrics["lifecycle"]["health"]["healthy"] == 1
    assert metrics["lifecycle"]["readiness"]["count"] == 1
    assert metrics["lifecycle"]["readiness"]["max_seconds"] >= 0
    assert metrics["lifecycle"]["hung_total"] == 0
    assert metrics["lifecycle"]["cleanup_failures_total"] == 0
    assert metrics["rns_resources"]["links"] == 1
    assert metrics["threads"]["live"] >= 0
    assert metrics["threads"]["runtime_live"] >= 1
    assert 0 <= metrics["threads"]["runtime_daemon"] <= metrics["threads"]["runtime_live"]
    assert metrics["callbacks"]["dropped_total"] == 0
    assert metrics["workers"]["hung_or_abandoned_total"] >= 0
    assert metrics["processes"] == {
        "managed_groups": 1,
        "managed_processes": 2,
        "raw_processes": 0,
        "total_live": 2,
        "restarts": 3,
        "restarts_total": metrics["processes"]["restarts_total"],
    }
    assert metrics["processes"]["restarts_total"] >= 0
    assert metrics["sdr"]["active_leases"] == 1
    assert metrics["sdr"]["canonical_claims"] >= 0
    assert "pending" in metrics["event_bus"]
    assert "pending" in metrics["announce_dispatcher"]
    assert all(isinstance(value, int) for value in metrics["migrations"].values())
    assert metrics["sqlite"]["failures"] >= metrics["sqlite"]["migration_failures"]
    assert metrics["sqlite"]["migration_failures"] == metrics["migrations"]["sqlite_failures"]
    assert "close_reasons" in metrics["dashboard"]["websocket"]
    assert metrics["dashboard"]["auth_admission"]["capacity"] == 4
    assert metrics["dashboard"]["service_worker"]["version"]
    encoded = json.dumps(metrics)
    assert "private-serial" not in encoded
    assert "private-caller" not in encoded
    assert "sensitive_plugin_name" not in encoded
    assert "do-not-export" not in encoded


def test_required_plugin_readiness_gate(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "reticulumpi:\n"
        "  plugins:\n"
        "    dashboard:\n"
        "      enabled: true\n"
        "      required: true\n"
        "    optional:\n"
        "      enabled: true\n"
    )
    app = ReticulumPiApp(config_path=str(config_file))
    app._plugin_state_history["dashboard"] = (PluginState.BLOCKED, "dependency failed")
    app._plugin_state_history["optional"] = (PluginState.FAILED, "optional failure")

    assert app._required_plugin_failures() == ["dashboard is blocked"]


def test_shutdown_stops_plugins_in_reverse():
    app = ReticulumPiApp()
    call_order = []
    plugin_a = MagicMock()
    plugin_a.stop.side_effect = lambda: call_order.append("a")
    plugin_b = MagicMock()
    plugin_b.stop.side_effect = lambda: call_order.append("b")
    app.plugins["a"] = plugin_a
    app.plugins["b"] = plugin_b
    app.shutdown()
    assert call_order == ["b", "a"]


def test_shutdown_continues_on_plugin_error():
    app = ReticulumPiApp()
    plugin_a = MagicMock()
    plugin_a.stop.side_effect = RuntimeError("boom")
    plugin_b = MagicMock()
    app.plugins["a"] = plugin_a
    app.plugins["b"] = plugin_b
    app.shutdown()  # Should not raise
    plugin_b.stop.assert_called_once()


def test_pre_stop_requests_managed_group_before_raw_process_signal():
    app = ReticulumPiApp()
    plugin = MagicMock()
    plugin._process_group.request_stop.return_value = True
    app.plugins["decoder"] = plugin
    with patch("reticulumpi.app.os.kill") as raw_kill:
        app._pre_stop_signal_subprocesses()
    plugin._process_group.request_stop.assert_called_once()
    raw_kill.assert_not_called()


@patch("RNS.Reticulum")
@patch("reticulumpi.identity_manager.load_or_create")
def test_start_initializes_reticulum(mock_identity, mock_rns, tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "reticulumpi:\n  log_level: 4\n  identity_path: {identity}\n  plugins: {{}}\n".format(
            identity=str(tmp_path / "identity")
        )
    )
    mock_id = MagicMock()
    mock_id.hash = b"\x00" * 16
    mock_identity.return_value = mock_id

    app = ReticulumPiApp(config_path=str(config_file))
    # Run start in a way that won't block on _shutdown_event.wait()
    app._shutdown_event.set()
    with patch.object(app, "shutdown"):
        app.start()

    mock_rns.assert_called_once()
    mock_identity.assert_called_once()
    assert app.reticulum is not None
    assert app.identity is mock_id


@patch("RNS.Reticulum")
@patch("reticulumpi.identity_manager.load_or_create")
def test_start_loads_and_starts_plugins(mock_identity, mock_rns, tmp_path, plugin_dir):
    config_file = tmp_path / "cfg.yaml"
    config_file.write_text(
        "reticulumpi:\n"
        "  log_level: 4\n"
        "  identity_path: {identity}\n"
        "  plugin_paths:\n"
        "    - {plugin_dir}\n"
        "  plugins:\n"
        "    sample:\n"
        "      enabled: true\n".format(
            identity=str(tmp_path / "identity"),
            plugin_dir=plugin_dir,
        )
    )
    mock_id = MagicMock()
    mock_id.hash = b"\x00" * 16
    mock_identity.return_value = mock_id

    app = ReticulumPiApp(config_path=str(config_file))
    app._shutdown_event.set()
    with patch.object(app, "shutdown"):
        app.start()

    assert "sample" in app.plugins
    assert app.plugins["sample"]._active is True


def test_get_version():
    from reticulumpi import __version__

    app = ReticulumPiApp()
    assert app._get_version() == __version__


def test_plugin_declared_migrations_run_before_start(app_with_config, tmp_path):
    order = []
    target = MigrationTarget(
        "fixture",
        tmp_path / "fixture.db",
        (Migration(1, "schema", ("CREATE TABLE fixture(id INTEGER PRIMARY KEY)",)),),
    )

    class MigratingPlugin(PluginBase):
        plugin_name = "migrating"

        def get_migration_targets(self):
            return (target,)

        def start(self):
            from contextlib import closing
            import sqlite3

            with closing(sqlite3.connect(target.path)) as connection:
                order.append(connection.execute("PRAGMA user_version").fetchone()[0])
            self._active = True

        def stop(self):
            self._active = False

    plugin = MigratingPlugin(app_with_config, {})
    app_with_config._migrate_plugin("migrating", plugin)
    app_with_config._start_plugin_with_timeout("migrating", plugin, 1)
    assert order == [1]


def test_failed_plugin_tracked_when_not_found(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "reticulumpi:\n  log_level: 4\n  plugins:\n    nonexistent:\n      enabled: true\n"
    )
    app = ReticulumPiApp(config_path=str(config_file))
    app._load_plugins()
    assert len(app._failed_plugins) == 1
    assert app._failed_plugins[0][0] == "nonexistent"
    assert "not found" in app._failed_plugins[0][1]


@patch("RNS.Reticulum")
@patch("reticulumpi.identity_manager.load_or_create")
def test_failed_plugin_tracked_on_start_error(mock_identity, mock_rns, tmp_path):
    """Plugin that raises in start() is tracked as failed and removed."""
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "bad_start.py").write_text(
        "from reticulumpi.plugin_base import PluginBase\n"
        "class BadStart(PluginBase):\n"
        "    plugin_name = 'bad_start'\n"
        "    plugin_version = '1.0.0'\n"
        "    def start(self): raise RuntimeError('boom')\n"
        "    def stop(self): pass\n"
    )
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "reticulumpi:\n"
        "  log_level: 4\n"
        "  identity_path: {identity}\n"
        "  plugin_paths:\n"
        "    - {pdir}\n"
        "  plugins:\n"
        "    bad_start:\n"
        "      enabled: true\n".format(
            identity=str(tmp_path / "identity"),
            pdir=str(plugin_dir),
        )
    )
    mock_id = MagicMock()
    mock_id.hash = b"\x00" * 16
    mock_identity.return_value = mock_id

    app = ReticulumPiApp(config_path=str(config_file))
    app._shutdown_event.set()
    app.start()

    assert "bad_start" not in app.plugins
    assert any(name == "bad_start" for name, _ in app._failed_plugins)


def test_get_status_includes_failed_plugins():
    app = ReticulumPiApp()
    app._failed_plugins.append(("broken", "not found in plugin directories"))
    status = app.get_status()
    assert len(status["failed_plugins"]) == 1
    assert status["failed_plugins"][0]["name"] == "broken"


@patch("RNS.Reticulum")
@patch("RNS.Transport")
@patch("reticulumpi.identity_manager.load_or_create")
def test_startup_report_logs_version(mock_identity, mock_transport, mock_rns, tmp_path, caplog):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "reticulumpi:\n  log_level: 4\n  identity_path: {identity}\n  plugins: {{}}\n".format(
            identity=str(tmp_path / "identity")
        )
    )
    mock_id = MagicMock()
    mock_id.hash = b"\x00" * 16
    mock_identity.return_value = mock_id
    mock_transport.interfaces = []

    app = ReticulumPiApp(config_path=str(config_file))
    app._shutdown_event.set()
    import logging

    with caplog.at_level(logging.INFO):
        app.start()
    assert any("ReticulumPi v" in msg for msg in caplog.messages)


@patch("RNS.Reticulum")
@patch("RNS.Transport")
@patch("reticulumpi.identity_manager.load_or_create")
def test_startup_report_logs_plugins(
    mock_identity, mock_transport, mock_rns, tmp_path, caplog, plugin_dir
):
    config_file = tmp_path / "cfg.yaml"
    config_file.write_text(
        "reticulumpi:\n"
        "  log_level: 4\n"
        "  identity_path: {identity}\n"
        "  plugin_paths:\n"
        "    - {plugin_dir}\n"
        "  plugins:\n"
        "    sample:\n"
        "      enabled: true\n".format(
            identity=str(tmp_path / "identity"),
            plugin_dir=plugin_dir,
        )
    )
    mock_id = MagicMock()
    mock_id.hash = b"\x00" * 16
    mock_identity.return_value = mock_id
    mock_transport.interfaces = []

    app = ReticulumPiApp(config_path=str(config_file))
    app._shutdown_event.set()
    import logging

    with caplog.at_level(logging.INFO):
        app.start()
    assert any("sample" in msg and "Plugin" in msg for msg in caplog.messages)


def test_startup_report_warns_on_failed_plugins(caplog):
    app = ReticulumPiApp()
    app._failed_plugins.append(("broken", "not found"))
    app.identity = MagicMock()
    app.identity.hash = b"\x00" * 16
    import logging

    with caplog.at_level(logging.ERROR), patch("RNS.Transport") as mock_transport:
        mock_transport.interfaces = []
        app._print_startup_report()
    assert any("FAILED" in msg or "broken" in msg for msg in caplog.messages)


def test_check_returns_true_valid(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("reticulumpi:\n  log_level: 4\n  plugins: {}\n")
    app = ReticulumPiApp(config_path=str(config_file))
    assert app.check() is True


def test_check_returns_false_missing_plugin(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "reticulumpi:\n  log_level: 4\n  plugins:\n    nonexistent:\n      enabled: true\n"
    )
    app = ReticulumPiApp(config_path=str(config_file))
    assert app.check() is False


def test_list_plugins_prints_discovered(tmp_path, plugin_dir, capsys):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "reticulumpi:\n  log_level: 4\n  plugin_paths:\n    - {pdir}\n  plugins: {{}}\n".format(
            pdir=plugin_dir
        )
    )
    app = ReticulumPiApp(config_path=str(config_file))
    app.list_plugins()
    captured = capsys.readouterr()
    assert "sample" in captured.out
    assert "0.1.0" in captured.out


# ---------------------------------------------------------------------------
# Off-grid mode
# ---------------------------------------------------------------------------


class TestSetOffgridMode:
    def test_returns_persisted_true_on_success(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("reticulumpi:\n  log_level: 4\n")
        app = ReticulumPiApp(config_path=str(config_file))
        app.internet_probe = MagicMock()
        app.internet_probe.force_offline = False
        result = app.set_offgrid_mode(True)
        assert result == {
            "enabled": True,
            "applied": True,
            "persisted": True,
            "reason": "persisted",
        }
        app.internet_probe.set_force_offline.assert_called_once_with(True)

    def test_returns_persisted_false_on_oserror(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("reticulumpi:\n  log_level: 4\n")
        app = ReticulumPiApp(config_path=str(config_file))
        app.internet_probe = MagicMock()
        app.internet_probe.force_offline = False
        with patch.object(app.config, "set_internet_force_offline", return_value=False):
            result = app.set_offgrid_mode(True)
        assert result["enabled"] is True
        assert result["applied"] is True
        assert result["persisted"] is False

    def test_directory_durability_failure_is_structured(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("reticulumpi:\n  log_level: 4\n")
        app = ReticulumPiApp(config_path=str(config_file))

        with patch.object(
            app.config,
            "_atomic_write_yaml",
            side_effect=OSError("directory fsync unavailable"),
        ):
            result = app.set_offgrid_mode(True)

        assert result == {
            "enabled": True,
            "applied": True,
            "persisted": False,
            "reason": "write_failed",
        }

    def test_no_change_returns_early(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("reticulumpi:\n  log_level: 4\n")
        app = ReticulumPiApp(config_path=str(config_file))
        app.internet_probe = MagicMock()
        app.internet_probe.force_offline = True
        result = app.set_offgrid_mode(True)
        assert result == {
            "enabled": True,
            "applied": False,
            "persisted": True,
            "reason": "unchanged",
        }
        app.internet_probe.set_force_offline.assert_not_called()

    def test_probe_updated_before_event_published(self, tmp_path):
        """internet_probe.set_force_offline is called BEFORE event_bus.publish."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("reticulumpi:\n  log_level: 4\n")
        app = ReticulumPiApp(config_path=str(config_file))
        app.internet_probe = MagicMock()
        app.internet_probe.force_offline = False
        call_order = []
        app.internet_probe.set_force_offline.side_effect = lambda v: call_order.append("probe")
        original_publish = app.event_bus.publish
        app.event_bus.publish = lambda evt, data: (
            call_order.append("event"),
            original_publish(evt, data),
        )
        app.set_offgrid_mode(True)
        assert call_order == ["probe", "event"]


# ---------------------------------------------------------------------------
# Hot-reload: enable_plugin / disable_plugin at runtime
# ---------------------------------------------------------------------------


def _make_hot_reload_app(tmp_path, plugin_dir_path, plugins_yaml="{}"):
    """Build a ReticulumPiApp with *plugin_dir_path* on its search path.

    *plugins_yaml* is inlined into the ``plugins:`` mapping so tests can
    seed per-plugin config without touching AppConfig internals.
    """
    config_file = tmp_path / "hot_reload_cfg.yaml"
    config_file.write_text(
        "reticulumpi:\n"
        "  log_level: 4\n"
        "  identity_path: {identity}\n"
        "  plugin_paths:\n"
        "    - {pdir}\n"
        "  plugins: {plugins}\n".format(
            identity=str(tmp_path / "identity"),
            pdir=plugin_dir_path,
            plugins=plugins_yaml,
        )
    )
    return ReticulumPiApp(config_path=str(config_file))


class TestHotReload:
    def test_enable_plugin_hot_loads_and_starts(self, tmp_path, plugin_dir):
        app = _make_hot_reload_app(tmp_path, plugin_dir)
        app.enable_plugin("sample")
        assert "sample" in app.plugins
        assert app.plugins["sample"]._active is True

    def test_enable_plugin_publishes_plugin_started_event(self, tmp_path, plugin_dir):
        from reticulumpi import events

        app = _make_hot_reload_app(tmp_path, plugin_dir)
        received: list[dict] = []
        app.event_bus.subscribe(events.PLUGIN_STARTED, lambda _evt, data: received.append(data))
        app.enable_plugin("sample")
        assert received == [{"name": "sample"}]

    def test_enable_plugin_raises_runtime_error_if_already_running(self, tmp_path, plugin_dir):
        app = _make_hot_reload_app(tmp_path, plugin_dir)
        app.enable_plugin("sample")
        with pytest.raises(RuntimeError, match="already running"):
            app.enable_plugin("sample")

    def test_enable_plugin_raises_key_error_if_not_discoverable(self, tmp_path, plugin_dir):
        app = _make_hot_reload_app(tmp_path, plugin_dir)
        with pytest.raises(KeyError, match="not found"):
            app.enable_plugin("no_such_plugin")

    def test_enable_plugin_calls_stop_if_start_fails(self, tmp_path):
        flag = tmp_path / "stop_called"
        (tmp_path / "bad_start_plugin.py").write_text(
            "from reticulumpi.plugin_base import PluginBase\n"
            "class BadStart(PluginBase):\n"
            "    plugin_name = 'bad_start_hr'\n"
            "    plugin_version = '0.1.0'\n"
            "    def start(self):\n"
            "        raise RuntimeError('boom')\n"
            "    def stop(self):\n"
            f"        with open({str(flag)!r}, 'w') as stream:\n"
            "            stream.write('1')\n"
        )
        app = _make_hot_reload_app(tmp_path, str(tmp_path))
        with pytest.raises(RuntimeError, match="boom"):
            app.enable_plugin("bad_start_hr")
        assert "bad_start_hr" not in app.plugins
        assert flag.exists(), "stop() should be called for cleanup on failed start"

    def test_enable_plugin_uses_config_from_yaml(self, tmp_path, plugin_dir):
        plugins_yaml = "\n    sample:\n      greeting: hi"
        app = _make_hot_reload_app(tmp_path, plugin_dir, plugins_yaml=plugins_yaml)
        app.enable_plugin("sample")
        assert app.plugins["sample"].config.get("greeting") == "hi"

    def test_disable_plugin_stops_and_removes(self, tmp_path, plugin_dir):
        app = _make_hot_reload_app(tmp_path, plugin_dir)
        app.enable_plugin("sample")
        plugin = app.plugins["sample"]
        app.disable_plugin("sample")
        assert "sample" not in app.plugins
        assert plugin._active is False

    def test_disable_retains_readiness_and_cleanup_failure_metrics(self, tmp_path, plugin_dir):
        app = _make_hot_reload_app(tmp_path, plugin_dir)
        app.enable_plugin("sample")
        plugin = app.plugins["sample"]

        def broken_cleanup():
            raise RuntimeError("cleanup fixture")

        plugin.register_cleanup(broken_cleanup)
        app.disable_plugin("sample")

        metrics = app._get_operational_metrics()["lifecycle"]
        assert metrics["readiness"]["count"] == 1
        assert metrics["readiness"]["total_seconds"] >= 0
        assert metrics["cleanup_failures_total"] == 1

        app._archive_plugin_operational_metrics(plugin)
        assert app._get_operational_metrics()["lifecycle"]["cleanup_failures_total"] == 1

    def test_disable_plugin_publishes_plugin_stopped_event(self, tmp_path, plugin_dir):
        from reticulumpi import events

        app = _make_hot_reload_app(tmp_path, plugin_dir)
        app.enable_plugin("sample")
        received: list[dict] = []
        app.event_bus.subscribe(events.PLUGIN_STOPPED, lambda _evt, data: received.append(data))
        app.disable_plugin("sample")
        assert received == [{"name": "sample"}]

    def test_disable_plugin_raises_key_error_if_not_running(self, tmp_path, plugin_dir):
        app = _make_hot_reload_app(tmp_path, plugin_dir)
        with pytest.raises(KeyError, match="not running"):
            app.disable_plugin("sample")

    def test_disable_plugin_keeps_failed_stop_registered(self, tmp_path):
        from reticulumpi import events

        (tmp_path / "angry_plugin.py").write_text(
            "from reticulumpi.plugin_base import PluginBase\n"
            "class Angry(PluginBase):\n"
            "    plugin_name = 'angry_hr'\n"
            "    plugin_version = '0.1.0'\n"
            "    def start(self):\n"
            "        self._active = True\n"
            "    def stop(self):\n"
            "        raise RuntimeError('cannot stop')\n"
        )
        app = _make_hot_reload_app(tmp_path, str(tmp_path))
        app.enable_plugin("angry_hr")
        received: list[dict] = []
        app.event_bus.subscribe(events.PLUGIN_STOPPED, lambda _evt, data: received.append(data))
        with pytest.raises(RuntimeError, match="did not stop cleanly"):
            app.disable_plugin("angry_hr")
        assert "angry_hr" in app.plugins
        assert app.plugins["angry_hr"].plugin_state.value == "failed"
        assert received == []

    def test_concurrent_enable_and_disable_is_safe(self, tmp_path, plugin_dir):
        import threading

        app = _make_hot_reload_app(tmp_path, plugin_dir)
        barrier = threading.Barrier(2)
        errors: list[tuple[str, Exception]] = []
        iterations = 20

        def enable_loop():
            barrier.wait()
            for _ in range(iterations):
                try:
                    app.enable_plugin("sample")
                except (RuntimeError, KeyError):
                    pass  # Expected when already running or raced
                except Exception as exc:  # pragma: no cover - defensive
                    errors.append(("enable", exc))

        def disable_loop():
            barrier.wait()
            for _ in range(iterations):
                try:
                    app.disable_plugin("sample")
                except KeyError:
                    pass  # Expected when not running or raced
                except Exception as exc:  # pragma: no cover - defensive
                    errors.append(("disable", exc))

        t1 = threading.Thread(target=enable_loop)
        t2 = threading.Thread(target=disable_loop)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert not t1.is_alive() and not t2.is_alive(), "Threads deadlocked"
        assert not errors, f"Unexpected errors under concurrency: {errors}"

    def test_lifecycle_v2_hundred_hot_cycles_return_resources_to_baseline(self, tmp_path):
        import os
        import threading
        import time
        from collections import Counter
        from concurrent.futures import ThreadPoolExecutor

        app = _make_hot_reload_app(tmp_path, str(tmp_path))
        resource_lock = threading.Lock()
        active_resources: Counter[str] = Counter()

        def track(kind: str, delta: int) -> None:
            with resource_lock:
                active_resources[kind] += delta

        def resource_snapshot() -> dict[str, int]:
            with resource_lock:
                return {kind: count for kind, count in active_resources.items() if count}

        class TrackedDestination:
            def __init__(self):
                self.handlers: set[str] = set()
                self.active = True
                track("destinations", 1)

            def register_request_handler(self, path, *_args, **_kwargs):
                self.handlers.add(path)
                track("request_handlers", 1)

            def deregister_request_handler(self, path):
                if path in self.handlers:
                    self.handlers.remove(path)
                    track("request_handlers", -1)

            def deregister(self):
                assert not self.handlers, "destination removed before its request handlers"
                if self.active:
                    self.active = False
                    track("destinations", -1)

        class TrackedLink:
            def __init__(self):
                self.active = True
                track("links", 1)

            def teardown(self):
                if self.active:
                    self.active = False
                    track("links", -1)

        class TrackedProcess:
            def __init__(self):
                self.returncode = None
                track("processes", 1)

            def poll(self):
                return self.returncode

            def _finish(self, returncode):
                if self.returncode is None:
                    self.returncode = returncode
                    track("processes", -1)

            def terminate(self):
                self._finish(-15)

            def kill(self):
                self._finish(-9)

            def wait(self, timeout=None):
                del timeout
                return self.returncode

        class TrackedExecutor(ThreadPoolExecutor):
            def __init__(self):
                super().__init__(max_workers=1, thread_name_prefix="lifecycle-stress-executor")
                self.resource_active = True
                track("executors", 1)

            def shutdown(self, *args, **kwargs):
                try:
                    return super().shutdown(*args, **kwargs)
                finally:
                    if self.resource_active:
                        self.resource_active = False
                        track("executors", -1)

        class LifecycleStressPlugin(PluginBase):
            plugin_name = "lifecycle_stress"
            plugin_lifecycle_api = 2

            def start(self):
                self._active = True
                destination = self.manage_destination(TrackedDestination())
                destination.register_request_handler("/stress", lambda *_args: None)
                self.manage_request_handler(destination, "/stress")
                self.manage_link(TrackedLink())
                self.manage_subscription(
                    self.event_bus.subscribe("stress.event", lambda *_args: None)
                )
                announce_id = self.announce_dispatcher.subscribe(
                    None,
                    lambda *_args: None,
                )
                self.register_cleanup(self.announce_dispatcher.unsubscribe, announce_id)

                executor = self.manage_executor(TrackedExecutor())
                executor.submit(lambda: None).result(timeout=1)
                self.manage_process(TrackedProcess(), timeout=0)

                self.descriptors = os.pipe()
                for descriptor in self.descriptors:
                    track("descriptors", 1)

                    def close_descriptor(fd=descriptor):
                        os.close(fd)
                        track("descriptors", -1)

                    self.register_cleanup(close_descriptor)

                thread_started = threading.Event()

                def worker():
                    track("plugin_threads", 1)
                    thread_started.set()
                    try:
                        self._stop_event.wait()
                    finally:
                        track("plugin_threads", -1)

                self._start_thread(worker, "lifecycle-stress-plugin")
                assert thread_started.wait(timeout=1)
                self.mark_ready()

            def stop(self):
                self._join_threads(timeout=1)

        def event_bus_registrations() -> tuple[int, int, int]:
            with app.event_bus._lock:
                return (
                    len(app.event_bus._handles),
                    sum(len(callbacks) for callbacks in app.event_bus._subscribers.values()),
                    len(app.event_bus._offload_map),
                )

        baseline_plugin_threads = PluginBase.get_thread_count()
        baseline_global_threads = threading.active_count()
        baseline_event_bus = event_bus_registrations()
        baseline_announces = app.announce_dispatcher.get_stats()["subscribers"]

        def wait_for_thread_baseline() -> None:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if (
                    PluginBase.get_thread_count() == baseline_plugin_threads
                    and threading.active_count() == baseline_global_threads
                ):
                    return
                time.sleep(0.005)
            assert PluginBase.get_thread_count() == baseline_plugin_threads
            assert threading.active_count() == baseline_global_threads

        with patch.object(
            app._plugin_loader,
            "discover",
            return_value={"lifecycle_stress": LifecycleStressPlugin},
        ):
            for _cycle in range(100):
                app.enable_plugin("lifecycle_stress")
                plugin = app.plugins["lifecycle_stress"]
                assert plugin.plugin_state == PluginState.READY
                assert resource_snapshot() == {
                    "descriptors": 2,
                    "destinations": 1,
                    "executors": 1,
                    "links": 1,
                    "plugin_threads": 1,
                    "processes": 1,
                    "request_handlers": 1,
                }

                app.disable_plugin("lifecycle_stress")

                wait_for_thread_baseline()
                assert "lifecycle_stress" not in app.plugins
                assert plugin._threads == []
                assert plugin._managed_cleanups == []
                assert resource_snapshot() == {}
                assert event_bus_registrations() == baseline_event_bus
                assert app.announce_dispatcher.get_stats()["subscribers"] == baseline_announces
                for descriptor in plugin.descriptors:
                    with pytest.raises(OSError):
                        os.fstat(descriptor)

    def test_disable_plugin_publishes_plugin_stopping_before_pop(
        self,
        tmp_path,
        plugin_dir,
    ):
        from reticulumpi import events

        app = _make_hot_reload_app(tmp_path, plugin_dir)
        app.enable_plugin("sample")
        still_in_registry: list[bool] = []
        app.event_bus.subscribe(
            events.PLUGIN_STOPPING,
            lambda _evt, data: still_in_registry.append(
                data["name"] in app.plugins,
            ),
        )
        app.disable_plugin("sample")
        assert still_in_registry == [True]

    def test_disable_plugin_warns_about_dependents(
        self,
        tmp_path,
        caplog,
    ):
        dep_plugin = tmp_path / "dep_plugin.py"
        dep_plugin.write_text(
            "from reticulumpi.plugin_base import PluginBase\n"
            "class DepPlugin(PluginBase):\n"
            "    plugin_name = 'dep'\n"
            "    plugin_dependencies = ['base_plugin']\n"
            "    def start(self): self._active = True\n"
            "    def stop(self): self._active = False\n"
        )
        base_plugin = tmp_path / "base_plugin.py"
        base_plugin.write_text(
            "from reticulumpi.plugin_base import PluginBase\n"
            "class BasePlugin(PluginBase):\n"
            "    plugin_name = 'base_plugin'\n"
            "    def start(self): self._active = True\n"
            "    def stop(self): self._active = False\n"
        )
        app = _make_hot_reload_app(tmp_path, str(tmp_path))
        app.enable_plugin("base_plugin")
        app.enable_plugin("dep")
        stopping: list[str] = []
        app.event_bus.subscribe(
            events.PLUGIN_STOPPING,
            lambda _event, data: stopping.append(data["name"]),
        )
        with pytest.raises(RuntimeError, match="dependents still running"):
            app.disable_plugin("base_plugin")
        assert stopping == []


# ---------------------------------------------------------------------------
# Plugin dependency ordering
# ---------------------------------------------------------------------------


class TestPluginDependencyOrdering:
    def test_hot_enable_rejects_missing_hard_dependency(self, tmp_path):
        (tmp_path / "alpha_plugin.py").write_text(
            "from reticulumpi.plugin_base import PluginBase\n"
            "class Alpha(PluginBase):\n"
            "    plugin_name = 'alpha'\n"
            "    plugin_dependencies = ['beta']\n"
            "    def start(self): self._active = True\n"
            "    def stop(self): self._active = False\n"
        )
        app = _make_hot_reload_app(tmp_path, str(tmp_path))
        blocked: list[dict] = []
        app.event_bus.subscribe(
            events.PLUGIN_BLOCKED,
            lambda _event, data: blocked.append(data),
        )
        with pytest.raises(RuntimeError, match="hard dependency 'beta'"):
            app.enable_plugin("alpha")
        assert app.get_plugin_state("alpha").value == "blocked"
        assert blocked[0]["name"] == "alpha"

    def test_topo_sort_respects_dependencies(self, tmp_path):
        (tmp_path / "alpha_plugin.py").write_text(
            "from reticulumpi.plugin_base import PluginBase\n"
            "class Alpha(PluginBase):\n"
            "    plugin_name = 'alpha'\n"
            "    plugin_dependencies = ['beta']\n"
            "    def start(self): self._active = True\n"
            "    def stop(self): self._active = False\n"
        )
        (tmp_path / "beta_plugin.py").write_text(
            "from reticulumpi.plugin_base import PluginBase\n"
            "class Beta(PluginBase):\n"
            "    plugin_name = 'beta'\n"
            "    def start(self): self._active = True\n"
            "    def stop(self): self._active = False\n"
        )
        app = _make_hot_reload_app(tmp_path, str(tmp_path))
        app.enable_plugin("beta")
        app.enable_plugin("alpha")
        order = app._topo_sort_plugins()
        assert order.index("beta") < order.index("alpha")

    def test_topo_sort_handles_no_dependencies(self, tmp_path, plugin_dir):
        app = _make_hot_reload_app(tmp_path, plugin_dir)
        app.enable_plugin("sample")
        order = app._topo_sort_plugins()
        assert "sample" in order

    def test_missing_dependency_logs_warning(self, tmp_path, caplog):
        (tmp_path / "lonely_plugin.py").write_text(
            "from reticulumpi.plugin_base import PluginBase\n"
            "class Lonely(PluginBase):\n"
            "    plugin_name = 'lonely'\n"
            "    plugin_dependencies = ['nonexistent']\n"
            "    def start(self): self._active = True\n"
            "    def stop(self): self._active = False\n"
        )
        config_file = tmp_path / "dep_cfg.yaml"
        config_file.write_text(
            "reticulumpi:\n"
            "  log_level: 4\n"
            "  identity_path: {identity}\n"
            "  plugin_paths:\n"
            "    - {pdir}\n"
            "  plugins:\n"
            "    lonely:\n"
            "      enabled: true\n".format(
                identity=str(tmp_path / "identity"),
                pdir=str(tmp_path),
            )
        )
        app = ReticulumPiApp(config_path=str(config_file))
        with caplog.at_level("WARNING"):
            app._load_plugins()
        assert "depends on 'nonexistent' which is not enabled" in caplog.text


# ---------------------------------------------------------------------------
# ThreadPoolExecutor timeout behavior (REL-1)
# ---------------------------------------------------------------------------


def test_stop_plugin_with_timeout_does_not_block_on_hung_plugin():
    """pool.shutdown(wait=False) prevents a hung stop() from blocking the caller."""
    import threading
    import time

    app = ReticulumPiApp()
    plugin = MagicMock()
    hang_time = 10

    def _hang():
        threading.current_thread()._stop_event = threading.Event()
        threading.current_thread()._stop_event.wait(hang_time)

    plugin.stop.side_effect = _hang

    timeout = 0.3
    t0 = time.monotonic()
    app._stop_plugin_with_timeout("hung", plugin, timeout)
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, (
        f"_stop_plugin_with_timeout blocked for {elapsed:.1f}s; expected < 1s (timeout={timeout}s)"
    )


def test_hung_start_is_not_stopped_concurrently(mock_app):
    import threading

    from reticulumpi.plugin_base import PluginBase, PluginState

    release = threading.Event()
    stop_called = threading.Event()
    start_returned = threading.Event()
    cleanup_called = threading.Event()
    cleanup_observations = []

    class HungStart(PluginBase):
        plugin_name = "hung_start"

        def start(self):
            self.register_cleanup(
                lambda: (
                    cleanup_observations.append(start_returned.is_set()),
                    cleanup_called.set(),
                )
            )
            release.wait(2)
            start_returned.set()
            self._active = True

        def stop(self):
            stop_called.set()

    app = ReticulumPiApp()
    plugin = HungStart(mock_app, {})
    with pytest.raises(TimeoutError):
        app._start_plugin_with_timeout("hung_start", plugin, 0.05)
    assert plugin.plugin_state == PluginState.HUNG
    assert not stop_called.is_set()
    assert cleanup_called.wait(timeout=1.0)
    assert cleanup_observations == [False]
    release.set()
    assert start_returned.wait(timeout=1.0)
    assert plugin.plugin_state == PluginState.HUNG
    assert plugin._active is False
    plugin.cleanup_managed_resources()
    assert cleanup_observations == [False]


def test_hung_stop_requests_managed_cleanup_without_waiting(mock_app):
    import threading

    from reticulumpi.plugin_base import PluginBase, PluginState

    release = threading.Event()
    cleanup_called = threading.Event()

    class HungStop(PluginBase):
        plugin_name = "hung_stop"

        def start(self):
            self._active = True

        def stop(self):
            release.wait(2)

    app = ReticulumPiApp()
    plugin = HungStop(mock_app, {})
    plugin.register_cleanup(cleanup_called.set)
    plugin.mark_ready()

    assert app._stop_plugin_with_timeout("hung_stop", plugin, 0.05) is False
    assert plugin.plugin_state == PluginState.HUNG
    assert cleanup_called.wait(timeout=1.0)
    release.set()


def test_lifecycle_v2_waits_for_explicit_readiness(mock_app):
    import threading

    from reticulumpi.plugin_base import PluginBase, PluginState

    class AsyncReady(PluginBase):
        plugin_name = "async_ready"
        plugin_lifecycle_api = 2

        def start(self):
            threading.Timer(0.03, self.mark_ready).start()

        def stop(self):
            self._active = False

    app = ReticulumPiApp()
    plugin = AsyncReady(mock_app, {})
    app._start_plugin_with_timeout("async_ready", plugin, 1)
    assert plugin.plugin_state == PluginState.READY


def test_db_migration_rolls_back_ddl_and_version_on_failure():
    from contextlib import closing
    import sqlite3

    with closing(sqlite3.connect(":memory:")) as conn:
        with pytest.raises(sqlite3.OperationalError):
            run_db_migrations(
                conn,
                ["CREATE TABLE first_table (id INTEGER); INSERT INTO missing_table VALUES (1);"],
            )
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='first_table'"
        ).fetchone()
        assert table is None
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0


def test_db_migration_commits_schema_and_version_together():
    from contextlib import closing
    import sqlite3

    with closing(sqlite3.connect(":memory:")) as conn:
        assert run_db_migrations(conn, ["CREATE TABLE example (id INTEGER);"]) == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='example'"
        ).fetchone() == (1,)


def test_db_migration_rejects_unsafe_or_nested_transactions():
    from contextlib import closing
    import sqlite3

    with closing(sqlite3.connect(":memory:")) as conn:
        assert run_db_migrations(conn, ["CREATE TABLE example (id INTEGER)"]) == 1
        assert run_db_migrations(conn, ["CREATE TABLE example (id INTEGER)"]) == 1

    with closing(sqlite3.connect(":memory:")) as conn:
        with pytest.raises(ValueError, match="cannot run atomically"):
            run_db_migrations(conn, ["VACUUM"])

    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE existing (id INTEGER)")
        conn.execute("INSERT INTO existing VALUES (1)")
        with pytest.raises(RuntimeError, match="active transaction"):
            run_db_migrations(conn, ["CREATE TABLE pending (id INTEGER)"])
        conn.rollback()


def test_offgrid_mode_uses_configuration_when_probe_is_not_running(tmp_path):
    config_file = tmp_path / "offline.yaml"
    config_file.write_text("reticulumpi:\n  internet:\n    force_offline: true\n")
    app = ReticulumPiApp(config_path=str(config_file))
    assert app.internet_probe is None
    assert app.offgrid_mode is True

    with patch.object(app.config, "set_internet_force_offline", return_value=True):
        result = app.set_offgrid_mode(False)
    assert result["applied"] is True
    assert result["persisted"] is True


def test_start_blocks_dependents_and_withholds_systemd_readiness(tmp_path):
    """A failed provider blocks dependents while unrelated plugins still run."""

    config_file = tmp_path / "required.yaml"
    config_file.write_text(
        "reticulumpi:\n"
        "  identity_path: {identity}\n"
        "  plugins:\n"
        "    provider:\n"
        "      enabled: true\n"
        "    dependent:\n"
        "      enabled: true\n"
        "      required: true\n"
        "    observer:\n"
        "      enabled: true\n".format(identity=tmp_path / "identity")
    )
    app = ReticulumPiApp(config_path=str(config_file))
    app.announce_dispatcher = MagicMock()
    app.sdr_scheduler = MagicMock()
    offline_delivery = MagicMock(side_effect=RuntimeError("offline hook failed"))

    class FailedProvider(PluginBase):
        plugin_name = "provider"

        def start(self):
            raise RuntimeError("radio missing")

        def stop(self):
            self._active = False

    class Dependent(PluginBase):
        plugin_name = "dependent"
        plugin_dependencies = ("provider",)

        def start(self):
            self._active = True

        def stop(self):
            self._active = False

    class OfflineObserver(PluginBase):
        plugin_name = "observer"

        def start(self):
            self._active = True

        def stop(self):
            self._active = False

        def on_internet_lost(self):
            offline_delivery()

    def load_plugins() -> None:
        app.plugins = {
            "provider": FailedProvider(app, {}),
            "dependent": Dependent(app, {}),
            "observer": OfflineObserver(app, {}),
        }

    identity = MagicMock(hash=b"\x00" * 16)
    probe = MagicMock(is_online=False)
    blocked: list[str] = []
    app.event_bus.subscribe(
        events.PLUGIN_BLOCKED,
        lambda _event, data: blocked.append(data["name"]),
    )
    with (
        patch("reticulumpi.app.RNS.Reticulum", return_value=MagicMock()),
        patch("reticulumpi.app.RNS.Transport.exit_handler"),
        patch("reticulumpi.app.identity_manager.load_or_create", return_value=identity),
        patch("reticulumpi.app.InternetProbe", return_value=probe),
        patch.object(app, "_load_plugins", side_effect=load_plugins),
        patch("reticulumpi.app.set_readiness_file") as readiness,
        patch("reticulumpi.app.systemd_ready") as notify_ready,
        patch("reticulumpi.app.systemd_stopping"),
    ):
        with pytest.raises(RuntimeError, match="required plugin readiness failed"):
            app.start()

    assert app.get_plugin_state("provider") == PluginState.FAILED
    assert app.get_plugin_state("dependent") == PluginState.STOPPED
    assert blocked == ["dependent"]
    offline_delivery.assert_called_once_with()
    notify_ready.assert_not_called()
    assert all(call.args != (True,) for call in readiness.call_args_list)


def test_start_notifies_systemd_after_required_v2_plugin_is_ready(tmp_path):
    config_file = tmp_path / "ready.yaml"
    config_file.write_text(
        "reticulumpi:\n"
        "  identity_path: {identity}\n"
        "  plugins:\n"
        "    required_v2:\n"
        "      enabled: true\n"
        "      required: true\n".format(identity=tmp_path / "identity")
    )
    app = ReticulumPiApp(config_path=str(config_file))
    app.announce_dispatcher = MagicMock()
    app.sdr_scheduler = MagicMock()

    class RequiredV2(PluginBase):
        plugin_name = "required_v2"
        plugin_lifecycle_api = 2

        def start(self):
            self.mark_ready()

        def stop(self):
            self._active = False

    def load_plugins() -> None:
        app.plugins = {"required_v2": RequiredV2(app, {})}

    calls: list[object] = []
    app._shutdown_event.set()
    identity = MagicMock(hash=b"\x01" * 16)
    probe = MagicMock(is_online=True)
    with (
        patch("reticulumpi.app.RNS.Reticulum", return_value=MagicMock()),
        patch("reticulumpi.app.RNS.Transport.exit_handler"),
        patch("reticulumpi.app.identity_manager.load_or_create", return_value=identity),
        patch("reticulumpi.app.InternetProbe", return_value=probe),
        patch.object(app, "_load_plugins", side_effect=load_plugins),
        patch(
            "reticulumpi.app.set_readiness_file",
            side_effect=lambda ready: calls.append(("file", ready)),
        ),
        patch(
            "reticulumpi.app.systemd_ready",
            side_effect=lambda status: calls.append(("notify", status)),
        ) as notify_ready,
        patch("reticulumpi.app.systemd_stopping"),
    ):
        app.start()

    notify_ready.assert_called_once()
    ready_index = calls.index(("file", True))
    notify_index = next(index for index, value in enumerate(calls) if value[0] == "notify")
    assert ready_index < notify_index


def test_global_startup_deadline_bounds_multiple_optional_hangs(tmp_path):
    config_file = tmp_path / "startup-deadline.yaml"
    config_file.write_text(
        "reticulumpi:\n"
        f"  identity_path: {tmp_path / 'identity'}\n"
        "  plugins:\n"
        "    optional_one:\n"
        "      enabled: true\n"
        "    optional_two:\n"
        "      enabled: true\n"
        "    required_ready:\n"
        "      enabled: true\n"
        "      required: true\n"
    )
    app = ReticulumPiApp(config_path=str(config_file))
    app.STARTUP_TIMEOUT = 0.05
    app.PLUGIN_START_TIMEOUT = 1.0
    app.announce_dispatcher = MagicMock()
    app.sdr_scheduler = MagicMock()

    class NeverReady(PluginBase):
        plugin_lifecycle_api = 2

        def start(self):
            return None

        def stop(self):
            raise AssertionError("unfinished start must not overlap stop")

    class RequiredReady(PluginBase):
        plugin_name = "required_ready"

        def start(self):
            raise AssertionError("global deadline must block later starts")

        def stop(self):
            self._active = False

    def load_plugins() -> None:
        app.plugins = {
            "optional_one": type("OptionalOne", (NeverReady,), {"plugin_name": "optional_one"})(
                app, {}
            ),
            "optional_two": type("OptionalTwo", (NeverReady,), {"plugin_name": "optional_two"})(
                app, {}
            ),
            "required_ready": RequiredReady(app, {}),
        }

    identity = MagicMock(hash=b"\x02" * 16)
    probe = MagicMock(is_online=True)
    started = time.monotonic()
    with (
        patch("reticulumpi.app.RNS.Reticulum", return_value=MagicMock()),
        patch("reticulumpi.app.RNS.Transport.exit_handler"),
        patch("reticulumpi.app.identity_manager.load_or_create", return_value=identity),
        patch("reticulumpi.app.InternetProbe", return_value=probe),
        patch.object(app, "_load_plugins", side_effect=load_plugins),
        patch.object(app, "shutdown") as shutdown,
        patch("reticulumpi.app.set_readiness_file"),
        patch("reticulumpi.app.systemd_ready") as notify_ready,
    ):
        with pytest.raises(RuntimeError, match="required plugin readiness failed"):
            app.start()

    assert time.monotonic() - started < 0.5
    assert app.get_plugin_state("optional_one") == PluginState.HUNG
    assert app.get_plugin_state("optional_two") == PluginState.BLOCKED
    assert app.get_plugin_state("required_ready") == PluginState.BLOCKED
    notify_ready.assert_not_called()
    shutdown.assert_called_once_with()


def test_lifecycle_v2_readiness_timeout_is_retained_as_hung(mock_app):
    import threading

    cleaned = threading.Event()
    stop_called = threading.Event()

    class NeverReady(PluginBase):
        plugin_name = "never_ready"
        plugin_lifecycle_api = 2

        def start(self):
            self.register_cleanup(cleaned.set)

        def stop(self):
            stop_called.set()

    app = ReticulumPiApp()
    app.PLUGIN_START_TIMEOUT = 0.01
    with patch.object(
        app._plugin_loader,
        "discover",
        return_value={"never_ready": NeverReady},
    ):
        with pytest.raises(TimeoutError, match="did not become ready"):
            app.enable_plugin("never_ready")

    assert app.get_plugin_state("never_ready") == PluginState.HUNG
    assert app.get_ready_plugin("never_ready") is None
    assert cleaned.wait(timeout=1)
    assert not stop_called.is_set()
    with pytest.raises(RuntimeError, match="hung and cannot be disabled"):
        app.disable_plugin("never_ready")


def test_lifecycle_v2_explicit_start_failure_preserves_reason(mock_app):
    class FailedReadiness(PluginBase):
        plugin_name = "failed_readiness"
        plugin_lifecycle_api = 2

        def start(self):
            self.mark_start_failed("RNS destination unavailable")

        def stop(self):
            self._active = False

    app = ReticulumPiApp()
    plugin = FailedReadiness(mock_app, {})
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="RNS destination unavailable"):
        app._start_plugin_with_timeout("failed_readiness", plugin, 1.0)
    assert time.monotonic() - started < 0.2
    assert plugin.plugin_state == PluginState.FAILED


def test_hot_enable_keeps_hung_failed_start_as_same_process_sentinel(tmp_path):
    (tmp_path / "hung_failure.py").write_text(
        "import time\n"
        "from reticulumpi.plugin_base import PluginBase\n"
        "class HungFailure(PluginBase):\n"
        "    plugin_name = 'hung_failure'\n"
        "    plugin_version = '1.0.0'\n"
        "    def start(self):\n"
        "        raise RuntimeError('start rejected')\n"
        "    def stop(self):\n"
        "        time.sleep(1)\n"
    )
    app = _make_hot_reload_app(tmp_path, str(tmp_path))
    app.PLUGIN_STOP_TIMEOUT = 0.02

    with pytest.raises(RuntimeError, match="start rejected"):
        app.enable_plugin("hung_failure")

    sentinel = app.plugins["hung_failure"]
    assert sentinel.plugin_state == PluginState.HUNG
    with pytest.raises(RuntimeError, match=r"already running \(hung\)"):
        app.enable_plugin("hung_failure")
    assert app.plugins["hung_failure"] is sentinel


def test_shutdown_serializes_with_hot_enable_and_stops_new_plugin():
    entered = threading.Event()
    release = threading.Event()
    stopped = threading.Event()

    class BlockingPlugin(PluginBase):
        plugin_name = "blocking_enable"

        def start(self):
            entered.set()
            release.wait(timeout=2)
            self._active = True

        def stop(self):
            self._active = False
            stopped.set()

    app = ReticulumPiApp()
    app._plugin_loader.discover = MagicMock(return_value={"blocking_enable": BlockingPlugin})
    app.sdr_scheduler = MagicMock()
    app.announce_dispatcher = MagicMock()
    errors: list[BaseException] = []

    def enable() -> None:
        try:
            app.enable_plugin("blocking_enable")
        except BaseException as exc:
            errors.append(exc)

    with (
        patch("reticulumpi.app.set_readiness_file"),
        patch("reticulumpi.app.systemd_stopping"),
    ):
        enable_thread = threading.Thread(target=enable)
        enable_thread.start()
        assert entered.wait(timeout=1)
        shutdown_thread = threading.Thread(target=app.shutdown)
        shutdown_thread.start()
        time.sleep(0.02)
        assert shutdown_thread.is_alive()
        release.set()
        enable_thread.join(timeout=2)
        shutdown_thread.join(timeout=2)

    assert errors == []
    assert not enable_thread.is_alive() and not shutdown_thread.is_alive()
    assert stopped.is_set()
    assert app.plugins["blocking_enable"].plugin_state == PluginState.STOPPED
    with pytest.raises(RuntimeError, match="during shutdown"):
        app.enable_plugin("blocking_enable")


def test_hot_discovery_never_holds_registry_lock():
    app = ReticulumPiApp()
    discovery_entered = threading.Event()
    release_discovery = threading.Event()
    errors: list[BaseException] = []

    def discover(_directories):
        discovery_entered.set()
        release_discovery.wait(timeout=2)
        return {}

    app._plugin_loader.discover = discover

    def enable() -> None:
        try:
            app.enable_plugin("missing")
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=enable)
    worker.start()
    assert discovery_entered.wait(timeout=1)
    started = time.monotonic()
    assert app.get_plugin("anything") is None
    assert app.get_plugin_state("anything") is None
    assert time.monotonic() - started < 0.1
    release_discovery.set()
    worker.join(timeout=2)
    assert len(errors) == 1 and isinstance(errors[0], KeyError)


def test_hot_migration_failure_blocks_without_start_or_stop():
    cleaned = threading.Event()
    started = MagicMock()
    stopped = MagicMock()

    class MigrationBlocked(PluginBase):
        plugin_name = "migration_blocked"

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.register_cleanup(cleaned.set)

        def start(self):
            started()

        def stop(self):
            stopped()

    app = ReticulumPiApp()
    app._plugin_loader.discover = MagicMock(return_value={"migration_blocked": MigrationBlocked})
    app._migrate_plugin = MagicMock(side_effect=RuntimeError("checksum mismatch"))

    with pytest.raises(RuntimeError, match="migration failed: checksum mismatch"):
        app.enable_plugin("migration_blocked")

    plugin = app.plugins["migration_blocked"]
    assert plugin.plugin_state == PluginState.BLOCKED
    assert "checksum mismatch" in plugin.get_lifecycle_status()["reason"]
    assert cleaned.wait(timeout=1)
    started.assert_not_called()
    stopped.assert_not_called()


def test_invalid_dependency_metadata_blocks_only_offending_plugin(mock_app):
    class Malformed(PluginBase):
        plugin_name = "malformed"
        plugin_dependencies = ([],)

        def start(self):
            raise AssertionError("blocked plugin must not start")

        def stop(self):
            raise AssertionError("blocked plugin must not stop")

    class Healthy(PluginBase):
        plugin_name = "healthy"

        def start(self):
            self._active = True

        def stop(self):
            self._active = False

    app = ReticulumPiApp()
    malformed = Malformed(mock_app, {})
    healthy = Healthy(mock_app, {})
    app.plugins = {"malformed": malformed, "healthy": healthy}

    app._block_invalid_plugin_metadata()
    order = app._topo_sort_plugins()

    assert malformed.plugin_state == PluginState.BLOCKED
    assert healthy.plugin_state == PluginState.DISCOVERED
    assert order == ["malformed", "healthy"]


def test_stopping_event_observers_cannot_resolve_ready_plugin(mock_app):
    observations = []

    class Stoppable(PluginBase):
        plugin_name = "stoppable"

        def start(self):
            self._active = True

        def stop(self):
            self._active = False

    app = ReticulumPiApp()
    plugin = Stoppable(mock_app, {})
    plugin.mark_starting()
    plugin.start()
    plugin.mark_ready()
    app.plugins["stoppable"] = plugin
    app.event_bus.subscribe(
        events.PLUGIN_STOPPING,
        lambda _event, _data: observations.append(
            (app.get_plugin_state("stoppable"), app.get_ready_plugin("stoppable"))
        ),
    )

    app.disable_plugin("stoppable")

    assert observations == [(PluginState.STOPPING, None)]


def test_ready_provider_loss_transitively_blocks_hard_dependents_only():
    cleanup_events = {name: threading.Event() for name in ("dependent", "transitive")}

    class Service(PluginBase):
        def start(self):
            self._active = True

        def stop(self):
            self._active = False

    app = ReticulumPiApp()
    provider = Service(app, {})
    provider.plugin_name = "provider"
    dependent = Service(app, {})
    dependent.plugin_name = "dependent"
    dependent.plugin_dependencies = ("provider",)
    transitive = Service(app, {})
    transitive.plugin_name = "transitive"
    transitive.plugin_dependencies = ("dependent",)
    soft = Service(app, {})
    soft.plugin_name = "soft"
    soft.plugin_soft_dependencies = ("provider",)
    app.plugins = {
        "provider": provider,
        "dependent": dependent,
        "transitive": transitive,
        "soft": soft,
    }
    for name, plugin in app.plugins.items():
        plugin.mark_starting()
        plugin.start()
        plugin.mark_ready()
        if name in cleanup_events:
            plugin.register_cleanup(cleanup_events[name].set)

    provider.mark_hung("radio vanished")

    assert dependent.plugin_state == PluginState.BLOCKED
    assert transitive.plugin_state == PluginState.BLOCKED
    assert soft.plugin_state == PluginState.READY
    assert app.get_ready_plugin("dependent") is None
    assert app.get_ready_plugin("transitive") is None
    assert app.get_ready_plugin("soft") is soft
    assert cleanup_events["dependent"].wait(timeout=1)
    assert cleanup_events["transitive"].wait(timeout=1)


def test_pre_stop_handles_group_fallback_and_process_races():
    from types import SimpleNamespace

    false_group = MagicMock()
    false_group.request_stop.return_value = False
    failed_group = MagicMock()
    failed_group.request_stop.side_effect = RuntimeError("broker unavailable")
    app = ReticulumPiApp()
    app.plugins = {
        "group-declined": SimpleNamespace(
            _process_group=false_group,
            _process=SimpleNamespace(pid=1, poll=lambda: None),
        ),
        "group-fallback": SimpleNamespace(
            _process_group=failed_group,
            _process=SimpleNamespace(pid=2, poll=lambda: None),
        ),
        "already-exited": SimpleNamespace(
            _process_group=None,
            _process=SimpleNamespace(pid=3, poll=lambda: 0),
        ),
        "vanished": SimpleNamespace(
            _process_group=None,
            _process=SimpleNamespace(pid=4, poll=lambda: None),
        ),
        "no-process": SimpleNamespace(_process_group=None, _process=None),
    }

    def kill(pid: int, _signum: int) -> None:
        if pid == 4:
            raise ProcessLookupError(pid)

    with patch("reticulumpi.app.os.kill", side_effect=kill) as raw_kill:
        app._pre_stop_signal_subprocesses()

    assert [call.args[0] for call in raw_kill.call_args_list] == [2, 4]
    false_group.request_stop.assert_called_once_with()
    failed_group.request_stop.assert_called_once_with()


def test_shutdown_is_idempotent_and_isolates_plugin_failures():
    from types import SimpleNamespace

    app = ReticulumPiApp()
    blocked = SimpleNamespace(
        plugin_state=PluginState.BLOCKED,
        cleanup_managed_resources=MagicMock(),
        mark_stopped=MagicMock(),
    )
    hung = SimpleNamespace(plugin_state=PluginState.HUNG)
    normal = SimpleNamespace(plugin_state=PluginState.READY)
    app.plugins = {"blocked": blocked, "hung": hung, "normal": normal}
    app.event_bus = MagicMock()
    app.event_bus.publish.side_effect = RuntimeError("subscriber failed")
    app.sdr_scheduler = MagicMock()
    app.announce_dispatcher = MagicMock()

    with (
        patch.object(app, "_pre_stop_signal_subprocesses"),
        patch.object(
            app,
            "_stop_plugin_with_timeout",
            side_effect=RuntimeError("stop failed"),
        ) as stop_plugin,
        patch.object(app, "_cleanup_rns"),
        patch("reticulumpi.app.set_readiness_file"),
        patch("reticulumpi.app.systemd_stopping"),
    ):
        app.shutdown()
        app.shutdown()

    stop_plugin.assert_called_once()
    blocked.cleanup_managed_resources.assert_called_once()
    blocked.mark_stopped.assert_called_once_with()
    app.event_bus.shutdown.assert_called_once_with()


def test_shutdown_honors_global_deadline():
    from types import SimpleNamespace

    app = ReticulumPiApp()
    app.plugins = {"late": SimpleNamespace(plugin_state=PluginState.READY)}
    app.event_bus = MagicMock()
    app.sdr_scheduler = MagicMock()
    app.announce_dispatcher = MagicMock()
    timestamps = iter([100.0, 146.0])
    with (
        patch(
            "reticulumpi.app.time.monotonic",
            side_effect=lambda: next(timestamps, 147.0),
        ),
        patch.object(app, "_pre_stop_signal_subprocesses"),
        patch.object(app, "_stop_plugin_with_timeout") as stop_plugin,
        patch.object(app, "_cleanup_rns"),
        patch("reticulumpi.app.set_readiness_file"),
        patch("reticulumpi.app.systemd_stopping"),
    ):
        app.shutdown()
    stop_plugin.assert_not_called()


def test_readiness_lookup_and_nonready_dependency_reason(mock_app):
    class MinimalPlugin(PluginBase):
        plugin_name = "minimal"

        def start(self):
            self._active = True

        def stop(self):
            self._active = False

    app = ReticulumPiApp()
    provider = MinimalPlugin(mock_app, {})
    dependent = MinimalPlugin(mock_app, {})
    dependent.plugin_dependencies = ("provider",)
    app.plugins = {"provider": provider}

    assert app.get_ready_plugin("missing") is None
    assert app.get_ready_plugin("provider") is None
    assert app.get_plugin_state("provider") == PluginState.DISCOVERED
    assert "is not ready (discovered)" in app._hard_dependency_problem(dependent)

    provider.mark_starting()
    provider.mark_ready()
    assert app.get_ready_plugin("provider") is provider
    assert app._hard_dependency_problem(dependent) is None


def test_required_plugin_gate_ignores_malformed_and_ready_entries():
    app = ReticulumPiApp()
    app.config._data["plugins"] = {
        "malformed": "not-a-mapping",
        "disabled": {"enabled": False, "required": True},
        "ready": {"enabled": True, "required": True},
        "missing": {"enabled": True, "required": True},
    }
    app._plugin_state_history["ready"] = (PluginState.READY, None)
    assert app._required_plugin_failures() == ["missing is not discovered"]


def test_migration_adapter_skips_empty_result(mock_app):
    from types import SimpleNamespace

    target = object()

    class Migrating(PluginBase):
        plugin_name = "migrating"

        def start(self):
            self._active = True

        def stop(self):
            self._active = False

        def get_migration_targets(self):
            return (target,)

    app = ReticulumPiApp()
    plugin = Migrating(mock_app, {})
    result = SimpleNamespace(applied=())
    with patch("reticulumpi.app.migrate_target", return_value=result) as migrate:
        app._migrate_plugin("migrating", plugin)
    migrate.assert_called_once_with(target, dry_run=False)


def test_status_failure_from_plugin_base_keeps_lifecycle(mock_app):
    class BrokenStatus(PluginBase):
        plugin_name = "broken_status"

        def start(self):
            self._active = True

        def stop(self):
            self._active = False

        def get_status(self):
            raise RuntimeError("telemetry unavailable")

    app = ReticulumPiApp()
    plugin = BrokenStatus(mock_app, {})
    plugin.mark_starting()
    plugin.mark_degraded("telemetry unavailable")
    app.plugins["broken"] = plugin

    status = app.get_status()["plugins"]["broken"]
    assert status["error"] == "status collection failed"
    assert status["_lifecycle"]["health"] == "degraded"


def test_operational_metrics_tolerate_malformed_processes_and_collectors(mock_app):
    class MetricsPlugin(PluginBase):
        plugin_name = "metrics_edge"

        def start(self):
            self._active = True

        def stop(self):
            self._active = False

    class BrokenGroup:
        @property
        def processes(self):
            raise AttributeError("no process snapshot")

    class BrokenPoll:
        @staticmethod
        def poll():
            raise ProcessLookupError("reaped")

    class BrokenStats:
        def get_stats(self):
            raise RuntimeError("counter lock poisoned")

    class BrokenSdrMetrics:
        def get_metrics(self):
            raise RuntimeError("scheduler lock poisoned")

    app = ReticulumPiApp()
    malformed_group = MetricsPlugin(mock_app, {})
    malformed_group._process_group = BrokenGroup()
    no_poll = MetricsPlugin(mock_app, {})
    no_poll._process = object()
    broken_poll = MetricsPlugin(mock_app, {})
    broken_poll._process = BrokenPoll()
    app.plugins = {
        "malformed-group": malformed_group,
        "no-poll": no_poll,
        "broken-poll": broken_poll,
    }
    app.event_bus = object()
    app.announce_dispatcher = BrokenStats()
    app.sdr_scheduler = BrokenSdrMetrics()

    metrics = app._get_operational_metrics()
    assert metrics["event_bus"] == {}
    assert metrics["announce_dispatcher"] == {}
    assert metrics["processes"] == {
        "managed_groups": 0,
        "managed_processes": 0,
        "raw_processes": 0,
        "total_live": 0,
        "restarts": 0,
        "restarts_total": metrics["processes"]["restarts_total"],
    }
    assert metrics["processes"]["restarts_total"] >= 0
    assert metrics["sdr"] == {"canonical_claims": 0, "active_leases": 0}


def test_load_plugins_isolates_constructor_and_reports_soft_dependency(tmp_path, caplog):
    config_file = tmp_path / "plugins.yaml"
    config_file.write_text(
        "reticulumpi:\n"
        "  plugins:\n"
        "    disabled:\n"
        "      enabled: false\n"
        "    broken:\n"
        "      enabled: true\n"
        "    soft:\n"
        "      enabled: true\n"
    )
    app = ReticulumPiApp(config_path=str(config_file))

    class BrokenConstructor:
        plugin_version = "1.0"

        def __init__(self, _app, _config):
            raise RuntimeError("invalid hardware config")

    class SoftConsumer(PluginBase):
        plugin_name = "soft"
        plugin_version = "1.0"
        plugin_soft_dependencies = ("optional_provider",)

        def start(self):
            self._active = True

        def stop(self):
            self._active = False

    with (
        patch.object(
            app._plugin_loader,
            "discover",
            return_value={"broken": BrokenConstructor, "soft": SoftConsumer},
        ),
        caplog.at_level("INFO"),
    ):
        app._load_plugins()

    assert "disabled" not in app.plugins
    assert "broken" not in app.plugins
    assert app._failed_plugins == [("broken", "instantiation failed: invalid hardware config")]
    assert "soft-depends on 'optional_provider'" in caplog.text


def test_topological_sort_isolates_cycles_and_orders_soft_dependencies():
    from types import SimpleNamespace

    app = ReticulumPiApp()
    app.plugins = {
        "alpha": SimpleNamespace(
            plugin_dependencies=("beta",),
            plugin_soft_dependencies=(),
        ),
        "beta": SimpleNamespace(
            plugin_dependencies=("alpha",),
            plugin_soft_dependencies=(),
        ),
    }
    assert app._topo_sort_plugins() == ["alpha", "beta"]
    assert app._plugin_dependency_cycles == {
        "alpha": ("alpha", "beta"),
        "beta": ("alpha", "beta"),
    }

    app.plugins = {
        "consumer": SimpleNamespace(
            plugin_dependencies=("missing",),
            plugin_soft_dependencies=("provider", "missing-soft"),
        ),
        "provider": SimpleNamespace(
            plugin_dependencies=(),
            plugin_soft_dependencies=(),
        ),
    }
    assert app._topo_sort_plugins() == ["provider", "consumer"]


def test_dependency_cycles_block_participants_and_hard_dependents_only():
    class LifecyclePlugin(PluginBase):
        def __init__(self, app, name, dependencies=()):
            super().__init__(app, {})
            self.plugin_name = name
            self.plugin_dependencies = dependencies

        def start(self):
            self._active = True

        def stop(self):
            self._active = False

    app = ReticulumPiApp()
    app.plugins = {
        "alpha": LifecyclePlugin(app, "alpha", ("beta",)),
        "beta": LifecyclePlugin(app, "beta", ("alpha",)),
        "dependent": LifecyclePlugin(app, "dependent", ("alpha",)),
        "unrelated": LifecyclePlugin(app, "unrelated"),
    }
    blocked: list[dict] = []
    app.event_bus.subscribe(events.PLUGIN_BLOCKED, lambda _event, data: blocked.append(data))

    order = app._topo_sort_plugins()
    app._block_dependency_cycles()

    assert order[:2] == ["alpha", "beta"]
    assert app.plugins["alpha"].plugin_state == PluginState.BLOCKED
    assert app.plugins["beta"].plugin_state == PluginState.BLOCKED
    assert "not ready (blocked)" in app._hard_dependency_problem(app.plugins["dependent"])
    assert app._hard_dependency_problem(app.plugins["unrelated"]) is None
    assert {item["name"] for item in blocked} == {"alpha", "beta"}


def test_startup_report_handles_interfaces_and_transport_failure(caplog):
    from types import SimpleNamespace

    class BrokenTransport:
        @property
        def interfaces(self):
            raise RuntimeError("RNS is stopping")

    app = ReticulumPiApp()
    with caplog.at_level("INFO"):
        with patch(
            "reticulumpi.app.RNS.Transport",
            new=SimpleNamespace(interfaces=["TCPClientInterface[test]"]),
        ):
            app._print_startup_report()
        with patch("reticulumpi.app.RNS.Transport", new=BrokenTransport()):
            app._print_startup_report()

    assert "Interface: TCPClientInterface[test]" in caplog.text
    assert "Interfaces: unavailable" in caplog.text


def test_check_and_list_plugins_report_empty_discovery(capsys):
    app = ReticulumPiApp()
    with patch.object(app._plugin_loader, "discover", return_value={}):
        assert app.check() is True
        app.list_plugins()
    output = capsys.readouterr().out
    assert "No plugins found in:" in output


def test_check_reports_enabled_discovered_plugin(tmp_path, capsys):
    config_file = tmp_path / "check.yaml"
    config_file.write_text("reticulumpi:\n  plugins:\n    ready:\n      enabled: true\n")
    app = ReticulumPiApp(config_path=str(config_file))
    plugin_class = type(
        "ReadyPlugin",
        (),
        {"plugin_version": "1.0", "plugin_description": "ready"},
    )
    with patch.object(app._plugin_loader, "discover", return_value={"ready": plugin_class}):
        assert app.check() is True
    assert "ready: OK" in capsys.readouterr().out


def test_signal_handlers_request_shutdown():
    import signal

    app = ReticulumPiApp()
    handlers: dict[int, object] = {}
    with patch(
        "reticulumpi.app.signal.signal",
        side_effect=lambda signum, callback: handlers.__setitem__(signum, callback),
    ):
        app._install_signal_handlers()

    handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert app._shutdown_event.is_set()
