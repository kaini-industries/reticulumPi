"""Transactional SQLite migration primitives shared by plugins and administration.

Migration declarations are immutable and checksummed. A target is always dry-run
on a clone before the live database is changed, and a verified SQLite backup is
retained for rollback.
"""

from __future__ import annotations

import contextlib
import fcntl
import functools
import hashlib
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from reticulumpi.runtime_metrics import record_sqlite_failure


_FORBIDDEN_SQL = re.compile(
    r"(?:^|;)\s*(?:BEGIN|COMMIT|END|ROLLBACK|VACUUM|ATTACH|DETACH|"
    r"PRAGMA\s+journal_mode)\b",
    re.IGNORECASE | re.MULTILINE,
)

_METRICS_LOCK = threading.Lock()
_METRICS = {
    "attempts": 0,
    "successes": 0,
    "failures": 0,
    "dry_runs": 0,
    "apply_runs": 0,
    "migrations_applied": 0,
    "backups_created": 0,
    "restore_attempts": 0,
    "restores": 0,
    "restore_failures": 0,
    "sqlite_failures": 0,
}


def _record_metric(name: str, amount: int = 1) -> None:
    with _METRICS_LOCK:
        _METRICS[name] += amount
    if name == "sqlite_failures":
        for _ in range(max(0, amount)):
            record_sqlite_failure()


def get_migration_metrics() -> dict[str, int]:
    """Return aggregate migration outcomes without target names or paths."""

    with _METRICS_LOCK:
        return dict(_METRICS)


class MigrationError(RuntimeError):
    """Raised when a declared migration cannot be applied safely."""


def _canonicalize_trusted_ancestors(path: Path) -> Path:
    """Resolve only immutable platform aliases such as macOS ``/var``."""

    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = candidate.absolute()
    current = Path(candidate.parts[0])
    trusted = True
    # A service-owned symlink is attacker-controlled even when migrations run as
    # that same service account. Platform aliases (for example macOS /var) are
    # accepted only when every prior component and the alias are root-owned.
    trusted_uids = {0}
    for index, part in enumerate(candidate.parts[1:], 1):
        next_path = current / part
        try:
            path_stat = next_path.lstat()
        except FileNotFoundError:
            current = next_path
            continue
        except OSError as exc:
            raise MigrationError(f"cannot inspect migration path {next_path}: {exc}") from exc
        final = index == len(candidate.parts) - 1
        if stat.S_ISLNK(path_stat.st_mode):
            if not trusted or path_stat.st_uid not in trusted_uids:
                raise MigrationError(f"migration path may not contain a symlink: {next_path}")
            try:
                resolved = next_path.resolve(strict=True)
                resolved_stat = resolved.lstat()
            except OSError as exc:
                raise MigrationError(f"trusted platform alias is invalid: {next_path}") from exc
            if (not final and not stat.S_ISDIR(resolved_stat.st_mode)) or (
                resolved_stat.st_uid not in trusted_uids
                or stat.S_IMODE(resolved_stat.st_mode) & 0o022
            ):
                raise MigrationError(f"trusted platform alias is unsafe: {next_path}")
            current = resolved
            continue
        current = next_path
        if not final and not stat.S_ISDIR(path_stat.st_mode):
            raise MigrationError(f"migration path ancestor is not a directory: {next_path}")
        if path_stat.st_uid not in trusted_uids or stat.S_IMODE(path_stat.st_mode) & 0o022:
            trusted = False
    return current


