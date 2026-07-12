"""Tests for transactional SQLite migration primitives."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import reticulumpi.migrations as migrations
from reticulumpi.migrations import (
    Migration,
    MigrationError,
    MigrationTarget,
    migrate_target,
    migration_checksums,
    plan_migrations,
    restore_database,
)
from reticulumpi.runtime_metrics import get_runtime_metrics


def _target(tmp_path):
    return MigrationTarget(
        "messages",
        tmp_path / "messages.db",
        (
            Migration(1, "create messages", ("CREATE TABLE messages(id INTEGER PRIMARY KEY)",)),
            Migration(2, "add body", ("ALTER TABLE messages ADD COLUMN body TEXT",)),
        ),
    )


def test_migration_versions_must_be_contiguous(tmp_path):
    with pytest.raises(ValueError, match="contiguous"):
        MigrationTarget(
            "bad",
            tmp_path / "bad.db",
            (Migration(2, "late", ("CREATE TABLE bad(id INTEGER)",)),),
        )


@pytest.mark.parametrize(
    "statement",
    [
        "VACUUM",
        "BEGIN",
        "PRAGMA journal_mode=WAL",
        "CREATE TABLE ok(id INTEGER); VACUUM",
    ],
)
def test_non_atomic_sql_is_rejected(statement):
    with pytest.raises(ValueError, match="non-atomic"):
        Migration(1, "bad", (statement,))


def test_dry_run_does_not_create_database(tmp_path):
    target = _target(tmp_path)
    result = migrate_target(target, dry_run=True)
    assert result.applied == (1, 2)
    assert result.to_version == 2
    assert not target.path.exists()


def test_migration_target_rejects_final_service_owned_symlink(tmp_path):
    outside = tmp_path / "outside.db"
    with closing(sqlite3.connect(outside)) as connection:
        connection.execute("CREATE TABLE protected(value TEXT)")
    link = tmp_path / "messages.db"
    link.symlink_to(outside)
    with pytest.raises(MigrationError, match="symlink"):
        _target(tmp_path)


def test_migration_target_canonicalizes_trusted_platform_alias():
    requested = Path("/var/lib/reticulumpi-migration-test/database.db")
    target = MigrationTarget(
        "trusted_alias",
        requested,
        (Migration(1, "create", ("CREATE TABLE value(id INTEGER)",)),),
    )
    assert target.path == requested.parent.resolve() / requested.name


def test_migration_target_rejects_service_owned_alias_without_root_override(tmp_path):
    real = tmp_path / "service-state"
    real.mkdir()
    alias = tmp_path / "service-alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(MigrationError, match="may not contain a symlink"):
        MigrationTarget(
            "service_alias",
            alias / "database.db",
            (Migration(1, "create", ("CREATE TABLE value(id INTEGER)",)),),
        )


def test_migration_target_normalizes_relative_path():
    target = MigrationTarget(
        "relative",
        "relative-migration.db",
        (Migration(1, "create", ("CREATE TABLE value(id INTEGER)",)),),
    )
    assert target.path.is_absolute()


def test_migration_target_rejects_invalid_unsafe_and_nondirectory_ancestors(tmp_path, monkeypatch):
    real_lstat = Path.lstat

    def root_owned_components(path):
        result = real_lstat(path)
        return SimpleNamespace(st_mode=result.st_mode, st_uid=0)

    monkeypatch.setattr(Path, "lstat", root_owned_components)
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "missing", target_is_directory=True)
    with pytest.raises(MigrationError, match="trusted platform alias is invalid"):
        MigrationTarget(
            "dangling",
            dangling / "database.db",
            (Migration(1, "create", ("CREATE TABLE value(id INTEGER)",)),),
        )

    unsafe_target = tmp_path / "unsafe-target"
    unsafe_target.mkdir(mode=0o777)
    unsafe_target.chmod(0o777)
    unsafe_alias = tmp_path / "unsafe-alias"
    unsafe_alias.symlink_to(unsafe_target, target_is_directory=True)
    with pytest.raises(MigrationError, match="trusted platform alias is unsafe"):
        MigrationTarget(
            "unsafe",
            unsafe_alias / "database.db",
            (Migration(1, "create", ("CREATE TABLE value(id INTEGER)",)),),
        )

    nondirectory = tmp_path / "not-a-directory"
    nondirectory.write_text("content", encoding="utf-8")
    with pytest.raises(MigrationError, match="ancestor is not a directory"):
        MigrationTarget(
            "nondirectory",
            nondirectory / "database.db",
            (Migration(1, "create", ("CREATE TABLE value(id INTEGER)",)),),
        )


def test_migration_lock_and_target_validation_reject_special_paths(tmp_path):
    target = _target(tmp_path)
    lock = target.path.with_name(f".{target.path.name}.migration.lock")
    outside = tmp_path / "outside-lock"
    outside.write_text("content", encoding="utf-8")
    lock.symlink_to(outside)
    with pytest.raises(MigrationError, match="cannot open migration lock safely"):
        with migrations._target_lock(target):
            pytest.fail("symlink lock must never be acquired")

    lock.unlink()
    target.path.mkdir()
    with pytest.raises(MigrationError, match="target is not a regular file"):
        migrations._validate_target_path(target.path)


def test_apply_creates_schema_and_metadata(tmp_path):
    target = _target(tmp_path)
    result = migrate_target(target, dry_run=False)
    assert result.applied == (1, 2)
    assert result.backup_path is None
    assert target.path.stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(target.path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        columns = [row[1] for row in connection.execute("PRAGMA table_info(messages)")]
        assert columns == ["id", "body"]
        assert connection.execute("SELECT COUNT(*) FROM reticulumpi_migrations").fetchone()[0] == 2

    second = migrate_target(target, dry_run=False)
    assert second.applied == ()
    assert second.dry_run is False


def test_existing_database_gets_verified_backup(tmp_path):
    target = _target(tmp_path)
    with closing(sqlite3.connect(target.path)) as connection:
        connection.execute("CREATE TABLE legacy(value TEXT)")
        connection.commit()
    result = migrate_target(target, dry_run=False, backup_dir=tmp_path / "backups")
    assert result.backup_path is not None
    assert result.backup_path.is_file()
    with closing(sqlite3.connect(result.backup_path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_failed_live_migration_restores_old_database(tmp_path, monkeypatch):
    target = _target(tmp_path)
    with closing(sqlite3.connect(target.path)) as connection:
        connection.execute("CREATE TABLE original(value TEXT)")
        connection.execute("INSERT INTO original VALUES ('kept')")
        connection.commit()

    import reticulumpi.migrations as module

    real_apply = module._apply
    calls = 0

    def fail_live(connection, declared, current):
        nonlocal calls
        calls += 1
        if calls == 2:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TABLE original")
            connection.commit()
            raise MigrationError("injected live failure")
        return real_apply(connection, declared, current)

    monkeypatch.setattr(module, "_apply", fail_live)
    with pytest.raises(MigrationError, match="injected"):
        migrate_target(target, dry_run=False, backup_dir=tmp_path / "backups")
    with closing(sqlite3.connect(target.path)) as connection:
        assert connection.execute("SELECT value FROM original").fetchone()[0] == "kept"


def test_checksum_history_change_is_rejected(tmp_path):
    target = _target(tmp_path)
    migrate_target(target, dry_run=False)
    changed = MigrationTarget(
        "messages",
        target.path,
        (
            Migration(1, "changed history", ("CREATE TABLE messages(id INTEGER PRIMARY KEY)",)),
            target.migrations[1],
        ),
    )
    with pytest.raises(MigrationError, match="historical checksum changed"):
        plan_migrations(changed)


def test_incomplete_migration_metadata_is_rejected(tmp_path):
    target = _target(tmp_path)
    migrate_target(target, dry_run=False)
    with closing(sqlite3.connect(target.path)) as connection:
        connection.execute("DELETE FROM reticulumpi_migrations WHERE version = 2")
        connection.commit()

    with pytest.raises(MigrationError, match="metadata does not match"):
        plan_migrations(target)


def test_restore_database_validates_and_replaces(tmp_path):
    source = tmp_path / "backup.db"
    target = tmp_path / "live.db"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE restored(value TEXT)")
        connection.execute("INSERT INTO restored VALUES ('yes')")
        connection.commit()
    restore_database(source, target)
    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute("SELECT value FROM restored").fetchone()[0] == "yes"


def test_restore_database_rejects_symlink_backup_and_target(tmp_path):
    source = tmp_path / "backup.db"
    target = tmp_path / "live.db"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE restored(value TEXT)")
    backup_link = tmp_path / "backup-link.db"
    backup_link.symlink_to(source)
    with pytest.raises(MigrationError, match="backup may not be a symlink"):
        restore_database(backup_link, target)

    target.symlink_to(tmp_path / "outside.db")
    with pytest.raises(MigrationError, match="restore target may not be a symlink"):
        restore_database(source, target)


def test_restore_primitives_reject_nonregular_source_and_durable_path(tmp_path):
    with pytest.raises(MigrationError, match="durable migration path is not a regular file"):
        migrations._fsync_path(tmp_path)
    with pytest.raises(MigrationError, match="backup is not a regular file"):
        migrations._restore_backup(tmp_path, tmp_path / "target.db")


def test_restore_backup_detects_short_write(tmp_path, monkeypatch):
    source = tmp_path / "backup.db"
    source.write_bytes(b"database bytes")
    target = tmp_path / "target.db"
    monkeypatch.setattr(migrations.os, "write", lambda _descriptor, _data: 0)
    with pytest.raises(MigrationError, match="short write"):
        migrations._restore_backup(source, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".target.db.restore-*"))


def test_restore_backup_detects_source_change(tmp_path, monkeypatch):
    source = tmp_path / "backup.db"
    source.write_bytes(b"database bytes")
    target = tmp_path / "target.db"
    real_fstat = migrations.os.fstat
    calls = 0

    def changed_fstat(descriptor):
        nonlocal calls
        calls += 1
        result = real_fstat(descriptor)
        if calls == 2:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_dev=result.st_dev,
                st_ino=result.st_ino,
                st_size=result.st_size,
                st_mtime_ns=result.st_mtime_ns,
                st_ctime_ns=result.st_ctime_ns + 1,
            )
        return result

    monkeypatch.setattr(migrations.os, "fstat", changed_fstat)
    with pytest.raises(MigrationError, match="changed while restoring"):
        migrations._restore_backup(source, target)
    assert not target.exists()


def test_restore_database_normalizes_relative_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with closing(sqlite3.connect("backup.db")) as connection:
        connection.execute("CREATE TABLE restored(value TEXT)")
    restore_database(Path("backup.db"), Path("target.db"))
    with closing(sqlite3.connect("target.db")) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize(
    ("version", "name", "statements", "checksum", "message"),
    [
        (0, "bad", ("SELECT 1",), "", "versions start at 1"),
        (1, " ", ("SELECT 1",), "", "name cannot be empty"),
        (1, "bad", (), "", "at least one statement"),
        (1, "bad", ("  ",), "", "statements cannot be empty"),
        (1, "bad", ("SELECT 1",), "incorrect", "checksum mismatch"),
    ],
)
def test_invalid_migration_declarations_are_rejected(version, name, statements, checksum, message):
    with pytest.raises(ValueError, match=message):
        Migration(version, name, statements, checksum)


def test_migration_target_name_is_required(tmp_path):
    with pytest.raises(ValueError, match="target name cannot be empty"):
        MigrationTarget(" ", tmp_path / "db.sqlite", ())


@pytest.mark.parametrize(
    "statement",
    [
        "COMMIT",
        "END",
        "ROLLBACK",
        "ATTACH DATABASE 'other.db' AS other",
        "DETACH DATABASE other",
        "CREATE TABLE ok(id INTEGER); BEGIN IMMEDIATE",
    ],
)
def test_every_implicit_commit_or_transaction_control_form_is_rejected(statement):
    with pytest.raises(ValueError, match="non-atomic"):
        Migration(1, "unsafe", (statement,))


def test_explicit_checksum_is_stable_and_reported():
    calculated = Migration(1, "one", ("CREATE TABLE one(id INTEGER)",))
    declared = Migration(
        calculated.version,
        calculated.name,
        calculated.statements,
        calculated.calculated_checksum,
    )
    assert declared.stable_checksum == calculated.calculated_checksum
    assert migration_checksums((declared, calculated)) == (
        declared.calculated_checksum,
        calculated.calculated_checksum,
    )


def test_nonblocking_target_lock_reports_contention(tmp_path, monkeypatch):
    target = _target(tmp_path)

    def blocked(*_args, **_kwargs):
        raise BlockingIOError("held")

    monkeypatch.setattr(migrations.fcntl, "flock", blocked)
    with pytest.raises(MigrationError, match="migration already active"):
        migrate_target(target, dry_run=True)


@pytest.mark.parametrize("integrity_row", [("corrupt",), None])
def test_integrity_failure_reports_result(integrity_row):
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = integrity_row
    expected = "corrupt" if integrity_row else "no result"
    with pytest.raises(MigrationError, match=expected):
        migrations._integrity(connection)


def test_newer_database_schema_is_rejected(tmp_path):
    target = _target(tmp_path)
    with closing(sqlite3.connect(target.path)) as connection:
        connection.execute("PRAGMA user_version = 3")
        connection.commit()
    with pytest.raises(MigrationError, match="newer than supported"):
        plan_migrations(target)


def test_unknown_recorded_migration_version_is_rejected(tmp_path):
    target = _target(tmp_path)
    with closing(sqlite3.connect(target.path)) as connection:
        connection.execute(
            "CREATE TABLE reticulumpi_migrations("
            "version INTEGER PRIMARY KEY, name TEXT, checksum TEXT, applied_at REAL)"
        )
        connection.execute("INSERT INTO reticulumpi_migrations VALUES (3, 'future', 'x', 0)")
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    with pytest.raises(MigrationError, match="unknown recorded migration version 3"):
        plan_migrations(target)


def test_plan_for_missing_database_returns_full_history_without_creating_file(tmp_path):
    target = _target(tmp_path)
    assert plan_migrations(target) == target.migrations
    assert not target.path.exists()


def test_plan_closes_database_connection(tmp_path, monkeypatch):
    target = _target(tmp_path)
    target.path.touch()
    connection = MagicMock()
    monkeypatch.setattr(migrations, "_connect", lambda _path: connection)
    monkeypatch.setattr(migrations, "_integrity", lambda *_args: None)
    monkeypatch.setattr(migrations, "_validate_history", lambda *_args: 0)

    assert plan_migrations(target) == target.migrations
    connection.close.assert_called_once_with()


def test_connect_setup_failure_closes_database_connection(tmp_path, monkeypatch):
    connection = MagicMock()
    connection.execute.side_effect = sqlite3.OperationalError("pragma failed")
    monkeypatch.setattr(migrations.sqlite3, "connect", lambda *args, **kwargs: connection)

    with pytest.raises(sqlite3.OperationalError, match="pragma failed"):
        migrations._connect(tmp_path / "broken.db")

    connection.close.assert_called_once_with()


def test_insufficient_free_space_fails_before_clone_or_database_creation(tmp_path, monkeypatch):
    target = _target(tmp_path)
    monkeypatch.setattr(
        migrations.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=0),
    )
    with pytest.raises(MigrationError, match="insufficient free space"):
        migrate_target(target, dry_run=False)
    assert not target.path.exists()


def test_apply_rolls_back_every_statement_on_sql_failure(tmp_path):
    target = MigrationTarget(
        "failing",
        tmp_path / "failing.db",
        (
            Migration(
                1,
                "fail after DDL",
                (
                    "CREATE TABLE staged(id INTEGER)",
                    "INSERT INTO missing_table VALUES (1)",
                ),
            ),
        ),
    )
    connection = sqlite3.connect(":memory:")
    try:
        with pytest.raises(sqlite3.OperationalError, match="missing_table"):
            migrations._apply(connection, target, 0)
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='staged'"
            ).fetchone()
            is None
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        connection.close()


def test_directory_fsync_unavailability_fails_durable_write(tmp_path, monkeypatch):
    durable = tmp_path / "durable.db"
    durable.write_bytes(b"content")
    real_open = migrations.os.open

    def fail_directory_open(path, flags, *args):
        if os.fspath(path) == os.fspath(tmp_path):
            raise OSError("directory fsync unsupported")
        return real_open(path, flags, *args)

    monkeypatch.setattr(migrations.os, "open", fail_directory_open)
    with pytest.raises(OSError, match="directory fsync unsupported"):
        migrations._fsync_path(durable)


def test_live_version_drift_removes_new_candidate_database(tmp_path, monkeypatch):
    target = _target(tmp_path)
    real_validate = migrations._validate_history
    validations = 0

    def drift_on_live(connection, declared):
        nonlocal validations
        validations += 1
        current = real_validate(connection, declared)
        return current + 1 if validations == 2 else current

    monkeypatch.setattr(migrations, "_validate_history", drift_on_live)
    with pytest.raises(MigrationError, match="database version changed"):
        migrate_target(target, dry_run=False)

    assert validations == 2
    assert not target.path.exists()


def test_backup_retention_deletes_older_automatic_backups(tmp_path):
    target = _target(tmp_path)
    with closing(sqlite3.connect(target.path)) as connection:
        connection.execute("CREATE TABLE legacy(value TEXT)")
        connection.commit()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    old_backups = [backup_dir / f"{target.path.name}.v0.old-{index}.bak" for index in range(2)]
    for index, backup in enumerate(old_backups, start=1):
        backup.write_bytes(b"old")
        os.utime(backup, (index, index))

    result = migrate_target(target, dry_run=False, backup_dir=backup_dir, retain=1)

    assert result.backup_path is not None
    assert list(backup_dir.glob(f"{target.path.name}.v*.bak")) == [result.backup_path]
    assert all(not backup.exists() for backup in old_backups)


def test_restore_rejects_missing_backup(tmp_path):
    before = migrations.get_migration_metrics()
    with pytest.raises(MigrationError, match="backup does not exist"):
        restore_database(tmp_path / "missing.db", tmp_path / "target.db")
    after = migrations.get_migration_metrics()
    assert after["restore_attempts"] == before["restore_attempts"] + 1
    assert after["restore_failures"] == before["restore_failures"] + 1


def test_migration_metrics_record_secret_free_aggregate_outcomes(tmp_path):
    before = migrations.get_migration_metrics()
    result = migrate_target(_target(tmp_path), dry_run=True)
    after = migrations.get_migration_metrics()

    assert result.applied == (1, 2)
    assert after["attempts"] == before["attempts"] + 1
    assert after["successes"] == before["successes"] + 1
    assert after["dry_runs"] == before["dry_runs"] + 1
    assert after["migrations_applied"] == before["migrations_applied"] + 2
    assert all(isinstance(value, int) for value in after.values())


def test_migration_metrics_count_sqlite_failures_without_target_labels(tmp_path, monkeypatch):
    before = migrations.get_migration_metrics()
    runtime_before = get_runtime_metrics()["sqlite_failures_total"]

    def fail_dry_run(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(migrations, "_dry_run", fail_dry_run)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        migrate_target(_target(tmp_path), dry_run=True)

    after = migrations.get_migration_metrics()
    assert after["attempts"] == before["attempts"] + 1
    assert after["failures"] == before["failures"] + 1
    assert after["sqlite_failures"] == before["sqlite_failures"] + 1
    assert get_runtime_metrics()["sqlite_failures_total"] == runtime_before + 1
    assert "target" not in after
