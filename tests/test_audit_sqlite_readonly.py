from __future__ import annotations

import sqlite3

import pytest

from tools.audit_sqlite_readonly import audit_database, main


def test_audit_database_reports_structure_without_sidecars(tmp_path):
    database = tmp_path / "state.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("PRAGMA user_version = 7")
        connection.commit()
    finally:
        connection.close()

    before = set(tmp_path.iterdir())
    result = audit_database(database)

    assert result["quick_check"] == ["ok"]
    assert result["user_version"] == 7
    assert result["tables"] == ["sample"]
    assert result["wal_present"] is False
    assert set(tmp_path.iterdir()) == before


def test_audit_database_rejects_symlink(tmp_path):
    database = tmp_path / "state.db"
    database.write_bytes(b"not sqlite")
    alias = tmp_path / "alias.db"
    alias.symlink_to(database)

    with pytest.raises(ValueError, match="regular file"):
        audit_database(alias)


def test_main_reports_each_failure(tmp_path, capsys):
    missing = tmp_path / "missing.db"

    assert main([str(missing)]) == 1
    assert "missing.db" in capsys.readouterr().out