@dataclass(frozen=True)
class Migration:
    """One immutable, ordered schema change."""

    version: int
    name: str
    statements: tuple[str, ...]
    checksum: str = ""

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("migration versions start at 1")
        if not self.name.strip():
            raise ValueError("migration name cannot be empty")
        if not self.statements:
            raise ValueError("migration must contain at least one statement")
        for statement in self.statements:
            if not statement.strip():
                raise ValueError("migration statements cannot be empty")
            if _FORBIDDEN_SQL.search(statement):
                raise ValueError(f"migration {self.version} contains non-atomic SQL")
        calculated = self.calculated_checksum
        if self.checksum and self.checksum != calculated:
            raise ValueError(
                f"migration {self.version} checksum mismatch: "
                f"declared {self.checksum}, calculated {calculated}"
            )

    @property
    def calculated_checksum(self) -> str:
        payload = "\0".join((str(self.version), self.name, *self.statements))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def stable_checksum(self) -> str:
        return self.checksum or self.calculated_checksum


@dataclass(frozen=True)
class MigrationTarget:
    """A named SQLite file and its complete ordered migration history."""

    name: str
    path: Path
    migrations: tuple[Migration, ...]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("migration target name cannot be empty")
        versions = [migration.version for migration in self.migrations]
        if versions != list(range(1, len(versions) + 1)):
            raise ValueError("migration versions must be contiguous and ordered")
        object.__setattr__(
            self,
            "path",
            _canonicalize_trusted_ancestors(Path(self.path)),
        )


@dataclass(frozen=True)
class MigrationResult:
    """Outcome of planning or applying a target."""

    target: str
    from_version: int
    to_version: int
    applied: tuple[int, ...]
    dry_run: bool
    backup_path: Path | None = None


def _record_migration_call(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        _record_metric("attempts")
        try:
            result = function(*args, **kwargs)
        except BaseException as exc:
            _record_metric("failures")
            if isinstance(exc, sqlite3.Error):
                _record_metric("sqlite_failures")
            raise
        _record_metric("successes")
        _record_metric("dry_runs" if result.dry_run else "apply_runs")
        _record_metric("migrations_applied", len(result.applied))
        if result.backup_path is not None:
            _record_metric("backups_created")
        return result

    return wrapped


@contextlib.contextmanager
def _target_lock(target: MigrationTarget) -> Iterator[None]:
    lock_path = target.path.with_name(f".{target.path.name}.migration.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise MigrationError(f"cannot open migration lock safely: {lock_path}") from exc
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise MigrationError(f"migration lock is not a regular file: {lock_path}")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MigrationError(f"migration already active for {target.name}") from exc
        yield


def _validate_target_path(path: Path) -> None:
    """Reject symlink/special migration paths without resolving attacker input."""

    candidate = _canonicalize_trusted_ancestors(path)
    current = Path(candidate.parts[0])
    missing = False
    for index, part in enumerate(candidate.parts[1:], 1):
        current /= part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            missing = True
            continue
        except OSError as exc:
            raise MigrationError(f"cannot validate migration path {current}: {exc}") from exc
        if missing:
            raise MigrationError(f"migration path changed during validation: {current}")
        if stat.S_ISLNK(current_stat.st_mode):
            raise MigrationError(f"migration path may not contain a symlink: {current}")
        final = index == len(candidate.parts) - 1
        if final:
            if not stat.S_ISREG(current_stat.st_mode):
                raise MigrationError(f"migration target is not a regular file: {current}")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(current, flags)
            except OSError as exc:
                raise MigrationError(f"cannot open migration target safely: {current}") from exc
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise MigrationError(f"migration target is not regular: {current}")
            finally:
                os.close(descriptor)
        elif not stat.S_ISDIR(current_stat.st_mode):
            raise MigrationError(f"migration path ancestor is not a directory: {current}")


def _connect(path: Path) -> sqlite3.Connection:
    _validate_target_path(path)
    connection = sqlite3.connect(path, timeout=10)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
    except BaseException:
        connection.close()
        raise


def _integrity(connection: sqlite3.Connection, pragma: str = "integrity_check") -> None:
    result = connection.execute(f"PRAGMA {pragma}").fetchone()
    if not result or result[0] != "ok":
        _record_metric("sqlite_failures")
        raise MigrationError(f"SQLite {pragma} failed: {result[0] if result else 'no result'}")


def _current_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _validate_history(connection: sqlite3.Connection, target: MigrationTarget) -> int:
    current = _current_version(connection)
    if current > len(target.migrations):
        raise MigrationError(
            f"{target.name} schema {current} is newer than supported {len(target.migrations)}"
        )
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reticulumpi_migrations'"
    ).fetchone()
    if table:
        rows = connection.execute(
            "SELECT version, checksum FROM reticulumpi_migrations ORDER BY version"
        ).fetchall()
        for version, checksum in rows:
            if version < 1 or version > len(target.migrations):
                raise MigrationError(f"unknown recorded migration version {version}")
            expected = target.migrations[version - 1].stable_checksum
            if checksum != expected:
                raise MigrationError(f"historical checksum changed for migration {version}")
        recorded_versions = [int(version) for version, _checksum in rows]
        if recorded_versions != list(range(1, current + 1)):
            raise MigrationError(
                "migration metadata does not match PRAGMA user_version "
                f"{current}: recorded {recorded_versions}"
            )
    return current


def plan_migrations(target: MigrationTarget) -> tuple[Migration, ...]:
    """Return pending migrations without modifying the database."""
    _validate_target_path(target.path)
    if not target.path.exists():
        return target.migrations
    with contextlib.closing(_connect(target.path)) as connection:
        _integrity(connection, "quick_check")
        current = _validate_history(connection, target)
    return target.migrations[current:]


def _ensure_space(path: Path) -> None:
    size = path.stat().st_size if path.exists() else 0
    required = size * 2 + 10 * 1024 * 1024
    free = shutil.disk_usage(path.parent).free
    if free < required:
        raise MigrationError(
            f"insufficient free space for migration: need {required} bytes, have {free}"
        )


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        contextlib.closing(_connect(source)) as source_connection,
        contextlib.closing(_connect(destination)) as backup_connection,
    ):
        source_connection.backup(backup_connection)
        _integrity(backup_connection)
    destination.chmod(0o600)
    _fsync_path(destination)


