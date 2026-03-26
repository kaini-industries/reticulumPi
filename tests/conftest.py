"""Shared test fixtures for reticulumPi tests."""

import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def tmp_config(tmp_path):
    """Create a temporary config YAML file."""
    config_content = """\
reticulumpi:
  reticulum_config_dir: null
  use_shared_instance: false
  identity_path: {identity_path}
  log_level: 4
  plugin_paths: []
  plugins:
    heartbeat_announce:
      enabled: true
      interval_seconds: 5
      app_name: test
      aspects:
        - node
        - heartbeat
      include_telemetry: false
    system_monitor:
      enabled: true
      collect_interval_seconds: 5
      metrics:
        - cpu_percent
""".format(identity_path=str(tmp_path / "identity"))
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)
    return str(config_file)


@pytest.fixture
def mock_rns_identity():
    """Create a mock RNS.Identity."""
    identity = MagicMock()
    identity.hash = b"\x00" * 16
    return identity


@pytest.fixture
def mock_rns_reticulum():
    """Patch RNS.Reticulum to return a mock instance."""
    with patch("RNS.Reticulum") as mock_cls:
        instance = MagicMock()
        mock_cls.return_value = instance
        yield instance


@pytest.fixture
def mock_app(mock_rns_reticulum, mock_rns_identity):
    """Create a mock ReticulumPiApp suitable for passing to plugins."""
    app = MagicMock()
    app.reticulum = mock_rns_reticulum
    app.identity = mock_rns_identity
    app.plugins = {}
    return app


@pytest.fixture
def plugin_dir(tmp_path):
    """Create a temporary plugin directory with a sample plugin file."""
    plugin_file = tmp_path / "sample_plugin.py"
    plugin_file.write_text("""\
from reticulumpi.plugin_base import PluginBase

class SamplePlugin(PluginBase):
    plugin_name = "sample"
    plugin_version = "0.1.0"

    def start(self):
        self._active = True

    def stop(self):
        self._active = False
""")
    return str(tmp_path)
