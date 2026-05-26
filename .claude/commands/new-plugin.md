Scaffold a new ReticulumPi plugin. Takes a plugin name as $ARGUMENTS (snake_case).

Reference: `src/reticulumpi/builtin_plugins/example_plugin.py` for the full scaffold.

Steps:
1. Validate $ARGUMENTS is a valid snake_case plugin name. If empty, ask for one.
2. Check `src/reticulumpi/builtin_plugins/$ARGUMENTS.py` does not already exist.
3. Create `src/reticulumpi/builtin_plugins/$ARGUMENTS.py`:
   - Copy the structure from `example_plugin.py` (PluginBase subclass)
   - Replace class name, `plugin_name`, `plugin_version`, `plugin_description`
   - Include `start()`, `stop()`, and a background thread stub
   - If the plugin needs optional deps (user will specify), add the imports
4. Create `tests/test_$ARGUMENTS.py`:
   - Import `pytest` and `unittest.mock`
   - Use `mock_app` fixture from `conftest.py`
   - If the plugin imports optional deps, apply the `sys.modules` patching pattern
     from `tests/test_meshtastic_gateway.py` lines 16-55 (module-level MagicMock +
     `@pytest.fixture(autouse=True)` with `patch.dict`)
   - Include basic tests: construction, config validation, start/stop lifecycle
5. Add a config section to `config/reticulumpi/config.example.yaml` under `plugins:`:
   ```yaml
   $ARGUMENTS:
     enabled: false
   ```
6. Add an entry to `docs/plugins.md` in the appropriate section.
7. Show the diff of all created files. Run `.venv/bin/pytest tests/test_$ARGUMENTS.py -v` to verify.
