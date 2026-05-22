# Builtin Plugins

## Inheritance

Two base classes in the parent directory:
- **PluginBase** (`plugin_base.py`): direct inheritance for non-SDR plugins
- **SignalPluginBase** (`signal_plugin_base.py`): for RTL-SDR plugins -- adds dongle scheduling,
  subprocess lifecycle, snapshot caching. Used by: adsb_radar, ais_receiver, acars_decoder,
  noaa_apt_decoder, radiosonde_tracker, weather_alert, ism_decoder, spectrum_scanner,
  lora_scanner, fm_receiver

## Dashboard Integration

Plugins participate in WebSocket broadcasts by setting class attributes:
- `broadcast_tier`: 0=always, 1=important, 2=background
- `broadcast_keys`: string or list of keys for the broadcast payload
- Override `broadcast_snapshot()` or `get_snapshot()` to return data

The broadcast registry (`web_dashboard/broadcast_registry.py`) collects snapshots from all
participating plugins within a time budget.

## web_dashboard/ Sub-Package

Not a single file -- it's a full sub-package:
- `plugin.py` -- WebDashboardPlugin (aiohttp server, SSL, mDNS)
- `server.py` -- app factory, middleware (auth, CORS, rate limiting)
- `auth.py` -- scrypt password hashing, token sessions, SQLite persistence
- `api.py` -- core REST routes
- `api_radio.py`, `api_mesh.py`, `api_services.py`, `api_interfaces.py` -- domain-specific routes
- `websocket_handler.py` -- `/ws/metrics` and `/ws/spectrum` endpoints, diff-based updates
- `broadcast_registry.py` -- tiered data collection
- `static/` -- vanilla JS frontend (no framework, no build step), one .js per panel

## New Plugin Checklist

1. Create `<name>.py` here (inherit PluginBase or SignalPluginBase)
2. Add `tests/test_<name>.py` with mock_app fixture
3. Add config section to `config/reticulumpi/config.example.yaml`
4. Add entry to `docs/plugins.md`
5. Reference `example_plugin.py` for the minimal scaffold
