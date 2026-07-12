"""Tests for the fixed privileged control protocol."""

import json
import socket
import struct
from pathlib import Path

from unittest.mock import MagicMock, patch

import pytest

from reticulumpi import control_broker
from reticulumpi.control_broker import BrokerError, handle_request
from reticulumpi.control_client import ControlError, request_control


def test_unknown_operation_is_rejected():
    with pytest.raises(BrokerError, match="unsupported operation"):
        handle_request({"operation": "run_shell", "arguments": []})


@pytest.mark.parametrize(
    "arguments",
    [
        ["activate", "wlan0;touch /tmp/pwn", "8080", "10.0.0.1"],
        ["activate", "wlan0", "1", "10.0.0.1"],
        ["activate", "wlan0", "8080", "::1"],
        ["cleanup", "extra"],
    ],
)
def test_captive_arguments_are_strict(arguments):
    with pytest.raises(BrokerError):
        handle_request({"operation": "captive_portal", "arguments": arguments})


def test_captive_command_uses_root_owned_helper():
    completed = MagicMock(returncode=0, stdout="activated\n", stderr="")
    with patch("reticulumpi.control_broker.subprocess.run", return_value=completed) as run:
        response = handle_request(
            {
                "operation": "captive_portal",
                "arguments": ["activate", "wlan0", "8080", "10.0.0.1"],
            }
        )
    assert response["ok"] is True
    assert run.call_args.args[0] == [
        "/usr/libexec/reticulumpi/captive_portal_helper.sh",
        "activate",
        "wlan0",
        "8080",
        "10.0.0.1",
    ]
    assert run.call_args.kwargs["cwd"] == "/"


def test_restart_has_no_caller_arguments():
    with pytest.raises(BrokerError, match="does not accept"):
        handle_request({"operation": "restart_rnsd", "arguments": ["other.service"]})


@pytest.mark.parametrize(
    "arguments",
    [
        ["configure", "0", "1e-1\nserver attacker", "0", "0.2", "-", "1e-9"],
        ["configure", "99", "1e-1", "0", "0.2", "-", "1e-9"],
        ["configure", "0", "1e-1", "0", "0.2", "/tmp/pps", "1e-9"],
        ["remove", "extra"],
    ],
)
def test_chrony_arguments_are_strict(arguments):
    with pytest.raises(BrokerError):
        handle_request({"operation": "chrony", "arguments": arguments})


def test_chrony_command_uses_root_owned_helper():
    completed = MagicMock(returncode=0, stdout="configured\n", stderr="")
    with patch("reticulumpi.control_broker.subprocess.run", return_value=completed) as run:
        response = handle_request(
            {
                "operation": "chrony",
                "arguments": ["configure", "0", "1e-1", "0.0", "0.2", "/dev/pps0", "1e-9"],
            }
        )
    assert response["ok"] is True
    assert run.call_args.args[0] == [
        "/usr/libexec/reticulumpi/chrony_helper.sh",
        "configure",
        "0",
        "1e-1",
        "0.0",
        "0.2",
        "/dev/pps0",
        "1e-9",
    ]


@pytest.mark.parametrize("arguments", [[], ["enable"], ["on", "extra"]])
def test_offline_arguments_are_strict(arguments):
    with pytest.raises(BrokerError, match="offline requires"):
        handle_request({"operation": "offline", "arguments": arguments})


def test_offline_command_uses_root_owned_helper():
    completed = MagicMock(returncode=0, stdout="active\n", stderr="")
    with patch("reticulumpi.control_broker.subprocess.run", return_value=completed) as run:
        response = handle_request({"operation": "offline", "arguments": ["status"]})
    assert response["ok"] is True
    assert run.call_args.args[0] == [
        "/usr/libexec/reticulumpi/simulate_offline.sh",
        "status",
    ]


def test_root_offline_status_never_reads_service_owned_marker_contents():
    helper = (Path(__file__).parents[1] / "scripts" / "simulate_offline.sh").read_text(
        encoding="utf-8"
    )
    status_body = helper.split("cmd_status() {", 1)[1].split("\n}", 1)[0]

    assert '$(<"$STATE_FILE")' not in status_body
    assert 'cat "$STATE_FILE"' not in status_body
    assert "Active since:" not in status_body
    assert 'if [ -L "$STATE_FILE" ]' in status_body
    assert "State marker: unsafe (ignored)" in status_body


def test_captive_helper_uses_root_only_atomic_state_and_config_writes():
    helper = (Path(__file__).parents[1] / "scripts" / "captive_portal_helper.sh").read_text(
        encoding="utf-8"
    )
    assert 'STATE_FILE="/var/backups/reticulumpi/admin/captive_portal.active"' in helper
    assert '> "$STATE_FILE"' not in helper
    assert '> "$DNSMASQ_CONF"' not in helper
    assert '| atomic_write "$STATE_FILE" 0600' in helper
    assert '| atomic_write "$DNSMASQ_CONF" 0644' in helper
    assert 'mv -fT -- "$temporary" "$destination"' in helper


