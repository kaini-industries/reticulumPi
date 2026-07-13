"""Focused tests for the root-owned transactional administrator."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import closing, nullcontext
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

import reticulumpi.admin_cli as admin
from reticulumpi.migrations import Migration, MigrationTarget


@pytest.fixture
def admin_paths(tmp_path, monkeypatch):
    paths = SimpleNamespace(
        root=tmp_path / "opt/reticulumpi",
        config=tmp_path / "etc/reticulumpi",
        data=tmp_path / "var/lib/reticulumpi",
        cache=tmp_path / "var/cache/reticulumpi",
        backups=tmp_path / "var/backups/reticulumpi",
        run=tmp_path / "run/reticulumpi",
        systemd=tmp_path / "etc/systemd/system",
        libexec=tmp_path / "usr/libexec/reticulumpi",
        shared=tmp_path / "usr/share/reticulumpi/config",
        sudoers=tmp_path / "etc/sudoers.d",
        chrony_config=tmp_path / "etc/chrony/conf.d/reticulumpi-gps.conf",
        captive_dnsmasq=tmp_path / "etc/dnsmasq.d/reticulumpi-captive-portal.conf",
        home=tmp_path / "home/reticulumpi",
    )
    monkeypatch.setattr(admin, "CONFIG_DIR", paths.config)
    monkeypatch.setattr(admin, "CONFIG_FILE", paths.config / "config.yaml")
    monkeypatch.setattr(admin, "DATA_DIR", paths.data)
    monkeypatch.setattr(admin, "CACHE_DIR", paths.cache)
    monkeypatch.setattr(admin, "BACKUP_DIR", paths.backups)
    monkeypatch.setattr(admin, "RUN_DIR", paths.run)
    monkeypatch.setattr(admin, "SYSTEMD_DIR", paths.systemd)
    monkeypatch.setattr(admin, "LIBEXEC_DIR", paths.libexec)
    monkeypatch.setattr(admin, "SHARED_CONFIG_DIR", paths.shared)
    monkeypatch.setattr(admin, "SUDOERS_DIR", paths.sudoers)
    monkeypatch.setattr(admin, "CHRONY_CONFIG_FILE", paths.chrony_config)
    monkeypatch.setattr(admin, "CAPTIVE_DNSMASQ_CONFIG_FILE", paths.captive_dnsmasq)
    monkeypatch.setattr(admin, "MANIFEST_FILE", paths.config / "install.json")
    monkeypatch.setattr(admin, "ADMIN_STATE_DIR", paths.backups / "admin")
    monkeypatch.setattr(admin, "JOURNAL_FILE", paths.backups / "admin/transaction.json")
    monkeypatch.setattr(admin, "LOCK_FILE", tmp_path / "run/lock/maintenance.lock")
    monkeypatch.setattr(admin, "_preflight_platform", lambda: {})
    monkeypatch.setattr(admin, "_wait_service_inactive", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(admin, "_wait_dashboard_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(admin, "_validate_install_root_ancestry", lambda _path: None)
    monkeypatch.setattr(admin, "_validate_release_immutability", lambda _release: None)
    monkeypatch.setattr(admin, "_verify_minisign", lambda *_args: None)
    monkeypatch.setattr(admin.os, "fchown", lambda *_args: None)
    monkeypatch.setattr(
        admin.pwd,
        "getpwnam",
        lambda _name: SimpleNamespace(
            pw_dir=str(paths.home),
            pw_uid=os.getuid(),
            pw_gid=os.getgid(),
        ),
    )
    return paths


def _source_bundle(path: Path, version: str = "0.3.0") -> Path:
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "reticulumpi"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    for directory in ("src", "systemd", "config/reticulumpi", "scripts"):
        (path / directory).mkdir(parents=True, exist_ok=True)
    (path / "config/reticulumpi/config.example.yaml").write_text(
        "reticulumpi: {}\n", encoding="utf-8"
    )
    for name in (
        "reticulumpi.service",
        "reticulumpi-control.socket",
        "reticulumpi-control@.service",
    ):
        command = (
            "ExecStart=/opt/reticulumpi/current/.venv/bin/python -I -m reticulumpi.control_broker\n"
            if name == "reticulumpi-control@.service"
            else "ExecStart=/opt/reticulumpi/current/.venv/bin/reticulumpi\n"
        )
        (path / "systemd" / name).write_text(command, encoding="utf-8")
    helper = path / "scripts/restart_services.sh"
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    helper.chmod(0o755)
    constraints = path / "constraints"
    constraints.mkdir()
    manifest_lines = []
    for name in admin._DEPENDENCY_PROFILES.values():
        profile = constraints / name
        profile.write_text(
            f"fixture==1.0 --hash=sha256:{'a' * 64}\n",
            encoding="utf-8",
        )
        manifest_lines.append(f"{admin._sha256(profile)}  constraints/{name}")
    (path / admin.BUNDLE_MANIFEST_NAME).write_text(
        "\n".join(manifest_lines) + "\n", encoding="utf-8"
    )
    (path / admin.BUNDLE_SIGNATURE_NAME).write_text("fixture signature\n", encoding="utf-8")
    return path


def _release(root: Path, version: str) -> Path:
    release = root / "releases" / version
    executable = release / ".venv/bin/reticulumpi"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    (release / "RELEASE").write_text(version + "\n", encoding="utf-8")
    return release


def _write_manifest(paths, release: Path, previous: Path | None, features=()) -> dict:
    paths.config.mkdir(parents=True, exist_ok=True)
    value = {
        "schema": 1,
        "version": (release / "RELEASE").read_text(encoding="utf-8").strip(),
        "install_root": str(paths.root),
        "release": str(release),
        "previous_release": str(previous) if previous else None,
        "features": list(features),
        "installed_at": "2026-07-10T00:00:00Z",
        "bundle_sha256": "a" * 64,
    }
    admin.MANIFEST_FILE.write_text(json.dumps(value), encoding="utf-8")
    return value


def _migration_target(path: Path) -> MigrationTarget:
    return MigrationTarget(
        "test_store",
        path,
        (
            Migration(
                1,
                "create records",
                ("CREATE TABLE records(value TEXT NOT NULL)",),
            ),
        ),
    )


@pytest.mark.parametrize("unsafe", ["/", "/usr", "/var", "/tmp"])
def test_safe_install_root_rejects_dangerous_roots(unsafe):
    with pytest.raises(admin.AdminError, match="unsafe install root"):
        admin._safe_install_root(unsafe)


def test_safe_install_root_rejects_final_and_intermediate_symlinks(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    final_link = tmp_path / "install-link"
    final_link.symlink_to(real, target_is_directory=True)
    with pytest.raises(admin.AdminError, match="symlink"):
        admin._safe_install_root(str(final_link))

    intermediate = tmp_path / "parent-link"
    intermediate.symlink_to(real, target_is_directory=True)
    with pytest.raises(admin.AdminError, match="symlink"):
        admin._safe_install_root(str(intermediate / "reticulumpi"))


def test_source_version_cannot_escape_release_directory(tmp_path):
    source = _source_bundle(tmp_path / "source", "../../etc")
    with pytest.raises(admin.AdminError, match="invalid release version"):
        admin._source_metadata(source)


def test_install_dry_run_is_non_mutating(admin_paths, tmp_path, capsys, monkeypatch):
    source = _source_bundle(tmp_path / "source")
    monkeypatch.setattr(admin, "_verify_signed_bundle", lambda *_args: None)
    args = SimpleNamespace(
        install_root=str(admin_paths.root),
        bundle=str(source),
        feature=["dashboard"],
        apply=False,
        dry_run=True,
        start=False,
    )
    assert admin._apply_release(args, "install") == 0
    output = capsys.readouterr().out
    assert '"operation": "install"' in output
    assert '"dashboard"' in output
    assert "Dry run only" in output
    assert not admin_paths.root.exists()
    assert not admin.MANIFEST_FILE.exists()
    assert not admin.JOURNAL_FILE.exists()


def test_source_bundle_must_be_separate_from_install_root(admin_paths):
    source = _source_bundle(admin_paths.root / "checkout")
    args = SimpleNamespace(
        install_root=str(admin_paths.root),
        bundle=str(source),
        feature=[],
        apply=False,
        dry_run=True,
        start=False,
    )
    with pytest.raises(admin.AdminError, match="must be separate"):
        admin._apply_release(args, "install")


def test_unit_renderer_maps_default_current_prefix_exactly_once(admin_paths, tmp_path):
    source = _source_bundle(tmp_path / "source")
    custom_root = tmp_path / "srv/reticulumpi"

    admin._render_units(source, custom_root, ())

    rendered = (admin_paths.systemd / "reticulumpi.service").read_text(encoding="utf-8")
    assert f"ExecStart={custom_root}/current/.venv/bin/reticulumpi" in rendered
    assert "current/current" not in rendered
    assert admin.DEFAULT_CURRENT_PREFIX not in rendered
    broker = (admin_paths.systemd / "reticulumpi-control@.service").read_text(encoding="utf-8")
    assert (
        f"ExecStart={custom_root}/current/.venv/bin/python -I -m reticulumpi.control_broker"
    ) in broker


def test_unit_renderer_rejects_broker_code_outside_immutable_release(admin_paths, tmp_path):
    source = _source_bundle(tmp_path / "source")
    (source / "systemd/reticulumpi-control@.service").write_text(
        "[Service]\nExecStart=/bin/sh /var/lib/reticulumpi/attacker.sh\n",
        encoding="utf-8",
    )
    with pytest.raises(admin.AdminError, match="control broker unit"):
        admin._render_units(source, admin_paths.root, ())


def test_upgrade_preserves_manifest_features_when_unspecified(
    admin_paths, tmp_path, capsys, monkeypatch
):
    current = _release(admin_paths.root, "0.2.5")
    (admin_paths.root / "current").symlink_to(current)
    _write_manifest(
        admin_paths,
        current,
        None,
        features=("dashboard", "chrony-control"),
    )
    source = _source_bundle(tmp_path / "source", "0.3.0")
    monkeypatch.setattr(admin, "_verify_signed_bundle", lambda *_args: None)
    args = SimpleNamespace(
        install_root=str(admin_paths.root),
        bundle=str(source),
        feature=[],
        apply=False,
        dry_run=True,
        start=False,
    )
    assert admin._apply_release(args, "upgrade") == 0
    output = capsys.readouterr().out
    assert '"chrony-control"' in output
    assert '"dashboard"' in output


def test_manifest_rejects_release_outside_install_root(admin_paths, tmp_path):
    outside = _release(tmp_path / "outside", "0.2.5")
    _write_manifest(admin_paths, outside, None)
    with pytest.raises(admin.AdminError, match="outside"):
        admin._load_manifest()


def test_manifest_rejects_non_string_previous_release(admin_paths):
    current = _release(admin_paths.root, "0.3.0")
    value = _write_manifest(admin_paths, current, None)
    value["previous_release"] = 123
    admin.MANIFEST_FILE.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(admin.AdminError, match="path or null"):
        admin._load_manifest()


def test_rollback_rejects_unmanaged_target(admin_paths, tmp_path):
    current = _release(admin_paths.root, "0.3.0")
    previous = _release(admin_paths.root, "0.2.5")
    (admin_paths.root / "current").symlink_to(current)
    _write_manifest(admin_paths, current, previous)
    outside = _release(tmp_path / "outside", "0.1.0")
    args = SimpleNamespace(to=str(outside), apply=False, dry_run=True)
    with pytest.raises(admin.AdminError, match="outside"):
        admin._rollback(args)


def test_failed_rollback_restores_pointer_and_manifest(admin_paths, monkeypatch):
    current = _release(admin_paths.root, "0.3.0")
    previous = _release(admin_paths.root, "0.2.5")
    (admin_paths.root / "current").symlink_to(current)
    original = _write_manifest(admin_paths, current, previous)
    args = SimpleNamespace(to=None, apply=True, dry_run=False)

    monkeypatch.setattr(admin, "_require_root", lambda: None)
    monkeypatch.setattr(admin, "_maintenance_lock", lambda: nullcontext())
    monkeypatch.setattr(admin, "_service_active", lambda _name: True)
    monkeypatch.setattr(admin, "_run", lambda *_args, **_kwargs: "")
    activation_count = 0

    def fail_target_activation(_action):
        nonlocal activation_count
        activation_count += 1
        if activation_count == 1:
            raise admin.AdminError("not ready")

    monkeypatch.setattr(admin, "_activate_application", fail_target_activation)

    with pytest.raises(admin.AdminError, match="not ready"):
        admin._rollback(args)
    assert (admin_paths.root / "current").resolve() == current
    restored = json.loads(admin.MANIFEST_FILE.read_text(encoding="utf-8"))
    assert restored["release"] == original["release"]
    assert restored["previous_release"] == original["previous_release"]


def test_failed_upgrade_restores_release_state_and_managed_files(
    admin_paths, tmp_path, monkeypatch
):
    current = _release(admin_paths.root, "0.2.5")
    (admin_paths.root / "current").symlink_to(current)
    _write_manifest(admin_paths, current, None, features=("dashboard",))
    admin_paths.config.mkdir(parents=True, exist_ok=True)
    admin.CONFIG_FILE.write_text("old-config\n", encoding="utf-8")
    admin_paths.data.mkdir(parents=True)
    state = admin_paths.data / "state.txt"
    state.write_text("old-state", encoding="utf-8")
    admin_paths.systemd.mkdir(parents=True)
    old_unit = admin_paths.systemd / "reticulumpi.service"
    old_unit.write_text("old-unit\n", encoding="utf-8")
    source = _source_bundle(tmp_path / "source", "0.3.0")
    fake_wheel = tmp_path / "reticulumpi-0.3.0-py3-none-any.whl"
    fake_wheel.write_bytes(b"wheel")

    def prepare_paths(_source):
        for path in (
            admin.CONFIG_DIR,
            admin.DATA_DIR,
            admin.CACHE_DIR,
            admin.BACKUP_DIR,
            admin.RUN_DIR,
        ):
            path.mkdir(parents=True, exist_ok=True)

    activation_count = 0

    def fail_readiness(_action):
        nonlocal activation_count
        activation_count += 1
        if activation_count == 1:
            state.write_text("new-state", encoding="utf-8")
            raise admin.AdminError("injected readiness failure")

    monkeypatch.setattr(admin, "_require_root", lambda: None)
    monkeypatch.setattr(admin, "_verify_signed_bundle", lambda *_args: None)
    monkeypatch.setattr(admin, "_maintenance_lock", lambda: nullcontext())
    monkeypatch.setattr(admin, "_service_active", lambda name: name == "reticulumpi.service")
    monkeypatch.setattr(admin, "_unit_enabled", lambda _name: False)
    monkeypatch.setattr(admin, "_build_wheel", lambda *_args: fake_wheel)
    monkeypatch.setattr(admin, "_validate_wheel", lambda wheel, *_args: admin._sha256(wheel))
    monkeypatch.setattr(admin, "_prepare_paths", prepare_paths)
    monkeypatch.setattr(admin, "_run", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(admin, "_activate_application", fail_readiness)
    monkeypatch.setattr(admin.os, "chown", lambda *_args: None)

    args = SimpleNamespace(
        install_root=str(admin_paths.root),
        bundle=str(source),
        feature=[],
        apply=True,
        dry_run=False,
        start=True,
    )
    with pytest.raises(admin.AdminError, match="injected readiness failure"):
        admin._apply_release(args, "upgrade")

    assert (admin_paths.root / "current").resolve() == current
    assert not (admin_paths.root / "releases/0.3.0").exists()
    assert state.read_text(encoding="utf-8") == "old-state"
    assert old_unit.read_text(encoding="utf-8") == "old-unit\n"
    manifest = json.loads(admin.MANIFEST_FILE.read_text(encoding="utf-8"))
    assert manifest["release"] == str(current)
    journal = json.loads(admin.JOURNAL_FILE.read_text(encoding="utf-8"))
    assert journal["state"] == "rolled_back"


def _populate_compatibility_state(paths) -> dict[str, Path]:
    for directory in (
        paths.config,
        paths.data,
        paths.home / ".reticulum",
        paths.home / ".config/reticulumpi",
        paths.home / ".local/share/reticulumpi/plugin",
        paths.home / ".nomadnet",
        paths.home / ".nomadnet-tui",
    ):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o750)
    admin.CONFIG_FILE.write_text(
        "reticulumpi:\n  identity_path: ~/.config/reticulumpi/identity\n",
        encoding="utf-8",
    )
    admin.CONFIG_FILE.chmod(0o640)
    files = {
        "data": paths.data / "queue.txt",
        "reticulum": paths.home / ".reticulum/config",
        "identity": paths.home / ".config/reticulumpi/identity",
        "plugin_identity": paths.home / ".local/share/reticulumpi/plugin/identity",
        "nomadnet": paths.home / ".nomadnet/config",
        "nomadnet_tui": paths.home / ".nomadnet-tui/config",
    }
    for name, path in files.items():
        path.write_text(f"original-{name}\n", encoding="utf-8")
        path.chmod(0o600 if "identity" in name else 0o640)
    sessions = paths.home / ".config/reticulumpi/sessions.db"
    with closing(sqlite3.connect(sessions)) as connection, connection:
        connection.execute("CREATE TABLE sessions(token TEXT)")
        connection.execute("INSERT INTO sessions VALUES ('preserved')")
    sessions.chmod(0o600)
    files["sessions"] = sessions
    return files


def test_transaction_backup_restores_all_compatibility_state_and_metadata(admin_paths):
    files = _populate_compatibility_state(admin_paths)
    admin._atomic_json(admin.JOURNAL_FILE, {"state": "preparing"}, 0o600)
    journal_before = admin.JOURNAL_FILE.read_bytes()

    backup = admin._backup_state("0.3.0", ("nomadnet",))
    metadata = json.loads((backup / "backup.json").read_text(encoding="utf-8"))

    assert metadata["schema"] == 2
    assert {record["name"] for record in metadata["state_roots"]} == {
        "etc",
        "data",
        "legacy-home-reticulum",
        "legacy-home-config",
        "legacy-home-data",
        "legacy-home-nomadnet",
        "legacy-home-nomadnet-tui",
    }
    assert set(metadata["identity_hashes"]) == {
        "legacy-home-config:identity",
        "legacy-home-data:plugin/identity",
    }
    assert not list(backup.rglob("transaction.json"))
    assert stat.S_IMODE((backup / "state/legacy-home-config/identity").stat().st_mode) == 0o600

    for name, path in files.items():
        if name == "sessions":
            path.unlink()
        else:
            path.write_text(f"candidate-{name}\n", encoding="utf-8")
    candidate_only = admin_paths.home / ".config/reticulumpi/candidate-only"
    candidate_only.write_text("remove me", encoding="utf-8")

    admin._restore_state_backup(backup)

    assert admin.JOURNAL_FILE.read_bytes() == journal_before

    for name, path in files.items():
        if name == "sessions":
            with closing(sqlite3.connect(path)) as connection, connection:
                assert connection.execute("SELECT token FROM sessions").fetchone()[0] == "preserved"
        else:
            assert path.read_text(encoding="utf-8") == f"original-{name}\n"
    assert not candidate_only.exists()
    assert stat.S_IMODE(files["identity"].stat().st_mode) == 0o600


def test_transaction_backup_rejects_symlinks_in_service_home(admin_paths, tmp_path):
    files = _populate_compatibility_state(admin_paths)
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    files["identity"].unlink()
    files["identity"].symlink_to(outside)

    with pytest.raises(admin.AdminError, match="symlink"):
        admin._backup_state("0.3.0")
    assert not list(admin_paths.backups.glob("release-*"))


def test_restore_manifest_failure_leaves_live_state_unchanged(admin_paths):
    files = _populate_compatibility_state(admin_paths)
    backup = admin._backup_state("0.3.0", ("nomadnet",))
    files["data"].write_text("candidate-data\n", encoding="utf-8")
    (backup / "state/data/queue.txt").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(admin.AdminError, match="manifest mismatch"):
        admin._restore_state_backup(backup)
    assert files["data"].read_text(encoding="utf-8") == "candidate-data\n"


def test_restore_switch_failpoint_restores_every_candidate_root(admin_paths, monkeypatch):
    files = _populate_compatibility_state(admin_paths)
    backup = admin._backup_state("0.3.0", ("nomadnet",))
    files["data"].write_text("candidate-data\n", encoding="utf-8")
    files["identity"].write_text("candidate-identity\n", encoding="utf-8")
    real_replace = admin.os.replace

    def fail_data_install(source, destination):
        source_path = Path(source)
        if Path(destination) == admin.DATA_DIR and ".restore-" in source_path.name:
            raise OSError("injected state switch failure")
        return real_replace(source, destination)

    monkeypatch.setattr(admin.os, "replace", fail_data_install)
    with pytest.raises(OSError, match="injected state switch failure"):
        admin._restore_state_backup(backup)
    assert files["data"].read_text(encoding="utf-8") == "candidate-data\n"
    assert files["identity"].read_text(encoding="utf-8") == "candidate-identity\n"


def test_application_readiness_requires_fresh_owned_marker(admin_paths, monkeypatch):
    admin_paths.run.mkdir(parents=True)
    marker = admin_paths.run / "ready"
    marker.write_text("ready\n", encoding="ascii")
    marker.chmod(0o644)
    admin._clear_application_readiness()
    assert not marker.exists()
    with pytest.raises(admin.AdminError, match="fresh readiness marker"):
        admin._wait_application_ready(timeout=0, stable_for=0)

    marker.write_text("ready\n", encoding="ascii")
    marker.chmod(0o644)
    monkeypatch.setattr(admin, "_service_active", lambda _name: True)
    admin._wait_application_ready(timeout=1, stable_for=0)

    marker.unlink()
    marker.symlink_to(admin_paths.data / "fake-ready")
    with pytest.raises(admin.AdminError, match="unsafe"):
        admin._readiness_marker_valid()


def test_identity_mismatch_after_activation_rolls_back_home_state(
    admin_paths, tmp_path, monkeypatch
):
    current = _release(admin_paths.root, "0.2.5")
    (admin_paths.root / "current").symlink_to(current)
    _write_manifest(admin_paths, current, None, features=("dashboard",))
    files = _populate_compatibility_state(admin_paths)
    original_identity = files["identity"].read_bytes()
    source = _source_bundle(tmp_path / "source", "0.3.0")
    fake_wheel = tmp_path / "reticulumpi-0.3.0-py3-none-any.whl"
    fake_wheel.write_bytes(b"wheel")

    def prepare_paths(_source):
        for path in (
            admin.CONFIG_DIR,
            admin.DATA_DIR,
            admin.CACHE_DIR,
            admin.BACKUP_DIR,
            admin.RUN_DIR,
        ):
            path.mkdir(parents=True, exist_ok=True)

    activation_count = 0

    def mutate_candidate_identity(_action):
        nonlocal activation_count
        activation_count += 1
        if activation_count == 1:
            files["identity"].write_bytes(b"candidate-replaced-identity")

    monkeypatch.setattr(admin, "_require_root", lambda: None)
    monkeypatch.setattr(admin, "_verify_signed_bundle", lambda *_args: None)
    monkeypatch.setattr(admin, "_maintenance_lock", lambda: nullcontext())
    monkeypatch.setattr(admin, "_service_active", lambda name: name == "reticulumpi.service")
    monkeypatch.setattr(admin, "_unit_enabled", lambda _name: False)
    monkeypatch.setattr(admin, "_build_wheel", lambda *_args: fake_wheel)
    monkeypatch.setattr(admin, "_validate_wheel", lambda wheel, *_args: admin._sha256(wheel))
    monkeypatch.setattr(admin, "_prepare_paths", prepare_paths)
    monkeypatch.setattr(admin, "_run", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(admin, "_activate_application", mutate_candidate_identity)
    monkeypatch.setattr(admin.os, "chown", lambda *_args: None)

    args = SimpleNamespace(
        install_root=str(admin_paths.root),
        bundle=str(source),
        feature=[],
        apply=True,
        dry_run=False,
        start=True,
    )
    with pytest.raises(admin.AdminError, match="identity continuity"):
        admin._apply_release(args, "upgrade")

    assert (admin_paths.root / "current").resolve() == current
    assert files["identity"].read_bytes() == original_identity
    journal = json.loads(admin.JOURNAL_FILE.read_text(encoding="utf-8"))
    assert journal["state"] == "rolled_back"
    assert (
        journal["identity_hashes_before"]["legacy-home-config:identity"]
        != journal["identity_hashes_after"]["legacy-home-config:identity"]
    )


def test_db_backup_dry_run_is_non_mutating(admin_paths, capsys):
    admin_paths.data.mkdir(parents=True)
    database = admin_paths.data / "messages.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE messages(value TEXT)")
    args = SimpleNamespace(apply=False, dry_run=True)
    assert admin._db_backup(args) == 0
    assert "Dry run only" in capsys.readouterr().out
    assert not admin_paths.backups.exists()


def test_loads_only_enabled_builtin_migration_targets(admin_paths, tmp_path, monkeypatch):
    home = tmp_path / "service-home"
    admin_paths.config.mkdir(parents=True)
    admin.CONFIG_FILE.write_text(
        """reticulumpi:
  plugins:
    node_location_tracker:
      enabled: true
    network_map:
      enabled: false
      db_path: /should/not/load.db
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        admin.pwd,
        "getpwnam",
        lambda _name: SimpleNamespace(pw_dir=str(home), pw_uid=123, pw_gid=456),
    )

    targets = admin._load_enabled_migration_targets()
    assert [target.name for target in targets] == ["node_location_tracker"]
    assert targets[0].path == admin_paths.data / ".local/share/reticulumpi/node_positions.db"


