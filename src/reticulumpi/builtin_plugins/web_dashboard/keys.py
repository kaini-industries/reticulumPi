"""Typed aiohttp state keys with compatibility accessors for test doubles."""

from __future__ import annotations

from typing import Any

import aiohttp.web


PLUGIN_KEY = aiohttp.web.AppKey("reticulumpi.dashboard.plugin", object)
WS_COMPRESS_KEY = aiohttp.web.AppKey("reticulumpi.dashboard.ws_compress", bool)
AUTH_TOKEN_KEY = aiohttp.web.RequestKey("reticulumpi.dashboard.auth_token", str)
LOCAL_API_KEY = aiohttp.web.RequestKey("reticulumpi.dashboard.local_api", bool)


def get_app_plugin(app: Any) -> Any:
    try:
        plugin = app[PLUGIN_KEY]
    except (KeyError, TypeError):
        return app["plugin"]
    return plugin if plugin is not None else app["plugin"]


def get_ws_compress(app: Any) -> bool:
    try:
        value = app[WS_COMPRESS_KEY]
    except (KeyError, TypeError):
        return bool(app.get("ws_compress", True))
    if value is None:
        return bool(app.get("ws_compress", True))
    return bool(value)


def get_request_token(request: Any) -> str | None:
    token = request.get(AUTH_TOKEN_KEY)
    if token is None:
        token = request.get("token")
    return token