def _apply(
    connection: sqlite3.Connection, target: MigrationTarget, current: int
) -> tuple[int, ...]:
    pending = target.migrations[current:]
    if not pending:
        return ()
    applied: list[int] = []
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS reticulumpi_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at REAL NOT NULL
            )"""
        )
        for migration in pending:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO reticulumpi_migrations(version, name, checksum, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    migration.stable_checksum,
                    time.time(),
                ),
            )
            connection.execute(f"PRAGMA user_version = {migration.version}")
            applied.append(migration.version)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return tuple(applied)


def _dry_run(target: MigrationTarget, source_exists: bool) -> tuple[int, tuple[int, ...]]:
    with tempfile.TemporaryDirectory(prefix=f"reticulumpi-{target.name}-migration-") as raw:
        clone = Path(raw) / target.path.name
        if source_exists:
            _sqlite_backup(target.path, clone)
        with contextlib.closing(_connect(clone)) as connection:
            current = _validate_history(connection, target)
            applied = _apply(connection, target, current)
            _integrity(connection)
        return current, applied


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise MigrationError(f"durable migration path is not a regular file: {path}")
        os.fsync(fd)
    finally:
        os.close(fd)
    directory_fd = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _restore_backup(backup: Path, target: Path) -> None:
    temporary = target.with_name(
        f".{target.name}.restore-{os.getpid()}-{time.time_ns() % 1_000_000_000:09d}"
    )
    source_fd: int | None = None
    destination_fd: int | None = None
    try:
        source_fd = os.open(backup, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise MigrationError(f"database backup is not a regular file: {backup}")
        destination_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        while block := os.read(source_fd, 1024 * 1024):
            view = memoryview(block)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise MigrationError(f"short write while restoring database: {target}")
                view = view[written:]
        os.fsync(destination_fd)
        after = os.fstat(source_fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise MigrationError(f"database backup changed while restoring: {backup}")
        completed_fd = destination_fd
        destination_fd = None
        os.close(completed_fd)
        _validate_target_path(target)
        os.replace(temporary, target)
        _fsync_path(target)
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
        temporary.unlink(missing_ok=True)


@_record_migration_call
def migrate_target(
    target: MigrationTarget,
    *,
    dry_run: bool = True,
    backup_dir: Path | None = None,
    retain: int = 3,
) -> MigrationResult:
    """Plan or safely apply every pending migration for *target*."""
    _validate_target_path(target.path)
    target.path.parent.mkdir(parents=True, exist_ok=True)
    with _target_lock(target):
        _validate_target_path(target.path)
        source_exists = target.path.exists()
        _ensure_space(target.path)
        current, applied = _dry_run(target, source_exists)
        if dry_run or not applied:
            return MigrationResult(
                target=target.name,
                from_version=current,
                to_version=current + len(applied),
                applied=applied,
                dry_run=dry_run,
            )

        backup: Path | None = None
        if source_exists:
            root = backup_dir or target.path.parent / "backups"
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            backup = root / (
                f"{target.path.name}.v{current}.{stamp}.{time.time_ns() % 1_000_000_000:09d}.bak"
            )
            _sqlite_backup(target.path, backup)

        try:
            with contextlib.closing(_connect(target.path)) as connection:
                live_current = _validate_history(connection, target)
                if live_current != current:
                    raise MigrationError("database version changed after migration dry run")
                live_applied = _apply(connection, target, current)
                _integrity(connection)
            target.path.chmod(0o600)
            _fsync_path(target.path)
        except BaseException:
            if backup is not None:
                _restore_backup(backup, target.path)
            elif not source_exists:
                target.path.unlink(missing_ok=True)
            raise

        if backup is not None and retain >= 0:
            candidates = sorted(
                backup.parent.glob(f"{target.path.name}.v*.bak"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for expired in candidates[retain:]:
                expired.unlink(missing_ok=True)
        return MigrationResult(
            target=target.name,
            from_version=current,
            to_version=current + len(live_applied),
            applied=live_applied,
            dry_run=False,
            backup_path=backup,
        )


def restore_database(backup: Path, target: Path) -> None:
    """Validate and atomically restore one SQLite backup."""
    _record_metric("restore_attempts")
    try:
        requested_backup = Path(backup).expanduser()
        requested_target = Path(target).expanduser()
        if not requested_backup.is_absolute():
            requested_backup = requested_backup.absolute()
        if not requested_target.is_absolute():
            requested_target = requested_target.absolute()
        for requested, label in (
            (requested_backup, "database backup"),
            (requested_target, "database restore target"),
        ):
            try:
                requested_stat = requested.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise MigrationError(f"cannot inspect {label}: {requested}: {exc}") from exc
            if stat.S_ISLNK(requested_stat.st_mode):
                raise MigrationError(f"{label} may not be a symlink: {requested}")
        backup = _canonicalize_trusted_ancestors(requested_backup)
        target = _canonicalize_trusted_ancestors(requested_target)
        _validate_target_path(backup)
        _validate_target_path(target)
        if not backup.is_file():
            raise MigrationError(f"backup does not exist: {backup}")
        with contextlib.closing(_connect(backup)) as connection:
            _integrity(connection)
        target.parent.mkdir(parents=True, exist_ok=True)
        _validate_target_path(target)
        _restore_backup(backup, target)
    except BaseException as exc:
        _record_metric("restore_failures")
        if isinstance(exc, sqlite3.Error):
            _record_metric("sqlite_failures")
        raise
    _record_metric("restores")


def migration_checksums(migrations: Sequence[Migration]) -> tuple[str, ...]:
    """Return stable checksums for documentation and review tooling."""
    return tuple(migration.stable_checksum for migration in migrations)
