"""Tests for the FileTransfer plugin."""

import os
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi.event_bus import EventBus


@pytest.fixture
def mock_app(tmp_path):
    app = MagicMock()
    app.reticulum = MagicMock()
    app.identity = MagicMock()
    app.identity.hash = b"\x01" * 16
    app.event_bus = EventBus()
    app.plugins = {}
    app.node_name = "TestNode"
    return app


@pytest.fixture
def plugin_config(tmp_path):
    return {
        "enabled": True,
        "shared_dir": str(tmp_path / "shared"),
        "max_file_size_mb": 10,
        "allowed_identities": [],
        "auto_accept": True,
    }


@patch("RNS.Destination")
def test_start_stop(mock_dest, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    plugin = FileTransferPlugin(mock_app, plugin_config)
    plugin.start()
    assert plugin._active is True
    assert os.path.isdir(plugin._shared_dir)
    plugin.stop()
    assert plugin._active is False


def test_validate_config_bad_max_size(mock_app):
    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    with pytest.raises(ValueError, match="max_file_size_mb"):
        FileTransferPlugin(mock_app, {"max_file_size_mb": 0})


def test_validate_config_bad_allowed_identities(mock_app):
    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    with pytest.raises(ValueError, match="allowed_identities"):
        FileTransferPlugin(mock_app, {"allowed_identities": "not-a-list"})


@patch("RNS.Destination")
def test_list_shared_files_empty(mock_dest, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    plugin = FileTransferPlugin(mock_app, plugin_config)
    plugin.start()
    files = plugin.get_shared_files()
    assert files == []
    plugin.stop()


@patch("RNS.Destination")
def test_list_shared_files_with_content(mock_dest, mock_app, plugin_config, tmp_path):
    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    plugin = FileTransferPlugin(mock_app, plugin_config)
    plugin.start()

    # Create a test file in the shared dir
    test_file = os.path.join(plugin._shared_dir, "test.txt")
    with open(test_file, "w") as f:
        f.write("hello world")

    files = plugin.get_shared_files()
    assert len(files) == 1
    assert files[0]["name"] == "test.txt"
    assert files[0]["size"] == 11
    plugin.stop()


@patch("RNS.Destination")
def test_resource_callback_accepts_within_limit(mock_dest, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    plugin = FileTransferPlugin(mock_app, plugin_config)
    plugin.start()

    mock_resource = MagicMock()
    mock_resource.size = 1024  # 1KB, well under 10MB limit

    assert plugin._resource_callback(mock_resource) is True
    plugin.stop()


@patch("RNS.Destination")
def test_resource_callback_rejects_over_limit(mock_dest, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    plugin = FileTransferPlugin(mock_app, plugin_config)
    plugin.start()

    mock_resource = MagicMock()
    mock_resource.size = 20 * 1024 * 1024  # 20MB, over 10MB limit

    assert plugin._resource_callback(mock_resource) is False
    plugin.stop()


@patch("RNS.Destination")
def test_resource_callback_rejects_when_auto_accept_false(mock_dest, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    plugin_config["auto_accept"] = False
    plugin = FileTransferPlugin(mock_app, plugin_config)
    plugin.start()

    mock_resource = MagicMock()
    mock_resource.size = 100

    assert plugin._resource_callback(mock_resource) is False
    plugin.stop()


@patch("RNS.Destination")
def test_safe_filename_no_overwrite(mock_dest, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    plugin = FileTransferPlugin(mock_app, plugin_config)
    plugin.start()

    # Create a conflicting file
    os.makedirs(plugin._shared_dir, exist_ok=True)
    with open(os.path.join(plugin._shared_dir, "test.txt"), "w") as f:
        f.write("existing")

    mock_resource = MagicMock()
    mock_resource.data = MagicMock()
    mock_resource.data.name = "test.txt"
    mock_resource.size = 100

    name = plugin._safe_filename(mock_resource)
    assert name == "test_1.txt"
    plugin.stop()


@patch("RNS.Destination")
def test_get_status(mock_dest, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    plugin = FileTransferPlugin(mock_app, plugin_config)
    plugin.start()
    status = plugin.get_status()
    assert status["active"] is True
    assert status["transfers_completed"] == 0
    assert status["shared_files"] == 0
    plugin.stop()
