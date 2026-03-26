"""Tests for the identity manager."""

from unittest.mock import MagicMock, patch


def test_creates_new_identity_when_file_missing(tmp_path):
    identity_path = str(tmp_path / "subdir" / "identity")
    mock_identity = MagicMock()

    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.return_value = mock_identity
        from reticulumpi.identity_manager import load_or_create
        result = load_or_create(identity_path)

    assert result is mock_identity
    mock_identity.to_file.assert_called_once_with(identity_path)


def test_loads_existing_identity(tmp_path):
    identity_path = tmp_path / "identity"
    identity_path.write_bytes(b"fake identity data")
    mock_identity = MagicMock()

    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.from_file.return_value = mock_identity
        from reticulumpi.identity_manager import load_or_create
        result = load_or_create(str(identity_path))

    assert result is mock_identity


def test_creates_new_identity_when_load_fails(tmp_path):
    identity_path = tmp_path / "identity"
    identity_path.write_bytes(b"corrupted data")
    mock_new_identity = MagicMock()

    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.from_file.return_value = None
        mock_rns.Identity.return_value = mock_new_identity
        from reticulumpi.identity_manager import load_or_create
        result = load_or_create(str(identity_path))

    assert result is mock_new_identity
    mock_new_identity.to_file.assert_called_once()
