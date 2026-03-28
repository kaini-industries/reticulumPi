"""Tests for the remote control client."""

from unittest.mock import MagicMock



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