def test_db_plan_does_not_create_missing_database_directory(
    admin_paths, tmp_path, monkeypatch, capsys
):
    target = _migration_target(tmp_path / "missing/records.db")
    monkeypatch.setattr(admin, "_load_enabled_migration_targets", lambda: (target,))

    assert admin._db_plan(SimpleNamespace()) == 0
    assert "pending=1" in capsys.readouterr().out
    assert not target.path.parent.exists()


def test_db_migrate_dry_run_uses_clone_and_is_non_mutating(
    admin_paths, tmp_path, monkeypatch, capsys
):
    target = _migration_target(tmp_path / "missing/records.db")
    monkeypatch.setattr(admin, "_load_enabled_migration_targets", lambda: (target,))

    args = SimpleNamespace(apply=False, dry_run=True)
    assert admin._db_migrate(args) == 0
    output = capsys.readouterr().out
    assert "dry_run=true pending=1" in output
    assert "Dry run only" in output
    assert not target.path.parent.exists()


def test_db_migrate_apply_requires_stopped_service(admin_paths, tmp_path, monkeypatch):
    target = _migration_target(tmp_path / "missing/records.db")
    monkeypatch.setattr(admin, "_load_enabled_migration_targets", lambda: (target,))
    monkeypatch.setattr(admin, "_require_root", lambda: None)
    monkeypatch.setattr(admin, "_maintenance_lock", lambda: nullcontext())
    monkeypatch.setattr(admin, "_service_active", lambda _name: True)

    with pytest.raises(admin.AdminError, match="stop reticulumpi.service"):
        admin._db_migrate(SimpleNamespace(apply=True, dry_run=False))
    assert not target.path.parent.exists()


