"""Regression tests for the canonical 0.3 service runtime layout."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from reticulumpi import _paths
from reticulumpi.config import AppConfig


ROOT = Path(__file__).resolve().parents[1]
STATE_ROOT = Path("/var/lib/reticulumpi")
CACHE_ROOT = Path("/var/cache/reticulumpi")


def test_systemd_units_share_canonical_home_xdg_and_write_roots():
    expected_environment = {
        "Environment=HOME=/var/lib/reticulumpi",
        "Environment=XDG_CONFIG_HOME=/var/lib/reticulumpi/.config",
        "Environment=XDG_DATA_HOME=/var/lib/reticulumpi/.local/share",
        "Environment=XDG_STATE_HOME=/var/lib/reticulumpi/.local/state",
        "Environment=XDG_CACHE_HOME=/var/cache/reticulumpi",
    }
    for name in ("reticulumpi.service", "rnsd.service"):
        unit = (ROOT / "systemd" / name).read_text(encoding="utf-8")
        assert expected_environment <= set(unit.splitlines())
        assert "WorkingDirectory=/var/lib/reticulumpi" in unit
        assert "StateDirectory=reticulumpi" in unit
        assert "StateDirectoryMode=0750" in unit
        assert "CacheDirectory=reticulumpi" in unit
        assert "CacheDirectoryMode=0750" in unit
        assert "UMask=0077" in unit
        assert "ProtectHome=true" in unit
        assert "ReadWritePaths=/var/lib/reticulumpi /var/cache/reticulumpi" in unit
        assert "/home/reticulumpi" not in unit


def test_production_example_uses_explicit_canonical_paths():
    example = ROOT / "config" / "reticulumpi" / "config.example.yaml"
    text = example.read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)["reticulumpi"]
    configured = AppConfig(str(example))

    assert parsed["reticulum_config_dir"] == str(STATE_ROOT / ".reticulum")
    assert parsed["identity_path"] == str(STATE_ROOT / ".config" / "reticulumpi" / "identity")
    assert parsed["plugins"]["message_echo"]["storage_path"] == str(
        STATE_ROOT / ".local" / "share" / "reticulumpi" / "lxmf"
    )
    assert configured.reticulum_config_dir == str(STATE_ROOT / ".reticulum")
    assert configured.identity_path == str(STATE_ROOT / ".config" / "reticulumpi" / "identity")
    assert "access_policy: deny" in text
    assert "config_dir: /var/lib/reticulumpi/.nomadnet" in text
    assert "secret_dir: /var/lib/reticulumpi/.config/reticulumpi" in text
    assert "cert_dir: /var/lib/reticulumpi/.config/reticulumpi/web_certs" in text
    assert "/var/lib/reticulumpi/.local/share/reticulumpi/messaging_hub.db" in text
    assert "/var/cache/reticulumpi/tile_cache" in text
    assert "/var/cache/reticulumpi/space_tracker" in text
    assert "/home/reticulumpi" not in text


def test_library_fallback_remains_safe_for_non_service_user(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = AppConfig()
    assert config.identity_path == str(tmp_path / ".config" / "reticulumpi" / "identity")
    assert config.reticulum_config_dir is None

    development = ROOT / "config" / "reticulumpi" / "config.development.example.yaml"
    parsed = yaml.safe_load(development.read_text(encoding="utf-8"))["reticulumpi"]
    assert parsed["identity_path"].startswith("~/")
    assert parsed["plugins"]["message_echo"]["storage_path"].startswith("~/")


def test_cache_helper_uses_production_and_container_roots(monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert _paths.runtime_cache_path("tile_cache") == str(CACHE_ROOT / "tile_cache")

    monkeypatch.setenv("XDG_CACHE_HOME", "/cache")
    assert _paths.runtime_cache_path("space_tracker") == "/cache/space_tracker"

    monkeypatch.setenv("RETICULUMPI_STATE_DIR", "/data")
    assert _paths.runtime_state_path(".reticulum", "config") == "/data/.reticulum/config"


def test_nomadnet_tui_launcher_uses_canonical_state_and_cache_roots():
    script = (ROOT / "scripts" / "nomadnet-tui.sh").read_text(encoding="utf-8")
    assert 'STATE_ROOT="${RETICULUMPI_STATE_DIR:-/var/lib/reticulumpi}"' in script
    assert 'RNS_CONFIG="${RETICULUMPI_RNS_CONFIG_DIR:-$STATE_ROOT/.reticulum}"' in script
    assert 'TUI_CONFIG="${RETICULUMPI_NOMADNET_TUI_DIR:-$STATE_ROOT/.nomadnet-tui}"' in script
    assert 'export XDG_CACHE_HOME="${XDG_CACHE_HOME:-/var/cache/reticulumpi}"' in script
    assert "/home/reticulumpi" not in script


def test_container_preserves_matching_xdg_state_on_data_volume():
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    config = yaml.safe_load((ROOT / "docker" / "config.example.yaml").read_text(encoding="utf-8"))[
        "reticulumpi"
    ]
    assert "RETICULUMPI_STATE_DIR=/data" in dockerfile
    assert "XDG_STATE_HOME=/data/.local/state" in dockerfile
    assert "XDG_CACHE_HOME=/cache" in dockerfile
    assert "/data/.local/state" in entrypoint
    assert config["reticulum_config_dir"] == "/data/.reticulum"
    assert config["identity_path"] == "/data/.config/reticulumpi/identity"


def test_container_waits_for_rnsd_socket_before_starting_readiness_client():
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    probe = entrypoint.index("rnsd_socket_ready()")
    readiness = entrypoint.index("rnsd_socket_ready && rnstatus")

    assert probe < readiness
    assert '"\\0rns/" + instance_name' in entrypoint
    assert "shared_instance_port" in entrypoint
    assert "rnsd.pid" in entrypoint
    assert 'rnsd_state" = "Z"' in entrypoint
    assert 'rm -f "$ready_file" "$rnsd_pid_file"' in entrypoint
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3" in dockerfile
    assert "python -m reticulumpi.container_healthcheck" in dockerfile


def test_container_test_context_excludes_local_secrets_and_tooling():
    ignored = set((ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())
    assert {
        ".claude",
        ".env",
        ".git",
        ".venv",
        "*.identity",
        "docker/config/",
        "node_modules",
        "playwright-results",
    } <= ignored


def test_container_test_stage_runs_as_nonroot_and_runtime_stays_wheel_only():
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    test_stage, runtime_stage = dockerfile.split(
        "FROM ${PYTHON_TRIXIE_IMAGE} AS runtime", maxsplit=1
    )

    assert "COPY --chown=reticulumpi-test:reticulumpi-test . ." in test_stage
    assert "USER reticulumpi-test" in test_stage
    assert "ARG PYTEST_WORKERS=2" in test_stage
    assert "ARG PYTEST_TIMEOUT=60" in test_stage
    assert (
        'pytest tests/ -q --tb=short -n "$PYTEST_WORKERS" --timeout="$PYTEST_TIMEOUT"' in test_stage
    )
    assert "COPY --from=wheel-artifact" in runtime_stage
    assert "COPY . ." not in runtime_stage
    assert "gcc" not in runtime_stage
    assert "sudo" not in runtime_stage


def test_normative_home_references_are_labeled_legacy_migration_inputs():
    excluded = {
        ROOT / "docs" / "audit-remediation-2026-07.md",
    }
    documents = [ROOT / "README.md", *(ROOT / "docs").glob("*.md")]
    references: list[Path] = []
    for document in documents:
        if document in excluded or document.parent.name == "release-verification":
            continue
        if "/home/reticulumpi" in document.read_text(encoding="utf-8"):
            references.append(document)
            assert re.search(
                r"legacy\s+migration\s+input",
                document.read_text(encoding="utf-8"),
                re.IGNORECASE,
            )

    # The advisory intentionally names the historical location. Normative
    # installation/upgrade guides should not retain it merely to satisfy this
    # test; any future historical reference must carry the same warning.
    assert {path.name for path in references} == {"security-advisory-0.2.5.md"}
