"""Tests for dependency-free systemd readiness notification."""

from unittest.mock import MagicMock, patch

from reticulumpi.systemd_notify import notify, ready, set_readiness_file, stopping


def test_notify_is_noop_without_environment(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert notify("READY=1") is False


def test_notify_supports_abstract_socket(monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "@reticulumpi-test")
    channel = MagicMock()
    channel.__enter__.return_value = channel
    with patch("reticulumpi.systemd_notify.socket.socket", return_value=channel):
        assert ready("all plugins ready") is True
    channel.connect.assert_called_once_with("\0reticulumpi-test")
    channel.sendall.assert_called_once_with(b"READY=1\nSTATUS=all plugins ready")


def test_stopping_message(monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/test-notify.sock")
    channel = MagicMock()
    channel.__enter__.return_value = channel
    with patch("reticulumpi.systemd_notify.socket.socket", return_value=channel):
        assert stopping("cleaning up") is True
    channel.sendall.assert_called_once_with(b"STOPPING=1\nSTATUS=cleaning up")


def test_notify_failure_is_nonfatal(monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "/missing/socket")
    with patch("reticulumpi.systemd_notify.socket.socket", side_effect=OSError("missing")):
        assert notify("READY=1") is False


def test_readiness_file_lifecycle(tmp_path, monkeypatch):
    marker = tmp_path / "ready"
    monkeypatch.setenv("RETICULUMPI_READY_FILE", str(marker))

    assert set_readiness_file(True) is True
    assert marker.read_text() == "ready\n"
    assert marker.stat().st_mode & 0o777 == 0o644
    assert set_readiness_file(False) is True
    assert not marker.exists()
