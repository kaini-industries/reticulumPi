#!/usr/bin/env python3
"""Emit a secret-free fingerprint of state that must survive container recreation."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path


DATA = Path("/data")


def _assert_beneath_data(path: Path) -> None:
    """Reject links and paths that escape the durable volume."""

    try:
        relative = path.relative_to(DATA)
    except ValueError as exc:
        raise SystemExit(f"durable path is outside /data: {path}") from exc
    current = DATA
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise SystemExit(f"required durable path is missing: {current}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise SystemExit(f"durable path must not contain a symbolic link: {current}")
    try:
        path.resolve(strict=True).relative_to(DATA.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"durable path escapes /data: {path}") from exc


def _required(path: Path, *, mode: int | None = None) -> Path:
    _assert_beneath_data(path)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"required durable file is not regular: {path}")
    if mode is not None and stat.S_IMODE(metadata.st_mode) != mode:
        raise SystemExit(
            f"durable file has unsafe mode {stat.S_IMODE(metadata.st_mode):04o}: {path}"
        )
    if metadata.st_uid != os.getuid() or metadata.st_gid != os.getgid():
        raise SystemExit(f"durable file is not owned by the container account: {path}")
    return path


def _required_directory(path: Path) -> Path:
    _assert_beneath_data(path)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise SystemExit(f"required durable directory is not a directory: {path}")
    return path


def _public_tree_fingerprint(root: Path) -> dict[str, object]:
    _required_directory(root)
    files: list[Path] = []
    for path in root.rglob("*"):
        _assert_beneath_data(path)
        metadata = path.lstat()
        if stat.S_ISREG(metadata.st_mode):
            files.append(path)
        elif not stat.S_ISDIR(metadata.st_mode):
            raise SystemExit(f"durable content tree contains a special file: {path}")
    files.sort()
    if not files:
        raise SystemExit(f"durable content tree is empty: {root}")
    digest = hashlib.sha256()
    names: list[str] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        names.append(relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return {"files": names, "sha256": digest.hexdigest()}


def _secret_file_identity(path: Path) -> dict[str, int]:
    """Prove the same file survived without exposing a reusable verifier."""

    metadata = _required(path, mode=0o600).stat()
    return {
        "inode": metadata.st_ino,
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "mode": metadata.st_mode & 0o777,
    }


def _database_state(path: Path) -> dict[str, object]:
    metadata = _required(path, mode=0o600).stat()
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise SystemExit(f"SQLite integrity failure: {path}")
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()
    return {
        "inode": metadata.st_ino,
        "mode": stat.S_IMODE(metadata.st_mode),
        "integrity": "ok",
        "tables": tables,
        "user_version": version,
    }


def main() -> None:
    identity = _required(DATA / ".config/reticulumpi/identity", mode=0o600)
    dashboard = DATA / ".config/reticulumpi/dashboard"
    state = {
        "identity_sha256": hashlib.sha256(identity.read_bytes()).hexdigest(),
        "dashboard_secret": _secret_file_identity(dashboard / "dashboard_secret"),
        "dashboard_bootstrap": _secret_file_identity(dashboard / "dashboard_password.txt"),
        "nomadnet_pages": _public_tree_fingerprint(DATA / ".nomadnet/storage/pages"),
        "databases": {
            "messages": _database_state(DATA / ".local/share/reticulumpi/messaging_hub.db"),
            "sessions": _database_state(dashboard / "sessions.db"),
        },
    }
    print(json.dumps(state, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
