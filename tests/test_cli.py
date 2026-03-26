"""Tests for the CLI entry point."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi.cli import _RNS_TO_LOGGING, main


def test_rns_to_logging_mapping():
    assert _RNS_TO_LOGGING[0] == logging.CRITICAL
    assert _RNS_TO_LOGGING[1] == logging.ERROR
    assert _RNS_TO_LOGGING[2] == logging.WARNING
    assert _RNS_TO_LOGGING[4] == logging.INFO
    assert _RNS_TO_LOGGING[7] == logging.DEBUG


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc_info:
        with patch("sys.argv", ["reticulumpi", "--version"]):
            main()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "reticulumpi" in captured.out


def test_main_starts_app(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "reticulumpi:\n  log_level: 4\n  plugins: {}\n"
    )
    mock_app = MagicMock()
    with (
        patch("sys.argv", ["reticulumpi", "--config", str(config_file)]),
        patch("reticulumpi.cli.ReticulumPiApp", return_value=mock_app) as mock_cls,
    ):
        main()

    mock_cls.assert_called_once_with(
        config_path=str(config_file),
        reticulum_config_dir=None,
        log_level_override=None,
    )
    mock_app.start.assert_called_once()


def test_main_keyboard_interrupt_calls_shutdown(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "reticulumpi:\n  log_level: 4\n  plugins: {}\n"
    )
    mock_app = MagicMock()
    mock_app.start.side_effect = KeyboardInterrupt
    with (
        patch("sys.argv", ["reticulumpi", "--config", str(config_file)]),
        patch("reticulumpi.cli.ReticulumPiApp", return_value=mock_app),
    ):
        main()

    mock_app.shutdown.assert_called_once()


def test_main_fatal_error_exits(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "reticulumpi:\n  log_level: 4\n  plugins: {}\n"
    )
    mock_app = MagicMock()
    mock_app.start.side_effect = RuntimeError("boom")
    with pytest.raises(SystemExit) as exc_info:
        with (
            patch("sys.argv", ["reticulumpi", "--config", str(config_file)]),
            patch("reticulumpi.cli.ReticulumPiApp", return_value=mock_app),
        ):
            main()
    assert exc_info.value.code == 1


def test_main_log_level_override(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "reticulumpi:\n  log_level: 4\n  plugins: {}\n"
    )
    mock_app = MagicMock()
    with (
        patch("sys.argv", ["reticulumpi", "--config", str(config_file), "--log-level", "7"]),
        patch("reticulumpi.cli.ReticulumPiApp", return_value=mock_app) as mock_cls,
    ):
        main()

    mock_cls.assert_called_once_with(
        config_path=str(config_file),
        reticulum_config_dir=None,
        log_level_override=7,
    )


def test_main_default_config_path(tmp_path, monkeypatch):
    """When no --config is given and the default file doesn't exist, config_path is None."""
    mock_app = MagicMock()
    # Ensure default path doesn't exist
    monkeypatch.setenv("HOME", str(tmp_path))
    with (
        patch("sys.argv", ["reticulumpi"]),
        patch("reticulumpi.cli.ReticulumPiApp", return_value=mock_app) as mock_cls,
    ):
        main()

    mock_cls.assert_called_once()
    call_kwargs = mock_cls.call_args[1]
    assert call_kwargs["config_path"] is None


def test_check_flag_exits_zero_on_valid_config(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("reticulumpi:\n  log_level: 4\n  plugins: {}\n")
    with pytest.raises(SystemExit) as exc_info:
        with patch("sys.argv", ["reticulumpi", "--config", str(config_file), "--check"]):
            main()
    assert exc_info.value.code == 0


def test_check_flag_exits_one_on_missing_plugin(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "reticulumpi:\n"
        "  log_level: 4\n"
        "  plugins:\n"
        "    nonexistent:\n"
        "      enabled: true\n"
    )
    with pytest.raises(SystemExit) as exc_info:
        with patch("sys.argv", ["reticulumpi", "--config", str(config_file), "--check"]):
            main()
    assert exc_info.value.code == 1


def test_check_does_not_start_reticulum(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("reticulumpi:\n  log_level: 4\n  plugins: {}\n")
    with (
        patch("sys.argv", ["reticulumpi", "--config", str(config_file), "--check"]),
        patch("RNS.Reticulum") as mock_rns,
    ):
        with pytest.raises(SystemExit):
            main()
    mock_rns.assert_not_called()


def test_list_plugins_flag_exits_zero(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("reticulumpi:\n  log_level: 4\n  plugins: {}\n")
    with pytest.raises(SystemExit) as exc_info:
        with patch("sys.argv", ["reticulumpi", "--config", str(config_file), "--list-plugins"]):
            main()
    assert exc_info.value.code == 0
