#!/usr/bin/env python3
"""Inspect SQLite metadata without creating locks, journals, or sidecar files.

The immutable URI deliberately ignores uncheckpointed WAL content.  This tool is
therefore an inventory/preflight aid, not a substitute for the administrator's
clone-based migration dry run and verified SQLite backup.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import stat
from pathlib import Path
from urllib.parse import quote


def _regular_file(path: Path) -> None:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"SQLite target must be a regular file: {path}")


def audit_database(raw_path: str | Path) -> dict[str, object]:
    """Return secret-free structural metadata for one immutable SQLite view."""

    path = Path(raw_path).expanduser().absolute()
    _regular_file(path)
    wal = path.with_name(f"{path.name}-wal")
    shm = path.with_name(f"{path.name}-shm")
    uri = f"file:{quote(path.as_posix(), safe='/')}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=0)
    try:
        connection.execute("PRAGMA query_only = ON")
        quick_check = [row[0] for row in connection.execute("PRAGMA quick_check")]
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            "path": str(path),
            "bytes": path.stat().st_size,
            "quick_check": quick_check,
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
            "page_count": connection.execute("PRAGMA page_count").fetchone()[0],
            "freelist_count": connection.execute("PRAGMA freelist_count").fetchone()[0],
            "tables": tables,
            "wal_present": wal.is_file(),
            "wal_bytes": wal.stat().st_size if wal.is_file() else 0,
            "shm_present": shm.is_file(),
        }
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", nargs="+", help="SQLite files to inspect")
    arguments = parser.parse_args(argv)
    results: list[dict[str, object]] = []
    failed = False
    for value in arguments.database:
        try:
            results.append(audit_database(value))
        except (OSError, sqlite3.Error, ValueError) as exc:
            failed = True
            results.append({"path": str(Path(value).expanduser().absolute()), "error": str(exc)})
    print(json.dumps(results, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
