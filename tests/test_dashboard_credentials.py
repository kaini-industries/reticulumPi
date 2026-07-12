"""Managed dashboard credential persistence and bootstrap-change contracts."""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace

import aiohttp.web
import pytest
from aiohttp.test_utils import TestClient, TestServer

from reticulumpi.builtin_plugins.web_dashboard import auth as auth_module
from reticulumpi.builtin_plugins.web_dashboard.api import (
    handle_change_password,
    handle_login,
)
from reticulumpi.builtin_plugins.web_dashboard.auth import (
    AuthManager,
    hash_password,
    verify_password,
    write_secret_file_atomic,
)
from reticulumpi.builtin_plugins.web_dashboard.keys import PLUGIN_KEY
from reticulumpi.builtin_plugins.web_dashboard.server import (
    auth_middleware_factory,
    security_headers_middleware,
)


def _managed_auth(tmp_path, password="temporary-bootstrap-password"):
    hash_file = tmp_path / "dashboard_secret"
    bootstrap_file = tmp_path / "dashboard_password.txt"
    password_hash = hash_password(password)
    write_secret_file_atomic(str(hash_file), password_hash + "\n")
    write_secret_file_atomic(str(bootstrap_file), password + "\n")
    manager = AuthManager(
        password_hash=password_hash,
        generated_pw_file=str(bootstrap_file),
        password_hash_file=str(hash_file),
    )
    return manager, hash_file, bootstrap_file


def test_atomic_secret_write_replaces_content_with_mode_0600(tmp_path):
    path = tmp_path / "secret"
    write_secret_file_atomic(str(path), "first\n")
    write_secret_file_atomic(str(path), "second\n")

    assert path.read_text() == "second\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".secret.*"))


def test_bootstrap_file_survives_login_and_is_flagged(tmp_path):
    manager, _, bootstrap_file = _managed_auth(tmp_path)

    token = manager.login("temporary-bootstrap-password", "127.0.0.1")

    assert token
    assert bootstrap_file.is_file()
    assert manager.password_change_required is True


def test_successful_password_change_is_durable_and_revokes_sessions(tmp_path):
    manager, hash_file, bootstrap_file = _managed_auth(tmp_path)
    first = manager.login("temporary-bootstrap-password", "127.0.0.1")
    second = manager.login("temporary-bootstrap-password", "192.0.2.10")

    result = manager.change_password(
        "temporary-bootstrap-password",
        "new-long-dashboard-password",
    )

    assert result.applied is True
    assert result.reason == "password_changed"
    assert set(result.revoked_tokens) == {first, second}
    assert len(manager.sessions) == 0
    assert manager.password_change_required is False
    assert not bootstrap_file.exists()
    assert hash_file.stat().st_mode & 0o777 == 0o600
    assert verify_password("new-long-dashboard-password", hash_file.read_text().strip())
    assert not verify_password("temporary-bootstrap-password", hash_file.read_text().strip())


def test_failed_password_persistence_preserves_current_credential(tmp_path, monkeypatch):
    manager, hash_file, bootstrap_file = _managed_auth(tmp_path)
    original_hash = hash_file.read_text()
    token = manager.login("temporary-bootstrap-password", "127.0.0.1")

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(auth_module, "write_secret_file_atomic", fail_write)
    result = manager.change_password(
        "temporary-bootstrap-password",
        "new-long-dashboard-password",
    )

    assert result.applied is False
    assert result.reason == "persistence_failed"
    assert hash_file.read_text() == original_hash
    assert bootstrap_file.exists()
    assert manager.validate_token(token)


def test_password_change_serializes_with_inflight_login(tmp_path, monkeypatch):
    manager, _, _ = _managed_auth(tmp_path)
    entered_verify = threading.Event()
    release_verify = threading.Event()
    original_verify = auth_module.verify_password
    login_token = []
    change_result = []

    def delayed_login_verify(password, stored_hash):
        if threading.current_thread().name == "credential-login":
            entered_verify.set()
            assert release_verify.wait(5)
        return original_verify(password, stored_hash)

    monkeypatch.setattr(auth_module, "verify_password", delayed_login_verify)
    login_thread = threading.Thread(
        target=lambda: login_token.append(
            manager.login("temporary-bootstrap-password", "127.0.0.1")
        ),
        name="credential-login",
    )
    change_thread = threading.Thread(
        target=lambda: change_result.append(
            manager.change_password(
                "temporary-bootstrap-password",
                "new-long-dashboard-password",
            )
        ),
        name="credential-change",
    )

    login_thread.start()
    assert entered_verify.wait(5)
    change_thread.start()
    release_verify.set()
    login_thread.join(5)
    change_thread.join(5)

    assert not login_thread.is_alive()
    assert not change_thread.is_alive()
    assert login_token[0] in change_result[0].revoked_tokens
    assert len(manager.sessions) == 0
    assert not manager.validate_token(login_token[0])


def test_externally_managed_password_cannot_be_silently_rewritten():
    manager = AuthManager(plaintext_password="configured-password")

    result = manager.change_password("configured-password", "new-long-dashboard-password")

    assert result.applied is False
    assert result.reason == "password_managed_externally"


@pytest.mark.asyncio
async def test_bootstrap_session_is_limited_until_password_change(tmp_path):
    manager, hash_file, bootstrap_file = _managed_auth(tmp_path)
    plugin = SimpleNamespace(
        _auth=manager,
        config={"ssl": {}, "local_api": {"enabled": False}},
    )
    app = aiohttp.web.Application(
        middlewares=[security_headers_middleware, auth_middleware_factory(plugin)]
    )
    app[PLUGIN_KEY] = plugin
    app.router.add_post("/api/auth/login", handle_login)
    app.router.add_post("/api/auth/password", handle_change_password)

    async def handle_status(_request):
        return aiohttp.web.json_response({"ok": True})

    app.router.add_get("/api/status", handle_status)

    async with TestClient(TestServer(app)) as client:
        login = await client.post(
            "/api/auth/login",
            json={"password": "temporary-bootstrap-password"},
        )
        assert login.status == 200
        login_data = await login.json()
        assert login_data["data"]["password_change_required"] is True
        token = login.cookies["session"].value
        session_headers = {
            "Cookie": f"session={token}",
            "X-Requested-With": "XMLHttpRequest",
        }

        blocked = await client.get("/api/status", headers=session_headers)
        assert blocked.status == 428
        assert (await blocked.json())["password_change_required"] is True

        changed = await client.post(
            "/api/auth/password",
            headers=session_headers,
            json={
                "current_password": "temporary-bootstrap-password",
                "new_password": "new-long-dashboard-password",
            },
        )
        assert changed.status == 200
        assert (await changed.json())["data"]["password_change_required"] is False

    assert not bootstrap_file.exists()
    assert verify_password("new-long-dashboard-password", hash_file.read_text().strip())
    assert os.stat(hash_file).st_mode & 0o777 == 0o600
