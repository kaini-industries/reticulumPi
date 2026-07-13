"""Durable Reticulum identity management.

Identity creation is serialized across processes and all updates are written
atomically.  A node must never continue with an identity that it failed to
persist: doing so would silently change its network identity after restart.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import logging
import os
import stat
import tempfile
import time
from collections.abc import Iterator

import RNS

log = logging.getLogger(__name__)


class IdentityPersistenceError(RuntimeError):
    """Raised when an identity cannot be durably loaded or persisted."""


_MAX_IDENTITY_BYTES = 1024 * 1024


def _expanded(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _ensure_parent(path: str) -> str:
    parent = os.path.dirname(path) or "."
    try:
        missing: list[str] = []
        cursor = parent
        while not os.path.lexists(cursor):
            missing.append(cursor)
            next_cursor = os.path.dirname(cursor) or "."
            if next_cursor == cursor:
                break
            cursor = next_cursor
        for directory in reversed(missing):
            os.mkdir(directory, 0o700)
            _fsync_directory(os.path.dirname(directory) or ".")

        parent_stat = os.lstat(parent)
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise OSError("identity parent is not a directory")
        writable_by_others = parent_stat.st_mode & 0o022
        sticky_root_directory = bool(parent_stat.st_mode & stat.S_ISVTX) and parent_stat.st_uid == 0
        if writable_by_others and not sticky_root_directory:
            raise OSError("identity parent is writable by group or others")
        if os.geteuid() != 0 and parent_stat.st_uid not in {0, os.geteuid()}:
            raise OSError("identity parent has an unexpected owner")
    except OSError as exc:
        raise IdentityPersistenceError(f"Cannot create identity directory {parent}: {exc}") from exc
    return parent


@contextlib.contextmanager
def _identity_lock(identity_path: str) -> Iterator[None]:
    """Hold the process-wide lock associated with *identity_path*."""

    parent = _ensure_parent(identity_path)
    lock_path = f"{identity_path}.lock"
    fd: int | None = None
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        lock_stat = os.fstat(fd)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
            raise IdentityPersistenceError(
                f"Identity lock is not a single-link regular file: {lock_path}"
            )
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    except IdentityPersistenceError:
        raise
    except OSError as exc:
        raise IdentityPersistenceError(f"Cannot lock identity {identity_path}: {exc}") from exc
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
        # Keep the lock inode in place.  Removing a flock file permits a
        # second process to lock a new inode while an existing waiter still
        # holds the old one.
        del parent


def _fsync_directory(parent: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    dir_fd = os.open(parent, flags)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _unlink_if_present(path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


def _atomic_identity_write(identity: RNS.Identity, destination: str) -> None:
    parent = _ensure_parent(destination)
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=parent,
            prefix=f".{os.path.basename(destination)}.",
            suffix=".tmp",
        )
        os.fchmod(fd, 0o600)
        os.close(fd)
        identity.to_file(tmp_path)
        if not os.path.isfile(tmp_path):
            raise OSError("RNS did not create the identity file")
        os.chmod(tmp_path, 0o600)
        with open(tmp_path, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp_path, destination)
        tmp_path = None
        os.chmod(destination, 0o600)
        _fsync_directory(parent)
    except Exception as exc:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
        if isinstance(exc, IdentityPersistenceError):
            raise
        raise IdentityPersistenceError(f"Cannot persist identity at {destination}: {exc}") from exc


def _read_identity_bytes(path: str) -> bytes:
    """Bind and read one private regular identity file without following links."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise IdentityPersistenceError(f"Cannot open identity file {path}: {exc}") from exc
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise IdentityPersistenceError(
                f"Identity file is not a single-link regular file: {path}"
            )
        try:
            os.fchmod(fd, 0o600)
        except OSError as exc:
            raise IdentityPersistenceError(f"Cannot secure identity file {path}: {exc}") from exc
        try:
            os.fsync(fd)
        except OSError as exc:
            raise IdentityPersistenceError(f"Cannot sync identity file {path}: {exc}") from exc
        chunks: list[bytes] = []
        remaining = _MAX_IDENTITY_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > _MAX_IDENTITY_BYTES:
            raise IdentityPersistenceError(
                f"Identity file exceeds {_MAX_IDENTITY_BYTES} bytes: {path}"
            )
        return payload
    finally:
        os.close(fd)


def _stage_identity_bytes(payload: bytes, destination: str) -> str:
    """Write bound candidate bytes to a private, fsynced same-directory file."""

    parent = _ensure_parent(destination)
    fd, staged_path = tempfile.mkstemp(
        dir=parent,
        prefix=f".{os.path.basename(destination)}.restore-",
        suffix=".tmp",
    )
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write while staging identity restore")
            view = view[written:]
        os.fsync(fd)
    except Exception as exc:
        os.close(fd)
        _unlink_if_present(staged_path)
        raise IdentityPersistenceError(
            f"Cannot stage identity bytes for {destination}: {exc}"
        ) from exc
    os.close(fd)
    return staged_path


