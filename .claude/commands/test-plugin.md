Run targeted tests for a single plugin. Takes a plugin name as $ARGUMENTS.

Steps:
1. Determine target plugin:
   - If $ARGUMENTS is provided, use it as the plugin name.
   - If empty, auto-detect from `git diff --name-only HEAD` — look for changed files in
     `src/reticulumpi/builtin_plugins/` and extract the plugin name from the filename.
   - If multiple or none found, ask which plugin to test.

2. Map plugin name to test file(s):
   - Default: `tests/test_$PLUGIN.py`
   - Special case `web_dashboard`: run all 4 dashboard test files:
     `tests/test_websocket_handler.py tests/test_api_write_endpoints.py tests/test_broadcast_registry.py tests/test_web_dashboard.py`
   - Verify the test file exists. If not, suggest creating it with `/new-plugin`.

3. Run the tests:
   ```
   .venv/bin/pytest tests/test_$PLUGIN.py -v
   ```
   If $ARGUMENTS contains `--cov`, add: `--cov=src/reticulumpi/builtin_plugins/$PLUGIN --cov-report=term-missing`

4. If tests fail, re-run with better diagnostics:
   ```
   .venv/bin/pytest tests/test_$PLUGIN.py -v -n0 --tb=long
   ```

5. Report: test count, pass/fail, and if using coverage, uncovered lines.
