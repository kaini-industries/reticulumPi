"""Failure-oriented authentication and credential durability regressions."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp.web
import pytest

from reticulumpi.builtin_plugins.web_dashboard import auth as auth_module
from reticulumpi.builtin_plugins.web_dashboard import websocket_handler as wsh
from reticulumpi.builtin_plugins.web_dashboard.api import (
    _scrub_sensitive,
    _run_auth_work,
    handle_change_password,
    handle_login,
)
from reticulumpi.builtin_plugins.web_dashboard.auth import (
    AuthManager,
    RateLimiter,
    SqliteSessionStore,
    hash_password,
    load_or_create_password_hash,
    verify_password,
    write_secret_file_atomic,
)
from reticulumpi.builtin_plugins.web_dashboard.keys import LOCAL_API_KEY
from reticulumpi.builtin_plugins.web_dashboard.plugin import WebDashboardPlugin
from reticulumpi.builtin_plugins.web_dashboard.server import (
    LOCALHOST_ALLOWED_PATHS,
    auth_middleware_factory,
)
from reticulumpi.runtime_metrics import get_runtime_metrics


PASSWORD = "correct-dashboard-password"


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password(PASSWORD)


def test_config_scrubber_redacts_nested_and_variant_secret_keys():
    scrubbed = _scrub_sensitive(
        {
            "mqtt": {
                "channel_key": "AQ-secret",
                "PASSWORD": "broker-secret",
                "broker": "mqtt.example",
            },
            "mesh": {"psk-value": "mesh-secret", "keyboard_layout": "us"},
            "serviceToken": "token-secret",
        }
    )

    assert scrubbed["mqtt"] == {
        "channel_key": "***",
        "PASSWORD": "***",
        "broker": "mqtt.example",
    }
    assert scrubbed["mesh"] == {"psk-value": "***", "keyboard_layout": "us"}
    assert scrubbed["serviceToken"] == "***"


class _JsonRequest:
    def __init__(self, plugin, payload: object):
        self.app = {"plugin": plugin}
        self.remote = "127.0.0.1"
        self.scheme = "http"
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self._payload = payload

    async def json(self) -> object:
        return self._payload


class _MiddlewareRequest(dict):
    def __init__(
        self,
        path: str,
        token: str,
        *,
        method: str = "GET",
        remote: str = "127.0.0.1",
    ):
        super().__init__()
        self.path = path
        self.method = method
        self.remote = remote
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self.cookies: dict[str, str] = {}


def _plugin_for_token_tests(password_hash: str) -> WebDashboardPlugin:
    app = MagicMock()
    return WebDashboardPlugin(
        app,
        {
            "enabled": True,
            "password_hash": password_hash,
            "local_api": {"enabled": False},
        },
    )


def test_legacy_password_hash_and_corrupt_hashes_fail_safely(caplog):
    salt = bytes.fromhex("00112233445566778899aabbccddeeff")
    expected = hashlib.scrypt(PASSWORD.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    legacy_hash = f"scrypt:{salt.hex()}:{expected.hex()}"

    assert verify_password(PASSWORD, legacy_hash)
    assert not verify_password(PASSWORD, "scrypt:not-hex:16384:8:2:not-hex")
    assert not verify_password(
        PASSWORD,
        f"scrypt:{salt.hex()}:{2**15}:8:2:{expected.hex()}",
    )
    assert "verify_password failed" in caplog.text


def test_atomic_secret_short_write_preserves_target_and_removes_temp(tmp_path, monkeypatch):
    target = tmp_path / "dashboard_secret"
    write_secret_file_atomic(str(target), "old-value\n")
    monkeypatch.setattr(auth_module.os, "write", lambda *_args: 0)

    with pytest.raises(OSError, match="short write"):
        write_secret_file_atomic(str(target), "new-value\n")

    assert target.read_text() == "old-value\n"
    assert not list(tmp_path.glob(".dashboard_secret.*"))


def test_atomic_secret_cleanup_tolerates_already_missing_temp(tmp_path, monkeypatch):
    real_unlink = os.unlink

    def unlink_then_report_missing(path):
        real_unlink(path)
        raise FileNotFoundError(path)

    monkeypatch.setattr(auth_module.os, "write", lambda *_args: 0)
    monkeypatch.setattr(auth_module.os, "unlink", unlink_then_report_missing)

    with pytest.raises(OSError, match="short write"):
        write_secret_file_atomic(str(tmp_path / "secret"), "value")

    assert not list(tmp_path.glob(".secret.*"))


def test_missing_bootstrap_file_unlink_is_idempotent(tmp_path):
    auth_module._unlink_secret_file_durably(str(tmp_path / "already-removed"))


def test_load_password_hash_repairs_mode_and_empty_file_is_replaced(tmp_path):
    existing_dir = tmp_path / "existing"
    existing_dir.mkdir()
    existing_path = existing_dir / auth_module.SECRET_FILENAME
    existing_hash = hash_password("existing-password")
    existing_path.write_text(existing_hash + "\n")
    existing_path.chmod(0o644)

    loaded_hash, generated = load_or_create_password_hash(str(existing_dir))

    assert loaded_hash == existing_hash
    assert generated is None
    assert existing_path.stat().st_mode & 0o777 == 0o600

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    (empty_dir / auth_module.SECRET_FILENAME).touch()
    replacement_hash, replacement_password = load_or_create_password_hash(str(empty_dir))

    assert replacement_password
    assert verify_password(replacement_password, replacement_hash)
    assert (empty_dir / auth_module.SECRET_FILENAME).stat().st_mode & 0o777 == 0o600


def test_generated_credentials_are_durable_private_and_match(tmp_path):
    password_hash, generated_password = load_or_create_password_hash(str(tmp_path))

    assert generated_password is not None
    assert verify_password(generated_password, password_hash)
    hash_path = tmp_path / auth_module.SECRET_FILENAME
    bootstrap_path = tmp_path / auth_module.BOOTSTRAP_FILENAME
    assert hash_path.read_text().strip() == password_hash
    assert bootstrap_path.read_text().strip() == generated_password
    assert hash_path.stat().st_mode & 0o777 == 0o600
    assert bootstrap_path.stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_password_generation_recovers_when_hash_publish_fails(tmp_path, monkeypatch):
    real_write = auth_module.write_secret_file_atomic

    def fail_hash_publish(path: str, value: str) -> None:
        if os.path.basename(path) == auth_module.SECRET_FILENAME:
            raise OSError("injected hash publish failure")
        real_write(path, value)

    monkeypatch.setattr(auth_module, "write_secret_file_atomic", fail_hash_publish)
    with pytest.raises(OSError, match="injected hash publish failure"):
        load_or_create_password_hash(str(tmp_path))

    bootstrap_path = tmp_path / auth_module.BOOTSTRAP_FILENAME
    bootstrap_password = bootstrap_path.read_text().strip()
    assert bootstrap_password
    assert not (tmp_path / auth_module.SECRET_FILENAME).exists()

    monkeypatch.setattr(auth_module, "write_secret_file_atomic", real_write)
    recovered_hash, generated_password = load_or_create_password_hash(str(tmp_path))

    assert generated_password is None
    assert verify_password(bootstrap_password, recovered_hash)
    assert (tmp_path / auth_module.SECRET_FILENAME).read_text().strip() == recovered_hash


@pytest.mark.parametrize("filename", [auth_module.SECRET_FILENAME, auth_module.BOOTSTRAP_FILENAME])
def test_password_credentials_reject_symlinks(tmp_path, filename):
    external = tmp_path / "external"
    external.write_text("attacker-controlled")
    credential = tmp_path / filename
    credential.symlink_to(external)

    with pytest.raises(OSError):
        load_or_create_password_hash(str(tmp_path))

    assert external.read_text() == "attacker-controlled"


def test_concurrent_password_generation_returns_one_credential(tmp_path):
    barrier = threading.Barrier(8)

    def load_credentials() -> tuple[str, str | None]:
        barrier.wait()
        return load_or_create_password_hash(str(tmp_path))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _index: load_credentials(), range(8)))

    hashes = {password_hash for password_hash, _password in results}
    generated_passwords = [password for _hash, password in results if password is not None]
    assert len(hashes) == 1
    assert len(generated_passwords) == 1
    assert verify_password(generated_passwords[0], hashes.pop())


def test_rate_limiter_cleanup_covers_expired_and_over_capacity_entries(monkeypatch):
    limiter = RateLimiter(max_attempts=2, window_seconds=60)
    assert limiter._effective_window("untracked") == 60
    now = time.monotonic()
    limiter.MAX_TRACKED_IPS = 2
    limiter._state = {
        "192.0.2.1": {"attempts": [now - 1], "consecutive_failures": 0},
        "192.0.2.2": {"attempts": [now - 2], "consecutive_failures": 0},
        "192.0.2.3": {"attempts": [now - 3], "consecutive_failures": 0},
    }

    assert limiter.cleanup_all_expired() == 1
    assert set(limiter._state) == {"192.0.2.1", "192.0.2.2"}
    assert limiter.retry_after("198.51.100.1") == 0

    limiter._state["198.51.100.2"] = {
        "attempts": [now - 1_000],
        "consecutive_failures": 0,
    }
    monkeypatch.setattr(auth_module.time, "monotonic", lambda: now)
    assert limiter.is_allowed("198.51.100.2")
    assert "198.51.100.2" not in limiter._state


def test_sqlite_store_migrates_legacy_schema_and_closed_operations_are_safe(tmp_path):
    db_path = tmp_path / "sessions.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("CREATE TABLE sessions (token TEXT PRIMARY KEY, data TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO sessions (token, data) VALUES (?, ?)",
            ("legacy", json.dumps({"last_seen": 1.0})),
        )

    store = SqliteSessionStore(str(db_path))
    with closing(sqlite3.connect(db_path)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
    assert "expires_at" in columns
    assert store["legacy"]["last_seen"] == 1.0
    with pytest.raises(KeyError):
        store.pop("missing")

    store.close()
    store.close()
    assert "legacy" not in store
    assert len(store) == 0
    assert list(store) == []
    assert store.items() == []
    assert store.cleanup_expired() == 0
    assert store.pop("missing", None) is None
    store.checkpoint()
    with pytest.raises(KeyError):
        _ = store["missing"]
    with pytest.raises(KeyError):
        store.pop("missing")
    with pytest.raises(RuntimeError, match="closed"):
        store["new"] = {"last_seen": 1.0}
    with pytest.raises(RuntimeError, match="closed"):
        del store["legacy"]


def test_sqlite_store_setup_failure_closes_connection(tmp_path, monkeypatch):
    db_path = tmp_path / "sessions.db"
    db_path.touch()
    connection = MagicMock()
    connection.execute.side_effect = sqlite3.OperationalError("setup failed")
    monkeypatch.setattr(auth_module.sqlite3, "connect", lambda *args, **kwargs: connection)

    before = get_runtime_metrics()["sqlite_failures_total"]
    with pytest.raises(sqlite3.OperationalError, match="setup failed"):
        SqliteSessionStore(str(db_path))

    connection.close.assert_called_once_with()
    assert get_runtime_metrics()["sqlite_failures_total"] == before + 1


def test_same_ip_session_limit_evicts_oldest_session(password_hash):
    manager = AuthManager(password_hash=password_hash, max_sessions=2)
    first = manager.login(PASSWORD, "::ffff:127.0.0.1")
    second = manager.login(PASSWORD, "127.0.0.1")
    third = manager.login(PASSWORD, "127.0.0.1")

    assert first not in manager.sessions
    assert second in manager.sessions
    assert third in manager.sessions


def test_failed_login_audit_state_is_bounded(password_hash):
    manager = AuthManager(password_hash=password_hash)
    manager._MAX_AUDIT_IPS = 2
    manager._audit_state = {
        "192.0.2.1": {"last_log_ts": 1.0, "suppressed_count": 0},
        "192.0.2.2": {"last_log_ts": 2.0, "suppressed_count": 0},
    }

    manager._audit_failed_login("192.0.2.3", "bad_password")

    assert set(manager._audit_state) == {"192.0.2.2", "192.0.2.3"}


def test_password_change_validation_and_bootstrap_cleanup_failure(
    tmp_path, monkeypatch, password_hash
):
    hash_file = tmp_path / "dashboard_secret"
    bootstrap_file = tmp_path / "dashboard_password.txt"
    write_secret_file_atomic(str(hash_file), password_hash + "\n")
    write_secret_file_atomic(str(bootstrap_file), PASSWORD + "\n")
    manager = AuthManager(
        password_hash=password_hash,
        password_hash_file=str(hash_file),
        generated_pw_file=str(bootstrap_file),
    )
    token = manager.login(PASSWORD, "127.0.0.1")

    assert manager.change_password(PASSWORD, "short").reason == "new_password_too_short"
    assert manager.change_password(PASSWORD, "x" * 257).reason == "new_password_too_long"
    assert (
        manager.change_password("wrong-current-password", "new-dashboard-password").reason
        == "invalid_current_password"
    )
    assert manager.change_password(PASSWORD, PASSWORD).reason == "password_unchanged"

    def fail_bootstrap_cleanup(_path):
        raise OSError("read-only directory")

    monkeypatch.setattr(auth_module, "_unlink_secret_file_durably", fail_bootstrap_cleanup)
    result = manager.change_password(PASSWORD, "new-dashboard-password")

    assert result.applied is True
    assert result.reason == "bootstrap_cleanup_failed"
    assert result.revoked_tokens == (token,)
    assert result.password_change_required is True
    assert bootstrap_file.exists()
    assert not manager.validate_token(token)
    assert verify_password("new-dashboard-password", hash_file.read_text().strip())


def test_stale_password_checksum_invalidates_every_session(password_hash):
    manager = AuthManager(password_hash=password_hash)
    stale = manager.login(PASSWORD, "192.0.2.1")
    other = manager.login(PASSWORD, "192.0.2.2")
    session = manager.sessions[stale]
    session["password_hash_checksum"] = "stale-checksum"
    manager.sessions[stale] = session

    assert not manager.validate_token(stale)
    assert stale not in manager.sessions
    assert other not in manager.sessions


def test_session_invalidation_tolerates_concurrent_disappearance(password_hash):
    class DisappearingSessions(dict):
        def __delitem__(self, token):
            if token == "gone":
                super().pop(token, None)
                raise KeyError(token)
            return super().__delitem__(token)

    manager = AuthManager(password_hash=password_hash)
    manager.sessions = DisappearingSessions(
        {
            "gone": {"last_seen": time.time()},
            "present": {"last_seen": time.time()},
        }
    )

    assert manager._invalidate_all_sessions() == ("gone", "present")
    assert manager.sessions == {}


@pytest.mark.asyncio
async def test_passive_websocket_closes_immediately_after_session_expiry(
    monkeypatch, password_hash
):
    manager = AuthManager(password_hash=password_hash, session_timeout=60)
    token = manager.login(PASSWORD, "127.0.0.1")
    session = manager.sessions[token]
    session["last_seen"] -= 61
    manager.sessions[token] = session
    plugin = SimpleNamespace(
        _auth=manager,
        config={"ws_session_revalidate_interval": 30},
    )
    ws = MagicMock()
    ws.closed = False
    ws.close = AsyncMock()
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(wsh.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(wsh, "_send_with_timeout", send)

    await wsh._revalidate_ws_session(ws, plugin, token)

    assert token not in manager.sessions
    send.assert_awaited_once()
    assert "Session expired" in send.await_args.args[1]
    ws.close.assert_awaited_once_with(code=4001, message=b"Session expired")


@pytest.mark.asyncio
async def test_password_rotation_handler_closes_every_revoked_socket(
    tmp_path, monkeypatch, password_hash
):
    hash_file = tmp_path / "dashboard_secret"
    write_secret_file_atomic(str(hash_file), password_hash + "\n")
    manager = AuthManager(password_hash=password_hash, password_hash_file=str(hash_file))
    tokens = {
        manager.login(PASSWORD, "192.0.2.1"),
        manager.login(PASSWORD, "192.0.2.2"),
    }
    plugin = SimpleNamespace(_auth=manager, _auth_executor=None)
    close_socket = AsyncMock()
    monkeypatch.setattr(wsh, "close_websockets_for_token", close_socket)
    request = _JsonRequest(
        plugin,
        {
            "current_password": PASSWORD,
            "new_password": "rotated-dashboard-password",
        },
    )

    response = await handle_change_password(request)

    assert response.status == 200
    assert len(manager.sessions) == 0
    assert {call.args[0] for call in close_socket.await_args_list} == tokens


@pytest.mark.asyncio
async def test_auth_pool_saturation_never_queues_work():
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    plugin = SimpleNamespace(_auth_executor=executor, _auth_slots=slots)
    work = MagicMock(return_value="should-not-run")
    try:
        admitted, result = await _run_auth_work(plugin, work)
    finally:
        slots.release()
        executor.shutdown(wait=True)

    assert admitted is False
    assert result is None
    work.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("slots", [None, object()])
async def test_auth_pool_missing_or_invalid_semaphore_fails_closed(slots):
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    plugin = SimpleNamespace(_auth_executor=executor, _auth_slots=slots)
    try:
        admitted, result = await _run_auth_work(plugin, lambda: "not-run")
    finally:
        executor.shutdown(wait=True)

    assert (admitted, result) == (False, None)


@pytest.mark.asyncio
async def test_auth_pool_acquire_and_executor_failures_return_unavailable():
    class BrokenAcquire:
        def acquire(self, *, blocking):
            raise RuntimeError("semaphore unavailable")

        def release(self):
            raise AssertionError("release must not run")

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        admitted, result = await _run_auth_work(
            SimpleNamespace(_auth_executor=executor, _auth_slots=BrokenAcquire()),
            lambda: "not-run",
        )
    finally:
        executor.shutdown(wait=True)
    assert (admitted, result) == (False, None)

    stopped_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    stopped_executor.shutdown(wait=True)
    slots = threading.BoundedSemaphore(1)
    admitted, result = await _run_auth_work(
        SimpleNamespace(_auth_executor=stopped_executor, _auth_slots=slots),
        lambda: "not-run",
    )
    assert (admitted, result) == (False, None)
    assert slots.acquire(blocking=False)
    slots.release()


@pytest.mark.asyncio
async def test_auth_worker_runtime_error_is_not_misreported_as_pool_saturation():
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    slots = threading.BoundedSemaphore(1)

    def fail_work():
        raise RuntimeError("credential store failed")

    try:
        with pytest.raises(RuntimeError, match="credential store failed"):
            await _run_auth_work(
                SimpleNamespace(_auth_executor=executor, _auth_slots=slots),
                fail_work,
            )
    finally:
        executor.shutdown(wait=True)

    assert slots.acquire(blocking=False)
    slots.release()


@pytest.mark.asyncio
async def test_auth_pool_release_failure_does_not_misreport_completed_work():
    class BrokenRelease:
        def acquire(self, *, blocking):
            return True

        def release(self):
            raise RuntimeError("semaphore bookkeeping failed")

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        admitted, result = await _run_auth_work(
            SimpleNamespace(_auth_executor=executor, _auth_slots=BrokenRelease()),
            lambda: "completed",
        )
    finally:
        executor.shutdown(wait=True)

    assert (admitted, result) == (True, "completed")


@pytest.mark.asyncio
async def test_login_endpoint_returns_503_when_auth_pool_is_saturated(password_hash):
    manager = AuthManager(password_hash=password_hash)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    plugin = SimpleNamespace(
        _auth=manager,
        _auth_executor=executor,
        _auth_slots=slots,
        config={"ssl": {}},
    )
    request = _JsonRequest(plugin, {"password": PASSWORD})
    try:
        response = await handle_login(request)
    finally:
        slots.release()
        executor.shutdown(wait=True)

    assert response.status == 503
    assert len(manager.sessions) == 0


@pytest.mark.asyncio
async def test_login_cookie_is_secure_behind_explicitly_trusted_https_proxy(monkeypatch):
    auth = SimpleNamespace(
        is_rate_limited=lambda _ip: False,
        get_retry_after=lambda _ip: 0,
        login=MagicMock(),
        password_change_required=False,
        force_secure_cookie=False,
        session_timeout=3600,
    )
    plugin = SimpleNamespace(
        _auth=auth,
        config={
            "ssl": {},
            "reverse_proxy": {
                "enabled": True,
                "trusted_networks": ["127.0.0.1/32"],
            },
        },
    )
    request = _JsonRequest(plugin, {"password": PASSWORD})
    request.headers = {"X-Forwarded-Proto": "https"}
    monkeypatch.setattr(
        "reticulumpi.builtin_plugins.web_dashboard.api._run_auth_work",
        AsyncMock(return_value=(True, "session-token")),
    )

    response = await handle_login(request)

    assert response.status == 200
    assert response.cookies["session"]["secure"] is True


def test_local_api_token_file_is_mode_0600_and_write_failure_is_fatal(
    tmp_path, monkeypatch, password_hash
):
    plugin = _plugin_for_token_tests(password_hash)
    assert plugin._load_or_create_local_api_token({"enabled": False}) is None

    token_file = tmp_path / "local_api.token"
    token = plugin._load_or_create_local_api_token({"enabled": True, "token_file": str(token_file)})
    assert token == token_file.read_text().strip()
    assert token_file.stat().st_mode & 0o777 == 0o600

    failed_file = tmp_path / "failed.token"

    def fail_write(*_args, **_kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(auth_module, "write_secret_file_atomic", fail_write)
    with pytest.raises(OSError, match="read-only"):
        plugin._load_or_create_local_api_token({"enabled": True, "token_file": str(failed_file)})
    assert not failed_file.exists()


@pytest.mark.asyncio
async def test_local_api_token_scope_is_loopback_read_only_and_route_limited(password_hash):
    local_token = "local-service-token-with-at-least-32-bytes"
    plugin = SimpleNamespace(
        _auth=AuthManager(password_hash=password_hash),
        _local_api_token=local_token,
        config={"local_api": {"enabled": True}},
    )
    middleware = auth_middleware_factory(plugin)

    async def ok_handler(_request):
        return aiohttp.web.Response(status=200)

    for index, path in enumerate(sorted(LOCALHOST_ALLOWED_PATHS)):
        remote = "::ffff:127.0.0.1" if index == 0 else "127.0.0.1"
        request = _MiddlewareRequest(path, local_token, remote=remote)
        response = await middleware(request, ok_handler)
        assert response.status == 200
        if path == "/api/version":
            assert LOCAL_API_KEY not in request
        else:
            assert request[LOCAL_API_KEY] is True

    for path, method, remote in (
        ("/ws/metrics", "GET", "127.0.0.1"),
        ("/api/status", "POST", "127.0.0.1"),
        ("/api/status", "GET", "192.0.2.1"),
    ):
        with pytest.raises(aiohttp.web.HTTPUnauthorized):
            await middleware(
                _MiddlewareRequest(path, local_token, method=method, remote=remote),
                ok_handler,
            )