def _identity_hash(identity: RNS.Identity) -> bytes | None:
    value = getattr(identity, "hash", None)
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    return None


def _atomic_identity_bytes(payload: bytes, destination: str) -> None:
    """Publish already-bound identity bytes atomically and durably."""

    parent = _ensure_parent(destination)
    staged_path = _stage_identity_bytes(payload, destination)
    try:
        os.replace(staged_path, destination)
        staged_path = ""
        os.chmod(destination, 0o600)
        _fsync_directory(parent)
    except Exception as exc:
        if staged_path:
            _unlink_if_present(staged_path)
        if isinstance(exc, IdentityPersistenceError):
            raise
        raise IdentityPersistenceError(
            f"Cannot persist identity bytes at {destination}: {exc}"
        ) from exc


def _preserve_corrupt_identity(identity_path: str) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    base = f"{identity_path}.corrupt-{stamp}-{os.getpid()}"
    backup_path = base
    counter = 0
    while os.path.exists(backup_path):
        counter += 1
        backup_path = f"{base}-{counter}"
    os.replace(identity_path, backup_path)
    os.chmod(backup_path, 0o600)
    _fsync_directory(os.path.dirname(identity_path) or ".")
    return backup_path


def load_or_create(identity_path: str) -> RNS.Identity:
    """Load a durable identity, or serialize creation of exactly one.

    A sibling ``.lock`` file is held while the target is re-read, a corrupt
    target is preserved, and a replacement is atomically persisted.  Failure
    to persist raises :class:`IdentityPersistenceError`; an ephemeral identity
    is never returned.
    """

    identity_path = _expanded(identity_path)
    with _identity_lock(identity_path):
        if os.path.lexists(identity_path):
            load_error: Exception | None = None
            existing_bytes = _read_identity_bytes(identity_path)
            existing_digest = hashlib.sha256(existing_bytes).digest()
            staged_path = _stage_identity_bytes(existing_bytes, identity_path)
            try:
                identity = RNS.Identity.from_file(staged_path)
            except Exception as exc:
                # Reticulum implementations may report malformed bytes by
                # returning None or by raising. Both are corruption signals;
                # preserve the exact bytes before creating a replacement.
                identity = None
                load_error = exc
            finally:
                _unlink_if_present(staged_path)
            if identity is not None:
                durable_bytes = _read_identity_bytes(identity_path)
                if hashlib.sha256(durable_bytes).digest() != existing_digest:
                    raise IdentityPersistenceError(
                        f"Identity at {identity_path} changed while it was being loaded"
                    )
                _fsync_directory(os.path.dirname(identity_path) or ".")
                log.info("Loaded existing identity from %s", identity_path)
                return identity

            if load_error is not None and not isinstance(load_error, IdentityPersistenceError):
                try:
                    durable_bytes = _read_identity_bytes(identity_path)
                except IdentityPersistenceError:
                    raise
                if hashlib.sha256(durable_bytes).digest() != existing_digest:
                    raise IdentityPersistenceError(
                        f"Identity at {identity_path} changed while corruption was assessed"
                    )

            try:
                backup_path = _preserve_corrupt_identity(identity_path)
            except OSError as exc:
                raise IdentityPersistenceError(
                    f"Cannot preserve corrupt identity at {identity_path}: {exc}"
                ) from exc
            log.warning(
                "Invalid identity at %s preserved as %s%s; creating a replacement",
                identity_path,
                backup_path,
                f" ({load_error})" if load_error is not None else "",
            )

        identity = RNS.Identity()
        _atomic_identity_write(identity, identity_path)

        # Verify that the durable bytes are loadable and return that instance,
        # not the in-memory object that happened to write them.
        persisted_bytes = _read_identity_bytes(identity_path)
        persisted_digest = hashlib.sha256(persisted_bytes).digest()
        staged_path = _stage_identity_bytes(persisted_bytes, identity_path)
        try:
            persisted = RNS.Identity.from_file(staged_path)
        except Exception as exc:
            raise IdentityPersistenceError(
                f"Cannot verify identity at {identity_path}: {exc}"
            ) from exc
        finally:
            _unlink_if_present(staged_path)
        if persisted is None:
            raise IdentityPersistenceError(
                f"Persisted identity at {identity_path} failed verification"
            )
        if hashlib.sha256(_read_identity_bytes(identity_path)).digest() != persisted_digest:
            raise IdentityPersistenceError(
                f"Persisted identity at {identity_path} changed during verification"
            )
        log.info("Created and durably saved identity to %s", identity_path)
        return persisted


