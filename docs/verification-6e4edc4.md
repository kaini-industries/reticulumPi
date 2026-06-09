# Verification Report: Commit 6e4edc4

**Commit:** `6e4edc4` -- Fix node tracker map trails never auto-refreshing
**Date:** 2026-06-08
**Method:** 5-agent parallel workflow (Opus 4.6), 6 agents total including synthesis
**Result:** PASS WITH WARNINGS (0 critical, 9 warnings, 19 info)

## Verdicts by Dimension

| Dimension | Verdict | Summary |
|-----------|---------|---------|
| Correctness | warning | Core logic sound; timer guards work; three minor edge cases |
| Integration | pass | Data contract matches end-to-end across all 4 layers |
| Tests | warning | All 2511 tests pass; missing test for new WS callback |
| Resources | warning | No leaks; overlapping XHR possible in edge cases |
| Regression | pass | All existing behavior preserved; full test suite green |

## Actionable Findings

### 1. Overlapping XHR in `_fetchTrails()` (warning)
**File:** `src/reticulumpi/builtin_plugins/web_dashboard/static/map.js:525`

WS `trail_update` events null the cache and call `_fetchTrails()` without an in-flight guard, so a near-simultaneous 30s timer tick causes duplicate requests. Both XHRs complete and overwrite each other's trail layers, causing visual flicker.

**Fix:** Add a `_trailFetching` boolean flag set before XHR and cleared in onload/onerror.

### 2. Trail refresh timer not stopped on trail toggle-off (warning)
**File:** `src/reticulumpi/builtin_plugins/web_dashboard/static/map.js:803`

`R.toggleMapTrails` calls `_clearTrails()` but not `_stopTrailRefresh()`, leaving the 30s interval ticking. Harmless due to guards in the interval callback, but a state leak.

**Fix:** Add `_stopTrailRefresh()` call in `toggleMapTrails` when `enabled` is false.

### 3. `_clearTrails()` has no null guard on `_map` (warning)
**File:** `src/reticulumpi/builtin_plugins/web_dashboard/static/map.js:520`

Currently safe because `_trails` is empty when `_map` is null, but fragile if that invariant ever breaks.

**Fix:** Add `if (!_map) return;` at the top of `_clearTrails()`.

### 4. Server-side `api_cache` uses `max_entries=1` (warning)
**File:** `src/reticulumpi/builtin_plugins/web_dashboard/api_services.py:963`

Multiple clients tracking different node sets will thrash the single LRU slot, sending every request to SQLite. At ~2.5 requests/min per client this is acceptable for 1 client but degrades with multiple.

**Fix:** Set `max_entries=4` on the `@api_cache` decorator.

### 5. Missing test for `_on_position_recorded_event` (warning)
**File:** `tests/test_websocket_handler.py`

All five sibling WS event callbacks (`_on_alert_event`, `_on_internet_event`, `_on_firmware_event`, `_on_message_event`, `_on_status_event`) have dedicated test classes with 3-4 tests each. The new `_on_position_recorded_event` has no test.

**Fix:** Add `TestOnPositionRecordedEvent` class following the established pattern.

### 6. `NODE_POSITION_RECORDED` event emission not tested (warning)
**File:** `tests/test_node_location_tracker.py:167`

No test asserts `event_bus.publish` is called after position recording in `_collect_positions()`.

### 7. No test for WS handler subscribing to `NODE_POSITION_RECORDED` (warning)
**File:** `src/reticulumpi/builtin_plugins/web_dashboard/websocket_handler.py:1085`

The subscription/unsubscription in `_start/_stop_broadcast_task` is not verified by tests.

### 8. Worst-case request rate may stress SQLite on Pi (warning)
**File:** `src/reticulumpi/builtin_plugins/web_dashboard/api_services.py:963`

Steady-state: ~2.5 requests/min per client (30s timer + 0.5/min from WS events). Acceptable for single client. With max_entries=1 and multiple clients tracking different sets, every request hits SQLite.

### 9. Pre-existing: `_on_conversation_deleted_event` never unsubscribed (warning)
**File:** `src/reticulumpi/builtin_plugins/web_dashboard/websocket_handler.py:1118`

Not introduced by this commit. `_on_conversation_deleted_event` is subscribed at line 1078 but missing from `_stop_broadcast_task` unsubscribe block.

## Confirmed Correct (info highlights)

- Event constant `NODE_POSITION_RECORDED` exists at `events.py:228`, string value matches everywhere
- WS push type `"trail_update"` matches exactly across backend and frontend
- RPI namespace wiring correct: `R.onTrailUpdate` on `window.RPI` callable from `app.js`
- Subscribe/unsubscribe lifecycle properly paired for the new callback
- `_startTrailRefresh()` double-start guard works correctly
- `trail_update` WS message cannot shadow the `update` handler (distinct type + early return)
- `setTrailHours` and `toggleMapTrails` unchanged and functional
- Event bus overhead negligible (~0.5 events/min)
- `call_soon_threadsafe` thread-crossing pattern correct per established convention
- All 2511 tests pass, 0 failures, 4 skipped