def test_db_migrate_apply_creates_database_and_backups_root(admin_paths, monkeypatch, capsys):
    target = _migration_target(admin_paths.data / "nested/records.db")
    monkeypatch.setattr(admin, "_load_enabled_migration_targets", lambda: (target,))
    monkeypatch.setattr(admin, "_require_root", lambda: None)
    monkeypatch.setattr(admin, "_maintenance_lock", lambda: nullcontext())
    monkeypatch.setattr(admin, "_service_active", lambda _name: False)
    monkeypatch.setattr(
        admin.pwd,
        "getpwnam",
        lambda _name: SimpleNamespace(pw_uid=123, pw_gid=456),
    )
    monkeypatch.setattr(admin.os, "chown", lambda *_args: None)

    assert admin._db_migrate(SimpleNamespace(apply=True, dry_run=False)) == 0
    with closing(sqlite3.connect(target.path)) as connection, connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='records'").fetchone()
    assert stat.S_IMODE(target.path.stat().st_mode) == 0o600
    assert (admin_paths.backups / "databases/test_store").is_dir()
    assert "applied=1" in capsys.readouterr().out


def test_db_backup_apply_creates_verified_copy(admin_paths, monkeypatch, capsys):
    admin_paths.data.mkdir(parents=True)
    database = admin_paths.data / "messages.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE messages(value TEXT)")
        connection.execute("INSERT INTO messages VALUES ('kept')")
    monkeypatch.setattr(admin, "_require_root", lambda: None)
    args = SimpleNamespace(apply=True, dry_run=False)
    assert admin._db_backup(args) == 0
    backup_dir = Path(capsys.readouterr().out.strip())
    backup = backup_dir / "messages.db"
    with closing(sqlite3.connect(backup)) as connection, connection:
        assert connection.execute("SELECT value FROM messages").fetchone()[0] == "kept"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_db_symlink_is_rejected(admin_paths, tmp_path):
    admin_paths.data.mkdir(parents=True)
    outside = tmp_path / "outside.db"
    with closing(sqlite3.connect(outside)) as connection, connection:
        connection.execute("CREATE TABLE secret(value TEXT)")
    (admin_paths.data / "leak.db").symlink_to(outside)
    with pytest.raises(admin.AdminError, match="symlink"):
        admin._databases()