def backup_identity(identity_path: str, backup_path: str) -> None:
    """Create an atomic, mode-0600 backup while holding the identity lock."""

    identity_path = _expanded(identity_path)
    backup_path = _expanded(backup_path)
    with _identity_lock(identity_path):
        if not os.path.lexists(identity_path):
            raise IdentityPersistenceError(f"Identity not found at {identity_path}")
        identity_bytes = _read_identity_bytes(identity_path)
        identity_digest = hashlib.sha256(identity_bytes).digest()
        staged_path = _stage_identity_bytes(identity_bytes, identity_path)
        try:
            identity = RNS.Identity.from_file(staged_path)
        except Exception as exc:
            raise IdentityPersistenceError(
                f"Cannot validate identity at {identity_path}: {exc}"
            ) from exc
        finally:
            _unlink_if_present(staged_path)
        if identity is None:
            raise IdentityPersistenceError(f"Identity at {identity_path} is invalid")
        if hashlib.sha256(_read_identity_bytes(identity_path)).digest() != identity_digest:
            raise IdentityPersistenceError(
                f"Identity at {identity_path} changed while its backup was prepared"
            )
        _atomic_identity_bytes(identity_bytes, backup_path)


def restore_identity(identity_path: str, backup_path: str) -> RNS.Identity:
    """Atomically restore and verify an identity while holding its lock."""

    identity_path = _expanded(identity_path)
    backup_path = _expanded(backup_path)
    with _identity_lock(identity_path):
        parent = os.path.dirname(identity_path) or "."
        safety_path = f"{identity_path}.pre-restore-{os.getpid()}-{time.time_ns()}"
        staged_path: str | None = None
        if not os.path.isfile(backup_path):
            raise IdentityPersistenceError(f"Identity backup not found at {backup_path}")
        try:
            candidate_bytes = _read_identity_bytes(backup_path)
            candidate_digest = hashlib.sha256(candidate_bytes).digest()
            staged_path = _stage_identity_bytes(candidate_bytes, identity_path)
            try:
                candidate = RNS.Identity.from_file(staged_path)
            except Exception as exc:
                raise IdentityPersistenceError(f"Invalid identity backup: {exc}") from exc
            if candidate is None:
                raise IdentityPersistenceError("Invalid identity backup")
            candidate_hash = _identity_hash(candidate)
        except BaseException:
            if staged_path is not None:
                _unlink_if_present(staged_path)
            raise

        had_original = os.path.lexists(identity_path)
        if had_original:
            try:
                original_bytes = _read_identity_bytes(identity_path)
                _atomic_identity_bytes(original_bytes, safety_path)
            except BaseException:
                if staged_path is not None:
                    _unlink_if_present(staged_path)
                raise
        restore_succeeded = False
        try:
            os.replace(staged_path, identity_path)
            staged_path = None
            os.chmod(identity_path, 0o600)
            _fsync_directory(parent)
            restored_bytes = _read_identity_bytes(identity_path)
            restored_stage = _stage_identity_bytes(restored_bytes, identity_path)
            try:
                restored = RNS.Identity.from_file(restored_stage)
            except Exception as exc:
                raise IdentityPersistenceError(
                    f"Restored identity at {identity_path} failed verification: {exc}"
                ) from exc
            finally:
                _unlink_if_present(restored_stage)
            if restored is None:
                raise IdentityPersistenceError(
                    f"Restored identity at {identity_path} failed verification"
                )
            restored_hash = _identity_hash(restored)
            if hashlib.sha256(restored_bytes).digest() != candidate_digest or (
                candidate_hash is not None
                and restored_hash is not None
                and restored_hash != candidate_hash
            ):
                raise IdentityPersistenceError(
                    f"Restored identity at {identity_path} does not match the locked backup"
                )
            restore_succeeded = True
        except BaseException as restore_error:
            if had_original and os.path.isfile(safety_path):
                try:
                    # Copy, rather than move, so a failed rollback leaves the
                    # last known-good bytes available for operator recovery.
                    _atomic_identity_bytes(_read_identity_bytes(safety_path), identity_path)
                    os.unlink(safety_path)
                    _fsync_directory(parent)
                except BaseException as rollback_error:
                    raise IdentityPersistenceError(
                        "Identity restore failed and rollback also failed; "
                        f"the last known-good safety copy is preserved at {safety_path}: "
                        f"{rollback_error}"
                    ) from restore_error
            elif not had_original:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(identity_path)
                    _fsync_directory(parent)
            raise
        finally:
            if staged_path is not None:
                _unlink_if_present(staged_path)
            if restore_succeeded:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(safety_path)
                    _fsync_directory(parent)
        return restored
