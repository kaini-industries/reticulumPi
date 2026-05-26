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

## Async / WebSocket Test Pattern

Async handlers are tested with `@pytest.mark.asyncio` or `asyncio.run()`. No aiohttp test
client -- call handler functions directly with mock requests.

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest

@pytest.fixture(autouse=True)
def _reset_ws_clients():
    _ws_clients.clear()
    yield
    _ws_clients.clear()

@pytest.mark.asyncio
async def test_broadcast(self):
    ws = MagicMock()
    ws.send_str = AsyncMock()
    _ws_clients.add(ws)
    await _broadcast_metrics(app_mock)
    ws.send_str.assert_called_once()
```

Cancel async loops by raising `asyncio.CancelledError`. Patch `asyncio.sleep` to avoid delays.

## REST API Test Pattern

Build mock `aiohttp.web.Request` objects, call the handler, parse the `web.Response`:

```python
def _make_request(body=None, match_info=None, plugin_mock=None):
    request = MagicMock()
    request.query = {}
    request.match_info = match_info or {}
    request.remote = "127.0.0.1"
    async def _json(): return body
    request.json = _json
    request.app = {"plugin": plugin_mock or MagicMock()}
    return request

resp = asyncio.run(handle_some_endpoint(request))
data = json.loads(resp.text)
assert resp.status == 200
```

Canonical example: `test_api_write_endpoints.py`.

## Inter-Plugin Communication Pattern

Use a real `EventBus` instance (not a mock) to test subscription/publication:

```python
from reticulumpi.event_bus import EventBus

def test_event_triggers_action(mock_app):
    mock_app.event_bus = EventBus()
    received = threading.Event()
    mock_app.event_bus.subscribe("alert.triggered", lambda e, d: received.set())
    plugin = MyPlugin(mock_app, config)
    plugin.start()
    mock_app.event_bus.publish("plugin.crashed", {"name": "x", "error": "boom"})
    assert received.wait(timeout=5)
    plugin.stop()
```

For `app.get_plugin()`, wire up mock plugins:

```python
other = MagicMock()
other.some_method.return_value = "data"
mock_app.get_plugin.return_value = other
```

## Rules

- Always mock external I/O: no real network, serial ports, or subprocesses
- `@pytest.mark.integration` for tests needing a real Reticulum instance (excluded from CI)
- `@pytest.mark.asyncio` for async tests (websocket_handler)

## Scoped Test Commands

Single plugin: `.venv/bin/pytest tests/test_<plugin_name>.py -v`
Single test: `.venv/bin/pytest tests/test_foo.py::TestClass::test_name -v -n0`
Core modules: `.venv/bin/pytest tests/test_app.py tests/test_event_bus.py tests/test_plugin_loader.py -v`
Dashboard: `.venv/bin/pytest tests/test_websocket_handler.py tests/test_api_write_endpoints.py tests/test_broadcast_registry.py -v`