def test_db_restore_dry_run_validates_source(admin_paths, tmp_path):
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_text("not sqlite", encoding="utf-8")
    target = admin_paths.data / "messages.db"
    args = SimpleNamespace(backup=str(corrupt), database=str(target), apply=False, dry_run=True)
    with pytest.raises(admin.AdminError, match="SQLite"):
        admin._db_restore(args)
    assert not target.exists()


def test_db_restore_apply_keeps_safety_backup_and_ownership(admin_paths, tmp_path, monkeypatch):
    admin_paths.data.mkdir(parents=True)
    live = admin_paths.data / "messages.db"
    source = tmp_path / "backup.db"
    with closing(sqlite3.connect(live)) as connection, connection:
        connection.execute("CREATE TABLE old(value TEXT)")
        connection.execute("INSERT INTO old VALUES ('before')")
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("CREATE TABLE restored(value TEXT)")
        connection.execute("INSERT INTO restored VALUES ('after')")
    original_owner = (live.stat().st_uid, live.stat().st_gid)
    monkeypatch.setattr(admin, "_require_root", lambda: None)
    monkeypatch.setattr(admin, "_service_active", lambda _name: False)
    monkeypatch.setattr(admin.os, "chown", lambda path, uid, gid: None)
    args = SimpleNamespace(backup=str(source), database=str(live), apply=True, dry_run=False)
    assert admin._db_restore(args) == 0
    with closing(sqlite3.connect(live)) as connection, connection:
        assert connection.execute("SELECT value FROM restored").fetchone()[0] == "after"
    safety = list(admin_paths.backups.glob("db-safety-*/*"))
    assert len(safety) == 1
    with closing(sqlite3.connect(safety[0])) as connection, connection:
        assert connection.execute("SELECT value FROM old").fetchone()[0] == "before"
    assert original_owner == (live.stat().st_uid, live.stat().st_gid)
    assert stat.S_IMODE(live.stat().st_mode) == 0o600


