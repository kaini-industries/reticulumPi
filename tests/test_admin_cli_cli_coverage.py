"""Focused branch coverage for safe ``reticulumpi-admin`` CLI control flow."""

from __future__ import annotations

import builtins
import os
import shutil
import sqlite3
import subprocess
from contextlib import closing, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

import reticulumpi.admin_cli as admin


@pytest.fixture
def admin_paths(tmp_path, monkeypatch):
    paths = SimpleNamespace(
        root=tmp_path / "opt/reticulumpi",
        config=tmp_path / "etc/reticulumpi",
        data=tmp_path / "var/lib/reticulumpi",
        backups=tmp_path / "var/backups/reticulumpi",
    )
    monkeypatch.setattr(admin, "CONFIG_FILE", paths.config / "config.yaml")
    monkeypatch.setattr(admin, "DATA_DIR", paths.data)
    monkeypatch.setattr(admin, "BACKUP_DIR", paths.backups)
    monkeypatch.setattr(admin, "MANIFEST_FILE", paths.config / "install.json")
    monkeypatch.setattr(admin, "JOURNAL_FILE", paths.backups / "admin/transaction.json")
    return paths


def _sqlite(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE records(value TEXT)")
    return path


def _platform_metadata(profile: str) -> dict[str, object]:
    if profile == "bookworm":
        return admin._preflight_platform(
            system="Linux",
            machine="aarch64",
            version_info=(3, 11, 9),
            os_release={"ID": "debian", "VERSION_ID": "12", "VERSION_CODENAME": "bookworm"},
        )
    return admin._preflight_platform(
        system="Linux",
        machine="aarch64",
        version_info=(3, 12, 3),
        os_release={"ID": "ubuntu", "VERSION_ID": "24.04", "VERSION_CODENAME": "noble"},
    )


def test_command_runner_forwards_modes_and_normalizes_captured_output(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout="  ready\n")

    monkeypatch.setattr(admin.subprocess, "run", run)
    assert admin._run(["tool", "status"], check=False, capture=True) == "ready"
    assert admin._run(["tool", "restart"]) == ""
    assert calls == [
        (
            ["tool", "status"],
            {"check": False, "text": True, "capture_output": True},
        ),
        (
            ["tool", "restart"],
            {"check": True, "text": True, "capture_output": False},
        ),
    ]


def test_platform_metadata_accepts_both_lanes_and_rejects_each_shape():
    bookworm = _platform_metadata("bookworm")
    noble = _platform_metadata("noble")
    assert admin._validate_platform_metadata(bookworm) == bookworm
    assert admin._validate_platform_metadata(noble) == noble
    assert admin._validate_platform_metadata(None) is None

    invalid_values = [
        ([], "object or null"),
        ({key: value for key, value in noble.items() if key != "python"}, "missing or invalid"),
        ({**noble, "system": "Darwin"}, "supported lane"),
        ({**noble, "profile_key": "linux-arm64-unknown"}, "supported lane"),
        ({**bookworm, "python_series": "3.12"}, "supported lane"),
    ]
    for value, message in invalid_values:
        with pytest.raises(admin.AdminError, match=message):
            admin._validate_platform_metadata(value)


def test_os_release_wraps_read_failures(tmp_path, monkeypatch):
    metadata = tmp_path / "os-release"
    metadata.write_text('ID="ubuntu"\n', encoding="utf-8")
    real_read_text = Path.read_text

    def fail_for_metadata(path, *args, **kwargs):
        if path == metadata:
            raise UnicodeError("invalid bytes")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_for_metadata)
    with pytest.raises(admin.AdminError, match="cannot read operating-system metadata"):
        admin._read_os_release(metadata)


def test_service_probes_fail_closed_and_restart_uses_runner(monkeypatch):
    monkeypatch.setattr(
        admin.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("systemd unavailable")),
    )
    assert admin._service_active("reticulumpi.service") is False
    assert admin._unit_enabled("reticulumpi.service") is False

    calls = []
    monkeypatch.setattr(admin, "_run", lambda command: calls.append(command))
    monkeypatch.setattr(admin, "_wait_service_active", lambda name: calls.append(["wait", name]))
    admin._restart_and_wait("reticulumpi.service")
    assert calls == [
        [admin.SYSTEMCTL, "restart", "reticulumpi.service"],
        ["wait", "reticulumpi.service"],
    ]


