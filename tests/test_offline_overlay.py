"""Regression tests for the narrow forced-offline runtime overlay."""

from __future__ import annotations

from pathlib import Path

import yaml

from reticulumpi.config import AppConfig


REPOSITORY = Path(__file__).resolve().parents[1]
OVERLAY = REPOSITORY / "config/reticulumpi/offline_profile.yaml"
HELPER = REPOSITORY / "scripts/simulate_offline.sh"


def test_offline_overlay_contains_only_allowlisted_key():
    value = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))
    assert value == {"internet": {"force_offline": True}}


def test_offline_overlay_preserves_system_configuration(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        """reticulumpi:
  node_name: Field-Node
  internet:
    force_offline: false
    probe_interval: 91
  plugins:
    info_bot:
      enabled: true
""",
        encoding="utf-8",
    )
    original = config.read_bytes()
    loaded = AppConfig(str(config), str(OVERLAY))

    assert loaded.offgrid_mode is True
    assert loaded.node_name == "Field-Node"
    assert loaded.internet["probe_interval"] == 91
    assert loaded.plugins["info_bot"]["enabled"] is True
    assert config.read_bytes() == original


def test_offline_helper_never_replaces_system_configuration():
    script = HELPER.read_text(encoding="utf-8")
    assert 'STATE_DIR="/var/lib/reticulumpi"' in script
    assert 'RUNTIME_OVERLAY="$STATE_DIR/runtime-overrides.yaml"' in script
    assert "mv -Tf" in script
    assert "sync -f" in script
    for forbidden in (
        "config.yaml.pre-offline",
        "CONFIG_BACKUP",
        "CONFIG_FILE=",
        'cp "$OFFLINE_PROFILE"',
    ):
        assert forbidden not in script


def test_broker_path_has_no_legacy_runtime_sudoers_rules():
    sudoers = REPOSITORY / "config/sudoers.d"
    for name in (
        "reticulumpi-services",
        "reticulumpi-chrony",
        "reticulumpi-offline",
    ):
        assert not (sudoers / name).exists()
