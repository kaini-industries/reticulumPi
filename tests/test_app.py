"""Tests for the ReticulumPiApp orchestrator."""

from unittest.mock import MagicMock, patch

import pytest

from reticulumpi.app import ReticulumPiApp


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
    app.start()

    assert "sample" in app.plugins
    assert app.plugins["sample"]._active is True


def test_get_version():
    from reticulumpi import __version__

    app = ReticulumPiApp()
    assert app._get_version() == __version__


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
        assert result == {"enabled": True, "persisted": True}
        app.internet_probe.set_force_offline.assert_called_once_with(True)

    def test_returns_persisted_false_on_oserror(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("reticulumpi:\n  log_level: 4\n")
        app = ReticulumPiApp(config_path=str(config_file))
        app.internet_probe = MagicMock()
        app.internet_probe.force_offline = False
        with patch.object(app.config, "set_internet_force_offline", return_value=False):
            result = app.set_offgrid_mode(True)
        assert result == {"enabled": True, "persisted": False}

    def test_no_change_returns_early(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("reticulumpi:\n  log_level: 4\n")
        app = ReticulumPiApp(config_path=str(config_file))
        app.internet_probe = MagicMock()
        app.internet_probe.force_offline = True
        result = app.set_offgrid_mode(True)
        assert result == {"enabled": True, "persisted": True}
        app.internet_probe.set_force_offline.assert_not_called()

    def test_probe_updated_before_event_published(self, tmp_path):
        """internet_probe.set_force_offline is called BEFORE event_bus.publish."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("reticulumpi:\n  log_level: 4\n")
        app = ReticulumPiApp(config_path=str(config_file))
        app.internet_probe = MagicMock()
        app.internet_probe.force_offline = False
        call_order = []
        app.internet_probe.set_force_offline.side_effect = (
            lambda v: call_order.append("probe")
        )
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
            f"        open({str(flag)!r}, 'w').write('1')\n"
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

    def test_disable_plugin_swallows_stop_exceptions(self, tmp_path):
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
        app.disable_plugin("angry_hr")  # Must not raise
        assert "angry_hr" not in app.plugins
        assert received == [{"name": "angry_hr"}]

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
        with caplog.at_level("WARNING"):
            app.disable_plugin("base_plugin")
        assert "dependency of running plugin 'dep'" in caplog.text


# ---------------------------------------------------------------------------
# Plugin dependency ordering
# ---------------------------------------------------------------------------


class TestPluginDependencyOrdering:
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
        app.enable_plugin("alpha")
        app.enable_plugin("beta")
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
        f"_stop_plugin_with_timeout blocked for {elapsed:.1f}s; "
        f"expected < 1s (timeout={timeout}s)"
    )
