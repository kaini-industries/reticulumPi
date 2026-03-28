"""aiohttp application setup, routes, security middleware."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import aiohttp.web

if TYPE_CHECKING:
    from reticulumpi.builtin_plugins.web_dashboard.plugin import WebDashboardPlugin

# Paths that do not require authentication
PUBLIC_PATHS = frozenset({
    "/login.html",
    "/api/auth/login",
    "/auth/login",
})

# Static file prefixes that are public
PUBLIC_PREFIXES = ("/static/",)


def create_app(plugin: WebDashboardPlugin) -> aiohttp.web.Application:
    """Build and return the aiohttp Application with all routes and middleware."""
    from reticulumpi.builtin_plugins.web_dashboard.api import setup_api_routes
    from reticulumpi.builtin_plugins.web_dashboard.websocket_handler import (
        setup_websocket_routes,
    )

    app = aiohttp.web.Application(
        middlewares=[
            security_headers_middleware,
            auth_middleware_factory(plugin),
        ]
    )
    app["plugin"] = plugin

    # API and WebSocket routes
    setup_api_routes(app)
    setup_websocket_routes(app)

    # Static files and root redirect
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app.router.add_static("/static/", static_dir, show_index=False)

    # Serve login.html and index.html directly
    app.router.add_get("/login.html", _serve_login)
    app.router.add_get("/", _serve_index)
    app.router.add_get("/index.html", _serve_index)

    return app


@aiohttp.web.middleware
async def security_headers_middleware(
    request: aiohttp.web.Request,
    handler,
) -> aiohttp.web.StreamResponse:
    """Add security headers to all responses."""
    response = await handler(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; connect-src 'self' ws: wss:; "
        "style-src 'self'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def auth_middleware_factory(plugin: WebDashboardPlugin):
    """Create authentication middleware that checks session tokens."""

    @aiohttp.web.middleware
    async def auth_middleware(
        request: aiohttp.web.Request,
        handler,
    ) -> aiohttp.web.StreamResponse:
        path = request.path

        # Allow public paths
        if path in PUBLIC_PATHS:
            return await handler(request)
        for prefix in PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return await handler(request)

        # Extract token from Authorization header or cookie
        token = _extract_token(request)
        if token and plugin._auth.validate_token(token):
            request["token"] = token
            return await handler(request)

        # Not authenticated
        accept = request.headers.get("Accept", "")
        if "text/html" in accept:
            raise aiohttp.web.HTTPFound("/login.html")
        raise aiohttp.web.HTTPUnauthorized(
            text='{"ok": false, "error": "Authentication required", "code": 401}',
            content_type="application/json",
        )

    return auth_middleware


def _extract_token(request: aiohttp.web.Request) -> str | None:
    """Extract bearer token from Authorization header or session cookie."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]

    return request.cookies.get("session")


async def _serve_login(request: aiohttp.web.Request) -> aiohttp.web.FileResponse:
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    return aiohttp.web.FileResponse(os.path.join(static_dir, "login.html"))


async def _serve_index(request: aiohttp.web.Request) -> aiohttp.web.FileResponse:
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    return aiohttp.web.FileResponse(os.path.join(static_dir, "index.html"))