def test_db_restore_target_must_be_under_data(admin_paths, tmp_path):
    source = tmp_path / "backup.db"
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("CREATE TABLE value(id INTEGER)")
    args = SimpleNamespace(
        backup=str(source),
        database=str(tmp_path / "outside.db"),
        apply=False,
        dry_run=True,
    )
    with pytest.raises(admin.AdminError, match="under"):
        admin._db_restore(args)


def test_wheel_validation_checks_version_and_dashboard_assets(tmp_path):
    wheel = tmp_path / "reticulumpi-0.3.0-py3-none-any.whl"
    with ZipFile(wheel, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "reticulumpi-0.3.0.dist-info/METADATA",
            "Name: reticulumpi\nVersion: 0.3.0\n",
        )
        archive.writestr("reticulumpi/builtin_plugins/web_dashboard/static/index.html", "index")
        archive.writestr("reticulumpi/builtin_plugins/web_dashboard/static/style.css", "css")
        archive.writestr("reticulumpi/builtin_plugins/web_dashboard/static/sw.js", "sw")
    assert len(admin._validate_wheel(wheel, "0.3.0", ("dashboard",))) == 64
    with pytest.raises(admin.AdminError, match="does not match"):
        admin._validate_wheel(wheel, "0.3.1", ("dashboard",))


