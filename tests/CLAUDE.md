# Tests

## Structure

Flat directory -- all test files here. Naming: `test_<module_or_plugin>.py`.

Framework: pytest with pytest-xdist (`-n 2`), pytest-timeout (`--timeout=60`), pytest-asyncio.
Override parallelism for debugging: `-n0`

## Core Fixtures (conftest.py)

- `mock_app` -- mock ReticulumPiApp with mock Reticulum, Identity, EventBus, plugins dict
- `mock_rns_reticulum` -- patches `RNS.Reticulum` constructor
- `mock_rns_identity` -- mock Identity with zeroed hash
- `tmp_config` -- temp YAML config file with sample plugin sections
- `plugin_dir` -- temp directory with a sample plugin .py

## Hardware Library Mocking Pattern

Plugins importing optional deps (meshtastic, paho-mqtt, sgp4, pyserial, etc.) need
`sys.modules` patching at module level BEFORE importing the plugin under test.

Canonical example: `test_meshtastic_gateway.py` lines 16-55 -- creates module-level
MagicMock objects, then uses `@pytest.fixture(autouse=True)` with `patch.dict(sys.modules, {...})`
to inject them before every test.

## Rules

- Always mock external I/O: no real network, serial ports, or subprocesses
- `@pytest.mark.integration` for tests needing a real Reticulum instance (excluded from CI)
- `@pytest.mark.asyncio` for async tests (websocket_handler)