def test_dashboard_readiness_wait_has_deterministic_success_and_timeout(monkeypatch):
    ticks = iter((0.0, 1.0, 1.0, 1.0))
    monkeypatch.setattr(admin.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(admin, "_service_active", lambda _name: True)
    monkeypatch.setattr(admin, "_dashboard_readiness_marker_valid", lambda: True)
    admin._wait_dashboard_ready(timeout=10.0, stable_for=0.0)

    ticks = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr(admin.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(admin, "_service_active", lambda _name: False)
    monkeypatch.setattr(admin.time, "sleep", lambda _delay: None)
    with pytest.raises(admin.AdminError, match="fresh readiness marker"):
        admin._wait_dashboard_ready(timeout=0.5)


def test_doctor_reports_manifest_journal_and_integrity_failures(admin_paths, monkeypatch, capsys):
    admin_paths.config.mkdir(parents=True)
    admin.CONFIG_FILE.write_text("reticulumpi: {}\n", encoding="utf-8")
    admin.MANIFEST_FILE.write_text("{}\n", encoding="utf-8")
    release = admin_paths.root / "releases/0.3.2"
    release.mkdir(parents=True)
    (admin_paths.root / "current").symlink_to(release)
    monkeypatch.setattr(
        admin,
        "_load_manifest",
        lambda: {"install_root": str(admin_paths.root), "release": str(release)},
    )
    monkeypatch.setattr(admin, "_validate_release", lambda _root, active: active)
    monkeypatch.setattr(
        admin,
        "_journal_state",
        lambda: (
            {"state": "recovered", "recovery_evidence": {"services_restored": False}},
            False,
        ),
    )
    monkeypatch.setattr(admin, "_databases", lambda: (admin_paths.data / "records.db",))

    class BadIntegrity:
        def execute(self, _sql):
            return SimpleNamespace(fetchone=lambda: ("corrupt",))

        def close(self):
            pass

    monkeypatch.setattr(admin.sqlite3, "connect", lambda *_args, **_kwargs: BadIntegrity())
    assert admin._doctor(SimpleNamespace()) == 1
    output = capsys.readouterr().out
    assert "missing durable recovery evidence" in output
    assert "database integrity failed" in output
    assert "current release does not match" not in output

    monkeypatch.setattr(
        admin,
        "_load_manifest",
        lambda: (_ for _ in ()).throw(admin.AdminError("invalid manifest")),
    )
    monkeypatch.setattr(
        admin,
        "_journal_state",
        lambda: (_ for _ in ()).throw(admin.AdminError("invalid journal")),
    )
    monkeypatch.setattr(admin, "_databases", lambda: ())
    assert admin._doctor(SimpleNamespace()) == 1
    output = capsys.readouterr().out
    assert "invalid manifest" in output
    assert "invalid journal" in output


def test_doctor_accepts_complete_recovery_evidence(admin_paths, monkeypatch, capsys):
    admin_paths.config.mkdir(parents=True)
    admin.CONFIG_FILE.write_text("reticulumpi: {}\n", encoding="utf-8")
    admin.CONFIG_FILE.chmod(0o600)
    monkeypatch.setattr(
        admin,
        "_journal_state",
        lambda: (
            {"state": "recovered", "recovery_evidence": {"services_restored": True}},
            False,
        ),
    )
    monkeypatch.setattr(admin, "_databases", lambda: ())
    real_stat = Path.stat

    def root_owned(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == admin.CONFIG_FILE:
            return SimpleNamespace(st_mode=result.st_mode, st_uid=0)
        return result

    monkeypatch.setattr(Path, "stat", root_owned)
    assert admin._doctor(SimpleNamespace()) == 0
    assert "checks passed" in capsys.readouterr().out


def test_migration_target_inspection_and_missing_backup_listing_fail_closed(
    admin_paths, tmp_path, monkeypatch
):
    target = SimpleNamespace(name="records", path=tmp_path / "records.db")
    monkeypatch.setattr(admin, "_reject_symlink_components", lambda _path: None)
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda path: (
            (_ for _ in ()).throw(OSError("denied")) if path == target.path else os.lstat(path)
        ),
    )
    with pytest.raises(admin.AdminError, match="cannot inspect migration target"):
        admin._validate_migration_target(target, set(), set())
    assert admin._db_backups(SimpleNamespace()) == 0


def test_database_discovery_and_migration_registry_avoids_plugin_imports(admin_paths, monkeypatch):
    unsafe = admin_paths.data / "directory.db"
    unsafe.mkdir(parents=True)
    with pytest.raises(admin.AdminError, match="unsafe database path"):
        admin._databases()

    admin.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    admin.CONFIG_FILE.write_text(
        "reticulumpi:\n  plugins:\n    messaging_hub:\n      enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(admin, "_service_home", lambda: admin_paths.data)

    real_import = builtins.__import__

    def reject_builtin_plugin(name, *args, **kwargs):
        if name == "reticulumpi.builtin_plugins.messaging_hub":
            raise ImportError("unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_builtin_plugin)
    targets = admin._load_enabled_migration_targets()
    assert [target.name for target in targets] == ["messaging_hub"]


def test_service_home_rejects_nonabsolute_resolution(monkeypatch):
    monkeypatch.setattr(admin, "_service_account", lambda: None)
    monkeypatch.setattr(admin, "DATA_DIR", SimpleNamespace(resolve=lambda: Path("relative")))
    with pytest.raises(admin.AdminError, match="invalid home directory"):
        admin._service_home()


def test_existing_database_is_cloned_for_dry_run(tmp_path, monkeypatch):
    source = _sqlite(tmp_path / "records.db")
    target = SimpleNamespace(name="records", path=source, migrations=())
    copied = []
    monkeypatch.setattr(admin, "_sqlite_backup_file", lambda src, dst: copied.append((src, dst)))

    import reticulumpi.migrations as migrations

    result = object()
    monkeypatch.setattr(migrations, "migrate_target", lambda *_args, **_kwargs: result)
    assert admin._dry_run_migration(target) is result
    assert copied and copied[0][0] == source
    assert copied[0][1].name == source.name


def test_apply_migration_handles_target_not_created(admin_paths, tmp_path, monkeypatch, capsys):
    target = SimpleNamespace(name="records", path=tmp_path / "records.db", migrations=())
    monkeypatch.setattr(admin, "_load_enabled_migration_targets", lambda: (target,))
    monkeypatch.setattr(admin, "_require_root", lambda: None)
    monkeypatch.setattr(admin, "_maintenance_lock", lambda: nullcontext())
    monkeypatch.setattr(admin, "_service_active", lambda _name: False)
    monkeypatch.setattr(
        admin,
        "_service_account",
        lambda: SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()),
    )
    monkeypatch.setattr(admin, "_ensure_real_directory", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(admin.os, "chown", lambda *_args: None)

    import reticulumpi.migrations as migrations

    monkeypatch.setattr(
        migrations,
        "migrate_target",
        lambda *_args, **_kwargs: SimpleNamespace(
            applied=(), from_version=0, to_version=0, backup_path=None
        ),
    )
    assert admin._db_migrate(SimpleNamespace(apply=True)) == 0
    assert "applied=none backup=none" in capsys.readouterr().out


def test_apply_migration_wraps_runner_failures(admin_paths, tmp_path, monkeypatch):
    target = SimpleNamespace(name="records", path=tmp_path / "records.db", migrations=())
    monkeypatch.setattr(admin, "_load_enabled_migration_targets", lambda: (target,))
    monkeypatch.setattr(admin, "_require_root", lambda: None)
    monkeypatch.setattr(admin, "_maintenance_lock", lambda: nullcontext())
    monkeypatch.setattr(admin, "_service_active", lambda _name: False)
    monkeypatch.setattr(
        admin,
        "_service_account",
        lambda: SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()),
    )
    monkeypatch.setattr(admin, "_ensure_real_directory", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(admin.os, "chown", lambda *_args: None)

    import reticulumpi.migrations as migrations

    monkeypatch.setattr(
        migrations,
        "migrate_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("write failed")),
    )
    with pytest.raises(admin.AdminError, match="migration failed: write failed"):
        admin._db_migrate(SimpleNamespace(apply=True))


def test_database_restore_covers_missing_canonical_source_and_new_target_owner(
    admin_paths, tmp_path, monkeypatch, capsys
):
    source = _sqlite(tmp_path / "backup.db")
    target = admin_paths.data / "restored.db"
    admin_paths.data.mkdir(parents=True)

    import reticulumpi.migrations as migrations

    missing = tmp_path / "vanished.db"
    monkeypatch.setattr(
        migrations,
        "_canonicalize_trusted_ancestors",
        lambda path: missing if path == source else path.absolute(),
    )
    args = SimpleNamespace(backup=str(source), database=str(target), apply=False)
    with pytest.raises(admin.AdminError, match="backup does not exist"):
        admin._db_restore(args)

    monkeypatch.setattr(migrations, "_canonicalize_trusted_ancestors", lambda path: path.absolute())
    assert admin._db_restore(args) == 0
    output = capsys.readouterr().out
    assert "Would replace" in output
    assert "safety backup" not in output

    monkeypatch.setattr(admin, "_require_root", lambda: None)
    monkeypatch.setattr(admin, "_maintenance_lock", lambda: nullcontext())
    monkeypatch.setattr(admin, "_service_active", lambda _name: False)
    monkeypatch.setattr(
        admin.pwd,
        "getpwnam",
        lambda _name: SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()),
    )
    monkeypatch.setattr(admin.os, "chown", lambda *_args: None)
    monkeypatch.setattr(migrations, "restore_database", lambda src, dst: shutil.copy2(src, dst))
    assert (
        admin._db_restore(SimpleNamespace(backup=str(source), database=str(target), apply=True))
        == 0
    )
    assert target.is_file()


def test_database_restore_rejects_existing_directory_target(admin_paths, tmp_path, monkeypatch):
    source = _sqlite(tmp_path / "backup.db")
    target = admin_paths.data / "directory.db"
    target.mkdir(parents=True)

    import reticulumpi.migrations as migrations

    monkeypatch.setattr(migrations, "_canonicalize_trusted_ancestors", lambda path: path.absolute())
    monkeypatch.setattr(admin, "_verify_sqlite", lambda _path: None)
    monkeypatch.setattr(admin, "_require_root", lambda: None)
    monkeypatch.setattr(admin, "_maintenance_lock", lambda: nullcontext())
    monkeypatch.setattr(admin, "_service_active", lambda _name: False)
    with pytest.raises(admin.AdminError, match="restore target is not a regular file"):
        admin._db_restore(SimpleNamespace(backup=str(source), database=str(target), apply=True))


def test_database_restore_requires_service_owner_for_new_target(admin_paths, tmp_path, monkeypatch):
    source = _sqlite(tmp_path / "backup.db")
    target = admin_paths.data / "restored.db"
    admin_paths.data.mkdir(parents=True)

    import reticulumpi.migrations as migrations

    monkeypatch.setattr(migrations, "_canonicalize_trusted_ancestors", lambda path: path.absolute())
    monkeypatch.setattr(admin, "_require_root", lambda: None)
    monkeypatch.setattr(admin, "_maintenance_lock", lambda: nullcontext())
    monkeypatch.setattr(admin, "_service_active", lambda _name: False)
    monkeypatch.setattr(
        admin.pwd,
        "getpwnam",
        lambda _name: (_ for _ in ()).throw(KeyError("missing")),
    )
    with pytest.raises(admin.AdminError, match="service user"):
        admin._db_restore(SimpleNamespace(backup=str(source), database=str(target), apply=True))


def test_main_converts_admin_errors_and_returns_fallback_when_parser_error_returns(monkeypatch):
    messages = []
    parser = SimpleNamespace(
        parse_args=lambda _argv: SimpleNamespace(
            handler=lambda _args: (_ for _ in ()).throw(admin.AdminError("unsafe request"))
        ),
        error=messages.append,
    )
    monkeypatch.setattr(admin, "_build_parser", lambda: parser)
    assert admin.main([]) == 2
    assert messages == ["unsafe request"]

    parser.parse_args = lambda _argv: SimpleNamespace(
        handler=lambda _args: (_ for _ in ()).throw(
            subprocess.CalledProcessError(9, ["systemctl", "restart", "reticulumpi.service"])
        )
    )
    assert admin.main([]) == 2
    assert messages[-1] == "command failed (9): systemctl restart reticulumpi.service"


def test_parser_dispatches_every_database_command():
    parser = admin._build_parser()
    cases = {
        "plan": ["db", "plan"],
        "migrate": ["db", "migrate", "--dry-run"],
        "backup": ["db", "backup", "--dry-run"],
        "backups": ["db", "backups"],
        "restore": ["db", "restore", "/tmp/source.db", "--database", "/tmp/target.db"],
    }
    for command, argv in cases.items():
        parsed = parser.parse_args(argv)
        assert parsed.db_command == command
        assert callable(parsed.handler)
