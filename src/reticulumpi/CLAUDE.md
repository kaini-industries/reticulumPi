# Core Modules

## Dependency Graph

```
cli.py -> app.py -> config.py
                 -> event_bus.py
                 -> plugin_loader.py
                 -> identity_manager.py
                 -> internet_probe.py
                 -> announce_dispatcher.py
                 -> sdr_scheduler.py
```

## Key Contracts

**plugin_base.py** -- ABC all plugins inherit. Threading primitives:
- `_stop_event` / `_active` property: cooperative shutdown signaling
- `_sleep_while_active(seconds)`: interruptible sleep via Event.wait
- `_start_thread(target, name)`: tracked daemon threads with global budget (soft limit 50)
- `_join_threads(timeout)`: shared-deadline join across all threads
- `broadcast_snapshot()` / `broadcast_tier` / `broadcast_keys`: dashboard data integration

**event_bus.py** -- two subscription modes:
- `subscribe()`: synchronous, runs in publisher's thread (fast handlers only)
- `subscribe_offloaded()`: background via ThreadPoolExecutor (4 workers, 64-pending backpressure)

**sdr_scheduler.py** -- priority-based RTL-SDR dongle time-sharing:
- P0 (CRITICAL): weather alerts -- preempts everything
- P1 (SCHEDULED): satellite passes -- time-windowed
- P2 (BACKGROUND): continuous decoders -- round-robin rotation

**events.py** -- append-only. Never rename or remove existing constants.

**announce_dispatcher.py** -- batched announce scheduling to prevent network flooding.
