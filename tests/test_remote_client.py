"""Tests for the remote control client."""

from unittest.mock import MagicMock, patch

import pytest


def test_identity_bytes_are_validated_before_reticulum_initializes():
    from reticulumpi.remote_client import RemoteClient

    events = []
    identity = object()

    def load_identity(payload):
        events.append(("identity", payload))
        return identity

    def initialize_reticulum(**kwargs):
        events.append(("reticulum", kwargs))
        return object()

    with (
        patch("reticulumpi.remote_client.RNS.Identity.from_bytes", side_effect=load_identity),
        patch("reticulumpi.remote_client.RNS.Reticulum", side_effect=initialize_reticulum),
    ):
        client = RemoteClient("ab" * 16, identity_bytes=b"private identity", output=None)

    assert client.identity is identity
    assert events[0] == ("identity", b"private identity")
    assert events[1][0] == "reticulum"
    assert callable(events[1][1]["logdest"])


def test_invalid_identity_bytes_never_initialize_reticulum():
    from reticulumpi.remote_client import RemoteClient

    with (
        patch("reticulumpi.remote_client.RNS.Identity.from_bytes", return_value=None),
        patch("reticulumpi.remote_client.RNS.Reticulum") as reticulum,
        pytest.raises(ValueError, match="not a valid RNS private identity"),
    ):
        RemoteClient("ab" * 16, identity_bytes=b"invalid")

    reticulum.assert_not_called()


def test_non_bytes_identity_never_initializes_reticulum():
    from reticulumpi.remote_client import RemoteClient

    with (
        patch("reticulumpi.remote_client.RNS.Reticulum") as reticulum,
        pytest.raises(TypeError, match="identity_bytes must be bytes"),
    ):
        RemoteClient("ab" * 16, identity_bytes=bytearray(b"identity"))

    reticulum.assert_not_called()


def test_identity_decoder_failure_never_initializes_reticulum():
    from reticulumpi.remote_client import RemoteClient

    with (
        patch(
            "reticulumpi.remote_client.RNS.Identity.from_bytes",
            side_effect=RuntimeError("decoder detail"),
        ),
        patch("reticulumpi.remote_client.RNS.Reticulum") as reticulum,
        pytest.raises(ValueError, match="not a valid RNS private identity"),
    ):
        RemoteClient("ab" * 16, identity_bytes=b"invalid")

    reticulum.assert_not_called()


def test_identity_sources_are_mutually_exclusive_before_reticulum_initializes():
    from reticulumpi.remote_client import RemoteClient

    with (
        patch("reticulumpi.remote_client.RNS.Reticulum") as reticulum,
        pytest.raises(ValueError, match="mutually exclusive"),
    ):
        RemoteClient("ab" * 16, identity_path="identity", identity_bytes=b"identity")

    reticulum.assert_not_called()


def test_connect_reports_invalid_destination_through_injected_output():
    from reticulumpi.remote_client import RemoteClient

    messages = []
    with (
        patch("reticulumpi.remote_client.RNS.Reticulum"),
        patch("reticulumpi.remote_client.RNS.Identity"),
    ):
        client = RemoteClient("not-a-destination", output=messages.append)

    assert client.connect() is False
    assert messages == ["Error: invalid destination hash: not-a-destination"]


def test_connect_reports_path_request_timeout_through_injected_output():
    from reticulumpi.remote_client import RemoteClient

    messages = []
    with (
        patch("reticulumpi.remote_client.RNS.Reticulum"),
        patch("reticulumpi.remote_client.RNS.Identity"),
        patch("reticulumpi.remote_client.RNS.Transport.has_path", return_value=False),
        patch("reticulumpi.remote_client.RNS.Transport.request_path") as request_path,
        patch("reticulumpi.remote_client.time.monotonic", side_effect=[10.0, 11.0]),
    ):
        client = RemoteClient("ab" * 16, timeout=0, output=messages.append)
        assert client.connect() is False

    request_path.assert_called_once_with(bytes.fromhex("ab" * 16))
    assert messages[0].startswith("Requesting path to ")
    assert messages[-1] == "Error: path request timed out"


def test_connect_reports_missing_remote_identity_through_injected_output():
    from reticulumpi.remote_client import RemoteClient

    messages = []
    with (
        patch("reticulumpi.remote_client.RNS.Reticulum"),
        patch("reticulumpi.remote_client.RNS.Identity") as identity_factory,
        patch("reticulumpi.remote_client.RNS.Transport.has_path", return_value=True),
    ):
        identity_factory.recall.return_value = None
        client = RemoteClient("ab" * 16, output=messages.append)
        assert client.connect() is False

    assert messages == [
        "Error: could not recall identity for <abababababababababababababababab>. "
        "The identity may not have been announced yet."
    ]


@pytest.mark.parametrize("closed", [True, False])
def test_connect_reports_link_establishment_failure_through_injected_output(closed):
    from reticulumpi.remote_client import RemoteClient

    messages = []
    link = MagicMock()
    with (
        patch("reticulumpi.remote_client.RNS.Reticulum"),
        patch("reticulumpi.remote_client.RNS.Identity") as identity_factory,
        patch("reticulumpi.remote_client.RNS.Transport.has_path", return_value=True),
        patch("reticulumpi.remote_client.RNS.Destination"),
        patch("reticulumpi.remote_client.RNS.Link", return_value=link),
    ):
        identity_factory.recall.return_value = object()
        client = RemoteClient("ab" * 16, timeout=0, output=messages.append)
        if closed:
            client._link_closed.set()
        assert client.connect() is False

    expected = (
        "Error: link was closed before establishment"
        if closed
        else "Error: link establishment timed out"
    )
    assert messages[-1] == expected


