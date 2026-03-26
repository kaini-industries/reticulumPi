"""Tests for the plugin loader."""

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
