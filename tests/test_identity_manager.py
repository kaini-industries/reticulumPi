"""Tests for durable identity management."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import reticulumpi.identity_manager as identity_manager
from reticulumpi.identity_manager import (
    IdentityPersistenceError,
    backup_identity,
    load_or_create,
    restore_identity,
)


def _writing_identity(payload: bytes) -> MagicMock:
    identity = MagicMock()
    identity.payload = payload
    identity.to_file.side_effect = lambda path: Path(path).write_bytes(payload)
    return identity


def test_creates_new_identity_when_file_missing(tmp_path):
    identity_path = str(tmp_path / "subdir" / "identity")
    created = _writing_identity(b"new identity")
    persisted = MagicMock()

    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.return_value = created
        mock_rns.Identity.from_file.return_value = persisted
        result = load_or_create(identity_path)

    assert result is persisted
    assert os.path.isfile(identity_path)
    assert os.stat(identity_path).st_mode & 0o777 == 0o600
    created.to_file.assert_called_once()


def test_loads_existing_identity_and_secures_mode(tmp_path):
    identity_path = tmp_path / "identity"
    identity_path.write_bytes(b"fake identity data")
    identity_path.chmod(0o644)
    mock_identity = MagicMock()

    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.from_file.return_value = mock_identity
        result = load_or_create(str(identity_path))

    assert result is mock_identity
    assert os.stat(identity_path).st_mode & 0o777 == 0o600


def test_creates_new_identity_when_load_fails_and_preserves_corrupt_file(tmp_path):
    identity_path = tmp_path / "identity"
    identity_path.write_bytes(b"corrupted data")
    created = _writing_identity(b"replacement")
    persisted = MagicMock()

    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.from_file.side_effect = [None, persisted]
        mock_rns.Identity.return_value = created
        result = load_or_create(str(identity_path))

    assert result is persisted
    corrupt = list(tmp_path.glob("identity.corrupt-*"))
    assert len(corrupt) == 1
    assert corrupt[0].read_bytes() == b"corrupted data"


def test_preserves_corrupt_identity_when_parser_raises(tmp_path):
    identity_path = tmp_path / "identity"
    identity_path.write_bytes(b"corrupted data")
    created = _writing_identity(b"replacement")
    persisted = MagicMock()

    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.from_file.side_effect = [ValueError("bad key"), persisted]
        mock_rns.Identity.return_value = created
        assert load_or_create(str(identity_path)) is persisted

    corrupt = list(tmp_path.glob("identity.corrupt-*"))
    assert len(corrupt) == 1
    assert corrupt[0].read_bytes() == b"corrupted data"


def test_persistence_failure_is_fatal(tmp_path):
    identity_path = tmp_path / "identity"
    created = MagicMock()
    created.to_file.side_effect = OSError("disk full")

    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.return_value = created
        with pytest.raises(IdentityPersistenceError, match="disk full"):
            load_or_create(str(identity_path))

    assert not identity_path.exists()


def test_concurrent_creation_returns_one_persisted_identity(tmp_path):
    identity_path = tmp_path / "identity"
    creation_lock = threading.Lock()
    serial = 0

    class FakeIdentity:
        def __init__(self, payload: bytes):
            self.payload = payload

        def to_file(self, path: str) -> None:
            with open(path, "wb") as handle:
                handle.write(self.payload)

    def create() -> FakeIdentity:
        nonlocal serial
        with creation_lock:
            serial += 1
            return FakeIdentity(f"identity-{serial}".encode())

    def from_file(path: str):
        try:
            return FakeIdentity(Path(path).read_bytes())
        except FileNotFoundError:
            return None

    barrier = threading.Barrier(2)
    results: list[FakeIdentity] = []

    def worker() -> None:
        barrier.wait()
        results.append(load_or_create(str(identity_path)))

    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.side_effect = create
        mock_rns.Identity.from_file.side_effect = from_file
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2
    assert results[0].payload == results[1].payload == identity_path.read_bytes()
    assert serial == 1


def test_backup_and_restore_are_atomic_and_mode_0600(tmp_path):
    identity_path = tmp_path / "identity"
    backup_path = tmp_path / "backups" / "identity"
    identity_path.write_bytes(b"original")

    class FakeIdentity:
        def __init__(self, payload: bytes):
            self.payload = payload

    def from_file(path: str):
        payload = Path(path).read_bytes()
        return FakeIdentity(payload) if payload in {b"original", b"changed"} else None

    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.from_file.side_effect = from_file
        backup_identity(str(identity_path), str(backup_path))
        identity_path.write_bytes(b"changed")
        restored = restore_identity(str(identity_path), str(backup_path))

    assert restored.payload == b"original"
    assert identity_path.read_bytes() == b"original"
    assert backup_path.read_bytes() == b"original"
    assert identity_path.stat().st_mode & 0o777 == 0o600
    assert backup_path.stat().st_mode & 0o777 == 0o600


def test_failed_restore_verification_rolls_back_original(tmp_path):
    identity_path = tmp_path / "identity"
    backup_path = tmp_path / "backup"
    identity_path.write_bytes(b"original")
    backup_path.write_bytes(b"replacement")

    replacement_reads = 0

    def from_file(path: str):
        nonlocal replacement_reads
        payload = Path(path).read_bytes()
        if payload == b"replacement":
            replacement_reads += 1
            return MagicMock(payload=payload) if replacement_reads == 1 else None
        return MagicMock(payload=payload)

    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.from_file.side_effect = from_file
        with pytest.raises(IdentityPersistenceError, match="failed verification"):
            restore_identity(str(identity_path), str(backup_path))

    assert identity_path.read_bytes() == b"original"
    assert list(tmp_path.glob("identity.pre-restore-*")) == []


def test_restore_binds_exact_backup_bytes_before_path_can_be_swapped(tmp_path, monkeypatch):
    identity_path = tmp_path / "identity"
    backup_path = tmp_path / "backup"
    identity_path.write_bytes(b"original")
    backup_path.write_bytes(b"candidate-a")

    class FakeIdentity:
        def __init__(self, payload: bytes):
            self.payload = payload
            self.hash = payload

    def from_file(path: str):
        payload = Path(path).read_bytes()
        return FakeIdentity(payload) if payload in {b"candidate-a", b"candidate-b"} else None

    real_stage = identity_manager._stage_identity_bytes

    def swap_then_stage(payload: bytes, destination: str) -> str:
        backup_path.write_bytes(b"candidate-b")
        return real_stage(payload, destination)

    monkeypatch.setattr(identity_manager, "_stage_identity_bytes", swap_then_stage)
    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.from_file.side_effect = from_file
        restored = restore_identity(str(identity_path), str(backup_path))

    assert restored.payload == b"candidate-a"
    assert identity_path.read_bytes() == b"candidate-a"
    assert backup_path.read_bytes() == b"candidate-b"


def test_restore_rollback_failure_preserves_last_good_safety_copy(tmp_path, monkeypatch):
    identity_path = tmp_path / "identity"
    backup_path = tmp_path / "backup"
    identity_path.write_bytes(b"original")
    backup_path.write_bytes(b"replacement")
    real_atomic_write = identity_manager._atomic_identity_bytes

    def fail_rollback_write(payload: bytes, destination: str) -> None:
        if destination == str(identity_path):
            raise IdentityPersistenceError("injected rollback failure")
        real_atomic_write(payload, destination)

    monkeypatch.setattr(identity_manager, "_atomic_identity_bytes", fail_rollback_write)
    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.from_file.side_effect = [MagicMock(), None]
        with pytest.raises(IdentityPersistenceError, match="rollback also failed") as raised:
            restore_identity(str(identity_path), str(backup_path))

    safety_copies = list(tmp_path.glob("identity.pre-restore-*"))
    assert len(safety_copies) == 1
    assert safety_copies[0].read_bytes() == b"original"
    assert safety_copies[0].stat().st_mode & 0o777 == 0o600
    assert "preserved at" in str(raised.value)
    assert isinstance(raised.value.__cause__, IdentityPersistenceError)


def test_parent_creation_failure_is_a_persistence_error(tmp_path, monkeypatch):
    identity_path = tmp_path / "missing" / "identity"

    def fail_mkdir(*_args, **_kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(identity_manager.os, "mkdir", fail_mkdir)
    with pytest.raises(IdentityPersistenceError, match="Cannot create identity directory"):
        load_or_create(str(identity_path))


def test_lock_open_failure_is_a_persistence_error(tmp_path, monkeypatch):
    identity_path = tmp_path / "identity"

    def fail_open(*_args, **_kwargs):
        raise OSError("file descriptor limit")

    monkeypatch.setattr(identity_manager.os, "open", fail_open)
    with pytest.raises(IdentityPersistenceError, match="Cannot lock identity"):
        load_or_create(str(identity_path))


def test_unlock_failure_does_not_mask_completed_locked_work(tmp_path, monkeypatch):
    identity_path = tmp_path / "identity"
    real_flock = identity_manager.fcntl.flock

    def fail_only_unlock(descriptor, operation):
        if operation == identity_manager.fcntl.LOCK_UN:
            raise OSError("injected unlock failure")
        return real_flock(descriptor, operation)

    monkeypatch.setattr(identity_manager.fcntl, "flock", fail_only_unlock)
    with identity_manager._identity_lock(str(identity_path)):
        identity_path.write_bytes(b"protected")

    assert identity_path.read_bytes() == b"protected"
    assert identity_path.with_name("identity.lock").is_file()


def test_rns_missing_temporary_identity_is_fatal_and_cleaned(tmp_path):
    identity_path = tmp_path / "identity"
    created = MagicMock()
    created.to_file.side_effect = os.unlink

    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.return_value = created
        with pytest.raises(IdentityPersistenceError, match="did not create the identity file"):
            load_or_create(str(identity_path))

    assert not identity_path.exists()
    assert list(tmp_path.glob(".identity.*.tmp")) == []


def test_identity_persistence_error_from_writer_is_not_rewrapped(tmp_path):
    identity_path = tmp_path / "identity"
    created = MagicMock()
    error = IdentityPersistenceError("durability sentinel")
    created.to_file.side_effect = error

    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.return_value = created
        with pytest.raises(IdentityPersistenceError, match="durability sentinel") as raised:
            load_or_create(str(identity_path))

    assert raised.value is error
    assert list(tmp_path.glob(".identity.*.tmp")) == []


def test_corrupt_identity_backup_name_collision_is_unique(tmp_path, monkeypatch):
    identity_path = tmp_path / "identity"
    identity_path.write_bytes(b"corrupt")
    monkeypatch.setattr(identity_manager.time, "strftime", lambda *_args: "STAMP")
    base = tmp_path / f"identity.corrupt-STAMP-{os.getpid()}"
    base.write_bytes(b"older-corrupt-copy")
    created = _writing_identity(b"replacement")
    persisted = MagicMock()

    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.from_file.side_effect = [None, persisted]
        mock_rns.Identity.return_value = created
        assert load_or_create(str(identity_path)) is persisted

    assert base.read_bytes() == b"older-corrupt-copy"
    assert base.with_name(f"{base.name}-1").read_bytes() == b"corrupt"


def test_existing_identity_fsync_failure_is_fatal(tmp_path, monkeypatch):
    identity_path = tmp_path / "identity"
    identity_path.write_bytes(b"valid")

    def fail_fsync(_descriptor):
        raise OSError("injected fsync failure")

    monkeypatch.setattr(identity_manager.os, "fsync", fail_fsync)
    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.from_file.return_value = MagicMock()
        with pytest.raises(IdentityPersistenceError, match="Cannot sync identity file"):
            load_or_create(str(identity_path))


def test_identity_and_lock_symlinks_are_rejected_without_touching_targets(tmp_path):
    identity_target = tmp_path / "identity-target"
    identity_target.write_bytes(b"valid")
    identity_path = tmp_path / "identity"
    identity_path.symlink_to(identity_target)

    with pytest.raises(IdentityPersistenceError, match="Cannot open identity file"):
        load_or_create(str(identity_path))
    assert identity_target.read_bytes() == b"valid"
    assert identity_path.is_symlink()

    identity_path.unlink()
    (tmp_path / "identity.lock").unlink()
    lock_target = tmp_path / "lock-target"
    lock_target.write_bytes(b"do-not-touch")
    lock_path = tmp_path / "identity.lock"
    lock_path.symlink_to(lock_target)

    with pytest.raises(IdentityPersistenceError, match="Cannot lock identity"):
        load_or_create(str(identity_path))
    assert lock_target.read_bytes() == b"do-not-touch"
    assert lock_path.is_symlink()


def test_existing_identity_stage_failure_does_not_rotate_valid_bytes(tmp_path, monkeypatch):
    identity_path = tmp_path / "identity"
    identity_path.write_bytes(b"valid")
    monkeypatch.setattr(
        identity_manager,
        "_stage_identity_bytes",
        MagicMock(side_effect=IdentityPersistenceError("injected staging failure")),
    )

    with pytest.raises(IdentityPersistenceError, match="injected staging failure"):
        load_or_create(str(identity_path))

    assert identity_path.read_bytes() == b"valid"
    assert list(tmp_path.glob("identity.corrupt-*")) == []


def test_restore_rejects_linked_current_identity_without_touching_target(tmp_path):
    external = tmp_path / "external-identity"
    external.write_bytes(b"original")
    identity_path = tmp_path / "identity"
    identity_path.symlink_to(external)
    backup_path = tmp_path / "backup"
    backup_path.write_bytes(b"replacement")

    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.from_file.return_value = MagicMock(hash=b"replacement")
        with pytest.raises(IdentityPersistenceError, match="Cannot open identity file"):
            restore_identity(str(identity_path), str(backup_path))

    assert identity_path.is_symlink()
    assert external.read_bytes() == b"original"
    assert list(tmp_path.glob(".identity.restore-*.tmp")) == []


def test_world_writable_nonsticky_identity_parent_is_fatal(tmp_path):
    unsafe_parent = tmp_path / "unsafe"
    unsafe_parent.mkdir(mode=0o700)
    unsafe_parent.chmod(0o777)
    try:
        with pytest.raises(IdentityPersistenceError, match="writable by group or others"):
            load_or_create(str(unsafe_parent / "identity"))
    finally:
        unsafe_parent.chmod(0o700)


def test_new_identity_directories_are_fsynced_from_parent_outward(tmp_path, monkeypatch):
    identity_path = tmp_path / "first" / "second" / "identity"
    created = _writing_identity(b"new")
    persisted = MagicMock()
    real_fsync_directory = identity_manager._fsync_directory
    synced: list[str] = []

    def record_fsync(path: str) -> None:
        synced.append(path)
        real_fsync_directory(path)

    monkeypatch.setattr(identity_manager, "_fsync_directory", record_fsync)
    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.return_value = created
        mock_rns.Identity.from_file.return_value = persisted
        load_or_create(str(identity_path))

    assert str(tmp_path) in synced
    assert str(tmp_path / "first") in synced


def test_corrupt_identity_preservation_failure_is_fatal(tmp_path, monkeypatch):
    identity_path = tmp_path / "identity"
    identity_path.write_bytes(b"corrupt")

    def fail_preserve(_path):
        raise OSError("injected rename failure")

    monkeypatch.setattr(identity_manager, "_preserve_corrupt_identity", fail_preserve)
    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.from_file.return_value = None
        with pytest.raises(IdentityPersistenceError, match="Cannot preserve corrupt identity"):
            load_or_create(str(identity_path))

    assert identity_path.read_bytes() == b"corrupt"


def test_new_identity_parser_exception_after_write_is_fatal(tmp_path):
    identity_path = tmp_path / "identity"
    created = _writing_identity(b"new")

    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.return_value = created
        mock_rns.Identity.from_file.side_effect = ValueError("unreadable persisted key")
        with pytest.raises(IdentityPersistenceError, match="Cannot verify identity"):
            load_or_create(str(identity_path))


def test_new_identity_none_after_write_is_fatal(tmp_path):
    identity_path = tmp_path / "identity"
    created = _writing_identity(b"new")

    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.return_value = created
        mock_rns.Identity.from_file.return_value = None
        with pytest.raises(IdentityPersistenceError, match="failed verification"):
            load_or_create(str(identity_path))


def test_backup_rejects_missing_invalid_and_parser_failure(tmp_path):
    identity_path = tmp_path / "identity"
    backup_path = tmp_path / "backup"
    with pytest.raises(IdentityPersistenceError, match="Identity not found"):
        backup_identity(str(identity_path), str(backup_path))

    identity_path.write_bytes(b"invalid")
    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.from_file.return_value = None
        with pytest.raises(IdentityPersistenceError, match="is invalid"):
            backup_identity(str(identity_path), str(backup_path))

        mock_rns.Identity.from_file.side_effect = ValueError("parser rejected bytes")
        with pytest.raises(IdentityPersistenceError, match="Cannot validate identity"):
            backup_identity(str(identity_path), str(backup_path))

    assert not backup_path.exists()


def test_restore_rejects_missing_and_invalid_backup(tmp_path):
    identity_path = tmp_path / "identity"
    backup_path = tmp_path / "backup"
    with pytest.raises(IdentityPersistenceError, match="backup not found"):
        restore_identity(str(identity_path), str(backup_path))

    backup_path.write_bytes(b"invalid")
    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.from_file.side_effect = ValueError("bad backup")
        with pytest.raises(IdentityPersistenceError, match="Invalid identity backup: bad backup"):
            restore_identity(str(identity_path), str(backup_path))

        mock_rns.Identity.from_file.side_effect = None
        mock_rns.Identity.from_file.return_value = None
        with pytest.raises(IdentityPersistenceError, match="Invalid identity backup"):
            restore_identity(str(identity_path), str(backup_path))


def test_failed_restore_without_original_removes_candidate(tmp_path):
    identity_path = tmp_path / "identity"
    backup_path = tmp_path / "backup"
    backup_path.write_bytes(b"replacement")

    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.from_file.side_effect = [MagicMock(), None]
        with pytest.raises(IdentityPersistenceError, match="failed verification"):
            restore_identity(str(identity_path), str(backup_path))

    assert not identity_path.exists()
    assert list(tmp_path.glob("identity.pre-restore-*")) == []


def test_restore_parser_failure_rolls_back_original(tmp_path):
    identity_path = tmp_path / "identity"
    backup_path = tmp_path / "backup"
    identity_path.write_bytes(b"original")
    backup_path.write_bytes(b"replacement")

    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.from_file.side_effect = [MagicMock(), ValueError("bad restored key")]
        with pytest.raises(IdentityPersistenceError, match="bad restored key"):
            restore_identity(str(identity_path), str(backup_path))

    assert identity_path.read_bytes() == b"original"
    assert list(tmp_path.glob("identity.pre-restore-*")) == []


def test_identity_hash_normalizes_supported_values():
    assert identity_manager._identity_hash(SimpleNamespace(hash=b"bytes")) == b"bytes"
    assert identity_manager._identity_hash(SimpleNamespace(hash=bytearray(b"array"))) == b"array"
    assert identity_manager._identity_hash(SimpleNamespace(hash="invalid")) is None


def test_identity_parent_rejects_non_directory_and_unexpected_owner(tmp_path, monkeypatch):
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_bytes(b"file")
    with pytest.raises(IdentityPersistenceError, match="parent is not a directory"):
        identity_manager._ensure_parent(str(parent_file / "identity"))

    parent = tmp_path / "owned-elsewhere"
    parent.mkdir()
    real_lstat = os.lstat

    def foreign_owner(path):
        value = real_lstat(path)
        if Path(path) == parent:
            return SimpleNamespace(st_mode=value.st_mode, st_uid=99_999)
        return value

    monkeypatch.setattr(identity_manager.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(identity_manager.os, "lstat", foreign_owner)
    with pytest.raises(IdentityPersistenceError, match="unexpected owner"):
        identity_manager._ensure_parent(str(parent / "identity"))


def test_identity_lock_and_reader_reject_multiply_linked_files(tmp_path):
    identity_path = tmp_path / "identity"
    lock_path = tmp_path / "identity.lock"
    lock_path.write_bytes(b"")
    os.link(lock_path, tmp_path / "lock-alias")
    with pytest.raises(IdentityPersistenceError, match="single-link regular file"):
        with identity_manager._identity_lock(str(identity_path)):
            pytest.fail("multiply-linked lock must not be acquired")

    identity_path.write_bytes(b"identity")
    os.link(identity_path, tmp_path / "identity-alias")
    with pytest.raises(IdentityPersistenceError, match="single-link regular file"):
        identity_manager._read_identity_bytes(str(identity_path))


def test_identity_reader_rejects_permission_failure_and_oversize(tmp_path, monkeypatch):
    identity_path = tmp_path / "identity"
    identity_path.write_bytes(b"identity")

    def fail_fchmod(*_args):
        raise OSError("permission denied")

    monkeypatch.setattr(identity_manager.os, "fchmod", fail_fchmod)
    with pytest.raises(IdentityPersistenceError, match="Cannot secure identity file"):
        identity_manager._read_identity_bytes(str(identity_path))

    monkeypatch.undo()
    identity_path.write_bytes(b"x" * (identity_manager._MAX_IDENTITY_BYTES + 1))
    with pytest.raises(IdentityPersistenceError, match="exceeds"):
        identity_manager._read_identity_bytes(str(identity_path))


def test_staging_short_write_is_fatal_and_removes_temporary_file(tmp_path, monkeypatch):
    destination = tmp_path / "identity"
    monkeypatch.setattr(identity_manager.os, "write", lambda *_args: 0)

    with pytest.raises(IdentityPersistenceError, match="short write"):
        identity_manager._stage_identity_bytes(b"identity", str(destination))

    assert list(tmp_path.glob(".identity.restore-*.tmp")) == []


def test_identity_writer_handles_failure_before_temporary_path_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(
        identity_manager.tempfile,
        "mkstemp",
        MagicMock(side_effect=OSError("temporary unavailable")),
    )
    with pytest.raises(IdentityPersistenceError, match="temporary unavailable"):
        identity_manager._atomic_identity_write(MagicMock(), str(tmp_path / "identity"))


def test_atomic_identity_bytes_reports_post_publish_permission_failure(tmp_path, monkeypatch):
    destination = tmp_path / "identity"

    def fail_chmod(*_args):
        raise OSError("chmod failed")

    monkeypatch.setattr(identity_manager.os, "chmod", fail_chmod)
    with pytest.raises(IdentityPersistenceError, match="chmod failed"):
        identity_manager._atomic_identity_bytes(b"identity", str(destination))

    assert destination.read_bytes() == b"identity"


@pytest.mark.parametrize(
    "error",
    (OSError("replace failed"), IdentityPersistenceError("durability failed")),
)
def test_atomic_identity_bytes_cleans_stage_for_replace_failures(tmp_path, monkeypatch, error):
    destination = tmp_path / "identity"
    monkeypatch.setattr(identity_manager.os, "replace", MagicMock(side_effect=error))

    with pytest.raises(IdentityPersistenceError, match="failed"):
        identity_manager._atomic_identity_bytes(b"identity", str(destination))

    assert not destination.exists()
    assert list(tmp_path.glob(".identity.restore-*.tmp")) == []


def test_existing_identity_digest_change_is_fatal(tmp_path, monkeypatch):
    identity_path = tmp_path / "identity"
    identity_path.write_bytes(b"first")
    reads = iter((b"first", b"changed"))
    monkeypatch.setattr(identity_manager, "_read_identity_bytes", lambda _path: next(reads))

    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.from_file.return_value = MagicMock()
        with pytest.raises(IdentityPersistenceError, match="changed while it was being loaded"):
            load_or_create(str(identity_path))


def test_corrupt_identity_digest_change_is_fatal(tmp_path, monkeypatch):
    identity_path = tmp_path / "identity"
    identity_path.write_bytes(b"first")
    reads = iter((b"first", b"changed"))
    monkeypatch.setattr(identity_manager, "_read_identity_bytes", lambda _path: next(reads))

    with patch("reticulumpi.identity_manager.RNS") as mock_rns:
        mock_rns.Identity.from_file.side_effect = ValueError("corrupt")
        with pytest.raises(IdentityPersistenceError, match="changed while corruption"):
            load_or_create(str(identity_path))


def test_restore_candidate_read_failure_never_creates_a_stage(tmp_path, monkeypatch):
    backup = tmp_path / "backup"
    backup.write_bytes(b"backup")
    monkeypatch.setattr(
        identity_manager,
        "_read_identity_bytes",
        MagicMock(side_effect=IdentityPersistenceError("bound read failed")),
    )

    with pytest.raises(IdentityPersistenceError, match="bound read failed"):
        restore_identity(str(tmp_path / "identity"), str(backup))

    assert list(tmp_path.glob(".identity.restore-*.tmp")) == []
