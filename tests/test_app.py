"""Tests for the ReticulumPiApp orchestrator."""

from unittest.mock import MagicMock, patch

import pytest

from reticulumpi.app import ReticulumPiApp


@pytest.fixture
def app_with_config(tmp_path):
    """Create an app instance with a minimal config (no plugins enabled)."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "reticulumpi:\n"
        "  log_level: 4\n"
        "  identity_path: {identity}\n"
        "  plugins: {{}}\n".format(identity=str(tmp_path / "identity"))
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
    config_file.write_text(
        "reticulumpi:\n  log_level: 4\n"
    )
    app = ReticulumPiApp(config_path=str(config_file), log_level_override=7)
    assert app._log_level == 7


def test_constructor_uses_config_log_level(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "reticulumpi:\n  log_level: 2\n"
    )
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
        "reticulumpi:\n"
        "  log_level: 4\n"
        "  identity_path: {identity}\n"
        "  plugins: {{}}\n".format(identity=str(tmp_path / "identity"))
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
        "reticulumpi:\n"
        "  log_level: 4\n"
        "  plugins:\n"
        "    nonexistent:\n"
        "      enabled: true\n"
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
        "reticulumpi:\n"
        "  log_level: 4\n"
        "  identity_path: {identity}\n"
        "  plugins: {{}}\n".format(identity=str(tmp_path / "identity"))
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
def test_startup_report_logs_plugins(mock_identity, mock_transport, mock_rns, tmp_path, caplog, plugin_dir):
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
    with caplog.at_level(logging.WARNING), patch("RNS.Transport") as mock_transport:
        mock_transport.interfaces = []
        app._print_startup_report()
    assert any("FAILED" in msg and "broken" in msg for msg in caplog.messages)


def test_check_returns_true_valid(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("reticulumpi:\n  log_level: 4\n  plugins: {}\n")
    app = ReticulumPiApp(config_path=str(config_file))
    assert app.check() is True


def test_check_returns_false_missing_plugin(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "reticulumpi:\n"
        "  log_level: 4\n"
        "  plugins:\n"
        "    nonexistent:\n"
        "      enabled: true\n"
    )
    app = ReticulumPiApp(config_path=str(config_file))
    assert app.check() is False


def test_list_plugins_prints_discovered(tmp_path, plugin_dir, capsys):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "reticulumpi:\n"
        "  log_level: 4\n"
        "  plugin_paths:\n"
        "    - {pdir}\n"
        "  plugins: {{}}\n".format(pdir=plugin_dir)
    )
    app = ReticulumPiApp(config_path=str(config_file))
    app.list_plugins()
    captured = capsys.readouterr()
    assert "sample" in captured.out
    assert "0.1.0" in captured.out
