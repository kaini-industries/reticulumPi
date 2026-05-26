Structured log analysis for ReticulumPi. Takes optional args via $ARGUMENTS:
`--plugin <name>`, `--since <minutes>` (default 30), `--priority <err|warning|info>`.

Steps:
1. Parse $ARGUMENTS for flags. Defaults: all plugins, last 30 minutes, all priorities.

2. **Fetch logs:**
   ```
   sudo journalctl -u reticulumpi --since "$MINUTES min ago" --no-pager -o short-iso
   ```
   If `--priority` given, add `-p $PRIORITY`.
   If `--plugin` given, pipe through `grep -i "$PLUGIN"`.

3. **Crash analysis:**
   - Search for `plugin.crashed`, `Traceback`, `Exception`, `CRITICAL` in the output.
   - For each crash, extract: plugin name, error message, timestamp.
   - Check if the plugin auto-restarted (look for `plugin.started` after the crash).

4. **Pattern detection:**
   - Group errors by plugin name and message pattern (first line of each error).
   - Count occurrences of each pattern.
   - Flag patterns that repeat >3 times as recurring issues.

5. **Connectivity log** (if relevant):
   - Check `~/.local/share/reticulumpi/connectivity.log` for transport/routing issues.
   - Cross-reference timestamps with journalctl findings.

6. **Event correlation:**
   - Look for event sequences: `INTERNET_OFFLINE` followed by hub reconnection failures,
     `RNSD_RESTARTING` followed by plugin restarts, etc.
   - Flag any `PATH_TABLE_EMPTY` or `SINGLE_INTERFACE_SPOF` events.

7. Report:
   - Total log lines examined
   - Error/warning count by plugin
   - Recurring patterns with counts
   - Crashes with restart status
   - Correlated event sequences
   - Suggested actions for the top issues
