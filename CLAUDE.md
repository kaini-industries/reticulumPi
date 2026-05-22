# ReticulumPi

Plugin-based Reticulum mesh networking node for Raspberry Pi 5 (ARM64, 4 cores).

## Architecture

Core orchestrator `src/reticulumpi/app.py` (`ReticulumPiApp`) initializes Reticulum, loads
plugins via `plugin_loader.py`, starts them in dependency order, manages shutdown in reverse.

Inter-plugin communication: `event_bus.py` (thread-safe pub/sub, ~120 event types in `events.py`).

Two plugin base classes:
- `PluginBase` (`plugin_base.py`) -- all 44 plugins inherit from this
- `SignalPluginBase` (`signal_plugin_base.py`) -- RTL-SDR signal plugins, adds dongle scheduling

Dashboard: `builtin_plugins/web_dashboard/` -- aiohttp backend + vanilla JS frontend (no build step).

## Commands

```
make test                          # full suite (pytest -n 2 --timeout=60)
make lint                          # ruff check src/ plugins/ tests/
make format                        # ruff format + --fix
make test-cov                      # pytest with coverage report
sudo bash scripts/update.sh        # deploy: pull, pip upgrade, sync systemd, restart
```

Single plugin test: `.venv/bin/pytest tests/test_<plugin_name>.py -v`
Debug (no parallelism): `.venv/bin/pytest tests/test_foo.py -v -n0`

## Gotchas

- **Always mock RNS in tests.** Never instantiate real `RNS.Reticulum`. Use `conftest.py` fixtures:
  `mock_app`, `mock_rns_reticulum`, `mock_rns_identity`.
- **Hardware library mocking:** Plugins importing optional deps (meshtastic, paho-mqtt, sgp4,
  pyserial) need `sys.modules` patching BEFORE import. See `tests/test_meshtastic_gateway.py`
  lines 16-55 for the canonical pattern.
- **Version lives in TWO files:** `pyproject.toml:7` and `src/reticulumpi/__init__.py:3`.
  Both must be updated together.
- **Secrets:** `.env` and `*.identity` files are secrets -- never commit.
- **Reticulum config format:** Interface sections use double brackets `[[Name]]` under `[interfaces]`.
  Single brackets are silently ignored.
- **systemd ReadWritePaths:** All directories in ReadWritePaths must exist before the service
  starts, or namespace mounting fails (exit 226).

## Code Style

- Ruff enforces style. Line length 100, target Python 3.9+.
- Plugin filenames are snake_case matching `plugin_name`.
- Event constants: UPPER_SNAKE_CASE in `events.py` (append-only by convention).

## Key Directories

- `src/reticulumpi/` -- core modules ([details](src/reticulumpi/CLAUDE.md))
- `src/reticulumpi/builtin_plugins/` -- 44 plugins ([details](src/reticulumpi/builtin_plugins/CLAUDE.md))
- `tests/` -- 58 test files ([details](tests/CLAUDE.md))
- `scripts/` -- bootstrap.sh, update.sh, offline simulation
- `config/` -- example YAML configs, Reticulum config templates, systemd units
- `docs/` -- plugin-development.md, api-reference.md, install-layout.md
