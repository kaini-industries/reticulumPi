"""Tests for the plugin loader."""

import logging
import os

from reticulumpi.plugin_base import PluginBase
from reticulumpi.plugin_loader import PluginLoader


def test_discover_finds_plugins(plugin_dir):
    loader = PluginLoader()
    found = loader.discover([plugin_dir])
    assert "sample" in found
    assert issubclass(found["sample"], PluginBase)


def test_discover_skips_underscored_files(tmp_path):
    (tmp_path / "_hidden.py").write_text("class Foo: pass")
    loader = PluginLoader()
    found = loader.discover([str(tmp_path)])
    assert len(found) == 0


def test_discover_skips_nonexistent_dirs():
    loader = PluginLoader()
    found = loader.discover(["/nonexistent/path"])
    assert len(found) == 0


def test_discover_handles_bad_module(tmp_path):
    (tmp_path / "broken.py").write_text("raise RuntimeError('broken')")
    loader = PluginLoader()
    found = loader.discover([str(tmp_path)])
    assert len(found) == 0


def test_discover_handles_hyphenated_dir(tmp_path):
    """Plugin dirs with hyphens/special chars in the name should load fine."""
    special_dir = tmp_path / "my-custom-plugins"
    special_dir.mkdir()
    (special_dir / "good_plugin.py").write_text(
        "from reticulumpi.plugin_base import PluginBase\n"
        "class GoodPlugin(PluginBase):\n"
        "    plugin_name = 'good'\n"
        "    plugin_version = '1.0.0'\n"
        "    def start(self): pass\n"
        "    def stop(self): pass\n"
    )
    loader = PluginLoader()
    found = loader.discover([str(special_dir)])
    assert "good" in found


def test_plugin_instantiation(plugin_dir, mock_app):
    loader = PluginLoader()
    found = loader.discover([plugin_dir])
    plugin = found["sample"](mock_app, {"enabled": True})
    assert plugin.plugin_name == "sample"
    plugin.start()
    assert plugin._active is True
    plugin.stop()
    assert plugin._active is False


def test_discover_warns_on_external_directory(tmp_path, caplog):
    (tmp_path / "ext_plugin.py").write_text(
        "from reticulumpi.plugin_base import PluginBase\n"
        "class ExtPlugin(PluginBase):\n"
        "    plugin_name = 'ext'\n"
        "    plugin_version = '1.0.0'\n"
        "    def start(self): pass\n"
        "    def stop(self): pass\n"
    )
    loader = PluginLoader()
    with caplog.at_level(logging.WARNING):
        found = loader.discover([str(tmp_path)])
    assert "ext" in found
    assert any("external directory" in r.message for r in caplog.records)


# --- gap-007: caching behavior tests ---


def test_discover_returns_cached_on_second_call(plugin_dir):
    """Second discover() call with unchanged dirs returns cached results."""
    loader = PluginLoader()
    first = loader.discover([plugin_dir])
    assert "sample" in first
    # Second call -- cache should be used (same object contents)
    second = loader.discover([plugin_dir])
    assert second == first
    # Verify _cache was populated
    assert loader._cache is not None


def test_discover_rescans_after_mtime_change(tmp_path):
    """Modifying the plugin directory mtime invalidates the cache."""
    plugin_file = tmp_path / "alpha_plugin.py"
    plugin_file.write_text(
        "from reticulumpi.plugin_base import PluginBase\n"
        "class AlphaPlugin(PluginBase):\n"
        "    plugin_name = 'alpha'\n"
        "    plugin_version = '1.0.0'\n"
        "    def start(self): pass\n"
        "    def stop(self): pass\n"
    )
    loader = PluginLoader()
    first = loader.discover([str(tmp_path)])
    assert "alpha" in first
    assert len(first) == 1

    # Add a second plugin (which changes the directory mtime)
    (tmp_path / "beta_plugin.py").write_text(
        "from reticulumpi.plugin_base import PluginBase\n"
        "class BetaPlugin(PluginBase):\n"
        "    plugin_name = 'beta'\n"
        "    plugin_version = '1.0.0'\n"
        "    def start(self): pass\n"
        "    def stop(self): pass\n"
    )
    # Force mtime bump in case filesystem granularity is too coarse
    import time

    new_mtime = time.time() + 10
    os.utime(str(tmp_path), (new_mtime, new_mtime))

    second = loader.discover([str(tmp_path)])
    assert "alpha" in second
    assert "beta" in second
    assert len(second) == 2


def test_discover_warns_on_duplicate_plugin_name(tmp_path, caplog):
    """Two plugins with the same plugin_name should log a warning."""
    (tmp_path / "first_plugin.py").write_text(
        "from reticulumpi.plugin_base import PluginBase\n"
        "class FirstPlugin(PluginBase):\n"
        "    plugin_name = 'dupe'\n"
        "    plugin_version = '1.0.0'\n"
        "    def start(self): pass\n"
        "    def stop(self): pass\n"
    )
    (tmp_path / "second_plugin.py").write_text(
        "from reticulumpi.plugin_base import PluginBase\n"
        "class SecondPlugin(PluginBase):\n"
        "    plugin_name = 'dupe'\n"
        "    plugin_version = '2.0.0'\n"
        "    def start(self): pass\n"
        "    def stop(self): pass\n"
    )
    loader = PluginLoader()
    with caplog.at_level(logging.WARNING):
        found = loader.discover([str(tmp_path)])
    # The duplicate name should still be in the result (second overrides first)
    assert "dupe" in found
    assert any("Duplicate plugin name" in r.message for r in caplog.records)
