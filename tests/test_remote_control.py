"""Tests for the RemoteControl plugin."""

from unittest.mock import MagicMock, patch

import pytest

from reticulumpi.event_bus import EventBus


@pytest.fixture
def mock_app():
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.identity.hash = b"\x01" * 16
    app.event_bus = EventBus()
    app.plugins = {}
    app.node_name = "TestNode"
    app.get_plugin.return_value = None
    app._failed_plugins = []
    return app


@pytest.fixture
def plugin_config():
    return {
        "enabled": True,
        "allowed_identities": ["aa" * 16],
        "log_buffer_lines": 100,
    }


@pytest.fixture
def authorized_identity():
    """A mock identity whose hash is in the default plugin_config allowlist."""
    identity = MagicMock()
    identity.hash = bytes.fromhex("aa" * 16)
    return identity


@pytest.fixture
def unauthorized_identity():
    """A mock identity whose hash is NOT in the allowlist."""
    identity = MagicMock()
    identity.hash = b"\xff" * 16
    return identity


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_start_stop(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.remote_control import RemoteControlPlugin

    plugin = RemoteControlPlugin(mock_app, plugin_config)
    plugin.start()
    assert plugin._active is True
    assert len(plugin._allowed_hashes) == 1
    plugin.stop()
    assert plugin._active is False


def test_validate_config_bad_identities(mock_app):
    from reticulumpi.builtin_plugins.remote_control import RemoteControlPlugin

    with pytest.raises(ValueError, match="allowed_identities"):
        RemoteControlPlugin(mock_app, {"allowed_identities": "not-a-list"})


def test_validate_config_bad_hash(mock_app):
    from reticulumpi.builtin_plugins.remote_control import RemoteControlPlugin

    with pytest.raises(ValueError, match="Invalid identity hash"):
        RemoteControlPlugin(mock_app, {"allowed_identities": ["ab"]})


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_no_allowed_identities_warning(mock_dest, mock_transport, mock_app):
    from reticulumpi.builtin_plugins.remote_control import RemoteControlPlugin

    plugin = RemoteControlPlugin(mock_app, {"enabled": True, "allowed_identities": []})
    plugin.start()
    assert len(plugin._allowed_hashes) == 0
    plugin.stop()


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_handle_ping(mock_dest, mock_transport, mock_app, plugin_config, authorized_identity):
    from reticulumpi.builtin_plugins.remote_control import RemoteControlPlugin
    import RNS.vendor.umsgpack as umsgpack

    plugin = RemoteControlPlugin(mock_app, plugin_config)
    plugin.start()

    result = plugin._handle_ping("/ping", None, None, None, authorized_identity, None)
    data = umsgpack.unpackb(result)
    assert data["ok"] is True
    assert data["node"] == "TestNode"
    plugin.stop()


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_handle_ping_rejects_unauthorized(
    mock_dest, mock_transport, mock_app, plugin_config, unauthorized_identity
):
    from reticulumpi.builtin_plugins.remote_control import RemoteControlPlugin
    import RNS.vendor.umsgpack as umsgpack

    plugin = RemoteControlPlugin(mock_app, plugin_config)
    plugin.start()

    result = plugin._handle_ping("/ping", None, None, None, unauthorized_identity, None)
    data = umsgpack.unpackb(result)
    assert data["ok"] is False
    assert "unauthorized" in data.get("error", "")
    plugin.stop()


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_handle_ping_rejects_no_identity(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.remote_control import RemoteControlPlugin
    import RNS.vendor.umsgpack as umsgpack

    plugin = RemoteControlPlugin(mock_app, plugin_config)
    plugin.start()

    result = plugin._handle_ping("/ping", None, None, None, None, None)
    data = umsgpack.unpackb(result)
    assert data["ok"] is False
    assert "identification" in data.get("error", "")
    plugin.stop()


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_handle_status(mock_dest, mock_transport, mock_app, plugin_config, authorized_identity):
    from reticulumpi.builtin_plugins.remote_control import RemoteControlPlugin
    import RNS.vendor.umsgpack as umsgpack

    mock_app.get_status.return_value = {"version": "0.2.0", "plugins": {}}
    plugin = RemoteControlPlugin(mock_app, plugin_config)
    plugin.start()

    result = plugin._handle_status("/status", None, None, None, authorized_identity, None)
    data = umsgpack.unpackb(result)
    assert data["ok"] is True
    assert "data" in data
    plugin.stop()


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_handle_metrics_no_monitor(
    mock_dest, mock_transport, mock_app, plugin_config, authorized_identity
):
    from reticulumpi.builtin_plugins.remote_control import RemoteControlPlugin
    import RNS.vendor.umsgpack as umsgpack

    plugin = RemoteControlPlugin(mock_app, plugin_config)
    plugin.start()

    result = plugin._handle_metrics("/metrics", None, None, None, authorized_identity, None)
    data = umsgpack.unpackb(result)
    assert data["ok"] is False
    plugin.stop()


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_handle_plugins(mock_dest, mock_transport, mock_app, plugin_config, authorized_identity):
    from reticulumpi.builtin_plugins.remote_control import RemoteControlPlugin
    import RNS.vendor.umsgpack as umsgpack

    mock_plugin = MagicMock()
    mock_plugin.plugin_version = "1.0.0"
    mock_plugin.plugin_description = "Test"
    mock_plugin.get_status.return_value = {"active": True}
    mock_app.plugins = {"test_plugin": mock_plugin}

    plugin = RemoteControlPlugin(mock_app, plugin_config)
    plugin.start()

    result = plugin._handle_plugins("/plugins", None, None, None, authorized_identity, None)
    data = umsgpack.unpackb(result)
    assert data["ok"] is True
    assert "test_plugin" in data["data"]
    plugin.stop()


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_link_rejects_unauthorized(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.remote_control import RemoteControlPlugin

    plugin = RemoteControlPlugin(mock_app, plugin_config)
    plugin.start()

    mock_link = MagicMock()
    mock_identity = MagicMock()
    mock_identity.hash = b"\xff" * 16  # Not in allowed list

    plugin._remote_identified(mock_link, mock_identity)
    mock_link.teardown.assert_called_once()
    assert mock_link not in plugin._active_links
    plugin.stop()


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_link_accepts_authorized(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.remote_control import RemoteControlPlugin

    plugin = RemoteControlPlugin(mock_app, plugin_config)
    plugin.start()

    mock_link = MagicMock()
    mock_identity = MagicMock()
    mock_identity.hash = bytes.fromhex("aa" * 16)

    plugin._remote_identified(mock_link, mock_identity)
    mock_link.teardown.assert_not_called()
    assert mock_link in plugin._active_links
    plugin.stop()


@patch("RNS.Transport")
@patch("RNS.Destination")
def test_get_status(mock_dest, mock_transport, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.remote_control import RemoteControlPlugin

    plugin = RemoteControlPlugin(mock_app, plugin_config)
    plugin.start()
    status = plugin.get_status()
    assert status["active"] is True
    assert status["active_links"] == 0
    assert status["allowed_identities"] == 1
    plugin.stop()


# ---------------------------------------------------------------------------
# Plugin enable/disable Link handlers
# ---------------------------------------------------------------------------


class TestPluginEnableDisableHandlers:
    @patch("RNS.Transport")
    @patch("RNS.Destination")
    def test_handle_plugin_enable_success(
        self, mock_dest, mock_transport, mock_app, plugin_config, authorized_identity
    ):
        from reticulumpi.builtin_plugins.remote_control import RemoteControlPlugin
        import RNS.vendor.umsgpack as umsgpack

        plugin = RemoteControlPlugin(mock_app, plugin_config)
        plugin.start()

        payload = umsgpack.packb({"name": "sample"})
        result = plugin._handle_plugin_enable(
            "/plugin/enable", payload, None, None, authorized_identity, None
        )
        data = umsgpack.unpackb(result)
        assert data["ok"] is True
        assert data["message"] == "Plugin 'sample' enabled"
        mock_app.enable_plugin.assert_called_once_with("sample")
        plugin.stop()

    @patch("RNS.Transport")
    @patch("RNS.Destination")
    def test_handle_plugin_enable_rejects_non_bytes_data(
        self, mock_dest, mock_transport, mock_app, plugin_config, authorized_identity
    ):
        from reticulumpi.builtin_plugins.remote_control import RemoteControlPlugin
        import RNS.vendor.umsgpack as umsgpack

        plugin = RemoteControlPlugin(mock_app, plugin_config)
        plugin.start()

        result = plugin._handle_plugin_enable(
            "/plugin/enable", None, None, None, authorized_identity, None
        )
        data = umsgpack.unpackb(result)
        assert data["ok"] is False
        assert "missing plugin name" in data.get("error", "")
        mock_app.enable_plugin.assert_not_called()
        plugin.stop()

    @patch("RNS.Transport")
    @patch("RNS.Destination")
    def test_handle_plugin_enable_rejects_malformed_msgpack(
        self, mock_dest, mock_transport, mock_app, plugin_config, authorized_identity
    ):
        from reticulumpi.builtin_plugins.remote_control import RemoteControlPlugin
        import RNS.vendor.umsgpack as umsgpack

        plugin = RemoteControlPlugin(mock_app, plugin_config)
        plugin.start()

        # 0xc1 is the msgpack "reserved/never used" code — umsgpack raises.
        result = plugin._handle_plugin_enable(
            "/plugin/enable",
            b"\xc1",
            None,
            None,
            authorized_identity,
            None,
        )
        data = umsgpack.unpackb(result)
        assert data["ok"] is False
        assert "invalid request" in data.get("error", "")
        mock_app.enable_plugin.assert_not_called()
        plugin.stop()

    @patch("RNS.Transport")
    @patch("RNS.Destination")
    def test_handle_plugin_enable_propagates_app_errors(
        self, mock_dest, mock_transport, mock_app, plugin_config, authorized_identity
    ):
        from reticulumpi.builtin_plugins.remote_control import RemoteControlPlugin
        import RNS.vendor.umsgpack as umsgpack

        mock_app.enable_plugin.side_effect = RuntimeError("already running")
        plugin = RemoteControlPlugin(mock_app, plugin_config)
        plugin.start()

        payload = umsgpack.packb({"name": "sample"})
        result = plugin._handle_plugin_enable(
            "/plugin/enable", payload, None, None, authorized_identity, None
        )
        data = umsgpack.unpackb(result)
        assert data["ok"] is False
        assert "already running" in data.get("error", "")
        plugin.stop()

    @patch("RNS.Transport")
    @patch("RNS.Destination")
    def test_handle_plugin_enable_requires_auth(
        self, mock_dest, mock_transport, mock_app, plugin_config, unauthorized_identity
    ):
        from reticulumpi.builtin_plugins.remote_control import RemoteControlPlugin
        import RNS.vendor.umsgpack as umsgpack

        plugin = RemoteControlPlugin(mock_app, plugin_config)
        plugin.start()

        payload = umsgpack.packb({"name": "sample"})
        result = plugin._handle_plugin_enable(
            "/plugin/enable", payload, None, None, unauthorized_identity, None
        )
        data = umsgpack.unpackb(result)
        assert data["ok"] is False
        assert "unauthorized" in data.get("error", "")
        mock_app.enable_plugin.assert_not_called()
        plugin.stop()

    @patch("RNS.Transport")
    @patch("RNS.Destination")
    def test_handle_plugin_disable_success(
        self, mock_dest, mock_transport, mock_app, plugin_config, authorized_identity
    ):
        from reticulumpi.builtin_plugins.remote_control import RemoteControlPlugin
        import RNS.vendor.umsgpack as umsgpack

        plugin = RemoteControlPlugin(mock_app, plugin_config)
        plugin.start()

        payload = umsgpack.packb({"name": "sample"})
        result = plugin._handle_plugin_disable(
            "/plugin/disable", payload, None, None, authorized_identity, None
        )
        data = umsgpack.unpackb(result)
        assert data["ok"] is True
        assert data["message"] == "Plugin 'sample' disabled"
        mock_app.disable_plugin.assert_called_once_with("sample")
        plugin.stop()

    @patch("RNS.Transport")
    @patch("RNS.Destination")
    def test_handle_plugin_disable_propagates_app_errors(
        self, mock_dest, mock_transport, mock_app, plugin_config, authorized_identity
    ):
        from reticulumpi.builtin_plugins.remote_control import RemoteControlPlugin
        import RNS.vendor.umsgpack as umsgpack

        mock_app.disable_plugin.side_effect = KeyError("'not running'")
        plugin = RemoteControlPlugin(mock_app, plugin_config)
        plugin.start()

        payload = umsgpack.packb({"name": "sample"})
        result = plugin._handle_plugin_disable(
            "/plugin/disable", payload, None, None, authorized_identity, None
        )
        data = umsgpack.unpackb(result)
        assert data["ok"] is False
        assert "not running" in data.get("error", "")
        plugin.stop()