def test_client_rejects_oversized_request(tmp_path):
    with pytest.raises(ControlError, match="exceeds"):
        request_control("captive_portal", ["x" * 5000], socket_path=tmp_path / "none")


@pytest.mark.parametrize(
    ("operation", "arguments", "command"),
    [
        ("restart_rnsd", [], ["/usr/bin/systemctl", "restart", "rnsd.service"]),
        (
            "restart_reticulumpi",
            [],
            [
                "/usr/bin/systemd-run",
                "--quiet",
                "--collect",
                "--unit=reticulumpi-delayed-restart",
                "--on-active=2s",
                "/usr/bin/systemctl",
                "restart",
                "reticulumpi.service",
            ],
        ),
        (
            "restart_services",
            [],
            ["/usr/libexec/reticulumpi/restart_services.sh"],
        ),
        (
            "captive_portal",
            ["deactivate"],
            ["/usr/libexec/reticulumpi/captive_portal_helper.sh", "deactivate"],
        ),
        (
            "chrony",
            ["online"],
            ["/usr/libexec/reticulumpi/chrony_helper.sh", "online"],
        ),
    ],
)
def test_enumerated_commands_are_exact_and_use_a_fixed_environment(operation, arguments, command):
    completed = MagicMock(returncode=0, stdout="ok\n", stderr="")
    with patch("reticulumpi.control_broker.subprocess.run", return_value=completed) as run:
        response = handle_request({"operation": operation, "arguments": arguments})

    assert response == {"ok": True, "operation": operation, "output": "ok"}
    assert run.call_args.args == (command,)
    assert run.call_args.kwargs["env"] == control_broker.SAFE_ENV
    assert run.call_args.kwargs["stdin"] is control_broker.subprocess.DEVNULL
    assert run.call_args.kwargs["check"] is False


@pytest.mark.parametrize(
    ("operation", "arguments", "message"),
    [
        ("captive_portal", [], "requires an action"),
        ("captive_portal", ["activate"], "activate requires"),
        (
            "captive_portal",
            ["activate", "wlan0", "not-a-port", "10.0.0.1"],
            "invalid captive portal port",
        ),
        (
            "captive_portal",
            ["activate", "wlan0", "8080", "not-an-address"],
            "invalid gateway address",
        ),
        ("chrony", [], "requires an action"),
        ("chrony", ["configure"], "configure requires"),
        (
            "chrony",
            ["configure", "bad", "-1", "0", "0", "-", "-1"],
            "invalid chrony SHM",
        ),
    ],
)
def test_validator_rejections_are_specific(operation, arguments, message):
    with pytest.raises(BrokerError, match=message):
        handle_request({"operation": operation, "arguments": arguments})


@pytest.mark.parametrize(
    ("stderr", "returncode", "message"),
    [("helper denied\n", 3, "helper denied"), ("", 9, "operation exited 9")],
)
def test_helper_failure_is_bounded_and_reported(stderr, returncode, message):
    completed = MagicMock(returncode=returncode, stdout="", stderr=stderr)
    with (
        patch("reticulumpi.control_broker.subprocess.run", return_value=completed),
        pytest.raises(BrokerError, match=message),
    ):
        handle_request({"operation": "offline", "arguments": ["status"]})


class _ReceiveSocket:
    def __init__(self, blocks):
        self.blocks = iter(blocks)
        self.sent = b""
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value

    def recv(self, _size):
        return next(self.blocks, b"")

    def sendall(self, value):
        self.sent += value


def test_request_reader_accepts_chunked_bounded_json():
    connection = _ReceiveSocket([b'{"operation":"offline",', b'"arguments":["status"]}\nignored'])
    assert control_broker._read_request(connection) == {
        "operation": "offline",
        "arguments": ["status"],
    }
    assert connection.timeout == control_broker.CONTROL_READ_TIMEOUT


def test_request_reader_rejects_client_that_holds_connection_open():
    connection = MagicMock()
    connection.recv.side_effect = TimeoutError("held open")
    with pytest.raises(BrokerError, match="read timed out"):
        control_broker._read_request(connection)
    connection.settimeout.assert_called_once_with(control_broker.CONTROL_READ_TIMEOUT)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"", "invalid JSON"),
        (b"not-json\n", "invalid JSON"),
        (b"[]\n", "contain only"),
        (b'{"operation":1,"arguments":[]}\n', "invalid request types"),
        (b'{"operation":"offline","arguments":[1]}\n', "short strings"),
        (
            json.dumps({"operation": "offline", "arguments": ["x" * 257]}).encode() + b"\n",
            "short strings",
        ),
    ],
)
def test_request_reader_rejects_malformed_schema(payload, message):
    with pytest.raises(BrokerError, match=message):
        control_broker._read_request(_ReceiveSocket([payload]))


