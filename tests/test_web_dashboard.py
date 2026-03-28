"""Tests for the Web Dashboard plugin."""

import time
from unittest.mock import MagicMock

import pytest


# --- Auth module tests ---


class TestPasswordHashing:
    def test_hash_and_verify(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import (
            hash_password,
            verify_password,
        )

        pw = "test-password-123"
        hashed = hash_password(pw)
        assert hashed.startswith("scrypt:")
        assert len(hashed.split(":")) == 3
        assert verify_password(pw, hashed)

    def test_wrong_password_fails(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import (
            hash_password,
            verify_password,
        )

        hashed = hash_password("correct")
        assert not verify_password("wrong", hashed)

    def test_invalid_hash_format_fails(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import verify_password

        assert not verify_password("pw", "invalid")
        assert not verify_password("pw", "scrypt:bad")
        assert not verify_password("pw", "md5:aabb:ccdd")

    def test_different_salts_produce_different_hashes(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import hash_password

        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2  # Different salts


class TestAutoGeneratePassword:
    def test_generates_new_password_and_saves_hash(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.auth import (
            load_or_create_password_hash,
            verify_password,
        )

        pw_hash, password = load_or_create_password_hash(str(tmp_path))
        assert password is not None
        assert len(password) > 10
        assert pw_hash.startswith("scrypt:")
        assert verify_password(password, pw_hash)

        # File should exist
        secret_file = tmp_path / "dashboard_secret"
        assert secret_file.exists()
        assert secret_file.read_text().strip() == pw_hash

    def test_loads_existing_hash(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.auth import (
            hash_password,
            load_or_create_password_hash,
        )

        # Pre-create the file
        existing_hash = hash_password("existing_pw")
        secret_file = tmp_path / "dashboard_secret"
        secret_file.write_text(existing_hash + "\n")

        pw_hash, password = load_or_create_password_hash(str(tmp_path))
        assert password is None  # Not generated
        assert pw_hash == existing_hash

    def test_regenerates_if_file_empty(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.auth import (
            load_or_create_password_hash,
        )

        secret_file = tmp_path / "dashboard_secret"
        secret_file.write_text("")

        pw_hash, password = load_or_create_password_hash(str(tmp_path))
        assert password is not None
        assert pw_hash.startswith("scrypt:")


class TestRateLimiter:
    def test_allows_initial_attempts(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import RateLimiter

        rl = RateLimiter(max_attempts=3, window_seconds=60)
        assert rl.is_allowed("1.2.3.4")

    def test_blocks_after_max_attempts(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import RateLimiter

        rl = RateLimiter(max_attempts=2, window_seconds=60)
        rl.record_attempt("1.2.3.4")
        rl.record_attempt("1.2.3.4")
        assert not rl.is_allowed("1.2.3.4")

    def test_different_ips_independent(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import RateLimiter

        rl = RateLimiter(max_attempts=1, window_seconds=60)
        rl.record_attempt("1.1.1.1")
        assert not rl.is_allowed("1.1.1.1")
        assert rl.is_allowed("2.2.2.2")

    def test_retry_after_positive(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import RateLimiter

        rl = RateLimiter(max_attempts=1, window_seconds=60)
        rl.record_attempt("1.1.1.1")
        assert rl.retry_after("1.1.1.1") > 0


class TestAuthManager:
    def _make_manager(self, password="testpass"):
        from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager

        return AuthManager(plaintext_password=password, max_sessions=3)

    def test_login_success(self):
        mgr = self._make_manager()
        token = mgr.login("testpass", "127.0.0.1")
        assert token is not None
        assert len(token) == 64  # 32 bytes hex

    def test_login_wrong_password(self):
        mgr = self._make_manager()
        token = mgr.login("wrong", "127.0.0.1")
        assert token is None

    def test_validate_token(self):
        mgr = self._make_manager()
        token = mgr.login("testpass", "127.0.0.1")
        assert mgr.validate_token(token)

    def test_validate_invalid_token(self):
        mgr = self._make_manager()
        assert not mgr.validate_token("nonexistent")

    def test_logout_invalidates_token(self):
        mgr = self._make_manager()
        token = mgr.login("testpass", "127.0.0.1")
        mgr.logout(token)
        assert not mgr.validate_token(token)

    def test_session_eviction(self):
        mgr = self._make_manager()
        tokens = []
        for i in range(4):
            t = mgr.login("testpass", f"10.0.0.{i}")
            tokens.append(t)
            time.sleep(0.01)  # Ensure different last_seen

        # max_sessions=3, so first token should have been evicted
        assert not mgr.validate_token(tokens[0])
        assert mgr.validate_token(tokens[-1])

    def test_expired_session(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager

        mgr = AuthManager(plaintext_password="test", session_timeout=0.01)
        token = mgr.login("test", "127.0.0.1")
        time.sleep(0.05)
        assert not mgr.validate_token(token)

    def test_rate_limiting(self):
        mgr = self._make_manager()
        # Fail 5 times
        for _ in range(5):
            mgr.login("wrong", "10.0.0.1")
        # Should be rate limited
        assert mgr.is_rate_limited("10.0.0.1")
        # Even correct password blocked
        token = mgr.login("testpass", "10.0.0.1")
        assert token is None

    def test_requires_password(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager

        with pytest.raises(ValueError, match="No password"):
            AuthManager()


# --- Plugin config validation tests ---


class TestPluginValidation:
    def _make_plugin(self, mock_app, config):
        from reticulumpi.builtin_plugins.web_dashboard.plugin import WebDashboardPlugin

        return WebDashboardPlugin(mock_app, config)

    def test_valid_config(self, mock_app):
        config = {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 8080,
            "password_hash": "scrypt:aa:bb",
        }
        plugin = self._make_plugin(mock_app, config)
        assert plugin.plugin_name == "web_dashboard"

    def test_accepts_plaintext_password(self, mock_app):
        config = {"enabled": True, "password": "test123"}
        plugin = self._make_plugin(mock_app, config)
        assert plugin.plugin_name == "web_dashboard"

    def test_accepts_no_password_for_auto_generation(self, mock_app):
        config = {"enabled": True}
        plugin = self._make_plugin(mock_app, config)
        assert plugin.plugin_name == "web_dashboard"

    def test_rejects_invalid_port(self, mock_app):
        config = {"enabled": True, "password": "test", "port": 99999}
        with pytest.raises(ValueError, match="port"):
            self._make_plugin(mock_app, config)

    def test_rejects_zero_port(self, mock_app):
        config = {"enabled": True, "password": "test", "port": 0}
        with pytest.raises(ValueError, match="port"):
            self._make_plugin(mock_app, config)

    def test_rejects_low_session_timeout(self, mock_app):
        config = {"enabled": True, "password": "test", "session_timeout": 10}
        with pytest.raises(ValueError, match="session_timeout"):
            self._make_plugin(mock_app, config)

    def test_rejects_zero_max_sessions(self, mock_app):
        config = {"enabled": True, "password": "test", "max_sessions": 0}
        with pytest.raises(ValueError, match="max_sessions"):
            self._make_plugin(mock_app, config)

    def test_rejects_low_metrics_interval(self, mock_app):
        config = {"enabled": True, "password": "test", "metrics_interval": 0.5}
        with pytest.raises(ValueError, match="metrics_interval"):
            self._make_plugin(mock_app, config)


# --- API response tests (mocked app) ---


@pytest.fixture
def dashboard_app(mock_app):
    """Create a mock app with system_monitor plugin for API testing."""
    monitor = MagicMock()
    monitor.latest_metrics = {
        "cpu_percent": 15.2,
        "cpu_temp": 42.1,
        "memory_percent": 35.8,
        "disk_percent": 22.3,
        "timestamp": 1711500000.0,
    }

    mock_app.get_plugin = MagicMock(side_effect=lambda name: monitor if name == "system_monitor" else None)
    mock_app.get_status.return_value = {
        "version": "0.1.2",
        "plugins": {"system_monitor": {"active": True}},
        "failed_plugins": [],
    }
    mock_app.config = MagicMock()
    mock_app.config.node_name = "TestNode"
    mock_app.config.log_level = 4
    mock_app.config.use_shared_instance = True
    mock_app.config.plugin_paths = []
    mock_app.config.plugins = {
        "system_monitor": {"enabled": True},
        "web_dashboard": {"enabled": True, "password_hash": "scrypt:aa:bb"},
    }
    mock_app._get_version = MagicMock(return_value="0.1.2")
    mock_app._failed_plugins = []
    return mock_app


@pytest.fixture
def dashboard_plugin(dashboard_app):
    """Create a WebDashboardPlugin instance without starting the server."""
    from reticulumpi.builtin_plugins.web_dashboard.plugin import WebDashboardPlugin

    config = {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8080,
        "password": "testpass",
        "session_timeout": 86400,
        "max_sessions": 5,
        "metrics_interval": 5,
        "max_websocket_clients": 10,
    }
    plugin = WebDashboardPlugin(dashboard_app, config)
    plugin._start_time = time.time()
    plugin._active = True

    from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager

    plugin._auth = AuthManager(plaintext_password="testpass")
    return plugin


@pytest.fixture
def aiohttp_app(dashboard_plugin):
    """Create the aiohttp Application for testing."""
    from reticulumpi.builtin_plugins.web_dashboard.server import create_app

    return create_app(dashboard_plugin)


class TestAPIEndpoints:
    """Test API handlers using aiohttp test client."""

    @pytest.fixture
    def client(self, aiohttp_app, event_loop):
        """Create an aiohttp test client."""
        pytest.importorskip("aiohttp")
        from aiohttp.test_utils import TestClient, TestServer

        async def _make():
            server = TestServer(aiohttp_app)
            client = TestClient(server)
            await client.start_server()
            return client

        client = event_loop.run_until_complete(_make())
        yield client
        event_loop.run_until_complete(client.close())

    @pytest.fixture
    def event_loop(self):
        import asyncio
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    def _login(self, client, event_loop):
        async def _do():
            resp = await client.post("/api/auth/login", json={"password": "testpass"})
            data = await resp.json()
            return data["data"]["token"]
        return event_loop.run_until_complete(_do())

    def test_login_success(self, client, event_loop):
        async def _do():
            resp = await client.post("/api/auth/login", json={"password": "testpass"})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert "token" in data["data"]
        event_loop.run_until_complete(_do())

    def test_login_wrong_password(self, client, event_loop):
        async def _do():
            resp = await client.post("/api/auth/login", json={"password": "wrong"})
            assert resp.status == 401
            data = await resp.json()
            assert data["ok"] is False
        event_loop.run_until_complete(_do())

    def test_unauthenticated_api_returns_401(self, client, event_loop):
        async def _do():
            resp = await client.get("/api/status", headers={"Accept": "application/json"})
            assert resp.status == 401
        event_loop.run_until_complete(_do())

    def test_status_endpoint(self, client, event_loop):
        token = self._login(client, event_loop)

        async def _do():
            resp = await client.get(
                "/api/status",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert "version" in data["data"]
        event_loop.run_until_complete(_do())

    def test_node_endpoint(self, client, event_loop):
        token = self._login(client, event_loop)

        async def _do():
            resp = await client.get(
                "/api/node",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["data"]["node_name"] == "TestNode"
        event_loop.run_until_complete(_do())

    def test_metrics_endpoint(self, client, event_loop):
        token = self._login(client, event_loop)

        async def _do():
            resp = await client.get(
                "/api/metrics",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["data"]["cpu_percent"] == 15.2
        event_loop.run_until_complete(_do())

    def test_plugins_endpoint(self, client, event_loop):
        token = self._login(client, event_loop)

        async def _do():
            resp = await client.get(
                "/api/plugins",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert "plugins" in data["data"]
        event_loop.run_until_complete(_do())

    def test_config_endpoint_strips_sensitive_keys(self, client, event_loop):
        token = self._login(client, event_loop)

        async def _do():
            resp = await client.get(
                "/api/config",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 200
            data = await resp.json()
            # Check that password_hash is stripped from plugins
            for name, cfg in data["data"]["plugins"].items():
                assert "password_hash" not in cfg
                assert "password" not in cfg
        event_loop.run_until_complete(_do())

    def test_logout(self, client, event_loop):
        token = self._login(client, event_loop)

        async def _do():
            # Logout
            resp = await client.post(
                "/api/auth/logout",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 200

            # Token should now be invalid
            resp = await client.get(
                "/api/status",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            assert resp.status == 401
        event_loop.run_until_complete(_do())

    def test_security_headers(self, client, event_loop):
        async def _do():
            resp = await client.get("/login.html")
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"
            assert resp.headers.get("X-Frame-Options") == "DENY"
            assert "Content-Security-Policy" in resp.headers
        event_loop.run_until_complete(_do())

    def test_rate_limiting(self, client, event_loop):
        async def _do():
            # Send 5 wrong login attempts
            for _ in range(5):
                await client.post("/api/auth/login", json={"password": "wrong"})

            # 6th attempt should be rate limited
            resp = await client.post("/api/auth/login", json={"password": "wrong"})
            assert resp.status == 429
            assert "Retry-After" in resp.headers
        event_loop.run_until_complete(_do())


class TestFormLogin:
    """Test form-based login flow (POST /auth/login -> 302 redirect)."""

    @pytest.fixture
    def client(self, aiohttp_app, event_loop):
        pytest.importorskip("aiohttp")
        from aiohttp.test_utils import TestClient, TestServer

        async def _make():
            server = TestServer(aiohttp_app)
            client = TestClient(server)
            await client.start_server()
            return client

        client = event_loop.run_until_complete(_make())
        yield client
        event_loop.run_until_complete(client.close())

    @pytest.fixture
    def event_loop(self):
        import asyncio
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    def test_form_login_redirects_on_success(self, client, event_loop):
        async def _do():
            resp = await client.post(
                "/auth/login",
                data={"password": "testpass"},
                allow_redirects=False,
            )
            assert resp.status == 302
            assert resp.headers.get("Location") == "/"
            assert "session=" in resp.headers.get("Set-Cookie", "")
        event_loop.run_until_complete(_do())

    def test_form_login_redirects_on_wrong_password(self, client, event_loop):
        async def _do():
            resp = await client.post(
                "/auth/login",
                data={"password": "wrong"},
                allow_redirects=False,
            )
            assert resp.status == 302
            assert "error=invalid" in resp.headers.get("Location", "")
        event_loop.run_until_complete(_do())

    def test_form_login_cookie_grants_access(self, client, event_loop):
        async def _do():
            # Login via form (client follows redirects and stores cookies)
            resp = await client.post(
                "/auth/login",
                data={"password": "testpass"},
            )
            # Should have followed redirect to / and gotten 200
            assert resp.status == 200

            # Subsequent API calls should work via cookie
            resp = await client.get("/api/node")
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
        event_loop.run_until_complete(_do())


class TestGetStatus:
    def test_status_fields(self, dashboard_plugin):
        status = dashboard_plugin.get_status()
        assert status["active"] is True
        assert status["host"] == "127.0.0.1"
        assert status["port"] == 8080
        assert "web_url" in status
        assert status["web_url"] == "http://127.0.0.1:8080"
        assert "uptime" in status
        assert status["active_sessions"] == 0
