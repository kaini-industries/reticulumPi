"""Focused fail-closed coverage for administration metadata and path helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import reticulumpi.admin_cli as admin


@pytest.fixture
def admin_paths(tmp_path, monkeypatch):
    paths = SimpleNamespace(
        root=tmp_path / "srv/reticulumpi",
        config=tmp_path / "etc/reticulumpi",
        data=tmp_path / "var/lib/reticulumpi",
        backups=tmp_path / "var/backups/reticulumpi",
        systemd=tmp_path / "etc/systemd/system",
        libexec=tmp_path / "usr/libexec/reticulumpi",
        sudoers=tmp_path / "etc/sudoers.d",
        run=tmp_path / "run",
    )
    monkeypatch.setattr(admin, "CONFIG_DIR", paths.config)
    monkeypatch.setattr(admin, "CONFIG_FILE", paths.config / "config.yaml")
    monkeypatch.setattr(admin, "DATA_DIR", paths.data)
    monkeypatch.setattr(admin, "BACKUP_DIR", paths.backups)
    monkeypatch.setattr(admin, "SYSTEMD_DIR", paths.systemd)
    monkeypatch.setattr(admin, "LIBEXEC_DIR", paths.libexec)
    monkeypatch.setattr(admin, "SUDOERS_DIR", paths.sudoers)
    monkeypatch.setattr(admin, "MANIFEST_FILE", paths.config / "install.json")
    monkeypatch.setattr(admin, "JOURNAL_FILE", paths.backups / "admin/transaction.json")
    monkeypatch.setattr(admin, "LOCK_FILE", paths.run / "maintenance.lock")
    monkeypatch.setattr(admin, "_validate_install_root_ancestry", lambda _path: None)
    return paths


def _platform_profile(
    *,
    system: str = "Linux",
    machine: str = "aarch64",
    version_info: tuple[int, ...] = (3, 12, 3),
    distribution: str = "ubuntu",
    codename: str = "noble",
    version_id: str = "24.04",
) -> dict[str, object]:
    return admin.select_platform_profile(
        system=system,
        machine=machine,
        version_info=version_info,
        os_release={
            "ID": distribution,
            "VERSION_CODENAME": codename,
            "VERSION_ID": version_id,
        },
    ).as_metadata()


def _manifest(paths) -> dict[str, object]:
    return {
        "schema": 1,
        "version": "0.3.2",
        "install_root": str(paths.root),
        "release": str(paths.root / "releases/0.3.2"),
        "previous_release": None,
        "features": ["dashboard"],
        "installed_at": "2026-07-12T00:00:00Z",
    }


def _service_evidence() -> dict[str, dict[str, bool]]:
    return {name: {"active": False, "enabled": False} for name in admin._TRANSACTION_SERVICE_NAMES}


def test_platform_metadata_rejects_malformed_and_unsupported_lanes():
    with pytest.raises(admin.AdminError, match="object or null"):
        admin._validate_platform_metadata([])

    canonical = _platform_profile()
    malformed = dict(canonical)
    malformed.pop("python")
    with pytest.raises(admin.AdminError, match="missing or invalid"):
        admin._validate_platform_metadata(malformed)
    malformed = {**canonical, "python": ""}
    with pytest.raises(admin.AdminError, match="missing or invalid"):
        admin._validate_platform_metadata(malformed)
    with pytest.raises(admin.AdminError, match="supported lane"):
        admin._validate_platform_metadata({**canonical, "system": "Darwin"})
    with pytest.raises(admin.AdminError, match="supported lane"):
        admin._validate_platform_metadata({**canonical, "profile_key": "future-lane"})
    with pytest.raises(admin.AdminError, match="supported lane"):
        admin._validate_platform_metadata({**canonical, "python": "3.13.0"})

    bookworm = _platform_profile(
        version_info=(3, 11, 9),
        distribution="debian",
        codename="bookworm",
        version_id="12",
    )
    assert admin._validate_platform_metadata(bookworm) == bookworm
    with pytest.raises(admin.AdminError, match="supported lane"):
        admin._validate_platform_metadata({**bookworm, "distribution": "ubuntu"})


def test_manifest_rejects_invalid_previous_release_and_bridge_evidence(admin_paths):
    value = _manifest(admin_paths)
    value["previous_release"] = 42
    with pytest.raises(admin.AdminError, match="path or null"):
        admin._validate_manifest(value)

    for bridge_backup, message in (
        (7, "path or null"),
        ("relative/release-0.3.2", "must be absolute"),
        (str(admin_paths.backups / "other/backup"), "managed backup root"),
    ):
        value = _manifest(admin_paths)
        value["legacy_bridge_backup"] = bridge_backup
        with pytest.raises(admin.AdminError, match=message):
            admin._validate_manifest(value)

    value = _manifest(admin_paths)
    value["legacy_bridge_backup"] = str(admin_paths.backups / "release-0.3.2")
    with pytest.raises(admin.AdminError, match="root evidence is missing"):
        admin._validate_manifest(value)

    value = _manifest(admin_paths)
    value["legacy_bridge_roots"] = [{"name": "etc", "path": str(admin_paths.config)}]
    with pytest.raises(admin.AdminError, match="evidence without a backup"):
        admin._validate_manifest(value)


def test_manifest_normalizes_complete_bridge_evidence(admin_paths):
    value = _manifest(admin_paths)
    backup = admin_paths.backups / "release-0.3.2"
    value.update(
        legacy_bridge_backup=str(backup),
        legacy_bridge_roots=[
            {"name": "etc", "path": str(admin_paths.config)},
            {"name": "data", "path": str(admin_paths.data)},
            {
                "name": "legacy-reticulum",
                "path": str(admin_paths.root / "legacy-home/.reticulum"),
            },
        ],
        legacy_bridge_services=_service_evidence(),
        platform_profile=_platform_profile(),
    )

    normalized = admin._validate_manifest(value, admin_paths.root.resolve())

    assert normalized["legacy_bridge_backup"] == str(backup.resolve())
    assert normalized["legacy_bridge_roots"][2]["name"] == "legacy-reticulum"
    assert normalized["platform_profile"] == value["platform_profile"]


@pytest.mark.parametrize(
    ("name", "raw_path", "message"),
    [
        (None, "/var/lib/reticulumpi", "path evidence is invalid"),
        ("legacy-reticulum", None, "path evidence is invalid"),
        ("legacy-reticulum", "relative/.reticulum", "not absolute"),
        ("etc", "/wrong/etc", "configuration root"),
        ("data", "/wrong/data", "data root"),
        ("legacy-unknown", "/old/.unknown", "invalid legacy state root"),
        ("legacy-reticulum", "/old/.nomadnet", "does not match its name"),
    ],
)
def test_backup_state_root_rejects_untrusted_evidence(admin_paths, name, raw_path, message):
    with pytest.raises(admin.AdminError, match=message):
        admin._validate_backup_state_root(name, raw_path)


def test_backup_state_root_accepts_named_suffix_and_rejects_symlink(admin_paths, tmp_path):
    legacy = tmp_path / "legacy/.config/reticulumpi"
    assert admin._validate_backup_state_root("old-config", str(legacy)) == admin.StateRoot(
        "old-config", legacy.resolve()
    )

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(admin.AdminError, match="symlink"):
        admin._validate_backup_state_root("old-reticulum", str(link / ".reticulum"))


def test_backup_state_root_rejects_even_matching_unsafe_canonical_root(monkeypatch):
    monkeypatch.setattr(admin, "CONFIG_DIR", Path("/usr"))
    monkeypatch.setattr(admin, "_reject_symlink_components", lambda _path: None)
    with pytest.raises(admin.AdminError, match="path is unsafe"):
        admin._validate_backup_state_root("etc", "/usr")


def test_backup_root_evidence_rejects_shape_duplicates_overlaps_and_omissions(admin_paths):
    with pytest.raises(admin.AdminError, match="must be a list"):
        admin._validate_backup_root_evidence({})
    with pytest.raises(admin.AdminError, match="evidence is invalid"):
        admin._validate_backup_root_evidence([{"name": "etc"}])
    with pytest.raises(admin.AdminError, match="omits canonical"):
        admin._validate_backup_root_evidence(
            [{"name": "legacy-reticulum", "path": "/old/.reticulum"}]
        )

    canonical = [
        {"name": "etc", "path": str(admin_paths.config)},
        {"name": "data", "path": str(admin_paths.data)},
    ]
    with pytest.raises(admin.AdminError, match="duplicates or overlaps"):
        admin._validate_backup_root_evidence([*canonical, canonical[0]])
    with pytest.raises(admin.AdminError, match="duplicates or overlaps"):
        admin._validate_backup_root_evidence(
            [
                *canonical,
                {"name": "old-data", "path": str(admin_paths.data / ".local/share/reticulumpi")},
            ]
        )


def test_backup_metadata_validators_cover_legacy_and_bound_paths(admin_paths, monkeypatch):
    with pytest.raises(admin.AdminError, match="records are missing"):
        admin._backup_roots_from_metadata({})
    with pytest.raises(admin.AdminError, match="feature metadata"):
        admin._backup_roots_from_metadata({"state_roots": [{}], "features": "dashboard"})

    discovered = (admin.StateRoot("etc", admin_paths.config),)
    monkeypatch.setattr(
        admin, "_state_roots", lambda features: discovered if features == () else ()
    )
    assert admin._backup_roots_from_metadata({"state_roots": [{}], "features": []}) == discovered

    records = [
        {"name": "etc", "path": str(admin_paths.config)},
        {"name": "data", "path": str(admin_paths.data)},
    ]
    assert [root.name for root in admin._backup_roots_from_metadata({"state_roots": records})] == [
        "etc",
        "data",
    ]

    for raw in ("dashboard", ["dashboard", 1]):
        with pytest.raises(admin.AdminError, match="feature metadata"):
            admin._backup_features({"features": raw})
    assert admin._backup_features({"features": ["dashboard", "dashboard"]}) == ("dashboard",)
    with pytest.raises(admin.AdminError, match="unknown features"):
        admin._backup_features({"features": ["future"]})


def test_backup_configuration_paths_are_typed_absolute_and_safe(admin_paths):
    assert admin._backup_configuration_file({}) is None
    with pytest.raises(admin.AdminError, match="evidence is invalid"):
        admin._backup_configuration_file({"configuration_file": 1})
    with pytest.raises(admin.AdminError, match="not absolute"):
        admin._backup_configuration_file({"configuration_file": "config.yaml"})
    with pytest.raises(admin.AdminError, match="path is unsafe"):
        admin._backup_configuration_file({"configuration_file": "/usr"})
    external = admin_paths.root / "legacy/config.yaml"
    assert admin._backup_configuration_file({"configuration_file": str(external)}) == external

    assert admin._backup_external_configuration_file({}) is None
    with pytest.raises(admin.AdminError, match="evidence is invalid"):
        admin._backup_external_configuration_file({"external_configuration_file": 1})
    with pytest.raises(admin.AdminError, match="path is unsafe"):
        admin._backup_external_configuration_file(
            {"external_configuration_file": str(admin.CONFIG_FILE)}
        )
    allowed = admin_paths.root / "external/config.yaml"
    assert (
        admin._backup_external_configuration_file({"external_configuration_file": str(allowed)})
        == allowed.resolve()
    )


def test_external_config_path_rejects_short_symlink_protected_and_admin_paths(
    admin_paths, tmp_path
):
    for raw in (Path("relative/config.yaml"), Path("/etc")):
        with pytest.raises(admin.AdminError, match="path is unsafe"):
            admin._validate_external_config_path(raw)

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(admin.AdminError, match="symlink"):
        admin._validate_external_config_path(link / "config.yaml")

    with pytest.raises(admin.AdminError, match="protected root"):
        admin._validate_external_config_path(admin_paths.backups / "nested/config.yaml")
    with pytest.raises(admin.AdminError, match="administration state"):
        admin._validate_external_config_path(admin.MANIFEST_FILE)

    allowed = admin_paths.root / "external/config.yaml"
    assert admin._validate_external_config_path(allowed) == allowed.resolve()


def test_legacy_config_discovery_rejects_unsafe_missing_and_ambiguous_sources(
    admin_paths, tmp_path, monkeypatch
):
    layout = SimpleNamespace(config_files=())
    monkeypatch.setattr(admin, "_discover_legacy_layout", lambda: layout)
    admin_paths.config.mkdir(parents=True)
    canonical_target = tmp_path / "canonical-target.yaml"
    canonical_target.write_text("reticulumpi: {}\n", encoding="utf-8")
    admin.CONFIG_FILE.symlink_to(canonical_target)
    with pytest.raises(admin.AdminError, match="symlink or special file"):
        admin._discover_legacy_config_source()
    admin.CONFIG_FILE.unlink()

    missing = tmp_path / "legacy/missing.yaml"
    layout.config_files = (missing,)
    with pytest.raises(admin.AdminError, match="configuration is missing"):
        admin._discover_legacy_config_source()

    unsafe_target = tmp_path / "legacy-target.yaml"
    unsafe_target.write_text("reticulumpi: {}\n", encoding="utf-8")
    missing.parent.mkdir()
    missing.symlink_to(unsafe_target)
    with pytest.raises(admin.AdminError, match="configuration is unsafe"):
        admin._discover_legacy_config_source()

    first = tmp_path / "first/config.yaml"
    second = tmp_path / "second/config.yaml"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    layout.config_files = (first, second)
    with pytest.raises(admin.AdminError, match="multiple legacy configurations"):
        admin._discover_legacy_config_source()


def test_legacy_config_discovery_handles_none_canonical_and_external_sources(
    admin_paths, tmp_path, monkeypatch
):
    layout = SimpleNamespace(config_files=())
    monkeypatch.setattr(admin, "_discover_legacy_layout", lambda: layout)
    assert admin._discover_legacy_config_source() is None

    admin_paths.config.mkdir(parents=True)
    admin.CONFIG_FILE.write_text("canonical\n", encoding="utf-8")
    layout.config_files = (admin.CONFIG_FILE,)
    assert admin._discover_legacy_config_source() is None

    external = tmp_path / "external/config.yaml"
    external.parent.mkdir()
    external.write_text("external\n", encoding="utf-8")
    layout.config_files = (external,)
    source = admin._discover_legacy_config_source()
    assert source == admin.LegacyConfigSource(external.resolve(), admin._sha256(external))


def test_legacy_meshchat_candidates_skip_managed_release_and_duplicate_roots(
    admin_paths, tmp_path, monkeypatch
):
    managed = admin_paths.data / "meshchat/storage"
    managed.mkdir(parents=True)
    old_root = tmp_path / "old-install"
    (old_root / "meshchat/storage").mkdir(parents=True)
    release = tmp_path / "releases/0.3.1"
    (release / "meshchat/storage").mkdir(parents=True)
    current = tmp_path / "current"
    current.symlink_to(old_root, target_is_directory=True)
    layout = SimpleNamespace(
        install_roots=(
            current,
            release,
            tmp_path / "missing",
            admin_paths.data,
            old_root,
            old_root,
        )
    )
    monkeypatch.setattr(admin, "_discover_legacy_layout", lambda: layout)

    assert admin._legacy_meshchat_storage_candidates() == (old_root / "meshchat/storage",)


def test_merge_state_roots_skips_nested_paths_and_rejects_reused_names(tmp_path):
    primary = (admin.StateRoot("data", tmp_path / "data"),)
    nested = admin.StateRoot("nested", tmp_path / "data/nested")
    added = admin.StateRoot("legacy", tmp_path / "legacy")
    assert admin._merge_state_roots(primary, (nested, added)) == (*primary, added)

    reused = admin.StateRoot("data", tmp_path / "other")
    with pytest.raises(admin.AdminError, match="name refers to multiple"):
        admin._merge_state_roots(primary, (reused,))


def test_legacy_bridge_feature_validation_reads_plugin_and_shared_rns_evidence(
    admin_paths, tmp_path
):
    admin._validate_legacy_bridge_features((), None)
    config = tmp_path / "legacy/config.yaml"
    config.parent.mkdir()
    config.write_text(
        "plugins:\n  web_dashboard:\n    enabled: true\nuse_shared_instance: true\n",
        encoding="utf-8",
    )
    with pytest.raises(admin.AdminError, match="dashboard, shared-rnsd"):
        admin._validate_legacy_bridge_features((), config)
    admin._validate_legacy_bridge_features(("dashboard", "shared-rnsd"), config)


def test_enabled_legacy_plugins_handles_absent_empty_and_ambiguous_yaml(tmp_path):
    with pytest.raises(admin.AdminError, match="cannot inspect"):
        admin._enabled_legacy_plugins(tmp_path / "missing.yaml")

    config = tmp_path / "config.yaml"
    config.write_text("reticulumpi: {}\nplugins: inline\n", encoding="utf-8")
    assert admin._enabled_legacy_plugins(config) == set()
    config.write_text("plugins:\n  # no configured plugins\n\nnext: value\n", encoding="utf-8")
    assert admin._enabled_legacy_plugins(config) == set()
    config.write_text("plugins:\n  one:\n    enabled: true\nplugins:\n", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="multiple plugins blocks"):
        admin._enabled_legacy_plugins(config)


def test_enabled_legacy_plugins_extracts_only_direct_explicit_true_values(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "plugins:\n"
        "  malformed: inline\n"
        "    enabled: true\n"
        "  nested_only:\n"
        "    settings:\n"
        "      enabled: true\n"
        "  active-plugin: # retained comment\n"
        "    settings:\n"
        "      value: one\n"
        "    enabled: TRUE # explicit\n"
        "  disabled:\n"
        "    enabled: false\n"
        "  no_children:\n"
        "next: value\n",
        encoding="utf-8",
    )

    assert admin._enabled_legacy_plugins(config) == {"active-plugin"}

    config.write_text(
        "plugins:\n  duplicate:\n    enabled: true\n    enabled: false\n",
        encoding="utf-8",
    )
    with pytest.raises(admin.AdminError, match="duplicate enabled"):
        admin._enabled_legacy_plugins(config)


def test_atomic_merge_accepts_identical_files_and_shared_directories(tmp_path, monkeypatch):
    source = tmp_path / "legacy"
    destination = tmp_path / "canonical"
    (source / "shared").mkdir(parents=True)
    (destination / "shared").mkdir(parents=True)
    (source / "shared/same").write_text("same", encoding="utf-8")
    (destination / "shared/same").write_text("same", encoding="utf-8")
    (source / "shared/new").write_text("new", encoding="utf-8")
    monkeypatch.setattr(admin.os, "chown", lambda *_args: None)

    admin._merge_tree_atomically(source, destination)

    assert (destination / "shared/same").read_text(encoding="utf-8") == "same"
    assert (destination / "shared/new").read_text(encoding="utf-8") == "new"


def test_atomic_merge_rejects_preexisting_displacement_slot(tmp_path, monkeypatch):
    source = tmp_path / "legacy"
    destination = tmp_path / "canonical"
    source.mkdir()
    destination.mkdir()
    (source / "new").write_text("new", encoding="utf-8")
    (destination / "old").write_text("old", encoding="utf-8")
    displaced = tmp_path / ".canonical.pre-legacy-123-456"
    displaced.mkdir()
    monkeypatch.setattr(admin.os, "chown", lambda *_args: None)
    monkeypatch.setattr(admin.os, "getpid", lambda: 123)
    monkeypatch.setattr(admin.time, "time_ns", lambda: 456)

    with pytest.raises(admin.AdminError, match="displacement path exists"):
        admin._merge_tree_atomically(source, destination)
    assert (destination / "old").read_text(encoding="utf-8") == "old"


def test_atomic_merge_rejects_unsafe_destination_and_verification_mismatch(tmp_path, monkeypatch):
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "value").write_text("value", encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir()
    destination = tmp_path / "canonical"
    destination.symlink_to(target, target_is_directory=True)
    with pytest.raises(admin.AdminError, match="destination is unsafe"):
        admin._merge_tree_atomically(source, destination)
    destination.unlink()
    destination.mkdir()
    monkeypatch.setattr(admin.os, "chown", lambda *_args: None)
    real_hash = admin._hash_regular_file

    def mismatched_candidate(path: Path) -> str:
        if path.name == "value" and ".canonical.legacy-merge-" in str(path):
            return "candidate-mismatch"
        return real_hash(path)

    monkeypatch.setattr(admin, "_hash_regular_file", mismatched_candidate)
    with pytest.raises(admin.AdminError, match="merge verification failed"):
        admin._merge_tree_atomically(source, destination)


def test_atomic_merge_restores_displaced_destination_when_install_replace_fails(
    tmp_path, monkeypatch
):
    source = tmp_path / "legacy"
    destination = tmp_path / "canonical"
    source.mkdir()
    destination.mkdir()
    (source / "new").write_text("new", encoding="utf-8")
    (destination / "old").write_text("old", encoding="utf-8")
    monkeypatch.setattr(admin.os, "chown", lambda *_args: None)
    real_replace = admin.os.replace

    def fail_candidate_install(source_path, destination_path):
        source_path = Path(source_path)
        if ".canonical.legacy-merge-" in source_path.name and Path(destination_path) == destination:
            raise OSError("injected candidate install failure")
        real_replace(source_path, destination_path)

    monkeypatch.setattr(admin.os, "replace", fail_candidate_install)
    with pytest.raises(OSError, match="candidate install failure"):
        admin._merge_tree_atomically(source, destination)
    assert (destination / "old").read_text(encoding="utf-8") == "old"


def test_atomic_merge_fresh_destination_restores_absence_after_fsync_failure(tmp_path, monkeypatch):
    source = tmp_path / "legacy"
    destination = tmp_path / "canonical"
    source.mkdir()
    (source / "new").write_text("new", encoding="utf-8")
    monkeypatch.setattr(admin.os, "chown", lambda *_args: None)
    real_fsync = admin._fsync_state_directory

    def fail_after_install(path: Path) -> None:
        if path == destination.parent and destination.exists():
            monkeypatch.setattr(admin, "_fsync_state_directory", real_fsync)
            raise admin.AdminError("injected final fsync failure")
        real_fsync(path)

    monkeypatch.setattr(admin, "_fsync_state_directory", fail_after_install)
    with pytest.raises(admin.AdminError, match="injected final fsync failure"):
        admin._merge_tree_atomically(source, destination)
    assert not destination.exists()


def test_legacy_config_path_pattern_matches_only_complete_prefix_boundaries():
    pattern = admin._legacy_config_path_pattern("/opt/reticulumpi")
    text = (
        "/opt/reticulumpi /opt/reticulumpi/config '/opt/reticulumpi' "
        "/opt/reticulumpi-old /opt/reticulumpi2"
    )
    assert pattern.sub("/srv/reticulumpi", text) == (
        "/srv/reticulumpi /srv/reticulumpi/config '/srv/reticulumpi' "
        "/opt/reticulumpi-old /opt/reticulumpi2"
    )


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ("# comment\n\n[Service]\nMemoryMax=128M\n", True),
        ("[Service]\nMemoryMax=\\\n128M\n", True),
        ("[Service]\nnot-an-assignment\n", False),
        ("MemoryMax=128M\n", False),
        ("[Unit]\nMemoryMax=128M\n", False),
        ("[Service]\nExecStart=/bin/false\n", False),
    ],
)
def test_legacy_dropin_safety_allows_only_resource_policy(tmp_path, contents, expected):
    fragment = tmp_path / "override.conf"
    fragment.write_text(contents, encoding="utf-8")
    assert admin._legacy_dropin_is_safe(fragment) is expected


def test_legacy_dropin_safety_rejects_unreadable_and_unterminated_directives(tmp_path):
    with pytest.raises(admin.AdminError, match="cannot inspect"):
        admin._legacy_dropin_is_safe(tmp_path / "missing.conf")
    fragment = tmp_path / "override.conf"
    fragment.write_text("[Service]\nMemoryMax=128M\\\n", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="unterminated directive"):
        admin._legacy_dropin_is_safe(fragment)


def test_legacy_allowlist_parser_handles_inline_and_block_lists():
    assert admin._legacy_allowlist_is_empty([], 0, 4, "[] # empty") is True
    assert admin._legacy_allowlist_is_empty([], 0, 4, "[abc]") is False
    with pytest.raises(admin.AdminError, match="expected a YAML list"):
        admin._legacy_allowlist_is_empty([], 0, 4, "abc")

    lines = [
        "    allowed_identities:\n",
        "      # retained comment\n",
        "\n",
        "      - abc\n",
        "    next: value\n",
    ]
    assert admin._legacy_allowlist_is_empty(lines, 0, 4, "") is False
    assert admin._legacy_allowlist_is_empty(lines[:3], 0, 4, "") is True
    with pytest.raises(admin.AdminError, match="expected list items"):
        admin._legacy_allowlist_is_empty(
            ["    allowed_identities:\n", "      identity: abc\n"], 0, 4, ""
        )


def test_release_validator_rejects_untrusted_shapes_and_accepts_complete_release(
    tmp_path, monkeypatch
):
    root = tmp_path / "srv/reticulumpi"
    releases = root / "releases"
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(admin.AdminError, match="outside the managed"):
        admin._validate_release(root, outside)

    release = releases / "0.3.2"
    release.mkdir(parents=True)
    link = releases / "linked"
    link.symlink_to(release, target_is_directory=True)
    with pytest.raises(admin.AdminError, match="may not be a symlink"):
        admin._validate_release(root, link)
    with pytest.raises(admin.AdminError, match="no trusted executable"):
        admin._validate_release(root, release)

    executable = release / ".venv/bin/reticulumpi"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    with pytest.raises(admin.AdminError, match="no RELEASE marker"):
        admin._validate_release(root, release)

    marker = release / "RELEASE"
    marker.write_text("not a version\n", encoding="utf-8")
    with pytest.raises(admin.AdminError, match="invalid release version"):
        admin._validate_release(root, release)

    marker.write_text("0.3.2\n", encoding="utf-8")
    monkeypatch.setattr(admin, "_validate_release_immutability", lambda _release: None)
    assert admin._validate_release(root, release) == release.resolve()


def test_service_state_evidence_requires_exact_boolean_records():
    with pytest.raises(admin.AdminError, match="invalid prior service-state"):
        admin._validate_service_state_snapshot([])
    with pytest.raises(admin.AdminError, match="invalid prior service-state"):
        admin._validate_service_state_snapshot({})

    evidence = _service_evidence()
    first = next(iter(evidence))
    evidence[first] = {"active": 1, "enabled": False}
    with pytest.raises(admin.AdminError, match="invalid prior service-state"):
        admin._validate_service_state_snapshot(evidence)

    evidence[first] = {"active": True, "enabled": False}
    assert admin._validate_service_state_snapshot(evidence) == evidence


def test_root_owned_regular_path_rejects_missing_symlink_and_directory(tmp_path):
    with pytest.raises(admin.AdminError, match="path is unavailable"):
        admin._validate_root_owned_regular_path(Path("/definitely/missing/file"), "fixture")

    # Use stable system paths so the validator reaches its type and ownership checks
    # without treating pytest's intentionally user-writable temporary root as trusted.
    with pytest.raises(admin.AdminError, match="unsafe type"):
        admin._validate_root_owned_regular_path(Path("/usr"), "fixture")
    if Path("/tmp").is_symlink():
        with pytest.raises(admin.AdminError, match="contains a symlink"):
            admin._validate_root_owned_regular_path(Path("/tmp/value"), "fixture")


def test_validate_version_accepts_release_charset_and_rejects_unsafe_text():
    assert admin._validate_version("0.3.2+prod.1") == "0.3.2+prod.1"
    for value in ("", "../escape", "spaces are unsafe", "x" * 65):
        with pytest.raises(admin.AdminError, match="invalid release version"):
            admin._validate_version(value)
