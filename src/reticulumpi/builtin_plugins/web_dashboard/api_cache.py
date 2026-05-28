"""Lightweight HTTP response cache for aiohttp API handlers.

Caches the serialized response body (bytes) keyed by request path + query
string.  Uses per-endpoint asyncio locks to prevent thundering-herd on
concurrent cache misses.  Supports stale-while-revalidate semantics.

Usage::

    from .api_cache import api_cache

    @api_cache(ttl=10, stale=30)
    async def handle_nodes(request):
        ...
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections import OrderedDict
from typing import Any, Callable

import aiohttp.web

log = logging.getLogger(__name__)

_CacheEntry = tuple[bytes, str, float]  # (body, content_type, timestamp)


class ApiResponseCache:
    """In-memory LRU response cache with TTL and stale-while-revalidate."""

    def __init__(self, max_entries: int = 1) -> None:
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._max = max_entries
        self._lock = asyncio.Lock()

    def get(self, key: str) -> _CacheEntry | None:
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)
        return entry

    def put(self, key: str, body: bytes, content_type: str) -> None:
        self._entries[key] = (body, content_type, time.time())
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)


def api_cache(
    ttl: float,
    stale: float = 0,
    max_entries: int = 1,
) -> Callable:
    """Decorator factory for caching aiohttp handler responses.

    Args:
        ttl: Seconds a cached response is considered fresh.
        stale: Additional seconds a stale response may be served while
            a background refresh runs.  0 disables stale-while-revalidate.
        max_entries: LRU cache capacity (>1 for query-parameterised endpoints).
    """

    def decorator(handler: Callable) -> Callable:
        cache = ApiResponseCache(max_entries)

        @functools.wraps(handler)
        async def wrapper(request: aiohttp.web.Request) -> aiohttp.web.Response:
            key = request.path_qs

            entry = cache.get(key)
            now = time.time()

            if entry is not None:
                body, ct, ts = entry
                age = now - ts

                if age < ttl:
                    return _cached_response(body, ct, ttl, stale)

                if stale and age < ttl + stale:
                    asyncio.ensure_future(_refresh(cache, key, handler, request))
                    return _cached_response(body, ct, ttl, stale)

            async with cache._lock:
                entry = cache.get(key)
                if entry is not None and time.time() - entry[2] < ttl:
                    return _cached_response(entry[0], entry[1], ttl, stale)

                resp = await handler(request)
                cache.put(key, resp.body, resp.content_type)
                _set_cache_headers(resp, ttl, stale)
                return resp

        return wrapper

    return decorator


async def _refresh(
    cache: ApiResponseCache,
    key: str,
    handler: Callable,
    request: Any,
) -> None:
    """Background refresh — errors are swallowed to keep stale data alive."""
    try:
        async with cache._lock:
            entry = cache.get(key)
            if entry is not None and time.time() - entry[2] < 2.0:
                return
            resp = await handler(request)
            cache.put(key, resp.body, resp.content_type)
    except Exception:
        log.debug("Background refresh failed for %s", key, exc_info=True)


def _cached_response(
    body: bytes, content_type: str, ttl: float, stale: float
) -> aiohttp.web.Response:
    resp = aiohttp.web.Response(body=body, content_type=content_type)
    _set_cache_headers(resp, ttl, stale)
    return resp


def _set_cache_headers(
    resp: aiohttp.web.Response, ttl: float, stale: float
) -> None:
    parts = [f"max-age={int(ttl)}"]
    if stale:
        parts.append(f"stale-while-revalidate={int(stale)}")
    resp.headers["Cache-Control"] = ", ".join(parts)
