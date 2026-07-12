"""Tests for the FileTransfer plugin."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import RNS

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


def test_validate_config_bad_access_policy(mock_app):
    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    with pytest.raises(ValueError, match="access_policy"):
        FileTransferPlugin(mock_app, {"access_policy": "sometimes"})


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

    link = MagicMock(link_id=b"resource-link")
    plugin._link_established(link)
    mock_resource = MagicMock()
    mock_resource.size = 1024  # 1KB, well under 10MB limit
    mock_resource.link = link

    assert plugin._resource_callback(mock_resource) is True
    plugin.stop()


@patch("RNS.Destination")
def test_resource_callback_rejects_over_limit(mock_dest, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    plugin = FileTransferPlugin(mock_app, plugin_config)
    plugin.start()

    link = MagicMock(link_id=b"resource-link")
    plugin._link_established(link)
    mock_resource = MagicMock()
    mock_resource.size = 20 * 1024 * 1024  # 20MB, over 10MB limit
    mock_resource.link = link

    assert plugin._resource_callback(mock_resource) is False
    plugin.stop()


@patch("RNS.Destination")
def test_resource_callback_rejects_when_auto_accept_false(mock_dest, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    plugin_config["auto_accept"] = False
    plugin = FileTransferPlugin(mock_app, plugin_config)
    plugin.start()

    link = MagicMock(link_id=b"resource-link")
    plugin._link_established(link)
    mock_resource = MagicMock()
    mock_resource.size = 100
    mock_resource.link = link

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
def test_received_file_is_atomically_private_and_never_follows_existing_link(
    mock_dest, mock_app, plugin_config, tmp_path
):
    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    plugin = FileTransferPlugin(mock_app, plugin_config)
    plugin.start()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"unchanged")
    (tmp_path / "shared" / "payload.txt").symlink_to(outside)
    resource = MagicMock()
    resource.data = MagicMock()
    resource.data.name = "payload.txt"
    data = b"received"
    resource.size = len(data)

    filename, path = plugin._store_received_file(resource, data)

    assert filename == "payload_1.txt"
    assert Path(path).read_bytes() == b"received"
    assert Path(path).stat().st_mode & 0o777 == 0o600
    assert outside.read_bytes() == b"unchanged"
    assert not list((tmp_path / "shared").glob(".reticulumpi-upload-*.tmp"))
    plugin.stop()


@patch("RNS.Destination")
def test_transfer_completes_only_after_durable_file_publication(mock_dest, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    plugin = FileTransferPlugin(mock_app, plugin_config)
    plugin.start()
    link = MagicMock(link_id=b"authorized")
    plugin._link_established(link)
    resource = MagicMock(status=RNS.Resource.COMPLETE, link=link, size=8)
    resource.data = MagicMock()
    resource.data.name = "incoming.bin"
    resource.data.read.return_value = b"received"

    plugin._resource_concluded(resource)

    assert plugin._transfers_completed == 1
    assert plugin._transfers_failed == 0
    assert Path(plugin._shared_dir, "incoming.bin").read_bytes() == b"received"
    plugin.stop()


@patch("RNS.Destination")
def test_transfer_persistence_failure_is_reported_as_failed(mock_dest, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    plugin = FileTransferPlugin(mock_app, plugin_config)
    plugin.start()
    link = MagicMock(link_id=b"authorized")
    plugin._link_established(link)
    resource = MagicMock(status=RNS.Resource.COMPLETE, link=link, size=8)
    resource.data = MagicMock()
    resource.data.name = "incoming.bin"
    resource.data.read.return_value = b"received"

    with patch.object(plugin, "_store_received_file", side_effect=OSError("disk failed")):
        plugin._resource_concluded(resource)

    assert plugin._transfers_completed == 0
    assert plugin._transfers_failed == 1
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
    assert status["access_policy"] == "open"  # legacy empty allowlist migration
    plugin.stop()


@patch("RNS.Destination")
def test_new_config_defaults_to_deny_and_closes_link(mock_dest, mock_app, tmp_path):
    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    plugin = FileTransferPlugin(
        mock_app,
        {"shared_dir": str(tmp_path / "shared"), "max_file_size_mb": 10},
    )
    plugin.start()
    link = MagicMock()
    link.link_id = b"denied"
    plugin._link_established(link)
    link.set_resource_strategy.assert_called_once_with(__import__("RNS").Link.ACCEPT_NONE)
    link.teardown.assert_called_once()
    plugin.stop()


@patch("RNS.Destination")
def test_allowlist_opens_only_after_remote_identity_is_verified(mock_dest, mock_app, plugin_config):
    import RNS

    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    allowed = b"\xaa" * 16
    plugin_config["access_policy"] = "allowlist"
    plugin_config["allowed_identities"] = [allowed.hex()]
    plugin = FileTransferPlugin(mock_app, plugin_config)
    plugin.start()
    link = MagicMock()
    link.link_id = b"authorized-link"
    plugin._link_established(link)
    assert link.set_resource_strategy.call_args_list == [
        __import__("unittest.mock").mock.call(RNS.Link.ACCEPT_NONE)
    ]

    identity = MagicMock()
    identity.hash = allowed
    plugin._check_identity(link, identity)
    assert link.set_resource_strategy.call_args_list[-1] == __import__("unittest.mock").mock.call(
        RNS.Link.ACCEPT_APP
    )

    resource = MagicMock(size=100, link=link)
    assert plugin._resource_callback(resource) is True
    plugin.stop()


@patch("RNS.Destination")
def test_allowlist_rechecks_requests_against_authorized_link(mock_dest, mock_app, plugin_config):
    import RNS.vendor.umsgpack as umsgpack

    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    allowed = b"\xbb" * 16
    plugin_config["access_policy"] = "allowlist"
    plugin_config["allowed_identities"] = [allowed.hex()]
    plugin = FileTransferPlugin(mock_app, plugin_config)
    plugin.start()

    response = plugin._handle_list("/list", b"", None, b"unknown", None, None)
    assert umsgpack.unpackb(response) == {"ok": False, "error": "unauthorized"}

    link = MagicMock()
    link.link_id = b"known"
    plugin._link_established(link)
    identity = MagicMock(hash=allowed)
    plugin._check_identity(link, identity)
    response = plugin._handle_list("/list", b"", None, b"known", identity, None)
    assert umsgpack.unpackb(response)["ok"] is True
    plugin.stop()


@patch("RNS.Destination")
def test_open_policy_still_requires_an_established_link(mock_dest, mock_app, plugin_config):
    import RNS.vendor.umsgpack as umsgpack

    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    plugin_config["access_policy"] = "open"
    plugin = FileTransferPlugin(mock_app, plugin_config)
    plugin.start()

    response = plugin._handle_list("/list", b"", None, b"unknown", None, None)
    assert umsgpack.unpackb(response) == {"ok": False, "error": "unauthorized"}

    link = MagicMock()
    link.link_id = b"known-open-link"
    plugin._link_established(link)
    response = plugin._handle_list("/list", b"", None, link.link_id, None, None)
    assert umsgpack.unpackb(response)["ok"] is True

    plugin._link_closed(link)
    response = plugin._handle_list("/list", b"", None, link.link_id, None, None)
    assert umsgpack.unpackb(response) == {"ok": False, "error": "unauthorized"}
    plugin.stop()


@patch("RNS.Destination")
def test_managed_cleanup_owns_file_destination_handlers_and_links(
    mock_destination_factory,
    mock_app,
    plugin_config,
):
    import RNS.vendor.umsgpack as umsgpack

    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    destination = MagicMock()
    destination.hash = b"\x20" * 16
    mock_destination_factory.return_value = destination
    plugin_config["access_policy"] = "open"
    plugin = FileTransferPlugin(mock_app, plugin_config)
    plugin.start()

    assert plugin.get_lifecycle_metrics()["rns_resources"] == {
        "links": 0,
        "destinations": 1,
        "request_handlers": 2,
    }
    link = MagicMock(link_id=b"managed-file-link")
    plugin._link_established(link)
    assert plugin.get_lifecycle_metrics()["rns_resources"]["links"] == 1

    plugin.stop()
    response = plugin._handle_list("/list", b"", None, link.link_id, None, None)
    assert umsgpack.unpackb(response) == {"ok": False, "error": "unauthorized"}
    plugin.cleanup_managed_resources()

    destination.deregister.assert_called_once_with()
    assert destination.deregister_request_handler.call_count == 2
    link.teardown.assert_called_once_with()
    assert plugin.get_lifecycle_metrics()["rns_resources"] == {
        "links": 0,
        "destinations": 0,
        "request_handlers": 0,
    }


@pytest.mark.parametrize(
    ("allowed", "message"),
    [
        ([123], "hex strings"),
        (["not-hex"], "valid hex"),
        ([""], "cannot be empty"),
    ],
)
def test_allowed_identity_entries_are_validated(mock_app, allowed, message):
    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    with pytest.raises(ValueError, match=message):
        FileTransferPlugin(mock_app, {"allowed_identities": allowed})


@patch("RNS.Destination")
def test_legacy_nonempty_allowlist_remains_allowlisted(mock_dest, mock_app, plugin_config):
    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    plugin_config["allowed_identities"] = [(b"\xcc" * 16).hex()]
    plugin = FileTransferPlugin(mock_app, plugin_config)
    plugin.start()
    assert plugin._access_policy == "allowlist"
    plugin.stop()


@patch("RNS.Destination")
def test_stop_isolates_link_and_destination_cleanup_errors(mock_dest, mock_app, plugin_config):
    import RNS

    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    plugin = FileTransferPlugin(mock_app, plugin_config)
    plugin.start()
    link = MagicMock(link_id=b"cleanup")
    link.teardown.side_effect = RuntimeError("link stuck")
    plugin._link_established(link)
    with patch.object(
        RNS.Transport,
        "deregister_destination",
        side_effect=RuntimeError("destination stuck"),
    ):
        plugin.stop()
    assert plugin.destination is None
    assert plugin._links == {}
    assert plugin._authorized_links == set()


@patch("RNS.Destination")
def test_unauthorized_resource_lifecycle_is_rejected_and_cancelled(mock_dest, mock_app, tmp_path):
    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    plugin = FileTransferPlugin(
        mock_app,
        {
            "access_policy": "deny",
            "shared_dir": str(tmp_path / "shared"),
            "max_file_size_mb": 10,
        },
    )
    plugin.start()
    assert plugin._link_key(bytearray(b"link")) == b"link"
    assert plugin._is_authorized_link(b"missing") is False

    resource = MagicMock(size=100, link=MagicMock(link_id=b"missing"))
    assert plugin._resource_callback(resource) is False
    plugin._resource_started(resource)
    resource.cancel.assert_called_once_with()
    plugin._resource_concluded(resource)
    assert plugin._current_transfers == {}
    assert plugin._transfers_completed == 0
    plugin.stop()


@patch("RNS.Destination")
def test_info_handler_rechecks_authorization_and_rejects_bad_payloads(
    mock_dest, mock_app, plugin_config
):
    import RNS.vendor.umsgpack as umsgpack

    from reticulumpi.builtin_plugins.file_transfer import FileTransferPlugin

    plugin_config["access_policy"] = "open"
    plugin = FileTransferPlugin(mock_app, plugin_config)
    plugin.start()

    unauthorized = plugin._handle_info("/info", b"", None, b"unknown", None, None)
    assert umsgpack.unpackb(unauthorized) == {"ok": False, "error": "unauthorized"}

    link = MagicMock(link_id=b"known")
    plugin._link_established(link)
    invalid_wire = plugin._handle_info("/info", b"\xc1", None, link.link_id, None, None)
    assert umsgpack.unpackb(invalid_wire) == {"ok": False, "error": "invalid request"}
    invalid_name = plugin._handle_info(
        "/info",
        umsgpack.packb({"name": ""}),
        None,
        link.link_id,
        None,
        None,
    )
    assert umsgpack.unpackb(invalid_name) == {"ok": False, "error": "invalid filename"}
    plugin.stop()
