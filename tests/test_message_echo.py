"""Tests for the message_echo plugin's propagation node auto-selection."""

import os
from unittest.mock import MagicMock, patch, call

import pytest
import RNS
import RNS.vendor.umsgpack as umsgpack


@pytest.fixture
def echo_plugin(mock_app, tmp_path):
    """Create a MessageEcho plugin instance with mocked dependencies."""
    config = {
        "display_name": "Test Echo",
        "storage_path": str(tmp_path / "lxmf"),
    }
    import RNS as _RNS

    with (
        patch("LXMF.LXMRouter") as mock_router_cls,
        patch.object(_RNS.Transport, "register_announce_handler"),
        patch.object(_RNS.Transport, "deregister_announce_handler"),
    ):
        mock_router = MagicMock()
        mock_dest = MagicMock()
        mock_dest.hash = b"\x01" * 16
        mock_router.register_delivery_identity.return_value = mock_dest
        mock_router_cls.return_value = mock_router

        from reticulumpi.builtin_plugins.message_echo import MessageEcho

        plugin = MessageEcho(mock_app, config)
        plugin.start()
        yield plugin
        plugin.stop()


def _make_app_data(enabled=True, name="TestNode", active=True):
    """Create msgpack-encoded propagation node announce data."""
    return umsgpack.packb([enabled, name, active])


class TestPropagationAutoSelect:
    """Tests for _handle_propagation_announce."""

    def test_selects_active_node(self, echo_plugin):
        """Active propagation node should be selected."""
        node_hash = b"\xaa" * 16
        app_data = _make_app_data(active=True)

        with (
            patch.object(RNS.Transport, "hops_to", return_value=3),
            patch("LXMF.pn_announce_data_is_valid", return_value=True),
        ):
            echo_plugin._handle_propagation_announce(node_hash, MagicMock(), app_data)

        echo_plugin.lxmf_router.set_outbound_propagation_node.assert_called_once_with(
            node_hash
        )
        assert echo_plugin._best_propagation_hops == 3

    def test_picks_closer_node(self, echo_plugin):
        """A closer node should replace a farther one."""
        far_hash = b"\xaa" * 16
        near_hash = b"\xbb" * 16
        app_data = _make_app_data(active=True)

        with (
            patch.object(RNS.Transport, "hops_to", return_value=5),
            patch("LXMF.pn_announce_data_is_valid", return_value=True),
        ):
            echo_plugin._handle_propagation_announce(far_hash, MagicMock(), app_data)

        with (
            patch.object(RNS.Transport, "hops_to", return_value=2),
            patch("LXMF.pn_announce_data_is_valid", return_value=True),
        ):
            echo_plugin._handle_propagation_announce(near_hash, MagicMock(), app_data)

        # Should have been called twice, last with the nearer node
        calls = echo_plugin.lxmf_router.set_outbound_propagation_node.call_args_list
        assert calls[-1] == call(near_hash)
        assert echo_plugin._best_propagation_hops == 2

    def test_ignores_farther_node(self, echo_plugin):
        """A farther node should not replace a closer one."""
        near_hash = b"\xaa" * 16
        far_hash = b"\xbb" * 16
        app_data = _make_app_data(active=True)

        with (
            patch.object(RNS.Transport, "hops_to", return_value=2),
            patch("LXMF.pn_announce_data_is_valid", return_value=True),
        ):
            echo_plugin._handle_propagation_announce(near_hash, MagicMock(), app_data)

        with (
            patch.object(RNS.Transport, "hops_to", return_value=5),
            patch("LXMF.pn_announce_data_is_valid", return_value=True),
        ):
            echo_plugin._handle_propagation_announce(far_hash, MagicMock(), app_data)

        # Should have been called only once with the nearer node
        echo_plugin.lxmf_router.set_outbound_propagation_node.assert_called_once_with(
            near_hash
        )
        assert echo_plugin._best_propagation_hops == 2

    def test_ignores_inactive_node(self, echo_plugin):
        """Inactive propagation nodes should be ignored."""
        node_hash = b"\xaa" * 16
        app_data = _make_app_data(active=False)

        with (
            patch.object(RNS.Transport, "hops_to", return_value=1),
            patch("LXMF.pn_announce_data_is_valid", return_value=True),
        ):
            echo_plugin._handle_propagation_announce(node_hash, MagicMock(), app_data)

        echo_plugin.lxmf_router.set_outbound_propagation_node.assert_not_called()

    def test_ignores_none_app_data(self, echo_plugin):
        """None app_data should not crash."""
        echo_plugin._handle_propagation_announce(b"\xaa" * 16, MagicMock(), None)
        echo_plugin.lxmf_router.set_outbound_propagation_node.assert_not_called()

    def test_ignores_invalid_app_data(self, echo_plugin):
        """Invalid app_data should not crash."""
        with patch("LXMF.pn_announce_data_is_valid", return_value=False):
            echo_plugin._handle_propagation_announce(
                b"\xaa" * 16, MagicMock(), b"garbage"
            )
        echo_plugin.lxmf_router.set_outbound_propagation_node.assert_not_called()


class TestWriteNomadNetPeersettings:
    """Tests for _write_nomadnet_propagation_node."""

    def test_writes_to_existing_storage_dir(self, echo_plugin, tmp_path):
        """Should write peersettings when storage directory exists."""
        storage_dir = tmp_path / "nomadnet" / "storage"
        storage_dir.mkdir(parents=True)
        config_dir = str(tmp_path / "nomadnet")

        echo_plugin._NOMADNET_CONFIG_DIRS = [config_dir]
        node_hash = b"\xcc" * 16

        echo_plugin._write_nomadnet_propagation_node(node_hash)

        path = storage_dir / "peersettings"
        assert path.exists()
        settings = umsgpack.unpackb(path.read_bytes())
        assert settings["propagation_node"] == node_hash

    def test_preserves_existing_settings(self, echo_plugin, tmp_path):
        """Should merge with existing peersettings, not overwrite them."""
        storage_dir = tmp_path / "nomadnet" / "storage"
        storage_dir.mkdir(parents=True)
        config_dir = str(tmp_path / "nomadnet")

        # Write existing settings
        existing = {"some_key": "some_value", "propagation_node": b"\x00" * 16}
        (storage_dir / "peersettings").write_bytes(umsgpack.packb(existing))

        echo_plugin._NOMADNET_CONFIG_DIRS = [config_dir]
        new_hash = b"\xdd" * 16

        echo_plugin._write_nomadnet_propagation_node(new_hash)

        settings = umsgpack.unpackb((storage_dir / "peersettings").read_bytes())
        assert settings["propagation_node"] == new_hash
        assert settings["some_key"] == "some_value"

    def test_skips_missing_storage_dir(self, echo_plugin, tmp_path):
        """Should silently skip when storage directory doesn't exist."""
        echo_plugin._NOMADNET_CONFIG_DIRS = [str(tmp_path / "nonexistent")]
        # Should not raise
        echo_plugin._write_nomadnet_propagation_node(b"\xcc" * 16)