def test_request_reader_rejects_oversized_wire_message():
    connection = _ReceiveSocket([b"x" * 1024] * 5)
    with pytest.raises(BrokerError, match="exceeds"):
        control_broker._read_request(connection)


def test_peer_credentials_are_required_and_decoded(monkeypatch):
    monkeypatch.delattr(control_broker.socket, "SO_PEERCRED", raising=False)
    with pytest.raises(BrokerError, match="unavailable"):
        control_broker._peer_uid(MagicMock())

    monkeypatch.setattr(control_broker.socket, "SO_PEERCRED", 17, raising=False)
    connection = MagicMock()
    connection.getsockopt.return_value = struct.pack("3i", 123, 456, 789)
    assert control_broker._peer_uid(connection) == 456


def test_serve_connection_accepts_expected_peer_and_serializes_success():
    connection = _ReceiveSocket([])
    with (
        patch("reticulumpi.control_broker._peer_uid", return_value=10001),
        patch(
            "reticulumpi.control_broker._read_request",
            return_value={"operation": "offline", "arguments": ["status"]},
        ),
        patch(
            "reticulumpi.control_broker.handle_request",
            return_value={"ok": True, "operation": "offline", "output": "active"},
        ),
    ):
        assert control_broker.serve_connection(connection, expected_uid=10001) == 0
    assert json.loads(connection.sent) == {
        "ok": True,
        "operation": "offline",
        "output": "active",
    }


def test_serve_connection_rejects_wrong_peer_and_handles_lookup_errors():
    for expected_uid, peer_uid, error in ((10001, 10002, "service user"), (None, 10001, "missing")):
        connection = _ReceiveSocket([])
        patches = [patch("reticulumpi.control_broker._peer_uid", return_value=peer_uid)]
        if expected_uid is None:
            patches.append(
                patch("reticulumpi.control_broker.pwd.getpwnam", side_effect=KeyError("missing"))
            )
        with patches[0]:
            if len(patches) == 2:
                with patches[1]:
                    status = control_broker.serve_connection(connection, expected_uid=expected_uid)
            else:
                status = control_broker.serve_connection(connection, expected_uid=expected_uid)
        assert status == 1
        assert error in json.loads(connection.sent)["error"]


def test_broker_main_closes_systemd_socket():
    connection = MagicMock()
    with (
        patch("reticulumpi.control_broker.socket.fromfd", return_value=connection) as fromfd,
        patch("reticulumpi.control_broker.serve_connection", return_value=7),
    ):
        assert control_broker.main() == 7
    fromfd.assert_called_once_with(0, socket.AF_UNIX, socket.SOCK_STREAM)
    connection.close.assert_called_once_with()


def _client_socket(*blocks):
    client = MagicMock()
    client.__enter__.return_value = client
    client.recv.side_effect = [*blocks, b""]
    return client


def test_control_client_round_trip_uses_bounded_unix_protocol(tmp_path):
    client = _client_socket(b'{"ok":true,', b'"output":"active"}\n')
    with patch("reticulumpi.control_client.socket.socket", return_value=client) as factory:
        response = request_control(
            "offline",
            ["status"],
            socket_path=tmp_path / "control.sock",
            timeout=2.5,
        )

    assert response == {"ok": True, "output": "active"}
    factory.assert_called_once_with(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout.assert_called_once_with(2.5)
    client.connect.assert_called_once_with(str(tmp_path / "control.sock"))
    assert json.loads(client.sendall.call_args.args[0]) == {
        "operation": "offline",
        "arguments": ["status"],
    }


@pytest.mark.parametrize(
    ("blocks", "message"),
    [
        ((b"not-json",), "invalid response"),
        ((b"not-json\n",), "invalid response"),
        ((b'{"ok":false,"error":"denied"}\n',), "denied"),
        ((b"[]\n",), "control operation failed"),
    ],
)
def test_control_client_rejects_invalid_or_failed_responses(tmp_path, blocks, message):
    with (
        patch("reticulumpi.control_client.socket.socket", return_value=_client_socket(*blocks)),
        pytest.raises(ControlError, match=message),
    ):
        request_control("offline", ["status"], socket_path=tmp_path / "control.sock")


def test_control_client_rejects_socket_and_oversized_response(tmp_path):
    client = _client_socket(b"x" * 1024, b"x" * 1024, b"x" * 1024, b"x" * 1024, b"x")
    with (
        patch("reticulumpi.control_client.socket.socket", return_value=client),
        pytest.raises(ControlError, match="response exceeds"),
    ):
        request_control("offline", socket_path=tmp_path / "control.sock")

    with (
        patch("reticulumpi.control_client.socket.socket", side_effect=OSError("refused")),
        pytest.raises(ControlError, match="unavailable: refused"),
    ):
        request_control("offline", socket_path=tmp_path / "control.sock")
