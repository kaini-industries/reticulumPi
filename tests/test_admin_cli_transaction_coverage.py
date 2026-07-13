"""Focused fail-closed coverage for admin transactions and credential recovery."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

import reticulumpi.admin_cli as admin


@pytest.fixture
def transaction_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    paths = SimpleNamespace(
        root=tmp_path / "srv/reticulumpi",
        config=tmp_path / "etc/reticulumpi",
        data=tmp_path / "var/lib/reticulumpi",
        backups=tmp_path / "var/backups/reticulumpi",
        systemd=tmp_path / "etc/systemd/system",
    )
    monkeypatch.setattr(admin, "CONFIG_DIR", paths.config)
    monkeypatch.setattr(admin, "CONFIG_FILE", paths.config / "config.yaml")
    monkeypatch.setattr(admin, "DATA_DIR", paths.data)
    monkeypatch.setattr(admin, "BACKUP_DIR", paths.backups)
    monkeypatch.setattr(admin, "ADMIN_STATE_DIR", paths.backups / "admin")
    monkeypatch.setattr(admin, "JOURNAL_FILE", paths.backups / "admin/transaction.json")
    monkeypatch.setattr(admin, "SYSTEMD_DIR", paths.systemd)
    monkeypatch.setattr(admin, "MANIFEST_FILE", paths.config / "install.json")
    monkeypatch.setattr(admin, "_validate_install_root_ancestry", lambda _path: None)
    return paths


def _services() -> dict[str, dict[str, bool]]:
    return {name: {"active": False, "enabled": False} for name in admin._TRANSACTION_SERVICE_NAMES}


def _recovery_journal(paths, candidate: Path, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": 1,
        "state": "switching",
        "install_root": str(paths.root),
        "previous_release": None,
        "new_release": str(candidate),
        "remove_candidate": False,
        "backup": str(paths.backups / "release-recovery"),
        "services_before": _services(),
    }
    value.update(overrides)
    return value


def _stub_recovery_io(monkeypatch: pytest.MonkeyPatch, journal: dict[str, object]) -> None:
    monkeypatch.setattr(admin, "_journal_state", lambda: (journal, True))
    monkeypatch.setattr(admin, "_load_file_snapshots", lambda _backup: ())
    monkeypatch.setattr(admin, "_restore_files", lambda _snapshots: None)
    monkeypatch.setattr(admin, "_restore_current", lambda *_args: None)
    monkeypatch.setattr(admin, "_restore_state_backup", lambda _backup: None)
    monkeypatch.setattr(admin, "_run", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(admin, "_restore_service_states", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(admin, "_atomic_json", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(admin, "_backup_roots_from_metadata", lambda _metadata: ())
    monkeypatch.setattr(admin, "_backup_configuration_file", lambda _metadata: None)


def test_recovery_rejects_backup_outside_managed_root(
    transaction_paths, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    candidate = transaction_paths.root / "releases/0.3.2"
    journal = _recovery_journal(
        transaction_paths, candidate, backup=str(tmp_path / "outside/release-recovery")
    )
    _stub_recovery_io(monkeypatch, journal)
    monkeypatch.setattr(admin, "_safe_install_root", lambda _raw: transaction_paths.root)
    with pytest.raises(admin.AdminError, match="outside the managed backup root"):
        admin._recover_interrupted_transaction(transaction_paths.root)


def test_recovery_rejects_manifest_pointer_disagreement(
    transaction_paths, monkeypatch: pytest.MonkeyPatch
):
    previous = transaction_paths.root / "releases/0.3.1"
    previous.mkdir(parents=True)
    candidate = transaction_paths.root / "releases/0.3.2"
    backup = transaction_paths.backups / "release-recovery"
    backup.mkdir(parents=True)
    (backup / "backup.json").write_text(
        json.dumps({"features": [], "identity_hashes": {}}), encoding="utf-8"
    )
    transaction_paths.config.mkdir(parents=True)
    admin.MANIFEST_FILE.write_text("{}", encoding="utf-8")
    journal = _recovery_journal(transaction_paths, candidate, previous_release=str(previous))
    _stub_recovery_io(monkeypatch, journal)
    monkeypatch.setattr(admin, "_safe_install_root", lambda _raw: transaction_paths.root)
    monkeypatch.setattr(
        admin,
        "_load_manifest",
        lambda _root: {"release": str(candidate), "features": []},
    )
    with pytest.raises(admin.AdminError, match="manifest and release pointer do not agree"):
        admin._recover_interrupted_transaction(transaction_paths.root)


def test_recovery_requires_manifest_when_previous_release_exists(
    transaction_paths, monkeypatch: pytest.MonkeyPatch
):
    previous = transaction_paths.root / "releases/0.3.1"
    previous.mkdir(parents=True)
    candidate = transaction_paths.root / "releases/0.3.2"
    backup = transaction_paths.backups / "release-recovery"
    backup.mkdir(parents=True)
    (backup / "backup.json").write_text(
        json.dumps({"features": [], "identity_hashes": {}}), encoding="utf-8"
    )
    journal = _recovery_journal(transaction_paths, candidate, previous_release=str(previous))
    _stub_recovery_io(monkeypatch, journal)
    monkeypatch.setattr(admin, "_safe_install_root", lambda _raw: transaction_paths.root)
    with pytest.raises(admin.AdminError, match="installation manifest is missing"):
        admin._recover_interrupted_transaction(transaction_paths.root)


def test_recovery_without_manifest_uses_validated_backup_features(
    transaction_paths, monkeypatch: pytest.MonkeyPatch
):
    candidate = transaction_paths.root / "releases/0.3.2"
    backup = transaction_paths.backups / "release-recovery"
    backup.mkdir(parents=True)
    (backup / "backup.json").write_text(
        json.dumps({"features": ["gps"], "identity_hashes": {"identity": "hash"}}),
        encoding="utf-8",
    )
    journal = _recovery_journal(transaction_paths, candidate)
    _stub_recovery_io(monkeypatch, journal)
    monkeypatch.setattr(admin, "_safe_install_root", lambda _raw: transaction_paths.root)
    restored: list[tuple] = []
    monkeypatch.setattr(
        admin,
        "_restore_service_states",
        lambda *args, **kwargs: restored.append((*args, kwargs)),
    )
    result = admin._recover_interrupted_transaction(transaction_paths.root)
    assert result is journal
    assert result["state"] == "recovered"
    assert restored[0][1] == ("gps",)


def test_recovery_rejects_invalid_identity_and_feature_evidence(
    transaction_paths, monkeypatch: pytest.MonkeyPatch
):
    candidate = transaction_paths.root / "releases/0.3.2"
    backup = transaction_paths.backups / "release-recovery"
    backup.mkdir(parents=True)
    metadata = backup / "backup.json"
    journal = _recovery_journal(transaction_paths, candidate)
    _stub_recovery_io(monkeypatch, journal)
    monkeypatch.setattr(admin, "_safe_install_root", lambda _raw: transaction_paths.root)

    metadata.write_text(json.dumps({"features": [], "identity_hashes": []}), encoding="utf-8")
    with pytest.raises(admin.AdminError, match="invalid identity evidence"):
        admin._recover_interrupted_transaction(transaction_paths.root)

    previous = transaction_paths.root / "releases/0.3.1"
    previous.mkdir(parents=True)
    journal["previous_release"] = str(previous)
    transaction_paths.config.mkdir(parents=True)
    admin.MANIFEST_FILE.write_text("{}", encoding="utf-8")
    metadata.write_text(json.dumps({"features": ["gps"], "identity_hashes": {}}), encoding="utf-8")
    monkeypatch.setattr(
        admin,
        "_load_manifest",
        lambda _root: {"release": str(previous), "features": ["dashboard"]},
    )
    with pytest.raises(admin.AdminError, match="feature evidence do not agree"):
        admin._recover_interrupted_transaction(transaction_paths.root)


def test_recovery_refuses_to_remove_non_directory_candidate(
    transaction_paths, monkeypatch: pytest.MonkeyPatch
):
    candidate = transaction_paths.root / "releases/0.3.2"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("not a directory", encoding="utf-8")
    journal = _recovery_journal(
        transaction_paths,
        candidate,
        state="preparing",
        backup=None,
        remove_candidate=True,
    )
    _stub_recovery_io(monkeypatch, journal)
    monkeypatch.setattr(admin, "_safe_install_root", lambda _raw: transaction_paths.root)
    monkeypatch.setattr(admin, "_state_roots", lambda _features: ())
    monkeypatch.setattr(admin, "_identity_hashes", lambda *_args: {})
    with pytest.raises(admin.AdminError, match="candidate release is unsafe"):
        admin._recover_interrupted_transaction(transaction_paths.root)


def test_journal_state_rejects_unsafe_directory_symlink_and_state(
    transaction_paths, tmp_path: Path
):
    admin.JOURNAL_FILE.parent.mkdir(parents=True)
    admin.JOURNAL_FILE.parent.chmod(0o755)
    with pytest.raises(admin.AdminError, match="directory ownership or permissions are unsafe"):
        admin._journal_state()

    admin.JOURNAL_FILE.parent.chmod(0o700)
    target = tmp_path / "journal-target"
    target.write_text('{"state": "complete"}', encoding="utf-8")
    admin.JOURNAL_FILE.symlink_to(target)
    with pytest.raises(admin.AdminError, match="journal is missing or unsafe"):
        admin._journal_state()

    admin.JOURNAL_FILE.unlink()
    admin.JOURNAL_FILE.write_text('{"state": 3}', encoding="utf-8")
    admin.JOURNAL_FILE.chmod(0o600)
    with pytest.raises(admin.AdminError, match="no valid state"):
        admin._journal_state()


def test_restore_state_rejects_missing_source_and_unsafe_destination(
    transaction_paths, monkeypatch: pytest.MonkeyPatch
):
    backup = transaction_paths.backups / "release-restore"
    backup.mkdir(parents=True)
    destination = transaction_paths.data
    root = admin.StateRoot("data", destination)
    monkeypatch.setattr(admin, "_read_json_object", lambda *_args: {"schema": 2})
    monkeypatch.setattr(admin, "_restore_records", lambda *_args: ([(root, True, [])], {}))
    with pytest.raises(admin.AdminError, match="state root is missing or unsafe"):
        admin._restore_state_backup(backup)

    monkeypatch.setattr(admin, "_restore_records", lambda *_args: ([(root, False, [])], {}))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("not a directory", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="unsafe restore destination"):
        admin._restore_state_backup(backup)


def test_restore_state_rejects_preexisting_displacement_path(
    transaction_paths, monkeypatch: pytest.MonkeyPatch
):
    backup = transaction_paths.backups / "release-restore"
    backup.mkdir(parents=True)
    destination = transaction_paths.data
    destination.parent.mkdir(parents=True)
    root = admin.StateRoot("data", destination)
    monkeypatch.setattr(admin, "_read_json_object", lambda *_args: {"schema": 2})
    monkeypatch.setattr(admin, "_restore_records", lambda *_args: ([(root, False, [])], {}))
    monkeypatch.setattr(admin.time, "time_ns", lambda: 42)
    displaced = destination.parent / f".{destination.name}.pre-restore-{os.getpid()}-42"
    displaced.mkdir()
    with pytest.raises(admin.AdminError, match="displacement path already exists"):
        admin._restore_state_backup(backup)


def test_restore_state_detects_staged_manifest_race(
    transaction_paths, monkeypatch: pytest.MonkeyPatch
):
    backup = transaction_paths.backups / "release-restore"
    source = backup / "state/data"
    source.mkdir(parents=True)
    source.joinpath("value").write_text("trusted", encoding="utf-8")
    destination = transaction_paths.data
    root = admin.StateRoot("data", destination)
    manifest = [{"path": "value"}]
    monkeypatch.setattr(admin, "_read_json_object", lambda *_args: {"schema": 2})
    monkeypatch.setattr(admin, "_restore_records", lambda *_args: ([(root, True, manifest)], {}))
    manifests = iter([manifest, [{"path": "changed"}]])
    monkeypatch.setattr(admin, "_tree_manifest", lambda _path: next(manifests))
    monkeypatch.setattr(admin, "_copy_tree_verified", lambda _source, target: target.mkdir())
    with pytest.raises(admin.AdminError, match="staged state-root manifest mismatch"):
        admin._restore_state_backup(backup)


def test_restore_state_detects_absent_root_reappearing_during_switch(
    transaction_paths, monkeypatch: pytest.MonkeyPatch
):
    backup = transaction_paths.backups / "release-restore"
    backup.mkdir(parents=True)
    destination = transaction_paths.data
    destination.parent.mkdir(parents=True)
    root = admin.StateRoot("data", destination)
    monkeypatch.setattr(admin, "_read_json_object", lambda *_args: {"schema": 2})
    monkeypatch.setattr(admin, "_restore_records", lambda *_args: ([(root, False, [])], {}))

    def recreate(_parent: Path) -> None:
        destination.mkdir(exist_ok=True)

    monkeypatch.setattr(admin, "_fsync_state_directory", recreate)
    with pytest.raises(admin.AdminError, match="unexpectedly restored"):
        admin._restore_state_backup(backup)


def test_credential_dropin_parsers_handle_continuations_and_fail_closed(
    transaction_paths, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    unit = transaction_paths.systemd / "reticulumpi.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(
        "# comment\n"
        "Environment=RETICULUMPI_DASHBOARD_PASSWORD_HASH=hash \\"
        "\n OTHER=value\nEnvironmentFile=/etc/dashboard\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(admin, "_installed_service_fragments", lambda: (unit,))
    assert admin._installed_dashboard_environment() == (True, True)

    dropin = transaction_paths.systemd / "reticulumpi.service.d"
    dropin.mkdir()
    credential = dropin / "10-credential.conf"
    credential.write_text(
        "; comment\nEnvironment=OTHER=value \\\n RETICULUMPI_DASHBOARD_PASSWORD=secret\n",
        encoding="utf-8",
    )
    env_file = dropin / "20-env-file.conf"
    env_file.write_text("EnvironmentFile=/etc/dashboard\n", encoding="utf-8")
    assert admin._dashboard_credential_dropins() == (credential, env_file)

    broken = dropin / "30-broken.conf"
    broken.write_text("Environment=VALUE=one \\", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="unterminated Environment"):
        admin._dashboard_credential_dropins()
    with pytest.raises(admin.AdminError, match="unterminated directive"):
        admin._legacy_dropin_is_safe(broken)

    unsafe_root = tmp_path / "unsafe-dropins"
    unsafe_root.mkdir()
    for path in dropin.iterdir():
        path.unlink()
    dropin.rmdir()
    dropin.symlink_to(unsafe_root, target_is_directory=True)
    with pytest.raises(admin.AdminError, match="drop-in directory is unsafe"):
        admin._dashboard_credential_dropins()


def test_remove_credential_dropins_requires_unchanged_snapshot(
    transaction_paths, monkeypatch: pytest.MonkeyPatch
):
    dropin = transaction_paths.systemd / "credential.conf"
    dropin.parent.mkdir(parents=True)
    dropin.write_bytes(b"original")
    with pytest.raises(admin.AdminError, match="was not snapshotted"):
        admin._remove_legacy_dropins((dropin,), ())
    snapshot = admin.FileSnapshot(dropin, b"original", 0o600)
    dropin.write_bytes(b"changed")
    with pytest.raises(admin.AdminError, match="changed before removal"):
        admin._remove_legacy_dropins((dropin,), (snapshot,))
    dropin.write_bytes(b"original")
    monkeypatch.setattr(admin, "_fsync_directory", lambda _path: None)
    admin._remove_legacy_dropins((dropin,), (snapshot,))
    assert not dropin.exists()


def test_dashboard_config_and_secret_paths_cover_termination_and_literal_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = tmp_path / "config.yaml"
    config.write_text(
        "web_dashboard:\n  password: legacy\nnext_plugin:\n  password: unrelated\n",
        encoding="utf-8",
    )
    fields, _text = admin._dashboard_config_fields(config)
    assert fields == {"password": (1, "legacy")}
    with pytest.raises(admin.AdminError, match="not a simple path"):
        admin._dashboard_secret_dir({"secret_dir": (0, '""')})
    monkeypatch.setattr(admin.ast, "literal_eval", lambda _value: [])
    with pytest.raises(admin.AdminError, match="not a simple path"):
        admin._dashboard_secret_dir({"secret_dir": (0, '"value"')})


def test_dashboard_plan_rejects_unsafe_bootstrap_and_deduplicates_home(
    transaction_paths, monkeypatch: pytest.MonkeyPatch
):
    secret_dir = transaction_paths.data / ".config/reticulumpi"
    secret_dir.mkdir(parents=True)
    bootstrap = secret_dir / "dashboard_password.txt"
    target = secret_dir / "target"
    target.write_text("secret", encoding="utf-8")
    bootstrap.symlink_to(target)
    monkeypatch.setattr(admin, "_installed_dashboard_environment", lambda: (False, False))
    monkeypatch.setattr(admin, "_legacy_home_candidates", lambda: (transaction_paths.data,))
    with pytest.raises(admin.AdminError, match="bootstrap file is unsafe"):
        admin._plan_dashboard_credential_migration(source_replaces_unit=True)


def _database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE value(number INTEGER)")


def test_db_restore_apply_rejects_nonfile_target_and_missing_service_user(
    transaction_paths, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    source = tmp_path / "backup.db"
    _database(source)
    target = transaction_paths.data / "target.db"
    target.mkdir(parents=True)
    monkeypatch.setattr(admin, "_require_root", lambda: None)
    monkeypatch.setattr(admin, "_maintenance_lock", lambda: nullcontext())
    monkeypatch.setattr(admin, "_service_active", lambda _name: False)
    with pytest.raises(admin.AdminError, match="not a regular file"):
        admin._db_restore(
            SimpleNamespace(backup=str(source), database=str(target), apply=True, dry_run=False)
        )

    target.rmdir()
    monkeypatch.setattr(
        admin.pwd,
        "getpwnam",
        lambda _name: (_ for _ in ()).throw(KeyError("missing")),
    )
    with pytest.raises(admin.AdminError, match="service user"):
        admin._db_restore(
            SimpleNamespace(backup=str(source), database=str(target), apply=True, dry_run=False)
        )


def test_db_restore_dry_run_reports_existing_target(
    transaction_paths, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    source = tmp_path / "backup.db"
    target = transaction_paths.data / "target.db"
    _database(source)
    _database(target)
    assert (
        admin._db_restore(
            SimpleNamespace(backup=str(source), database=str(target), apply=False, dry_run=True)
        )
        == 0
    )
    assert "safety backup" in capsys.readouterr().out
