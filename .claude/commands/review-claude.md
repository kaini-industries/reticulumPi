Audit all CLAUDE.md files for accuracy and staleness. Run these checks and report findings.

## Numeric Accuracy

1. **Plugin count:** Root CLAUDE.md references a plugin count.
   - Verify: `ls src/reticulumpi/builtin_plugins/*.py | grep -v __init__ | grep -v __pycache__ | wc -l`
   - Subtract non-plugin files: `signal_plugin_base.py`, `example_plugin.py`
   - Update the number if it has drifted.

2. **Test file count:** Root CLAUDE.md and tests/CLAUDE.md reference a test count.
   - Verify: `ls tests/test_*.py | wc -l`

3. **Event count:** Core CLAUDE.md says "~120 event types".
   - Verify: `grep -c "^[A-Z_]* = " src/reticulumpi/events.py`

## File/Function Existence

4. **Referenced paths:** For each file path mentioned in any CLAUDE.md, verify it exists.
   - `conftest.py` fixtures: `mock_app`, `mock_rns_reticulum`, `mock_rns_identity`, `tmp_config`, `plugin_dir`
   - `example_plugin.py`, `signal_plugin_base.py`, `plugin_base.py`
   - Test file referenced: `test_meshtastic_gateway.py` (for canonical mocking pattern)

5. **Referenced functions:** Grep for key functions/methods mentioned in CLAUDE.md:
   - `_stop_event`, `_active`, `_sleep_while_active`, `_start_thread`, `_join_threads`
   - `broadcast_snapshot`, `broadcast_tier`, `broadcast_keys`
   - `subscribe`, `subscribe_offloaded`

## Staleness Detection

6. **Signal plugin list** in builtin_plugins/CLAUDE.md: Verify the 10 listed signal plugins
   match actual SignalPluginBase subclasses:
   `grep -rl "SignalPluginBase" src/reticulumpi/builtin_plugins/*.py`

7. **Dashboard file list** in builtin_plugins/CLAUDE.md: Verify the listed files match:
   `ls src/reticulumpi/builtin_plugins/web_dashboard/*.py`

8. **Gotchas still apply:** Test each gotcha in root CLAUDE.md:
   - Version in two files: verify both `pyproject.toml:7` and `src/reticulumpi/__init__.py:3` exist
   - Reticulum double-bracket format: still documented correctly

## Missing Documentation

9. **New plugins without mention:** Compare plugin files to CLAUDE.md references:
   `ls src/reticulumpi/builtin_plugins/*.py | grep -v __init__`

10. **New conftest fixtures:** Compare actual fixtures to tests/CLAUDE.md:
    `grep "^def \|^async def " tests/conftest.py`

11. **Undocumented patterns:** Check for commonly-used patterns not in CLAUDE.md:
    - `plugin_dependencies` usage: `grep -rl "plugin_dependencies" src/reticulumpi/builtin_plugins/`
    - `validate_config` usage: `grep -rl "validate_config" src/reticulumpi/builtin_plugins/`

## Commands Still Work

12. **Test commands:** Verify `make test`, `make lint` still work (dry-run, just check Makefile targets exist):
    `grep -E "^test:|^lint:|^format:" Makefile`

## Output

Present a checklist: each check with PASS/FAIL and the specific correction needed for failures.
After review, update the marker in root CLAUDE.md: `<!-- Last reviewed: YYYY-MM-DD -->`