def test_connect_reports_link_rejection_through_injected_output():
    from reticulumpi.remote_client import RemoteClient

    messages = []
    link = MagicMock()
    with (
        patch("reticulumpi.remote_client.RNS.Reticulum"),
        patch("reticulumpi.remote_client.RNS.Identity") as identity_factory,
        patch("reticulumpi.remote_client.RNS.Transport.has_path", return_value=True),
        patch("reticulumpi.remote_client.RNS.Destination"),
        patch("reticulumpi.remote_client.RNS.Link", return_value=link),
        patch("reticulumpi.remote_client.time.sleep"),
    ):
        identity_factory.recall.return_value = object()
        client = RemoteClient("ab" * 16, output=messages.append)
        client._link_ready.set()
        link.identify.side_effect = lambda _identity: client._link_closed.set()
        assert client.connect() is False

    assert messages[-1] == "Error: link closed after identification (likely unauthorized)"


def test_quiet_client_emits_no_destination_or_config_path(capsys):
    from reticulumpi.remote_client import RemoteClient

    destination = "ab" * 16
    config_path = "/private/lab/rns"
    identity = object()
    link = MagicMock()
    with (
        patch("reticulumpi.remote_client.RNS.Identity.from_bytes", return_value=identity),
        patch("reticulumpi.remote_client.RNS.Reticulum"),
        patch("reticulumpi.remote_client.RNS.Transport.has_path", return_value=True),
        patch("reticulumpi.remote_client.RNS.Identity.recall", return_value=object()),
        patch("reticulumpi.remote_client.RNS.Destination"),
        patch("reticulumpi.remote_client.RNS.Link", return_value=link),
        patch("reticulumpi.remote_client.time.sleep"),
    ):
        client = RemoteClient(
            destination,
            reticulum_config_dir=config_path,
            identity_bytes=b"private identity",
            output=None,
        )
        client._link_ready.set()
        assert client.connect() is True

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert destination not in captured.out
    assert config_path not in captured.out
    link.identify.assert_called_once_with(identity)


def test_format_response():
    from reticulumpi.remote_client import _format_response

    data = {"name": "TestNode", "version": "0.2.0"}
    output = _format_response(data)
    assert "name: TestNode" in output
    assert "version: 0.2.0" in output


def test_format_response_nested():
    from reticulumpi.remote_client import _format_response

    data = {"node": {"name": "Test", "uptime": 100}}
    output = _format_response(data)
    assert "node:" in output
    assert "  name: Test" in output


def test_format_response_list():
    from reticulumpi.remote_client import _format_response

    data = {"items": ["a", "b"]}
    output = _format_response(data)
    assert "items:" in output
    assert "  - a" in output
    assert "  - b" in output


def test_simple_commands_mapping():
    from reticulumpi.remote_client import SIMPLE_COMMANDS

    assert "ping" in SIMPLE_COMMANDS
    assert "status" in SIMPLE_COMMANDS
    assert "metrics" in SIMPLE_COMMANDS
    assert "plugins" in SIMPLE_COMMANDS
    assert "interfaces" in SIMPLE_COMMANDS
    assert "config" in SIMPLE_COMMANDS
    assert "logs" in SIMPLE_COMMANDS
    assert "announce" in SIMPLE_COMMANDS


def test_run_single_command_unknown():
    from reticulumpi.remote_client import run_single_command

    client = MagicMock()
    rc = run_single_command(client, "nonexistent")
    assert rc == 1


def test_run_single_command_ping_success():
    from reticulumpi.remote_client import run_single_command
    import time

    client = MagicMock()
    client.request.return_value = {"ok": True, "node": "TestNode", "time": time.time()}

    rc = run_single_command(client, "ping")
    assert rc == 0
    client.request.assert_called_once_with("/ping", data=None)


def test_run_single_command_ping_timeout():
    from reticulumpi.remote_client import run_single_command

    client = MagicMock()
    client.request.return_value = None

    rc = run_single_command(client, "ping")
    assert rc == 1


def test_run_single_command_ping_error():
    from reticulumpi.remote_client import run_single_command

    client = MagicMock()
    client.request.return_value = {"ok": False, "error": "test error"}

    rc = run_single_command(client, "ping")
    assert rc == 1


def test_run_single_command_enable_success():
    from reticulumpi.remote_client import run_single_command

    client = MagicMock()
    client.request.return_value = {"ok": True, "message": "Plugin 'test' enabled"}

    rc = run_single_command(client, "enable", "test")
    assert rc == 0
    client.request.assert_called_once_with("/plugin/enable", {"name": "test"})


def test_run_single_command_enable_no_args():
    from reticulumpi.remote_client import run_single_command

    client = MagicMock()
    rc = run_single_command(client, "enable", "")
    assert rc == 1


def test_run_single_command_disable_success():
    from reticulumpi.remote_client import run_single_command

    client = MagicMock()
    client.request.return_value = {"ok": True, "message": "Plugin 'test' disabled"}

    rc = run_single_command(client, "disable", "test")
    assert rc == 0


def test_run_single_command_logs_with_count():
    from reticulumpi.remote_client import run_single_command

    client = MagicMock()
    client.request.return_value = {"ok": True, "data": []}

    rc = run_single_command(client, "logs", "50")
    assert rc == 0
    client.request.assert_called_once_with("/logs", data={"count": 50})


def test_run_single_command_status_with_data():
    from reticulumpi.remote_client import run_single_command

    client = MagicMock()
    client.request.return_value = {
        "ok": True,
        "data": {"version": "0.2.0", "plugins": {"test": {"active": True}}},
    }

    rc = run_single_command(client, "status")
    assert rc == 0
