"""Tests for the api_cache response caching decorator."""

import asyncio
from unittest.mock import MagicMock

import pytest

import aiohttp.web

from reticulumpi.builtin_plugins.web_dashboard.api_cache import (
    ApiResponseCache,
    api_cache,
)


class TestApiResponseCache:
    def test_put_and_get(self):
        cache = ApiResponseCache(max_entries=5)
        cache.put("/a", b"body-a", "application/json")
        entry = cache.get("/a")
        assert entry is not None
        assert entry[0] == b"body-a"
        assert entry[1] == "application/json"

    def test_get_missing_returns_none(self):
        cache = ApiResponseCache(max_entries=5)
        assert cache.get("/missing") is None

    def test_lru_eviction(self):
        cache = ApiResponseCache(max_entries=2)
        cache.put("/a", b"a", "text/plain")
        cache.put("/b", b"b", "text/plain")
        cache.put("/c", b"c", "text/plain")
        assert cache.get("/a") is None
        assert cache.get("/b") is not None
        assert cache.get("/c") is not None

    def test_access_refreshes_lru_order(self):
        cache = ApiResponseCache(max_entries=2)
        cache.put("/a", b"a", "text/plain")
        cache.put("/b", b"b", "text/plain")
        cache.get("/a")
        cache.put("/c", b"c", "text/plain")
        assert cache.get("/a") is not None
        assert cache.get("/b") is None


def _make_request(path="/test"):
    req = MagicMock(spec=aiohttp.web.Request)
    req.path_qs = path
    return req


def _make_response(body=b'{"ok":true}', content_type="application/json"):
    return aiohttp.web.Response(body=body, content_type=content_type)


class TestApiCacheDecorator:
    @pytest.mark.asyncio
    async def test_cache_hit(self):
        call_count = 0

        @api_cache(ttl=10, stale=0)
        async def handler(request):
            nonlocal call_count
            call_count += 1
            return _make_response(b'{"n":1}')

        req = _make_request()
        resp1 = await handler(req)
        resp2 = await handler(req)
        assert call_count == 1
        assert resp1.body == b'{"n":1}'
        assert resp2.body == b'{"n":1}'

    @pytest.mark.asyncio
    async def test_cache_miss_after_ttl(self, monkeypatch):
        calls = []
        t = [100.0]
        monkeypatch.setattr(
            "reticulumpi.builtin_plugins.web_dashboard.api_cache._monotonic",
            lambda: t[0],
        )

        @api_cache(ttl=5, stale=0)
        async def handler(request):
            calls.append(1)
            return _make_response(f'{{"n":{len(calls)}}}'.encode())

        req = _make_request()
        await handler(req)
        assert len(calls) == 1

        t[0] = 106.0
        await handler(req)
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_stale_while_revalidate(self, monkeypatch):
        calls = []
        t = [100.0]
        monkeypatch.setattr(
            "reticulumpi.builtin_plugins.web_dashboard.api_cache._monotonic",
            lambda: t[0],
        )

        @api_cache(ttl=5, stale=30)
        async def handler(request):
            calls.append(1)
            return _make_response(f'{{"n":{len(calls)}}}'.encode())

        req = _make_request()
        resp1 = await handler(req)
        assert len(calls) == 1

        t[0] = 107.0
        resp2 = await handler(req)
        assert resp2.body == resp1.body
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_different_paths_get_separate_entries(self):
        call_count = 0

        @api_cache(ttl=60, stale=0, max_entries=10)
        async def handler(request):
            nonlocal call_count
            call_count += 1
            return _make_response(f'{{"path":"{request.path_qs}"}}'.encode())

        await handler(_make_request("/a"))
        await handler(_make_request("/b"))
        assert call_count == 2

        await handler(_make_request("/a"))
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_cache_control_header(self):
        @api_cache(ttl=10, stale=30)
        async def handler(request):
            return _make_response()

        resp = await handler(_make_request())
        assert "max-age=10" in resp.headers["Cache-Control"]
        assert "stale-while-revalidate=30" in resp.headers["Cache-Control"]
        assert "private" in resp.headers["Cache-Control"]
        assert resp.headers["Vary"] == "Cookie, Authorization"

    @pytest.mark.asyncio
    async def test_error_response_is_not_cached_or_relabelled_success(self):
        calls = 0

        @api_cache(ttl=60)
        async def handler(request):
            nonlocal calls
            calls += 1
            return aiohttp.web.Response(
                status=503,
                body=f"failure-{calls}".encode(),
                content_type="text/plain",
            )

        first = await handler(_make_request())
        second = await handler(_make_request())
        assert first.status == second.status == 503
        assert first.body != second.body
        assert calls == 2
        assert first.headers["Cache-Control"] == "private, no-store"

    @pytest.mark.asyncio
    async def test_concurrent_requests_no_thundering_herd(self):
        call_count = 0
        gate = asyncio.Event()

        @api_cache(ttl=60, stale=0)
        async def handler(request):
            nonlocal call_count
            call_count += 1
            await gate.wait()
            return _make_response()

        async def fire():
            return await handler(_make_request())

        t1 = asyncio.create_task(fire())
        t2 = asyncio.create_task(fire())
        await asyncio.sleep(0.01)
        gate.set()
        await asyncio.gather(t1, t2)
        assert call_count == 1
