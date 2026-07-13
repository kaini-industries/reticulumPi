"""Tests for web dashboard server middleware (auth + security headers)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import aiohttp.web
import pytest
from aiohttp.test_utils import make_mocked_request


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_transport(peername=None):
    """Create a mock transport with configurable peername."""
    transport = Mock()

    def get_extra_info(key):
        if key == "peername":
            return peername
        return None

    transport.get_extra_info.side_effect = get_extra_info
    return transport


def _make_request(method, path, headers=None, peername=None):
    """Build a mocked aiohttp request with optional peername and headers."""
    transport = _make_transport(peername)
    protocol = Mock()
    protocol.transport = transport
    protocol.max_field_size = 8190
    protocol.max_line_length = 8190
    protocol.max_headers = 128
    type(protocol).peername = property(lambda self: transport.get_extra_info("peername"))
    type(protocol).ssl_context = property(lambda self: None)

    async def _noop_write_headers(*a):
        pass

    writer = Mock()
    writer.write_headers = _noop_write_headers
    writer.transport = transport

    req = make_mocked_request(
        method,
        path,
        headers=headers or {},
        transport=transport,
        protocol=protocol,
        writer=writer,
    )
    return req


def _make_plugin(allow_localhost_api=False, token_valid=True):
    """Build a mock plugin with config and _auth.validate_token."""
    plugin = MagicMock()
    plugin.config = {"local_api": {"enabled": allow_localhost_api}}
    plugin._local_api_token = "local-service-token-with-at-least-32-bytes"
    plugin._auth = MagicMock()
    plugin._auth.validate_token.return_value = token_valid
    plugin._auth.password_change_required = False
    return plugin


async def _ok_handler(request):
    """Trivial handler that returns 200."""
    return aiohttp.web.Response(text="ok")


# ---------------------------------------------------------------------------
# Auth middleware factory
# ---------------------------------------------------------------------------


def _get_auth_middleware(plugin):
    from reticulumpi.builtin_plugins.web_dashboard.server import (
        auth_middleware_factory,
    )

    return auth_middleware_factory(plugin)


# ---------------------------------------------------------------------------
# Security headers middleware
# ---------------------------------------------------------------------------


def _get_security_middleware():
    from reticulumpi.builtin_plugins.web_dashboard.server import (
        security_headers_middleware,
    )

    return security_headers_middleware


# ===========================================================================
# 1. Public paths
# ===========================================================================


class TestPublicPaths:
    """Requests to public paths bypass auth entirely."""

    def test_login_html_is_public(self):
        plugin = _make_plugin(token_valid=False)
        mw = _get_auth_middleware(plugin)
        req = _make_request("GET", "/login.html")
        loop = asyncio.new_event_loop()
        resp = loop.run_until_complete(mw(req, _ok_handler))
        loop.close()
        assert resp.status == 200

    def test_api_auth_login_is_public(self):
        plugin = _make_plugin(token_valid=False)
        mw = _get_auth_middleware(plugin)
        req = _make_request("POST", "/api/auth/login")
        loop = asyncio.new_event_loop()
        resp = loop.run_until_complete(mw(req, _ok_handler))
        loop.close()
        assert resp.status == 200

    def test_auth_login_is_public(self):
        plugin = _make_plugin(token_valid=False)
        mw = _get_auth_middleware(plugin)
        req = _make_request("POST", "/auth/login")
        loop = asyncio.new_event_loop()
        resp = loop.run_until_complete(mw(req, _ok_handler))
        loop.close()
        assert resp.status == 200

    def test_api_version_is_public(self):
        plugin = _make_plugin(token_valid=False)
        mw = _get_auth_middleware(plugin)
        req = _make_request("GET", "/api/version")
        loop = asyncio.new_event_loop()
        resp = loop.run_until_complete(mw(req, _ok_handler))
        loop.close()
        assert resp.status == 200

    def test_static_prefix_is_public(self):
        plugin = _make_plugin(token_valid=False)
        mw = _get_auth_middleware(plugin)
        req = _make_request("GET", "/static/app.js")
        loop = asyncio.new_event_loop()
        resp = loop.run_until_complete(mw(req, _ok_handler))
        loop.close()
        assert resp.status == 200

    def test_non_public_path_requires_auth(self):
        plugin = _make_plugin(token_valid=False)
        mw = _get_auth_middleware(plugin)
        req = _make_request("GET", "/api/status")
        loop = asyncio.new_event_loop()
        with pytest.raises(aiohttp.web.HTTPUnauthorized):
            loop.run_until_complete(mw(req, _ok_handler))
        loop.close()


# ===========================================================================
# 2. Localhost bypass
# ===========================================================================


class TestLocalhostBypass:
    """The local API requires a scoped token and never blocks session auth."""

    def test_ipv4_localhost_get_bypasses_auth(self):
        plugin = _make_plugin(allow_localhost_api=True, token_valid=False)
        mw = _get_auth_middleware(plugin)
        req = _make_request(
            "GET",
            "/api/status",
            headers={"Authorization": "Bearer local-service-token-with-at-least-32-bytes"},
            peername=("127.0.0.1", 12345),
        )
        loop = asyncio.new_event_loop()
        resp = loop.run_until_complete(mw(req, _ok_handler))
        loop.close()
        assert resp.status == 200

    def test_ipv6_localhost_get_bypasses_auth(self):
        plugin = _make_plugin(allow_localhost_api=True, token_valid=False)
        mw = _get_auth_middleware(plugin)
        req = _make_request(
            "GET",
            "/api/status",
            headers={"Authorization": "Bearer local-service-token-with-at-least-32-bytes"},
            peername=("::1", 12345),
        )
        loop = asyncio.new_event_loop()
        resp = loop.run_until_complete(mw(req, _ok_handler))
        loop.close()
        assert resp.status == 200

    def test_localhost_bypass_disabled_requires_auth(self):
        plugin = _make_plugin(allow_localhost_api=False, token_valid=False)
        mw = _get_auth_middleware(plugin)
        req = _make_request("GET", "/api/status", peername=("127.0.0.1", 12345))
        loop = asyncio.new_event_loop()
        with pytest.raises(aiohttp.web.HTTPUnauthorized):
            loop.run_until_complete(mw(req, _ok_handler))
        loop.close()

    def test_non_localhost_not_bypassed(self):
        plugin = _make_plugin(allow_localhost_api=True, token_valid=False)
        mw = _get_auth_middleware(plugin)
        req = _make_request("GET", "/api/status", peername=("192.168.1.50", 12345))
        loop = asyncio.new_event_loop()
        with pytest.raises(aiohttp.web.HTTPUnauthorized):
            loop.run_until_complete(mw(req, _ok_handler))
        loop.close()

    def test_local_api_token_cannot_post(self):
        plugin = _make_plugin(allow_localhost_api=True, token_valid=False)
        mw = _get_auth_middleware(plugin)
        req = _make_request(
            "POST",
            "/api/data",
            headers={"Authorization": "Bearer local-service-token-with-at-least-32-bytes"},
            peername=("127.0.0.1", 12345),
        )
        loop = asyncio.new_event_loop()
        with pytest.raises(aiohttp.web.HTTPUnauthorized):
            loop.run_until_complete(mw(req, _ok_handler))
        loop.close()

    def test_authenticated_localhost_post_falls_through(self):
        plugin = _make_plugin(allow_localhost_api=True, token_valid=True)
        mw = _get_auth_middleware(plugin)
        req = _make_request(
            "POST",
            "/api/data",
            headers={
                "Authorization": "Bearer valid-session",
                "X-Requested-With": "XMLHttpRequest",
            },
            peername=("127.0.0.1", 12345),
        )
        loop = asyncio.new_event_loop()
        resp = loop.run_until_complete(mw(req, _ok_handler))
        loop.close()
        assert resp.status == 200

    def test_local_api_token_cannot_put(self):
        plugin = _make_plugin(allow_localhost_api=True, token_valid=False)
        mw = _get_auth_middleware(plugin)
        req = _make_request(
            "PUT",
            "/api/data",
            headers={"Authorization": "Bearer local-service-token-with-at-least-32-bytes"},
            peername=("::1", 12345),
        )
        loop = asyncio.new_event_loop()
        with pytest.raises(aiohttp.web.HTTPUnauthorized):
            loop.run_until_complete(mw(req, _ok_handler))
        loop.close()

    def test_local_api_token_cannot_delete(self):
        plugin = _make_plugin(allow_localhost_api=True, token_valid=False)
        mw = _get_auth_middleware(plugin)
        req = _make_request(
            "DELETE",
            "/api/data",
            headers={"Authorization": "Bearer local-service-token-with-at-least-32-bytes"},
            peername=("127.0.0.1", 12345),
        )
        loop = asyncio.new_event_loop()
        with pytest.raises(aiohttp.web.HTTPUnauthorized):
            loop.run_until_complete(mw(req, _ok_handler))
        loop.close()


# ===========================================================================
# 3. Token extraction
# ===========================================================================


class TestTokenExtraction:
    """Auth middleware extracts Bearer tokens and session cookies."""

    def test_bearer_token_accepted(self):
        plugin = _make_plugin(token_valid=True)
        mw = _get_auth_middleware(plugin)
        req = _make_request(
            "GET",
            "/api/status",
            headers={"Authorization": "Bearer valid-token-abc"},
        )
        loop = asyncio.new_event_loop()
        resp = loop.run_until_complete(mw(req, _ok_handler))
        loop.close()
        assert resp.status == 200
        plugin._auth.validate_token.assert_called_with("valid-token-abc")

    def test_session_cookie_accepted(self):
        plugin = _make_plugin(token_valid=True)
        mw = _get_auth_middleware(plugin)
        req = _make_request(
            "GET",
            "/api/status",
            headers={"Cookie": "session=cookie-token-xyz"},
        )
        loop = asyncio.new_event_loop()
        resp = loop.run_until_complete(mw(req, _ok_handler))
        loop.close()
        assert resp.status == 200
        plugin._auth.validate_token.assert_called_with("cookie-token-xyz")

    def test_invalid_token_rejected(self):
        plugin = _make_plugin(token_valid=False)
        mw = _get_auth_middleware(plugin)
        req = _make_request(
            "GET",
            "/api/status",
            headers={"Authorization": "Bearer bad-token"},
        )
        loop = asyncio.new_event_loop()
        with pytest.raises(aiohttp.web.HTTPUnauthorized):
            loop.run_until_complete(mw(req, _ok_handler))
        loop.close()

    def test_no_token_rejected(self):
        plugin = _make_plugin(token_valid=False)
        mw = _get_auth_middleware(plugin)
        req = _make_request("GET", "/api/status")
        loop = asyncio.new_event_loop()
        with pytest.raises(aiohttp.web.HTTPUnauthorized):
            loop.run_until_complete(mw(req, _ok_handler))
        loop.close()


# ===========================================================================
# 4. CSRF protection
# ===========================================================================


class TestCSRFProtection:
    """State-changing requests require X-Requested-With header."""

    def test_post_without_csrf_header_forbidden(self):
        plugin = _make_plugin(token_valid=True)
        mw = _get_auth_middleware(plugin)
        req = _make_request(
            "POST",
            "/api/data",
            headers={"Authorization": "Bearer good-token"},
        )
        loop = asyncio.new_event_loop()
        with pytest.raises(aiohttp.web.HTTPForbidden):
            loop.run_until_complete(mw(req, _ok_handler))
        loop.close()

    def test_put_without_csrf_header_forbidden(self):
        plugin = _make_plugin(token_valid=True)
        mw = _get_auth_middleware(plugin)
        req = _make_request(
            "PUT",
            "/api/data",
            headers={"Authorization": "Bearer good-token"},
        )
        loop = asyncio.new_event_loop()
        with pytest.raises(aiohttp.web.HTTPForbidden):
            loop.run_until_complete(mw(req, _ok_handler))
        loop.close()

    def test_delete_without_csrf_header_forbidden(self):
        plugin = _make_plugin(token_valid=True)
        mw = _get_auth_middleware(plugin)
        req = _make_request(
            "DELETE",
            "/api/data",
            headers={"Authorization": "Bearer good-token"},
        )
        loop = asyncio.new_event_loop()
        with pytest.raises(aiohttp.web.HTTPForbidden):
            loop.run_until_complete(mw(req, _ok_handler))
        loop.close()

    def test_get_does_not_require_csrf_header(self):
        plugin = _make_plugin(token_valid=True)
        mw = _get_auth_middleware(plugin)
        req = _make_request(
            "GET",
            "/api/status",
            headers={"Authorization": "Bearer good-token"},
        )
        loop = asyncio.new_event_loop()
        resp = loop.run_until_complete(mw(req, _ok_handler))
        loop.close()
        assert resp.status == 200

    def test_any_csrf_header_value_accepted(self):
        plugin = _make_plugin(token_valid=True)
        mw = _get_auth_middleware(plugin)
        req = _make_request(
            "POST",
            "/api/data",
            headers={
                "Authorization": "Bearer good-token",
                "X-Requested-With": "custom-value",
            },
        )
        loop = asyncio.new_event_loop()
        resp = loop.run_until_complete(mw(req, _ok_handler))
        loop.close()
        assert resp.status == 200


# ===========================================================================
# 5. Unauthorized responses
# ===========================================================================


class TestUnauthorizedResponses:
    """Browser vs API clients get different unauthorized responses."""

    def test_browser_gets_redirect(self):
        plugin = _make_plugin(token_valid=False)
        mw = _get_auth_middleware(plugin)
        req = _make_request(
            "GET",
            "/api/status",
            headers={"Accept": "text/html"},
        )
        loop = asyncio.new_event_loop()
        with pytest.raises(aiohttp.web.HTTPFound) as exc_info:
            loop.run_until_complete(mw(req, _ok_handler))
        loop.close()
        assert exc_info.value.location == "/login.html"

    def test_api_gets_401_json(self):
        plugin = _make_plugin(token_valid=False)
        mw = _get_auth_middleware(plugin)
        req = _make_request(
            "GET",
            "/api/status",
            headers={"Accept": "application/json"},
        )
        loop = asyncio.new_event_loop()
        with pytest.raises(aiohttp.web.HTTPUnauthorized) as exc_info:
            loop.run_until_complete(mw(req, _ok_handler))
        loop.close()
        assert exc_info.value.content_type == "application/json"
        assert "Authentication required" in exc_info.value.text

    def test_no_accept_header_gets_401_json(self):
        """Requests without Accept: text/html get JSON 401, not a redirect."""
        plugin = _make_plugin(token_valid=False)
        mw = _get_auth_middleware(plugin)
        req = _make_request("GET", "/api/status")
        loop = asyncio.new_event_loop()
        with pytest.raises(aiohttp.web.HTTPUnauthorized):
            loop.run_until_complete(mw(req, _ok_handler))
        loop.close()


# ===========================================================================
# 6. Security headers
# ===========================================================================


class TestSecurityHeaders:
    """Security headers middleware adds hardening headers to all responses."""

    def test_standard_headers_on_public_path(self):
        mw = _get_security_middleware()
        req = _make_request("GET", "/login.html")
        loop = asyncio.new_event_loop()
        resp = loop.run_until_complete(mw(req, _ok_handler))
        loop.close()
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        csp = resp.headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp
        assert "script-src-attr 'none'" in csp
        assert "style-src 'self'" in csp
        assert "style-src-elem 'self'" in csp
        assert "style-src-attr 'none'" in csp
        assert "'unsafe-inline'" not in csp
        assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"

    def test_api_version_on_api_path(self):
        mw = _get_security_middleware()
        req = _make_request("GET", "/api/version")
        loop = asyncio.new_event_loop()
        resp = loop.run_until_complete(mw(req, _ok_handler))
        loop.close()
        assert resp.headers["Api-Version"] == "1.1"

    def test_no_api_version_on_non_api_path(self):
        mw = _get_security_middleware()
        req = _make_request("GET", "/login.html")
        loop = asyncio.new_event_loop()
        resp = loop.run_until_complete(mw(req, _ok_handler))
        loop.close()
        assert "Api-Version" not in resp.headers

    def test_headers_present_on_authenticated_api_path(self):
        mw = _get_security_middleware()
        req = _make_request("GET", "/api/status")
        loop = asyncio.new_event_loop()
        resp = loop.run_until_complete(mw(req, _ok_handler))
        loop.close()
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert "Api-Version" in resp.headers

    def test_hsts_trusts_https_header_only_from_configured_proxy(self, monkeypatch):
        from reticulumpi.builtin_plugins.web_dashboard import server

        plugin = SimpleNamespace(
            config={
                "reverse_proxy": {
                    "enabled": True,
                    "trusted_networks": ["127.0.0.1/32"],
                }
            }
        )
        monkeypatch.setattr(server, "get_app_plugin", lambda _app: plugin)
        trusted = _make_request(
            "GET",
            "/login.html",
            headers={"X-Forwarded-Proto": "https"},
            peername=("127.0.0.1", 43100),
        )
        response = aiohttp.web.Response(text="ok")

        server._apply_security_headers(trusted, response)

        assert response.headers["Strict-Transport-Security"] == "max-age=31536000"

        untrusted = _make_request(
            "GET",
            "/login.html",
            headers={"X-Forwarded-Proto": "https"},
            peername=("192.0.2.10", 43100),
        )
        response = aiohttp.web.Response(text="ok")
        server._apply_security_headers(untrusted, response)
        assert "Strict-Transport-Security" not in response.headers

    def test_ambiguous_forwarded_proto_fails_closed(self):
        from reticulumpi.builtin_plugins.web_dashboard.server import _request_is_secure

        plugin = SimpleNamespace(
            config={
                "reverse_proxy": {
                    "enabled": True,
                    "trusted_networks": ["127.0.0.1/32"],
                }
            }
        )
        request = _make_request(
            "GET",
            "/login.html",
            headers={"X-Forwarded-Proto": "http, https"},
            peername=("127.0.0.1", 43100),
        )
        assert _request_is_secure(request, plugin) is False
