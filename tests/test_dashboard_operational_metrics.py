"""Secret-free dashboard operational metrics regression tests."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp.web
import pytest

from reticulumpi import __version__
from reticulumpi.builtin_plugins.web_dashboard import api as dashboard_api
from reticulumpi.builtin_plugins.web_dashboard import api_cache as cache_module
from reticulumpi.builtin_plugins.web_dashboard import server as dashboard_server
from reticulumpi.builtin_plugins.web_dashboard import websocket_handler as websocket_module
from reticulumpi.builtin_plugins.web_dashboard.operational_metrics import (
    _reset_dashboard_operational_metrics,
    get_dashboard_operational_metrics,
    record_websocket_close,
)


@pytest.fixture(autouse=True)
def _reset_metrics():
    _reset_dashboard_operational_metrics()
    websocket_module._ws_clients.clear()
    websocket_module._spectrum_clients.clear()
    websocket_module._ws_pending = 0
    yield
    websocket_module._ws_clients.clear()
    websocket_module._spectrum_clients.clear()
    websocket_module._ws_pending = 0
    _reset_dashboard_operational_metrics()


def _snapshot(plugin=None):
    return get_dashboard_operational_metrics(plugin)


def test_snapshot_has_fixed_secret_free_schema_and_bounded_usage():
    plugin = SimpleNamespace(
        config={
            "tile_proxy": {
                "enabled": True,
                "cache_dir": "/private/cache/customer-a",
                "upstream_url": "https://10.0.0.8/tiles?token=private-token",
            },
            "password": "private-password",
        },
        _tile_cache_bytes=1234,
        _tile_max_bytes=5678,
        _local_api_token="private-local-token",
    )

    metrics = _snapshot(plugin)

    assert set(metrics) == {
        "websocket",
        "auth_admission",
        "api_cache_refresh",
        "workers",
        "tile_cache",
        "service_worker",
    }
    assert metrics["tile_cache"]["usage_bytes"] == 1234
    assert metrics["tile_cache"]["limit_bytes"] == 5678
    assert metrics["service_worker"]["version"] == str(__version__)[:128]
    assert metrics["workers"] == {"broadcast_hung_total": 0}
    assert set(metrics["websocket"]["close_reasons"]) == {
        "normal",
        "going_away",
        "authentication",
        "capacity",
        "origin",
        "message_too_large",
        "protocol",
        "abnormal",
        "other",
    }

    encoded = json.dumps(metrics, sort_keys=True)
    for secret in (
        "private/cache",
        "10.0.0.8",
        "private-token",
        "private-password",
        "customer-a",
    ):
        assert secret not in encoded


@pytest.mark.asyncio
async def test_websocket_rejections_and_close_codes_use_fixed_categories():
    async def reject(request):
        ws = MagicMock()
        ws.prepare = AsyncMock()
        ws.close = AsyncMock()
        with patch.object(websocket_module.aiohttp.web, "WebSocketResponse", return_value=ws):
            await websocket_module.websocket_metrics(request)

    plugin = SimpleNamespace(
        config={"host": "localhost", "port": 8080, "max_websocket_clients": 10},
        _auth=SimpleNamespace(validate_token=lambda _token: True),
    )
    cross_origin = SimpleNamespace(
        headers={"Origin": "http://untrusted.invalid"},
        host="localhost:8080",
        app={"plugin": plugin},
    )
    await reject(cross_origin)

    unauthenticated = SimpleNamespace(
        headers={"Origin": "http://localhost:8080"},
        host="localhost:8080",
        cookies={},
        app={"plugin": plugin},
    )
    await reject(unauthenticated)

    plugin.config["max_websocket_clients"] = 0
    saturated = SimpleNamespace(
        headers={"Origin": "http://localhost:8080"},
        host="localhost:8080",
        cookies={"session": "not-exported"},
        app={"plugin": plugin},
    )
    await reject(saturated)

    for code in (1000, 1001, 1009, 1002, 1006, None, 4999, []):
        record_websocket_close(code)

    reasons = _snapshot()["websocket"]["close_reasons"]
    assert reasons == {
        "normal": 1,
        "going_away": 1,
        "authentication": 1,
        "capacity": 1,
        "origin": 1,
        "message_too_large": 1,
        "protocol": 1,
        "abnormal": 2,
        "other": 2,
    }


@pytest.mark.asyncio
async def test_auth_admission_reports_in_flight_peak_and_saturation():
    started = threading.Event()
    finish = threading.Event()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    plugin = SimpleNamespace(
        _auth_executor=executor,
        _auth_slots=threading.BoundedSemaphore(1),
    )

    def slow_work():
        started.set()
        finish.wait(timeout=2)
        return "complete"

    try:
        task = asyncio.create_task(dashboard_api._run_auth_work(plugin, slow_work))
        assert await asyncio.to_thread(started.wait, 1)

        admitted, result = await dashboard_api._run_auth_work(plugin, lambda: "not-run")
        assert admitted is False
        assert result is None

        active = _snapshot()["auth_admission"]
        assert active["capacity"] == 4
        assert active["attempts"] == 2
        assert active["admitted"] == 1
        assert active["saturated"] == 1
        assert active["in_flight"] == 1
        assert active["peak_in_flight"] == 1

        finish.set()
        assert await task == (True, "complete")
        complete = _snapshot()["auth_admission"]
        assert complete["in_flight"] == 0
        assert complete["work_failures"] == 0
    finally:
        finish.set()
        executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
async def test_api_cache_refresh_metrics_cover_pending_success_and_failure(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(cache_module, "_monotonic", lambda: clock[0])
    gate = asyncio.Event()
    calls = 0

    @cache_module.api_cache(ttl=5, stale=30)
    async def successful_handler(_request):
        nonlocal calls
        calls += 1
        if calls > 1:
            await gate.wait()
        return aiohttp.web.Response(body=b'{"ok":true}', content_type="application/json")

    request = SimpleNamespace(path_qs="/aggregate-only")
    await successful_handler(request)
    clock[0] = 110.0
    await successful_handler(request)

    pending = _snapshot()["api_cache_refresh"]
    assert pending["started"] == 1
    assert pending["pending"] == 1
    gate.set()
    for _ in range(50):
        if _snapshot()["api_cache_refresh"]["pending"] == 0:
            break
        await asyncio.sleep(0.01)
    succeeded = _snapshot()["api_cache_refresh"]
    assert succeeded["succeeded"] == 1
    assert succeeded["pending"] == 0

    failure_calls = 0

    @cache_module.api_cache(ttl=5, stale=30)
    async def failing_handler(_request):
        nonlocal failure_calls
        failure_calls += 1
        if failure_calls > 1:
            raise RuntimeError("refresh fixture")
        return aiohttp.web.Response(body=b'{"ok":true}', content_type="application/json")

    failure_request = SimpleNamespace(path_qs="/aggregate-failure")
    await failing_handler(failure_request)
    clock[0] = 120.0
    await failing_handler(failure_request)
    for _ in range(50):
        if _snapshot()["api_cache_refresh"]["pending"] == 0:
            break
        await asyncio.sleep(0.01)

    failed = _snapshot()["api_cache_refresh"]
    assert failed["started"] == 2
    assert failed["succeeded"] == 1
    assert failed["failed"] == 1
    assert failed["pending"] == 0


@pytest.mark.asyncio
async def test_api_cache_refresh_metrics_cover_skipped_and_cancelled(monkeypatch):
    monkeypatch.setattr(cache_module, "_monotonic", lambda: 100.0)
    cache = cache_module.ApiResponseCache()
    cache.put("/fresh", b"{}", "application/json")

    async def unused_handler(_request):
        raise AssertionError("fresh entry should skip the handler")

    skipped = asyncio.create_task(
        cache_module._refresh(cache, "/fresh", unused_handler, SimpleNamespace())
    )
    cache._refresh_tasks["/fresh"] = skipped
    cache_module.record_api_refresh_started()
    assert await skipped == "skipped"
    cache_module._finish_refresh(cache, "/fresh", skipped)

    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_handler(_request):
        entered.set()
        await release.wait()
        return aiohttp.web.Response(body=b"{}", content_type="application/json")

    cancelled = asyncio.create_task(
        cache_module._refresh(cache, "/cancelled", blocked_handler, SimpleNamespace())
    )
    cache._refresh_tasks["/cancelled"] = cancelled
    cache_module.record_api_refresh_started()
    await entered.wait()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    cache_module._finish_refresh(cache, "/cancelled", cancelled)

    metrics = _snapshot()["api_cache_refresh"]
    assert metrics["started"] == 2
    assert metrics["skipped"] == 1
    assert metrics["cancelled"] == 1
    assert metrics["pending"] == 0


@pytest.mark.asyncio
async def test_tile_metrics_track_disk_usage_hits_misses_storage_and_capacity(tmp_path):
    payload = b"\x89PNG\r\n\x1a\nfixture"
    upstream = SimpleNamespace(
        status=200,
        headers={"Content-Type": "image/png"},
        content=SimpleNamespace(read=AsyncMock(return_value=payload)),
    )
    response_context = MagicMock()
    response_context.__aenter__ = AsyncMock(return_value=upstream)
    response_context.__aexit__ = AsyncMock(return_value=False)
    plugin = SimpleNamespace(
        config={
            "tile_proxy": {
                "enabled": True,
                "cache_dir": "/not-exported/cache",
                "upstream_url": "https://192.0.2.1/{z}/{x}/{y}.png?token=not-exported",
            }
        },
        _tile_session=SimpleNamespace(get=MagicMock(return_value=response_context)),
        _tile_cache_dir=str(tmp_path),
        _tile_locks={},
        _tile_upstream="https://tiles.invalid/{z}/{x}/{y}.png",
        _tile_max_tile_bytes=1024,
        _tile_max_bytes=0,
        _tile_cache_bytes=0,
    )

    def request(y: int):
        return SimpleNamespace(
            app={"plugin": plugin},
            match_info={"z": "1", "x": "0", "y": str(y)},
        )

    stored = await dashboard_server._handle_tile_proxy(request(0))
    assert stored.status == 200
    cached = await dashboard_server._handle_tile_proxy(request(0))
    assert isinstance(cached, aiohttp.web.FileResponse)

    plugin._tile_max_bytes = plugin._tile_cache_bytes
    uncached = await dashboard_server._handle_tile_proxy(request(1))
    assert uncached.status == 200

    metrics = _snapshot(plugin)["tile_cache"]
    assert metrics["enabled"] is True
    assert metrics["usage_bytes"] == len(payload)
    assert metrics["limit_bytes"] == len(payload)
    assert metrics["hits"] == 1
    assert metrics["misses"] == 2
    assert metrics["stored"] == 2
    assert metrics["evictions"] == 1
    assert metrics["rejects"] == 0
    assert metrics["reject_reasons"]["capacity"] == 0

    encoded = json.dumps(metrics)
    assert "not-exported" not in encoded
    assert "192.0.2.1" not in encoded