def test_parser_exposes_admin_and_database_commands(capsys):
    parser = admin._build_parser()
    parsed = parser.parse_args(["db", "backup", "--dry-run"])
    assert parsed.db_command == "backup"
    assert parsed.apply is False
    migrate = parser.parse_args(["db", "migrate", "--apply"])
    assert migrate.apply is True
    with pytest.raises(SystemExit) as stopped:
        admin.main(["--help"])
    assert stopped.value.code == 0
    help_text = capsys.readouterr().out
    for command in ("install", "upgrade", "rollback", "status", "doctor", "db"):
        assert command in help_text


def test_compatibility_launchers_only_use_a_fixed_root_owned_administrator():
    repository = Path(__file__).resolve().parents[1]
    for relative in ("scripts/bootstrap.sh", "scripts/update.sh"):
        script = (repository / relative).read_text(encoding="utf-8")
        assert "/usr/sbin/reticulumpi-admin" in script
        assert "/usr/bin/reticulumpi-admin" in script
        assert "signed ReticulumPi recovery administrator package" in script
        assert "--dry-run" in script
        assert "--apply" in script
        for forbidden in (
            "PYTHONPATH",
            "python3 -m",
            "reticulumpi.admin_cli",
            "command -v reticulumpi-admin",
            "current/.venv/bin/reticulumpi-admin",
            "apt-get",
            "rsync",
            "chown -R",
            "pip install",
            "git pull",
        ):
            assert forbidden not in script
