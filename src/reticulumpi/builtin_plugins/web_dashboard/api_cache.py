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
from collections import OrderedDict
from time import monotonic as _monotonic
from typing import Any, Callable, NamedTuple

import aiohttp.web

from reticulumpi.builtin_plugins.web_dashboard.operational_metrics import (
    record_api_refresh_finished,
    record_api_refresh_started,
)

log = logging.getLogger(__name__)


class _CacheEntry(NamedTuple):
    """Complete response state safe to replay to an authenticated caller."""

    body: bytes
    content_type: str
    status: int
    reason: str | None
    timestamp: float


class ApiResponseCache:
    """In-memory LRU response cache with TTL and stale-while-revalidate."""

    def __init__(self, max_entries: int = 1) -> None:
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._max = max_entries
        self._lock = asyncio.Lock()
        self._refresh_tasks: dict[str, asyncio.Task[str]] = {}

    def get(self, key: str) -> _CacheEntry | None:
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)
        return entry

    def put(
        self,
        key: str,
        body: bytes,
        content_type: str,
        status: int = 200,
        reason: str | None = None,
    ) -> None:
        self._entries[key] = _CacheEntry(
            body=body,
            content_type=content_type,
            status=status,
            reason=reason,
            timestamp=_monotonic(),
        )
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
            now = _monotonic()

            if entry is not None:
                age = now - entry.timestamp

                if age < ttl:
                    return _cached_response(entry, ttl, stale)

                if stale and age < ttl + stale:
                    if key not in cache._refresh_tasks:
                        task = asyncio.create_task(_refresh(cache, key, handler, request))
                        cache._refresh_tasks[key] = task
                        record_api_refresh_started()
                        task.add_done_callback(
                            lambda _task, *, _key=key: _finish_refresh(cache, _key, _task)
                        )
                    return _cached_response(entry, ttl, stale)

            async with cache._lock:
                entry = cache.get(key)
                if entry is not None and _monotonic() - entry.timestamp < ttl:
                    return _cached_response(entry, ttl, stale)

                resp = await handler(request)
                if _is_cacheable(resp):
                    cache.put(
                        key,
                        resp.body or b"",
                        resp.content_type,
                        resp.status,
                        resp.reason,
                    )
                    _set_cache_headers(resp, ttl, stale)
                else:
                    resp.headers.setdefault("Cache-Control", "private, no-store")
                return resp

        return wrapper

    return decorator


async def _refresh(
    cache: ApiResponseCache,
    key: str,
    handler: Callable,
    request: Any,
) -> str:
    """Background refresh — errors are swallowed to keep stale data alive."""
    try:
        async with cache._lock:
            entry = cache.get(key)
            if entry is not None and _monotonic() - entry.timestamp < 2.0:
                return "skipped"
            resp = await handler(request)
            if _is_cacheable(resp):
                cache.put(
                    key,
                    resp.body or b"",
                    resp.content_type,
                    resp.status,
                    resp.reason,
                )
                return "succeeded"
            return "failed"
    except asyncio.CancelledError:
        raise
    except Exception:
        log.debug("Background refresh failed for %s", key, exc_info=True)
        return "failed"


def _finish_refresh(cache: ApiResponseCache, key: str, task: asyncio.Task[str]) -> None:
    cache._refresh_tasks.pop(key, None)
    if task.cancelled():
        outcome = "cancelled"
    else:
        try:
            outcome = task.result()
        except BaseException:
            outcome = "failed"
    record_api_refresh_finished(outcome)


def _is_cacheable(resp: aiohttp.web.Response) -> bool:
    """Only retain successful, materialised responses without credentials."""
    return (
        resp.status == 200
        and resp.body is not None
        and "Set-Cookie" not in resp.headers
        and "WWW-Authenticate" not in resp.headers
    )


def _cached_response(entry: _CacheEntry, ttl: float, stale: float) -> aiohttp.web.Response:
    resp = aiohttp.web.Response(
        body=entry.body,
        content_type=entry.content_type,
        status=entry.status,
        reason=entry.reason,
    )
    _set_cache_headers(resp, ttl, stale)
    return resp


def _set_cache_headers(resp: aiohttp.web.Response, ttl: float, stale: float) -> None:
    parts = ["private", f"max-age={int(ttl)}"]
    if stale:
        parts.append(f"stale-while-revalidate={int(stale)}")
    resp.headers["Cache-Control"] = ", ".join(parts)
    resp.headers["Vary"] = "Cookie, Authorization"
