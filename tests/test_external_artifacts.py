"""Production pinning policy for MeshChat and native radio tools."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from reticulumpi import external_artifacts as artifacts
from reticulumpi import config as config_module
from reticulumpi.config import AppConfig, ConfigError


@pytest.fixture
def trusted_tmp(monkeypatch):
    monkeypatch.setattr(artifacts, "TRUSTED_OWNER_UID", os.getuid())
    monkeypatch.setattr(artifacts, "_validate_trusted_ancestry", lambda path: None)


def _manifest(path: Path, records: dict) -> Path:
    target = path / "external-artifacts.yaml"
    target.write_text(yaml.safe_dump({"schema": 1, "artifacts": records}), encoding="utf-8")
    target.chmod(0o600)
    return target


def _file_record(path: Path, *, version: str = "1.2.3") -> dict[str, str]:
    return {
        "kind": "file",
        "version": version,
        "path": str(path.resolve()),
        "sha256": artifacts.file_sha256(path),
    }


def test_production_example_lists_every_observed_external_artifact_category():
    repository = Path(__file__).resolve().parents[1]
    example = yaml.safe_load(
        (repository / "config/reticulumpi/external-artifacts.example.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert example["schema"] == 1
    assert set(example["artifacts"]) == {
        "meshchat",
        "rtl_test",
        "dump1090",
        "rtl_fm",
        "rtl_power",
    }
    assert example["artifacts"]["meshchat"]["path"].startswith("/srv/reticulumpi-external/")
    assert all(record["sha256"] == "0" * 64 for record in example["artifacts"].values())


def test_development_policy_preserves_path_compatibility(tmp_path):
    policy = artifacts.ExternalArtifactPolicy.from_config(None)
    assert policy.required is False
    assert (
        policy.verify_path(tmp_path / "not-installed", kind="file")
        == (tmp_path / "not-installed").absolute()
    )
    policy.preflight_enabled_plugins(
        {"fm_receiver": {"enabled": True}, "meshchat_server": {"enabled": True}}
    )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([], "must be a mapping"),
        ({"unknown": True}, "unsupported"),
        ({"mode": "sometimes"}, "development or required"),
        ({"mode": "required", "manifest_path": None}, "absolute manifest_path"),
        ({"mode": "required", "manifest_path": "relative.yaml"}, "absolute manifest_path"),
        ({"manifest_path": 42}, "path or null"),
    ],
)
def test_policy_config_rejects_unsafe_shapes(raw, message):
    with pytest.raises(artifacts.ArtifactPolicyError, match=message):
        artifacts.ExternalArtifactPolicy.from_config(raw)


def test_required_policy_verifies_exact_file_digest(trusted_tmp, tmp_path):
    executable = tmp_path / "rtl_fm"
    executable.write_bytes(b"reviewed binary")
    executable.chmod(0o755)
    manifest = _manifest(tmp_path, {"rtl_fm": _file_record(executable)})
    policy = artifacts.ExternalArtifactPolicy("required", manifest)

    assert policy.verify_path(executable, kind="file") == executable.resolve()
    executable.write_bytes(b"replaced binary")
    executable.chmod(0o755)
    with pytest.raises(artifacts.ArtifactPolicyError, match="reviewed SHA-256"):
        policy.verify_path(executable, kind="file")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data.update(schema=2), "schema 1"),
        (lambda data: data.update(artifacts=[]), "must be a mapping"),
        (lambda data: data["artifacts"].update({"bad name": {}}), "invalid external"),
        (
            lambda data: data["artifacts"].update(
                {
                    "tool": {
                        "kind": "file",
                        "version": "latest",
                        "path": "/bin/x",
                        "sha256": "0" * 64,
                    }
                }
            ),
            "immutable version",
        ),
        (
            lambda data: data["artifacts"].update(
                {"tool": {"kind": "blob", "version": "1", "path": "/bin/x", "sha256": "0" * 64}}
            ),
            "kind must",
        ),
        (
            lambda data: data["artifacts"].update(
                {"tool": {"kind": "file", "version": "1", "path": "bin/x", "sha256": "0" * 64}}
            ),
            "path must be absolute",
        ),
    ],
)
def test_manifest_validation_rejects_untrusted_metadata(
    trusted_tmp,
    tmp_path,
    mutation,
    message,
):
    data = {"schema": 1, "artifacts": {}}
    mutation(data)
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    manifest.chmod(0o600)
    with pytest.raises(artifacts.ArtifactPolicyError, match=message):
        artifacts.load_manifest(manifest)


def test_manifest_rejects_duplicate_yaml_keys(trusted_tmp, tmp_path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("schema: 1\nschema: 1\nartifacts: {}\n", encoding="utf-8")
    manifest.chmod(0o600)
    with pytest.raises(artifacts.ArtifactPolicyError, match="duplicate manifest key"):
        artifacts.load_manifest(manifest)


def test_tree_digest_covers_source_venv_modes_and_ignores_generated_cache(tmp_path):
    tree = tmp_path / "meshchat"
    (tree / ".venv" / "lib").mkdir(parents=True)
    source = tree / "meshchat.py"
    source.write_text("print('meshchat')\n", encoding="utf-8")
    dependency = tree / ".venv" / "lib" / "dependency.py"
    dependency.write_text("VERSION = 1\n", encoding="utf-8")
    first = artifacts.tree_sha256(tree)

    cache = tree / "__pycache__"
    cache.mkdir()
    (cache / "meshchat.pyc").write_bytes(b"generated")
    assert artifacts.tree_sha256(tree) == first

    dependency.write_text("VERSION = 2\n", encoding="utf-8")
    assert artifacts.tree_sha256(tree) != first


def test_required_tree_policy_blocks_mutated_meshchat(trusted_tmp, tmp_path):
    tree = tmp_path / "meshchat"
    tree.mkdir()
    source = tree / "meshchat.py"
    source.write_text("reviewed\n", encoding="utf-8")
    record = {
        "kind": "tree",
        "version": "v2.4.1+git.0123456789abcdef",
        "path": str(tree.resolve()),
        "sha256": artifacts.tree_sha256(tree),
    }
    policy = artifacts.ExternalArtifactPolicy("required", _manifest(tmp_path, {"meshchat": record}))
    assert policy.verify_path(tree, kind="tree") == tree.resolve()
    source.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(artifacts.ArtifactPolicyError, match="reviewed SHA-256"):
        policy.verify_path(tree, kind="tree")


def test_required_plugin_preflight_maps_all_radio_tools(monkeypatch, tmp_path):
    policy = artifacts.ExternalArtifactPolicy("required", tmp_path / "missing-manifest")
    seen: list[str] = []
    monkeypatch.setattr(
        artifacts.ExternalArtifactPolicy,
        "verify_executable",
        lambda self, command: seen.append(command) or Path("/bin/tool"),
    )
    monkeypatch.setattr(
        artifacts.ExternalArtifactPolicy,
        "verify_path",
        lambda self, path, kind: Path(path),
    )
    policy.preflight_enabled_plugins(
        {
            "radiosonde_tracker": {"enabled": True, "decoder_bin": "sonde-custom"},
            "weather_alert": {"enabled": True},
            "acars_decoder": {"enabled": True},
            "ais_receiver": {"enabled": True},
            "ism_decoder": {"enabled": True},
            "noaa_apt_decoder": {"enabled": True},
            "adsb_radar": {"enabled": True, "enable_bias_tee": True},
            "spectrum_scanner": {"enabled": True},
            "fm_receiver": {"enabled": True},
            "unrelated": {"enabled": True},
        }
    )
    assert {
        "rtl_test",
        "rtl_fm",
        "sonde-custom",
        "multimon-ng",
        "acarsdec",
        "AIS-catcher",
        "rtl_433",
        "sox",
        "noaa-apt",
        "dump1090",
        "rtl_biast",
        "rtl_power",
    } <= set(seen)


def test_production_meshchat_requires_absolute_install_dir(monkeypatch, tmp_path):
    policy = artifacts.ExternalArtifactPolicy("required", tmp_path / "manifest")
    with pytest.raises(artifacts.ArtifactPolicyError, match="absolute install_dir"):
        policy.preflight_enabled_plugins(
            {"meshchat_server": {"enabled": True, "install_dir": "./meshchat"}}
        )


def test_app_config_wraps_artifact_policy_failures(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "reticulumpi:\n"
        "  external_artifacts:\n"
        "    mode: required\n"
        "    manifest_path: relative.yaml\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="Invalid external artifact policy"):
        AppConfig(str(config))


def test_legacy_canonical_production_config_defaults_to_required(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("reticulumpi:\n  plugins: {}\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "PRODUCTION_CONFIG_PATH", str(config))
    loaded = AppConfig(str(config))
    assert loaded.external_artifact_policy.required is True
    assert loaded.external_artifact_policy.manifest_path == Path(
        "/etc/reticulumpi/external-artifacts.yaml"
    )


def test_canonical_production_config_cannot_opt_out(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "reticulumpi:\n  external_artifacts:\n    mode: development\n    manifest_path: null\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "PRODUCTION_CONFIG_PATH", str(config))
    with pytest.raises(ConfigError, match="may not disable required mode"):
        AppConfig(str(config))


def test_path_and_low_level_file_guards(tmp_path):
    inside = tmp_path / "inside"
    inside.mkdir()
    assert artifacts._is_relative_to(inside, tmp_path) is True
    assert artifacts._is_relative_to(tmp_path, inside) is False

    with pytest.raises(artifacts.ArtifactPolicyError, match="cannot open"):
        artifacts._open_regular_file(tmp_path / "missing")
    with pytest.raises(artifacts.ArtifactPolicyError, match="not a regular file"):
        artifacts._open_regular_file(inside)

    with pytest.raises(artifacts.ArtifactPolicyError, match="not root-owned"):
        artifacts._validate_trusted_stat(
            SimpleNamespace(st_uid=artifacts.TRUSTED_OWNER_UID + 1, st_mode=stat.S_IFREG | 0o644),
            inside,
        )
    with pytest.raises(artifacts.ArtifactPolicyError, match="group/world-writable"):
        artifacts._validate_trusted_stat(
            SimpleNamespace(st_uid=artifacts.TRUSTED_OWNER_UID, st_mode=stat.S_IFREG | 0o666),
            inside,
        )


def test_real_ancestry_rejects_untrusted_or_writable_path(tmp_path):
    with pytest.raises(artifacts.ArtifactPolicyError, match="not root-owned|group/world-writable"):
        artifacts._validate_trusted_ancestry(tmp_path)


def test_tree_guard_branches_and_symlinks(trusted_tmp, monkeypatch, tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(artifacts.ArtifactPolicyError, match="unavailable"):
        artifacts.tree_sha256(missing)
    plain = tmp_path / "plain"
    plain.write_text("x", encoding="utf-8")
    with pytest.raises(artifacts.ArtifactPolicyError, match="not a directory"):
        artifacts.tree_sha256(plain)
    tree_link = tmp_path / "tree-link"
    tree_link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(artifacts.ArtifactPolicyError, match="may not be a symlink"):
        artifacts.tree_sha256(tree_link)

    tree = tmp_path / "tree"
    target = tree / "target"
    target.mkdir(parents=True)
    (target / "value").write_text("value", encoding="utf-8")
    (tree / "internal-link").symlink_to(target, target_is_directory=True)
    digest = artifacts.tree_sha256(tree, require_trusted=True)
    assert len(digest) == 64

    outside = tmp_path / "outside"
    outside.mkdir()
    (tree / "external-link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(artifacts.ArtifactPolicyError, match="escapes deployment tree"):
        artifacts.tree_sha256(tree)
    (tree / "external-link").unlink()

    file_target = tree / "target-file"
    file_target.write_text("target", encoding="utf-8")
    (tree / "file-link").symlink_to(file_target)
    assert len(artifacts.tree_sha256(tree, require_trusted=True)) == 64

    fifo = tree / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(artifacts.ArtifactPolicyError, match="special file"):
        artifacts.tree_sha256(tree)
    fifo.unlink()

    monkeypatch.setattr(artifacts, "MAX_TREE_ENTRIES", 0)
    with pytest.raises(artifacts.ArtifactPolicyError, match="safety limits"):
        artifacts.tree_sha256(tree)


def test_manifest_reader_boundary_and_parse_errors(trusted_tmp, monkeypatch, tmp_path):
    with pytest.raises(artifacts.ArtifactPolicyError, match="must be absolute"):
        artifacts._read_manifest(Path("relative.yaml"))

    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("[]\n", encoding="utf-8")
    manifest.chmod(0o600)
    with pytest.raises(artifacts.ArtifactPolicyError, match="root must be a mapping"):
        artifacts.load_manifest(manifest)

    manifest.write_text("{invalid\n", encoding="utf-8")
    with pytest.raises(artifacts.ArtifactPolicyError, match="invalid external"):
        artifacts.load_manifest(manifest)

    manifest.write_bytes(b"x" * 20)
    monkeypatch.setattr(artifacts, "MAX_MANIFEST_BYTES", 10)
    with pytest.raises(artifacts.ArtifactPolicyError, match="exceeds"):
        artifacts.load_manifest(manifest)


def test_manifest_rejects_bad_fields_digest_and_duplicate_path(trusted_tmp, tmp_path):
    executable = tmp_path / "tool"
    executable.write_text("tool", encoding="utf-8")
    base = _file_record(executable)
    manifest = _manifest(tmp_path, {"tool": {"kind": "file"}})
    with pytest.raises(artifacts.ArtifactPolicyError, match="invalid fields"):
        artifacts.load_manifest(manifest)

    bad_digest = dict(base, sha256="BAD")
    manifest = _manifest(tmp_path, {"tool": bad_digest})
    with pytest.raises(artifacts.ArtifactPolicyError, match="invalid SHA-256"):
        artifacts.load_manifest(manifest)

    manifest = _manifest(tmp_path, {"tool": base, "tool-copy": dict(base)})
    with pytest.raises(artifacts.ArtifactPolicyError, match="duplicate external artifact path"):
        artifacts.load_manifest(manifest)


def test_required_policy_defensive_and_resolution_branches(
    trusted_tmp,
    monkeypatch,
    tmp_path,
):
    assert artifacts.ExternalArtifactPolicy("development")._records() == ()
    with pytest.raises(artifacts.ArtifactPolicyError, match="not configured"):
        artifacts.ExternalArtifactPolicy("required")._records()

    policy = artifacts.ExternalArtifactPolicy("required", tmp_path / "manifest")
    with pytest.raises(artifacts.ArtifactPolicyError, match="unavailable"):
        policy.verify_path(tmp_path / "missing", kind="file")

    existing = tmp_path / "existing"
    existing.write_text("x", encoding="utf-8")
    existing.chmod(0o755)
    policy = artifacts.ExternalArtifactPolicy("required", _manifest(tmp_path, {}))
    with pytest.raises(artifacts.ArtifactPolicyError, match="no unique"):
        policy.verify_path(existing, kind="file")

    record = artifacts.ArtifactRecord("blob", "blob", "1", existing, "0" * 64)
    monkeypatch.setattr(artifacts.ExternalArtifactPolicy, "_records", lambda self: (record,))
    with pytest.raises(artifacts.ArtifactPolicyError, match="unsupported"):
        policy.verify_path(existing, kind="blob")

    monkeypatch.setattr(artifacts.shutil, "which", lambda command: None)
    with pytest.raises(artifacts.ArtifactPolicyError, match="not found"):
        policy.verify_executable("missing")
    monkeypatch.setattr(artifacts.shutil, "which", lambda command: "/usr/bin/tool")
    assert artifacts.ExternalArtifactPolicy().verify_executable("tool") == Path("/usr/bin/tool")


def test_preflight_valid_meshchat_disabled_plugins_and_cli(monkeypatch, tmp_path, capsys):
    tree = tmp_path / "meshchat"
    tree.mkdir()
    policy = artifacts.ExternalArtifactPolicy("required", tmp_path / "manifest")
    verified: list[tuple[str, str]] = []
    monkeypatch.setattr(
        artifacts.ExternalArtifactPolicy,
        "verify_path",
        lambda self, path, kind: verified.append((str(path), kind)) or Path(path),
    )
    policy.preflight_enabled_plugins(
        {
            "disabled": {"enabled": False},
            "meshchat_server": {"enabled": True, "install_dir": str(tree)},
        }
    )
    assert verified == [(str(tree), "tree")]
    assert artifacts._radio_commands("adsb_radar", {"enable_bias_tee": False}) == (
        "rtl_test",
        "dump1090",
    )

    source = tmp_path / "source"
    source.write_text("value", encoding="utf-8")
    assert artifacts._main(["--kind", "file", str(source)]) == 0
    assert len(capsys.readouterr().out.strip()) == 64
    tree_dir = tmp_path / "tree-digest"
    tree_dir.mkdir()
    assert artifacts._main(["--kind", "tree", str(tree_dir)]) == 0
